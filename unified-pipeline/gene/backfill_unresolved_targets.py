"""
Backfill gene_targets for existing MOAs after the Excel is extended.

When Tom adds new genes to `New_Gene_updated.xlsx` (via
apply_gene_additions.py or manually), MOAs that had those targets in
their ``unresolved_targets`` field don't automatically gain
``gene_targets`` — the pipeline only resolves targets at ingest time.

This script does the retroactive pass:

  1. Load moa_master.json and a fresh GeneMaster (Excel + supplement).
  2. For every MOA with a non-empty ``unresolved_targets`` list, re-try
     GeneMaster.resolve_target() on each token.
  3. Any token that now resolves to an Excel gene:
       - add its HGNC symbol to the MOA's gene_targets
       - update the matching entry in targets[].gene_symbol
       - remove from unresolved_targets
  4. Refresh gene_master.moas_targeting for the affected genes.
  5. Write both files + an audit row to ingest_history.

Idempotent: if nothing in the Excel changed, every token stays
unresolved and the script is a no-op.

Run:
    python backfill_unresolved_targets.py
    python backfill_unresolved_targets.py --dry-run
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from moa_master import MoaMaster, ChangeSet
from gene_master import GeneMaster

HISTORY_DIR = os.path.join(_HERE, "ingest_history")


def backfill(moa: MoaMaster, gm: GeneMaster) -> Tuple[ChangeSet, dict]:
    """Try to resolve every MOA's ``unresolved_targets`` against the
    current Excel. Returns (ChangeSet, stats)."""
    cs = ChangeSet()
    scanned = 0
    token_resolved = 0
    moas_touched = 0

    for mk, entry in moa.moas.items():
        unresolved = list(entry.get("unresolved_targets") or [])
        if not unresolved:
            continue
        scanned += 1

        still_unresolved: List[str] = []
        newly_resolved: List[Tuple[str, str]] = []  # (raw_token, hgnc)

        for token in unresolved:
            gene = gm.resolve_target(token)
            if gene:
                newly_resolved.append((token, gene))
            else:
                still_unresolved.append(token)

        if not newly_resolved:
            continue

        # Update the MOA entry.
        gene_targets = entry.setdefault("gene_targets", [])
        for _, g in newly_resolved:
            if g not in gene_targets:
                gene_targets.append(g)
                cs.affected_genes.add(g)
            token_resolved += 1

        # Also attach gene_symbol on the matching targets[] row where we
        # originally recorded raw=token. This keeps the entry internally
        # consistent.
        token_to_gene = dict(newly_resolved)
        for tgt in entry.get("targets") or []:
            if not isinstance(tgt, dict):
                continue
            raw = tgt.get("raw")
            if raw in token_to_gene and not tgt.get("gene_symbol"):
                tgt["gene_symbol"] = token_to_gene[raw]

        entry["unresolved_targets"] = still_unresolved
        cs.extended_moa_keys.add(mk)
        moas_touched += 1

    stats = {
        "moas_with_unresolved": scanned,
        "moas_touched": moas_touched,
        "tokens_resolved": token_resolved,
        "genes_affected": len(cs.affected_genes),
    }
    return cs, stats


def _write_history(cs: ChangeSet, stats: dict, tag: str) -> str:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"backfill_{tag}.json")
    payload = {
        "timestamp": datetime.now().isoformat(),
        "kind": "backfill_unresolved_targets",
        "stats": stats,
        "changeset": cs.to_dict(),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def main(dry_run: bool = False) -> None:
    print("[backfill] loading moa_master + GeneMaster")
    moa = MoaMaster().load()
    gm = GeneMaster().load_from_excel()
    gm.merge_existing_enrichment()
    print(f"  moa_master: {len(moa.moas)} MOAs")
    print(f"  Excel: {len(gm.genes)} genes, alias_index {len(gm.alias_index)}")

    cs, stats = backfill(moa, gm)
    print(f"[backfill] {stats}")

    if cs.affected_genes:
        n = gm.refresh_for_genes(cs.affected_genes, moa)
        print(f"[backfill] refreshed moas_targeting for {n} genes")

    if dry_run or not cs.extended_moa_keys:
        if dry_run:
            print("[backfill] dry-run — no write")
        else:
            print("[backfill] nothing to write — no unresolved token "
                  "newly resolves. Excel unchanged since last ingest?")
        return

    moa.save()
    gm.save()
    tag = datetime.now().strftime("%Y-%m-%d")
    history_path = _write_history(cs, stats, tag)
    print(f"[backfill] wrote {moa.path}")
    print(f"[backfill] wrote {gm.path}")
    print(f"[backfill] audit: {history_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
