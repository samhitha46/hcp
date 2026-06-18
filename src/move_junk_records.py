"""
Moves junk records from tbl_newsletter_subscribers to tbl_newsletter_junk_data.

Reads IDs from junk_data.csv, inserts each row into the junk table via
INSERT INTO ... SELECT *, then deletes it from the source table.
Validates row-count checksum before committing.

Defaults to DRY RUN — pass --apply to execute real changes.

Run from the repo root:
    python -m src.move_junk_records                     # dry run, all records
    python -m src.move_junk_records --limit 10          # dry run, first 10
    python -m src.move_junk_records --apply --limit 10  # apply first 10
    python -m src.move_junk_records --apply             # apply all
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_JUNK_DIR = Path(__file__).parent.parent / "data" / "processed" / "junk_data"
_JUNK_CSV = _JUNK_DIR / "junk_data.csv"
_MOVED_CSV = _JUNK_DIR / "moved_junk_data.csv"
_SOURCE = "tbl_newsletter_subscribers"
_TARGET = "tbl_newsletter_junk_data"


def _count(conn) -> int:
    return conn.execute(text(f"SELECT COUNT(*) FROM {_SOURCE}")).scalar()


def _max_id(conn) -> int:
    return conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {_SOURCE}")).scalar()


def main(apply: bool, limit: int | None) -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    if not _JUNK_CSV.exists():
        raise SystemExit(
            f"Junk CSV not found at {_JUNK_CSV}. Run identify_junk_records.py first."
        )

    full_df = pd.read_csv(_JUNK_CSV)
    batch_df = full_df.head(limit) if limit is not None else full_df.copy()

    ids = batch_df["id"].tolist()
    if not ids:
        logger.info("No records in CSV to process.")
        return

    logger.info("Mode   : %s", "APPLY" if apply else "DRY RUN")
    logger.info("Records: %d", len(ids))

    if not apply:
        logger.info("[DRY RUN] Would move %d record(s): %s -> %s", len(ids), _SOURCE, _TARGET)
        for rid in ids:
            logger.info(
                "[DRY RUN]   id=%-8s  INSERT INTO %s SELECT * FROM %s WHERE id=%s"
                "  ->  DELETE FROM %s WHERE id=%s",
                rid, _TARGET, _SOURCE, rid, _SOURCE, rid,
            )
        logger.info("[DRY RUN] No changes made. Re-run with --apply to execute.")
        return

    # --- APPLY MODE ---
    with engine.connect() as conn:
        # Pre-flight snapshot (outside transaction so reads are committed state)
        pre_count = _count(conn)
        pre_max_id = _max_id(conn)
        logger.info("Pre-flight  : row_count=%d  max_id=%d", pre_count, pre_max_id)

        moved = 0
        moved_ids: list[int] = []
        try:
            for rid in ids:
                result = conn.execute(
                    text(f"INSERT INTO {_TARGET} SELECT * FROM {_SOURCE} WHERE id = :id"),
                    {"id": rid},
                )
                if result.rowcount == 0:
                    # Row already missing from source — skip delete, carry on
                    logger.warning("id=%s not found in %s — skipping.", rid, _SOURCE)
                    continue

                conn.execute(
                    text(f"DELETE FROM {_SOURCE} WHERE id = :id"),
                    {"id": rid},
                )
                moved_ids.append(rid)
                moved += 1
                logger.debug("Moved id=%s (%d/%d)", rid, moved, len(ids))

            # ---- Checksum validation ----
            post_count = _count(conn)

            # Rows inserted by other sessions while we were working
            concurrent_inserts = conn.execute(
                text(f"SELECT COUNT(*) FROM {_SOURCE} WHERE id > :max_id"),
                {"max_id": pre_max_id},
            ).scalar()

            expected = pre_count - moved + concurrent_inserts

            logger.info(
                "Post-flight : row_count=%d  concurrent_new=%d  expected=%d",
                post_count, concurrent_inserts, expected,
            )

            if post_count != expected:
                conn.rollback()
                logger.error(
                    "CHECKSUM FAILED — expected %d rows but found %d. "
                    "All changes rolled back.",
                    expected, post_count,
                )
                sys.exit(1)

            conn.commit()
            logger.info(
                "CHECKSUM OK — committed. Moved %d record(s) to %s.", moved, _TARGET
            )

            # Re-query after commit — now sees all concurrent inserts that landed during our run
            post_commit_count = _count(conn)
            post_commit_max_id = _max_id(conn)
            concurrent_inserts = post_commit_count - (pre_count - moved)

            # Update CSV files to reflect what was moved
            moved_set = set(moved_ids)
            newly_moved = full_df[full_df["id"].isin(moved_set)]
            remaining = full_df[~full_df["id"].isin(moved_set)]

            # Append to moved_junk_data.csv (write header only on first run)
            write_header = not _MOVED_CSV.exists()
            newly_moved.to_csv(_MOVED_CSV, mode="a", index=False, header=write_header)

            # Overwrite junk_data.csv with remaining records
            remaining.to_csv(_JUNK_CSV, index=False)

            logger.info(
                "CSV update: %d row(s) appended to %s, %d row(s) remaining in %s.",
                len(newly_moved), _MOVED_CSV.name,
                len(remaining), _JUNK_CSV.name,
            )

            print("\n" + "=" * 60)
            print("  MOVE JUNK RECORDS — RUN SUMMARY")
            print("=" * 60)
            print(f"  Pre-run  : count = {pre_count:,}   max_id = {pre_max_id:,}")
            print(f"  Removed  : {moved:,} record(s) moved to {_TARGET}")
            if concurrent_inserts:
                print(f"  New rows : {concurrent_inserts:,} new subscriber(s) inserted during run")
            print(f"  Post-run : count = {post_commit_count:,}   max_id = {post_commit_max_id:,}")
            print(f"  Checksum : OK")
            print("-" * 60)
            print(f"  CSV      : {len(remaining):,} row(s) remaining in {_JUNK_CSV.name}")
            print(f"  CSV      : {len(newly_moved):,} row(s) appended to {_MOVED_CSV.name}")
            print("=" * 60 + "\n")

        except SystemExit:
            raise
        except Exception as exc:
            conn.rollback()
            logger.error(
                "Unexpected error after moving %d/%d record(s): %s — rolled back.",
                moved, len(ids), exc,
            )
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Move junk records from tbl_newsletter_subscribers to "
            "tbl_newsletter_junk_data. Defaults to dry run."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute changes (default: dry run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N records from junk_data.csv",
    )
    args = parser.parse_args()
    main(apply=args.apply, limit=args.limit)
