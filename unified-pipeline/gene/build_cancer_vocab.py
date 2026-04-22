"""
Build `dictionaries/cancer_vocab.json` from conquest_master.json.

Canonical vocabulary of cancer types used to constrain gene_master's
`detailed_conditions` so they come from the actual trial universe rather
than free-text Gemini output.

Emits:
    {
        "oncotree_to_name":   {"PAAD": "Pancreatic Adenocarcinoma", ...},
        "broad_cancers":      ["Breast Cancer", ...],
        "oncotree_to_broad":  {"PAAD": ["Pancreatic Cancer"], ...},
        "name_to_oncotree":   {"pancreatic adenocarcinoma": "PAAD", ...}
    }

Run:
    python build_cancer_vocab.py
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

CONQUEST_PATH = os.path.join(
    os.path.dirname(_HERE), "output", "conquest_master.json"
)
OUT_PATH = os.path.join(_HERE, "dictionaries", "cancer_vocab.json")


def build() -> None:
    print(f"[vocab] loading {CONQUEST_PATH}")
    with open(CONQUEST_PATH) as f:
        data = json.load(f)
    trials = data.get("trials") or {}
    it = trials.values() if isinstance(trials, dict) else trials

    oncotree_to_name: dict = {}
    oncotree_to_broad: defaultdict = defaultdict(Counter)
    oncotree_to_level: dict = {}  # code → hierarchy level (1=tissue, 2=cancer_type, ...)
    broad_cancer_counts: Counter = Counter()
    name_lower_to_oncotree: dict = {}

    n_trials = 0
    n_conds = 0
    for t in it:
        n_trials += 1
        for co in t.get("conditions") or []:
            n_conds += 1
            code = co.get("oncotree_code")
            hier = co.get("oncotree_hierarchy") or {}
            chain = hier.get("full_chain") or []
            # Walk the full chain — EVERY intermediate code is a valid vocab
            # entry, not just the deepest one. That lets downstream code
            # know that "PANCREAS" is tissue-level (skip for gene subtypes)
            # while "PAAD" is cancer-type-level (keep).
            for node in chain:
                ncode = node.get("code")
                nname = node.get("name")
                try:
                    nlevel = int(node.get("level") or 0)
                except (TypeError, ValueError):
                    nlevel = 0
                if ncode and nname and ncode not in oncotree_to_name:
                    oncotree_to_name[ncode] = nname
                    name_lower_to_oncotree[nname.strip().lower()] = ncode
                    if nlevel:
                        oncotree_to_level[ncode] = nlevel

            for b in co.get("broad_cancers") or []:
                broad_cancer_counts[b] += 1
                if code:
                    oncotree_to_broad[code][b] += 1

    # Flatten oncotree_to_broad: each code maps to its top broad cancer(s)
    # (any broad that appears in ≥20% of trials with that code).
    oncotree_to_broad_final: dict = {}
    for code, bcounts in oncotree_to_broad.items():
        total = sum(bcounts.values())
        if total == 0:
            continue
        kept = [b for b, c in bcounts.items() if c / total >= 0.2]
        if not kept:
            kept = [bcounts.most_common(1)[0][0]]
        oncotree_to_broad_final[code] = sorted(kept)

    # Split codes by level so downstream filters can exclude tissue-level
    # names (e.g. "Breast", "Lung") from detailed_conditions.
    tissue_codes = {c: n for c, n in oncotree_to_name.items()
                    if oncotree_to_level.get(c) == 1}
    subtype_codes = {c: n for c, n in oncotree_to_name.items()
                     if oncotree_to_level.get(c, 0) >= 2}

    vocab = {
        "metadata": {
            "version": "1.1",
            "built_at": datetime.now().isoformat(),
            "source": os.path.basename(CONQUEST_PATH),
            "n_trials_scanned": n_trials,
            "n_conditions_scanned": n_conds,
            "n_oncotree_codes": len(oncotree_to_name),
            "n_tissue_codes": len(tissue_codes),
            "n_subtype_codes": len(subtype_codes),
            "n_broad_cancers": len(broad_cancer_counts),
        },
        "oncotree_to_name": dict(sorted(oncotree_to_name.items())),
        "oncotree_to_level": dict(sorted(oncotree_to_level.items())),
        "tissue_codes": dict(sorted(tissue_codes.items())),
        "subtype_codes": dict(sorted(subtype_codes.items())),
        "name_to_oncotree": dict(sorted(name_lower_to_oncotree.items())),
        "oncotree_to_broad": dict(sorted(oncotree_to_broad_final.items())),
        "broad_cancers": sorted(broad_cancer_counts.keys()),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(vocab, f, indent=2)

    print(f"[vocab] wrote {OUT_PATH}")
    print(f"  trials: {n_trials:,}  conditions: {n_conds:,}")
    print(f"  oncotree codes: {len(oncotree_to_name)} "
          f"({len(tissue_codes)} tissue, {len(subtype_codes)} subtype)")
    print(f"  broad cancers:  {len(broad_cancer_counts)}")


if __name__ == "__main__":
    build()
