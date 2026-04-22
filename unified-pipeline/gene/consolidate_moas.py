"""
Consolidate singleton MOA entries via two-phase merge.

Phase A — deterministic re-parse
    For each entry with drug_count=1, re-run its canonical_name through
    parse_short_moa_multi. If it resolves to an existing canonical_key,
    queue a merge. Catches cases where improved parser rules now collapse
    a string that previously got its own entry.

Phase B — Gemini semantic match
    For remaining singletons, batch 20 per Gemini call alongside the
    top-100 canonical MOAs as context. Gemini returns either an existing
    key match or null. Strict prompt requires identical target + mutation +
    action + compatible drug class.

Run:
    python consolidate_moas.py                  # full A+B
    python consolidate_moas.py --phase-a-only
    python consolidate_moas.py --dry-run        # report only, no write

Idempotent: Phase B cached by (key, canonical_name) SHA.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from moa_master import MoaMaster
from parser import parse_short_moa_multi
from gemini_client import JsonCache, _call_gemini, _hash, CACHE_DIR


# --------------------------------------------------------------------------
# Phase A — deterministic re-parse
# --------------------------------------------------------------------------

def phase_a_reparse(moa: MoaMaster) -> Dict[str, str]:
    """Map singleton keys to existing canonical keys that re-parse would
    produce from their canonical_name.

    SAFE merges only — requires ALL of:
      - singleton has exactly ONE target (multi-target singletons may
        have info the re-parse lost, e.g. unresolved aromatase in
        "aromatase_cdk4_cdk6_inhibitor")
      - singleton has no unresolved_targets (re-parse would drop them)
      - target canonical has drug_count >= 2 (a real canonical, not
        another singleton — singleton-to-singleton is Phase B's job)
      - gene_targets and action match exactly between singleton and
        target (so we're really merging equivalents, not close cousins)
    """
    merges: Dict[str, str] = {}
    for k, v in list(moa.moas.items()):
        if v.get("drug_count", 0) != 1:
            continue
        if len(v.get("targets") or []) > 1:
            continue
        if v.get("unresolved_targets"):
            continue
        name = (v.get("canonical_name") or "").strip()
        if not name:
            continue
        parsed_list = parse_short_moa_multi(name) or []
        for parsed in parsed_list:
            new_key = parsed.get("canonical_key")
            if not new_key or new_key == k:
                continue
            if new_key not in moa.moas:
                continue
            target = moa.moas[new_key]
            if target.get("drug_count", 0) < 2:
                continue
            if sorted(v.get("gene_targets") or []) != sorted(
                target.get("gene_targets") or []
            ):
                continue
            if v.get("action") != target.get("action"):
                continue
            merges[k] = new_key
            break
    return merges


# --------------------------------------------------------------------------
# Phase B — Gemini semantic match
# --------------------------------------------------------------------------

_CONSOLIDATE_PROMPT = """You are deduplicating a pharmaceutical MOA dictionary.

For each "singleton" MOA entry below, determine whether it is semantically equivalent to any of the "canonical" MOAs from the list.

Two MOAs are equivalent ONLY IF they have ALL of:
  - Identical molecular target(s) (e.g. EGFR vs EGFR/HER2 → NOT equivalent)
  - Identical mutation context, or both unspecified
  - Identical action (inhibitor / agonist / antagonist / blocker / modulator / degrader / activator / antibody / targeted)
  - Compatible drug class

Do NOT merge:
  - Single-target vs multi-target (e.g. "EGFR inhibitor" vs "EGFR/HER2 inhibitor")
  - Different mutation variants (e.g. "KRAS G12C inhibitor" vs "KRAS G12D inhibitor")
  - Different actions (e.g. "PD-1 inhibitor" vs "PD-1 antagonist")
  - Specific-target with pathway-level classes

If equivalent, return the canonical key. Otherwise return null.

Canonical MOAs (top by drug count):
{canonicals}

Singletons to classify:
{singletons}

Return strict JSON:
{"results": [{"singleton_key": "<key>", "match": "<canonical_key or null>", "reason": "<short>"}]}
"""


def _score_canonical(kv) -> Tuple[int, int, int]:
    k, v = kv
    return (
        1 if (v.get("gene_targets") or []) else 0,
        v.get("drug_count", 0),
        -len(k),
    )


def _build_canonicals_block(moa: MoaMaster, n: int = 100,
                             exclude: Optional[Set[str]] = None) -> str:
    exclude = exclude or set()
    eligible = [
        (k, v) for k, v in moa.moas.items()
        if k not in exclude and v.get("drug_count", 0) >= 2
    ]
    top = sorted(eligible, key=_score_canonical, reverse=True)[:n]
    lines = []
    for k, v in top:
        gt = ",".join(v.get("gene_targets") or []) or "-"
        act = v.get("action") or "-"
        lines.append(
            f"  {k} | {v.get('canonical_name', '')} | "
            f"action={act} | genes={gt} | drugs={v.get('drug_count', 0)}"
        )
    return "\n".join(lines)


def _build_singletons_block(singletons: List[Tuple[str, dict]]) -> str:
    lines = []
    for k, v in singletons:
        alias = (v.get("aliases") or ["-"])[0]
        lines.append(
            f"  {k} | canonical_name={v.get('canonical_name', '')!r} | "
            f"raw={alias!r}"
        )
    return "\n".join(lines)


def phase_b_gemini(moa: MoaMaster, *, batch_size: int = 10,
                   max_workers: int = 5,
                   already_merged: Optional[Set[str]] = None) -> Dict[str, str]:
    already_merged = already_merged or set()
    singletons = [
        (k, v) for k, v in moa.moas.items()
        if v.get("drug_count", 0) == 1 and k not in already_merged
    ]
    print(f"[phase B] {len(singletons)} singletons to classify")
    if not singletons:
        return {}

    canonicals_block = _build_canonicals_block(moa, n=60,
                                                exclude=already_merged)
    cache = JsonCache(os.path.join(CACHE_DIR, "consolidate_moa_cache.json"))
    results: Dict[str, str] = {}

    # Split pending into batches after honoring the cache.
    pending: List[List[Tuple[str, dict]]] = []
    current: List[Tuple[str, dict]] = []
    for k, v in singletons:
        cache_key = _hash(f"{k}||{v.get('canonical_name', '')}")
        cached = cache.get(cache_key)
        if cached is not None:
            match = cached.get("match")
            if match and match in moa.moas and match != k:
                results[k] = match
            continue
        current.append((k, v))
        if len(current) >= batch_size:
            pending.append(current)
            current = []
    if current:
        pending.append(current)

    print(f"  cached matches: {len(results)} | batches to run: {len(pending)}")

    def _call_batch(batch_items):
        prompt = _CONSOLIDATE_PROMPT.replace(
            "{canonicals}", canonicals_block
        ).replace(
            "{singletons}", _build_singletons_block(batch_items)
        )
        try:
            resp = _call_gemini(prompt)
        except Exception as exc:
            return None, f"error: {exc}"
        if not resp:
            return None, "empty"
        return resp.get("results") or [], None

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_call_batch, b): b for b in pending}
        for fut in as_completed(futures):
            batch = futures[fut]
            res, err = fut.result()
            done += 1
            if err:
                print(f"  batch error: {err}")
                continue
            batch_keys = {b[0]: b[1] for b in batch}
            for item in res or []:
                if not isinstance(item, dict):
                    continue
                sk = item.get("singleton_key")
                match = item.get("match")
                if not sk or sk not in batch_keys:
                    continue
                # Cache regardless of match value.
                cache.set(
                    _hash(f"{sk}||{batch_keys[sk].get('canonical_name', '')}"),
                    {"match": match, "reason": item.get("reason")},
                )
                if match and match in moa.moas and match != sk:
                    results[sk] = match
            cache.flush()
            if done % 10 == 0:
                print(f"  phase B progress: {done}/{len(pending)} batches | "
                      f"matches so far: {len(results)}")

    print(f"  phase B total matches: {len(results)}")
    return results


# --------------------------------------------------------------------------
# Apply merges
# --------------------------------------------------------------------------

def apply_merges(moa: MoaMaster, merges: Dict[str, str]) -> int:
    """Merge singleton_key → target_key across moas + drug_to_moa."""
    merged = 0
    for singleton_key, target_key in merges.items():
        if singleton_key == target_key:
            continue
        if singleton_key not in moa.moas or target_key not in moa.moas:
            continue
        se = moa.moas.pop(singleton_key)
        te = moa.moas[target_key]
        for field in ("aliases", "gene_targets", "pathway",
                      "example_drugs", "unresolved_targets"):
            src = se.get(field) or []
            dst = te.setdefault(field, [])
            for x in src:
                if x not in dst:
                    dst.append(x)
        # Merge targets[] by raw name.
        te_targets = te.setdefault("targets", [])
        seen_raw = {t.get("raw") for t in te_targets if isinstance(t, dict)}
        for t in se.get("targets") or []:
            if isinstance(t, dict) and t.get("raw") not in seen_raw:
                te_targets.append(t)
                seen_raw.add(t.get("raw"))
        te["drug_count"] = len(te.get("example_drugs") or [])
        # Rewrite drug_to_moa references.
        for drug, mk_list in list(moa.drug_to_moa.items()):
            rewritten = [
                target_key if mk == singleton_key else mk for mk in mk_list
            ]
            seen = []
            for mk in rewritten:
                if mk not in seen:
                    seen.append(mk)
            moa.drug_to_moa[drug] = seen
        merged += 1
    return merged


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(phase_a_only: bool = False, dry_run: bool = False) -> None:
    moa = MoaMaster().load()
    print(f"[consolidate] loaded {len(moa.moas)} MOAs, "
          f"{len(moa.drug_to_moa)} drugs mapped")
    singletons_before = sum(
        1 for v in moa.moas.values() if v.get("drug_count") == 1
    )
    print(f"  singletons (drug_count=1): {singletons_before}")

    # Phase A
    print("[phase A] deterministic re-parse")
    a_merges = phase_a_reparse(moa)
    print(f"  phase A candidates: {len(a_merges)}")
    if a_merges and not dry_run:
        n = apply_merges(moa, a_merges)
        print(f"  phase A applied: {n}")

    if phase_a_only:
        _save(moa, dry_run, a_merges, {}, singletons_before)
        return

    # Phase B
    print("[phase B] gemini semantic match")
    b_merges = phase_b_gemini(moa, already_merged=set(a_merges.keys()))
    print(f"  phase B candidates: {len(b_merges)}")
    if b_merges and not dry_run:
        n = apply_merges(moa, b_merges)
        print(f"  phase B applied: {n}")

    _save(moa, dry_run, a_merges, b_merges, singletons_before)


def _save(moa: MoaMaster, dry_run: bool, a_merges: Dict[str, str],
          b_merges: Dict[str, str], singletons_before: int) -> None:
    singletons_after = sum(
        1 for v in moa.moas.values() if v.get("drug_count") == 1
    )
    print(f"[consolidate] MOAs now: {len(moa.moas)} | "
          f"singletons: {singletons_after} (was {singletons_before})")
    moa.metadata = {
        **(moa.metadata or {}),
        "consolidated_at": datetime.now().isoformat(),
        "phase_a_merges": len(a_merges),
        "phase_b_merges": len(b_merges),
    }
    if dry_run:
        print("[consolidate] dry-run — no write")
        return
    moa.save()
    print(f"[consolidate] wrote {moa.path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase-a-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    main(phase_a_only=args.phase_a_only, dry_run=args.dry_run)
