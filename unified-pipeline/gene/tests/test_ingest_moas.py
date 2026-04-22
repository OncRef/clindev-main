"""
Integration tests for MoaMaster.ingest_moas() + GeneMaster.refresh_for_genes().

Hermetic: tests never call Gemini (use_gemini=False) and operate on
temporary MoaMaster instances so they don't touch real dictionaries.

Run:
    python -m unittest gene.tests.test_ingest_moas
    # or from within gene/
    python -m unittest tests.test_ingest_moas
"""

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_GENE_DIR = os.path.dirname(_HERE)
sys.path.insert(0, _GENE_DIR)

from moa_master import MoaMaster, ChangeSet  # noqa: E402
from gene_master import GeneMaster  # noqa: E402


class IngestMoasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Shared GeneMaster across tests — loading Excel is the expensive
        # step and Excel is read-only here so sharing is safe.
        cls.gm = GeneMaster().load_from_excel()

    def _fresh_moa(self) -> MoaMaster:
        """Return a MoaMaster pointed at a throwaway path."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        )
        tmp.close()
        return MoaMaster(path=tmp.name)

    # -- core upsert behavior ------------------------------------------------

    def test_new_drug_adds_one_moa(self):
        moa = self._fresh_moa()
        drugs = {"drug-a": {"short_moa": "KRAS G12C inhibitor",
                            "drug_class": "small_molecule_inhibitor"}}
        cs = moa.ingest_moas(drugs, self.gm, use_gemini=False)

        self.assertEqual(len(cs.processed_drugs), 1)
        self.assertEqual(len(cs.added_moa_keys), 1)
        self.assertEqual(len(cs.extended_moa_keys), 0)
        key = next(iter(cs.added_moa_keys))
        self.assertEqual(key, "kras_g12c_inhibitor")
        self.assertIn("KRAS", cs.affected_genes)
        self.assertIn(key, moa.moas)
        self.assertEqual(moa.moas[key]["gene_targets"], ["KRAS"])
        self.assertIn("drug-a", moa.drug_to_moa)

    def test_ingest_is_idempotent(self):
        moa = self._fresh_moa()
        drugs = {"drug-a": {"short_moa": "KRAS G12C inhibitor"}}
        cs1 = moa.ingest_moas(drugs, self.gm, use_gemini=False)
        moas_before = dict(moa.moas)
        drug_to_moa_before = dict(moa.drug_to_moa)

        cs2 = moa.ingest_moas(drugs, self.gm, use_gemini=False)

        # Second pass should extend the same key, not add new or rename.
        self.assertEqual(len(cs2.added_moa_keys), 0)
        self.assertEqual(set(moa.moas.keys()), set(moas_before.keys()))
        self.assertEqual(moa.drug_to_moa, drug_to_moa_before)

    def test_canonical_keys_stable_across_ingests(self):
        moa = self._fresh_moa()
        first = moa.ingest_moas(
            {"d1": {"short_moa": "KRAS G12C inhibitor"},
             "d2": {"short_moa": "EGFR inhibitor"}},
            self.gm, use_gemini=False,
        )
        keys_first = set(moa.moas.keys())
        self.assertIn("kras_g12c_inhibitor", keys_first)
        self.assertIn("egfr_inhibitor", keys_first)

        # Add new drugs with both new and existing MOAs.
        second = moa.ingest_moas(
            {"d3": {"short_moa": "BRAF V600E inhibitor"},
             "d4": {"short_moa": "EGFR inhibitor"}},  # already exists
            self.gm, use_gemini=False,
        )
        self.assertIn("kras_g12c_inhibitor", moa.moas)
        self.assertIn("egfr_inhibitor", moa.moas)
        self.assertIn("egfr_inhibitor", second.extended_moa_keys)
        self.assertIn("braf_v600e_inhibitor", second.added_moa_keys)

    def test_compound_string_splits_into_multiple_entries(self):
        moa = self._fresh_moa()
        drugs = {"combo": {
            "short_moa": "PARP inhibitor, PD-1 inhibitor, alkylating agent"
        }}
        cs = moa.ingest_moas(drugs, self.gm, use_gemini=False)
        self.assertGreaterEqual(len(cs.added_moa_keys), 3)
        self.assertIn("pd1_inhibitor", moa.moas)
        # Drug should be linked to every fragment.
        linked_keys = set(moa.drug_to_moa["combo"])
        self.assertGreaterEqual(len(linked_keys), 3)

    # -- ChangeSet structure -------------------------------------------------

    def test_changeset_is_dataclass(self):
        cs = ChangeSet()
        self.assertEqual(cs.processed_drugs, [])
        self.assertEqual(cs.added_moa_keys, set())
        d = cs.to_dict()
        self.assertIn("added_moa_keys", d)
        self.assertIn("extended_moa_keys", d)
        self.assertIn("affected_genes", d)
        self.assertIn("new_unresolved_targets", d)

    def test_affected_genes_matches_gene_targets(self):
        moa = self._fresh_moa()
        cs = moa.ingest_moas(
            {"d1": {"short_moa": "KRAS G12C inhibitor"},
             "d2": {"short_moa": "BRAF V600E inhibitor"}},
            self.gm, use_gemini=False,
        )
        # Every gene reported in affected_genes should appear as a gene
        # target in some MOA we just touched.
        gene_targets_union = set()
        for mk in (cs.added_moa_keys | cs.extended_moa_keys):
            gene_targets_union.update(moa.moas[mk].get("gene_targets") or [])
        self.assertEqual(cs.affected_genes, gene_targets_union)

    def test_unresolved_target_populates_audit_field(self):
        # "Stargazin" isn't in Excel → stays unresolved.
        moa = self._fresh_moa()
        cs = moa.ingest_moas(
            {"exotic": {"short_moa": "Stargazin inhibitor"}},
            self.gm, use_gemini=False,
        )
        self.assertTrue(cs.new_unresolved_targets)
        # MOA should still be created with is_class_only as parsed (not
        # silently flipped to class-only because gene didn't resolve).
        some_key = next(iter(cs.added_moa_keys))
        entry = moa.moas[some_key]
        self.assertTrue(entry["unresolved_targets"])
        self.assertFalse(entry["is_class_only"])

    # -- refresh_for_genes ---------------------------------------------------

    def test_refresh_for_genes_only_touches_affected(self):
        moa = self._fresh_moa()
        moa.ingest_moas(
            {"d1": {"short_moa": "KRAS G12C inhibitor"}},
            self.gm, use_gemini=False,
        )
        # Fresh GeneMaster so moas_targeting starts empty.
        gm2 = GeneMaster().load_from_excel()
        n = gm2.refresh_for_genes({"KRAS"}, moa)
        self.assertEqual(n, 1)
        self.assertIn("kras_g12c_inhibitor", gm2.genes["KRAS"]["moas_targeting"])
        # Other genes should still have empty moas_targeting.
        self.assertEqual(gm2.genes["EGFR"]["moas_targeting"], [])

    def test_refresh_ignores_unknown_gene(self):
        moa = self._fresh_moa()
        gm2 = GeneMaster().load_from_excel()
        # Unknown gene name should be silently skipped, not error.
        n = gm2.refresh_for_genes({"NOT_A_REAL_GENE_XYZ"}, moa)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
