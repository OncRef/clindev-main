"""
Stage 1 — Deterministic MOA parser.

Parses free-text `short_moa` and `drug_class` strings from drug_master.json
into a structured form:

    {
        "raw": "KRAS G12C inhibitor",
        "targets": ["KRAS"],
        "mutations": ["G12C"],
        "action": "inhibitor",
        "drug_class_hint": "small_molecule_inhibitor",
        "canonical_key": "kras_g12c_inhibitor",
    }

Returns None for strings that don't match a known pattern — those are
handed off to Stage 2 (Gemini).
"""

import re
from typing import Optional, Dict, List


# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

ACTION_WORDS = (
    "inhibitor", "antagonist", "agonist", "modulator", "degrader",
    "blocker", "activator", "binder", "disruptor", "targeting",
    "antibody", "antibodies",
)

# "Inhibits X, does Y" — verb-form prefix. Captures the target phrase
# following an action verb when no suffix-form match is found.
VERB_ACTIONS = {
    "inhibits": "inhibitor",
    "blocks": "blocker",
    "antagonizes": "antagonist",
    "activates": "activator",
    "stimulates": "agonist",
    "degrades": "degrader",
    "modulates": "modulator",
    "targets": "targeted",
    "binds": "binder",
    "disrupts": "disruptor",
}

VERB_RE = re.compile(
    r"^\s*(" + "|".join(VERB_ACTIONS.keys()) + r")\s+(.+?)(?:[,.;]|$)", re.I
)

# Bare class words — no target, no action suffix, just a class name.
BARE_CLASS_WORDS = {
    "serm", "serd", "tki", "mab", "adc", "protac",
    "anthracycline", "anthracyclines", "anthracycline antibiotic",
    "bisphosphonate", "bisphosphonates", "cytokine", "cytokines",
    "chemotherapy", "chemotherapeutic", "immunotherapy", "vaccine",
    "radiopharmaceutical", "antimetabolite", "alkylating agent",
    "dna alkylating agent", "dna hypomethylating agent",
    "hormone therapy", "taxane", "taxanes", "platinum",
    "platinum compound", "platinum agent", "microtubule inhibitor",
    "microtubule stabilizer", "folic acid analog", "folate analog",
    "nucleoside analog", "purine analog", "pyrimidine analog",
    "corticosteroid", "steroid", "glucocorticoid",
}

# Anything ending in one of these is a "class-only" drug_class, not a MOA.
# Gene-family descriptors like "parp inhibitor", "hdac inhibitor",
# "topoisomerase i inhibitor" are deliberately NOT listed here — they flow
# through the normal target-extraction path so Gemini's supplement expansion
# can propose HGNC symbols (PARP1, HDAC1, TOP1, ...). Whether those genes
# end up linked depends on whether they exist in New_Gene_updated.xlsx.
CLASS_ONLY_MARKERS = {
    "monoclonal antibody", "bispecific antibody", "bispecific t-cell engager",
    "antibody-drug conjugate", "adc", "car-t cell therapy", "car t-cell therapy",
    "cd19-directed car-t cell therapy", "cancer vaccine", "vaccine",
    "radiopharmaceutical", "alkylating agent", "chemotherapy",
    "antimetabolite", "kinase inhibitor", "multikinase inhibitor",
    "multi-kinase inhibitor", "small molecule", "small molecule inhibitor",
    "cell therapy", "gene therapy", "oncolytic virus", "immunotherapy",
    "hormone therapy", "targeted therapy", "radioligand therapy",
    "bispecific",
}

# Sentinel / junk strings → unparseable.
JUNK_MOAS = {
    "", "unknown", "undefined mechanism", "mechanism of action not found",
    "unknown mechanism of action", "not available", "n/a", "none",
    "investigational", "investigational compound", "investigational drug",
    "experimental", "not a drug", "not_a_drug",
}

# Tokens to strip from target phrases before canonicalization.
NOISE_TOKENS = {
    "receptor", "receptors", "pathway", "protein",
    "kinase", "kinases", "tyrosine", "serine", "threonine",
    "selective", "competitive", "allosteric",
    "positive", "negative", "anti", "humanized", "human",
    "multi", "pan", "the", "a", "an", "dual", "type", "class",
    "small", "molecule", "inhibitor",
}

# Mutation patterns (order matters — most specific first).
# These are also used in lookahead form to split run-together strings like
# "KRASG12C" → "KRAS G12C" (see _preprocess).
MUTATION_TOKENS = (
    r"V600[EK]?",
    r"L858R",
    r"T790M",
    r"G12[CDRSVA]",
    r"G13[DCR]",
    r"Q61[RKHL]",
)

MUTATION_PATTERNS = [
    re.compile(r"\b(" + tok + r")\b", re.I) for tok in MUTATION_TOKENS
]
MUTATION_PATTERNS.append(
    re.compile(r"\bexon\s*(\d+)\s*(del|ins|deletion|insertion|skipping)\b", re.I)
)
MUTATION_PATTERNS.append(
    re.compile(r"\b([A-Z]\d{2,4}[A-Z])\b")            # generic, case sensitive
)

# Splitter: insert a space between a gene-like prefix and a mutation
# token (e.g. "KRASG12C" → "KRAS G12C") so the rest of the pipeline can
# extract the mutation cleanly.
_MUT_SPLIT_RE = re.compile(
    r"([A-Z]{2,6})(" + "|".join(MUTATION_TOKENS) + r")", re.I
)

# "anti-" / "anti " / "anti_" prefixes carry no MOA information.
_ANTI_PREFIX_RE = re.compile(r"\banti[-_\s]+", re.I)

# "TKI" / "TKIs" → "tyrosine kinase inhibitor" → just "inhibitor" after
# noise stripping, but only when used as a suffix on a gene name.
_TKI_SUFFIX_RE = re.compile(
    r"^(?P<target>.+?)\s+TKIs?\.?\s*$", re.I
)

# Connector splits (multi-target expressions).
CONNECTOR_RE = re.compile(r"\s*(?:/|,|\+|&|\band\b|\bor\b)\s*", re.I)

# Action regex — captures the MOA action suffix. Allows optional
# trailing qualifier after a comma (e.g. "..., anti-inflammatory") or
# a parenthetical acronym gloss ("...(SERM)").
ACTION_RE = re.compile(
    r"\b(" + "|".join(ACTION_WORDS) + r")s?\b"
    r"(?:\s*\([^)]*\))?"           # optional "(SERM)"
    r"(?:\s*,.*)?"                 # optional trailing ", anti-inflammatory"
    r"\.?\s*$",
    re.I,
)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def _extract_mutations(text: str) -> List[str]:
    found = []
    for pat in MUTATION_PATTERNS:
        for m in pat.finditer(text):
            token = m.group(0).upper().replace(" ", "_")
            if token not in found:
                found.append(token)
    return found


def _strip_mutations(text: str) -> str:
    for pat in MUTATION_PATTERNS:
        text = pat.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_target_token(tok: str) -> str:
    tok = re.sub(r"\([^)]*\)", "", tok)            # strip parentheticals
    tok = tok.strip(" -\t:")
    # Drop noise words
    parts = [p for p in re.split(r"\s+", tok) if p and p.lower() not in NOISE_TOKENS]
    return " ".join(parts).strip()


def _split_targets(target_phrase: str) -> List[str]:
    pieces = [p.strip() for p in CONNECTOR_RE.split(target_phrase) if p and p.strip()]
    # CDK4/6 is expanded to CDK4 and CDK6 by the connector split, but only
    # if the trailing number sticks to the preceding token. Handle the
    # "CDK4/6" → ["CDK4","6"] case by rejoining with the prefix.
    expanded: List[str] = []
    prev_prefix = None
    for p in pieces:
        cleaned = _clean_target_token(p)
        if not cleaned:
            continue
        # Pure digit fragment → expand with prev_prefix (e.g. CDK4/6 → CDK6)
        if re.fullmatch(r"\d+", cleaned) and prev_prefix:
            expanded.append(prev_prefix + cleaned)
            continue
        expanded.append(cleaned)
        m = re.match(r"^([A-Za-z]+)\d+$", cleaned)
        if m:
            prev_prefix = m.group(1)
        else:
            prev_prefix = None
    return expanded


# Phrase-level normalizations applied before slugifying. Collapse common
# variants of the same concept so e.g. "multi-kinase inhibitor",
# "multi kinase inhibitor", and "multikinase inhibitor" all produce the
# same canonical key. Order matters — earlier rules run first.
_PHRASE_NORMALIZE = [
    # Strip parenthetical acronym glosses: "... (DHFR)" / "(Topo I)"
    (re.compile(r"\s*\([^)]*\)\s*"), " "),
    # "multi X" variants → "multiX" (only for common compound stems)
    (re.compile(r"\bmulti[-\s_]+kinase\b", re.I), "multikinase"),
    (re.compile(r"\bmulti[-\s_]+target\b", re.I), "multitarget"),
    (re.compile(r"\bmulti[-\s_]+receptor\b", re.I), "multireceptor"),
    # Roman numerals in enzyme families → digits (topoisomerase I/II,
    # PARP-1/2, CDK1/2...). These are handled via explicit rules rather
    # than a generic roman→digit pass to avoid hitting gene names.
    (re.compile(r"\btopoisomerase[\s_]*ii\b", re.I), "topoisomerase2"),
    (re.compile(r"\btopoisomerase[\s_]*i\b", re.I), "topoisomerase1"),
    # "hdac inhibitor" family — collapse "histone deacetylase" alias.
    (re.compile(r"\bhistone[\s_]+deacetylase\b", re.I), "hdac"),
    # Dihydrofolate reductase full spelling → DHFR.
    (re.compile(r"\bdihydrofolate[\s_]+reductase\b", re.I), "dhfr"),
    # Common dash-form receptor names — strip hyphens inside well-known
    # HGNC-adjacent tokens so PD-1 / PD 1 / PD1 all collapse.
    (re.compile(r"\bpd[-\s_]*l[-\s_]*1\b", re.I), "pdl1"),
    (re.compile(r"\bpd[-\s_]*1\b", re.I), "pd1"),
    (re.compile(r"\bctla[-\s_]*4\b", re.I), "ctla4"),
    (re.compile(r"\bher[-\s_]*2\b", re.I), "her2"),
    (re.compile(r"\bher[-\s_]*3\b", re.I), "her3"),
]


def _normalize_phrase(s: str) -> str:
    s = s.lower()
    for pat, repl in _PHRASE_NORMALIZE:
        s = pat.sub(repl, s)
    return re.sub(r"\s+", " ", s).strip()


def _slugify(s: str) -> str:
    """Slugify a multi-word phrase: spaces → underscore, lowercased.
    Applies phrase-level normalizations first so semantic duplicates
    collapse to the same slug."""
    s = _normalize_phrase(s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _slugify_target(s: str) -> str:
    """Slugify a single target token: strip ALL non-alphanumerics so
    `BCL-2`, `BCL2`, `Bcl 2` all collapse to `bcl2`. Applies phrase
    normalization first for receptor/enzyme aliases."""
    s = _normalize_phrase(s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _canonical_key(targets: List[str], mutations: List[str], action: str) -> str:
    t = "_".join(sorted(_slugify_target(t) for t in targets if t and _slugify_target(t)))
    if not t:
        return ""
    if mutations:
        m = "_".join(sorted(_slugify_target(x) for x in mutations))
        return f"{t}_{m}_{action}"
    return f"{t}_{action}"


def _preprocess(s: str) -> str:
    """Normalize an input string before parsing."""
    s = _MUT_SPLIT_RE.sub(r"\1 \2", s)   # KRASG12C → KRAS G12C
    s = _ANTI_PREFIX_RE.sub("", s)        # anti-PD-1 → PD-1
    return s.strip()


def _build_result(raw: str, targets: List[str], mutations: List[str],
                  action: Optional[str], is_class_only: bool,
                  canonical: Optional[str] = None) -> Dict:
    canonical = canonical or _canonical_key(targets, mutations, action or "class")
    return {
        "raw": raw,
        "targets": targets,
        "mutations": mutations,
        "action": action,
        "is_class_only": is_class_only,
        "canonical_key": canonical,
    }


def parse_short_moa(raw: Optional[str]) -> Optional[Dict]:
    """Parse a `short_moa` string into structured form, or return None."""
    if not raw or not isinstance(raw, str):
        return None

    s = raw.strip().rstrip(".")
    if not s:
        return None
    low = s.lower()
    if low in JUNK_MOAS:
        return None

    # Preprocess: split run-together mutations, strip anti- prefix.
    s = _preprocess(s)
    low = s.lower()

    # 0. TKI suffix shorthand: "EGFR TKI" → treat as "EGFR inhibitor".
    m_tki = _TKI_SUFFIX_RE.match(s)
    if m_tki:
        target_phrase = m_tki.group("target")
        mutations = _extract_mutations(target_phrase)
        targets = _split_targets(_strip_mutations(target_phrase))
        if targets:
            return _build_result(raw, targets, mutations, "inhibitor", False)

    # 1. Bare class words (exact match).
    if low in BARE_CLASS_WORDS:
        return _build_result(raw, [], [], None, True, canonical=_slugify(low))

    # 2. Class-only marker (multi-word common classes).
    if low in CLASS_ONLY_MARKERS:
        return _build_result(raw, [], [], None, True, canonical=_slugify(low))

    # 3. Suffix form: "<target> <action>"
    m = ACTION_RE.search(s)
    if m:
        action = m.group(1).lower()
        if action == "targeting":
            action = "targeted"
        elif action in ("antibody", "antibodies"):
            action = "antibody"
        target_phrase = s[: m.start()].strip().rstrip("-: ")

        if target_phrase:
            mutations = _extract_mutations(target_phrase)
            targets = _split_targets(_strip_mutations(target_phrase))

            if targets:
                return _build_result(raw, targets, mutations, action, False)

            # No target tokens after cleaning → class-only variant.
            return _build_result(raw, [], [], action, True,
                                 canonical=_slugify(s))

    # 4. Prefix verb form: "Inhibits <target>, ..." / "Blocks <target>, ..."
    m = VERB_RE.match(s)
    if m:
        action = VERB_ACTIONS[m.group(1).lower()]
        target_phrase = m.group(2).strip()
        mutations = _extract_mutations(target_phrase)
        targets = _split_targets(_strip_mutations(target_phrase))
        if targets:
            return _build_result(raw, targets, mutations, action, False)

    # 5. Unresolved — hand off to Stage 2 (Gemini).
    return None


# --------------------------------------------------------------------------
# Compound splitter — handle "PARP inhibitor, PD-1 inhibitor, alkylating agent"
# --------------------------------------------------------------------------

_ACTION_ANY_RE = re.compile(
    r"\b(" + "|".join(ACTION_WORDS) + r")s?\b", re.I
)
_CLASS_SUFFIX_RE = re.compile(
    r"\b(agent|therapy|vaccine|conjugate|analog|drug|antibody|antibodies)\b",
    re.I,
)


def _looks_like_independent_moa(part: str) -> bool:
    """Does this fragment look like its own MOA phrase (not a qualifier)?"""
    p = part.strip().lower()
    if len(p) < 4:
        return False
    if p in BARE_CLASS_WORDS or p in CLASS_ONLY_MARKERS:
        return True
    if _ACTION_ANY_RE.search(p):
        return True
    if _CLASS_SUFFIX_RE.search(p):
        return True
    return False


def parse_short_moa_multi(raw: Optional[str]) -> Optional[List[Dict]]:
    """Parse a short_moa, splitting on top-level commas when each fragment
    is independently a valid MOA phrase.

    Returns a list of parsed dicts (typically 1), or None if nothing parses.
    Prevents compound strings like "PARP inhibitor, PD-1 inhibitor,
    alkylating agent" from collapsing into a single noisy entry.
    """
    if not raw or not isinstance(raw, str):
        return None

    # Try compound split only when every comma-separated part looks like its
    # own MOA (not a trailing qualifier like ", anti-inflammatory").
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) >= 2 and all(_looks_like_independent_moa(p) for p in parts):
        sub_parses: List[Dict] = []
        for p in parts:
            r = parse_short_moa(p)
            if r is not None:
                sub_parses.append(r)
        if len(sub_parses) >= 2:
            return sub_parses

    single = parse_short_moa(raw)
    if single is None:
        return None
    return [single]


def normalize_drug_class(raw: Optional[str]) -> Optional[str]:
    """Collapse noisy `drug_class` strings to a canonical slug.

    Handles variants like:
      "kinase_inhibitor", "Kinase inhibitor", "kinase inhibitor",
      "small_molecule_inhibitor", "Small molecule inhibitor"
    → all become "kinase_inhibitor" / "small_molecule_inhibitor".
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    # Hand-unifications for the most common duplicates.
    aliases = {
        "mab": "monoclonal_antibody",
        "monoclonal_ab": "monoclonal_antibody",
        "small_molecule": "small_molecule_inhibitor",
        "kinase_inhibitors": "kinase_inhibitor",
        "bispecific_ab": "bispecific_antibody",
        "bispecific": "bispecific_antibody",
        "adc": "antibody_drug_conjugate",
        "car_t": "car_t_therapy",
        "cart": "car_t_therapy",
    }
    return aliases.get(s, s)
