"""
Analyzes professions from the input CSV against what exists in eMed (tbl_system_params).

Reads:  src/migrations/profession/data/profession.csv
Writes: src/migrations/profession/data/processed_profession.csv

Run from the repo root:
    python -m src.migrations.profession.analyze_profession
"""

from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_DATA_DIR = Path(__file__).parent / "data"
_INPUT_CSV = _DATA_DIR / "profession.csv"
_OUTPUT_CSV = _DATA_DIR / "processed_profession.csv"

_STATUS_MAP = {
    1: "Active",
    2: "Inactive",
    3: "FE User Created",
    4: "Deleted",
    5: "Others",
}

_QUERY = text("""
    SELECT IdSystem_Params, SubCategory, KeyValue, KeyDescription, status
    FROM tbl_system_params
    WHERE Category = 'MedicalResources'
      AND SubCategory IN ('Profession', 'ProfessionType')
""")


def fetch_emed_professions() -> pd.DataFrame:
    logger.info("Querying tbl_system_params for Category='MedicalResources' AND SubCategory IN ('Profession', 'ProfessionType')")
    with engine.connect() as conn:
        df = pd.read_sql(_QUERY, conn)
    logger.info("Found %d profession record(s) in eMed", len(df))
    return df


def load_input_professions() -> list[str]:
    df = pd.read_csv(_INPUT_CSV)
    col = "eMedEvents_Profession_Name"
    names = df[col].dropna().str.strip().replace("", pd.NA).dropna().tolist()
    logger.info("Loaded %d profession name(s) from input CSV", len(names))
    return names


def analyze(input_names: list[str], emed_df: pd.DataFrame) -> pd.DataFrame:
    # Build two lookups (both case-insensitive):
    #   1. keyed by KeyValue alone          e.g. "md"
    #   2. keyed by "KeyDescription (KeyValue)"  e.g. "physician (md)"
    # Sort descending by status so status=1 (Active) rows are processed last
    # and overwrite any lower-priority duplicates for the same key.
    kv_lookup: dict[str, object] = {}
    combined_lookup: dict[str, object] = {}

    for _, row in emed_df.sort_values(["status", "SubCategory"], ascending=[False, False]).iterrows():
        kv = str(row["KeyValue"]).strip()
        desc = str(row["KeyDescription"]).strip() if row["KeyDescription"] else ""
        kv_lookup[kv.lower()] = row
        if desc:
            combined_lookup[f"{desc} ({kv})".lower()] = row

    rows = []
    for name in input_names:
        key = name.strip().lower()
        match = kv_lookup.get(key)
        if match is None:
            match = combined_lookup.get(key)
        if match is not None:
            rows.append({
                "Profession Name":      name,
                "Available in eMed?":   "YES",
                "eMed ID":              match["IdSystem_Params"],
                "eMed Key Value":       match["KeyValue"],
                "eMed Key Description": match["KeyDescription"],
                "eMed Status":          _STATUS_MAP.get(int(match["status"]), f"Unknown ({match['status']})"),
            })
        else:
            rows.append({
                "Profession Name":      name,
                "Available in eMed?":   "NO",
                "eMed ID":              "",
                "eMed Key Value":       "",
                "eMed Key Description": "",
                "eMed Status":          "",
            })

    return pd.DataFrame(rows)


def main() -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    input_names = load_input_professions()
    emed_df = fetch_emed_professions()
    result_df = analyze(input_names, emed_df)

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    result_df.to_csv(_OUTPUT_CSV, index=False)

    found = (result_df["Available in eMed?"] == "YES").sum()
    missing = (result_df["Available in eMed?"] == "NO").sum()

    logger.info("Output written to %s", _OUTPUT_CSV)
    print("\n" + "=" * 55)
    print("  PROFESSION ANALYSIS — SUMMARY")
    print("=" * 55)
    print(f"  Input professions  : {len(input_names)}")
    print(f"  Found in eMed      : {found}")
    print(f"  Not found in eMed  : {missing}")
    print(f"  Output CSV         : {_OUTPUT_CSV.name}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
