"""
Analyzes profession data in tbl_newsletter_subscribers and produces a proposed mapping
to the canonical profession list in Professions_Mapping.csv.

Reads:
  src/migrations/profession/data/Professions_Mapping.csv  — canonical mapping list

Writes (--apply only):
  src/migrations/profession/data/profession_cleanup/profession_missing.csv
      Subscribers where both profession_id and profession_type_id are null/empty — cannot be mapped.

  src/migrations/profession/data/profession_cleanup/profession_mapping.csv
      All other subscribers with their current profession details and the proposed correct eMed ID.

Defaults to DRY RUN — pass --apply to write the CSV files.

Run from the repo root:
    python -m src.migrations.profession.migrate_profession                    # dry run, all
    python -m src.migrations.profession.migrate_profession --limit 100        # dry run, first 100
    python -m src.migrations.profession.migrate_profession --apply --limit 100
    python -m src.migrations.profession.migrate_profession --apply
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_DATA_DIR      = Path(__file__).parent / "data"
_MAPPING_CSV   = _DATA_DIR / "Professions_Mapping.csv"
_CLEANUP_DIR = _DATA_DIR / "profession_cleanup"
_MISSING_CSV             = _CLEANUP_DIR / "profession_missing.csv"
_MATCHED_CSV             = _CLEANUP_DIR / "profession_matched.csv"
_UNMATCHED_CSV           = _CLEANUP_DIR / "profession_unmatched.csv"
_USA_PHYSICIAN_REVIEW_CSV = _CLEANUP_DIR / "profession_usa_physician_review.csv"
_USA_NURSE_REVIEW_CSV    = _CLEANUP_DIR / "profession_usa_nurse_review.csv"

# eMed IDs valid only for non-USA subscribers (plain parent-level entries)
_NON_USA_ONLY_EMED_IDS = {167, 169}  # 167=Physician, 169=Nurse
_USA_COUNTRY_ID        = 1

_SUBSCRIBER_COLS = (
    "id, firstname, lastname, country_id, "
    "profession_id, profession_parent_id, profession_type_id"
)


_LOG_FILE = _CLEANUP_DIR / "run.log"


# ---------------------------------------------------------------------------
# Run setup
# ---------------------------------------------------------------------------

class _Tee:
    """Write to both the original stdout and a file simultaneously."""
    def __init__(self, stream, file):
        self._stream = stream
        self._file   = file

    def write(self, data):
        self._stream.write(data)
        self._file.write(data)

    def flush(self):
        self._stream.flush()
        self._file.flush()

    def __getattr__(self, attr):
        return getattr(self._stream, attr)


def _prepare_run_dir() -> "tuple[object, logging.FileHandler]":
    """Clear the cleanup dir, open run.log, wire up file logging and stdout tee."""
    _CLEANUP_DIR.mkdir(parents=True, exist_ok=True)
    for f in _CLEANUP_DIR.iterdir():
        if f.is_file():
            f.unlink()

    log_fh   = open(_LOG_FILE, "w", encoding="utf-8")
    tee      = _Tee(sys.stdout, log_fh)
    sys.stdout = tee

    file_handler = logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(file_handler)

    return log_fh, file_handler


def _teardown_run_dir(log_fh, file_handler, original_stdout):
    logging.getLogger().removeHandler(file_handler)
    file_handler.close()
    sys.stdout = original_stdout
    log_fh.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_empty(val) -> bool:
    """True if val represents a missing/unset ID (None, NaN, 0)."""
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass
    try:
        return int(val) == 0
    except (ValueError, TypeError):
        return str(val).strip() == ""


def _to_int(val) -> int | None:
    if _is_empty(val):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _fmt_id(val) -> int | str:
    """Return val as a clean int, or empty string if null/zero."""
    v = _to_int(val)
    return v if v is not None else ""


def _fmt_id_with_sp(val, sp_lookup: dict) -> str:
    """Return 'id(KeyDescription, KeyValue)' or empty string if null/zero."""
    v = _to_int(val)
    if v is None:
        return ""
    sp = sp_lookup.get(v)
    if sp:
        desc  = str(sp.get("KeyDescription") or "").strip()
        kval  = str(sp.get("KeyValue") or "").strip()
        return f"{v}({desc}, {kval})"
    return str(v)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_subscribers(limit: int | None) -> pd.DataFrame:
    if limit is not None:
        query = text(f"SELECT {_SUBSCRIBER_COLS} FROM tbl_newsletter_subscribers LIMIT :limit")
        params = {"limit": limit}
    else:
        query = text(f"SELECT {_SUBSCRIBER_COLS} FROM tbl_newsletter_subscribers")
        params = {}

    logger.info("Fetching subscribers from tbl_newsletter_subscribers%s",
                f" (limit={limit})" if limit else "")
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params)
    logger.info("Fetched %d subscriber(s)", len(df))
    return df


def fetch_system_params(ids: list[int]) -> dict[int, dict]:
    """Batch-fetch tbl_system_params rows for the given IDs in one query."""
    if not ids:
        return {}
    # IDs are all ints — safe to inline directly
    id_list = ",".join(str(i) for i in ids)
    query = text(f"""
        SELECT IdSystem_Params, KeyValue, KeyDescription, SubCategory, status
        FROM tbl_system_params
        WHERE IdSystem_Params IN ({id_list})
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return {int(row["IdSystem_Params"]): row.to_dict() for _, row in df.iterrows()}


def load_canonical_professions() -> pd.DataFrame:
    df = pd.read_csv(_MAPPING_CSV)
    return df[df["Status"] == 1].copy()


def build_canonical_id_lookup(canonical_df: pd.DataFrame) -> dict[int, dict]:
    """Primary lookup — keyed by eMedEvents_Profession_ID (same as tbl_system_params ID)."""
    return {
        int(row["eMedEvents_Profession_ID"]): row.to_dict()
        for _, row in canonical_df.iterrows()
    }


def build_canonical_name_lookup(canonical_df: pd.DataFrame) -> dict[str, dict]:
    """Fallback lookup — keyed by eMedEvents_Profession_Name (lowercased)."""
    return {
        str(row["eMedEvents_Profession_Name"]).strip().lower(): row.to_dict()
        for _, row in canonical_df.iterrows()
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_canonical_match(
    prof_id: int | None,
    prof_parent_id: int | None,
    key_desc: str,
    key_value: str,
    id_lookup: dict,
    name_lookup: dict,
) -> "tuple[dict, int | None] | tuple[None, None]":
    """
    Match in priority order:
      1. Direct ID match on profession_id        (most reliable)
      2. Direct ID match on profession_parent_id (parent-level fallback)
      3. "{KeyDescription} ({KeyValue})"         e.g. "Physician (MD)"
      4. KeyValue alone                          e.g. "MD"
      5. KeyDescription alone                    e.g. "Physician"

    Returns (canon_entry, matched_id) where matched_id is the sp_lookup ID
    that caused the match (None for name-based matches).
    """
    for eid in (prof_id, prof_parent_id):
        if eid is not None:
            hit = id_lookup.get(eid)
            if hit:
                return hit, eid

    for candidate in (
        f"{key_desc} ({key_value})".strip(),
        key_value.strip(),
        key_desc.strip(),
    ):
        hit = name_lookup.get(candidate.lower())
        if hit:
            return hit, None

    return None, None


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze(subscribers: pd.DataFrame, sp_lookup: dict, id_lookup: dict, name_lookup: dict):
    missing_rows = []
    mapping_rows = []

    for _, sub in subscribers.iterrows():
        prof_id        = _to_int(sub["profession_id"])
        prof_parent_id = _to_int(sub["profession_parent_id"])
        prof_type_id   = _to_int(sub["profession_type_id"])

        base = {
            "Subscriber ID":        _fmt_id(sub["id"]),
            "Firstname":            sub["firstname"] or "",
            "Lastname":             sub["lastname"] or "",
            "country_id":           _fmt_id(sub["country_id"]),
            "profession_id":        _fmt_id_with_sp(sub["profession_id"], sp_lookup),
            "profession_parent_id": _fmt_id_with_sp(sub["profession_parent_id"], sp_lookup),
        }

        # ── Case 1: no profession info at all ──────────────────────────────
        if prof_id is None and prof_type_id is None:
            missing_rows.append({
                **base,
                "Reason": "Both profession_id and profession_type_id are null or empty",
            })
            continue

        # ── Case 2: look up profession details ─────────────────────────────
        key_desc  = ""
        key_value = ""

        if prof_parent_id is None:
            # Only profession_id available — look it up directly
            sp = sp_lookup.get(prof_id)
            if sp:
                key_desc  = str(sp.get("KeyDescription") or "").strip()
                key_value = str(sp.get("KeyValue") or "").strip()
        else:
            # Both profession_parent_id and profession_id present
            child_sp  = sp_lookup.get(prof_id)
            parent_sp = sp_lookup.get(prof_parent_id)
            if child_sp:
                key_desc  = str(child_sp.get("KeyDescription") or "").strip()
                key_value = str(child_sp.get("KeyValue") or "").strip()
            elif parent_sp:
                key_desc  = str(parent_sp.get("KeyDescription") or "").strip()
                key_value = str(parent_sp.get("KeyValue") or "").strip()

        # ── Case 3: match against canonical list ───────────────────────────
        canon, matched_id = find_canonical_match(
            prof_id, prof_parent_id,
            key_desc, key_value,
            id_lookup, name_lookup,
        )

        # If the match was made via an ID, pull key_desc/key_value from that
        # specific sp_lookup record so the columns reflect the matched entry,
        # not a sibling record that happened to be looked up first.
        if matched_id is not None:
            matched_sp = sp_lookup.get(matched_id)
            if matched_sp:
                key_desc  = str(matched_sp.get("KeyDescription") or "").strip()
                key_value = str(matched_sp.get("KeyValue") or "").strip()

        mapping_rows.append({
            **base,
            "Lookup Key Description":  key_desc,
            "Lookup Key Value":        key_value,
            "Proposed eMed ID":        _fmt_id(canon["eMedEvents_Profession_ID"]) if canon else "",
            "Match Status":            "Matched" if canon else "Not in canonical list",
        })

    return pd.DataFrame(missing_rows), pd.DataFrame(mapping_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(apply: bool, limit: int | None) -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    if not _MAPPING_CSV.exists():
        raise SystemExit(f"Professions mapping CSV not found: {_MAPPING_CSV}")

    original_stdout = sys.stdout
    log_fh, file_handler = _prepare_run_dir()

    try:
        _main(apply, limit)
    finally:
        _teardown_run_dir(log_fh, file_handler, original_stdout)


def _main(apply: bool, limit: int | None) -> None:
    logger.info("Mode: %s", "APPLY" if apply else "DRY RUN")

    canonical_df  = load_canonical_professions()
    id_lookup     = build_canonical_id_lookup(canonical_df)
    name_lookup   = build_canonical_name_lookup(canonical_df)
    logger.info("Loaded %d canonical profession(s)", len(canonical_df))

    subscribers = fetch_subscribers(limit)

    # Collect all unique profession IDs for a single batch DB lookup
    all_ids: set[int] = set()
    for col in ("profession_id", "profession_parent_id", "profession_type_id"):
        all_ids.update(_to_int(v) for v in subscribers[col] if _to_int(v) is not None)

    sp_lookup = fetch_system_params(list(all_ids))
    logger.info("Fetched %d tbl_system_params row(s) for ID lookup", len(sp_lookup))

    missing_df, mapping_df = analyze(subscribers, sp_lookup, id_lookup, name_lookup)

    # ── Output (always written) ────────────────────────────────────────────
    _CLEANUP_DIR.mkdir(parents=True, exist_ok=True)

    all_matched_df = mapping_df[mapping_df["Match Status"] == "Matched"] if not mapping_df.empty else mapping_df
    unmatched_df   = mapping_df[mapping_df["Match Status"] == "Not in canonical list"] if not mapping_df.empty else mapping_df

    # USA subscribers matched to plain "Physician" or "Nurse" need manual review —
    # these parent-level entries are valid only for non-USA HCPs.
    def _proposed_id(x):
        return _to_int(x)

    if not all_matched_df.empty:
        proposed_ids = all_matched_df["Proposed eMed ID"].apply(_proposed_id)
        country_ids  = all_matched_df["country_id"].apply(_to_int)
        usa_mask          = country_ids == _USA_COUNTRY_ID
        usa_physician_mask = usa_mask & (proposed_ids == 167)
        usa_nurse_mask     = usa_mask & (proposed_ids == 169)
        usa_review_mask    = usa_physician_mask | usa_nurse_mask
    else:
        usa_physician_mask = pd.Series(dtype=bool)
        usa_nurse_mask     = pd.Series(dtype=bool)
        usa_review_mask    = pd.Series(dtype=bool)

    usa_physician_df = all_matched_df[usa_physician_mask] if not all_matched_df.empty else all_matched_df
    usa_nurse_df     = all_matched_df[usa_nurse_mask]     if not all_matched_df.empty else all_matched_df
    matched_df       = all_matched_df[~usa_review_mask]   if not all_matched_df.empty else all_matched_df

    missing_df.to_csv(_MISSING_CSV, index=False)
    matched_df.to_csv(_MATCHED_CSV, index=False)
    unmatched_df.to_csv(_UNMATCHED_CSV, index=False)
    usa_physician_df.to_csv(_USA_PHYSICIAN_REVIEW_CSV, index=False)
    usa_nurse_df.to_csv(_USA_NURSE_REVIEW_CSV, index=False)
    logger.info("Written %s (%d row(s))", _MISSING_CSV.name, len(missing_df))
    logger.info("Written %s (%d row(s))", _MATCHED_CSV.name, len(matched_df))
    logger.info("Written %s (%d row(s))", _UNMATCHED_CSV.name, len(unmatched_df))
    logger.info("Written %s (%d row(s))", _USA_PHYSICIAN_REVIEW_CSV.name, len(usa_physician_df))
    logger.info("Written %s (%d row(s))", _USA_NURSE_REVIEW_CSV.name, len(usa_nurse_df))

    print("\n" + "=" * 62)
    print("  PROFESSION MIGRATION ANALYSIS — SUMMARY")
    print("=" * 62)
    print(f"  Subscribers processed  : {len(subscribers):,}")
    print(f"  Missing profession     : {len(missing_df):,}  → {_MISSING_CSV.name}")
    print(f"  Mapped to canonical    : {len(matched_df):,}  → {_MATCHED_CSV.name}")
    print(f"  USA plain Physician    : {len(usa_physician_df):,}  → {_USA_PHYSICIAN_REVIEW_CSV.name}  ⚠ needs review")
    print(f"  USA plain Nurse        : {len(usa_nurse_df):,}  → {_USA_NURSE_REVIEW_CSV.name}  ⚠ needs review")
    print(f"  Not in canonical list  : {len(unmatched_df):,}  → {_UNMATCHED_CSV.name}")
    print(f"  DB writes              : {'APPLY — enabled' if apply else 'DRY RUN — no DB changes'}")
    print("=" * 62 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analyze and propose profession ID mapping for newsletter subscribers."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write output CSVs (default: dry run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N subscribers",
    )
    args = parser.parse_args()
    main(apply=args.apply, limit=args.limit)
