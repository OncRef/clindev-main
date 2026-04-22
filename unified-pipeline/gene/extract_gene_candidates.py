"""
Emit `dictionaries/proposed_gene_additions.csv` for Tom's review.

For each HGNC symbol Gemini proposed during the supplement pre-pass that
is NOT currently in `New_Gene_updated.xlsx`, compute:

    drugs_gained   — number of drugs whose MOAs currently have empty
                     gene_targets and would gain this HGNC symbol
    moas_gained    — number of MOAs that would flip from empty/unresolved
                     to gene-targeting
    confidence     — average confidence Gemini assigned to proposals
                     resolving to this HGNC symbol
    sample_tokens  — example raw target tokens that mapped to this HGNC
    sample_drugs   — example drugs that would gain coverage
    sample_moas    — example MOA canonical_keys that would flip
    broad_cancers  — most common trial broad_cancers across those drugs
                     (requires conquest_master.json; optional column)

Output sorted by drugs_gained desc. Tom prunes and renames to
`approved_gene_additions.csv`; then `apply_gene_additions.py` appends
to the Excel.

Run:
    python extract_gene_candidates.py
    python extract_gene_candidates.py --include-broad  # adds broad_cancers column
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from gene_master import GeneMaster
from moa_master import MoaMaster

CACHE = os.path.join(_HERE, "cache", "target_hgnc_cache.json")
OUT = os.path.join(_HERE, "dictionaries", "proposed_gene_additions.csv")
CONQUEST = os.path.join(
    os.path.dirname(_HERE), "output", "conquest_master.json"
)


def load_hgnc_cache() -> Dict[str, dict]:
    with open(CACHE) as f:
        return json.load(f)


def _build_token_to_hgnc(cache: Dict[str, dict], excel_genes: Set[str]
                          ) -> Dict[str, tuple]:
    """Return {raw_token_lower: (hgnc_symbol, confidence)} for every
    proposal whose HGNC isn't in the Excel."""
    out: Dict[str, tuple] = {}
    for item in cache.values():
        if not isinstance(item, dict):
            continue
        if not item.get("is_gene"):
            continue
        hgnc = item.get("hgnc_symbol")
        if not hgnc or hgnc in excel_genes:
            continue
        token = (item.get("input") or "").strip().lower()
        if not token:
            continue
        out[token] = (hgnc, float(item.get("confidence") or 0))
    return out


def _load_drug_broad_cancers() -> Dict[str, Counter]:
    """Scan conquest_master and return {drug_name → Counter(broad_cancer)}
    across all trials that use the drug. Returns empty dict if file is
    missing."""
    if not os.path.exists(CONQUEST):
        return {}
    print(f"  loading {CONQUEST} for broad_cancer stats")
    with open(CONQUEST) as f:
        cm = json.load(f)
    trials = cm.get("trials") or {}
    it = trials.values() if isinstance(trials, dict) else trials
    out: Dict[str, Counter] = defaultdict(Counter)
    for t in it:
        broads = []
        for co in t.get("conditions") or []:
            for b in co.get("broad_cancers") or []:
                broads.append(b)
        if not broads:
            continue
        for d in t.get("drugs") or []:
            name = (d.get("normalized_name") or d.get("name") or "").lower()
            if not name:
                continue
            for b in broads:
                out[name][b] += 1
    return out


def extract(include_broad: bool = False) -> None:
    print("[extract] loading Excel + moa_master")
    gm = GeneMaster().load_from_excel()
    excel_genes = set(gm.genes.keys())
    print(f"  Excel genes: {len(excel_genes)}")

    moa = MoaMaster().load()
    print(f"  MOAs: {len(moa.moas)}")

    print("[extract] loading target_hgnc_cache")
    cache = load_hgnc_cache()
    print(f"  cached decisions: {len(cache)}")

    token_to_hgnc = _build_token_to_hgnc(cache, excel_genes)
    print(f"  candidate HGNC symbols (not in Excel): "
          f"{len({h for h, _ in token_to_hgnc.values()})}")

    # Group by HGNC: collect tokens, MOAs, drugs, confidence.
    hgnc_tokens: Dict[str, Set[str]] = defaultdict(set)
    hgnc_conf: Dict[str, list] = defaultdict(list)
    for token, (h, conf) in token_to_hgnc.items():
        hgnc_tokens[h].add(token)
        hgnc_conf[h].append(conf)

    # Find MOAs / drugs impacted — scan moa_master. A MOA "would gain"
    # a HGNC symbol if:
    #   - any unresolved_target normalizes to that HGNC's proposed token
    #   - OR any raw target in `targets[].raw` lowercased is a key in
    #     token_to_hgnc
    hgnc_moas: Dict[str, Set[str]] = defaultdict(set)
    hgnc_drugs: Dict[str, Set[str]] = defaultdict(set)
    for mk, entry in moa.moas.items():
        raw_targets = set()
        for t in entry.get("targets") or []:
            if isinstance(t, dict) and t.get("raw"):
                raw_targets.add(t["raw"].strip().lower())
        for ut in entry.get("unresolved_targets") or []:
            raw_targets.add(ut.strip().lower())
        matched_hgnc = set()
        for rt in raw_targets:
            if rt in token_to_hgnc:
                matched_hgnc.add(token_to_hgnc[rt][0])
        if not matched_hgnc:
            continue
        example_drugs = entry.get("example_drugs") or []
        for h in matched_hgnc:
            hgnc_moas[h].add(mk)
            for d in example_drugs:
                hgnc_drugs[h].add(d)

    broad_map = _load_drug_broad_cancers() if include_broad else {}

    rows: List[dict] = []
    for h, tokens in hgnc_tokens.items():
        drugs = hgnc_drugs.get(h, set())
        moas = hgnc_moas.get(h, set())
        conf = sum(hgnc_conf[h]) / max(1, len(hgnc_conf[h]))

        broad_counter: Counter = Counter()
        if include_broad:
            for d in drugs:
                broad_counter.update(broad_map.get(d.lower(), Counter()))
        top_broads = [b for b, _ in broad_counter.most_common(5)]

        rows.append({
            "hgnc_symbol": h,
            "drugs_gained": len(drugs),
            "moas_gained": len(moas),
            "confidence": round(conf, 2),
            "sample_tokens": "; ".join(sorted(tokens)[:5]),
            "sample_drugs": "; ".join(sorted(drugs)[:5]),
            "sample_moas": "; ".join(sorted(moas)[:5]),
            "top_broad_cancers": "; ".join(top_broads),
            "approved": "",  # Tom fills: y/n
            "notes": "",
        })

    rows.sort(key=lambda r: (-r["drugs_gained"], -r["moas_gained"], r["hgnc_symbol"]))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fields = [
        "approved", "hgnc_symbol", "drugs_gained", "moas_gained",
        "confidence", "sample_tokens", "sample_drugs", "sample_moas",
        "top_broad_cancers", "notes",
    ]
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"[extract] wrote {OUT}")
    print(f"  candidate HGNC symbols: {len(rows)}")
    print(f"  top-10 by drugs_gained:")
    for r in rows[:10]:
        print(f"    {r['hgnc_symbol']:10} | drugs={r['drugs_gained']:4} "
              f"moas={r['moas_gained']:4} | {r['sample_tokens']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-broad", action="store_true",
                    help="Add broad_cancers column (requires conquest_master.json)")
    args = ap.parse_args()
    extract(include_broad=args.include_broad)
