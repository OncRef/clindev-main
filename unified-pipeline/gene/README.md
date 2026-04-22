# gene/ — MOA Master & Gene Master

> **📘 Picking this up?** Start with [`PROCESS.md`](./PROCESS.md) — that
> is the living handoff document: current state, architecture, per-script
> reference, problems we hit and how we solved them, Tom's review queue,
> and what's left to ship. This README is the original design spec from
> before implementation; parts may be aspirational or outdated.

Two normalized dictionaries built side-by-side:

1. **`moa_master.json`** — collapses the ~7,500 free-text `short_moa`
   strings in `dictionaries/drug_master.json` into canonical entries
   keyed by `{gene, action, mutation_context}`, each linked to HGNC
   gene symbols.
2. **`gene_master.json`** — starts from the curated gene list in
   `New_Gene_updated.xlsx` (461 genes + aliases + broad cancers) and
   enriches every gene with **detailed cancer conditions** (beyond
   broad category) and a reverse lookup of **MOAs that target it**.

Together these drive gene classification for every trial in
`output/conquest_master.json`.

## Input files (already in this folder)

- `New_Gene_updated.xlsx` — **source of truth for gene aliases and the
  seed for the target → HGNC alias map**. Three columns: `Gene`,
  `Gene Aliases` (comma-separated), `Broad Cancers` (comma-separated).
  461 genes. Use this instead of downloading HGNC.
- `../dictionaries/drug_master.json` — drug master (9,739 drugs with
  free-text `short_moa` / `drug_class` / `long_moa`).
- `../output/conquest_master.json` — 50,777 trials, each carrying a
  `drugs[]` array copied from drug master.

---

## 1. Problem

Drug MOA data in `drug_master.json` is free-text and not normalized:

| Field        | Drugs | Unique values | Notes                                           |
|--------------|------:|--------------:|-------------------------------------------------|
| `short_moa`  | 9,739 |     **7,467** | ~76% unique strings — nearly free text          |
| `drug_class` |  ~9.7k|     **2,037** | Case/separator variants (`kinase_inhibitor` vs `Kinase inhibitor` vs `kinase inhibitor`) |
| `long_moa`   | 8,642 |             — | Prose descriptions                              |

Because every drug carries its own MOA string, there is no way to ask:

- *"Which trials use **any** KRAS inhibitor?"*
- *"Which genes are targeted in this trial's regimen?"*
- *"How many trials use a whole-class **PD-1 inhibitor**?"*

The goal is to collapse the 7,467 `short_moa` strings into a small set of
canonical MOA entries, link each to **HGNC gene symbols**, and use that
mapping to enrich `trials[*].genes[]` in `conquest_master.json`.

---

## 2. Pattern analysis (what the data actually looks like)

Empirical scan of all 9,739 non-null `short_moa` values:

### 2a. Dominant shape: `{TARGET} {ACTION}`

About **4,190 of 7,467** unique strings (~56%) end in one of these action
suffixes:

```
inhibitor    2,689
antagonist     404
agonist        399
targeting      323
modulator      142
degrader        85
blocker         80
activator       44
binder          18
disruptor        6
```

Examples:

- `EGFR inhibitor` — bare target + action
- `PD-1 inhibitor`, `PD-L1 inhibitor`
- `KRAS G12C inhibitor` — target + **mutation context** + action
- `EGFR tyrosine kinase inhibitor` — target + subtype + action
- `VEGF receptor 2 antagonist`
- `Aromatase and CDK4/6 inhibitor` — **multi-target** with connector
- `PD-1 and VEGF bispecific antibody`
- `Selective estrogen receptor modulator (SERM)` — with acronym gloss
- `PROTAC estrogen receptor (ER) degrader`

### 2b. Gene-like tokens are abundant and parseable

Top gene tokens extracted via `\b[A-Z][A-Z0-9]{1,7}\b` (after noise filtering):

```
EGFR 192   HER2 183   CD19 158   PSMA 114   CD3 99
KRAS  88   VEGF  83   CD20  75   CTLA 71    PI3K 66
CD47  46   VEGFR 44   FLT3 41    BTK  39    CDK4 38
MET   38   HER3  35   CLDN18 34  CD38 31    ALK  30
DLL3  30   PARP1 26   GPRC5D 25  TIGIT 24   FGFR 24
```

Many of these (`EGFR`, `HER2`, `KRAS`, `ALK`, `MET`, `FLT3`, `BTK`, `FGFR`…)
are already valid HGNC symbols. Others (`PD-1`, `PD-L1`, `CTLA-4`, `HER2`)
need an **alias → HGNC** map (`PD-1 → PDCD1`, `PD-L1 → CD274`,
`CTLA-4 → CTLA4`, `HER2 → ERBB2`, `HER3 → ERBB3`, `VEGF → VEGFA`, etc.).

### 2c. Class-only entries

Broad-class strings with no specific target:

- `monoclonal antibody`, `bispecific antibody`, `CAR-T cell therapy`
- `cancer vaccine`, `Antibody-drug conjugate`
- `Alkylating agent`, `Topoisomerase I inhibitor`
- `radiopharmaceutical`

### 2d. Prose / junk

Descriptive sentences and sentinel values the parser must handle separately:

- `"Inhibits bacterial cell wall synthesis"`
- `"Reduces inflammation and suppresses immune system"`
- `"Undefined mechanism"` / `"Unknown"` / `"Mechanism of action not found"`

### Takeaway

About **~60%** of unique strings are cleanly parseable with regex.
Another **~15–20%** can be parsed with light rewriting (acronym gloss,
multi-target splitting). The remaining **~20–25%** (prose, rare phrasing,
novel targets) needs LLM help — **perfect fit for Gemini 2.5 Flash**.

---

## 3. Recommended approach: **3-stage hybrid pipeline**

Don't pick regex *or* Gemini — use both, in the right order. Regex is free,
fast, and deterministic; Gemini is expensive but handles the long tail.

```
           drug_master.json
                 │
                 ▼
  ┌──────────────────────────────┐
  │ Stage 1: Deterministic parser│  regex + rules + alias map
  │   — parses ~60% cleanly      │  free, deterministic, cacheable
  └──────────────┬───────────────┘
                 │  unresolved short_moa strings
                 ▼
  ┌──────────────────────────────┐
  │ Stage 2: Gemini 2.5 Flash    │  structured JSON output
  │   batch extraction           │  one call per ~20 unresolved strings
  └──────────────┬───────────────┘
                 │
                 ▼
  ┌──────────────────────────────┐
  │ Stage 3: HGNC gene linker    │  symbol + alias lookup
  │   attach gene_targets[]      │  flag "class-only" entries
  └──────────────┬───────────────┘
                 │
                 ▼
        moa_master.json
                 │
                 ▼
  enrich conquest_master.json
   trials[*].genes[]  ←  union of gene_targets from all trial drugs
```

### Stage 1 — Deterministic parser (fast path)

**Inputs:** every unique `short_moa` and `drug_class` string across
`drug_master.json`.

**Steps:**

1. **Normalize whitespace and casing** for `drug_class`:
   - Replace `_` with space, lowercase, strip → collapses the 2,037
     class variants to a much smaller set (target: <100 canonical classes).
2. **Regex-parse `short_moa`** with a layered pattern:
   ```python
   ACTION = r'(inhibitor|antagonist|agonist|modulator|degrader|blocker|activator|binder|disruptor|targeting|targeted therapy)'
   MOA_RE = re.compile(
       rf'^(?P<target>.+?)\s+(?P<action>{ACTION})s?$', re.I)
   ```
3. **Split multi-target** strings on `/`, `and`, `&`, `+`, `,`
   (e.g. `"CDK4/6 inhibitor"` → `["CDK4", "CDK6"]`;
   `"Aromatase and CDK4/6 inhibitor"` → `["Aromatase", "CDK4", "CDK6"]`).
4. **Extract mutation context** (`G12C`, `G12D`, `V600E`, `L858R`, `T790M`,
   `exon 19 del`, `exon 20 ins`) into a separate `mutation_context` field
   so `KRAS G12C inhibitor` and `KRAS G12D inhibitor` collapse to the same
   canonical target (KRAS) but keep the mutation info.
5. **Strip noise tokens** from targets: "receptor", "tyrosine kinase",
   "pathway", "(xxx)", trailing version numbers. Keep a raw copy for audit.
6. **Build canonical key** from `{target_set}_{action}`:
   e.g. `kras_inhibitor`, `egfr_inhibitor`, `pd1_inhibitor`.
7. **Mark unresolved** anything that doesn't match — prose, unknown,
   empty, or matches but target is not in HGNC/alias set.

### Stage 2 — Gemini 2.5 Flash (long tail)

**When:** Stage 1 could not confidently parse the string, or the parsed
target isn't in HGNC or the curated alias list.

**Why Flash and not Pro:** MOA normalization is pattern-matching, not
deep reasoning. Flash is ~10× cheaper and fast enough to run the whole
long tail in one pass.

**Prompt strategy:**

- **Batch 20 strings per call** to amortize overhead.
- Request structured JSON (not free text) via `responseMimeType:
  application/json` and an explicit JSON schema.
- Ask for: `targets` (list of specific molecular targets),
  `hgnc_candidates` (the model's guess at HGNC symbols),
  `action` (enum), `drug_class` (enum), `pathway` (e.g. MAPK, PI3K/AKT),
  `is_specific` (false for class-only).
- **Temperature 0**, strict schema, retry on parse failure.
- **Cache by `sha256(short_moa)`** in `gene/cache/moa_gemini_cache.json` —
  identical strings never get re-queried.

**Volume estimate:**

- Unresolved strings after Stage 1: ~1,500–1,800 (long tail).
- At 20 per batch: ~75–90 Gemini calls.
- At Flash pricing (~\$0.075 in / \$0.30 out per 1M tokens) with ~2K
  tokens per request: **< \$0.50 total**. Cost is not a concern.

See `Conquest/step6f_gemini_moa.py` for the existing Gemini integration
pattern in this repo — same endpoint, same JSON-response handling.

### Stage 3 — Gene linker (driven by `New_Gene_updated.xlsx`)

**Inputs:** every parsed target token from Stages 1 & 2.

**No HGNC download.** The Excel sheet is the source of truth: 461
curated genes with aliases. We build the alias map directly from it.

**Steps:**

1. **Load `New_Gene_updated.xlsx`** into a lookup table:
   ```python
   alias_to_gene = {}
   for row in sheet:
       gene = row["Gene"]
       for a in row["Gene Aliases"].split(","):
           alias_to_gene[a.strip().lower()] = gene
       alias_to_gene[gene.lower()] = gene
   ```
   This gives ~2,500 alias → canonical gene mappings (≈5 aliases per
   row average).
2. **Hand-add a small supplemental map** for receptor/pathway names
   the Excel sheet doesn't cover but that show up in MOA strings —
   these go in `dictionaries/moa_target_supplement.json`:
   ```
   PD-1              → PDCD1       (only if PDCD1 is in the excel)
   PD-L1             → CD274
   CTLA-4            → CTLA4
   HER2              → ERBB2
   Aromatase         → CYP19A1
   Androgen receptor → AR
   Estrogen receptor → ESR1
   ```
   **Important:** any gene that isn't already a row in
   `New_Gene_updated.xlsx` gets dropped from `gene_targets` and flagged
   as `non_gene_target`, because the Excel sheet defines the universe
   of genes we care about.
3. For each parsed target: try excel alias → excel gene → supplement
   → drop (flag as `non_gene_target`).
4. Each MOA entry ends up with `gene_targets: [GENE, ...]` or
   `gene_targets: []` + `is_class_only: true`.

---

## 4. Proposed schema: `moa_master.json`

Mirrors `drug_master.json` layout so the existing `DrugMaster` helper
patterns translate directly.

```json
{
  "metadata": {
    "version": "1.0",
    "timestamp": "2026-04-15T12:00:00",
    "total_moas": 850,
    "total_drugs_mapped": 9739,
    "source": "drug_master_2026-04-12.json",
    "resolution": {
      "deterministic": 6100,
      "gemini": 1500,
      "unresolved": 100
    }
  },

  "moas": {
    "kras_g12c_inhibitor": {
      "canonical_name": "KRAS G12C inhibitor",
      "aliases": ["KRAS G12C inhibitor", "KRASG12C inhibitor", "KRAS(G12C) inhibitor"],
      "targets": [{"raw": "KRAS", "hgnc_symbol": "KRAS", "mutation": "G12C"}],
      "gene_targets": ["KRAS"],
      "action": "inhibitor",
      "drug_class": "small_molecule_inhibitor",
      "pathway": ["MAPK", "RAS/RAF/MEK/ERK"],
      "is_class_only": false,
      "drug_count": 19,
      "example_drugs": ["sotorasib", "adagrasib", "divarasib"],
      "source": "deterministic"
    },

    "kras_g12d_inhibitor": {
      "canonical_name": "KRAS G12D inhibitor",
      "aliases": ["KRAS G12D inhibitor", "KRASG12D inhibitor"],
      "targets": [{"raw": "KRAS", "hgnc_symbol": "KRAS", "mutation": "G12D"}],
      "gene_targets": ["KRAS"],
      "action": "inhibitor",
      "drug_class": "small_molecule_inhibitor",
      "pathway": ["MAPK"],
      "is_class_only": false,
      "drug_count": 4,
      "example_drugs": ["MRTX1133"],
      "source": "deterministic"
    },

    "kras_inhibitor": {
      "canonical_name": "KRAS inhibitor",
      "aliases": ["KRAS inhibitor", "pan-KRAS inhibitor"],
      "targets": [{"raw": "KRAS", "hgnc_symbol": "KRAS", "mutation": null}],
      "gene_targets": ["KRAS"],
      "action": "inhibitor",
      "drug_class": "small_molecule_inhibitor",
      "is_class_only": false,
      "drug_count": 6,
      "example_drugs": ["BI-2493"],
      "source": "deterministic"
    },

    "egfr_tki": {
      "canonical_name": "EGFR tyrosine kinase inhibitor",
      "aliases": ["EGFR inhibitor", "EGFR tyrosine kinase inhibitor", "EGFR TKI"],
      "targets": [{"raw": "EGFR", "hgnc_symbol": "EGFR", "mutation_context": ["L858R", "T790M", "exon 19 del"]}],
      "gene_targets": ["EGFR"],
      "action": "inhibitor",
      "drug_class": "kinase_inhibitor",
      "pathway": ["EGFR", "PI3K/AKT", "MAPK"],
      "is_class_only": false,
      "drug_count": 48,
      "example_drugs": ["erlotinib", "gefitinib", "osimertinib", "afatinib"],
      "source": "deterministic"
    },

    "monoclonal_antibody": {
      "canonical_name": "monoclonal antibody",
      "aliases": ["monoclonal antibody", "mAb", "humanized monoclonal antibody"],
      "targets": [],
      "gene_targets": [],
      "action": null,
      "drug_class": "monoclonal_antibody",
      "is_class_only": true,
      "drug_count": 822
    }
  },

  "classes": {
    "kinase_inhibitor": {
      "canonical_name": "kinase_inhibitor",
      "aliases": ["kinase_inhibitor", "kinase inhibitor", "Kinase inhibitor",
                  "small_molecule_inhibitor", "Small molecule inhibitor"],
      "drug_count": 946
    }
  },

  "drug_to_moa": {
    "sotorasib": ["kras_g12c_inhibitor"],
    "adagrasib": ["kras_g12c_inhibitor"],
    "MRTX1133": ["kras_g12d_inhibitor"],
    "osimertinib": ["egfr_tki"],
    "pembrolizumab": ["pd1_inhibitor", "monoclonal_antibody"]
  }
}
```

### What `drug_to_moa` is for

`drug_to_moa` is the **reverse index** — a flat dict `{drug_name →
[moa_keys]}` so that downstream code can go from a drug name in a trial
straight to its canonical MOA(s) in **O(1)**:

```python
# trial enrichment — without drug_to_moa you'd scan every MOA entry:
for drug in trial["drugs"]:
    moa_keys = moa_master["drug_to_moa"].get(drug["normalized_name"], [])
    for mk in moa_keys:
        trial_genes.update(moa_master["moas"][mk]["gene_targets"])
```

It's a pure derived field — built last, after all `moas` entries are
final, by inverting each entry's `example_drugs` list. Cheap to
regenerate from scratch.

### Why this shape

- **`moas`** is the primary dict, keyed by canonical slug. Mirrors
  `drug_master.json → drugs`.
- **`classes`** holds the broad-class lookup (`monoclonal_antibody`,
  `ADC`, `chemotherapy`) separately. A drug can belong to both a
  specific MOA (`pd1_inhibitor`) and a class (`monoclonal_antibody`).
- **`drug_to_moa`** is the reverse index — used when enriching trial drugs.
  Built last, cheap to regenerate.
- **`gene_targets`** is the field that drives gene classification for
  trials. Empty for class-only entries.

---

## 4b. `gene_master.json` — the parallel gene dictionary

Built from `New_Gene_updated.xlsx` + Gemini enrichment. One entry per
gene row in the spreadsheet.

```json
{
  "metadata": {
    "version": "1.0",
    "timestamp": "2026-04-15T12:00:00",
    "total_genes": 461,
    "source_excel": "New_Gene_updated.xlsx",
    "source_moa_master": "moa_master.json"
  },
  "genes": {
    "KRAS": {
      "gene": "KRAS",
      "aliases": ["KRAS", "K-RAS", "KI-RAS2", "KRAS2", "NS3", "RASK2"],
      "broad_cancers": [
        "Colorectal Cancer", "Lung Cancers", "Pancreatic Cancer"
      ],
      "detailed_conditions": [
        "Non-Small Cell Lung Cancer (NSCLC)",
        "KRAS G12C-mutant NSCLC",
        "Pancreatic Ductal Adenocarcinoma (PDAC)",
        "Colorectal Adenocarcinoma",
        "MSI-stable Colorectal Cancer"
      ],
      "mutation_hotspots": ["G12C", "G12D", "G12V", "G13D", "Q61H"],
      "pathway": ["MAPK", "RAS/RAF/MEK/ERK", "PI3K/AKT"],
      "moas_targeting": [
        "kras_g12c_inhibitor",
        "kras_g12d_inhibitor",
        "kras_inhibitor",
        "pan_ras_inhibitor"
      ],
      "example_drugs": ["sotorasib", "adagrasib", "divarasib", "MRTX1133"],
      "source": {
        "aliases": "excel",
        "broad_cancers": "excel",
        "detailed_conditions": "gemini",
        "mutation_hotspots": "gemini",
        "pathway": "gemini",
        "moas_targeting": "derived_from_moa_master"
      }
    }
  }
}
```

### How it's built

1. **Seed** from `New_Gene_updated.xlsx` → fill `gene`, `aliases`,
   `broad_cancers`.
2. **Enrich with Gemini 2.5 Flash** (one call per gene, cached by gene
   symbol in `cache/gene_gemini_cache.json`). Prompt asks for:
   - `detailed_conditions`: specific cancer subtypes / oncotree-style
     names where this gene is clinically actionable, **not the broad
     bucket the Excel already has**.
   - `mutation_hotspots`: commonly-targeted mutations (e.g. BRAF V600E,
     EGFR L858R, KRAS G12C).
   - `pathway`: major pathways the gene is part of.
   - Strict JSON schema, temperature 0.
3. **Cross-reference with `moa_master.json`** — for each gene, collect
   all MOAs whose `gene_targets` contain it, populate `moas_targeting`
   and `example_drugs`. This is the link between the two masters.

### Volume / cost

461 genes × 1 Gemini call each = 461 calls. At Flash pricing with
~1.5K tokens per request: **~\$0.20 total**, one-time. Re-runs hit the
cache and cost nothing.

### Why this ordering matters

`gene_master.json` depends on `moa_master.json` (for `moas_targeting`),
so the orchestrator must run `build_moa_master.py` first, then
`build_gene_master.py`.

---

## 5. Downstream use: classifying genes in `conquest_master.json`

Once `moa_master.json` exists, gene classification is a straightforward
join. For every trial:

```python
trial_genes = set(trial.get("genes", []))  # explicit genes from trial text
for drug in trial.get("drugs", []):
    moa_keys = moa_master["drug_to_moa"].get(drug["normalized_name"], [])
    for mk in moa_keys:
        trial_genes.update(moa_master["moas"][mk]["gene_targets"])
trial["genes"] = sorted(trial_genes)
trial["genes_from_drugs"] = [...]  # keep audit trail of which genes
                                    # came from drug MOAs vs the trial text
```

This takes `trials_with_genes: 1,755` (current) and pushes it much
higher — any trial using `osimertinib` now counts as an EGFR trial,
any trial using `sotorasib` counts as KRAS, etc. Exact uplift depends
on how many trial drugs resolve to gene-targeted MOAs vs class-only
(chemotherapy, monoclonal antibody). Rough estimate: **40–60% of trials
will gain at least one gene** from this join.

---

## 6. File layout

```
conquest-pipeline-v2/gene/
├── README.md                          # this file
├── New_Gene_updated.xlsx              # INPUT: 461 curated genes + aliases + broad cancers
├── dictionaries/
│   ├── moa_master.json                # OUTPUT: normalized MOAs
│   ├── gene_master.json               # OUTPUT: enriched genes
│   └── moa_target_supplement.json     # hand-curated extras (PD-1→PDCD1, etc.)
├── cache/
│   ├── moa_gemini_cache.json          # sha256(moa_string) → gemini response
│   └── gene_gemini_cache.json         # gene symbol → gemini response
├── parser.py                          # Stage 1 — deterministic regex parser
├── gemini_client.py                   # shared Gemini 2.5 Flash wrapper + cache
├── moa_master.py                      # MoaMaster class (mirrors DrugMaster)
├── gene_master.py                     # GeneMaster class (loads Excel)
├── gene_linker.py                     # Stage 3 — target → gene resolution
├── build_moa_master.py                # orchestrator: stages 1→2→3, writes moa_master.json
├── build_gene_master.py               # orchestrator: excel + gemini, writes gene_master.json
├── classify_trial_genes.py            # applies moa_master to conquest_master
└── logs/
```

---

## 7. Implementation steps (in order)

1. **Load `New_Gene_updated.xlsx`** and build the alias → gene map
   in-memory (used by Stage 3). Add `moa_target_supplement.json` by
   hand for the ~15 receptor/pathway tokens that show up in MOA
   strings but aren't in the Excel (PD-1, PD-L1, CTLA-4, Aromatase,
   Androgen/Estrogen receptor, VEGF, …).
2. **Write `parser.py`** (Stage 1). Run it against all unique
   `short_moa` + `drug_class` strings. Report coverage — target ≥60%.
3. **Write `gemini_client.py`** — shared Gemini 2.5 Flash wrapper
   with sha256 cache, batched calls, structured JSON output, retries.
4. **Write `moa_master.py`** and **`gene_master.py`** classes.
5. **Write `build_moa_master.py`** — orchestrator that runs 1→2→3 and
   writes `moa_master.json`. Idempotent: re-running hits cache.
6. **Write `build_gene_master.py`** — loads the Excel, fills aliases
   + broad_cancers, Gemini-enriches each of 461 rows with
   `detailed_conditions` + `mutation_hotspots` + `pathway`, then
   cross-references `moa_master.json` to fill `moas_targeting` +
   `example_drugs`. Writes `gene_master.json`.
7. **Write `classify_trial_genes.py`** — loads both masters +
   `conquest_master.json`, builds the drug→MOA→gene join, writes
   `conquest_master_with_gene_targets.json` adding
   `genes_from_drugs[]` per trial.
8. **Manual audit** — spot-check 50 random trials before trusting
   the enrichment. Compare against trial text for false positives
   (e.g., a drug with a class-only MOA should not add a gene).

---

## 8. Decisions (resolved with Tom 2026-04-15)

1. **Mutation-specific MOAs are separate entries.** `kras_g12c_inhibitor`
   and `kras_g12d_inhibitor` are distinct canonical MOAs. A pan-inhibitor
   with no mutation specified gets its own `kras_inhibitor` key. The
   canonical key for a parsed MOA is
   `{target}_{mutation|∅}_{action}`. All three roll up to the same
   `gene_targets: ["KRAS"]` so gene classification still works at the
   gene level.
2. **`drug_to_moa` is the reverse index** (drug → MOA keys). See §4.
3. **Chemotherapy backbones get no genes.** `cisplatin`, `carboplatin`,
   `cyclophosphamide`, etc. are marked `is_class_only: true` with empty
   `gene_targets`. Trials using only chemo backbones contribute zero
   genes from drugs — which is correct.
4. **Write enrichment to a new file**
   (`conquest_master_with_gene_targets.json`), not in place.
5. **Gene master is seeded from `New_Gene_updated.xlsx`** (461 rows),
   not HGNC. Gemini adds detailed conditions + mutation hotspots +
   pathway per gene — that's the enrichment beyond the Excel's broad
   cancer buckets.

---

## 9. What is *not* in scope for v1

- Pathway-level classification beyond simple tags. No KEGG / Reactome
  integration in v1 — `pathway` is a free-text list.
- Mutation-specific trial matching. We capture the mutation context
  but don't yet drive queries like "trials for KRAS G12C but not G12D".
- Live refresh from HGNC. One-time dump is fine.
- Any change to `drug_master.json` itself. This pipeline is strictly
  additive — it reads the drug master, writes the MOA master.
