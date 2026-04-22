"""
Monthly incremental ingest — the production entry point.

Unlike `build_moa_master.py` (full rebuild, renames keys, starts from empty
state), this script:

  1. Loads EXISTING moa_master.json + gene_master.json.
  2. Optionally runs the Gemini supplement pre-pass for any NEW target
     tokens introduced this month (cache-hit for seen ones).
  3. Calls MoaMaster.ingest_moas() — upserts only; no key renames.
  4. Refreshes gene_master.moas_targeting + example_drugs for the
     affected genes only (no Gemini re-enrichment of detailed_conditions).
  5. Writes both files atomically.
  6. Appends a ChangeSet audit to `ingest_history/<YYYY-MM>.json`.

Stable canonical keys across runs — downstream systems can safely hold
references.

Run:
    python ingest_monthly.py                          # uses dictionaries/drug_master.json
    python ingest_monthly.py --drug-master path.json
    python ingest_monthly.py --subset new_drugs.json  # only these drugs
    python ingest_monthly.py --no-gemini              # skip Stage-2 / pre-pass
    python ingest_monthly.py --dry-run                # report but don't write
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  # for config.py

from moa_master import MoaMaster, ChangeSet
from gene_master import GeneMaster
from parser import parse_short_moa_multi
from gemini_client import resolve_targets_to_hgnc, JsonCache

try:
    from config import DRUG_MASTER_FILE
except Exception:
    DRUG_MASTER_FILE = os.path.join(
        os.path.dirname(_HERE), "dictionaries", "drug_master.json"
    )

SUPPLEMENT_PATH = os.path.join(
    _HERE, "dictionaries", "moa_target_supplement.json"
)
HISTORY_DIR = os.path.join(_HERE, "ingest_history")


# --------------------------------------------------------------------------
# Supplement pre-pass — extract + persist (mirrors build_moa_master logic,
# but scoped to the current drug subset so monthly runs don't rescan
# all 9,772 drugs)
# --------------------------------------------------------------------------

def _collect_unresolved_tokens(drug_entries: Dict[str, dict],
                                gm: GeneMaster) -> list:
    seen: set = set()
    unresolved: set = set()
    for entry in drug_entries.values():
        raw = entry.get("short_moa") or ""
        parsed_list = parse_short_moa_multi(raw) or []
        for parsed in parsed_list:
            for tok in parsed.get("targets") or []:
                if not tok:
                    continue
                key = tok.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                if gm.resolve_target(tok) is None:
                    unresolved.add(tok)
    return sorted(unresolved)


def _expand_supplement(tokens: list, gm: GeneMaster) -> dict:
    if not tokens:
        return {}
    cache = JsonCache(os.path.join(_HERE, "cache", "target_hgnc_cache.json"))
    responses = resolve_targets_to_hgnc(tokens, cache=cache)
    additions: Dict[str, str] = {}
    for token, item in responses.items():
        if not isinstance(item, dict):
            continue
        if not item.get("is_gene"):
            continue
        hgnc = item.get("hgnc_symbol")
        if not hgnc or hgnc not in gm.genes:
            continue
        additions[token.strip().lower()] = hgnc
    return additions


def _persist_supplement(new_entries: dict) -> int:
    if not new_entries:
        return 0
    existing: Dict = {}
    if os.path.exists(SUPPLEMENT_PATH):
        try:
            with open(SUPPLEMENT_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    added = 0
    for k, v in new_entries.items():
        if k not in existing:
            existing[k] = v
            added += 1
    os.makedirs(os.path.dirname(SUPPLEMENT_PATH), exist_ok=True)
    with open(SUPPLEMENT_PATH, "w") as f:
        json.dump(existing, f, indent=2, sort_keys=True)
    return added


# --------------------------------------------------------------------------
# Audit trail — one JSON per monthly run
# --------------------------------------------------------------------------

def _write_history(cs: ChangeSet, extra: dict, tag: Optional[str]) -> str:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = tag or datetime.now().strftime("%Y-%m")
    path = os.path.join(HISTORY_DIR, f"{stamp}.json")
    payload = {
        "timestamp": datetime.now().isoformat(),
        "changeset": cs.to_dict(),
        **extra,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def ingest(drug_master_path: str, *, subset_path: Optional[str] = None,
           use_gemini: bool = True, dry_run: bool = False,
           tag: Optional[str] = None) -> ChangeSet:
    print(f"[ingest] loading moa_master + gene_master")
    moa = MoaMaster().load()
    gm = GeneMaster().load_from_excel()
    print(f"  moa_master: {len(moa.moas)} MOAs, {len(moa.drug_to_moa)} drugs")
    print(f"  gene_master Excel: {len(gm.genes)} genes")

    # Preserve existing gene-level enrichment (detailed_conditions etc.)
    # when we write gene_master.json at the end.
    gm.merge_existing_enrichment()

    # Load the drugs to ingest.
    path = subset_path or drug_master_path
    with open(path) as f:
        data = json.load(f)
    drugs = data.get("drugs", data)  # accept raw dict or drug_master shape
    print(f"[ingest] drugs to ingest: {len(drugs)}")

    # Supplement pre-pass — only for tokens we haven't seen before. The
    # resolve_targets_to_hgnc cache makes this effectively free on reruns.
    if use_gemini:
        unresolved = _collect_unresolved_tokens(drugs, gm)
        print(f"  unresolved target tokens: {len(unresolved)}")
        if unresolved:
            additions = _expand_supplement(unresolved, gm)
            added = _persist_supplement(additions) if not dry_run else len(additions)
            print(f"  supplement additions: {added}")
            if added and not dry_run:
                gm = GeneMaster().load_from_excel()
                gm.merge_existing_enrichment()

    # The upsert call — stable canonical keys, no merge pass.
    print("[ingest] MoaMaster.ingest_moas()")
    cs = moa.ingest_moas(drugs, gm, use_gemini=use_gemini)
    print(f"  {cs.summary()}")

    # Refresh only affected genes — no Gemini re-enrichment.
    if cs.affected_genes:
        n = gm.refresh_for_genes(cs.affected_genes, moa)
        print(f"[ingest] refreshed moas_targeting for {n} genes")

    if dry_run:
        print("[ingest] dry-run — no write")
        return cs

    moa.metadata = {
        **(moa.metadata or {}),
        "last_incremental_at": datetime.now().isoformat(),
        "last_incremental_added": len(cs.added_moa_keys),
        "last_incremental_extended": len(cs.extended_moa_keys),
    }
    moa.save()
    gm.save()

    history_path = _write_history(
        cs,
        extra={
            "drug_master_source": os.path.basename(path),
            "use_gemini": use_gemini,
            "moa_master_total_after": len(moa.moas),
        },
        tag=tag,
    )
    print(f"[ingest] wrote {moa.path}")
    print(f"[ingest] wrote {gm.path}")
    print(f"[ingest] audit: {history_path}")
    return cs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--drug-master", default=DRUG_MASTER_FILE)
    ap.add_argument("--subset",
                    help="Path to a JSON file with just the drugs to ingest "
                         "(either {drugs: {...}} or a bare {drug_key: entry} dict)")
    ap.add_argument("--no-gemini", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tag",
                    help="Override YYYY-MM tag for the audit file")
    args = ap.parse_args()
    ingest(
        args.drug_master,
        subset_path=args.subset,
        use_gemini=not args.no_gemini,
        dry_run=args.dry_run,
        tag=args.tag,
    )
