"""
Orchestrator: build `moa_master.json` from `drug_master.json`.

Stages:
    1. Deterministic parser (parser.py)           — free, ~58% coverage
    2. Gemini 2.5 Flash enrichment (gemini_client) — long tail, cached
    3. Gene linker (GeneMaster)                    — target → excel gene

Run:
    python build_moa_master.py
    python build_moa_master.py --no-gemini       # Stage 1 only
    python build_moa_master.py --limit 200       # small test run

Idempotent: re-runs hit the Gemini cache and cost nothing.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  # for config.py

from parser import parse_short_moa_multi, normalize_drug_class
from moa_master import MoaMaster, ChangeSet
from gene_master import GeneMaster
from gemini_client import enrich_moa_batch, resolve_targets_to_hgnc, JsonCache

try:
    from config import DRUG_MASTER_FILE
except Exception:
    DRUG_MASTER_FILE = os.path.join(
        os.path.dirname(_HERE), "dictionaries", "drug_master.json"
    )


# Ingestion helpers moved to moa_master.py (MoaMaster._ingest_parsed /
# _ingest_gemini). Both full-rebuild (here) and monthly-incremental
# orchestrators share that single upsert code path via ingest_moas().


# --------------------------------------------------------------------------
# Pre-pass: expand moa_target_supplement.json via Gemini
# --------------------------------------------------------------------------

SUPPLEMENT_PATH = os.path.join(
    _HERE, "dictionaries", "moa_target_supplement.json"
)


def _collect_unresolved_target_tokens(drugs: dict, gm: GeneMaster) -> List[str]:
    """First-pass scan: collect unique target tokens that fail Excel lookup."""
    seen: set = set()
    unresolved: set = set()
    for entry in drugs.values():
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


def _expand_supplement_via_gemini(unresolved_tokens: List[str],
                                  gm: GeneMaster) -> dict:
    """Ask Gemini to propose HGNC symbols for unresolved tokens.
    Only tokens whose HGNC suggestion is a gene in the Excel are kept.
    Returns dict[lower_token → hgnc_symbol]."""
    if not unresolved_tokens:
        return {}

    cache = JsonCache(os.path.join(_HERE, "cache", "target_hgnc_cache.json"))
    responses = resolve_targets_to_hgnc(unresolved_tokens, cache=cache)

    additions: Dict[str, str] = {}
    skipped_not_in_excel = 0
    skipped_not_gene = 0
    for token, item in responses.items():
        if not isinstance(item, dict):
            continue
        if not item.get("is_gene"):
            skipped_not_gene += 1
            continue
        hgnc = item.get("hgnc_symbol")
        if not hgnc:
            continue
        if hgnc not in gm.genes:
            skipped_not_in_excel += 1
            continue
        additions[token.strip().lower()] = hgnc

    print(f"  proposed: {len(responses)} | kept: {len(additions)} | "
          f"skipped(non-gene): {skipped_not_gene} | "
          f"skipped(not-in-excel): {skipped_not_in_excel}")
    return additions


def _persist_supplement(new_entries: dict) -> int:
    """Merge new_entries into moa_target_supplement.json without clobbering
    any hand-curated entries. Returns count of newly added keys."""
    if not new_entries:
        return 0
    existing: Dict[str, str] = {}
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
# Main
# --------------------------------------------------------------------------

def build(use_gemini: bool = True, limit: Optional[int] = None) -> None:
    print(f"[build_moa_master] loading drug_master from {DRUG_MASTER_FILE}")
    with open(DRUG_MASTER_FILE) as f:
        dm = json.load(f)
    drugs = dm.get("drugs", {})
    if limit:
        drugs = dict(list(drugs.items())[:limit])
    print(f"  {len(drugs):,} drugs")

    print("[build_moa_master] loading gene master from Excel")
    gm = GeneMaster().load_from_excel()
    print(f"  {len(gm.genes)} genes, {len(gm.alias_index)} alias entries")

    # ----- Pre-pass: expand target → HGNC supplement via Gemini -----
    if use_gemini:
        print("[pre-pass] discovering unresolved target tokens")
        unresolved_tokens = _collect_unresolved_target_tokens(drugs, gm)
        print(f"  {len(unresolved_tokens)} unresolved tokens")
        if unresolved_tokens:
            additions = _expand_supplement_via_gemini(unresolved_tokens, gm)
            added_count = _persist_supplement(additions)
            print(f"  supplement updated: +{added_count} new mappings")
            if added_count:
                # Reload GeneMaster so the new supplement entries are picked
                # up by subsequent resolve_target() calls.
                gm = GeneMaster().load_from_excel()
                print(f"  gene master reloaded: {len(gm.alias_index)} "
                      f"alias entries")

    # Full rebuild starts from an empty MoaMaster — incremental runs would
    # load the existing one via MoaMaster().load() and skip this reset.
    moa = MoaMaster()

    # ----- Stages 1 + 2: delegate to the library entry point -----
    print("[stages 1+2] MoaMaster.ingest_moas()")
    changeset = moa.ingest_moas(drugs, gm, use_gemini=use_gemini)
    print(f"  {changeset.summary()}")
    print(f"  unresolved raw strings: {len(changeset.unresolved_raw_strings)}")

    # When Gemini is disabled, record the unparseable raw strings as
    # placeholder class-only entries so no drug gets silently dropped.
    if not use_gemini:
        for raw in changeset.unresolved_raw_strings:
            key = f"unresolved_{abs(hash(raw)) % 10**8}"
            moa.add_moa(
                key, canonical_name=raw, targets=[], gene_targets=[],
                action=None, drug_class=None, pathway=None,
                is_class_only=True, source="unresolved",
            )
            moa.record_alias(key, raw)
            # Link every drug whose short_moa produced this raw string.
            for drug_key, entry in drugs.items():
                if (entry.get("short_moa") or "").strip() == raw:
                    moa.link_drug(drug_key, key)

    # ----- Post-build merge: collapse near-duplicate keys -----
    # This renames canonical keys — safe only for full rebuild, NEVER for
    # monthly incremental runs (would break downstream references).
    print("[post] merging near-duplicate entries")
    merge_stats = moa.merge_near_duplicates()
    print(f"  merged {merge_stats['merged']} entries across "
          f"{merge_stats['groups']} groups")

    # ----- Save -----
    moa.metadata = {
        "version": "1.0",
        "built_at": datetime.now().isoformat(),
        "source_drug_master": os.path.basename(DRUG_MASTER_FILE),
        "processed_drugs": len(changeset.processed_drugs),
        "added_moa_keys": len(changeset.added_moa_keys),
        "stage2_unique_inputs": len(changeset.unresolved_raw_strings)
                                if use_gemini else 0,
        "use_gemini": use_gemini,
        "post_merge": merge_stats,
    }
    moa.save()
    print(f"[build_moa_master] wrote {moa.path}")
    print(f"  stats: {moa.stats()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gemini", action="store_true",
                    help="Skip Gemini, use regex only.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N drugs (smoke test).")
    args = ap.parse_args()
    build(use_gemini=not args.no_gemini, limit=args.limit)
