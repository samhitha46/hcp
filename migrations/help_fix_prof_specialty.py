"""
help_fix_prof_specialty.py

Interactive helper for building specialty_id_overrides.csv.

For each specialty ID you enter it:
  1. Looks up override_proposed_list.csv (column 1 = input ID, column 3 = proposed override ID)
  2. Queries tbl_master_specialities for both IDs plus any non-zero parents
  3. Displays the results for review
  4. On confirmation, appends a row to specialty_id_overrides.csv

Run from the repo root:
    python migrations/help_fix_prof_specialty.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_INPUT_DIR          = Path(__file__).parent.parent / "data" / "input"
_PROPOSED_LIST_CSV  = _INPUT_DIR / "override_proposed_list.csv"
_OVERRIDES_CSV      = _INPUT_DIR / "specialty_id_overrides.csv"
_SPECIALTIES_TABLE  = "tbl_master_specialities"
_OVERRIDES_COLUMNS  = ["original_id", "mapped_id", "parent_id", "notes"]


def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_proposed_list() -> pd.DataFrame:
    if not _PROPOSED_LIST_CSV.exists():
        raise FileNotFoundError(
            f"Proposed list not found: {_PROPOSED_LIST_CSV}\n"
            "Create data/input/override_proposed_list.csv with at least 3 columns:\n"
            "  column 1 = original DB specialty ID\n"
            "  column 3 = proposed eMed override ID"
        )
    # Read without assuming a header so positional access always works.
    # Non-numeric rows (e.g. a header row) are naturally skipped by _to_int().
    df = pd.read_csv(_PROPOSED_LIST_CSV, dtype=str, header=None)
    logger.info("Loaded %d row(s) from %s", len(df), _PROPOSED_LIST_CSV.name)
    return df


def lookup_proposed(df: pd.DataFrame, input_id: int):
    """Return the proposed override ID from column 3 (index 2), or None."""
    matches = df[df.iloc[:, 0].apply(_to_int) == input_id]
    if matches.empty:
        return None
    return _to_int(matches.iloc[0, 2])


def already_in_overrides(input_id: int) -> bool:
    if not _OVERRIDES_CSV.exists():
        return False
    try:
        df = pd.read_csv(_OVERRIDES_CSV, dtype=str)
        df.columns = df.columns.str.strip()
        return input_id in df["original_id"].dropna().apply(_to_int).tolist()
    except Exception:
        return False


def append_override(original_id: int, mapped_id: int, parent_id: int | None, notes: str) -> None:
    record = {
        "original_id": str(original_id),
        "mapped_id":   str(mapped_id),
        "parent_id":   str(parent_id) if parent_id else "",
        "notes":       notes,
    }
    df = pd.DataFrame([record], columns=_OVERRIDES_COLUMNS)
    write_header = not _OVERRIDES_CSV.exists()
    df.to_csv(_OVERRIDES_CSV, mode="a", header=write_header, index=False)
    logger.info("Override written: %d → %d (parent=%s)", original_id, mapped_id, parent_id)


# ── Database helpers ──────────────────────────────────────────────────────────

def fetch_specialties(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    id_list = ", ".join(str(i) for i in ids)
    query = text(
        f"SELECT id, name, parent_id FROM `{_SPECIALTIES_TABLE}` WHERE id IN ({id_list})"
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return {
        int(row["id"]): {
            "name":      str(row["name"] or "").strip(),
            "parent_id": int(row["parent_id"]) if pd.notna(row["parent_id"]) else 0,
        }
        for _, row in df.iterrows()
    }


# ── Display ───────────────────────────────────────────────────────────────────

def _show_row(label: str, sid: int, info: dict, parent_info: dict | None) -> None:
    pid = info.get("parent_id", 0)
    if pid and parent_info:
        parent_str = f"{pid} — {parent_info['name']}"
    elif pid:
        parent_str = str(pid)
    else:
        parent_str = "(top-level specialty)"

    print(f"  [{label}]")
    print(f"    ID        : {sid}")
    print(f"    Name      : {info['name']}")
    print(f"    Parent    : {parent_str}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    proposed_df = load_proposed_list()

    W = 62
    print(f"\n{'═' * W}")
    print("  Specialty Override Helper")
    print(f"  Proposed list : {_PROPOSED_LIST_CSV.name}  ({len(proposed_df)} rows)")
    print(f"  Overrides CSV : {_OVERRIDES_CSV.name}")
    print(f"{'═' * W}")
    print("  Enter a specialty ID to look up, or N / No to quit.\n")

    while True:
        try:
            raw = input("  Specialty ID: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if raw.lower() in ("n", "no"):
            print("\n  Done. Goodbye!")
            break

        input_id = _to_int(raw)
        if input_id is None:
            print("  Invalid input — please enter a numeric ID.\n")
            continue

        # ── Step 1: proposed list lookup ──────────────────────────────────────
        proposed_id = lookup_proposed(proposed_df, input_id)
        if proposed_id is None:
            print(f"\n  ID {input_id} not found in {_PROPOSED_LIST_CSV.name}.\n")
            continue

        # ── Step 2: DB lookup ─────────────────────────────────────────────────
        rows = fetch_specialties([input_id, proposed_id])

        input_info    = rows.get(input_id)
        proposed_info = rows.get(proposed_id)

        if not input_info:
            print(f"\n  ID {input_id} not found in {_SPECIALTIES_TABLE}.\n")
            continue
        if not proposed_info:
            print(f"\n  Proposed override ID {proposed_id} not found in {_SPECIALTIES_TABLE}.\n")
            continue

        # Collect non-zero parent IDs and fetch them
        parent_ids = {
            pid
            for pid in [input_info.get("parent_id"), proposed_info.get("parent_id")]
            if pid and pid != 0
        }
        parent_rows = fetch_specialties(list(parent_ids)) if parent_ids else {}

        # ── Step 3: display ───────────────────────────────────────────────────
        print(f"\n  {'─' * W}")
        _show_row("INPUT", input_id, input_info, parent_rows.get(input_info["parent_id"]))
        print(f"  {'─' * W}")
        _show_row("PROPOSED OVERRIDE", proposed_id, proposed_info, parent_rows.get(proposed_info["parent_id"]))
        print(f"  {'─' * W}")

        if already_in_overrides(input_id):
            print(f"  WARNING: ID {input_id} already has an entry in {_OVERRIDES_CSV.name}.")

        # ── Confirm ───────────────────────────────────────────────────────────
        try:
            answer = input("\n  Write this override? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if answer in ("", "y", "yes"):
            override_parent = proposed_info.get("parent_id") or None
            if override_parent == 0:
                override_parent = None

            parent_info = parent_rows.get(override_parent) if override_parent else None
            notes = (
                f"{input_info['name']} → {proposed_info['name']}"
                + (f" (sub of {parent_info['name']} {override_parent})" if parent_info else "")
            )
            append_override(input_id, proposed_id, override_parent, notes)
            print(f"  Written: {input_id},{proposed_id},{override_parent or ''},{notes}\n")
        else:
            print("  Skipped.\n")


if __name__ == "__main__":
    main()
