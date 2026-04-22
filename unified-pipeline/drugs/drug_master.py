"""
DrugMaster — loads, indexes, and mutates drug_master.json.

Every known drug name (generic / brand / alternative / compound code) is
indexed to a canonical key so variant spellings resolve to a single entry.
"""

import json
import os
from datetime import datetime

from config import DRUG_MASTER_FILE, EXCLUDED_CLASSES


PLACEHOLDER_NAMES = {
    "", "...", "....", ".....", "n/a", "na", "none", "null",
    "not applicable", "not available", "multiple", "various",
    "various brand names", "tbd", "unknown", "undefined",
    "drug", "drugs", "compound", "test", "placeholder",
}

MAX_BRAND_NAMES = 50
MAX_OTHER_NAMES = 50


def _is_placeholder(name):
    if not isinstance(name, str):
        return True
    return name.strip().lower() in PLACEHOLDER_NAMES


class DrugMaster:
    def __init__(self, path=None):
        self.path = path or DRUG_MASTER_FILE
        self.drugs = {}
        self._index = {}
        self._dirty = False

    def load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
            self.drugs = data.get("drugs", {})
        else:
            self.drugs = {}
        self._rebuild_index()
        return self

    def _rebuild_index(self):
        self._index = {}
        for key, entry in self.drugs.items():
            self._add_to_index(key, entry)

    def _add_to_index(self, key, entry):
        # Defensive: if the canonical key itself is a placeholder, the entry
        # is a corrupted bucket — refuse to index it so it can never be
        # discovered by lookup() and continue absorbing unrelated drugs.
        if _is_placeholder(key):
            return
        k = key.lower().strip()
        if k and not _is_placeholder(k):
            self._index[k] = key
        gn = (entry.get("generic_name") or "").lower().strip()
        if gn and not _is_placeholder(gn) and gn not in self._index:
            self._index[gn] = key
        for name in entry.get("brand_names", []) or []:
            n = name.lower().strip()
            if n and not _is_placeholder(n) and n not in self._index:
                self._index[n] = key
        for name in entry.get("other_names", []) or []:
            n = name.lower().strip()
            if n and not _is_placeholder(n) and n not in self._index:
                self._index[n] = key

    def lookup(self, name):
        if not name:
            return None, None
        k = name.lower().strip()
        canon = self._index.get(k)
        if canon:
            return self.drugs[canon], canon
        return None, None

    def add_or_update(self, raw_name, enrichment):
        """Add/update an entry from a Gemini enrichment result.

        Returns (action, reason) where action ∈ {"added","updated","skipped"}.
        """
        if not enrichment or not isinstance(enrichment, dict):
            return "skipped", "no enrichment data"
        if enrichment.get("error"):
            return "skipped", "enrichment error"
        if enrichment.get("is_drug") is False:
            return "skipped", "not a drug"

        dc = (enrichment.get("drug_class") or "").lower().strip()
        if dc in EXCLUDED_CLASSES:
            return "skipped", f"excluded class: {dc}"

        # Reject placeholder-shaped raw_name outright — there is no real drug
        # called "...", "N/A", etc., and accepting one creates a bucket that
        # absorbs every subsequent unidentified enrichment.
        if _is_placeholder(raw_name):
            return "skipped", f"placeholder raw_name: {raw_name!r}"

        # Reject the entire enrichment if generic_name is a placeholder —
        # that's the signature of a Gemini template echo, and the rest of
        # the fields can't be trusted either. Don't fall back to raw_name;
        # that's exactly what created the original "..." / "N/A" buckets.
        gn_raw = enrichment.get("generic_name")
        if _is_placeholder(gn_raw):
            return "skipped", f"placeholder generic_name (raw_name={raw_name!r})"
        gn = gn_raw.lower().strip()
        if not gn or _is_placeholder(gn):
            return "skipped", "placeholder generic_name"

        all_alt = set()
        for alt in enrichment.get("alternative_names", []) or []:
            if alt and not _is_placeholder(alt) and alt.lower() != gn:
                all_alt.add(alt)
        if raw_name.lower() != gn and not _is_placeholder(raw_name):
            all_alt.add(raw_name)

        brand_names = [
            b for b in (enrichment.get("brand_names") or [])
            if b and not _is_placeholder(b)
        ]

        existing, canon_key = self.lookup(gn)
        if not existing:
            existing, canon_key = self.lookup(raw_name)

        if existing:
            # Defense-in-depth: if the lookup somehow returned a bucket
            # entry, refuse to merge — that's the original failure mode.
            if (
                _is_placeholder(canon_key)
                or len(existing.get("brand_names") or []) > MAX_BRAND_NAMES
                or len(existing.get("other_names") or []) > MAX_OTHER_NAMES
            ):
                return "skipped", f"refused merge into bucket-shaped entry {canon_key!r}"
            changed = False
            for field in ("short_moa", "long_moa", "drug_class", "company", "target"):
                if enrichment.get(field) and not existing.get(field):
                    existing[field] = enrichment[field]
                    changed = True
            eb = set(existing.get("brand_names", []) or [])
            for b in brand_names:
                if b not in eb:
                    eb.add(b)
                    changed = True
            existing["brand_names"] = sorted(eb)
            ea = set(existing.get("other_names", []) or [])
            for a in all_alt:
                if a.lower() not in {x.lower() for x in ea}:
                    ea.add(a)
            existing["other_names"] = sorted(ea)
            self._add_to_index(canon_key, existing)
            self._dirty = True
            return ("updated", f"updated {canon_key}") if changed else ("skipped", f"already complete in {canon_key}")

        entry = {
            "generic_name": enrichment.get("generic_name") if not _is_placeholder(enrichment.get("generic_name")) else raw_name,
            "brand_names": sorted(set(brand_names)),
            "other_names": sorted(all_alt),
            "short_moa": enrichment.get("short_moa", ""),
            "long_moa": enrichment.get("long_moa", ""),
            "target": enrichment.get("target", ""),
            "drug_class": enrichment.get("drug_class", ""),
            "company": enrichment.get("company", ""),
            "approval_status": "approved" if enrichment.get("is_approved") else "investigational",
            "approved_indications": enrichment.get("approved_indications", []),
            "source": "unified_pipeline",
            "added_date": datetime.now().isoformat(),
        }
        self.drugs[gn] = entry
        self._add_to_index(gn, entry)
        self._dirty = True
        return "added", gn

    def save(self, path=None):
        out = path or self.path
        data = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "total_compounds": len(self.drugs),
                "version": "unified-1.0",
            },
            "drugs": self.drugs,
        }
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(data, f, indent=2, default=str)
        self._dirty = False

    @property
    def is_dirty(self):
        return self._dirty

    def __len__(self):
        return len(self.drugs)

    def stats(self):
        total = len(self.drugs)
        if total == 0:
            return {"total": 0}
        fields = ("generic_name", "short_moa", "drug_class", "company", "target")
        return {"total": total, **{f: sum(1 for e in self.drugs.values() if e.get(f)) for f in fields}}
