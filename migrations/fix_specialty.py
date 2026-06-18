"""
fix_specialty.py

Validates and optionally fixes profession data for newsletter subscribers
whose profession_parent_id matches --profession.

Reads physician_md_do.csv from ./data/input to build a map of known physician
specialty IDs and sub-specialty IDs, then for each subscriber checks whether
their specialty_ids column is consistent with that map.

Default mode: dry-run (no DB changes). Use --apply to commit updates.

Already-analyzed subscriber IDs are tracked in fix_specialty_log.csv and
skipped on subsequent runs — new results are appended to the same file.

Run from the repo root:
    python migrations/fix_specialty.py --profession 1
    python migrations/fix_specialty.py --profession 1 --limit 100
    python migrations/fix_specialty.py --profession 1 --limit 100 --apply
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_CANONICAL_PHYSICIAN_ID = 167
_MD_PROFESSION_TYPE_ID  = 184
_DO_PROFESSION_TYPE_ID  = 185

# user_types excluded from processing (admin/internal accounts)
_EXCLUDED_USER_TYPES = (1, 2, 5, 7, 8, 10)

_INPUT_CSV = Path(__file__).parent.parent / "data" / "input"  / "physician_md_do.csv"
_LOG_PATH  = Path(__file__).parent.parent / "data" / "processed" / "physician" / "fix_specialty_log.csv"

_LOG_COLUMNS = [
    "row_type",                      # SUMMARY | DETAIL
    "id",
    # ── SUMMARY-only columns (blank in DETAIL rows) ───────────────────────────
    "analyzed_at",
    "current_profession_parent_id",
    "current_profession_id",
    "current_specialty_ids",
    "match_type",                    # full_match | partial_match | no_match | no_specialty_data
    "intended_profession_parent_id",
    "intended_profession_id",
    "status",                        # dry_run | applied
    "notes",
    # ── DETAIL-only columns (blank in SUMMARY rows) ───────────────────────────
    "specialty_id",
    "db_name",                       # name from tbl_master_specialities
    "db_type",                       # specialty (parent_id=0) | subspecialty | not_in_db
    "map_match",                     # matched | unmatched
    "map_name",                      # name from physician_md_do.csv (blank if unmatched)
    "map_type",                      # specialty | subspecialty | - (blank if unmatched)
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_ids(value) -> list[int]:
    """Parse a comma-separated specialty_ids string into a list of ints."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    s = str(value).strip()
    if not s:
        return []
    ids = []
    for part in s.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(float(part)))
            except ValueError:
                logger.debug("Could not parse specialty id token: %r", part)
    return ids


def _is_set(value) -> bool:
    """True when value is non-null, non-empty, and non-zero."""
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    return str(value).strip() not in ("", "0")


# ── Data loading ──────────────────────────────────────────────────────────────

def load_physician_map() -> tuple[dict, dict]:
    """
    Parse physician_md_do.csv and return two lookup dicts:
        specialty_map    : eMed_Specialty_ID    → {name, md, do}
        subspecialty_map : eMed_SubSpecialty_ID → {name, md, do}

    md / do are booleans indicating whether that taxonomy is MD- or DO-specific.
    """
    if not _INPUT_CSV.exists():
        raise FileNotFoundError(f"Input CSV not found: {_INPUT_CSV}")

    df = pd.read_csv(_INPUT_CSV, dtype=str)
    df.columns = df.columns.str.strip()
    logger.info("Loaded %d row(s) from %s", len(df), _INPUT_CSV)

    specialty_map    = {}
    subspecialty_map = {}

    for _, row in df.iterrows():
        is_md = _is_set(row.get("MD"))
        is_do = _is_set(row.get("DO"))

        spec_id_raw = row.get("eMed_Speciality_ID", "")
        if _is_set(spec_id_raw):
            sid = int(float(spec_id_raw))
            if sid not in specialty_map:
                specialty_map[sid] = {
                    "name": str(row.get("eMed_Speciality", "") or "").strip(),
                    "md":   is_md,
                    "do":   is_do,
                }

        subspec_id_raw = row.get("eMed_SubSpeciality_ID", "")
        if _is_set(subspec_id_raw):
            ssid = int(float(subspec_id_raw))
            if ssid not in subspecialty_map:
                subspecialty_map[ssid] = {
                    "name": str(row.get("eMed_SubSpeciality", "") or "").strip(),
                    "md":   is_md,
                    "do":   is_do,
                }

    logger.info(
        "Physician map: %d specialty ID(s), %d sub-specialty ID(s)",
        len(specialty_map), len(subspecialty_map),
    )
    return specialty_map, subspecialty_map


def load_already_processed_ids() -> set[int]:
    """Return the set of subscriber IDs already recorded in the log file."""
    if not _LOG_PATH.exists():
        return set()
    try:
        df = pd.read_csv(_LOG_PATH, dtype=str)
        # Only SUMMARY rows represent a fully analyzed subscriber
        summary = df[df["row_type"] == "SUMMARY"]
        ids = set(summary["id"].astype(int).tolist())
        logger.info("Found %d already-analyzed ID(s) in log — will skip them", len(ids))
        return ids
    except Exception as exc:
        logger.warning("Could not read log file (%s) — treating as empty", exc)
        return set()


def fetch_specialty_names(ids: list[int]) -> dict[int, dict]:
    """
    Return {specialty_id: {name, parent_id}} from tbl_master_specialities
    for all IDs in the given list.
    """
    if not ids:
        return {}
    id_list = ", ".join(str(i) for i in ids)
    query = text(f"""
        SELECT id, name, parent_id
        FROM tbl_master_specialities
        WHERE id IN ({id_list})
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return {
        int(row["id"]): {
            "name":      str(row["name"] or "").strip(),
            "parent_id": int(row["parent_id"]) if pd.notna(row["parent_id"]) else None,
        }
        for _, row in df.iterrows()
    }


def fetch_candidate_ids(profession_id: int, order_desc: bool = False) -> list[int]:
    """
    Return all subscriber IDs matching the base filter.
    Only IDs are fetched here (lightweight); full records come later.
    """
    excluded  = ", ".join(str(u) for u in _EXCLUDED_USER_TYPES)
    order_dir = "DESC" if order_desc else "ASC"
    query = text(f"""
        SELECT id
        FROM tbl_newsletter_subscribers
        WHERE profession_parent_id = :pid
          AND user_type NOT IN ({excluded})
          AND country_id = 1
        ORDER BY id {order_dir}
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"pid": profession_id})
    return df["id"].tolist()


def fetch_subscriber_records(ids: list[int]) -> pd.DataFrame:
    """Fetch full rows for the given subscriber IDs."""
    id_list = ", ".join(str(i) for i in ids)
    query = text(f"""
        SELECT id, profession_parent_id, profession_id, specialty_ids
        FROM tbl_newsletter_subscribers
        WHERE id IN ({id_list})
        ORDER BY id
    """)
    with engine.connect() as conn:
        return pd.read_sql(query, conn)


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_subscriber(
    row,
    specialty_map: dict,
    subspecialty_map: dict,
) -> dict:
    """
    Compare the subscriber's specialty_ids against the physician map and return
    a dict describing what was found and what change (if any) is intended.

    match_type values:
        no_specialty_data — specialty_ids is null/empty
        no_match          — none of the IDs appear in the physician map
        partial_match     — some IDs match, some do not
        full_match        — all IDs match the physician map
    """
    current_ids = _parse_ids(row["specialty_ids"])

    if not current_ids:
        return _make_record(
            row,
            matched=[],
            unmatched=[],
            match_type="no_specialty_data",
            intended_parent=None,
            intended_type=None,
            notes="specialty_ids is empty or null",
        )

    matched    = []
    unmatched  = []
    md_signals = 0
    do_signals = 0

    for sid in current_ids:
        info = specialty_map.get(sid) or subspecialty_map.get(sid)
        if info:
            matched.append(sid)
            if info["md"]:
                md_signals += 1
            if info["do"]:
                do_signals += 1
        else:
            unmatched.append(sid)

    if not matched:
        return _make_record(
            row,
            matched=[],
            unmatched=unmatched,
            match_type="no_match",
            intended_parent=None,
            intended_type=None,
            notes="none of the specialty_ids found in physician map",
        )

    match_type     = "full_match" if not unmatched else "partial_match"
    intended_parent = _CANONICAL_PHYSICIAN_ID

    if md_signals > 0 and do_signals == 0:
        intended_type = _MD_PROFESSION_TYPE_ID
        notes = f"MD signals={md_signals}"
    elif do_signals > 0 and md_signals == 0:
        intended_type = _DO_PROFESSION_TYPE_ID
        notes = f"DO signals={do_signals}"
    elif md_signals > 0 and do_signals > 0:
        intended_type = None
        notes = f"mixed MD/DO (md={md_signals} do={do_signals}) — profession_id left unchanged"
    else:
        intended_type = None
        notes = "physician match but MD/DO type indeterminate — profession_id left unchanged"

    if unmatched:
        notes += f"; unmatched IDs not in map: {unmatched}"

    return _make_record(
        row,
        matched=matched,
        unmatched=unmatched,
        match_type=match_type,
        intended_parent=intended_parent,
        intended_type=intended_type,
        notes=notes,
    )


def _make_record(row, matched, unmatched, match_type, intended_parent, intended_type, notes) -> dict:
    return {
        "id":                            int(row["id"]),
        "current_profession_parent_id":  row["profession_parent_id"],
        "current_profession_id":         row["profession_id"],
        "current_specialty_ids":         row["specialty_ids"],
        "matched_specialty_ids":         ",".join(str(s) for s in matched),
        "unmatched_specialty_ids":       ",".join(str(s) for s in unmatched),
        "match_type":                    match_type,
        "intended_profession_parent_id": intended_parent,
        "intended_profession_id":        intended_type,
        "notes":                         notes,
    }


# ── DB update ─────────────────────────────────────────────────────────────────

def apply_update(record: dict) -> None:
    """Execute the UPDATE statement for a single subscriber."""
    intended_parent = record["intended_profession_parent_id"]
    if intended_parent is None:
        return

    intended_type = record["intended_profession_id"]
    set_clauses   = ["profession_parent_id = :prof_parent"]
    params        = {"prof_parent": intended_parent, "sub_id": record["id"]}

    if intended_type is not None:
        set_clauses.append("profession_id = :prof_id")
        params["prof_id"] = intended_type

    query = text(
        f"UPDATE tbl_newsletter_subscribers "
        f"SET {', '.join(set_clauses)} "
        f"WHERE id = :sub_id"
    )
    with engine.begin() as conn:
        conn.execute(query, params)


# ── Output ────────────────────────────────────────────────────────────────────

def write_log(
    records: list[dict],
    status: str,
    specialty_names: dict,
    specialty_map: dict,
    subspecialty_map: dict,
) -> None:
    """
    Append analysis results to the log CSV.

    For each subscriber, writes:
      • One SUMMARY row — overall findings and intended action
      • One DETAIL row per specialty_id — side-by-side comparison of the
        subscriber's current specialty against what is in our physician map
    """
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    _blank_summary = {c: "" for c in _LOG_COLUMNS}
    _blank_detail  = {c: "" for c in _LOG_COLUMNS}

    for r in records:
        # ── SUMMARY row ────────────────────────────────────────────────────────
        summary = {**_blank_summary}
        summary.update({
            "row_type":                      "SUMMARY",
            "id":                            r["id"],
            "analyzed_at":                   now,
            "current_profession_parent_id":  r["current_profession_parent_id"],
            "current_profession_id":         r["current_profession_id"],
            "current_specialty_ids":         r["current_specialty_ids"],
            "match_type":                    r["match_type"],
            "intended_profession_parent_id": r["intended_profession_parent_id"],
            "intended_profession_id":        r["intended_profession_id"],
            "status":                        status,
            "notes":                         r["notes"],
        })
        rows.append(summary)

        # ── DETAIL rows — one per specialty_id ────────────────────────────────
        for sid in _parse_ids(r["current_specialty_ids"]):
            db_info   = specialty_names.get(sid, {})
            db_name   = db_info.get("name", "") if db_info else ""
            parent_id = db_info.get("parent_id") if db_info else None

            if not db_info:
                db_type = "not_in_db"
            elif parent_id == 0:
                db_type = "specialty"
            else:
                db_type = "subspecialty"

            in_spec    = sid in specialty_map
            in_subspec = sid in subspecialty_map
            map_info   = specialty_map.get(sid) or subspecialty_map.get(sid)

            if map_info:
                map_match = "matched"
                map_name  = map_info["name"]
                map_type  = "specialty" if in_spec else "subspecialty"
            else:
                map_match = "unmatched"
                map_name  = ""
                map_type  = "-"

            detail = {**_blank_detail}
            detail.update({
                "row_type":    "DETAIL",
                "id":          r["id"],
                "specialty_id": sid,
                "db_name":     db_name,
                "db_type":     db_type,
                "map_match":   map_match,
                "map_name":    map_name,
                "map_type":    map_type,
            })
            rows.append(detail)

    df = pd.DataFrame(rows, columns=_LOG_COLUMNS)
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _LOG_PATH.exists()
    df.to_csv(_LOG_PATH, mode="a", header=write_header, index=False)
    logger.info(
        "Appended %d subscriber(s) as %d row(s) to %s",
        len(records), len(rows), _LOG_PATH,
    )


def print_summary(records: list[dict], applied: bool) -> None:
    total         = len(records)
    full_match    = sum(1 for r in records if r["match_type"] == "full_match")
    partial_match = sum(1 for r in records if r["match_type"] == "partial_match")
    no_match      = sum(1 for r in records if r["match_type"] == "no_match")
    no_data       = sum(1 for r in records if r["match_type"] == "no_specialty_data")
    will_update   = sum(1 for r in records if r["intended_profession_parent_id"] is not None)
    will_set_md   = sum(1 for r in records if r["intended_profession_id"] == _MD_PROFESSION_TYPE_ID)
    will_set_do   = sum(1 for r in records if r["intended_profession_id"] == _DO_PROFESSION_TYPE_ID)

    mode_label = "APPLIED" if applied else "DRY-RUN (no DB changes)"
    logger.info("─" * 58)
    logger.info("  SUMMARY  [%s]  —  %d subscriber(s) analyzed", mode_label, total)
    logger.info("─" * 58)
    logger.info("  WHAT WAS FOUND")
    logger.info("  %-40s %6d", "Full specialty match:",      full_match)
    logger.info("  %-40s %6d", "Partial specialty match:",   partial_match)
    logger.info("  %-40s %6d", "No match in physician map:", no_match)
    logger.info("  %-40s %6d", "No specialty data:",         no_data)
    logger.info("─" * 58)
    logger.info("  WHAT %s DO", "WAS DONE" if applied else "WE INTEND TO")
    logger.info("  %-40s %6d", "Set profession_parent_id → 167:",  will_update)
    logger.info("  %-40s %6d", "Set profession_id → 184 (MD):",    will_set_md)
    logger.info("  %-40s %6d", "Set profession_id → 185 (DO):",    will_set_do)
    logger.info("  %-40s %6d", "No change (no match / no data):",  total - will_update)
    logger.info("─" * 58)
    logger.info("  Results written to: %s", _LOG_PATH)
    logger.info("─" * 58)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and fix specialty/profession data for newsletter subscribers."
    )
    parser.add_argument(
        "--profession",
        type=int,
        default=1,
        metavar="ID",
        help="Target profession_parent_id (default: 1)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Max new subscribers to analyze in this run (default: all)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes to the database (default: dry-run)",
    )
    parser.add_argument(
        "--order",
        type=str,
        default=None,
        metavar="DIR",
        help="Sort order for candidate IDs: DESC for newest-first (default: ASC)",
    )
    args = parser.parse_args()

    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    order_desc = isinstance(args.order, str) and args.order.strip().upper() == "DESC"

    logger.info(
        "fix_specialty  [mode=%s  profession=%d  limit=%s  order=%s]",
        "APPLY" if args.apply else "DRY-RUN",
        args.profession,
        args.limit if args.limit is not None else "all",
        "DESC" if order_desc else "ASC",
    )

    specialty_map, subspecialty_map = load_physician_map()
    already_processed               = load_already_processed_ids()

    all_ids = fetch_candidate_ids(args.profession, order_desc=order_desc)
    logger.info(
        "Total candidates with profession_parent_id=%d: %d",
        args.profession, len(all_ids),
    )

    new_ids = [i for i in all_ids if i not in already_processed]
    logger.info(
        "%d new (unanalyzed) candidate(s) after excluding %d already-processed",
        len(new_ids), len(all_ids) - len(new_ids),
    )

    if not new_ids:
        logger.info("Nothing to do — all candidates have already been analyzed.")
        return

    if args.limit is not None:
        new_ids = new_ids[: args.limit]
        logger.info("Applying --limit: processing %d subscriber(s) this run", len(new_ids))

    records = []
    for _, row in fetch_subscriber_records(new_ids).iterrows():
        records.append(analyze_subscriber(row, specialty_map, subspecialty_map))

    # Collect all unique specialty IDs across analyzed subscribers so we can
    # look up their canonical names from tbl_master_specialities in one query.
    all_specialty_ids = sorted({
        sid
        for r in records
        for sid in _parse_ids(r["current_specialty_ids"])
    })
    specialty_names = fetch_specialty_names(all_specialty_ids)
    logger.info(
        "Fetched names for %d unique specialty ID(s) from tbl_master_specialities",
        len(specialty_names),
    )

    if args.apply:
        applied_count = 0
        for r in records:
            if r["intended_profession_parent_id"] is not None:
                apply_update(r)
                applied_count += 1
        logger.info("Database updated: %d row(s) changed", applied_count)
        write_log(records, status="applied",
                  specialty_names=specialty_names,
                  specialty_map=specialty_map,
                  subspecialty_map=subspecialty_map)
    else:
        write_log(records, status="dry_run",
                  specialty_names=specialty_names,
                  specialty_map=specialty_map,
                  subspecialty_map=subspecialty_map)

    print_summary(records, applied=args.apply)


if __name__ == "__main__":
    main()
