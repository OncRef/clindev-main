"""
Smoke-test the three corruption patches without touching the live pipeline:

  1. drug_master.add_or_update refuses placeholder generic_name
  2. drug_master.add_or_update refuses to merge into a bucket-shaped existing entry
  3. enricher._is_template_echo flags a Gemini response with all-placeholder fields
  4. classifier title_oncotree no longer tags benign-only titles
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drugs.drug_master import DrugMaster, _is_placeholder
from drugs.enricher import _is_template_echo
from conditions.classifier import classify_from_title, _is_clearly_benign


def test_drug_master_rejects_placeholder():
    dm = DrugMaster.__new__(DrugMaster)
    dm.path = "/tmp/_unused.json"
    dm.drugs = {}
    dm._index = {}
    dm._dirty = False

    bad_enrichment = {
        "drug_name": "alpha blocker",
        "generic_name": "...",
        "short_moa": "...",
        "long_moa": "...",
        "drug_class": "...",
        "company": "...",
        "brand_names": ["AS01", "Abecma"],
        "is_drug": True,
    }
    action, reason = dm.add_or_update("alpha blocker", bad_enrichment)
    assert action == "skipped" and "placeholder" in reason, (action, reason)
    assert "..." not in dm.drugs, "placeholder generic_name leaked into drugs dict"
    assert not any(_is_placeholder(k) for k in dm._index), "placeholder leaked into index"
    print("PASS  drug_master rejects placeholder generic_name")


def test_drug_master_refuses_bucket_merge():
    dm = DrugMaster.__new__(DrugMaster)
    dm.path = "/tmp/_unused.json"
    dm.drugs = {
        "trastuzumab-qyyp": {
            "generic_name": "trastuzumab-qyyp",
            "brand_names": [f"brand_{i}" for i in range(120)],   # bucket-shaped
            "other_names": [f"alt_{i}" for i in range(2000)],
        },
    }
    dm._index = {}
    dm._rebuild_index()

    incoming = {
        "drug_name": "some-other-drug",
        "generic_name": "trastuzumab-qyyp",
        "short_moa": "fake MOA",
        "brand_names": ["new-brand"],
        "is_drug": True,
    }
    action, reason = dm.add_or_update("some-other-drug", incoming)
    assert action == "skipped" and "bucket" in reason, (action, reason)
    print("PASS  drug_master refuses to merge into bucket-shaped existing entry")


def test_enricher_detects_template_echo():
    echoed = {
        "drug_name": "alpha blocker",
        "generic_name": "...",
        "short_moa": "...",
        "long_moa": "...",
        "target": "...",
        "drug_class": "...",
        "company": "...",
        "brand_names": [],
        "is_drug": True,
    }
    real = {
        "drug_name": "osimertinib",
        "generic_name": "osimertinib",
        "short_moa": "EGFR inhibitor",
        "drug_class": "targeted_therapy",
        "is_drug": True,
    }
    assert _is_template_echo(echoed), "should detect echoed prompt template"
    assert not _is_template_echo(real), "should not flag a real response"
    print("PASS  enricher detects placeholder template echo")


def test_classifier_rejects_benign_title():
    minimal_cfg = {"oncotree_broad": {"prostate adenocarcinoma": {"code": "PRAD", "broad_cancer": "Prostate Cancer"},
                                       "prostate":               {"code": "PROS", "broad_cancer": "Prostate Cancer"},
                                       "lung":                   {"code": "LUNG", "broad_cancer": "Lung Cancers"}},
                   "broad_cancer_kw": []}
    bph_title = ("Water Vapor Thermotherapy vs. Combination Pharmacotherapy for "
                 "Symptomatic Benign Prostatic Hyperplasia Refractory to Alpha "
                 "Blocker Monotherapy in Sexually Active Men")
    cancer_title = "A Phase 3 Trial of Osimertinib in Non-Small Cell Lung Cancer"

    assert _is_clearly_benign(bph_title.lower()), "BPH title should be clearly benign"
    assert classify_from_title(bph_title, minimal_cfg) is None, "BPH title must not classify"
    out = classify_from_title(cancer_title, minimal_cfg)
    assert out and out[1] == "title_oncotree", f"NSCLC title must still classify, got {out}"
    print("PASS  classifier rejects benign title, still accepts cancer title")


if __name__ == "__main__":
    test_drug_master_rejects_placeholder()
    test_drug_master_refuses_bucket_merge()
    test_enricher_detects_template_echo()
    test_classifier_rejects_benign_title()
    print()
    print("All 4 smoke tests passed.")
