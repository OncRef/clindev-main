"""
Gene Master — loads New_Gene_updated.xlsx and provides:

- The canonical gene universe (461 rows).
- The alias → canonical gene lookup used by the Stage 3 gene linker.
- An in-memory dict for gene_master.json (enriched later with Gemini).

Shape of gene_master.json:
    {
        "metadata": {...},
        "genes": { "<GENE>": { ...entry... } },
        "alias_index": { "<alias_lower>": "<GENE>" }  # derived, included for convenience
    }
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Set

try:
    import openpyxl
except ImportError as e:
    raise SystemExit(
        "openpyxl is required. Install with: pip install openpyxl"
    ) from e

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXCEL = os.path.join(_HERE, "New_Gene_updated.xlsx")
DEFAULT_PATH = os.path.join(_HERE, "dictionaries", "gene_master.json")
SUPPLEMENT_PATH = os.path.join(_HERE, "dictionaries", "moa_target_supplement.json")


# Seed supplement for common MOA-string tokens the Excel doesn't cover.
# Only genes that are already present in the Excel get gene_targets —
# anything else is dropped. This map bridges common names → excel gene.
DEFAULT_SUPPLEMENT = {
    "pd-1": "PDCD1",
    "pd1": "PDCD1",
    "pd-l1": "CD274",
    "pdl1": "CD274",
    "ctla-4": "CTLA4",
    "ctla4": "CTLA4",
    "her2": "ERBB2",
    "her3": "ERBB3",
    "her4": "ERBB4",
    "vegf": "VEGFA",
    "vegfr": "KDR",
    "vegfr2": "KDR",
    "vegfr-2": "KDR",
    "androgen receptor": "AR",
    "estrogen receptor": "ESR1",
    "ers": "ESR1",
    "er": "ESR1",
    "progesterone receptor": "PGR",
    "gnrh receptor": "GNRHR",
    "glucocorticoid receptor": "NR3C1",
    "aromatase": "CYP19A1",
    "bcr-abl": "ABL1",
    "bcr/abl": "ABL1",
    "ph+": "ABL1",
}


class GeneMaster:
    def __init__(self, excel_path: Optional[str] = None,
                 path: Optional[str] = None):
        self.excel_path = excel_path or DEFAULT_EXCEL
        self.path = path or DEFAULT_PATH
        self.genes: Dict[str, dict] = {}          # canonical_gene -> entry
        self.alias_index: Dict[str, str] = {}     # alias_lower -> canonical_gene
        self.metadata: Dict = {}

    # -- seed from Excel --------------------------------------------------

    def load_from_excel(self) -> "GeneMaster":
        """Load New_Gene_updated.xlsx and populate self.genes + alias_index."""
        if not os.path.exists(self.excel_path):
            raise FileNotFoundError(f"Excel not found: {self.excel_path}")

        wb = openpyxl.load_workbook(self.excel_path, data_only=True)
        ws = wb.active
        header = None
        for row in ws.iter_rows(values_only=True):
            if header is None:
                header = [str(c).strip() if c is not None else "" for c in row]
                continue
            if not row or not row[0]:
                continue
            gene = str(row[0]).strip()
            aliases_raw = str(row[1] or "").strip()
            broad_raw = str(row[2] or "").strip()

            aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
            if gene not in aliases:
                aliases.insert(0, gene)
            broad_cancers = [b.strip() for b in broad_raw.split(",") if b.strip()]

            self.genes[gene] = {
                "gene": gene,
                "aliases": aliases,
                "broad_cancers": broad_cancers,
                "detailed_conditions": [],
                "mutation_hotspots": [],
                "pathway": [],
                "role": None,
                "notes": "",
                "moas_targeting": [],
                "example_drugs": [],
                "source": {
                    "aliases": "excel",
                    "broad_cancers": "excel",
                    "detailed_conditions": None,
                    "mutation_hotspots": None,
                    "pathway": None,
                    "moas_targeting": None,
                },
            }

            for a in aliases:
                self.alias_index[a.lower()] = gene

        # Supplement map — only entries whose target gene exists in Excel.
        self._load_supplement()
        return self

    def _load_supplement(self) -> None:
        """Load moa_target_supplement.json (or the default map)."""
        mapping = dict(DEFAULT_SUPPLEMENT)
        if os.path.exists(SUPPLEMENT_PATH):
            try:
                with open(SUPPLEMENT_PATH) as f:
                    extra = json.load(f)
                if isinstance(extra, dict):
                    mapping.update({k.lower(): v for k, v in extra.items()})
            except Exception:
                pass
        for alias_lower, gene in mapping.items():
            if gene in self.genes and alias_lower not in self.alias_index:
                self.alias_index[alias_lower] = gene

    # -- lookup for the gene linker ---------------------------------------

    def resolve_target(self, raw_token: str) -> Optional[str]:
        """Return the canonical gene for a parsed target token, or None."""
        if not raw_token:
            return None
        t = raw_token.strip().lower()
        if not t:
            return None
        if t in self.alias_index:
            return self.alias_index[t]
        # Retry without hyphens / spaces.
        stripped = t.replace("-", "").replace(" ", "")
        for k, g in self.alias_index.items():
            if k.replace("-", "").replace(" ", "") == stripped:
                return g
        return None

    # -- load / save gene_master.json -------------------------------------

    def load(self) -> "GeneMaster":
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
            self.genes = data.get("genes", {})
            self.alias_index = data.get("alias_index", {})
            self.metadata = data.get("metadata", {})
        return self

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "metadata": {
                **self.metadata,
                "timestamp": datetime.now().isoformat(),
                "total_genes": len(self.genes),
                "source_excel": os.path.basename(self.excel_path),
            },
            "genes": self.genes,
            "alias_index": self.alias_index,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    # -- enrichment API ---------------------------------------------------

    def apply_gemini_enrichment(self, gene: str, resp: dict) -> None:
        entry = self.genes.get(gene)
        if not entry or not isinstance(resp, dict):
            return

        for field in ("detailed_conditions", "mutation_hotspots", "pathway"):
            val = resp.get(field)
            if isinstance(val, list):
                entry[field] = [str(x) for x in val if x]
                entry["source"][field] = "gemini"

        for field in ("role", "notes"):
            val = resp.get(field)
            if val:
                entry[field] = val

    def merge_existing_enrichment(self, existing_path: Optional[str] = None) -> int:
        """Merge Gemini-enriched fields from an existing gene_master.json
        back onto the freshly-loaded-from-Excel genes dict.

        Preserves: detailed_conditions, detailed_conditions_dropped,
        mutation_hotspots, pathway, role, notes, source{}. Leaves
        aliases/broad_cancers/moas_targeting/example_drugs alone since
        those are recomputed from Excel + moa_master.

        Returns the number of genes whose enrichment was merged back.
        """
        path = existing_path or self.path
        if not os.path.exists(path):
            return 0
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            return 0
        prior_genes = data.get("genes") or {}
        merged = 0
        for gene_key, prior in prior_genes.items():
            if gene_key not in self.genes:
                continue
            entry = self.genes[gene_key]
            touched = False
            for field in (
                "detailed_conditions", "detailed_conditions_dropped",
                "mutation_hotspots", "pathway", "role", "notes",
            ):
                val = prior.get(field)
                if val:
                    entry[field] = val
                    touched = True
            # Preserve per-field source audit
            prior_source = prior.get("source") or {}
            if prior_source:
                entry_source = entry.setdefault("source", {})
                for k, v in prior_source.items():
                    if v and not entry_source.get(k):
                        entry_source[k] = v
            if touched:
                merged += 1
        return merged

    def apply_moa_master(self, moa_master) -> None:
        """Fill `moas_targeting` and `example_drugs` for ALL genes. Used on
        full rebuild. For monthly/incremental, prefer refresh_for_genes()
        which only touches the affected subset."""
        for gene, entry in self.genes.items():
            mks = moa_master.moas_for_gene(gene)
            entry["moas_targeting"] = mks
            entry["source"]["moas_targeting"] = "derived_from_moa_master"
            drugs: Set[str] = set()
            for mk in mks:
                drugs.update(moa_master.moas[mk].get("example_drugs", []))
            entry["example_drugs"] = sorted(drugs)[:25]

    def refresh_for_genes(self, affected: Set[str], moa_master) -> int:
        """Recompute moas_targeting + example_drugs for a subset of genes
        only. Intended for incremental/monthly runs where a ChangeSet
        reports which genes could have changed coverage. Returns count
        of genes whose entries were updated. Unknown gene symbols are
        ignored (logged by caller if they care)."""
        updated = 0
        for gene in affected:
            entry = self.genes.get(gene)
            if entry is None:
                continue
            mks = moa_master.moas_for_gene(gene)
            entry["moas_targeting"] = mks
            entry["source"]["moas_targeting"] = "derived_from_moa_master"
            drugs: Set[str] = set()
            for mk in mks:
                drugs.update(moa_master.moas[mk].get("example_drugs", []))
            entry["example_drugs"] = sorted(drugs)[:25]
            updated += 1
        return updated
