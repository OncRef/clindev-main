# Unified Conquest Pipeline

One pipeline. Single fetch from ClinicalTrials.gov, two independent branches
that run in parallel:

- **Conditions** — reference v1 steps 1→5b (dict tiers → Gemini → title fallback)
- **Drugs** — v2 modular flow (normalize → drug_master lookup → Gemini enrich)

Results merge into a single `output/conquest_master.json`. A genes branch is
planned and will depend on conditions output.

## Layout

```
unified-pipeline/
├── pipeline.py          # main orchestrator (shared fetch + parallel fan-out)
├── config.py            # paths, API keys, constants
├── fetcher.py           # CT.gov API client, supports --days or date range
├── conditions/
│   ├── extractor.py     # LOT / stage / gene extraction
│   ├── classifier.py    # tier ladder + Gemini fallbacks
│   └── processor.py     # per-trial orchestration
├── drugs/
│   ├── normalizer.py    # clean/split drug names
│   ├── drug_master.py   # drug_master.json index + mutations
│   ├── enricher.py      # Gemini + Google Search grounding
│   └── processor.py     # per-trial orchestration
├── dictionaries/        # condition dicts + drug_master.json
├── output/              # conquest_master.json lands here
├── logs/                # run logs + enrichment checkpoints
└── Search-Terms-for-Cancer.csv
```

## Setup

```bash
cd unified-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and set GEMINI_API_KEY
```

## Running

**Test run (what this was built for — April 1-15, 2026):**

```bash
python pipeline.py --start-date 2026-04-01 --end-date 2026-04-15
```

**Rolling daily mode:**

```bash
python pipeline.py --days 7
```

**Debug one branch at a time:**

```bash
python pipeline.py --start-date 2026-04-01 --end-date 2026-04-15 --skip-drugs
python pipeline.py --start-date 2026-04-01 --end-date 2026-04-15 --skip-conditions
```

**Resume drug enrichment from checkpoint:**

```bash
python pipeline.py --days 7 --resume
```

## Output

- `output/conquest_master.json` — merged conditions + drugs, keyed by NCT ID.
- `dictionaries/drug_master.json` — appended in-place as Gemini discovers new drugs.
- `dictionaries/llm_classified_conditions.json` — cache of Gemini condition classifications.
- `logs/run_<window>.json` — per-run stats (tiers, drug stats, errors).
- `logs/enrichment_<date>.json` — full Gemini enrichment trace for the run.

## Notes

- Conditions and drugs don't touch the same dictionary files, so the two
  threads can run in parallel without locking.
- GCP deployment (Cloud Run Jobs + Workflows) is a later step — the current
  layout is deliberately a plain Python package so it'll wrap cleanly into
  a container later.
