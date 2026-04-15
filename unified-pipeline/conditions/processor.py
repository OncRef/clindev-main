"""
ConditionProcessor — runs steps 1-5b for each trial.

Flow per trial:
  for each raw condition string:
    1. extract_components  (LOT / stage / genes)
    2-3. classify_condition (tier 1 → tier 3 lookup ladder)
  4. Gemini fallback for anything still unclassified
  5a. If no broad cancer yet, classify_from_title (regex)
  5b. If still nothing, Gemini on the title
"""

from conditions.extractor import load_extractor_config, extract_components
from conditions.classifier import (
    load_classifier_config, classify_condition, classify_from_title,
    llm_classify_conditions, llm_classify_title, save_llm_conditions_cache,
)


class ConditionProcessor:
    def __init__(self):
        print("[C] Loading condition dictionaries...")
        self.ext_cfg = load_extractor_config()
        self.cls_cfg = load_classifier_config()
        self._bc_names = {bc["name"] for bc in self.cls_cfg["broad_cancer_kw"]}
        self._stats = {
            "trials": 0,
            "conditions_total": 0,
            "tier_1": 0, "tier_1.5_recycle": 0, "tier_2": 0, "tier_2_substr": 0,
            "tier_3": 0, "llm": 0, "unclassifiable": 0,
            "title_fallback": 0,
            "no_broad_cancer": 0,
        }
        print(f"[C]   {len(self.cls_cfg['llm_conditions'])} cached Gemini classifications, "
              f"{len(self.cls_cfg['broad_cancer_kw'])} broad cancer categories")

    def process_trial(self, trial):
        """Return the conditions-side slice of the conquest_master entry."""
        self._stats["trials"] += 1

        enriched_conditions = []
        broad_cancers = set()
        all_stages, all_genes, all_lot = set(), set(), set()
        needs_llm = []  # (cond, core, components)

        for cond in trial.get("conditions", []) or []:
            self._stats["conditions_total"] += 1
            core, components = extract_components(cond, self.ext_cfg)

            for s in components["stage"]: all_stages.add(s)
            for g in components["genes"]: all_genes.add(g)
            for l in components["line_of_therapy"]: all_lot.add(l)

            result = classify_condition(cond, core, self.cls_cfg)
            if result is not None:
                bcs, tier = result
                self._stats[tier] = self._stats.get(tier, 0) + 1
                for bc in bcs: broad_cancers.add(bc)
                enriched_conditions.append({
                    "condition": cond, "core_condition": core,
                    "broad_cancers": bcs, "tier": tier,
                    "extracted_components": components,
                })
            else:
                needs_llm.append((cond, core, components))

        # Step 4: Gemini fallback for unclassified conditions
        if needs_llm:
            cond_texts = [c[0] for c in needs_llm]
            result_map = llm_classify_conditions(cond_texts, self.cls_cfg)

            for cond, core, components in needs_llm:
                bc = result_map.get(cond, "UNCLASSIFIABLE")
                self.cls_cfg["llm_conditions"][cond.lower().strip()] = bc
                self.cls_cfg["_llm_dirty"] = True

                if bc and bc != "UNCLASSIFIABLE" and bc in self._bc_names:
                    self._stats["llm"] += 1
                    broad_cancers.add(bc)
                    enriched_conditions.append({
                        "condition": cond, "core_condition": core,
                        "broad_cancers": [bc], "tier": "llm",
                        "extracted_components": components,
                    })
                else:
                    self._stats["unclassifiable"] += 1
                    enriched_conditions.append({
                        "condition": cond, "core_condition": core,
                        "broad_cancers": [], "tier": "unclassifiable",
                        "extracted_components": components,
                    })

        # Step 5a / 5b: Title fallback if still nothing
        title_class = None
        if not broad_cancers:
            title = trial.get("official_title") or trial.get("brief_title", "")
            result = classify_from_title(title, self.cls_cfg)
            if result:
                bcs, tier = result
                for bc in bcs: broad_cancers.add(bc)
                title_class = {"broad_cancer": list(bcs), "source": tier}
                self._stats["title_fallback"] += 1

            if not broad_cancers and title:
                llm_result = llm_classify_title(title, self.cls_cfg)
                if isinstance(llm_result, dict):
                    bc = llm_result.get("broad_cancer")
                    if bc and bc != "UNCLASSIFIABLE" and bc in self._bc_names:
                        broad_cancers.add(bc)
                        title_class = {
                            "broad_cancer": [bc],
                            "title_condition": llm_result.get("title_condition", ""),
                            "source": "title_llm",
                        }
                        self._stats["title_fallback"] += 1

        if not broad_cancers:
            self._stats["no_broad_cancer"] += 1

        return {
            "broad_cancers": sorted(broad_cancers),
            "conditions": enriched_conditions,
            "stages": sorted(all_stages),
            "genes": sorted(all_genes),
            "lines_of_therapy": sorted(all_lot),
            "title_classification": title_class,
        }

    def process_all(self, trials):
        """Process every trial sequentially and return {nct_id: conditions_result}."""
        results = {}
        total = len(trials)
        for i, (nct_id, trial) in enumerate(trials.items(), 1):
            if i == 1 or i % 50 == 0:
                print(f"[C] [{i}/{total}] {nct_id}...", flush=True)
            results[nct_id] = self.process_trial(trial)
        return results

    def save_cache(self):
        save_llm_conditions_cache(self.cls_cfg)

    @property
    def stats(self):
        return dict(self._stats)
