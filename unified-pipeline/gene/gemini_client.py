"""
Gemini 2.5 Flash wrapper with on-disk caching and structured JSON output.

Two entry points:
- `enrich_moa_batch(strings)`   — MOA normalization, batched.
- `enrich_gene(gene, aliases, broad_cancers)` — per-gene detailed conditions.

Both use the same underlying REST call and the same sha256 cache layout:

    cache/<name>.json = { "<sha256>": <response_dict>, ... }

Cache keys are content-derived, so identical inputs across runs always
hit cache. Deleting the cache file forces a fresh fetch.
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from typing import Dict, List, Optional

import requests

# Reuse the pipeline's env-loading pattern.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_HERE)
sys.path.insert(0, _PIPELINE_DIR)
try:
    from config import GEMINI_API_KEY  # type: ignore
except Exception:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

CACHE_DIR = os.path.join(_HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

class JsonCache:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        self._dirty = False
        # Guard concurrent get/set/flush from multiple threads.
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            return self.data.get(key)

    def set(self, key: str, value: dict) -> None:
        with self._lock:
            self.data[key] = value
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # Unique temp file per thread so concurrent flushes don't race
            # on os.replace of a shared .tmp path.
            tmp = f"{self.path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
            self._dirty = False


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Low-level REST call
# --------------------------------------------------------------------------

def _call_gemini(prompt: str, max_retries: int = 3,
                 timeout: int = 120) -> Optional[dict]:
    """Call Gemini 2.5 Flash with JSON response mode. Returns parsed dict."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it to conquest-pipeline-v2/.env"
        )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            text = ""
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    text += part.get("text", "")
            if not text:
                return None
            # Strip accidental ```json fences.
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except requests.HTTPError:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------
# MOA enrichment
# --------------------------------------------------------------------------

_MOA_PROMPT = """You are normalizing pharmaceutical mechanism-of-action (MOA) strings.

For each input string, return a JSON object describing what the MOA means.

Schema for each item:
{
  "input": "<original string>",
  "targets": ["<molecular target, e.g. EGFR, PD-1, KRAS>", ...],
  "hgnc_candidates": ["<best guess HGNC symbol for each target>", ...],
  "mutations": ["<mutation context like G12C, V600E, L858R>", ...],
  "action": "inhibitor" | "antagonist" | "agonist" | "modulator" | "degrader" | "blocker" | "activator" | "antibody" | "targeted" | null,
  "drug_class": "kinase_inhibitor" | "monoclonal_antibody" | "bispecific_antibody" | "antibody_drug_conjugate" | "car_t_therapy" | "chemotherapy" | "radiopharmaceutical" | "cancer_vaccine" | "hormone_therapy" | "small_molecule_inhibitor" | "other",
  "pathway": ["<pathway name>", ...],
  "is_class_only": true | false,
  "is_oncology_relevant": true | false
}

Rules:
- If the string describes a non-oncology mechanism (e.g. "Reduces fever and relieves pain"), set is_oncology_relevant=false and leave targets empty.
- If the string names a class without a specific target (e.g. "Anthracycline"), set is_class_only=true and leave targets/hgnc_candidates empty.
- targets should be the specific molecular target(s) — proteins, receptors, genes. Do NOT include generic words like "cancer cell" or "tumor".
- hgnc_candidates should be a best-guess HGNC gene symbol for each target, in the same order. Use null for non-gene targets.
- Split multi-target expressions (e.g. "CDK4/6" → ["CDK4","CDK6"]).

Return a JSON object with key "results" containing an array, one entry per input.

Inputs:
{items}
"""


def enrich_moa_batch(strings: List[str], cache: Optional[JsonCache] = None,
                     batch_size: int = 20) -> Dict[str, dict]:
    """Normalize a list of MOA strings via Gemini. Returns {input: response}."""
    cache = cache or JsonCache(os.path.join(CACHE_DIR, "moa_gemini_cache.json"))
    out: Dict[str, dict] = {}

    # Dedupe and check cache first.
    uniq = list(dict.fromkeys(s for s in strings if s and isinstance(s, str)))
    pending: List[str] = []
    for s in uniq:
        cached = cache.get(_hash(s))
        if cached is not None:
            out[s] = cached
        else:
            pending.append(s)

    if not pending:
        return out

    for i in range(0, len(pending), batch_size):
        chunk = pending[i : i + batch_size]
        items_json = json.dumps(chunk, indent=2)
        prompt = _MOA_PROMPT.replace("{items}", items_json)

        try:
            resp = _call_gemini(prompt)
        except Exception as e:
            print(f"[gemini] batch {i//batch_size} failed: {e}")
            continue

        if not resp:
            continue
        results = resp.get("results", [])
        # Align by order — Gemini should return them in order, but also
        # trust the `input` field when present.
        for idx, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            key = item.get("input") or (chunk[idx] if idx < len(chunk) else None)
            if not key:
                continue
            out[key] = item
            cache.set(_hash(key), item)
        cache.flush()
        print(f"[gemini] moa batch {i//batch_size + 1}/{(len(pending)+batch_size-1)//batch_size} "
              f"({len(chunk)} items) → {len(results)} results")

    return out


# --------------------------------------------------------------------------
# Gene enrichment
# --------------------------------------------------------------------------

_GENE_PROMPT = """You are enriching an oncology gene dictionary.

For the gene "{gene}" (aliases: {aliases}), known broad cancer types: {broad_cancers}.

Return a JSON object with these keys:
{{
  "gene": "{gene}",
  "detailed_conditions": ["<specific cancer subtype, e.g. 'Non-Small Cell Lung Cancer (NSCLC)', 'KRAS G12C-mutant NSCLC', 'Pancreatic Ductal Adenocarcinoma'>", ...],
  "mutation_hotspots": ["<commonly targeted mutations like G12C, V600E, L858R, T790M, exon 19 del>", ...],
  "pathway": ["<major pathway, e.g. MAPK, PI3K/AKT, RAS/RAF/MEK/ERK>", ...],
  "role": "oncogene" | "tumor_suppressor" | "fusion_partner" | "other",
  "notes": "<one sentence on clinical relevance>"
}}

Rules:
- detailed_conditions should be MORE specific than the broad cancer types above — list oncotree-style subtypes or mutation-specific contexts where this gene is clinically actionable.
- Be precise and clinically accurate. Temperature is 0.
- Return ONLY the JSON object, no prose.
"""


# --------------------------------------------------------------------------
# Target → HGNC resolution (supplement expansion)
# --------------------------------------------------------------------------

_TARGET_HGNC_PROMPT = """You are resolving molecular target names to HGNC gene symbols.

For each input token, return an object describing the best-guess HGNC symbol.

Schema for each item:
{
  "input": "<original token>",
  "hgnc_symbol": "<approved HGNC symbol>" | null,
  "is_gene": true | false,
  "confidence": 0.0 - 1.0,
  "notes": "<one phrase on what this is, if not a gene>"
}

Rules:
- is_gene=true only if the token names a specific protein-coding human gene.
- Set is_gene=false for: pathway names (e.g. "NF-kB pathway", "MAPK"), complexes
  (e.g. "CDK4/6 complex"), non-human targets, DNA-level processes, cell types,
  or non-gene biological entities.
- hgnc_symbol must be the approved HGNC symbol (e.g. "ERBB2" not "HER2",
  "CYP19A1" not "Aromatase", "PDCD1" not "PD-1"). Use null when is_gene=false
  or when uncertain.
- Output the array under key "results". Temperature is 0; be strict.

Inputs:
{items}
"""


def resolve_targets_to_hgnc(tokens: List[str], cache: Optional[JsonCache] = None,
                            batch_size: int = 40) -> Dict[str, dict]:
    """Ask Gemini to map target tokens → HGNC symbols. Returns {token: response}."""
    cache = cache or JsonCache(
        os.path.join(CACHE_DIR, "target_hgnc_cache.json")
    )
    out: Dict[str, dict] = {}

    uniq = list(dict.fromkeys(t for t in tokens if t and isinstance(t, str)))
    pending: List[str] = []
    for t in uniq:
        cached = cache.get(_hash(t.lower()))
        if cached is not None:
            out[t] = cached
        else:
            pending.append(t)

    if not pending:
        return out

    for i in range(0, len(pending), batch_size):
        chunk = pending[i : i + batch_size]
        prompt = _TARGET_HGNC_PROMPT.replace(
            "{items}", json.dumps(chunk, indent=2)
        )
        try:
            resp = _call_gemini(prompt)
        except Exception as e:
            print(f"[gemini] target-hgnc batch {i//batch_size} failed: {e}")
            continue
        if not resp:
            continue
        results = resp.get("results") if isinstance(resp, dict) else resp
        if not isinstance(results, list):
            continue
        for idx, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            token = item.get("input") or (chunk[idx] if idx < len(chunk) else None)
            if not token:
                continue
            out[token] = item
            cache.set(_hash(token.lower()), item)
        cache.flush()
        print(f"[gemini] target-hgnc batch {i//batch_size + 1}/"
              f"{(len(pending)+batch_size-1)//batch_size} ({len(chunk)} items)")

    return out


def enrich_gene(gene: str, aliases: List[str], broad_cancers: List[str],
                cache: Optional[JsonCache] = None) -> Optional[dict]:
    """Enrich one gene via Gemini. Returns the response dict or None."""
    cache = cache or JsonCache(os.path.join(CACHE_DIR, "gene_gemini_cache.json"))
    key = _hash(f"{gene}|{','.join(sorted(aliases))}")
    cached = cache.get(key)
    if cached is not None:
        return cached

    prompt = _GENE_PROMPT.format(
        gene=gene,
        aliases=", ".join(aliases[:10]) or gene,
        broad_cancers=", ".join(broad_cancers) or "unknown",
    )
    try:
        resp = _call_gemini(prompt)
    except Exception as e:
        print(f"[gemini] gene {gene} failed: {e}")
        return None

    if resp:
        cache.set(key, resp)
        cache.flush()
    return resp
