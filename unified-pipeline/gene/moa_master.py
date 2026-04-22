"""
MOA Master — dictionary management.

Mirrors the DrugMaster class in ../drugs/drug_master.py. Holds the
canonical MOA entries, the broad-class entries, and the drug → MOA
reverse index.

Shape:
    {
        "metadata": {...},
        "moas": { "<canonical_key>": { ...entry... } },
        "classes": { "<class_slug>": { ...entry... } },
        "drug_to_moa": { "<drug_name>": ["<moa_key>", ...] }
    }
"""

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(_HERE, "dictionaries", "moa_master.json")


# --------------------------------------------------------------------------
# Helpers used by ingest_moas() — module-level so both full-rebuild
# (build_moa_master.py) and incremental (ingest_monthly.py) orchestrators
# can import them.
# --------------------------------------------------------------------------

def _resolve_targets(targets: List[str], gm) -> List[str]:
    """Map parsed target tokens to canonical gene symbols via GeneMaster."""
    out: List[str] = []
    for t in targets:
        g = gm.resolve_target(t)
        if g and g not in out:
            out.append(g)
    return out


def _mk_canonical_name(targets: List[str], mutations: List[str],
                       action: Optional[str],
                       fallback: Optional[str] = None) -> str:
    """Build a human-readable canonical name. Falls back to the raw input
    when targets+action are empty (prevents the old 'unknown' artifact)."""
    t = "/".join(targets) if targets else ""
    m = " ".join(mutations) if mutations else ""
    parts = [p for p in (t, m, action) if p]
    name = " ".join(parts).strip()
    if name:
        return name
    if fallback:
        f = fallback.strip()
        if f:
            return f
    return "unknown"


# --------------------------------------------------------------------------
# ChangeSet — what an ingestion pass actually changed
# --------------------------------------------------------------------------

@dataclass
class ChangeSet:
    """Describes what a single `ingest_moas()` call added to the master.

    Fields:
      processed_drugs       — every drug the caller handed in, whether or
                              not it produced a change (useful audit)
      added_moa_keys        — canonical keys that did NOT exist before this
                              ingest call
      extended_moa_keys     — keys that already existed but gained new
                              aliases / example_drugs / gene_targets
      affected_genes        — HGNC symbols (or Excel gene rows) whose
                              coverage could have changed; pass these to
                              ``GeneMaster.refresh_for_genes()``
      new_unresolved_targets — target tokens that parsed but didn't resolve
                              to an Excel gene this run — candidates for
                              Tom's review in the next supplement pre-pass
      unresolved_raw_strings — short_moa strings Stage 1 couldn't parse and
                              Stage 2 (Gemini) was skipped or also failed
      errors                — per-input error records (not fatal)
    """
    processed_drugs: List[str] = field(default_factory=list)
    added_moa_keys: Set[str] = field(default_factory=set)
    extended_moa_keys: Set[str] = field(default_factory=set)
    affected_genes: Set[str] = field(default_factory=set)
    new_unresolved_targets: Set[str] = field(default_factory=set)
    unresolved_raw_strings: Set[str] = field(default_factory=set)
    errors: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "processed_drugs": len(self.processed_drugs),
            "added_moa_keys": sorted(self.added_moa_keys),
            "extended_moa_keys": sorted(self.extended_moa_keys),
            "affected_genes": sorted(self.affected_genes),
            "new_unresolved_targets": sorted(self.new_unresolved_targets),
            "unresolved_raw_strings": sorted(self.unresolved_raw_strings),
            "errors": self.errors,
        }

    def summary(self) -> str:
        return (
            f"processed={len(self.processed_drugs)} | "
            f"added={len(self.added_moa_keys)} | "
            f"extended={len(self.extended_moa_keys)} | "
            f"affected_genes={len(self.affected_genes)} | "
            f"new_unresolved={len(self.new_unresolved_targets)} | "
            f"errors={len(self.errors)}"
        )


class MoaMaster:
    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_PATH
        self.moas: Dict[str, dict] = {}
        self.classes: Dict[str, dict] = {}
        self.drug_to_moa: Dict[str, List[str]] = {}
        self.metadata: Dict = {}

    # -- load / save ------------------------------------------------------

    def load(self) -> "MoaMaster":
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
            self.moas = data.get("moas", {})
            self.classes = data.get("classes", {})
            self.drug_to_moa = data.get("drug_to_moa", {})
            self.metadata = data.get("metadata", {})
        return self

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "metadata": {
                **self.metadata,
                "timestamp": datetime.now().isoformat(),
                "total_moas": len(self.moas),
                "total_classes": len(self.classes),
                "total_drugs_mapped": len(self.drug_to_moa),
            },
            "moas": self.moas,
            "classes": self.classes,
            "drug_to_moa": self.drug_to_moa,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    # -- add / update -----------------------------------------------------

    def add_moa(self, key: str, *, canonical_name: str, targets: List[dict],
                gene_targets: List[str], action: Optional[str],
                drug_class: Optional[str], pathway: Optional[List[str]],
                is_class_only: bool, source: str,
                unresolved_targets: Optional[List[str]] = None) -> dict:
        """Create or fetch a canonical MOA entry."""
        if key in self.moas:
            entry = self.moas[key]
            # Merge gene_targets / pathway / unresolved_targets if we learn
            # new ones later.
            for g in gene_targets:
                if g not in entry["gene_targets"]:
                    entry["gene_targets"].append(g)
            for p in pathway or []:
                if p not in entry.get("pathway", []):
                    entry.setdefault("pathway", []).append(p)
            for u in unresolved_targets or []:
                ul = entry.setdefault("unresolved_targets", [])
                if u not in ul:
                    ul.append(u)
            return entry

        entry = {
            "canonical_name": canonical_name,
            "aliases": [],
            "targets": targets,
            "gene_targets": gene_targets,
            "action": action,
            "drug_class": drug_class,
            "pathway": pathway or [],
            "is_class_only": is_class_only,
            "unresolved_targets": list(unresolved_targets or []),
            "drug_count": 0,
            "example_drugs": [],
            "source": source,
        }
        self.moas[key] = entry
        return entry

    def add_class(self, key: str, *, canonical_name: str) -> dict:
        if key in self.classes:
            return self.classes[key]
        entry = {
            "canonical_name": canonical_name,
            "aliases": [],
            "drug_count": 0,
            "example_drugs": [],
        }
        self.classes[key] = entry
        return entry

    def record_alias(self, moa_key: str, alias: str) -> None:
        if moa_key in self.moas:
            if alias and alias not in self.moas[moa_key]["aliases"]:
                self.moas[moa_key]["aliases"].append(alias)
        elif moa_key in self.classes:
            if alias and alias not in self.classes[moa_key]["aliases"]:
                self.classes[moa_key]["aliases"].append(alias)

    def link_drug(self, drug_name: str, moa_key: str) -> None:
        """Record drug_name → moa_key (both directions)."""
        if not drug_name or not moa_key:
            return

        keys = self.drug_to_moa.setdefault(drug_name, [])
        if moa_key not in keys:
            keys.append(moa_key)

        target = self.moas.get(moa_key) or self.classes.get(moa_key)
        if target is not None:
            if drug_name not in target["example_drugs"]:
                target["example_drugs"].append(drug_name)
            target["drug_count"] = len(target["example_drugs"])

    # -- ingest (library entry point for monthly / incremental mode) ------

    def ingest_moas(self, drug_entries: Dict[str, dict], gm,
                    *, use_gemini: bool = True,
                    gemini_cache_path: Optional[str] = None) -> ChangeSet:
        """Upsert MOAs for a set of drugs. Idempotent.

        Args:
          drug_entries: {drug_key: drug_entry} subset from drug_master.json
          gm: a loaded GeneMaster (with supplement applied)
          use_gemini: run Stage-2 Gemini for unresolved strings
          gemini_cache_path: override default MOA cache location

        Returns a ChangeSet. Does NOT call ``merge_near_duplicates()`` —
        that's a full-rebuild concern (it renames canonical keys, which
        is unsafe for incremental / monthly runs).
        """
        # Imports are local to avoid build_moa_master ↔ moa_master cycles.
        import sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from parser import parse_short_moa_multi, normalize_drug_class  # type: ignore

        cs = ChangeSet()

        unresolved_strings: Counter = Counter()
        unresolved_drugs: Dict[str, List[str]] = {}

        # ---- Stage 1: deterministic ----
        for drug_key, entry in drug_entries.items():
            raw = (entry.get("short_moa") or "")
            dc_hint = normalize_drug_class(entry.get("drug_class"))
            cs.processed_drugs.append(drug_key)

            parsed_list = parse_short_moa_multi(raw)
            if not parsed_list:
                if raw:
                    stripped = raw.strip()
                    unresolved_strings[stripped] += 1
                    unresolved_drugs.setdefault(stripped, []).append(drug_key)
                else:
                    # No short_moa at all — fall back to drug_class as class-only.
                    if dc_hint:
                        existed = dc_hint in self.classes
                        self.add_class(dc_hint, canonical_name=dc_hint)
                        self.link_drug(drug_key, dc_hint)
                        if not existed:
                            cs.added_moa_keys.add(dc_hint)
                        else:
                            cs.extended_moa_keys.add(dc_hint)
                continue

            for parsed in parsed_list:
                self._ingest_parsed(
                    gm, drug_key, raw, parsed,
                    source="deterministic",
                    drug_class_hint=dc_hint,
                    change_set=cs,
                )

        # ---- Stage 2: Gemini for anything Stage 1 couldn't parse ----
        if use_gemini and unresolved_strings:
            from gemini_client import enrich_moa_batch, JsonCache  # type: ignore
            if gemini_cache_path is None:
                gemini_cache_path = os.path.join(
                    _here, "cache", "moa_gemini_cache.json"
                )
            cache = JsonCache(gemini_cache_path)
            responses = enrich_moa_batch(
                list(unresolved_strings.keys()), cache=cache, batch_size=20
            )
            for raw, resp in responses.items():
                for drug_key in unresolved_drugs.get(raw, []):
                    self._ingest_gemini(gm, drug_key, raw, resp, cs)
            # Any string the Gemini pass didn't resolve → explicit audit.
            for raw in unresolved_strings:
                if raw not in responses:
                    cs.unresolved_raw_strings.add(raw)
        else:
            for raw in unresolved_strings:
                cs.unresolved_raw_strings.add(raw)

        return cs

    def _ingest_parsed(self, gm, drug_key: str, raw: str, parsed: dict,
                       *, source: str, drug_class_hint: Optional[str],
                       change_set: ChangeSet) -> None:
        """Add or extend one Stage-1-parsed MOA. Updates change_set."""
        canonical_key = parsed.get("canonical_key")
        if not canonical_key:
            return

        targets = parsed.get("targets") or []
        mutations = parsed.get("mutations") or []
        action = parsed.get("action")
        is_class_only = bool(parsed.get("is_class_only"))

        gene_targets = _resolve_targets(targets, gm)
        unresolved = [t for t in targets if gm.resolve_target(t) is None]

        target_entries = []
        for tk in targets:
            g = gm.resolve_target(tk)
            target_entries.append({
                "raw": tk,
                "gene_symbol": g,
                "mutation": mutations[0] if len(mutations) == 1 else None,
            })

        existed = canonical_key in self.moas
        self.add_moa(
            canonical_key,
            canonical_name=_mk_canonical_name(
                gene_targets or targets, mutations, action, fallback=raw
            ),
            targets=target_entries,
            gene_targets=gene_targets,
            action=action,
            drug_class=drug_class_hint,
            pathway=None,
            is_class_only=is_class_only,
            source=source,
            unresolved_targets=unresolved,
        )
        self.record_alias(canonical_key, raw)
        self.link_drug(drug_key, canonical_key)

        if existed:
            change_set.extended_moa_keys.add(canonical_key)
        else:
            change_set.added_moa_keys.add(canonical_key)
        for g in gene_targets:
            change_set.affected_genes.add(g)
        for u in unresolved:
            change_set.new_unresolved_targets.add(u)

    def _ingest_gemini(self, gm, drug_key: str, raw: str, resp: dict,
                       change_set: ChangeSet) -> None:
        """Add or extend one Gemini-classified MOA. Updates change_set."""
        if not isinstance(resp, dict):
            return

        import sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from parser import _canonical_key, _slugify  # type: ignore

        # Non-oncology string → record but don't invent gene targets.
        if not resp.get("is_oncology_relevant", True):
            key = f"nononc_{abs(hash(raw)) % 10**8}"
            existed = key in self.moas
            self.add_moa(
                key, canonical_name=raw, targets=[], gene_targets=[],
                action=None, drug_class=resp.get("drug_class"),
                pathway=resp.get("pathway") or [],
                is_class_only=True, source="gemini",
            )
            self.record_alias(key, raw)
            self.link_drug(drug_key, key)
            (change_set.extended_moa_keys if existed
             else change_set.added_moa_keys).add(key)
            return

        targets = [t for t in (resp.get("targets") or []) if t]
        mutations = [m for m in (resp.get("mutations") or []) if m]
        action = resp.get("action")
        is_class_only = bool(resp.get("is_class_only"))

        gene_targets: List[str] = []
        for candidate in (resp.get("hgnc_candidates") or []):
            if not candidate:
                continue
            g = gm.resolve_target(candidate)
            if g and g not in gene_targets:
                gene_targets.append(g)
        if not gene_targets:
            for t in targets:
                g = gm.resolve_target(t)
                if g and g not in gene_targets:
                    gene_targets.append(g)

        if gene_targets and action:
            key = _canonical_key(gene_targets, mutations, action)
        elif targets and action:
            key = _canonical_key(targets, mutations, action)
        else:
            key = _slugify(raw)[:80]
        if not key:
            return

        unresolved = [t for t in targets if t and gm.resolve_target(t) is None]

        target_entries = []
        src_targets = gene_targets or targets
        for tk in src_targets:
            g = gm.resolve_target(tk) if tk else None
            target_entries.append({
                "raw": tk,
                "gene_symbol": g,
                "mutation": mutations[0] if len(mutations) == 1 else None,
            })

        existed = key in self.moas
        self.add_moa(
            key,
            canonical_name=_mk_canonical_name(
                src_targets, mutations, action, fallback=raw
            ),
            targets=target_entries,
            gene_targets=gene_targets,
            action=action,
            drug_class=resp.get("drug_class"),
            pathway=resp.get("pathway") or [],
            is_class_only=is_class_only,
            source="gemini",
            unresolved_targets=unresolved,
        )
        self.record_alias(key, raw)
        self.link_drug(drug_key, key)

        if existed:
            change_set.extended_moa_keys.add(key)
        else:
            change_set.added_moa_keys.add(key)
        for g in gene_targets:
            change_set.affected_genes.add(g)
        for u in unresolved:
            change_set.new_unresolved_targets.add(u)

    # -- post-build merge -------------------------------------------------

    def merge_near_duplicates(self) -> dict:
        """Collapse entries whose keys re-slugify to the same canonical form.

        Catches duplicates that leak through different code paths (e.g., a
        Stage 1 class-only entry keyed as ``multi_kinase_inhibitor`` and a
        Stage 2 Gemini entry keyed as ``multikinase_inhibitor``).

        Returns stats: ``{"merged": N, "groups": M}``.
        """
        import sys, os
        _HERE = os.path.dirname(os.path.abspath(__file__))
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        from parser import _slugify  # type: ignore

        # Bucket keys by their re-slugified canonical form.
        buckets: Dict[str, List[str]] = {}
        for k in list(self.moas.keys()):
            canon = _slugify(k.replace("_", " "))
            buckets.setdefault(canon, []).append(k)

        merged_count = 0
        groups_changed = 0
        for canon, keys in buckets.items():
            if len(keys) <= 1:
                continue
            groups_changed += 1
            # Pick the "richest" entry as the winner: most gene_targets,
            # then most aliases, then longest canonical_name.
            def _score(k: str) -> tuple:
                e = self.moas[k]
                return (
                    1 if e.get("gene_targets") else 0,
                    len(e.get("gene_targets") or []),
                    len(e.get("aliases") or []),
                    len(e.get("canonical_name") or ""),
                    -len(k),  # shorter key wins on ties
                )
            keys_sorted = sorted(keys, key=_score, reverse=True)
            winner = keys_sorted[0]
            we = self.moas[winner]
            for loser in keys_sorted[1:]:
                le = self.moas.pop(loser)
                merged_count += 1
                # Union aliases, gene_targets, pathway, example_drugs,
                # unresolved_targets.
                for field in ("aliases", "gene_targets", "pathway",
                              "example_drugs", "unresolved_targets"):
                    src = le.get(field) or []
                    dst = we.setdefault(field, [])
                    for v in src:
                        if v not in dst:
                            dst.append(v)
                # Merge targets[] by raw name
                we_targets = we.setdefault("targets", [])
                seen_raw = {t.get("raw") for t in we_targets if isinstance(t, dict)}
                for t in le.get("targets") or []:
                    if isinstance(t, dict) and t.get("raw") not in seen_raw:
                        we_targets.append(t)
                        seen_raw.add(t.get("raw"))
                # Sum drug_count (will be recomputed from example_drugs)
                we["drug_count"] = len(we.get("example_drugs") or [])
                # If loser had a better canonical_name (not "unknown"), keep it.
                if we.get("canonical_name") in (None, "", "unknown") \
                        and le.get("canonical_name") not in (None, "", "unknown"):
                    we["canonical_name"] = le["canonical_name"]
                # is_class_only: AND over both (if either is gene-linked, final is gene-linked)
                we["is_class_only"] = bool(we.get("is_class_only")) and bool(le.get("is_class_only"))
                # Rewrite drug_to_moa references loser → winner
                for drug, mk_list in self.drug_to_moa.items():
                    self.drug_to_moa[drug] = [
                        winner if mk == loser else mk for mk in mk_list
                    ]
                    # dedupe
                    seen = []
                    for mk in self.drug_to_moa[drug]:
                        if mk not in seen:
                            seen.append(mk)
                    self.drug_to_moa[drug] = seen
            # If winner key differs from canon slug, also rename to canon.
            if winner != canon and canon not in self.moas:
                self.moas[canon] = self.moas.pop(winner)
                for drug, mk_list in self.drug_to_moa.items():
                    self.drug_to_moa[drug] = [
                        canon if mk == winner else mk for mk in mk_list
                    ]

        return {"merged": merged_count, "groups": groups_changed}

    # -- queries ----------------------------------------------------------

    def moas_for_gene(self, gene: str) -> List[str]:
        """Return canonical keys of all MOAs that target the given gene."""
        return [k for k, v in self.moas.items() if gene in v.get("gene_targets", [])]

    def gene_targets_for_drug(self, drug_name: str) -> Set[str]:
        """Union of gene_targets across all MOAs for a drug."""
        genes: Set[str] = set()
        for mk in self.drug_to_moa.get(drug_name, []):
            entry = self.moas.get(mk)
            if entry:
                genes.update(entry.get("gene_targets", []))
        return genes

    def stats(self) -> dict:
        return {
            "moas": len(self.moas),
            "classes": len(self.classes),
            "drugs_mapped": len(self.drug_to_moa),
            "gene_targeting_moas": sum(
                1 for v in self.moas.values() if v.get("gene_targets")
            ),
            "class_only_moas": sum(
                1 for v in self.moas.values() if v.get("is_class_only")
            ),
        }
