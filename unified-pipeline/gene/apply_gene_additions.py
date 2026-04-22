"""
Apply Tom's approved gene additions to the HGNC-normalized Excel.

Reads:
  - New_Gene_updated.xlsx.hgnc_normalized   (from normalize_excel_hgnc.py)
  - dictionaries/proposed_gene_additions.csv
      (or whatever path Tom returns; use --csv to override)

For each row where the `approved` column is truthy (y/yes/true/1), append
a new gene row to the Excel:

    Gene          = hgnc_symbol
    Gene Aliases  = cleaned sample_tokens (dedup, drop phrase fragments)
    Broad Cancers = top_broad_cancers

Writes:
  - New_Gene_updated.xlsx.definitive
      (non-destructive — doesn't touch the canonical Excel)

After Tom reviews this output, the commit step is:
    cp New_Gene_updated.xlsx                New_Gene_updated.xlsx.before_definitive_fix
    cp New_Gene_updated.xlsx.definitive     New_Gene_updated.xlsx

…and then rebuild both masters.

Run:
    python apply_gene_additions.py
    python apply_gene_additions.py --csv some_other_approved.csv
    python apply_gene_additions.py --dry-run
"""

import argparse
import csv
import os
import re
import sys
from typing import Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import openpyxl
except ImportError as e:
    raise SystemExit(
        "openpyxl required. Install: pip install openpyxl"
    ) from e

DEFAULT_EXCEL = os.path.join(_HERE, "New_Gene_updated.hgnc_normalized.xlsx")
DEFAULT_CSV = os.path.join(_HERE, "dictionaries", "proposed_gene_additions.csv")
DEFAULT_OUT = os.path.join(_HERE, "New_Gene_updated.definitive.xlsx")

APPROVED_TOKENS = {"y", "yes", "true", "1", "t", "ok", "approved"}


def _clean_alias(raw: str) -> str:
    """Strip parentheticals / leading artifacts / trailing prose.
    Returns '' if the cleaned form is too noisy to use as an alias."""
    s = raw.strip()
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)  # strip parens
    s = s.strip(" -.,:;/")
    # Drop if it's very long (a prose fragment, not a real alias) or all
    # lowercase with >= 3 words (likely a description, not a symbol).
    words = s.split()
    if not s or len(s) > 30:
        return ""
    if len(words) >= 3 and s == s.lower():
        return ""
    return s


def _build_aliases(sample_tokens_field: str) -> str:
    """Turn the semicolon-separated sample_tokens from the CSV into a
    comma-separated alias list for the Excel row."""
    tokens = [t.strip() for t in sample_tokens_field.split(";") if t.strip()]
    cleaned: List[str] = []
    seen = set()
    for tok in tokens:
        c = _clean_alias(tok)
        if not c:
            continue
        if c.lower() in seen:
            continue
        seen.add(c.lower())
        cleaned.append(c)
    return ", ".join(cleaned)


def _parse_broad_cancers(field: str) -> str:
    parts = [p.strip() for p in field.split(";") if p.strip()]
    return ", ".join(parts)


def _load_approved(csv_path: str) -> List[Dict[str, str]]:
    approved: List[Dict[str, str]] = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            flag = (row.get("approved") or "").strip().lower()
            if flag not in APPROVED_TOKENS:
                continue
            approved.append(row)
    return approved


def _existing_genes(ws) -> set:
    # Assume column A is Gene (matches normalize_excel_hgnc contract).
    out = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[0]:
            out.add(str(row[0]).strip())
    return out


def apply(excel_in: str, csv_path: str, excel_out: str,
          dry_run: bool = False) -> None:
    if not os.path.exists(excel_in):
        raise SystemExit(
            f"HGNC-normalized Excel not found: {excel_in}\n"
            f"Run normalize_excel_hgnc.py first."
        )
    if not os.path.exists(csv_path):
        raise SystemExit(f"approved CSV not found: {csv_path}")

    approved = _load_approved(csv_path)
    print(f"[apply] approved rows in CSV: {len(approved)}")

    wb = openpyxl.load_workbook(excel_in)
    ws = wb.active
    existing = _existing_genes(ws)

    appended: List[Tuple[str, str, str]] = []  # (gene, aliases, broad)
    skipped_existing: List[str] = []
    skipped_bad: List[str] = []

    for row in approved:
        hgnc = (row.get("hgnc_symbol") or "").strip()
        if not hgnc:
            skipped_bad.append(str(row))
            continue
        if hgnc in existing:
            skipped_existing.append(hgnc)
            continue
        aliases = _build_aliases(row.get("sample_tokens") or "")
        broad = _parse_broad_cancers(row.get("top_broad_cancers") or "")
        # Always include the HGNC symbol itself at the front of aliases
        # for consistency with existing Excel rows.
        alias_list = [hgnc]
        if aliases:
            for a in aliases.split(", "):
                if a and a not in alias_list:
                    alias_list.append(a)
        alias_str = ", ".join(alias_list)
        appended.append((hgnc, alias_str, broad))
        if not dry_run:
            ws.append([hgnc, alias_str, broad])
        existing.add(hgnc)

    print(f"[apply] appended: {len(appended)}")
    if skipped_existing:
        print(f"[apply] skipped (already in Excel): {len(skipped_existing)}")
        print(f"    {', '.join(sorted(skipped_existing)[:10])}"
              + ("..." if len(skipped_existing) > 10 else ""))
    if skipped_bad:
        print(f"[apply] skipped (bad rows): {len(skipped_bad)}")

    if dry_run:
        print("[apply] dry-run — no write")
        print("[apply] preview (first 10):")
        for g, a, b in appended[:10]:
            print(f"    {g} | aliases={a!r} | broad={b!r}")
        return

    wb.save(excel_out)
    print(f"[apply] wrote {excel_out}")
    print(f"\nNext step (manual):")
    print(f"  cp New_Gene_updated.xlsx                "
          f"New_Gene_updated.xlsx.before_definitive_fix")
    print(f"  cp New_Gene_updated.xlsx.definitive     "
          f"New_Gene_updated.xlsx")
    print(f"  python build_moa_master.py")
    print(f"  python build_gene_master.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel-in", default=DEFAULT_EXCEL)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    apply(args.excel_in, args.csv, args.out, dry_run=args.dry_run)
