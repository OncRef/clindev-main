"""
Audit drug_master.json for "bucket" corruption — entries that absorbed
unrelated drugs because of placeholder generic_name leakage from Gemini
responses (see drug_master.py + enricher.py for the underlying bug).

Emits three CSVs into the directory passed via --out (or alongside the input):

  bucket_entries_for_review.csv   one row per bucket entry to delete from drug_master.json
  compromised_drug_names.csv      one row per drug name that resolves to a bucket and needs re-enrichment
  affected_trials.csv             one row per NCT in conquest_master.json whose drugs hit a bucket

This script is read-only — it never mutates drug_master.json or any
conquest file. Run it before any cleanup pass.

Usage:
  python -m drugs.audit_drug_master \
      --drug-master path/to/drug_master.json \
      --conquest path/to/conquest_master.json \
      --out audit_out/
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict


PLACEHOLDER_NAMES = {
    "", "...", "....", ".....", "n/a", "na", "none", "null",
    "not applicable", "not available", "multiple", "various",
    "various brand names", "tbd", "unknown", "undefined",
    "drug", "drugs", "compound", "test", "placeholder",
}

# Heuristic thresholds — entries above either limit are bucket-shaped.
# Real drugs rarely exceed ~30 brand names (e.g. estradiol has many
# formulations) and ~50 alternative names.
MAX_BRAND_NAMES = 50
MAX_OTHER_NAMES = 50


def is_placeholder(name):
    return isinstance(name, str) and name.strip().lower() in PLACEHOLDER_NAMES


# A real drug name is a single token-ish identifier — letters, digits,
# limited punctuation. Regimen strings ("X followed by Y", "A & B",
# "A and B and C"), free-text descriptions, and conjunction-laden phrases
# are not real drug identifiers.
_REGIMEN_PATTERNS = (
    " followed by ", " and ", " & ", " plus ", " with ", " + ",
    " or ", "/", ",", ";",
)

# Tokens that legitimately appear inside multi-word real drug names
# (e.g. "lutetium lu 177 dotatate", "cytarabine liposome", "imatinib mesylate").
# A multi-word key built only from these is NOT a regimen.
_LEGIT_DRUG_QUALIFIERS = {
    "liposome", "liposomal", "mesylate", "sulfate", "hydrochloride",
    "acetate", "phosphate", "tartrate", "citrate", "dihydrate",
    "hydrate", "fumarate", "tosylate", "sodium", "potassium",
    "calcium", "magnesium", "lu", "177", "225", "131", "111", "90",
    "ga", "tc", "f", "i", "y", "pmda", "alfa", "beta", "gamma",
}


def _looks_like_regimen(key):
    if not key:
        return False
    k = key.lower()
    if any(p in k for p in _REGIMEN_PATTERNS):
        return True
    # 3+ word keys where the words include real drug-shaped tokens stacked
    # together without conjunctions are usually regimens
    # ("velcade thalidomide dexamethasone").
    words = k.split()
    if len(words) >= 3:
        non_qualifier_words = [
            w for w in words
            if w not in _LEGIT_DRUG_QUALIFIERS and not w.isdigit()
        ]
        if len(non_qualifier_words) >= 3:
            return True
    return False


def _suggest_action(key, entry):
    if is_placeholder(key) or is_placeholder(entry.get("generic_name") or ""):
        return "delete"
    if _looks_like_regimen(key):
        return "delete"
    return "trim"


def detect_bucket_keys(drugs):
    bucket_reasons = {}
    for k, e in drugs.items():
        gn = (e.get("generic_name") or "").strip().lower()
        bn_count = len(e.get("brand_names") or [])
        on_count = len(e.get("other_names") or [])
        reasons = []
        if is_placeholder(k):
            reasons.append(f"placeholder_key={k!r}")
        if is_placeholder(gn):
            reasons.append(f"placeholder_generic_name={gn!r}")
        if bn_count > MAX_BRAND_NAMES:
            reasons.append(f"oversize_brand_names={bn_count}")
        if on_count > MAX_OTHER_NAMES:
            reasons.append(f"oversize_other_names={on_count}")
        if reasons:
            bucket_reasons[k] = reasons
    return bucket_reasons


def build_lookup_index(drugs):
    """Replicates DrugMaster._rebuild_index — first occurrence wins."""
    index = {}
    for key, entry in drugs.items():
        candidates = [
            key,
            entry.get("generic_name"),
            *(entry.get("brand_names") or []),
            *(entry.get("other_names") or []),
        ]
        for cand in candidates:
            if not isinstance(cand, str):
                continue
            n = cand.lower().strip()
            if n and n not in index:
                index[n] = key
    return index


def audit(drug_master_path, conquest_path, out_dir):
    with open(drug_master_path) as f:
        dm = json.load(f)
    drugs = dm.get("drugs") or dm
    if not isinstance(drugs, dict):
        sys.exit(f"drug_master.json at {drug_master_path} has no 'drugs' dict")

    bucket_reasons = detect_bucket_keys(drugs)
    bucket_keys = set(bucket_reasons)
    index = build_lookup_index(drugs)

    # Names whose lookup hits a bucket entry → these are the compromised
    # drug-name aliases that need re-enrichment after cleanup.
    name_to_bucket = {n: canon for n, canon in index.items() if canon in bucket_keys}
    by_bucket = defaultdict(list)
    for n, canon in name_to_bucket.items():
        by_bucket[canon].append(n)

    # Trials affected
    affected = []  # (nct_id, drug_name, bucket_canonical_key)
    if conquest_path and os.path.exists(conquest_path):
        with open(conquest_path) as f:
            cm = json.load(f)
        trials = cm.get("trials", cm)
        for nct, t in trials.items():
            for d in t.get("drugs") or []:
                n = (d.get("normalized_name") or "").lower().strip()
                if not n:
                    continue
                canon = index.get(n)
                if canon and canon in bucket_keys:
                    affected.append((nct, d.get("normalized_name"), canon))

    os.makedirs(out_dir, exist_ok=True)

    # === 1. bucket_entries_for_review.csv =================================
    # Two suggested actions:
    #   delete  — pure junk: placeholder keys ("...", "n/a"), regimen
    #             strings ("velcade thalidomide dexamethasone"), or
    #             keys with no real drug identity
    #   trim    — real drug whose other_names/brand_names got polluted by
    #             the bucket bug; keep the entry, clear its aliases, then
    #             re-enrich the entry to repopulate from a single Gemini call
    bucket_csv = os.path.join(out_dir, "bucket_entries_for_review.csv")
    with open(bucket_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "approved_action",
            "suggested_action",
            "canonical_key",
            "generic_name",
            "brand_count",
            "other_names_count",
            "names_routing_here",
            "trial_refs_via_this_bucket",
            "short_moa",
            "reasons",
        ])
        ref_counts = Counter(canon for _, _, canon in affected)
        for k in sorted(bucket_keys, key=lambda x: -ref_counts.get(x, 0)):
            e = drugs[k]
            suggested = _suggest_action(k, e)
            w.writerow([
                "",  # left blank for Tom: "delete" | "trim" | "keep"
                suggested,
                k,
                e.get("generic_name") or "",
                len(e.get("brand_names") or []),
                len(e.get("other_names") or []),
                len(by_bucket.get(k, [])),
                ref_counts.get(k, 0),
                (e.get("short_moa") or "")[:80],
                "; ".join(bucket_reasons[k]),
            ])

    # === 2. compromised_drug_names.csv ===================================
    comp_csv = os.path.join(out_dir, "compromised_drug_names.csv")
    with open(comp_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "drug_name_lower",
            "currently_resolves_to_bucket",
            "bucket_generic_name",
            "trials_referencing_this_name",
        ])
        ref_by_name = Counter(d.lower() for _, d, _ in affected if d)
        for n in sorted(name_to_bucket):
            canon = name_to_bucket[n]
            w.writerow([
                n,
                canon,
                drugs[canon].get("generic_name") or "",
                ref_by_name.get(n, 0),
            ])

    # === 3. affected_trials.csv ==========================================
    trials_csv = os.path.join(out_dir, "affected_trials.csv")
    with open(trials_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["nct_id", "drug_name_in_trial", "resolves_to_bucket"])
        for nct, drug, canon in sorted(set(affected)):
            w.writerow([nct, drug or "", canon])

    print("=== drug_master audit ===")
    print(f"input drug_master:       {drug_master_path}")
    print(f"input conquest_master:   {conquest_path or '(none — skipped trial scan)'}")
    print(f"out_dir:                 {out_dir}")
    print()
    print(f"bucket entries:          {len(bucket_keys)}")
    print(f"compromised name keys:   {len(name_to_bucket)}")
    print(f"trial drug refs hit:     {len(affected)}")
    print(f"distinct trials hit:     {len({nct for nct, _, _ in affected})}")
    print()
    print(f"wrote {bucket_csv}")
    print(f"wrote {comp_csv}")
    print(f"wrote {trials_csv}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--drug-master", required=True, help="path to drug_master.json")
    p.add_argument("--conquest", default=None, help="path to conquest_master.json (optional)")
    p.add_argument("--out", required=True, help="output directory for CSVs")
    args = p.parse_args()
    audit(args.drug_master, args.conquest, args.out)


if __name__ == "__main__":
    main()
