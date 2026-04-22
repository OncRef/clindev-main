"""
Condition classifier — maps a condition string to broad cancer type(s).

Tier ladder (from the reference v1 pipeline, steps 3 → 5b):
  tier_1            exact match in extracted_cancer_to_broad
  tier_1.5_recycle  cached Gemini result in llm_classified_conditions
  tier_2            exact OncoTree match
  tier_2_substr     longest OncoTree substring match in the core phrase
  tier_3            keyword/subtype scoring over broad_cancer_mapping
  llm (step 4)      Gemini fallback for conditions we couldn't classify
  title_oncotree / title_keyword / title_llm (step 5a/5b)
                    if no broad cancer came out of any condition, classify
                    from the trial title instead (regex, then Gemini)

Gemini 2.5 Flash + Google Search grounding replaces OpenAI from v1.
"""

import csv
import json
import os
import re
import time

import requests

from config import (
    EXTRACTED_CANCER_FILE, ONCOTREE_MAPPING_FILE, BROAD_CANCER_MAPPING_FILE,
    LLM_CONDITIONS_CACHE_FILE, GEMINI_API_KEY, GEMINI_MODEL,
)


CANCER_WORDS = re.compile(
    r"\b(?:cancer|carcinoma|tumor|tumou?r|neoplasm|malignant|malignancy|sarcoma|lymphoma|"
    r"leukemia|leukaemia|myeloma|blastoma|glioma|mesothelioma|adenoma|melanoma|"
    r"metastat|carcinoid|dysplasia|neoplasia|oncolog)\b",
    re.IGNORECASE,
)

# A title/condition that loudly mentions a benign or non-cancerous condition
# should not be classified as a cancer trial even if a substring incidentally
# matches an oncotree node. Without this guard, "Benign Prostatic Hyperplasia"
# was flagged as a Solid Tumor via title_oncotree substring matching.
BENIGN_INDICATORS = re.compile(
    r"\b(?:benign\s+prostatic\s+hyperplasia|"
    r"benign|non[-\s]?malignant|non[-\s]?cancerous|hyperplasia|"
    r"healthy\s+(?:volunteer|subject|control)|"
    r"normal\s+(?:tissue|control)|"
    r"endometriosis|"
    r"polycystic\s+ovary|"
    r"uterine\s+fibroid)\b",
    re.IGNORECASE,
)


def _is_clearly_benign(text):
    if not text:
        return False
    if BENIGN_INDICATORS.search(text) and not CANCER_WORDS.search(text):
        return True
    return False


def load_classifier_config():
    cfg = {}

    with open(EXTRACTED_CANCER_FILE) as f:
        cfg["ec_lookup"] = {k.lower(): v for k, v in json.load(f).get("mapping", {}).items()}

    cfg["oncotree_broad"] = {}
    with open(ONCOTREE_MAPPING_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            code = (row.get("code") or "").strip()
            bc = (row.get("user_broad_cancer") or "").strip()
            if name and bc:
                cfg["oncotree_broad"][name.lower()] = {"code": code, "broad_cancer": bc}

    cfg["broad_cancer_kw"] = []
    with open(BROAD_CANCER_MAPPING_FILE, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader); next(reader)  # skip two header rows
        for row in reader:
            if not row or not row[0].strip():
                continue
            keywords = [k.strip().lower() for k in row[1].split(",") if k.strip()] if len(row) > 1 else []
            subtypes = [s.strip().lower() for s in row[4].split(",") if s.strip()] if len(row) > 4 else []
            cfg["broad_cancer_kw"].append({
                "name": row[0].strip(),
                "keywords": keywords,
                "keyword_regexes": [re.compile(r"\b" + re.escape(k) + r"\b", re.IGNORECASE) for k in keywords],
                "subtypes": subtypes,
            })

    if os.path.exists(LLM_CONDITIONS_CACHE_FILE):
        with open(LLM_CONDITIONS_CACHE_FILE) as f:
            cfg["llm_conditions"] = json.load(f).get("mapping", {})
    else:
        cfg["llm_conditions"] = {}

    cfg["_llm_dirty"] = False
    return cfg


def save_llm_conditions_cache(cfg):
    if not cfg.get("_llm_dirty"):
        return
    from datetime import datetime
    with open(LLM_CONDITIONS_CACHE_FILE, "w") as f:
        json.dump({
            "version": "2.0",
            "last_updated": datetime.now().isoformat(),
            "total_entries": len(cfg["llm_conditions"]),
            "mapping": cfg["llm_conditions"],
        }, f, indent=2, default=str)
    cfg["_llm_dirty"] = False


def classify_condition(original, core, cfg):
    """Return (broad_cancers, tier) or None (unclassified — caller should try LLM)."""
    for key in (original.lower().strip(), core.lower().strip()):
        if key in cfg["ec_lookup"]:
            return cfg["ec_lookup"][key], "tier_1"

    for key in (original.lower().strip(), core.lower().strip()):
        if key in cfg["llm_conditions"]:
            bc = cfg["llm_conditions"][key]
            if bc and bc != "UNCLASSIFIABLE":
                return ([bc] if isinstance(bc, str) else bc), "tier_1.5_recycle"
            return [], "unclassifiable"

    # Refuse substring matching for clearly-benign condition strings — they
    # incidentally overlap oncotree node names and produce false cancer tags.
    if _is_clearly_benign(original) or _is_clearly_benign(core):
        return [], "unclassifiable"

    core_lower = core.lower().strip()
    if core_lower in cfg["oncotree_broad"]:
        return [cfg["oncotree_broad"][core_lower]["broad_cancer"]], "tier_2"

    best, best_len = None, 0
    for name, entry in cfg["oncotree_broad"].items():
        if len(name) >= 4 and name in core_lower and len(name) > best_len:
            best, best_len = entry, len(name)
    if best:
        return [best["broad_cancer"]], "tier_2_substr"

    has_cancer = bool(CANCER_WORDS.search(original.lower()))
    scores = []
    for bc in cfg["broad_cancer_kw"]:
        score = 0
        for st in bc["subtypes"]:
            if st and len(st) >= 4 and (st in core_lower or core_lower in st):
                score += 3
                break
        for kw_re in bc["keyword_regexes"]:
            if kw_re.search(core_lower):
                kw = kw_re.pattern.replace(r"\b", "").replace("(?i)", "")
                if len(kw) <= 6 and not has_cancer:
                    continue
                score += 1
        if score > 0:
            scores.append((bc["name"], score))
    if scores:
        scores.sort(key=lambda x: -x[1])
        return [scores[0][0]], "tier_3"

    return None


def classify_from_title(title, cfg):
    """Step 5a — regex/keyword classification of the trial title."""
    if not title:
        return None
    tl = title.lower()

    # Refuse to classify titles that loudly describe a benign / non-cancer
    # condition. Substring matching against oncotree (next block) is too
    # eager — it tagged "Benign Prostatic Hyperplasia" as Solid Tumors via
    # an incidental oncotree-name overlap.
    if _is_clearly_benign(tl):
        return None

    best, best_len = None, 0
    for name, entry in cfg["oncotree_broad"].items():
        if len(name) >= 4 and name in tl and len(name) > best_len:
            best, best_len = entry, len(name)
    if best:
        # Require a real cancer keyword somewhere in the title. Without this,
        # any title containing a noun like "prostate", "lung", "breast" gets
        # mapped to a cancer broad-bucket regardless of whether the trial is
        # actually oncology.
        if CANCER_WORDS.search(tl):
            return [best["broad_cancer"]], "title_oncotree"

    has_cancer = bool(CANCER_WORDS.search(tl))
    scores = []
    for bc in cfg["broad_cancer_kw"]:
        score = 0
        for st in bc["subtypes"]:
            if st and len(st) >= 4 and st in tl:
                score += 3
                break
        for kw_re in bc["keyword_regexes"]:
            if kw_re.search(tl):
                kw = kw_re.pattern.replace(r"\b", "").replace("(?i)", "")
                if len(kw) <= 6 and not has_cancer:
                    continue
                score += 1
        if score > 0:
            scores.append((bc["name"], score))
    if scores:
        scores.sort(key=lambda x: -x[1])
        return [scores[0][0]], "title_keyword"

    return None


# === Gemini calls ===============================================================

def _gemini_json(prompt_text, timeout=30):
    """Call Gemini 2.5 Flash with grounded search; parse the JSON response."""
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        resp = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt_text}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0, "responseMimeType": "text/plain"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = ""
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "text" in part:
                    text += part["text"]
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        print(f"    [C] Gemini error: {e}")
        return None


def llm_classify_conditions(condition_texts, cfg):
    """Step 4 — Gemini fallback for a batch of unclassified condition strings.

    Returns {condition_text: broad_cancer_or_UNCLASSIFIABLE}.
    """
    if not condition_texts:
        return {}

    bc_names = sorted(bc["name"] for bc in cfg["broad_cancer_kw"])
    bc_list = "\n".join(f"  - {n}" for n in bc_names)

    prompt = (
        "Classify each clinical-trial condition string into exactly one of these "
        f"broad cancer categories (or UNCLASSIFIABLE if none fit):\n{bc_list}\n"
        "  - UNCLASSIFIABLE\n\n"
        f"Conditions:\n{json.dumps(condition_texts)}\n\n"
        'Return ONLY a valid JSON array: [{"condition": "...", "broad_cancer": "..."}]'
    )

    time.sleep(0.3)
    results = _gemini_json(prompt)
    if not isinstance(results, list):
        return {}
    return {r.get("condition", ""): r.get("broad_cancer", "UNCLASSIFIABLE") for r in results if isinstance(r, dict)}


def llm_classify_title(title, cfg):
    """Step 5b — Gemini fallback when no broad cancer came out of any condition."""
    if not title:
        return None
    bc_names = sorted(bc["name"] for bc in cfg["broad_cancer_kw"])
    bc_list = "\n".join(f"  - {n}" for n in bc_names)
    prompt = (
        "Extract the cancer condition from this clinical-trial title and classify it "
        f"into exactly one of:\n{bc_list}\n  - UNCLASSIFIABLE\n\n"
        f"Title: {title}\n\n"
        'Return ONLY valid JSON: {"title_condition": "...", "broad_cancer": "..."}'
    )
    time.sleep(0.3)
    return _gemini_json(prompt)
