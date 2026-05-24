"""
One-time script: creates tbl_newsletter_junk_data with the same structure
as tbl_newsletter_subscribers (no rows loaded).

Run from the repo root:
    python -m one_time_scripts.create_junk_data_table
"""

from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_TARGET_TABLE = "tbl_newsletter_junk_data"
_SOURCE_TABLE = "tbl_newsletter_subscribers"


def main() -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    with engine.begin() as conn:
        conn.execute(
            text(f"CREATE TABLE IF NOT EXISTS {_TARGET_TABLE} LIKE {_SOURCE_TABLE}")
        )

    logger.info("Table '%s' created successfully (mirroring '%s').", _TARGET_TABLE, _SOURCE_TABLE)


if __name__ == "__main__":
    main()
