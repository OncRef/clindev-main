"""
Drug name normalizer — splits combo regimens, strips dosages/formulations,
and drops non-drug interventions.
"""

import re

COMBO_SPLIT = re.compile(
    r"\s*(?:\+|/|;|；|,|\band\b|\bor\b|\bwith\b|\bplus\b|\bcombined\s+with\b)\s*",
    re.IGNORECASE,
)

DOSAGE_RE = re.compile(
    r"\b\d+\.?\d*\s*(?:mg|mcg|µg|g|ml|mL|IU|units?|%|kg|mmol|umol|nmol|cc)\b",
    re.IGNORECASE,
)

FORMULATION_RE = re.compile(
    r"\b(?:tablet|capsule|injection|infusion|solution|suspension|oral|intravenous|"
    r"subcutaneous|intramuscular|topical|transdermal|rectal|nasal|ophthalmic|"
    r"ointment|cream|gel|patch|suppository|aerosol|inhaler|"
    r"iv\b|im\b|sc\b|po\b|bid\b|tid\b|qd\b|qid\b|prn\b|"
    r"placebo|cohort\s+\d+|arm\s+[a-z0-9]|group\s+\d+|dose\s+level)\b",
    re.IGNORECASE,
)

NON_DRUGS = {
    "placebo", "saline", "normal saline", "standard of care",
    "observation", "watchful waiting", "no intervention", "sham",
    "control", "best supportive care", "water", "sterile water",
    "surgery", "radiation", "radiotherapy", "physical therapy",
    "physiotherapy", "counseling", "diet", "exercise", "device",
    "questionnaire", "survey", "blood draw", "biopsy", "screening",
    "follow-up", "routine care", "usual care", "active surveillance",
    "monitoring", "behavioral", "psychotherapy", "cognitive",
    "mindfulness", "acupuncture", "massage", "yoga", "meditation",
    # Observed in 2026-04 test run — intervention metadata that slipped
    # through because it was registered under intervention type DRUG/BIOLOGICAL
    # but isn't actually a drug name.
    "chemotherapy", "the chemotherapy", "chemo", "chemotherapies",
    "adjuvant endocrine therapy", "endocrine therapy",
    "acute normovolemic hemodilution", "normovolemic hemodilution",
    "biological", "biologic", "drug", "drugs",
    "relapsed", "prophylaxis", "prophylaxis a", "prophylaxis b",
    "local equivalent", "treatment", "the treatment",
    "maintenance", "induction", "consolidation",
    "active treatment", "experimental arm", "experimental",
    "endocrine", "hormonal", "hormone", "immune",
}

# Trademark / registered / copyright symbols that appear inline as "(R)" or "(TM)"
# and confuse trailing-paren stripping. Stripped before the main clean pass.
TRADEMARK_RE = re.compile(r"\s*\(\s*(?:r|tm|c|©|®|™)\s*\)", re.IGNORECASE)

# Match a single trailing parenthetical OR bracketed group (greedy for nested
# content). Used iteratively so `Pembrolizumab (Keytruda(R))` → `Pembrolizumab`.
TRAILING_GROUP_RE = re.compile(r"\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$")

# Trailing qualifier words that describe how/when a drug is given rather than
# what it is. These should be stripped after the main name.
TRAILING_QUALIFIER_RE = re.compile(
    r"\s+\b(?:monotherapy|therapy|maintenance|induction|consolidation|"
    r"treatment|arm|cohort|regimen|combination|infusion|injection)\b\s*$",
    re.IGNORECASE,
)

# Leading qualifier words that describe therapeutic intent rather than drug name.
LEADING_QUALIFIER_RE = re.compile(
    r"^\s*(?:the|adjuvant|neoadjuvant|prophylactic|prophylaxis|preventive|"
    r"preventative|experimental|active|intravenous|oral|inhaled|topical|"
    r"intrathecal|subcutaneous|low[\s\-]dose|high[\s\-]dose|standard[\s\-]dose)\s+",
    re.IGNORECASE,
)


def balance_parens(text):
    """Strip unmatched parens/brackets left over from combo splitting."""
    result = text
    for open_c, close_c in [("(", ")"), ("[", "]"), ("{", "}")]:
        chars = list(result)
        to_remove = []
        depth = 0
        for i, c in enumerate(chars):
            if c == open_c:
                depth += 1
            elif c == close_c:
                if depth > 0:
                    depth -= 1
                else:
                    to_remove.append(i)
        depth = 0
        for i in range(len(chars) - 1, -1, -1):
            if chars[i] == close_c:
                depth += 1
            elif chars[i] == open_c:
                if depth > 0:
                    depth -= 1
                else:
                    to_remove.append(i)
        for i in sorted(set(to_remove), reverse=True):
            chars[i] = ""
        result = "".join(chars)
    return result


def clean_drug_name(raw_name):
    name = raw_name.strip()
    if not name:
        return None
    name = balance_parens(name)
    name = TRADEMARK_RE.sub("", name)
    name = DOSAGE_RE.sub("", name)
    name = FORMULATION_RE.sub("", name)
    name = re.sub(r"\([^)]*(?:mg|mcg|ml|dose|arm|cohort|group|level)[^)]*\)", "", name, flags=re.IGNORECASE)

    # Iteratively strip trailing parenthetical / bracketed annotations:
    #   "Pembrolizumab (Keytruda(R))" → "Pembrolizumab"
    #   "Eflornithine (DFMO)"        → "Eflornithine"
    #   "Mupirocin [treatment]"      → "Mupirocin"
    # The loop handles nested groups and multiple trailing groups in a row.
    for _ in range(5):
        stripped = TRAILING_GROUP_RE.sub("", name)
        if stripped == name:
            break
        name = stripped

    # Strip trailing qualifier words like "monotherapy" / "maintenance" that
    # appear after the drug name, then a second pass of trailing-group strip
    # in case the qualifier hid a paren ("Epcoritamab (maintenance)").
    name = TRAILING_QUALIFIER_RE.sub("", name)
    for _ in range(3):
        stripped = TRAILING_GROUP_RE.sub("", name)
        if stripped == name:
            break
        name = stripped

    # Strip leading qualifier words like "the", "adjuvant", "prophylactic".
    name = LEADING_QUALIFIER_RE.sub("", name)

    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^[\s,;:+\-–]+|[\s,;:+\-–]+$", "", name).strip()
    name = name.strip("\"'")
    if not name or len(name) < 2:
        return None
    if name.lower() in NON_DRUGS:
        return None
    return name


def split_and_clean(intervention_name):
    parts = COMBO_SPLIT.split(intervention_name)
    cleaned, seen = [], set()
    for part in parts:
        name = clean_drug_name(part)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            cleaned.append(name)
    return cleaned


def extract_drugs_from_trial(trial):
    """Return the unique list of cleaned drug names for a flattened trial."""
    names = trial.get("intervention_names", [])
    types = trial.get("intervention_types", [])
    all_drugs, seen = [], set()
    for name, itype in zip(names, types):
        if itype not in ("DRUG", "BIOLOGICAL"):
            continue
        for drug in split_and_clean(name):
            if drug.lower() not in seen:
                seen.add(drug.lower())
                all_drugs.append(drug)
    return all_drugs
