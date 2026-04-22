"""
Apply moa_master to conquest_master.json — adds `genes_from_drugs[]`
to every trial by joining trial drugs → MOA → gene_targets.

Reads:
    output/conquest_master.json
    gene/dictionaries/moa_master.json

Writes:
    output/conquest_master_with_gene_targets.json   (default)

Run:
    python classify_trial_genes.py
    python classify_trial_genes.py --in-place
    python classify_trial_genes.py --report      # only print stats, no write
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from moa_master import MoaMaster

try:
    from config import CONQUEST_MASTER_FILE, OUTPUT_DIR
except Exception:
    CONQUEST_MASTER_FILE = os.path.join(
        os.path.dirname(_HERE), "output", "conquest_master.json"
    )
    OUTPUT_DIR = os.path.join(os.path.dirname(_HERE), "output")

DEFAULT_OUT = os.path.join(OUTPUT_DIR, "conquest_master_with_gene_targets.json")


def classify(in_place: bool = False, report_only: bool = False) -> None:
    print(f"[classify] loading conquest_master from {CONQUEST_MASTER_FILE}")
    with open(CONQUEST_MASTER_FILE) as f:
        cm = json.load(f)
    trials = cm.get("trials", {})
    print(f"  {len(trials):,} trials")

    print("[classify] loading moa_master")
    moa = MoaMaster().load()
    if not moa.moas:
        raise SystemExit("moa_master.json is empty — run build_moa_master.py first")
    print(f"  {len(moa.moas)} MOAs, {len(moa.drug_to_moa)} drug→moa entries")

    # Stats
    trials_with_existing_genes = 0
    trials_gaining_genes = 0
    drug_lookups = 0
    drug_hits = 0
    gene_count_added: Counter = Counter()

    for nct_id, trial in trials.items():
        existing = set(trial.get("genes") or [])
        if existing:
            trials_with_existing_genes += 1

        new_genes: set = set()
        for drug in trial.get("drugs", []) or []:
            name = (drug.get("normalized_name") or
                    drug.get("generic_name") or "").strip()
            if not name:
                continue
            drug_lookups += 1

            # Try direct lookup, then lowercased.
            keys = (moa.drug_to_moa.get(name) or
                    moa.drug_to_moa.get(name.lower()) or [])
            if keys:
                drug_hits += 1
            for mk in keys:
                entry = moa.moas.get(mk)
                if entry:
                    for g in entry.get("gene_targets", []):
                        new_genes.add(g)

        if new_genes - existing:
            trials_gaining_genes += 1
        for g in new_genes:
            gene_count_added[g] += 1

        trial["genes_from_drugs"] = sorted(new_genes)
        # Union into the trial's main genes list while preserving the audit trail.
        trial["genes_combined"] = sorted(existing | new_genes)

    cm.setdefault("metadata", {})["gene_classification"] = {
        "applied_at": datetime.now().isoformat(),
        "moa_master_version": moa.metadata.get("version"),
        "trials_with_existing_genes": trials_with_existing_genes,
        "trials_gaining_genes_from_drugs": trials_gaining_genes,
        "drug_lookup_hit_rate": (
            f"{drug_hits}/{drug_lookups} ({drug_hits/drug_lookups*100:.1f}%)"
            if drug_lookups else "0/0"
        ),
        "top_genes_added": gene_count_added.most_common(30),
    }

    print(f"[classify] trials with existing genes:    {trials_with_existing_genes:,}")
    print(f"[classify] trials gaining genes:          {trials_gaining_genes:,}")
    print(f"[classify] drug→MOA hit rate:             {drug_hits:,}/{drug_lookups:,} "
          f"({drug_hits/drug_lookups*100:.1f}%)")
    print("[classify] top 15 genes added across trials:")
    for g, n in gene_count_added.most_common(15):
        print(f"  {n:5d}  {g}")

    if report_only:
        print("[classify] --report only, not writing output")
        return

    out_path = CONQUEST_MASTER_FILE if in_place else DEFAULT_OUT
    print(f"[classify] writing {out_path}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cm, f, indent=2, default=str)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-place", action="store_true",
                    help="Overwrite conquest_master.json (default writes a new file).")
    ap.add_argument("--report", action="store_true",
                    help="Print stats only, do not write any file.")
    args = ap.parse_args()
    classify(in_place=args.in_place, report_only=args.report)
