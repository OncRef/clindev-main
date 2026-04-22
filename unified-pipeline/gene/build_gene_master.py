"""
Orchestrator: build `gene_master.json` from `New_Gene_updated.xlsx`.

Steps:
    1. Seed each gene from the Excel (aliases + broad cancers).
    2. Enrich each gene via Gemini 2.5 Flash:
         - detailed_conditions (specific cancer subtypes)
         - mutation_hotspots
         - pathway
         - role / notes
    3. Cross-reference moa_master.json: fill `moas_targeting` and
       `example_drugs` from MOAs whose gene_targets contain this gene.

Run:
    python build_gene_master.py
    python build_gene_master.py --no-gemini       # excel + moa_master only
    python build_gene_master.py --limit 10        # smoke test 10 genes

Idempotent: cached responses are reused across runs.
"""

import argparse
import os
import sys
from datetime import datetime
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import json
import re

from gene_master import GeneMaster
from moa_master import MoaMaster
from gemini_client import enrich_gene, JsonCache

CANCER_VOCAB_PATH = os.path.join(_HERE, "dictionaries", "cancer_vocab.json")


def _load_cancer_vocab() -> Optional[dict]:
    """Load cancer_vocab.json, or None if it doesn't exist yet."""
    if not os.path.exists(CANCER_VOCAB_PATH):
        return None
    try:
        with open(CANCER_VOCAB_PATH) as f:
            return json.load(f)
    except Exception:
        return None


_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")


def _normalize_condition(s: str) -> str:
    """Lowercase, strip parenthetical acronyms like "(CRC)" / "(mCRC)"."""
    s = s.lower().strip()
    s = _PAREN_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _filter_conditions_to_vocab(conditions, vocab: dict,
                                 include_tissue_level: bool = False) -> tuple:
    """Constrain Gemini's detailed_conditions to canonical oncotree names.

    By default, ``tissue_codes`` (level-1 oncotree entries like "Breast",
    "Lung", "Prostate") are excluded so ``detailed_conditions`` holds
    real cancer subtypes, not organ names. Pass ``include_tissue_level=
    True`` to keep tissue-level matches in the output.

    Matching strategy (in order, first hit wins):
      1. Direct oncotree code (e.g. "DLBCL")
      2. Parenthetical-code match (e.g. "Colorectal Cancer (CRC)")
      3. Normalized-name equality against subtype codes / broad_cancers
      4. Substring containment against canonical subtype names
    Parenthetical acronyms are stripped before matching so Gemini
    suffixes don't cause false drops.
    """
    if not vocab or not conditions:
        return list(conditions or []), []

    name_to_code = vocab.get("name_to_oncotree") or {}
    # Prefer subtype_codes when available (level ≥ 2 only); fall back to
    # the full oncotree_to_name for vocabs built by older versions.
    if include_tissue_level:
        canonical_map = vocab.get("oncotree_to_name") or {}
    else:
        canonical_map = (vocab.get("subtype_codes")
                          or vocab.get("oncotree_to_name") or {})
    canonical_names = set(canonical_map.values())
    canonical_lower = {n.lower(): n for n in canonical_names}
    broad_lower = {b.lower(): b for b in (vocab.get("broad_cancers") or [])}
    codes_upper = set(canonical_map.keys())

    kept: list = []
    dropped: list = []
    for cond in conditions:
        if not isinstance(cond, str):
            continue
        c = cond.strip()
        if not c:
            continue
        # 1. Direct oncotree code (case-insensitive)
        if c.upper() in codes_upper:
            matched = canonical_map[c.upper()]
            if matched not in kept:
                kept.append(matched)
            continue
        # 2. Also check for code embedded in parens, e.g. "... (DLBCL)"
        paren_hits = re.findall(r"\(([A-Z][A-Z0-9_]{1,10})\)", c)
        matched_via_paren = None
        for h in paren_hits:
            if h in codes_upper:
                matched_via_paren = canonical_map[h]
                break
        if matched_via_paren:
            if matched_via_paren not in kept:
                kept.append(matched_via_paren)
            continue
        # 3. Normalized-name equality.
        norm = _normalize_condition(c)
        if norm in canonical_lower:
            matched = canonical_lower[norm]
            if matched not in kept:
                kept.append(matched)
            continue
        if norm in name_to_code:
            code = name_to_code[norm]
            # name_to_code can point to any level; only keep if that code
            # survives the tissue-level filter (i.e. it's in canonical_map).
            matched = canonical_map.get(code)
            if matched and matched not in kept:
                kept.append(matched)
                continue
        if norm in broad_lower:
            matched = broad_lower[norm]
            if matched not in kept:
                kept.append(matched)
            continue
        # 4. Substring containment against canonical names.
        matched = None
        for canon_lower, canon_name in canonical_lower.items():
            if canon_lower in norm or norm in canon_lower:
                matched = canon_name
                break
        if matched:
            if matched not in kept:
                kept.append(matched)
        else:
            dropped.append(c)

    return kept, dropped


def build(use_gemini: bool = True, limit: Optional[int] = None,
          include_tissue_level: bool = False) -> None:
    print("[build_gene_master] loading Excel")
    gm = GeneMaster().load_from_excel()
    print(f"  {len(gm.genes)} genes")

    # If we're not running Gemini this time, pull any prior enrichment
    # (detailed_conditions, mutation_hotspots, pathway, role, notes)
    # from the existing gene_master.json so --no-gemini doesn't wipe it.
    # When use_gemini is True, Gemini output will populate those fields
    # directly, so merging prior state is unnecessary.
    if not use_gemini:
        merged = gm.merge_existing_enrichment()
        if merged:
            print(f"  preserved prior enrichment for {merged} genes "
                  f"(--no-gemini mode)")

    print("[build_gene_master] loading moa_master.json")
    moa = MoaMaster().load()
    if not moa.moas:
        print("  WARNING: moa_master.json is empty — run build_moa_master.py first")
    else:
        print(f"  {len(moa.moas)} MOAs ({sum(1 for v in moa.moas.values() if v.get('gene_targets'))} gene-linked)")

    vocab = _load_cancer_vocab()
    if vocab:
        print(f"  cancer vocab: {len(vocab.get('oncotree_to_name') or {})} "
              f"oncotree codes, {len(vocab.get('broad_cancers') or [])} broad")
    else:
        print("  cancer vocab: not found — run build_cancer_vocab.py first "
              "to constrain detailed_conditions")

    # Stage 2: Gemini enrichment per gene (concurrent)
    if use_gemini:
        print("[stage 2] gemini 2.5 flash (per gene, concurrent)")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        cache = JsonCache(os.path.join(_HERE, "cache", "gene_gemini_cache.json"))

        def _fetch(gene: str) -> tuple:
            # JsonCache is thread-safe; enrich_gene handles cache read/write.
            entry = gm.genes[gene]
            resp = enrich_gene(
                gene,
                entry["aliases"],
                entry["broad_cancers"],
                cache=cache,
            )
            return gene, resp

        gene_keys = list(gm.genes.keys())
        if limit:
            gene_keys = gene_keys[:limit]

        ok = 0
        filtered_out = 0
        done_count = 0

        # 10 concurrent workers — Gemini Flash handles this comfortably and
        # this makes the 461-gene run finish in minutes, not an hour.
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_fetch, g): g for g in gene_keys}
            for fut in as_completed(futures):
                gene, resp = fut.result()
                done_count += 1
                if resp:
                    apply_resp = resp
                    dropped_for_audit: list = []
                    if vocab and isinstance(resp, dict):
                        raw_conds = resp.get("detailed_conditions") or []
                        kept, dropped_for_audit = _filter_conditions_to_vocab(
                            raw_conds, vocab,
                            include_tissue_level=include_tissue_level,
                        )
                        apply_resp = {**resp, "detailed_conditions": kept}
                        filtered_out += len(dropped_for_audit)
                    gm.apply_gemini_enrichment(gene, apply_resp)
                    if dropped_for_audit:
                        gm.genes[gene][
                            "detailed_conditions_dropped"
                        ] = dropped_for_audit
                    ok += 1
                if done_count % 25 == 0:
                    print(f"  {done_count}/{len(gene_keys)}  ({ok} enriched)")

        print(f"  total enriched: {ok}/{len(gene_keys)} | "
              f"conditions dropped as off-vocab: {filtered_out}")
    else:
        print("[stage 2] skipped (--no-gemini)")

    # Stage 3: cross-reference MOA master
    print("[stage 3] cross-referencing moa_master.json")
    gm.apply_moa_master(moa)
    n_with_moas = sum(1 for v in gm.genes.values() if v["moas_targeting"])
    print(f"  genes with moas_targeting: {n_with_moas}/{len(gm.genes)}")

    gm.metadata = {
        "version": "1.0",
        "built_at": datetime.now().isoformat(),
        "use_gemini": use_gemini,
    }
    gm.save()
    print(f"[build_gene_master] wrote {gm.path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gemini", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include-tissue-level", action="store_true",
                    help="Keep level-1 oncotree codes (Breast, Lung, ...) "
                         "in detailed_conditions. Default: excluded.")
    args = ap.parse_args()
    build(use_gemini=not args.no_gemini, limit=args.limit,
          include_tissue_level=args.include_tissue_level)
