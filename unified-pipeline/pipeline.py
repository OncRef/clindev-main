"""
Unified Conquest Pipeline

Single fetch from ClinicalTrials.gov fans out to two independent processors
that run in parallel threads:

  [C] Conditions  — reference v1 steps 1→5b (dict tiers → Gemini → title fallback)
  [D] Drugs       — v2 modular flow (normalize → drug_master → Gemini enrich)

Results merge per-NCT into a single conquest_master.json. Genes is a planned
third branch that depends on conditions output and will be added later.

Usage:
  # test run (what this pipeline was built for)
  python pipeline.py --start-date 2026-04-01 --end-date 2026-04-15

  # rolling daily mode
  python pipeline.py --days 7

  # skip a side (useful for debugging one branch)
  python pipeline.py --start-date 2026-04-01 --end-date 2026-04-15 --skip-drugs
"""

import argparse
import json
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    CONQUEST_MASTER_FILE, DICT_DIR, OUTPUT_DIR, LOG_DIR,
    GEMINI_API_KEY, PARALLEL_WORKERS,
)
from fetcher import fetch_trials
from conditions.processor import ConditionProcessor
from drugs.drug_master import DrugMaster
from drugs.enricher import DrugEnricher
from drugs.processor import DrugProcessor


# --- Master I/O -----------------------------------------------------------

def load_master():
    if os.path.exists(CONQUEST_MASTER_FILE):
        with open(CONQUEST_MASTER_FILE) as f:
            return json.load(f)
    return {"metadata": {}, "trials": {}}


def save_master(master, window):
    master.setdefault("metadata", {})
    master["metadata"]["last_updated"] = datetime.now().isoformat()
    master["metadata"]["total_trials"] = len(master.get("trials", {}))
    master["metadata"]["last_window"] = window
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CONQUEST_MASTER_FILE, "w") as f:
        json.dump(master, f, indent=2, default=str)


# --- Trial merging ---------------------------------------------------------

def build_trial_entry(trial, cond_result, drug_result):
    """Shape the final per-NCT record written to conquest_master.json."""
    cond_result = cond_result or {}
    return {
        "nct_id": trial["nct_id"],
        "brief_title": trial.get("brief_title", ""),
        "official_title": trial.get("official_title", ""),
        "lead_sponsor": trial.get("lead_sponsor", ""),
        "sponsor_class": trial.get("sponsor_class", "UNKNOWN"),
        "overall_status": trial.get("overall_status", ""),
        "phase": trial.get("phase", []),
        "study_type": trial.get("study_type", ""),
        "enrollment": trial.get("enrollment"),
        "start_date": trial.get("start_date", ""),
        "completion_date": trial.get("completion_date", ""),
        # conditions side
        "broad_cancers": cond_result.get("broad_cancers", []),
        "conditions": cond_result.get("conditions", []),
        "stages": cond_result.get("stages", []),
        "genes": cond_result.get("genes", []),
        "lines_of_therapy": cond_result.get("lines_of_therapy", []),
        "title_classification": cond_result.get("title_classification"),
        # drugs side
        "drugs": drug_result or [],
        # raw signal kept for debugging
        "raw_conditions": trial.get("conditions", []),
        "raw_keywords": trial.get("keywords", []),
    }


# --- Parallel runners ------------------------------------------------------

def _run_conditions(trials):
    try:
        processor = ConditionProcessor()
        results = processor.process_all(trials)
        processor.save_cache()
        return {"results": results, "stats": processor.stats, "error": None}
    except Exception as e:
        return {"results": {}, "stats": {}, "error": f"{e}\n{traceback.format_exc()}"}


def _run_drugs(trials, resume=False):
    try:
        dm = DrugMaster().load()
        print(f"[D] Drug master loaded: {len(dm)} entries")
        enricher = DrugEnricher(checkpoint_dir=LOG_DIR)
        if resume:
            enricher.load_checkpoint()
        processor = DrugProcessor(dm, enricher)
        results = processor.process_all(trials)
        if dm.is_dirty:
            dm.save()
            print(f"[D] Drug master saved: {len(dm)} entries")
        enricher.save_results(os.path.join(LOG_DIR, f"enrichment_{date.today().isoformat()}.json"))
        enricher.cleanup_checkpoint()
        return {
            "results": results,
            "stats": processor.stats,
            "drug_master_size": len(dm),
            "drug_master_stats": dm.stats(),
            "enricher_stats": enricher.stats,
            "error": None,
        }
    except Exception as e:
        return {"results": {}, "stats": {}, "error": f"{e}\n{traceback.format_exc()}"}


# --- Main ------------------------------------------------------------------

def run_pipeline(days=7, start_date=None, end_date=None,
                 skip_conditions=False, skip_drugs=False, resume=False):
    print("=" * 60)
    print("Unified Conquest Pipeline")
    print("=" * 60)

    if not GEMINI_API_KEY:
        print("WARNING: GEMINI_API_KEY not set — LLM fallbacks will be skipped.")

    for d in (DICT_DIR, OUTPUT_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)

    window = {"days": days, "start_date": start_date, "end_date": end_date}
    print(f"Window: {window}")

    master = load_master()
    existing_ids = set(master.get("trials", {}).keys())
    print(f"Existing master: {len(existing_ids)} trials")

    # --- Fetch (shared) ---
    fetched = fetch_trials(days=days, start_date=start_date, end_date=end_date)
    new_trials = {nct: t for nct, t in fetched.items() if nct not in existing_ids}
    print(f"\nNew trials to process: {len(new_trials)} / fetched {len(fetched)}")

    if not new_trials:
        print("No new trials. Done.")
        save_master(master, window)
        return

    # --- Fan out conditions + drugs in parallel ---
    print(f"\nProcessing {len(new_trials)} trials in parallel "
          f"(conditions={'skip' if skip_conditions else 'on'}, "
          f"drugs={'skip' if skip_drugs else 'on'})...\n")

    cond_out = {"results": {}, "stats": {}, "error": None}
    drug_out = {"results": {}, "stats": {}, "error": None}

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
        futures = {}
        if not skip_conditions:
            futures[pool.submit(_run_conditions, new_trials)] = "conditions"
        if not skip_drugs:
            futures[pool.submit(_run_drugs, new_trials, resume)] = "drugs"

        for fut in as_completed(futures):
            side = futures[fut]
            out = fut.result()
            if side == "conditions":
                cond_out = out
            else:
                drug_out = out
            if out.get("error"):
                print(f"\n[{side}] FAILED: {out['error']}")
            else:
                print(f"\n[{side}] done.")

    # --- Merge + write master ---
    cond_results = cond_out["results"]
    drug_results = drug_out["results"]

    added = 0
    for nct_id, trial in new_trials.items():
        entry = build_trial_entry(
            trial,
            cond_results.get(nct_id),
            drug_results.get(nct_id),
        )
        master.setdefault("trials", {})[nct_id] = entry
        added += 1

    save_master(master, window)

    # --- Run log ---
    log = {
        "timestamp": datetime.now().isoformat(),
        "window": window,
        "trials_fetched": len(fetched),
        "new_trials": len(new_trials),
        "added_to_master": added,
        "total_master": len(master.get("trials", {})),
        "conditions_stats": cond_out.get("stats", {}),
        "drug_stats": drug_out.get("stats", {}),
        "drug_master_size": drug_out.get("drug_master_size"),
        "enricher_stats": drug_out.get("enricher_stats"),
        "errors": {k: v["error"] for k, v in (("conditions", cond_out), ("drugs", drug_out)) if v.get("error")},
    }
    stamp = date.today().isoformat()
    if start_date and end_date:
        stamp = f"{start_date}_to_{end_date}"
    log_file = os.path.join(LOG_DIR, f"run_{stamp}.json")
    with open(log_file, "w") as f:
        json.dump(log, f, indent=2, default=str)

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print("Pipeline Complete")
    print(f"  Window:        {window}")
    print(f"  Fetched:       {len(fetched)}")
    print(f"  New:           {len(new_trials)}")
    print(f"  Added:         {added}")
    print(f"  Master total:  {len(master.get('trials', {}))}")
    if cond_out.get("stats"):
        cs = cond_out["stats"]
        print(f"  Conditions:    {cs.get('conditions_total', 0)} parsed "
              f"| tier1+2+3={cs.get('tier_1',0)+cs.get('tier_2',0)+cs.get('tier_2_substr',0)+cs.get('tier_3',0)}"
              f" | llm={cs.get('llm',0)}"
              f" | title_fallback={cs.get('title_fallback',0)}"
              f" | no_bc={cs.get('no_broad_cancer',0)}")
    if drug_out.get("stats"):
        ds = drug_out["stats"]
        print(f"  Drugs:         total={ds.get('total_drugs',0)}"
              f" | found={ds.get('found_in_master',0)}"
              f" | added={ds.get('added_to_master',0)}"
              f" | updated={ds.get('updated_in_master',0)}"
              f" | errors={ds.get('errors',0)}")
        print(f"  Drug master:   {drug_out.get('drug_master_size', 0)} compounds")
    print(f"  Log:           {log_file}")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Unified Conquest Pipeline")
    parser.add_argument("--days", type=int, default=7, help="Rolling window size in days (default: 7)")
    parser.add_argument("--start-date", type=str, help="Absolute window start (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="Absolute window end (YYYY-MM-DD)")
    parser.add_argument("--skip-conditions", action="store_true", help="Skip the conditions branch")
    parser.add_argument("--skip-drugs", action="store_true", help="Skip the drugs branch")
    parser.add_argument("--resume", action="store_true", help="Resume drug enrichment from checkpoint")
    args = parser.parse_args()

    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be used together")

    run_pipeline(
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        skip_conditions=args.skip_conditions,
        skip_drugs=args.skip_drugs,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
