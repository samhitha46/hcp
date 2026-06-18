"""
Orchestrator for move_junk_records — splits work into batches of 1000
to avoid DB spikes.

Usage:
    python src/run_junk_migration.py --limit 10000           # dry run, 10 batches
    python src/run_junk_migration.py --limit 10000 --apply   # apply, 10 batches
    python src/run_junk_migration.py --limit 500  --apply    # apply, single batch

If --limit is omitted all records in junk_data.csv are processed.
Batches <= 1000 records run directly without orchestration.

Between apply batches a 2-second pause is inserted so the DB can breathe.
"""

import argparse
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from src.db import check_connection
from src.logger import get_logger
import src.move_junk_records as mover

logger = get_logger(__name__)

BATCH_SIZE    = 1000
BATCH_PAUSE_S = 2       # seconds to sleep between apply batches
_JUNK_CSV     = mover._JUNK_CSV


# ── Progress display ──────────────────────────────────────────────────────────

def _bar(done: int, total: int, width: int = 38) -> str:
    pct   = done / total if total else 1.0
    filled = int(width * pct)
    return "[" + "#" * filled + "-" * (width - filled) + f"]  {pct:>4.0%}"


def _print_batch_header(batch_num, total_batches, start_rec, end_rec, total_records, apply):
    mode = "APPLY" if apply else "DRY RUN"
    print()
    print("=" * 65)
    print(f"  Batch {batch_num}/{total_batches}  |  Records {start_rec:,}-{end_rec:,} of {total_records:,}  |  {mode}")
    print(f"  Progress: {_bar(batch_num - 1, total_batches)}")
    print("=" * 65)


def _print_batch_footer(batch_num, total_batches, elapsed_s):
    print(f"\n  Batch {batch_num}/{total_batches} done  ({elapsed_s:.1f}s)")
    print(f"  Progress: {_bar(batch_num, total_batches)}")


def _print_summary(total_batches, total_records, total_elapsed, apply):
    print()
    print("=" * 65)
    print("  MIGRATION COMPLETE" if apply else "  DRY RUN COMPLETE")
    print("=" * 65)
    print(f"  Batches processed : {total_batches:,}")
    print(f"  Records targeted  : {total_records:,}")
    print(f"  Total elapsed     : {total_elapsed:.1f}s")
    if apply:
        print(f"  See {_JUNK_CSV.parent / 'moved_junk_data.csv'} for the full moved log.")
    print("=" * 65)
    print()


# ── Orchestration ─────────────────────────────────────────────────────────────

def main(apply: bool, limit: int | None) -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database -- aborting.")

    if not _JUNK_CSV.exists():
        raise SystemExit(
            f"Junk CSV not found at {_JUNK_CSV}.\n"
            "Run identify_junk_records.py first."
        )

    total_available = len(pd.read_csv(_JUNK_CSV))
    total_records   = min(limit, total_available) if limit is not None else total_available

    if total_records == 0:
        print("No records to process in junk_data.csv.")
        return

    # Single batch path — no overhead
    if total_records <= BATCH_SIZE:
        print(
            f"\nLimit ({total_records:,}) fits in one batch "
            f"(threshold {BATCH_SIZE:,}) -- running directly.\n"
        )
        mover.main(apply=apply, limit=total_records)
        return

    # Multi-batch path
    total_batches = math.ceil(total_records / BATCH_SIZE)
    print(
        f"\nOrchestrating {total_records:,} records across "
        f"{total_batches} batch(es) of up to {BATCH_SIZE:,}."
    )
    if apply:
        print(f"A {BATCH_PAUSE_S}s pause will be inserted between batches.")
    else:
        print(
            "DRY RUN: no changes will be made. "
            "Each batch preview shows the first N rows of the unchanged CSV."
        )

    overall_start   = time.time()
    records_done    = 0

    for batch_num in range(1, total_batches + 1):
        remaining   = total_records - records_done
        batch_limit = min(BATCH_SIZE, remaining)
        start_rec   = records_done + 1
        end_rec     = records_done + batch_limit

        _print_batch_header(batch_num, total_batches, start_rec, end_rec, total_records, apply)

        batch_start = time.time()
        mover.main(apply=apply, limit=batch_limit)
        batch_elapsed = time.time() - batch_start

        records_done += batch_limit
        _print_batch_footer(batch_num, total_batches, batch_elapsed)

        # Pause between apply batches to reduce DB load
        if apply and batch_num < total_batches:
            print(f"\n  Pausing {BATCH_PAUSE_S}s before next batch ...", flush=True)
            time.sleep(BATCH_PAUSE_S)

    _print_summary(total_batches, records_done, time.time() - overall_start, apply)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Orchestrate junk-record migration in batches of 1,000 "
            "to avoid DB spikes. Defaults to dry run."
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
        help="Total number of records to process (default: all in junk_data.csv)",
    )
    args = parser.parse_args()
    main(apply=args.apply, limit=args.limit)
