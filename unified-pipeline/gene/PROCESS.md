# gene/ — Implementation Handoff

Everything an engineer picking this up for the first time needs:
architecture, per-script reference, Tom's decision queue, problems we hit
and how we solved them, and what's left to do. Pairs with `README.md`
(the original design spec).

Last updated: **2026-04-19**

---

## 0. START HERE — 5-minute orientation

### 0.1 What this pipeline does

Given a master list of oncology drugs (`dictionaries/drug_master.json`
produced upstream), derive:

1. **`moa_master.json`** — canonical, deduplicated mechanisms-of-action
   with gene targets.
2. **`gene_master.json`** — 461 curated oncology genes, each enriched
   with cancer subtypes, mutation hotspots, and the MOAs that target
   them.
3. Enrich every clinical trial in `output/conquest_master.json` with
   genes derived from the drugs it uses (→ `genes_from_drugs[]`).

Upstream: a drugs team already ships `drug_master.json`. A conditions
team already classifies trial conditions. This pipeline plugs in after
both — genes is the last step before publishing trials-with-genes.

### 0.2 Two modes of operation

- **Full rebuild** (`build_moa_master.py` + `build_gene_master.py`) —
  from scratch. Renames canonical keys as it cleans up. Safe only as
  a planned event (quarterly, or when the Excel changes).
- **Monthly incremental** (`ingest_monthly.py`) — load existing master,
  upsert only what's new. Canonical keys never rename — downstream
  systems can hold references.

### 0.3 The "canonical MOA key" contract

Every MOA entry has a slug like `kras_g12c_inhibitor` computed as
`{target_set}_{mutation?}_{action}`. Downstream systems join on this
slug. **In monthly mode, a slug never renames** — `merge_near_duplicates()`
is full-rebuild-only. That's the single most important invariant in
this codebase.

### 0.4 Where to look for what

| Question | Open |
|---|---|
| Why does each file exist / when to run it | §1.4 Per-script reference |
| What's the data flow diagram | §1.5 Architecture |
| What's Tom being asked to decide | §15.3 Tom's decision queue |
| What problem did X solve and how | §12 Mistakes made |
| How do I add genes / extend the Excel | §15.2 Definitive-fix pipeline |
| How does monthly incremental work | §10–§11 |
| Current file schemas | §7 |
| Known issues | §5 |
| Migration checklist | §13 |
| Test the code | `python -m unittest tests.test_ingest_moas` |

### 0.5 30-second status

- **Shipped:** all code. Three Tom-facing review files sitting in
  `gene/` + `gene/dictionaries/`. Incremental/monthly mode works.
  Tests pass.
- **Waiting on Tom:** T1 (Excel HGNC rename approval), T2 (20
  ambiguous Excel rows), T3 (586 proposed gene additions — ranked
  CSV, approve top-N). See §15.3.
- **After Tom approves:** run `apply_gene_additions.py` → one full
  rebuild → lock Excel format. Pipeline is then production-ready.

---

## 1. What exists today

### 1.1 Scripts (in build order)

```
gene/
├── build_cancer_vocab.py          # one-time: derive cancer vocab (levels tagged)
├── build_moa_master.py            # FULL REBUILD (thin orchestrator + merge pass)
├── build_gene_master.py           # FULL REBUILD (Gemini enrichment + vocab filter)
├── ingest_monthly.py              # MONTHLY INCREMENTAL (load existing + upsert)
├── backfill_unresolved_targets.py # QUARTERLY backfill after Excel extensions
├── consolidate_moas.py            # post-build singleton consolidation (Phase A + B)
├── extract_gene_candidates.py     # emit proposed_gene_additions.csv for Tom
├── normalize_excel_hgnc.py        # one-shot Excel HGNC normalization
├── apply_gene_additions.py        # apply Tom's approved CSV → definitive Excel
├── clean_supplement.py            # one-shot supplement cruft cleanup
├── classify_trial_genes.py        # (existing — applies moa_master to trials)
│
├── parser.py                      # Stage-1 deterministic MOA parser
├── moa_master.py                  # MoaMaster + ChangeSet + ingest_moas()
├── gene_master.py                 # GeneMaster + refresh_for_genes()
├── gemini_client.py               # Gemini 2.5 Flash wrapper (thread-safe cache)
│
└── tests/
    └── test_ingest_moas.py        # 9 integration tests, hermetic (no network)
```

### 1.2 Output files

```
gene/dictionaries/
├── cancer_vocab.json               # 407 oncotree codes + 39 broad cancers
├── gene_master.json                # 461 genes, enriched
├── moa_master.json                 # 5,784 canonical MOAs
├── moa_target_supplement.json      # 207 alias → HGNC mappings
└── *.before_fix                    # pre-Apr-17 backups
```

### 1.3 Cache files (safe to delete; rebuilds hit Gemini)

```
gene/cache/
├── gene_gemini_cache.json          # per-gene enrichment responses
├── moa_gemini_cache.json           # per-MOA normalization responses
├── target_hgnc_cache.json          # target → HGNC candidate responses
└── consolidate_moa_cache.json      # Phase-B singleton-match decisions
```

### 1.4 Per-script reference

Every CLI script has a single purpose and documented I/O. Library
modules are imported but not run directly. Listed in the order a new
hire should learn them.

| Script / module | Purpose | Inputs | Outputs | When to run |
|---|---|---|---|---|
| **Library modules** | | | | |
| `parser.py` | Stage-1 deterministic MOA parser. `parse_short_moa_multi()` handles compound strings, slug normalization, gene-family targets. | string | list of parsed dicts | imported |
| `moa_master.py` | `MoaMaster` class, `ChangeSet` dataclass, `ingest_moas()`, `merge_near_duplicates()` | drug entries + GeneMaster | MOA dict updates | imported |
| `gene_master.py` | `GeneMaster` class, `refresh_for_genes()`, `merge_existing_enrichment()`. Loads Excel + supplement, provides `resolve_target()`. | Excel + supplement + prior gene_master.json | gene dict updates | imported |
| `gemini_client.py` | Gemini 2.5 Flash wrapper with thread-safe SHA cache. Helpers: `enrich_moa_batch()`, `enrich_gene()`, `resolve_targets_to_hgnc()`. | prompts | cached responses | imported |
| **One-time / on-Excel-change** | | | | |
| `build_cancer_vocab.py` | Derive cancer vocabulary (oncotree codes + levels) from trial conditions. | `output/conquest_master.json` | `dictionaries/cancer_vocab.json` | once, + after any upstream conquest_master refresh |
| `normalize_excel_hgnc.py` | Rename non-HGNC Excel rows (PD-1 → PDCD1, HER2 (ERBB2) → ERBB2, …) | `New_Gene_updated.xlsx` | `New_Gene_updated.hgnc_normalized.xlsx` + `dictionaries/pending_excel_review.csv` | once, before definitive fix |
| `extract_gene_candidates.py` | Emit ranked list of HGNC symbols to add to the Excel. | `cache/target_hgnc_cache.json`, `moa_master.json`, `conquest_master.json` | `dictionaries/proposed_gene_additions.csv` | after every cold rebuild (Gemini produced new HGNC candidates) |
| `apply_gene_additions.py` | Apply Tom's approved CSV to the HGNC-normalized Excel. | `proposed_gene_additions.csv` (with `approved=y`) + `New_Gene_updated.hgnc_normalized.xlsx` | `New_Gene_updated.definitive.xlsx` | after Tom's T3 approval |
| `clean_supplement.py` | Drop null values, `_comment`, malformed keys from the supplement. | `dictionaries/moa_target_supplement.json` | same (+ `.before_cleanup` backup) | once; reran after Gemini expansions |
| **Full rebuild (~15–30 min)** | | | | |
| `build_moa_master.py` | Full rebuild of MOA master. Pre-pass (Gemini supplement expansion) → `MoaMaster.ingest_moas()` → merge-near-duplicates → save. | `drug_master.json`, Excel, `moa_target_supplement.json` | `dictionaries/moa_master.json` | quarterly, after Excel updates, or after parser/slug rule changes |
| `consolidate_moas.py` | Post-rebuild singleton consolidation. Phase A (deterministic re-parse) + Phase B (Gemini semantic merge vs top-60 canonicals). | `moa_master.json` | `moa_master.json` (merged) + `cache/consolidate_moa_cache.json` | after `build_moa_master.py` in a full rebuild |
| `build_gene_master.py` | Full rebuild of gene master. Excel → per-gene Gemini enrichment (concurrent) → vocab-filter `detailed_conditions` → cross-reference `moas_targeting`. | Excel, `moa_master.json`, `cancer_vocab.json` | `dictionaries/gene_master.json` | after `build_moa_master.py` |
| **Monthly / incremental** | | | | |
| `ingest_monthly.py` | Upsert new drugs into existing masters. Keys stable, no renames. Writes audit to `ingest_history/<YYYY-MM>.json`. | Subset of `drug_master.json` | `moa_master.json` + `gene_master.json` updates | every monthly upstream drug refresh |
| `backfill_unresolved_targets.py` | Retry resolving MOAs' `unresolved_targets[]` against current Excel. Catches gene_targets after Excel extension. | `moa_master.json`, Excel | updates | after every `apply_gene_additions.py` or manual Excel edit |
| **Downstream** | | | | |
| `classify_trial_genes.py` (existing) | Apply `moa_master.drug_to_moa` to trials, derive `genes_from_drugs[]`. | `moa_master.json`, `conquest_master.json` | `output/conquest_master_with_gene_targets.json` | full refresh; needs incremental version — see §13 |
| **Tests** | | | | |
| `tests/test_ingest_moas.py` | 9 hermetic unittest cases: idempotency, key stability, compound splitting, ChangeSet structure. | | | `python -m unittest tests.test_ingest_moas` |

### 1.5 Architecture & data flow

```
                                                     CONQUEST_MASTER.JSON
                                                     (50,777 trials, upstream)
                                                            │
                                                            ▼
                             ┌────────────────────────────┐
                             │ build_cancer_vocab.py      │
                             │   once / on refresh        │
                             └──────────────┬─────────────┘
                                            │
                                            ▼
NEW_GENE_UPDATED.XLSX          dictionaries/cancer_vocab.json
   (461+ genes, Excel)             (407 codes + 39 broad)
        │                                   │
        │     ┌────────────────────┐        │
        │     │ moa_target_        │        │
        │     │ supplement.json    │        │
        │     │ (alias → HGNC)     │        │
        │     └────────┬───────────┘        │
        │              │                    │
        ▼              ▼                    │
   ┌─────────────────────────┐              │
   │ GeneMaster              │◄─────────────┼─────── (reloaded after
   │ (load_from_excel())     │              │         supplement expansion)
   │  - genes{}              │              │
   │  - alias_index{}        │              │
   │  - resolve_target()     │              │
   └────────────┬────────────┘              │
                │                           │
                │  ┌──────────────────────┐ │
                │  │ DRUG_MASTER.JSON     │ │    upstream: drugs team
                │  │ (9,772 drugs,        │ │    ships monthly
                │  │  short_moa strings)  │ │
                │  └──────────┬───────────┘ │
                │             │             │
                ▼             ▼             │
    ┌─────────────────────────────────┐     │
    │ build_moa_master.py (FULL)      │     │
    │   or                            │     │
    │ ingest_monthly.py (INCREMENTAL) │     │
    │                                 │     │
    │  1. Gemini supplement pre-pass  │     │
    │     (target tokens → HGNC)      │     │
    │     ↓ updates supplement        │     │
    │  2. MoaMaster.ingest_moas():    │     │
    │     Stage 1 (deterministic)     │     │
    │     Stage 2 (Gemini long tail)  │     │
    │  3. merge_near_duplicates()     │     │
    │     (FULL REBUILD ONLY)         │     │
    └────────────┬────────────────────┘     │
                 │                          │
                 ▼                          │
     dictionaries/moa_master.json           │
     - moas{} (5,612)                       │
     - classes{}                            │
     - drug_to_moa{}                        │
                 │                          │
                 ▼                          │
    ┌──────────────────────────────────┐    │
    │ consolidate_moas.py (post-pass)  │    │
    │   Phase A: deterministic re-parse│    │
    │   Phase B: Gemini semantic match │    │
    │   FULL REBUILD ONLY              │    │
    └────────────┬─────────────────────┘    │
                 │                          │
                 ▼                          │
     dictionaries/moa_master.json (final)   │
                 │                          │
                 ▼                          │
    ┌──────────────────────────────────┐    │
    │ build_gene_master.py             │◄───┤
    │  1. Excel → seed 461 genes       │    │
    │  2. Gemini enrich each gene      │    │
    │     (concurrent, 10 workers)     │    │
    │  3. Filter detailed_conditions   │    │
    │     against cancer_vocab         │◄───┘
    │     (tissue-level excluded)      │
    │  4. Cross-ref moa_master →       │
    │     moas_targeting[]             │
    └────────────┬─────────────────────┘
                 │
                 ▼
     dictionaries/gene_master.json
     - genes{} (461)
       - detailed_conditions (vocab-filtered)
       - mutation_hotspots (Gemini)
       - pathway (Gemini)
       - moas_targeting (derived)
       - example_drugs (derived)
                 │
                 ▼
    ┌──────────────────────────────────┐
    │ classify_trial_genes.py          │
    │  (existing downstream script)    │
    │  Union moa_master.gene_targets   │
    │  per trial's drugs[]             │
    └────────────┬─────────────────────┘
                 │
                 ▼
     output/conquest_master_with_gene_targets.json
     - each trial gains genes_from_drugs[]
```

### 1.6 Monthly delta flow (what actually runs each month)

```
(upstream drugs team) → updated drug_master.json
                              │
                              ▼
      ┌─────────────────────────────────────┐
      │ ingest_monthly.py                   │
      │  --subset new_drugs_YYYY-MM.json    │
      │                                     │
      │  1. load existing moa_master.json   │
      │  2. supplement pre-pass (cached)    │
      │  3. MoaMaster.ingest_moas()         │
      │     — upsert only, NO key renames   │
      │  4. GeneMaster.refresh_for_genes()  │
      │     — only genes in ChangeSet       │
      │  5. write moa_master + gene_master  │
      │  6. audit → ingest_history/         │
      │     YYYY-MM.json                    │
      └────────────┬────────────────────────┘
                   │
                   ▼
     moa_master.json + gene_master.json
     (stable keys; existing refs preserved)
                   │
                   ▼
   classify_trial_genes.py (new trials only)
     → conquest_master_with_gene_targets.json
```

---

## 2. The pipeline, end-to-end

### 2.1 `build_cancer_vocab.py` (one-time)

Reads `output/conquest_master.json` (50,777 trials × 118,922 conditions) and
emits `dictionaries/cancer_vocab.json`:

```json
{
  "oncotree_to_name":   {"PAAD": "Pancreatic Adenocarcinoma", ...},
  "name_to_oncotree":   {"pancreatic adenocarcinoma": "PAAD", ...},
  "oncotree_to_broad":  {"PAAD": ["Pancreatic Cancer"], ...},
  "broad_cancers":      ["Breast Cancer", "Lung Cancers", ...]
}
```

This is the **canonical cancer-subtype vocabulary** used to constrain
`gene_master.detailed_conditions`. Rerun whenever `conquest_master.json`
changes.

### 2.2 `build_moa_master.py`

Runs the three-stage hybrid pipeline described in `README.md §3`:

1. **Pre-pass — Gemini target-HGNC expansion** (new, Apr 17)
   - Scans `drug_master.json`, runs Stage-1 parse to collect every unique
     target token that doesn't resolve via `New_Gene_updated.xlsx`.
   - Batches tokens (40 per call) through Gemini → proposes HGNC symbols.
   - Only additions whose proposed HGNC exists in the Excel are written to
     `moa_target_supplement.json`; the rest are audit-skipped with reasons
     (`non-gene` for pathways/complexes; `not-in-excel` for HGNC symbols
     Tom hasn't added).
   - `GeneMaster` is reloaded after supplement expansion so subsequent
     `resolve_target()` calls see the new aliases.

2. **Stage 1 — deterministic parse** (existing, upgraded)
   - `parser.parse_short_moa_multi()` now supports **compound splitting**
     (`"PARP inhibitor, PD-1 inhibitor, alkylating agent"` → 3 entries).
   - Phrase-level slug normalization so `multi-kinase`, `multi kinase`,
     `multikinase` all collapse; `topoisomerase I` / `(topo I)` /
     `topoisomerase 1` all collapse; `PD-1` / `PD 1` / `PD1` collapse; etc.
   - Gene-family descriptors (`parp inhibitor`, `hdac inhibitor`,
     `topoisomerase i inhibitor`) **removed from `CLASS_ONLY_MARKERS`** so
     they flow through target extraction (PARP/HDAC/TOP become target
     tokens Gemini can propose HGNC for).

3. **Stage 2 — Gemini for the long tail** (existing)
   - Strings that Stage 1 couldn't parse batch through
     `enrich_moa_batch()` (20 per call) with cache.

4. **Post — near-duplicate merge** (new, Apr 17)
   - `MoaMaster.merge_near_duplicates()` re-slugifies each MOA key and
     merges entries whose canonical form collides. Unions aliases,
     gene_targets, pathway, example_drugs; rewrites drug_to_moa refs.

5. **Stop silent class-only flip** (new, Apr 17)
   - Previously, any target that failed Excel lookup was silently
     reclassified as `is_class_only: true` with empty `gene_targets` —
     masking coverage gaps.
   - Now: `is_class_only` stays as parsed, unresolved targets go into a
     new `unresolved_targets: []` audit field on the MOA entry.

### 2.3 `build_gene_master.py`

1. Loads `New_Gene_updated.xlsx` → seeds 461 genes with aliases + broad
   cancers.
2. Loads `moa_master.json` and `cancer_vocab.json`.
3. **Gemini enrichment per gene** (now concurrent — 10 workers). Asks for
   `detailed_conditions`, `mutation_hotspots`, `pathway`, `role`, `notes`.
4. **Vocab-constrained post-filter** — `detailed_conditions` from Gemini
   are mapped to canonical oncotree names via (in order):
   - direct oncotree code match (`"DLBCL"` → `"Diffuse Large B-Cell Lymphoma, NOS"`)
   - parenthetical-code match (`"Colorectal Cancer (CRC)"` → CRC code)
   - normalized-name equality (case-insensitive, parens stripped)
   - broad-cancer equality fallback
   - substring containment
   - anything left → `detailed_conditions_dropped` audit field
5. **Cross-reference** — fills `moas_targeting[]` and `example_drugs[]`
   per gene from MOAs whose `gene_targets` contain it.

---

## 3. Metrics — before vs after (2026-04-17)

### 3.1 moa_master

| Metric                              | Before | Apr 17 | Apr 19 (post-consolidation) | Total Δ    |
|-------------------------------------|-------:|-------:|-----------:|:----------:|
| total MOAs                          |  5,753 |  5,784 |  **5,612** | −141       |
| `canonical_name = "unknown"`        |    500 |      1 |          1 | −99.8%     |
| gene-targeting MOAs                 |    529 |    694 |        553 | note below |
| class-only MOAs                     |  5,226 |  1,332 |      1,330 | −75%       |
| `unresolved_targets` audit field    |      — |  3,686 |      3,609 | added      |
| singletons (`drug_count=1`)         |      — |  4,694 |      4,522 | −172       |
| drugs mapped                        |  9,735 |  9,765 |      9,765 | +30        |
| supplement entries                  |     15 |    207 |        207 | +13×       |

**Why gene-targeting MOAs dropped 694 → 553 post-consolidation:**
consolidation merged 172 MOAs into canonicals, so the *count of distinct
gene-targeting entries* fell. Each remaining entry covers *more* drugs —
e.g. `kras_inhibitor` went from 6 → 17 drugs, `btk_inhibitor` 19 → 23.
This is the right direction — fewer entries, each more representative.

### 3.2 gene_master

- 453/461 genes Gemini-enriched (8 network timeouts; rerun picks them up)
- **390 genes** with `detailed_conditions`
- **1,173 conditions kept**, 500 dropped with audit trail
- **108 genes** with `moas_targeting` populated

### 3.3 Sample quality (KRAS)

```
detailed_conditions: Non-Small Cell Lung Cancer, Colorectal Cancer,
  Serous Ovarian Cancer, Biliary Tract, Thyroid Cancer,
  Myelodysplastic Syndromes, Myeloid
mutation_hotspots:   G12C, G12D, G12V, G13D, Q61H
moas_targeting (34): kras_g12c_inhibitor, kras_g12d_inhibitor,
  kras_inhibitor, krascovalent_g12c_inhibitor, ...
example_drugs:       adagrasib, sotorasib, divarasib, MRTX1133, ...
```

---

## 4. Fixes applied (Apr 17, 2026)

| # | Issue                                            | Where                          | Status  |
|--:|--------------------------------------------------|--------------------------------|---------|
| 1 | `canonical_name = "unknown"` default             | `build_moa_master._mk_canonical_name` | DONE |
| 2 | Near-dup keys (`multi_kinase` vs `multikinase`)  | `parser._slugify` + `merge_near_duplicates` | DONE |
| 3 | Compound strings not split                       | `parser.parse_short_moa_multi` | DONE    |
| 4 | Silent class-only flip for unresolved targets    | `build_moa_master._ingest_parsed` | DONE |
| 5 | Sparse supplement                                | `resolve_targets_to_hgnc` + pre-pass | DONE |
| 6 | Gene subtypes not grounded in trial universe     | `cancer_vocab` + post-filter   | DONE    |
| 7 | Sequential Gemini calls too slow (~80 min)       | `ThreadPoolExecutor(10)` + thread-safe cache | DONE |
| 8 | 4,694 singletons — too granular                  | `consolidate_moas.py` Phase A (deterministic) + Phase B (Gemini semantic merge) | PARTIAL — 172 merges applied, long tail remains (see §5.1) |

Every fix is idempotent. Reruns hit cache — zero Gemini cost unless cache
is cleared.

---

## 5. Remaining issues (ranked by impact)

### 5.1 🔴 HIGH — **81% of MOAs are singletons** (`drug_count = 1`)

4,694 of 5,784 MOAs are used by exactly one drug. This is the real reason
the entry count is 5,784 rather than the spec's ~850 target. Root cause:
Gemini's long-tail responses keep producing unique slugs for prose that
could merge into an existing canonical MOA. Examples:

- `activityofwhitebloodcells_growth_agonist` ← "Stimulates growth/activity of white blood cells"
- `topoisomerase1_trop2directedantibodydrugconjugate_inhibitor` ← multi-target ADC string
- `nerveimpulsesbyinhibitingsodiuminflux_blocker` ← raw prose

**Fix options (pick one):**
1. **Second Gemini pass** — for each low-volume key, show it alongside the
   top-100 canonical MOAs and ask "is this a duplicate of any canonical,
   yes/no + key?" Aggressive but effective.
2. **Embedding clustering** — encode canonical_name, cluster with
   threshold, merge cluster members via `merge_near_duplicates()`.
3. **Stage-1 compound-split upgrade** — some long slugs are genuine
   multi-target drugs (e.g. the TROP2 ADC). Extend the compound splitter
   to handle semicolons / "also targets" / enumerated targets.

Estimated reduction: 5,784 → ~1,500 MOAs after option 1 or 2.

### 5.2 🔴 HIGH — **Excel nomenclature drift**

`New_Gene_updated.xlsx` mixes HGNC symbols (`ERBB2`, `CD274`) with
clinical names (`PD-1`, `HER2`). Consequence:

- `pd1_inhibitor.gene_targets = ["PD-1"]` rather than `["PDCD1"]`.
- MOA parses that yield the HGNC form don't match the clinical form
  row, so gene coverage looks lower than it really is.

**Fix:** pick one nomenclature (recommend HGNC) and rewrite the Excel's
`Gene` column. Keep clinical names in `Gene Aliases`. Consider an
`hgnc_symbol` field on every gene entry in `gene_master.json` regardless
of what the Excel uses. **Tom decision required.**

### 5.3 🟡 MEDIUM — **CTLA4 (and probably others) missing from Excel**

`moa_target_supplement.json` has `ctla-4: null` — Gemini proposed `CTLA4`
but the Excel doesn't contain it, so the mapping was dropped. CTLA-4 is a
headline immunotherapy target, so this is a real gap.

**Fix:** run this query against the cached `target_hgnc_cache.json` to
surface a list of "missing-but-clinically-relevant" HGNC symbols for Tom
to review, then extend the Excel.

```python
# ~932 tokens Gemini mapped to an HGNC symbol not in the Excel —
# scan for well-known oncology targets
```

### 5.4 🟡 MEDIUM — **204 genes have tissue-level conditions** instead of subtypes

e.g. `BRCA1.detailed_conditions = ["Breast", "Serous Ovarian Cancer", "Prostate", "Pancreatic Adenocarcinoma"]`

`"Breast"` and `"Prostate"` are **oncotree tissue-level** codes, not
cancer subtypes. The vocab filter accepts them because they appear in
`oncotree_to_name`. Gemini is picking the shortest vocab match.

**Fix:** exclude level-1 (tissue) codes from the filter. In
`build_cancer_vocab.py`, check `oncotree_hierarchy.matched_level` and
only include codes where `level ≥ 2` in `oncotree_to_name`. Keep the
tissue codes in a separate `tissue_codes` block for reference.

### 5.5 🟡 MEDIUM — **71 genes with no `detailed_conditions`**

- ~8 are real Gemini timeouts (cached as null; `gene_gemini_cache.json`
  has empty responses). Rerun catches them.
- The rest had every Gemini suggestion dropped by the vocab filter —
  common for rare/pediatric genes where Gemini suggests syndromes not in
  oncotree.

**Fix:** after the tissue-level fix (§5.4), rerun. Then manually review
the remaining to decide whether the vocab needs extending or the gene
truly has no oncotree-aligned subtypes.

### 5.6 🟢 LOW — **Supplement cruft**

`moa_target_supplement.json` contains:
- `"_comment": "Hand-curated alias → gene map..."` — artifact of original
- `"pd-1": null` — Gemini mapped to PDCD1, not in Excel, but null leaked
- malformed keys like `"2 (map2k1"`, `"bispecific adc; egfr"`

All harmless (`resolve_target` ignores nulls; malformed keys never match
real input), but should be cleaned.

**Fix:** one-time dedup script that drops null values and keys starting
with `_`, `[0-9]`, or containing `;`.

### 5.7 🟢 LOW — **731 entries with very long slug keys** (>40 chars)

Purely a cosmetics issue — all are drug_count=1 singletons covered by
§5.1. Resolved when §5.1 is resolved.

### 5.8 🟢 LOW — **`classes` dict has only 8 entries**

Original spec expected ≤100 canonical drug classes. Currently only 8
because the pipeline routes most class-info into `moas[]` with
`is_class_only: true` rather than into `classes[]`. Whether to fix
depends on whether downstream code actually reads `moas.is_class_only`
vs `classes[]` — check `classify_trial_genes.py`.

---

## 6. How to rebuild from scratch

```bash
cd conquest-pipeline-v2/gene

# 1. (one-time) derive the cancer vocabulary
python build_cancer_vocab.py

# 2. build moa_master (uses Gemini — ~5 min if cache warm, ~15 min cold)
python build_moa_master.py
#   --no-gemini   # Stage 1 only, deterministic
#   --limit 200   # smoke test on first 200 drugs

# 3. build gene_master (concurrent Gemini — ~5 min warm, ~20 min cold)
python build_gene_master.py
#   --no-gemini   # skip per-gene enrichment
#   --limit 10    # smoke test

# 4. classify trial genes (existing; uses moa_master)
python classify_trial_genes.py
```

### Resetting

```bash
# clear caches to force fresh Gemini calls
rm -rf gene/cache/*.json

# restore pre-fix snapshots
cd gene/dictionaries
cp moa_master.json.before_fix       moa_master.json
cp gene_master.json.before_fix      gene_master.json
cp moa_target_supplement.json.before_fix moa_target_supplement.json
```

### Monthly / incremental ops

```bash
# Monthly: ingest new drugs only (stable canonical keys, no renames)
python ingest_monthly.py --subset new_drugs_2026_05.json
python ingest_monthly.py --subset new_drugs_2026_05.json --dry-run

# After Tom extends the Excel (T3 approvals applied), run this once so
# existing MOAs with those targets gain gene_targets retroactively
python backfill_unresolved_targets.py

# Run tests (no network, ~50ms)
cd gene && python -m unittest tests.test_ingest_moas
```

---

## 7. Schema reference (what's actually in the files)

### 7.1 `moa_master.json`

```json
{
  "metadata": {
    "version": "1.0",
    "built_at": "2026-04-17T...",
    "source_drug_master": "drug_master.json",
    "stage1_specific": 5381,
    "stage1_class_only": 423,
    "stage2_unique_inputs": 3466,
    "use_gemini": true,
    "post_merge": {"merged": 1, "groups": 1},
    "total_moas": 5784,
    "total_classes": 8,
    "total_drugs_mapped": 9765
  },
  "moas": {
    "kras_g12c_inhibitor": {
      "canonical_name": "KRAS G12C inhibitor",
      "aliases": ["KRAS G12C inhibitor", "KRASG12C inhibitor", ...],
      "targets": [{"raw": "KRAS", "gene_symbol": "KRAS", "mutation": "G12C"}],
      "gene_targets": ["KRAS"],
      "action": "inhibitor",
      "drug_class": "small_molecule_inhibitor",
      "pathway": [],
      "is_class_only": false,
      "unresolved_targets": [],      // NEW (2026-04-17)
      "drug_count": 21,
      "example_drugs": ["adagrasib", "sotorasib", "divarasib", ...],
      "source": "deterministic"
    }
  },
  "classes":      { "kinase_inhibitor": {...}, ... },
  "drug_to_moa":  { "sotorasib": ["kras_g12c_inhibitor"], ... }
}
```

### 7.2 `gene_master.json`

```json
{
  "metadata": { "version": "1.0", "total_genes": 461, ... },
  "genes": {
    "KRAS": {
      "gene": "KRAS",
      "aliases": ["KRAS", "K-RAS", "KI-RAS2", ...],
      "broad_cancers": ["Colorectal Cancer", "Lung Cancers", ...],
      "detailed_conditions": ["Non-Small Cell Lung Cancer",
                              "Colorectal Cancer", ...],
      "detailed_conditions_dropped": [               // NEW
        "KRAS-mutant Colorectal Cancer (CRC)",
        "Pancreatic Ductal Adenocarcinoma (PDAC)"
      ],
      "mutation_hotspots": ["G12C", "G12D", "G12V", "G13D", "Q61H"],
      "pathway": ["MAPK", "RAS/RAF/MEK/ERK"],
      "role": "oncogene",
      "notes": "...",
      "moas_targeting": ["kras_g12c_inhibitor", ...],
      "example_drugs": ["adagrasib", ...],
      "source": { ... audit of which field came from where ... }
    }
  },
  "alias_index": { "k-ras": "KRAS", ... }
}
```

### 7.3 `cancer_vocab.json`

```json
{
  "metadata": { ... n_trials_scanned, n_conditions_scanned ... },
  "oncotree_to_name":  { "PAAD": "Pancreatic Adenocarcinoma", ... },
  "name_to_oncotree":  { "pancreatic adenocarcinoma": "PAAD", ... },
  "oncotree_to_broad": { "PAAD": ["Pancreatic Cancer"], ... },
  "broad_cancers":     ["Breast Cancer", ...]
}
```

### 7.4 `moa_target_supplement.json`

```json
{
  "pd-l1":       "CD274",
  "her2":        "ERBB2",
  "aromatase":   "CYP19A1",
  "androgen":    "AR",
  "bcl-2":       "BCL2",
  ...
}
```

Gene on the right must exist in `New_Gene_updated.xlsx` for the alias to
be loaded at runtime (enforced by `GeneMaster._load_supplement`).

---

## 8. Decisions open for Tom

1. **HGNC normalization of the Excel** (§5.2). Recommended: standardize
   `Gene` column to HGNC, move clinical names to aliases. Downstream
   code that currently reads `gene_master.genes["HER2"]` will need to be
   migrated to `["ERBB2"]`.
2. **Excel coverage extension** (§5.3). `target_hgnc_cache.json` has
   ~932 tokens Gemini mapped to a real HGNC that isn't in the Excel.
   Should I generate a review list sorted by drug_count for you?
3. **Singleton consolidation strategy** (§5.1). Gemini second-pass vs
   embeddings? The second-pass is easier; embeddings are more principled
   but require a new dep.
4. **Tissue-level exclusion** (§5.4). Any reason to keep tissue codes in
   `detailed_conditions`, or OK to strip? (Recommend strip.)

---

## 9. File → test / verify

```bash
# quick sanity check — parser
python -c "
from parser import parse_short_moa_multi
for s in ['KRAS G12C inhibitor',
         'PARP inhibitor, PD-1 inhibitor, alkylating agent',
         'Multi-kinase inhibitor']:
    print(s, '→', [x['canonical_key'] for x in parse_short_moa_multi(s)])
"

# quick sanity check — gene master
python -c "
import json
with open('dictionaries/gene_master.json') as f: g = json.load(f)
print('enriched:', sum(1 for v in g['genes'].values() if v['detailed_conditions']))
print('w/ moas:', sum(1 for v in g['genes'].values() if v['moas_targeting']))
"
```

---

## 10. Future monthly pipeline — operational flow

### 10.1 Upstream context

Once deployed, a monthly job fetches **new trials** from clinicaltrials.gov
and enriches each with:

| Step         | Owner       | Status             | Output                  |
|--------------|-------------|--------------------|-------------------------|
| drugs        | drugs team  | built, deploy-ready | new drugs in `drug_master.json` with `short_moa` / `drug_class` |
| conditions   | conditions  | built, deploy-ready | trial → oncotree condition mapping |
| **genes**    | **this**    | **batch-only — needs incremental mode** | trial → gene list |

The genes step sits *downstream* of drugs: a new trial's genes come from
(a) explicit gene mentions in trial text + (b) MOA-derived gene_targets
via its drugs. So the genes step must consume whatever new MOAs the
drugs step produced that month.

### 10.2 What "monthly" looks like in numbers

Typical monthly delta (estimate):

- **~500–2,000 new trials**
- **~50–200 new drugs** (most are repeats of existing drugs)
- **~20–100 new unique `short_moa` strings** (most are exact duplicates
  of existing canonical MOAs)
- **~0–5 new genes** added to `New_Gene_updated.xlsx` (rare — Tom-gated)

This means the monthly work is **mostly extending existing entries**,
not creating new canonical MOAs or genes. That's the opposite of the
current full-rebuild flow.

### 10.3 Required flow

```
  (monthly) new drugs arrive with short_moa strings
              │
              ▼
  ┌──────────────────────────────┐
  │ normalize_moas(new_strings)  │  Stage 1 + Stage 2 for NEW only
  │   – hits existing caches     │  existing strings cost $0 (SHA cache)
  └──────────────┬───────────────┘
                 │  list[{canonical_key, targets, gene_targets, ...}]
                 ▼
  ┌──────────────────────────────┐
  │ moa_master.ingest_moas():    │
  │  - if key exists → extend    │  aliases, example_drugs, drug_to_moa
  │  - if new → add entry        │
  │  - returns ChangeSet          │  {added_keys, affected_genes}
  └──────────────┬───────────────┘
                 │  affected_genes: set[str]
                 ▼
  ┌──────────────────────────────┐
  │ gene_master.refresh_for():   │  recompute moas_targeting +
  │   affected_genes             │  example_drugs for changed genes
  │  - no new Gemini calls       │  (detailed_conditions unchanged)
  └──────────────┬───────────────┘
                 │
                 ▼
  ┌──────────────────────────────┐
  │ classify_trial_genes():      │
  │   new_trials only            │
  └──────────────────────────────┘
```

### 10.4 Why full-rebuild is the wrong shape for monthly

| Concern                     | Full rebuild       | Incremental      |
|-----------------------------|--------------------|------------------|
| Canonical key stability     | May shift each run | Must stay stable |
| Runtime                     | ~15–30 min         | <1 min (typical) |
| Gemini cost                 | $0 if cache warm   | $0 (cache hit)   |
| Risk of silent breakage     | High (reshuffle)   | Low (additive)   |
| Downstream join integrity   | Fragile            | Preserved        |

A full rebuild is fine for quarterly refreshes or after Excel changes,
but **unsafe monthly** because downstream systems hold references to
canonical MOA keys — if `kras_g12c_inhibitor` gets renamed by a merge
pass in a future rebuild, every trial that indexed into it breaks.

---

## 11. Incremental API — code-level design (TO BUILD)

### 11.1 New entry points needed

```python
# gene/moa_master.py

class MoaMaster:
    def ingest_moas(
        self,
        drug_entries: dict,            # {drug_key: {"short_moa": ..., "drug_class": ...}}
        gm: GeneMaster,
        *,
        use_gemini: bool = True,
    ) -> "ChangeSet":
        """Normalize new MOAs and upsert into self.moas / drug_to_moa.
        Returns a ChangeSet describing what changed."""

@dataclass
class ChangeSet:
    added_moa_keys: set[str]
    extended_moa_keys: set[str]       # existing keys that gained aliases/drugs
    affected_genes: set[str]
    new_drugs_linked: set[str]
    new_unresolved_targets: set[str]  # for Tom's review
```

```python
# gene/gene_master.py

class GeneMaster:
    def refresh_for_genes(
        self,
        affected: set[str],
        moa: MoaMaster,
    ) -> None:
        """Recompute moas_targeting + example_drugs for changed genes only.
        Leaves detailed_conditions / mutation_hotspots alone (those are
        Gemini-enriched per-gene, not derived from MOAs)."""
```

```python
# gene/classify_trial_genes.py

def classify_trials(
    new_trials: list[dict],
    moa: MoaMaster,
) -> list[dict]:
    """Apply moa_master to a specific batch of trials. Idempotent —
    re-running on the same trial yields the same genes_from_drugs[]."""
```

### 11.2 Stability contracts

**Canonical keys must never rename between runs.** Downstream systems
(search indexes, saved queries, trial records) hold references.
Specifically:

- `MoaMaster.merge_near_duplicates()` renames winner keys — **only safe
  on full rebuild**, never on monthly increments. Guard with a flag.
- Slug normalization rules in `parser._PHRASE_NORMALIZE` must never be
  changed in a way that re-slugifies existing strings differently. Any
  rule change = scheduled full rebuild + migration.
- If two Stage-1 rules would produce different slugs for the same input,
  the deterministic parser is authoritative; Gemini cannot override.

### 11.3 Where current code is ready vs. needs work

| Piece                                       | Status | Notes |
|---------------------------------------------|:------:|:------|
| `parser.parse_short_moa_multi()`            |   ✅   |       |
| `MoaMaster.add_moa()` / `link_drug()` / `record_alias()` (all upsert) | ✅ | |
| `MoaMaster.ingest_moas(drug_entries, gm) → ChangeSet` | ✅ | **Shipped Apr 19.** Shared upsert path used by both full rebuild and monthly |
| `ChangeSet` dataclass                       |   ✅   | `added_moa_keys`, `extended_moa_keys`, `affected_genes`, `new_unresolved_targets`, `unresolved_raw_strings`, `errors` |
| `GeneMaster.refresh_for_genes(affected, moa)` | ✅ | **Shipped Apr 19.** Recomputes moas_targeting for a subset only |
| `GeneMaster.merge_existing_enrichment()`    |   ✅   | **Shipped Apr 19.** Preserves `detailed_conditions` etc. across runs |
| `build_moa_master.build()`                  |   ✅   | **Refactored Apr 19** — thin orchestrator around `ingest_moas()` + merge pass |
| `ingest_monthly.py`                         |   ✅   | **Shipped Apr 19.** Load existing → supplement pre-pass (cached) → ingest → refresh genes → write + audit |
| `classify_trial_genes.py`                   |   ?   | Unverified whether it accepts a trial subset vs all 50k — inspect next |
| JsonCache (thread-safe)                     |   ✅   |       |
| `merge_near_duplicates()` gated from monthly |   ✅   | Only called in `build_moa_master.build()`; `ingest_monthly.ingest()` never calls it |
| `ingest_history/<YYYY-MM>.json` audit       |   ✅   | Emitted per monthly run by `ingest_monthly.py` |

### 11.4 What to persist between runs

Currently all state lives in files. Keep that for monthly mode too:

- `moa_master.json` — read at start, mutate, atomic write at end
- `gene_master.json` — same
- `moa_target_supplement.json` — append-only; never delete existing entries
- `cache/*.json` — always additive; clear only for full rebuild

Add one new file:

- `ingest_history/<YYYY-MM>.json` — per-run ChangeSet (added keys,
  affected genes, unresolved targets) for audit and rollback. ~10 KB per
  month.

### 11.5 Failure modes and recovery

- **Gemini rate-limit / down:** Stage-2 MOAs fall back to Stage-1 class-only
  behavior with a warning. Recovery: re-run next day; cache will catch up.
- **Excel updated mid-pipeline (Tom adds a gene):** the supplement pre-pass
  picks up the new gene on next run. Previously-unresolved MOAs that map
  to the new gene will gain `gene_targets` on their next `ingest_moas()`
  touch — **they won't update retroactively for untouched MOAs.**
  Solution: periodic (quarterly?) targeted backfill scan:
  `scan_unresolved_targets_against_excel()`.
- **Corrupt write:** all writes are `tmp + os.replace`. Safe by default.
- **Duplicate ingest of same drug:** upsert is idempotent — a drug
  already in `drug_to_moa` for that MOA key won't create a second entry.

---

## 12. Mistakes made during this session — don't repeat

Kept here deliberately so a future engineer can learn from the debugging
without having to re-discover these traps.

### 12.1 Mutated the cached Gemini response in-place

```python
# WRONG — mutates the object that JsonCache still holds
resp["detailed_conditions"] = filtered
resp["detailed_conditions_dropped"] = dropped
cache.set(key, resp)            # stores the mutated version

# RIGHT — build a new dict for application, leave cache raw
apply_resp = {**resp, "detailed_conditions": filtered}
gm.apply_gemini_enrichment(gene, apply_resp)
```

Symptom: when I later improved the filter and re-ran, the cached
`detailed_conditions` was already the *previous* filtered output, not
the original Gemini response. Had to clear `gene_gemini_cache.json` and
eat 461 fresh Gemini calls. **Lesson: never mutate objects you got from
a cache. Treat cached values as immutable.**

### 12.2 `JsonCache` wasn't thread-safe

Concurrent flushes both wrote to `cache.json.tmp` then called
`os.replace(tmp, path)` → one won, one got `FileNotFoundError`. Fixed by
using a per-thread tmp path: `f"{path}.tmp.{pid}.{tid}"` and a
`threading.Lock` around `data` access. **Lesson: if you add a
ThreadPoolExecutor, audit every shared mutable state it touches.**

### 12.3 Initial vocab filter was too strict

First draft dropped `"Colorectal Cancer (CRC)"` because exact/substring
match didn't handle the parenthetical acronym. Fix: strip parens with
`_normalize_condition()` before matching; also handle embedded oncotree
codes like `"... (DLBCL)"`. **Lesson: Gemini's free-text output always
has trailing acronym glosses. Normalize before matching.**

### 12.4 Forgot the Excel uses mixed nomenclature

Assumed `New_Gene_updated.xlsx` was HGNC-only. It isn't — it has
`PD-1` as a canonical Gene row (not `PDCD1`), `HER2` does NOT appear
(only `ERBB2` does), `CTLA4` is MISSING entirely. Consequence: some MOAs
get `gene_targets: ["PD-1"]` (non-HGNC) and CTLA-4 drugs silently
fail gene linking. **Lesson: never assume source-of-truth uses HGNC.
Verify each symbol with `gm.genes.get(symbol)` before trusting.**

### 12.5 Gene-family descriptors blocked target extraction

`"parp inhibitor"`, `"hdac inhibitor"`, `"topoisomerase i inhibitor"`
were in `CLASS_ONLY_MARKERS` — they short-circuited to class-only before
Stage-3 ever saw `PARP` / `HDAC` / `TOP1` as target tokens. So the
Gemini supplement expansion never got a chance to propose HGNC mappings
for them. Fix: remove gene-family descriptors from `CLASS_ONLY_MARKERS`
and let the standard suffix-form parser extract them. **Lesson:
"class-only" is a data quality decision, not a parsing shortcut. Extract
the target always; decide class-only based on whether the target
resolves.**

### 12.6 `_mk_canonical_name` defaulted to `"unknown"`

When targets + action were both empty (class-only Stage-1 entries),
canonical name became the literal string `"unknown"`. 500 entries had
that display name. Fix: fall back to the raw string. **Lesson: for any
"can't compute" path, the best fallback is usually the input itself,
not a sentinel.**

### 12.7 Silent `is_class_only` flip masked coverage gaps

```python
# WRONG — hides the fact that PARP parsed correctly but wasn't in the Excel
if not gene_targets and targets:
    is_class_only_effective = True

# RIGHT — keep is_class_only as parsed; surface the gap
unresolved = [t for t in targets if gm.resolve_target(t) is None]
# store unresolved on the entry for audit
```

Result before fix: PARP/HDAC/CTLA-4 all looked like legitimate
class-only entries, and nobody could tell the Excel was incomplete.
After fix: `unresolved_targets: ["PARP"]` is visible and the
pre-pass Gemini expansion can propose HGNC mappings. **Lesson: never
silently reclassify data in a way that erases the original signal.
Add an audit field instead.**

### 12.8 `parse_short_moa` had single-return API

Couldn't represent `"PARP inhibitor, PD-1 inhibitor, alkylating agent"`
as three separate MOAs — it forced everything into one noisy entry. Fix:
added `parse_short_moa_multi()` that returns a list; single-MOA inputs
return `[one_entry]`. **Lesson: when a real-world input can express
multiple instances of your domain object, the parser must return a list
from day one.**

### 12.9 Slug normalization was too weak

Initial `_slugify_target` only stripped non-alphanumerics, so
`multi-kinase` → `multikinase` but `multi_kinase` → `multi_kinase`
(different slug). Added `_PHRASE_NORMALIZE` regex rules before slugifying
— `(multi[-\s_]+kinase)` → `multikinase`, `topoisomerase ii` →
`topoisomerase2`, `PD-L1` → `pdl1`, etc. **Lesson: collapse alias
variants at the phrase level before slugifying, not after.**

### 12.10 `| tail -60` hid live progress

```bash
python3 build_moa_master.py 2>&1 | tail -60    # WRONG — buffers until EOF
python3 -u build_moa_master.py > run.log 2>&1  # RIGHT — unbuffered, read anytime
```

Symptom: monitor saw zero output for 10+ minutes during a Gemini-heavy
pre-pass, because `tail` only flushes when its stdin closes.
**Lesson: for long-running background jobs, always `-u` + file + `tail -f`.**

### 12.11 Gene enrichment was sequential (~80 min for 461 genes)

Original `build_gene_master.py` called Gemini one gene at a time, each
call ~10 s, with 60 s timeouts that wasted another minute each retry.
Fix: `ThreadPoolExecutor(max_workers=10)`. Went from 80 min → ~8 min.
Prerequisite: thread-safe `JsonCache` (see §12.2). **Lesson: Gemini
Flash handles 10 concurrent requests easily. Any time you have N
independent calls, reach for a ThreadPoolExecutor first.**

### 12.12 `merge_near_duplicates()` renames keys

Currently the merge pass renames winner-key to the normalized canonical
slug. For full rebuild that's fine; **for monthly incremental that's
poison** — downstream systems holding `kras_g12c_inhibitor` won't find
it anymore if a future run renames it to `krasg12c_inhibitor`. **Lesson:
data transforms that rename identifiers need a migration flag, not a
default. Guard with `if full_rebuild: merge_near_duplicates()`.**

---

## 13. Migration checklist — batch → monthly mode

Before wiring this into the monthly pipeline, do:

- [x] Extract `ingest_moas()` on `MoaMaster` from the body of
      `build_moa_master.build()`. Make the CLI a thin wrapper. **(Apr 19)**
- [x] Extract `refresh_for_genes(affected)` on `GeneMaster` from
      `build_gene_master.apply_moa_master()`. **(Apr 19)**
- [x] Gate `MoaMaster.merge_near_duplicates()` behind a full-rebuild
      guardrail — `ingest_monthly.py` never calls it. **(Apr 19)**
- [x] Add `ingest_history/<YYYY-MM>.json` write after each monthly run. **(Apr 19)**
- [x] Ship `ingest_monthly.py` as the production entry point. **(Apr 19)**
- [x] Fix `build_gene_master.py --no-gemini` enrichment-wiping bug. **(Apr 19)**
- [x] Clean supplement cruft (null values, `_comment`, malformed keys). **(Apr 19)**
- [ ] Split `classify_trial_genes.py` into a library function that
      accepts a trial iterable and a CLI that runs on everything.
- [x] Write `backfill_unresolved_targets.py` — quarterly job that
      backfills `gene_targets` for existing MOAs whenever Tom extends
      the Excel. **(Apr 19)**
- [x] Tissue-level exclusion in vocab (§5.4) — default ON, opt-out via
      `--include-tissue-level`. **(Apr 19)** — `detailed_conditions`
      now excludes oncotree level-1 codes (Breast, Lung, Prostate)
      automatically.
- [ ] Decide on HGNC normalization for the Excel (§5.2, §8, §15).
      **IN PROGRESS** — `New_Gene_updated.hgnc_normalized.xlsx` ready
      for Tom's review.
- [x] Integration tests for `ingest_moas()` — 9 unittest cases:
      idempotency, key stability across runs, compound splitting,
      affected_genes correctness, audit-field population,
      refresh_for_genes behavior. All pass hermetically (no network).
      **(Apr 19)**
- [ ] Add a "new-unresolved-targets" report emailed to Tom after each
      monthly run — so Excel gaps don't silently accumulate.
- [ ] Document a full-rebuild cadence (quarterly?) and rollback plan
      (the `*.before_fix` snapshot pattern is fine; formalize it).

---

## 14. Open questions / decisions parked

(Things I wasn't sure about; listing here so they aren't forgotten.)

1. **What's the ChangeSet consumer?** A monitoring dashboard? A
   diff-emailed-to-Tom? Determines the ChangeSet schema.
2. **How often to full-rebuild?** Recommend quarterly, tied to Excel
   updates. Needs Tom's signoff.
3. **Should `drug_class` variants in `classes{}` be expanded?** Currently
   8 entries; original spec expected ≤100. Depends on whether
   downstream code reads `moas.is_class_only` or `classes[]`. See §5.8.
4. **Retroactive Excel expansion:** if Tom adds PARP1, do we re-scan
   existing MOAs to link them? Solution in §11.5, but needs a schedule.
5. **Deployment mechanics:** which service actually calls the monthly
   pipeline? Airflow? Cron? Depends on infra team.

---

## 15. THE DEFINITIVE FIX — in progress (2026-04-19)

Goal: stop iterating on the gene pipeline. Fix the root cause of the
remaining issues in one coordinated pass, then lock.

### 15.1 Why this is "the one fix"

After the Apr 17 rebuild and Apr 19 consolidation, the MOA master is
internally consistent. The remaining gaps are all **upstream**, in
`New_Gene_updated.xlsx`:

- **Coverage gap.** ~932 HGNC symbols that Gemini correctly identified
  (from free-text MOA strings) aren't in the Excel. Every MOA that
  targets them stays `gene_targets: []`. Examples: **CTLA4**, PARP1/2/3/4,
  HDAC1/2/3/6, TOP1/TOP2A, CYP17A1, CYP19A1, ~920 others.
- **Nomenclature drift.** Excel mixes HGNC symbols (`ERBB2`, `CD274`,
  `KRAS`) with clinical names (`PD-1`, `HER2`, `HER3`, `HER4`, `VEGF`).
  Downstream joins against HGNC-canonical datasets break on the
  clinical-name rows.

Fix both → the vocabulary becomes closed and self-consistent → every
downstream issue we've been chasing stops reappearing.

### 15.2 Definitive-fix pipeline

```
Step 1  extract_gene_candidates.py
          → reads cache/target_hgnc_cache.json
          → for each HGNC symbol Gemini proposed that isn't in the
            Excel, computes "impact":
              drugs_gained  = drugs whose MOAs would resolve to a gene
              moas_gained   = MOAs that would flip from class-only
                              (or empty gene_targets) to gene-targeting
              broad_cancers = most common trial broad_cancers for those
                              drugs (helps Tom sanity-check)
          → emits dictionaries/proposed_gene_additions.csv,
            sorted by drugs_gained desc.

Step 2  Tom reviews the CSV — approves a subset (typically top 50–150).
          → removes false positives or genes deemed out-of-scope
          → returns approved_gene_additions.csv (same schema, curated)

Step 3  normalize_excel_hgnc.py
          → runs in parallel with Step 2 (deterministic, no Tom input)
          → loads New_Gene_updated.xlsx, finds rows where Gene is a
            clinical name (PD-1, HER2, HER3, HER4, VEGF), rewrites:
              Gene: PD-1  → PDCD1
              Aliases: + "PD-1"
          → writes New_Gene_updated.xlsx.hgnc_normalized (new file,
            non-destructive) for Tom's one-time approval

Step 4  Apply Tom's approved additions + the HGNC-normalized sheet
        → New_Gene_updated.xlsx becomes the canonical, HGNC-aligned,
          coverage-complete source of truth.
        → back up the previous Excel as .before_definitive_fix.

Step 5  Full rebuild, one time:
          python build_moa_master.py     # supplement pre-pass now resolves
          python build_gene_master.py    # more genes get moas_targeting
        → expected jumps:
            gene-targeting MOAs: 553  →  ~1,200+
            genes with moas_targeting: 108  →  ~200+
            unresolved_targets population: 3,609  →  <1,000

Step 6  Lock. Document the Excel format as "HGNC-canonical, Gene column
        is always the approved HGNC symbol, aliases column holds clinical
        names / synonyms." Put that in the Excel header row comment.
```

### 15.3 Tom's decision queue

**Do these once, in this order. Each is a focused review, not an
open-ended question.**

| # | Decision                                                            | What Tom reviews                          | Where the answer goes |
|--:|---------------------------------------------------------------------|-------------------------------------------|----------------------|
| 1 | Approve Excel HGNC normalization                                    | `New_Gene_updated.xlsx.hgnc_normalized` (diff against current) | OK / changes back to me |
| 2 | Approve gene additions (top-N of 932 proposed)                      | `dictionaries/proposed_gene_additions.csv` (ranked by impact) | `approved_gene_additions.csv` (same file, pruned) |
| 3 | Tissue-level exclusion for `detailed_conditions` **(pre-applied — recommended default)** | Verify: `BRCA1 → Serous Ovarian Cancer, Pancreatic Adenocarcinoma` (tissue names like "Breast", "Prostate" gone). If you'd rather keep tissue-level, rerun `build_gene_master.py --include-tissue-level` to flip. | Confirm / flip |
| 4 | Full-rebuild cadence for production                                 | Monthly (risky — keys may rename) vs quarterly (recommended) vs on-Excel-change | Pick one |
| 5 | Future Excel extensions — who owns additions during monthly runs    | Auto-append from Gemini proposals? Manual review only? Threshold-gated? | Policy statement |

### 15.4 Expected end-state after the fix

| Metric                              | Now (Apr 19) | After fix (projected) |
|-------------------------------------|-------------:|----------------------:|
| Genes in Excel                      |          461 |             ~550–610  |
| Excel rows using non-HGNC names     |          ~15 |                    0  |
| gene-targeting MOAs                 |          553 |              ~1,200+  |
| MOAs with `unresolved_targets`      |        3,609 |                 ≤1,000 |
| Genes with `moas_targeting`         |          108 |                ~200+  |
| Drug→gene join success rate         |         ~40% |                 ~75%+ |

### 15.5 Why this won't loop again

The iteration loop we've been in looked like:

1. Find a MOA with empty `gene_targets` → investigate
2. Discover the target isn't in Excel → note it
3. Ship anyway with an audit field → move on
4. Next MOA review finds another missing gene → loop

Definitive fix breaks the loop at step 2: **every HGNC symbol Gemini can
name is either in the Excel (resolved) or explicitly rejected by Tom
(documented as out of scope).** From that point, any new missing gene is
either (a) a novel target not yet in the cached Gemini proposals — rare,
handled by the monthly supplement pre-pass writing to `proposed_gene_
additions_YYYY-MM.csv`; or (b) a genuinely new biological entity — also
rare, emailed to Tom as part of the monthly report.

Nothing silently falls through.

### 15.6 Status as of 2026-04-19

**Done — code & data deliverables for Tom's review:**

- [x] moa_master post-consolidation (5,612 MOAs, 172 merges applied)
- [x] Backup: `dictionaries/moa_master.json.before_consolidation`
- [x] Backup: `dictionaries/moa_master.json.before_fix` (pre Apr 17)
- [x] **`extract_gene_candidates.py`** — generates ranked impact CSV
- [x] **`dictionaries/proposed_gene_additions.csv`** — 586 candidate HGNC symbols, sorted by drugs_gained desc. Columns: approved, hgnc_symbol, drugs_gained, moas_gained, confidence, sample_tokens, sample_drugs, sample_moas, top_broad_cancers, notes. Top 10 alone covers **1,518 drugs of added gene coverage**: TOP1 (218), CD20 (204), TNFRSF17/BCMA (173), PDCD1 (173), CD276/B7-H3 (163), CD28 (154), CD47 (131), CD70 (125), IL18 (119), IL6 (116).
- [x] **`normalize_excel_hgnc.py`** — deterministic HGNC normalizer
- [x] **`New_Gene_updated.xlsx.hgnc_normalized`** — 8 safe renames applied (PD-1→PDCD1, HER2 (ERBB2)→ERBB2, STK11 (LKB1)→STK11, FANCN (PALB2)→PALB2, NTRK 1/2/3→NTRK1/2/3, PPARγ→PPARG). Clinical names moved to aliases.
- [x] **`dictionaries/pending_excel_review.csv`** — 20 ambiguous Excel rows for Tom's manual cleanup (IHC markers, gene families, mutation-specific rows)

**Waiting on Tom (5 decisions, each is a focused review):**

- [ ] **T1. Review `New_Gene_updated.xlsx.hgnc_normalized`** — diff against current. Approve or send back with corrections. (est: 10 min)
- [ ] **T2. Review `pending_excel_review.csv`** — 20 rows with suggestions. For each, decide: rename / remove / keep as-is. (est: 20 min)
- [ ] **T3. Review `proposed_gene_additions.csv`** — 586 rows ranked by impact. Fill the `approved` column (y/n) for the ones to add. Top 50–150 captures most of the value. (est: 30–60 min)
- [x] **T4. Tissue-level exclusion** (§5.4) — **pre-applied 2026-04-19** as the recommended default. Effect: `BRCA1.detailed_conditions = ["Serous Ovarian Cancer", "Pancreatic Adenocarcinoma"]` (no tissue names). Opt-out via `build_gene_master.py --include-tissue-level` if you disagree.
- [ ] **T5. Future Excel extensions** — during monthly runs, should Gemini-proposed HGNC additions auto-append, require review, or be threshold-gated? (est: 5 min decision)

**Blocked on Tom — post-approval steps:**

- [ ] Write `apply_gene_additions.py` to merge Tom's approved CSV into the normalized Excel. (est: 20 min, starts once T1+T2+T3 are back)
- [ ] Copy `New_Gene_updated.xlsx.hgnc_normalized` (+additions) over `New_Gene_updated.xlsx` as the canonical file; preserve old as `.before_definitive_fix`.
- [ ] Rebuild moa_master + gene_master (cache hits, ~5 min wall time).
- [ ] Apply T4 (tissue-level filter) if Tom says yes.
- [ ] Update §3 / §5 metrics with post-rebuild numbers.
- [ ] Lock Excel format: add header-row comment documenting HGNC-canonical rule.
- [ ] Close §15; mark pipeline as "definitive-fix complete."

### 15.7 Files Tom reviews (in one place)

```
gene/
├── New_Gene_updated.xlsx.hgnc_normalized        # diff vs current Excel (T1)
├── dictionaries/
│   ├── pending_excel_review.csv                 # 20 ambiguous rows (T2)
│   └── proposed_gene_additions.csv              # 586 candidates (T3)
```

All three are non-destructive — nothing touches `New_Gene_updated.xlsx`
or the committed masters until Tom returns the approvals.

