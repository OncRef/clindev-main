"""
DrugEnricher — Gemini 2.5 Flash + Google Search grounding.

Enriches unknown drugs with generic name, MOA, class, target, company,
brand/alternative names, and approval info. Caches results in-memory and
persists a checkpoint to disk every 100 calls so long runs can be resumed.
"""

import json
import os
import re
import time
import requests
from datetime import datetime

from config import GEMINI_API_KEY, GEMINI_MODEL, LOG_DIR


PLACEHOLDER_VALUES = {"", "...", "....", ".....", "n/a", "na", "none", "null",
                      "not applicable", "not available", "tbd", "unknown",
                      "undefined", "placeholder"}

# Fields where a literal placeholder value indicates Gemini echoed the
# prompt template instead of producing a real answer.
_VALIDATED_FIELDS = ("generic_name", "short_moa", "long_moa", "target",
                     "drug_class", "company")


def _looks_like_placeholder(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in PLACEHOLDER_VALUES
    return False


def _is_template_echo(parsed):
    """Detect responses where Gemini returned the prompt template unchanged.

    A real enrichment must have a non-placeholder generic_name AND at least
    one of short_moa / long_moa / drug_class / target filled in. If all the
    validated fields are placeholders, the model didn't actually answer.
    """
    if not isinstance(parsed, dict):
        return True
    gn = parsed.get("generic_name")
    if _looks_like_placeholder(gn):
        return True
    real_fields = sum(
        1 for f in _VALIDATED_FIELDS[1:]   # skip generic_name (checked above)
        if not _looks_like_placeholder(parsed.get(f))
    )
    return real_fields == 0

GEMINI_PROMPT = """You are a pharmaceutical and oncology expert. For the drug "{drug_name}", provide:

1. generic_name: INN (International Nonproprietary Name). Keep compound code if no INN exists.
2. short_moa: Brief mechanism of action in 2-8 words
   (e.g., "PD-1 inhibitor", "EGFR tyrosine kinase inhibitor", "alkylating agent")
3. long_moa: Official mechanism of action (1-3 sentences)
4. target: List the molecular target (e.g., "PD-1", "EGFR", "CD19", "tubulin") if appropriate.
5. drug_class: Classify into exactly ONE of these cancer treatment categories (do not invent categories):
   - chemotherapy
   - radiation_therapy
   - hormone_therapy
   - targeted_therapy
   - immunotherapy
   - stem_cell_transplant
   - photodynamic_therapy
   - hyperthermia
   - gene_therapy
   - radioligand_therapy
   - ablation_therapy
   - nanomedicine
   - oncolytic_virus_therapy
   - cancer_vaccine
   - imaging_agent
   - supportive_care
   - other
   - not_cancer_related
6. company: Pharmaceutical company that developed this drug
7a. brand_names: List the brand name(s) if any
7b. alternative_names: List any nicknames or alternative names (development codes, abbreviations, etc.)
8. is_approved: true/false for FDA approval
9. approved_indications: List of FDA-approved cancer indications (if any)
10. is_drug: true/false — false ONLY for things like "placebo", "saline",
    "standard of care", "surgery", "observation". Compound codes, CAR-T cells,
    imaging tracers, cancer vaccines, and radioligand therapies ARE drugs.

Return ONLY valid JSON, no markdown:
{{"drug_name": "{drug_name}", "generic_name": "...", "short_moa": "...", "long_moa": "...", "target": "...", "drug_class": "...", "company": "...", "brand_names": ["..."], "alternative_names": ["..."], "is_approved": false, "is_drug": true, "approved_indications": []}}"""


def _gemini_call(drug_name):
    if not GEMINI_API_KEY:
        return None

    prompt = GEMINI_PROMPT.format(drug_name=drug_name)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

    try:
        resp = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0, "responseMimeType": "text/plain"},
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if "text" in part:
                    text += part["text"]
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
        if _is_template_echo(parsed):
            return {"drug_name": drug_name, "error": "placeholder_response", "is_drug": None}
        return parsed

    except Exception as e:
        return {"drug_name": drug_name, "error": str(e), "is_drug": None}


class DrugEnricher:
    def __init__(self, checkpoint_dir=None):
        self.checkpoint_dir = checkpoint_dir or LOG_DIR
        self._checkpoint_path = os.path.join(self.checkpoint_dir, "enrichment_checkpoint.json")
        self._cache = {}
        self._stats = {"processed": 0, "enriched": 0, "errors": 0, "not_drug": 0}

    def load_checkpoint(self):
        if os.path.exists(self._checkpoint_path):
            with open(self._checkpoint_path) as f:
                self._cache = json.load(f)
            print(f"[D]   enricher: resumed from checkpoint ({len(self._cache)} cached)")

    def _save_checkpoint(self):
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        with open(self._checkpoint_path, "w") as f:
            json.dump(self._cache, f, default=str)

    def cleanup_checkpoint(self):
        if os.path.exists(self._checkpoint_path):
            os.remove(self._checkpoint_path)

    def enrich(self, drug_name):
        key = drug_name.lower().strip()
        if key in self._cache:
            return self._cache[key]

        # Short-circuit when no API key is configured: mark as no_key, cache,
        # and skip the rate-limit sleep. Without this a 2400-trial run spends
        # ~hours on sleeps for calls that never happen.
        if not GEMINI_API_KEY:
            self._cache[key] = {"drug_name": drug_name, "error": "no_api_key", "is_drug": None}
            self._stats["processed"] += 1
            self._stats["errors"] += 1
            return self._cache[key]

        result = _gemini_call(drug_name)
        self._stats["processed"] += 1

        if result and not result.get("error"):
            self._cache[key] = result
            if result.get("is_drug") is False:
                self._stats["not_drug"] += 1
            else:
                self._stats["enriched"] += 1
        else:
            self._cache[key] = result or {"drug_name": drug_name, "error": "no_response", "is_drug": None}
            self._stats["errors"] += 1

        if self._stats["processed"] % 100 == 0:
            self._save_checkpoint()
            print(f"[D]   enricher checkpoint: {self._stats['processed']} processed, "
                  f"{self._stats['enriched']} enriched, {self._stats['errors']} errors")

        time.sleep(0.5)
        return self._cache[key]

    def save_results(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "metadata": {
                    "timestamp": datetime.now().isoformat(),
                    "stats": self._stats,
                    "model": f"{GEMINI_MODEL} + google_search grounding",
                },
                "results": self._cache,
            }, f, indent=2, default=str)

    @property
    def stats(self):
        return self._stats.copy()
