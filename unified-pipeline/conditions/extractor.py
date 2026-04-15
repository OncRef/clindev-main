"""
Condition component extractor — pulls line_of_therapy, stage, and gene
mentions out of a raw condition string and returns the "core" cancer phrase
that's left.

This is the pipeline's Step 2 (per the reference v1 pipeline).
"""

import csv
import json
import re

from config import LINE_OF_THERAPY_FILE, STAGE_FILE, GENES_FILE


def load_extractor_config():
    """Load the three extractor dictionaries once; returns a dict used by extract_components."""
    cfg = {"lot_patterns": [], "stage_patterns": [], "gene_patterns": []}

    with open(LINE_OF_THERAPY_FILE) as f:
        cfg["lot_patterns"] = [
            {"name": t["canonical_name"], "regex": re.compile(t["regex"])}
            for t in json.load(f)["terms"]
        ]

    with open(STAGE_FILE) as f:
        cfg["stage_patterns"] = [
            {"name": t["canonical_name"], "regex": re.compile(t["regex"])}
            for t in json.load(f)["terms"]
        ]

    with open(GENES_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            gene = (row.get("Gene") or "").strip()
            aliases = [a.strip() for a in (row.get("Gene Aliases") or "").split(",") if a.strip()]
            for alias in aliases:
                if len(alias) < 3:
                    continue
                flags = 0 if len(alias) < 4 else re.IGNORECASE
                cfg["gene_patterns"].append({
                    "gene": gene,
                    "alias": alias,
                    "regex": re.compile(r"\b" + re.escape(alias) + r"\b", flags),
                })
    cfg["gene_patterns"].sort(key=lambda x: -len(x["alias"]))
    return cfg


def extract_components(condition, cfg):
    """Strip LOT/stage/gene mentions out of a condition string.

    Returns (core_condition, {"line_of_therapy": [...], "stage": [...], "genes": [...]}).
    """
    text = condition
    extracted = {"line_of_therapy": [], "stage": [], "genes": []}

    for pat in cfg["lot_patterns"]:
        m = pat["regex"].search(text)
        if m:
            extracted["line_of_therapy"].append(pat["name"])
            text = text[:m.start()] + " " + text[m.end():]

    for pat in cfg["stage_patterns"]:
        m = pat["regex"].search(text)
        if m:
            extracted["stage"].append(pat["name"])
            text = text[:m.start()] + " " + text[m.end():]

    found = set()
    for pat in cfg["gene_patterns"]:
        m = pat["regex"].search(text)
        if m and pat["gene"] not in found:
            found.add(pat["gene"])
            extracted["genes"].append(pat["gene"])
            text = text[:m.start()] + " " + text[m.end():]

    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip().strip("-– ,;:")
    return text, extracted
