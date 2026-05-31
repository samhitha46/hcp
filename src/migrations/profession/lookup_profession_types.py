"""
Fetches the tbl_system_params details for every distinct profession_type_id
found in tbl_newsletter_subscribers.

Writes:
  src/migrations/profession/data/profession_type_lookup.csv

Run from the repo root:
    python -m src.migrations.profession.lookup_profession_types
"""

from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_OUTPUT_CSV = Path(__file__).parent / "data" / "profession_type_lookup.csv"

_DISTINCT_IDS_QUERY = text("""
    SELECT DISTINCT profession_type_id
    FROM tbl_newsletter_subscribers
    WHERE profession_type_id IS NOT NULL
""")

_PARAMS_QUERY = text("""
    SELECT IdSystem_Params, Category, SubCategory, KeyDescription, KeyValue, status
    FROM tbl_system_params
    WHERE IdSystem_Params IN :ids
""")


def main() -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    with engine.connect() as conn:
        id_df = pd.read_sql(_DISTINCT_IDS_QUERY, conn)

    raw_ids = id_df["profession_type_id"].dropna().tolist()
    ids = [int(v) for v in raw_ids if v is not None]
    logger.info("Found %d distinct profession_type_id value(s)", len(ids))

    if not ids:
        logger.warning("No profession_type_id values found — writing empty CSV")
        pd.DataFrame(columns=["IdSystem_Params", "Category", "SubCategory", "KeyDescription", "KeyValue", "status"]).to_csv(_OUTPUT_CSV, index=False)
        return

    with engine.connect() as conn:
        params_df = pd.read_sql(
            text(f"""
                SELECT IdSystem_Params, Category, SubCategory, KeyDescription, KeyValue, status
                FROM tbl_system_params
                WHERE IdSystem_Params IN ({','.join(str(i) for i in ids)})
            """),
            conn,
        )

    logger.info("Retrieved %d row(s) from tbl_system_params", len(params_df))

    _OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    params_df.to_csv(_OUTPUT_CSV, index=False)

    print("\n" + "=" * 55)
    print("  PROFESSION TYPE LOOKUP — SUMMARY")
    print("=" * 55)
    print(f"  Distinct profession_type_id(s) : {len(ids)}")
    print(f"  Rows matched in tbl_system_params : {len(params_df)}")
    print(f"  Output CSV                     : {_OUTPUT_CSV.name}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
