# clindev-main

> Unified data pipeline that fetches oncology clinical trials from ClinicalTrials.gov, classifies their cancer conditions, and enriches their drug interventions using dictionary lookups and Gemini LLM fallbacks, producing a structured master JSON consumed by the OncRef platform.

**Status:** Active
**Language:** Python 3.x
**Last meaningful update:** April 2026
**Part of:** OncRef Platform (https://app.oncref.com)

---

## What This Project Does

This repo contains the "Conquest Pipeline," a batch data-processing system that pulls interventional oncology clinical trials from the ClinicalTrials.gov v2 API. It splits the fetched trials into two parallel branches: one classifies each trial's cancer conditions into broad cancer categories, and the other normalizes drug intervention names and enriches unknown compounds with mechanism-of-action, drug class, and approval data. Both branches use a tiered approach that tries fast dictionary lookups first and falls back to Gemini 2.5 Flash (with Google Search grounding) only when local dictionaries fail. Results merge into a single `conquest_master.json` keyed by NCT ID, which feeds clinical trial data into the OncRef application.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.x (no pinned version; standard library + two dependencies) |
| Framework | Plain Python (no web framework; CLI via `argparse`) |
| Database | None (file-based JSON/CSV dictionaries and output) |
| AI/ML | Gemini 2.5 Flash via REST API with Google Search grounding |
| Auth | None (API key for Gemini only) |
| Deployment | None yet (designed for future GCP Cloud Run Jobs + Workflows containerization) |

**Key Dependencies:**
- `requests` - HTTP client for ClinicalTrials.gov API and Gemini REST calls
- `python-dotenv` - Loads `.env` file for `GEMINI_API_KEY` and model config

The project deliberately keeps dependencies minimal. All data processing (regex extraction, CSV/JSON dictionary lookups, string normalization) uses the Python standard library.

## Project Structure

```
clindev-main/
└── unified-pipeline/
    ├── pipeline.py              # Main orchestrator: fetch, fan-out, merge, write master
    ├── config.py                # Paths, API keys, CT.gov field list, drug class enums
    ├── fetcher.py               # CT.gov v2 API client with rolling/absolute date windows
    ├── conditions/
    │   ├── __init__.py
    │   ├── extractor.py         # Strips LOT, stage, gene mentions from condition strings
    │   ├── classifier.py        # Tier ladder (exact -> OncoTree -> keyword -> Gemini)
    │   └── processor.py         # Per-trial orchestration for the conditions branch
    ├── drugs/
    │   ├── __init__.py
    │   ├── normalizer.py        # Splits combo regimens, strips dosages, drops non-drugs
    │   ├── drug_master.py       # In-memory drug index with multi-name alias resolution
    │   ├── enricher.py          # Gemini enrichment with checkpoint/resume support
    │   └── processor.py         # Per-trial orchestration for the drugs branch
    ├── dictionaries/
    │   ├── extracted_cancer_to_broad.json   # Tier-1 exact condition-to-broad-cancer map
    │   ├── llm_classified_conditions.json   # Gemini classification cache (grows over runs)
    │   ├── oncotree_to_broad_cancer_mapping.csv  # OncoTree codes to broad cancer
    │   ├── broad_cancer_mapping.csv         # Keyword/subtype scoring rules per broad cancer
    │   ├── drug_master.json                 # Canonical drug reference (grows over runs)
    │   ├── line_of_therapy.json             # Regex patterns for 1L/2L/3L extraction
    │   ├── stage.json                       # Regex patterns for stage I-IV extraction
    │   └── genes.csv                        # Gene names and aliases for extraction
    ├── Search-Terms-for-Cancer.csv          # ~30 cancer search terms for CT.gov queries
    ├── .env.example
    ├── .gitignore
    ├── README.md
    ├── requirements.txt
    ├── output/                  # conquest_master.json lands here (gitignored)
    └── logs/                    # Run logs + enrichment checkpoints (gitignored)
```

## Key Files Guide

**Start here when you need to:**

| Task | Start at | Then look at |
|------|----------|-------------|
| Understand the full pipeline flow | `unified-pipeline/pipeline.py` | `unified-pipeline/fetcher.py` |
| See how conditions are classified | `unified-pipeline/conditions/processor.py` | `unified-pipeline/conditions/classifier.py` |
| See how condition strings are parsed | `unified-pipeline/conditions/extractor.py` | `unified-pipeline/dictionaries/line_of_therapy.json` |
| See how drug names are cleaned | `unified-pipeline/drugs/normalizer.py` | `unified-pipeline/drugs/processor.py` |
| Understand the drug master index | `unified-pipeline/drugs/drug_master.py` | `unified-pipeline/dictionaries/drug_master.json` |
| See how Gemini enriches drugs | `unified-pipeline/drugs/enricher.py` | `unified-pipeline/config.py` |
| Change API settings or file paths | `unified-pipeline/config.py` | `unified-pipeline/.env.example` |
| Add or modify cancer search terms | `unified-pipeline/Search-Terms-for-Cancer.csv` | `unified-pipeline/fetcher.py` |

## Architecture

The pipeline follows a fetch-once, fan-out-in-parallel, merge-back pattern. A single `fetcher.py` call pulls all interventional oncology trials updated within a date window from the ClinicalTrials.gov v2 API, iterating through ~30 cancer-related search terms and three status buckets (recruiting, completed within 2 years, other within 1 year).

The fetched trials are then handed to two independent branches running on separate threads. The conditions branch extracts structured components (line of therapy, cancer stage, gene mutations) from each raw condition string, then attempts to classify the remaining "core" phrase into a broad cancer category using a four-tier ladder: exact dictionary match, OncoTree lookup, keyword/subtype scoring, and finally a Gemini LLM call. If no condition yields a broad cancer, the trial title itself is classified as a last resort.

The drugs branch splits combination intervention strings into individual drug names, strips dosages and formulations, drops non-drug entries (placebo, standard of care), and looks each name up in `drug_master.json` via a multi-alias index. Unknown drugs go to Gemini 2.5 Flash with Google Search grounding for enrichment (mechanism, class, target, company, approval status), and newly discovered compounds are appended to the drug master for future runs.

Both branches write their results back to `pipeline.py`, which merges them per NCT ID into `conquest_master.json`.

```
                    ┌─────────────────────────────┐
                    │   ClinicalTrials.gov v2 API  │
                    └─────────────┬───────────────┘
                                  │
                          fetch_trials()
                                  │
              ┌───────────────────┴───────────────────┐
              │                                       │
     ConditionProcessor                        DrugProcessor
     (extractor + classifier)              (normalizer + drug_master
              │                              + enricher)
     tier 1-3 dict lookups                 drug_master.json lookup
              │                                       │
     Gemini fallback (step 4)              Gemini enrichment (new drugs)
              │                                       │
     title fallback (step 5a/5b)                      │
              │                                       │
              └───────────────────┬───────────────────┘
                                  │
                       conquest_master.json
```

## Pipeline Steps

| Step | Branch | What Happens |
|------|--------|-------------|
| Fetch | Shared | Query CT.gov v2 API with ~30 cancer terms across 3 status buckets; deduplicate by NCT ID |
| Extract components | Conditions | Strip line-of-therapy, stage, and gene mentions from raw condition strings via regex dictionaries |
| Tier 1 classify | Conditions | Exact match against `extracted_cancer_to_broad.json` |
| Tier 1.5 recycle | Conditions | Check `llm_classified_conditions.json` cache from previous Gemini calls |
| Tier 2 classify | Conditions | Exact and substring match against OncoTree mapping |
| Tier 3 classify | Conditions | Keyword/subtype scoring against `broad_cancer_mapping.csv` |
| Step 4 LLM | Conditions | Batch-send remaining unclassified conditions to Gemini 2.5 Flash |
| Step 5a/5b title | Conditions | If still no broad cancer, classify from trial title (regex then Gemini) |
| Normalize | Drugs | Split combos, strip dosages/formulations, drop non-drugs (placebo, SOC, etc.) |
| Master lookup | Drugs | Resolve drug name through multi-alias index in `drug_master.json` |
| Gemini enrich | Drugs | For unknown drugs: call Gemini with Google Search grounding for MOA, class, target, company, approval |
| Master update | Drugs | Append newly discovered compounds to `drug_master.json` |
| Merge | Shared | Combine conditions + drugs per NCT ID into `conquest_master.json` |

## Environment Variables

| Variable | Purpose | Required | Example |
|----------|---------|----------|---------|
| `GEMINI_API_KEY` | Google Gemini API key for LLM classification and drug enrichment | Yes (pipeline runs without it but skips all LLM fallbacks) | `AIza...` |
| `GEMINI_MODEL` | Override the default Gemini model | No (defaults to `gemini-2.5-flash`) | `gemini-2.5-flash` |

## How to Run

```bash
# Navigate to the pipeline directory
cd unified-pipeline

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy env template and set your Gemini API key
cp .env.example .env
# Edit .env and add GEMINI_API_KEY

# Run with an absolute date window
python pipeline.py --start-date 2026-04-01 --end-date 2026-04-15

# Run with a rolling window (last 7 days)
python pipeline.py --days 7

# Skip one branch for debugging
python pipeline.py --days 7 --skip-drugs
python pipeline.py --days 7 --skip-conditions

# Resume drug enrichment from a checkpoint (after a crash or timeout)
python pipeline.py --days 7 --resume
```

## Connects To

- **ClinicalTrials.gov v2 API** (`https://clinicaltrials.gov/api/v2/studies`) - source of all trial data
- **Google Gemini API** (`generativelanguage.googleapis.com`) - LLM fallback for condition classification and drug enrichment
- **OncRef app** (`app.oncref.com`) - downstream consumer of `conquest_master.json` (the app repo ingests this file into its database)

## Data Flow

```
ClinicalTrials.gov v2 API --> [clindev-main / unified-pipeline] --> conquest_master.json --> OncRef App DB
                                       |
                                       +--> drug_master.json (self-growing drug reference)
                                       +--> llm_classified_conditions.json (Gemini cache)
                                       +--> logs/run_*.json (run statistics)
                                       +--> logs/enrichment_*.json (Gemini call traces)
```

## Known Patterns and Conventions

- **Self-growing dictionaries:** Both `drug_master.json` and `llm_classified_conditions.json` accumulate entries across runs. Dictionary lookups are always tried before Gemini calls, so repeated runs become faster and cheaper over time.
- **Checkpoint/resume for enrichment:** The drug enricher writes a checkpoint every 100 Gemini calls. If the pipeline crashes mid-run, `--resume` picks up where it left off without re-calling Gemini for already-enriched drugs.
- **Parallel-safe by design:** Conditions and drugs operate on separate dictionary files and separate output structures, so the two threads never need locks or coordination.
- **Flat trial dict:** The fetcher flattens the nested CT.gov API response into a simple dict per trial. All downstream processors receive this flat structure.
- **Non-drug filter list:** The normalizer maintains a curated set of ~50 non-drug terms (placebo, saline, standard of care, etc.) that are dropped before enrichment. This list grows as edge cases are discovered in production runs.

---

*Generated on 2026-04-16 by README Generator for OncRef organization.*
*This README is designed for LLM agent consumption. For user-facing documentation, see the OncRef landing page.*
