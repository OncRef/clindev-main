"""
Clean `dictionaries/moa_target_supplement.json`.

Drops:
  - null / empty values (can't resolve anywhere useful)
  - keys starting with "_" (artifact comments like "_comment")
  - keys containing ";" or unbalanced parens (malformed aliases that
    never match anything)
  - keys starting with a digit (noise tokens like "2 (map2k1")

Keeps: the hand-curated non-null aliases + the Gemini-expanded valid
token → HGNC mappings.

Writes the cleaned file in place; backs up the original to
`moa_target_supplement.json.before_cleanup`.

Run:
    python clean_supplement.py
    python clean_supplement.py --dry-run
"""

import argparse
import json
import os
import shutil
import sys
from typing import Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
SUPPLEMENT = os.path.join(_HERE, "dictionaries", "moa_target_supplement.json")


def _is_bad_key(k: str) -> Tuple[bool, str]:
    if not isinstance(k, str):
        return True, "not-a-string"
    if k.startswith("_"):
        return True, "artifact (_prefix)"
    if not k.strip():
        return True, "empty"
    if k and k[0].isdigit():
        return True, "starts with digit"
    if ";" in k:
        return True, "contains ';'"
    # Unbalanced parens
    if k.count("(") != k.count(")"):
        return True, "unbalanced parens"
    return False, ""


def clean(dry_run: bool = False) -> None:
    if not os.path.exists(SUPPLEMENT):
        raise SystemExit(f"not found: {SUPPLEMENT}")
    with open(SUPPLEMENT) as f:
        data = json.load(f)
    print(f"[clean] loaded {len(data)} entries")

    kept: Dict[str, str] = {}
    dropped: List[Tuple[str, str]] = []
    for k, v in data.items():
        bad, reason = _is_bad_key(k)
        if bad:
            dropped.append((k, reason))
            continue
        if v is None or (isinstance(v, str) and not v.strip()):
            dropped.append((k, "null/empty value"))
            continue
        if not isinstance(v, str):
            dropped.append((k, f"non-string value: {type(v).__name__}"))
            continue
        kept[k] = v

    print(f"[clean] kept: {len(kept)} | dropped: {len(dropped)}")
    if dropped:
        print("  dropped samples:")
        by_reason: Dict[str, List[str]] = {}
        for k, r in dropped:
            by_reason.setdefault(r, []).append(k)
        for reason, keys in by_reason.items():
            shown = ", ".join(repr(k) for k in keys[:5])
            tail = f" ... (+{len(keys)-5})" if len(keys) > 5 else ""
            print(f"    {reason} ({len(keys)}): {shown}{tail}")

    if dry_run:
        print("[clean] dry-run — no write")
        return

    backup = SUPPLEMENT + ".before_cleanup"
    shutil.copy2(SUPPLEMENT, backup)
    print(f"[clean] backup: {backup}")

    # Restore the header comment as a first-line dict key so downstream
    # readers keep an audit note; GeneMaster._load_supplement ignores
    # underscore-prefixed keys when building alias_index.
    cleaned = {
        "_comment": (
            "Hand-curated + Gemini-expanded alias → gene map. Only "
            "entries whose target gene exists in New_Gene_updated.xlsx "
            "are loaded at runtime. Cleaned by clean_supplement.py "
            "(drops nulls, malformed keys, artifacts)."
        ),
        **dict(sorted(kept.items())),
    }
    with open(SUPPLEMENT, "w") as f:
        json.dump(cleaned, f, indent=2)
    print(f"[clean] wrote {SUPPLEMENT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    clean(dry_run=args.dry_run)
