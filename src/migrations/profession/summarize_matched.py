"""
Prints the unique canonical professions found in profession_matched.csv.

Run from the repo root:
    python -m src.migrations.profession.summarize_matched
"""

from pathlib import Path

import pandas as pd

_MATCHED_CSV = Path(__file__).parent / "data" / "profession_cleanup" / "profession_matched.csv"


def main() -> None:
    if not _MATCHED_CSV.exists():
        raise SystemExit(f"File not found: {_MATCHED_CSV}. Run migrate_profession.py first.")

    df = pd.read_csv(_MATCHED_CSV)
    summary = (
        df.groupby(["Proposed eMed ID", "Lookup Key Value"])
        .size()
        .reset_index(name="Count")
        .sort_values("Proposed eMed ID")
    )

    print(f"\n{'eMed ID':<12}{'Count':<10}Lookup Key Value")
    print("-" * 65)
    for _, row in summary.iterrows():
        print(f"{int(row['Proposed eMed ID']):<12}{int(row['Count']):<10}{row['Lookup Key Value']}")
    print("-" * 65)
    print(f"{'Total':<12}{df['Proposed eMed ID'].notna().sum():<10}across {len(summary)} unique profession(s)")
    print()


if __name__ == "__main__":
    main()
