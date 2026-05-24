"""
Identifies junk records in tbl_newsletter_subscribers and writes them to a CSV.

Current junk criteria:
  - firstname is NULL or empty  AND  lastname is NULL or empty

Output: data/processed/junk_data/junk_data.csv  (overwritten on each run)

Run from the repo root:
    python -m src.identify_junk_records
"""

from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_OUTPUT_PATH = Path(__file__).parent.parent / "data" / "processed" / "junk_data" / "junk_data.csv"

_QUERY = text("""
    SELECT id, firstname, lastname, specialty_ids, profession_parent_id, profession_id
    FROM tbl_newsletter_subscribers
    WHERE (firstname           IS NULL OR TRIM(firstname)           = '')
      AND (lastname            IS NULL OR TRIM(lastname)            = '')
      AND (specialty_ids       IS NULL OR TRIM(specialty_ids)       = '')
      AND (profession_parent_id IS NULL OR profession_parent_id = 0)
      AND (profession_id        IS NULL OR profession_id        = 0)
""")


def fetch_junk_records() -> pd.DataFrame:
    logger.info(
        "Querying tbl_newsletter_subscribers for junk records "
        "(no firstname, no lastname, no specialty_ids, no/zero profession_parent_id, no profession_id)"
    )
    with engine.connect() as conn:
        df = pd.read_sql(_QUERY, conn)
    logger.info("Found %d junk record(s)", len(df))
    return df


def write_csv(df: pd.DataFrame) -> None:
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df["junk_reason"] = (
        "firstname is null/empty; "
        "lastname is null/empty; "
        "specialty_ids is null/empty; "
        "profession_parent_id is null/0; "
        "profession_id is null/empty"
    )
    df.to_csv(_OUTPUT_PATH, index=False)
    logger.info("Wrote %d row(s) to %s", len(df), _OUTPUT_PATH)


def main() -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    df = fetch_junk_records()

    if df.empty:
        logger.info("No junk records found — nothing to write.")
        return

    write_csv(df)


if __name__ == "__main__":
    main()
