"""
DrugProcessor — orchestrates drug extraction → master lookup → Gemini enrichment.
"""

from drugs.normalizer import extract_drugs_from_trial
from drugs.drug_master import DrugMaster
from drugs.enricher import DrugEnricher


class DrugProcessor:
    def __init__(self, drug_master, enricher):
        self.dm = drug_master
        self.enricher = enricher
        self._stats = {
            "total_drugs": 0,
            "found_in_master": 0,
            "enriched_new": 0,
            "added_to_master": 0,
            "updated_in_master": 0,
            "skipped": 0,
            "errors": 0,
        }

    def process_trial(self, trial):
        drug_names = extract_drugs_from_trial(trial)
        enriched_drugs = []
        for name in drug_names:
            self._stats["total_drugs"] += 1
            entry = self._process_single_drug(name)
            if entry:
                enriched_drugs.append(entry)
        return enriched_drugs

    def process_all(self, trials):
        """Process every trial sequentially and return {nct_id: drug_list}."""
        results = {}
        total = len(trials)
        for i, (nct_id, trial) in enumerate(trials.items(), 1):
            if i == 1 or i % 50 == 0:
                print(f"[D] [{i}/{total}] {nct_id}...", flush=True)
            results[nct_id] = self.process_trial(trial)
        return results

    def _process_single_drug(self, name):
        entry, _ = self.dm.lookup(name)
        if entry:
            self._stats["found_in_master"] += 1
            return self._as_trial_drug(name, entry)

        enrichment = self.enricher.enrich(name)
        if not enrichment or enrichment.get("error"):
            self._stats["errors"] += 1
            return {
                "normalized_name": name,
                "generic_name": "",
                "brand_names": [],
                "short_moa": "",
                "drug_class": "",
                "company": "",
                "target": "",
                "approval_status": "unknown",
                "needs_review": True,
            }

        action, _ = self.dm.add_or_update(name, enrichment)
        if action == "added":
            self._stats["added_to_master"] += 1
            self._stats["enriched_new"] += 1
        elif action == "updated":
            self._stats["updated_in_master"] += 1
            self._stats["enriched_new"] += 1
        else:
            self._stats["skipped"] += 1

        entry, _ = self.dm.lookup(name)
        if entry:
            return self._as_trial_drug(name, entry)

        return {
            "normalized_name": name,
            "generic_name": enrichment.get("generic_name", ""),
            "brand_names": enrichment.get("brand_names", []),
            "short_moa": enrichment.get("short_moa", ""),
            "drug_class": enrichment.get("drug_class", ""),
            "company": enrichment.get("company", ""),
            "target": enrichment.get("target", ""),
            "approval_status": "approved" if enrichment.get("is_approved") else "investigational",
        }

    @staticmethod
    def _as_trial_drug(name, entry):
        return {
            "normalized_name": name,
            "generic_name": entry.get("generic_name", ""),
            "brand_names": entry.get("brand_names", []),
            "short_moa": entry.get("short_moa", ""),
            "drug_class": entry.get("drug_class", ""),
            "company": entry.get("company", ""),
            "target": entry.get("target", ""),
            "approval_status": entry.get("approval_status", "unknown"),
        }

    @property
    def stats(self):
        return self._stats.copy()
