"""
fix_profession_speciality.py

Loads physician specialty maps from:
  - data/input/physician_md_do.csv  (MD / DO taxonomy codes)
  - data/input/physician_dpm.csv    (DPM taxonomy codes)

Then interactively shows random entries from the combined map so you can
verify the mappings look correct.

Run from the repo root:
    python migrations/fix_profession_speciality.py
"""

import argparse
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import requests
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_INPUT_DIR        = Path(__file__).parent.parent / "data" / "input"
_MD_DO_CSV        = _INPUT_DIR / "physician_md_do.csv"
_DPM_CSV          = _INPUT_DIR / "physician_dpm.csv"
_ID_OVERRIDES_CSV        = _INPUT_DIR / "specialty_id_overrides.csv"
_PROFESSION_OVERRIDES_CSV = _INPUT_DIR / "profession_overrides.csv"

_DPM_PROFESSION_TYPE_ID = 357
_MD_PROFESSION_TYPE_ID  = 184
_DO_PROFESSION_TYPE_ID  = 185

# Profession parent IDs whose subscribers we will analyze.
# Add more IDs here as needed in the future.
_PROFESSION_PARENT_IDS = [167]

_TARGET_TABLE   = "tbl_newsletter_subscribers_physicians"
_SOURCE_TABLE   = "tbl_newsletter_subscribers"
_CACHE_CSV      = _INPUT_DIR / "newsletter_table.csv"
_LOG_DIR        = Path(__file__).parent.parent / "logs"
_LOG_FILE       = _LOG_DIR / "fix_profession_specialty.log"
_PROCESSED_DIR  = Path(__file__).parent.parent / "data" / "processed" / "physician"
_CLEAN_CSV               = _PROCESSED_DIR / "clean_specialty.csv"       # full_match
_PARTIAL_CSV             = _PROCESSED_DIR / "partial_specialty.csv"     # partial_match
_MISMATCH_CSV            = _PROCESSED_DIR / "mismatch_specialty.csv"    # no_match
_NO_SPECIALTY_CSV        = _PROCESSED_DIR / "no_specialty.csv"          # no_specialty_data
_PROFESSION_OVERRIDE_CSV = _PROCESSED_DIR / "profession_override.csv"   # profession_override

_ALL_OUTPUT_CSVS = (_CLEAN_CSV, _PARTIAL_CSV, _MISMATCH_CSV, _NO_SPECIALTY_CSV, _PROFESSION_OVERRIDE_CSV)

# When a sub-specialty is matched, inject its parent specialty ID into the
# recommendation ahead of the sub-specialty.  Add entries here to extend this
# logic to other specialties in the future.
_PARENT_SPECIALTY_INJECTION = {
    # Internal Medicine (IDs are a contiguous range)
    626:  frozenset(range(2556, 2586)),
    # Emergency Medicine
    2:    frozenset({924, 927, 928, 929, 2545, 2546}),
    # Family Medicine
    71:   frozenset({930, 2547, 2548, 2549, 2550, 2551, 2552, 2553, 2554, 2555}),
    # Allergy & Immunology
    889:  frozenset({2531, 2532}),
    # Anesthesiology
    81:   frozenset({915, 2533, 2534, 2535, 2536, 2537}),
    # Dermatology
    82:   frozenset({1919, 2541, 2542, 2543, 2544}),
    # Medical Genetics
    1904: frozenset({2587, 2588, 2589, 2590, 2591, 2592}),
    # Nuclear Medicine
    183:  frozenset({2594, 2595, 2596}),
    # Obstetrics & Gynecology
    231:  frozenset({2597, 2598, 2599, 2600, 2601, 2602, 2603, 2604, 2605, 2606}),
    # Ophthalmology
    100:  frozenset({2608, 2609, 2610, 2611, 2612, 2613, 2614}),
    # Orthopaedic Surgery
    637:  frozenset({2616, 2617, 2618, 2619, 2620, 2621, 2622}),
    # Otolaryngology
    43:   frozenset({2623, 2624, 2625, 2626, 2627, 2628, 2629}),
    # Pain Medicine
    87:   frozenset({2630, 2631}),
    # Pathology
    80:   frozenset({977, 978, 979, 980, 981, 982, 985, 986, 987,
                     2632, 2633, 2634, 2635, 2636, 2637, 2638}),
    # Pediatrics
    65:   frozenset({62, 211, 280, 405, 507, 998, 1002, 1004,
                     2639, 2640, 2641, 2642, 2643, 2644, 2645, 2646,
                     2647, 2648, 2649, 2650, 2651, 2652, 2653, 2654}),
    # Physical Medicine & Rehabilitation
    1463: frozenset({2656, 2657, 2658, 2659, 2660, 2661, 2662}),
    # Plastic Surgery
    70:   frozenset({2663, 2664}),
    # Preventive Medicine
    230:  frozenset({1017, 2666, 2667, 2668, 2669, 2670, 2671, 2672, 2673, 2674}),
    # Psychiatry & Neurology
    906:  frozenset({1021, 1022, 1023, 1024, 1026, 1027, 1028, 1029, 1030,
                     1031, 1032, 1033, 1034, 1035, 2675, 2676, 2677, 2678,
                     2679, 2680, 2681, 2682, 2683}),
    # Radiology
    1912: frozenset({1037, 2684, 2685, 2686, 2687, 2688, 2694, 2695, 2696, 2697, 2698}),
    # Surgery
    68:   frozenset({2689, 2690, 2691, 2701, 2702, 2703, 2704, 2705, 2706}),
    # Urology
    63:   frozenset({2708, 2709}),
    # Podiatrist (from physician_dpm.csv)
    207:  frozenset({862}),
}

_OUTPUT_COLUMNS = [
    "id",
    "parent_profession_id_orig",
    "profession_type_id_orig",
    "specialty_ids_orig",
    "parent_profession_id_recommended",
    "profession_type_id_recommended",
    "specialty_ids_recommended",
    "subscription_status",
    "email_status",
]

_SUBSCRIPTION_STATUS_MAP = {
    0: "Unknown",
    1: "Subscribed",
    2: "Unsubscribed",
    3: "Bounced",
    4: "Dropped",
}

_NPPES_URL   = "https://npiregistry.cms.hhs.gov/api/"
_NPPES_DELAY = 0.4   # seconds between calls — stays within ~2 req/sec

# When a subscriber has more specialties than this threshold, we don't trust
# the DB values and instead look the person up in NPPES by name.
_MAX_SPECIALTIES_TRUST = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    """Normalise a specialty name for comparison: lowercase and replace & with and."""
    return name.lower().replace("&", "and").replace("  ", " ").strip()


def _is_set(value) -> bool:
    """True when value is non-null, non-empty, and non-zero."""
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    return str(value).strip() not in ("", "0")


def _to_int(value):
    """Parse a numeric string to int, or return None."""
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def _parse_ids(value) -> list[int]:
    """Parse a comma-separated specialty_ids string into a list of ints."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    s = str(value).strip()
    if not s:
        return []
    seen = set()
    ids = []
    for part in s.split(","):
        part = part.strip()
        if part:
            try:
                val = int(float(part))
                if val not in seen:
                    seen.add(val)
                    ids.append(val)
            except ValueError:
                pass
    return ids


# ── CSV loaders ───────────────────────────────────────────────────────────────

def load_md_do_map() -> list[dict]:
    """
    Parse physician_md_do.csv and return a flat list of mapping entries.
    Each entry represents one NPPES taxonomy code row from the CSV.
    Rows with no eMed specialty or sub-specialty ID are skipped.
    """
    if not _MD_DO_CSV.exists():
        raise FileNotFoundError(f"Input CSV not found: {_MD_DO_CSV}")

    df = pd.read_csv(_MD_DO_CSV, dtype=str)
    df.columns = df.columns.str.strip()
    logger.info("Loaded %d row(s) from %s", len(df), _MD_DO_CSV.name)

    entries = []
    skipped = 0
    for _, row in df.iterrows():
        spec_id    = row.get("eMed_Speciality_ID", "")
        subspec_id = row.get("eMed_SubSpeciality_ID", "")

        if not _is_set(spec_id) and not _is_set(subspec_id):
            skipped += 1
            continue

        entries.append({
            "source":            "physician_md_do.csv",
            "code":              str(row.get("Code",           "") or "").strip(),
            "grouping":          str(row.get("Grouping",       "") or "").strip(),
            "classification":    str(row.get("Classification", "") or "").strip(),
            "specialization":    str(row.get("Specialization", "") or "").strip(),
            "emed_profession":    str(row.get("eMed_Profession", "") or "").strip(),
            "emed_profession_id": _to_int(row.get("Not Sure")),
            "md":                _is_set(row.get("MD")),
            "do":                _is_set(row.get("DO")),
            "dpm":               False,
            "specialty_name":    str(row.get("eMed_Speciality",    "") or "").strip(),
            "specialty_id":      _to_int(spec_id)    if _is_set(spec_id)    else None,
            "subspecialty_name": str(row.get("eMed_SubSpeciality", "") or "").strip(),
            "subspecialty_id":   _to_int(subspec_id) if _is_set(subspec_id) else None,
        })

    logger.info(
        "physician_md_do.csv: %d mapped row(s), %d row(s) skipped (no eMed ID)",
        len(entries), skipped,
    )
    return entries


def load_dpm_map() -> list[dict]:
    """
    Parse physician_dpm.csv and return a flat list of mapping entries.
    Each entry represents one NPPES taxonomy code row from the CSV.
    Rows with no eMed specialty or sub-specialty ID are skipped.
    """
    if not _DPM_CSV.exists():
        raise FileNotFoundError(f"Input CSV not found: {_DPM_CSV}")

    df = pd.read_csv(_DPM_CSV, dtype=str)
    df.columns = df.columns.str.strip()
    logger.info("Loaded %d row(s) from %s", len(df), _DPM_CSV.name)

    entries = []
    skipped = 0
    for _, row in df.iterrows():
        spec_id    = row.get("eMed_Speciality_ID", "")
        subspec_id = row.get("eMed_SubSpeciality_ID", "")

        if not _is_set(spec_id) and not _is_set(subspec_id):
            skipped += 1
            continue

        entries.append({
            "source":            "physician_dpm.csv",
            "code":              str(row.get("Code",           "") or "").strip(),
            "grouping":          str(row.get("Grouping",       "") or "").strip(),
            "classification":    str(row.get("Classification", "") or "").strip(),
            "specialization":    str(row.get("Specialization", "") or "").strip(),
            "emed_profession":    str(row.get("eMed_Profession", "") or "").strip(),
            "emed_profession_id": _to_int(row.get("Not DPM")),
            "md":                False,
            "do":                False,
            "dpm":               _is_set(row.get("DPM")),
            "specialty_name":    str(row.get("eMed_Speciality",    "") or "").strip(),
            "specialty_id":      _to_int(spec_id)    if _is_set(spec_id)    else None,
            "subspecialty_name": str(row.get("eMed_SubSpeciality", "") or "").strip(),
            "subspecialty_id":   _to_int(subspec_id) if _is_set(subspec_id) else None,
        })

    logger.info(
        "physician_dpm.csv: %d mapped row(s), %d row(s) skipped (no eMed ID)",
        len(entries), skipped,
    )
    return entries


def load_combined_map() -> list[dict]:
    """Load and combine both CSV maps into a single list."""
    md_do = load_md_do_map()
    dpm   = load_dpm_map()
    combined = md_do + dpm
    logger.info("Combined map: %d total entry/entries", len(combined))
    return combined


# ── Display ───────────────────────────────────────────────────────────────────

def _profession_types(entry: dict) -> str:
    types = []
    if entry["md"]:
        types.append(f"MD (ID={_MD_PROFESSION_TYPE_ID})")
    if entry["do"]:
        types.append(f"DO (ID={_DO_PROFESSION_TYPE_ID})")
    if entry["dpm"]:
        types.append(f"DPM (ID={_DPM_PROFESSION_TYPE_ID})")
    return ", ".join(types) if types else "(none flagged)"


def show_entry(entry: dict, index: int, total: int) -> None:
    sep = "─" * 62
    spec_display    = (
        f"{entry['specialty_name']} (ID={entry['specialty_id']})"
        if entry["specialty_id"] is not None
        else "(none)"
    )
    subspec_display = (
        f"{entry['subspecialty_name']} (ID={entry['subspecialty_id']})"
        if entry["subspecialty_id"] is not None
        else "(none)"
    )
    print(f"\n{sep}")
    print(f"  Entry {index} of {total} (random sample)")
    print(sep)
    print(f"  Source         : {entry['source']}")
    print(f"  NPPES Code     : {entry['code'] or '(blank)'}")
    print(f"  Grouping       : {entry['grouping'] or '(blank)'}")
    print(f"  Classification : {entry['classification'] or '(blank)'}")
    print(f"  Specialization : {entry['specialization'] or '(blank)'}")
    prof_id = entry.get("emed_profession_id")
    prof_display = entry["emed_profession"] or "(blank)"
    if prof_id is not None:
        prof_display += f" (ID={prof_id})"
    print(f"  eMed Profession: {prof_display}")
    print(f"  Profession type: {_profession_types(entry)}")
    print(f"  Specialty      : {spec_display}")
    print(f"  Sub-specialty  : {subspec_display}")
    print(sep)


# ── Database ──────────────────────────────────────────────────────────────────

def ensure_physician_table() -> None:
    """
    Check whether tbl_newsletter_subscribers_physicians exists.
    If it does, log and return. If not, create it with the same column
    definitions as tbl_newsletter_subscribers using CREATE TABLE ... LIKE ...
    """
    check_query  = text("SHOW TABLES LIKE :name")
    create_query = text(
        f"CREATE TABLE `{_TARGET_TABLE}` LIKE `{_SOURCE_TABLE}`"
    )

    with engine.connect() as conn:
        result = conn.execute(check_query, {"name": _TARGET_TABLE}).fetchone()

    if result:
        logger.info(
            "Table '%s' already exists — nothing to do.", _TARGET_TABLE
        )
        print(f"\n  Table '{_TARGET_TABLE}' already exists — skipping creation.")
        return

    logger.info(
        "Table '%s' not found — creating from '%s' ...",
        _TARGET_TABLE, _SOURCE_TABLE,
    )
    with engine.begin() as conn:
        conn.execute(create_query)

    logger.info("Table '%s' created successfully.", _TARGET_TABLE)
    print(f"\n  Table '{_TARGET_TABLE}' created successfully (same structure as '{_SOURCE_TABLE}').")


def build_lookups(combined: list[dict]) -> tuple[dict, dict]:
    """
    Flatten the combined map list into two lookup dicts keyed by eMed ID.
    MD/DO/DPM flags are OR'd together when multiple CSV rows share the same ID.
    """
    specialty_lookup    = {}
    subspecialty_lookup = {}

    for entry in combined:
        sid = entry.get("specialty_id")
        if sid is not None:
            if sid not in specialty_lookup:
                specialty_lookup[sid] = {"name": entry["specialty_name"],
                                         "md": False, "do": False, "dpm": False}
            specialty_lookup[sid]["md"]  = specialty_lookup[sid]["md"]  or entry["md"]
            specialty_lookup[sid]["do"]  = specialty_lookup[sid]["do"]  or entry["do"]
            specialty_lookup[sid]["dpm"] = specialty_lookup[sid]["dpm"] or entry["dpm"]

        ssid = entry.get("subspecialty_id")
        if ssid is not None:
            if ssid not in subspecialty_lookup:
                subspecialty_lookup[ssid] = {"name": entry["subspecialty_name"],
                                              "md": False, "do": False, "dpm": False}
            subspecialty_lookup[ssid]["md"]  = subspecialty_lookup[ssid]["md"]  or entry["md"]
            subspecialty_lookup[ssid]["do"]  = subspecialty_lookup[ssid]["do"]  or entry["do"]
            subspecialty_lookup[ssid]["dpm"] = subspecialty_lookup[ssid]["dpm"] or entry["dpm"]

    logger.info(
        "Lookups built: %d specialty ID(s), %d sub-specialty ID(s)",
        len(specialty_lookup), len(subspecialty_lookup),
    )
    return specialty_lookup, subspecialty_lookup


def build_name_lookups(combined: list[dict]) -> tuple[dict, dict]:
    """
    Build name-keyed (lowercase) lookups for Rule 3 (name-based matching).

    spec_by_name:
        lower(specialty_name) → (specialty_id, entry)

    subspec_by_name:
        lower(subspecialty_name) → [(subspecialty_id, parent_specialty_id, entry), ...]

    Subspecialties are stored as a list because the same name can appear under
    multiple parent specialties. The parent_specialty_id (from the CSV row) is
    stored alongside each entry so the caller can prefer the one whose parent
    is already confirmed for the current subscriber.
    """
    spec_by_name    = {}
    subspec_by_name = {}

    for entry in combined:
        sid   = entry.get("specialty_id")
        sname = (entry.get("specialty_name") or "").strip()
        if sid and sname:
            key = _norm_name(sname)
            if key not in spec_by_name:
                spec_by_name[key] = (sid, {
                    "name": sname,
                    "md": entry["md"], "do": entry["do"], "dpm": entry["dpm"],
                })

        ssid   = entry.get("subspecialty_id")
        ssname = (entry.get("subspecialty_name") or "").strip()
        if ssid and ssname:
            key = _norm_name(ssname)
            if key not in subspec_by_name:
                subspec_by_name[key] = []
            # Avoid duplicates (same ssid can appear in multiple CSV rows)
            if not any(e[0] == ssid for e in subspec_by_name[key]):
                subspec_by_name[key].append((
                    ssid,
                    sid,   # parent specialty ID from this CSV row — may be None
                    {"name": ssname, "md": entry["md"], "do": entry["do"], "dpm": entry["dpm"]},
                ))

    logger.info(
        "Name lookups built: %d specialty name(s), %d sub-specialty name(s)",
        len(spec_by_name), len(subspec_by_name),
    )
    return spec_by_name, subspec_by_name


def build_taxonomy_lookup(combined: list[dict]) -> dict:
    """
    Build a dict keyed by NPPES taxonomy code (uppercase) → map entry.
    Used to translate NPPES taxonomy codes from a live lookup into eMed IDs.
    """
    lookup = {}
    for entry in combined:
        code = entry.get("code", "").strip().upper()
        if not code:
            continue
        if code not in lookup:
            lookup[code] = {
                "specialty_id":      entry.get("specialty_id"),
                "specialty_name":    entry.get("specialty_name", ""),
                "subspecialty_id":   entry.get("subspecialty_id"),
                "subspecialty_name": entry.get("subspecialty_name", ""),
                "md":                entry["md"],
                "do":                entry["do"],
                "dpm":               entry["dpm"],
            }
        else:
            # OR the profession-type flags in case the same code appears in both CSVs
            lookup[code]["md"]  = lookup[code]["md"]  or entry["md"]
            lookup[code]["do"]  = lookup[code]["do"]  or entry["do"]
            lookup[code]["dpm"] = lookup[code]["dpm"] or entry["dpm"]

    logger.info("Taxonomy code lookup built: %d NPPES code(s)", len(lookup))
    return lookup


def load_id_overrides() -> dict[int, dict]:
    """
    Load manual specialty-ID overrides from specialty_id_overrides.csv.

    Returns a dict mapping original_id (DB) → {"mapped_id": int, "parent_id": int|None}.
    Required columns: original_id, mapped_id.
    Optional columns:
      parent_id — when the mapped_id is a sub-specialty, set this to its parent
                  specialty ID so it gets injected into the recommendation ahead
                  of the sub-specialty (mirrors _PARENT_SPECIALTY_INJECTION logic).
      notes     — human-readable description, logged only.
    """
    if not _ID_OVERRIDES_CSV.exists():
        logger.info("No specialty_id_overrides.csv found — skipping manual overrides.")
        return {}

    df = pd.read_csv(_ID_OVERRIDES_CSV, dtype=str)
    df.columns = df.columns.str.strip()
    overrides = {}
    for _, row in df.iterrows():
        orig   = _to_int(row.get("original_id"))
        mapped = _to_int(row.get("mapped_id"))   # None when blank → delete override
        parent = _to_int(row.get("parent_id"))
        if orig is not None:
            overrides[orig] = {"mapped_id": mapped, "parent_id": parent}
            if mapped is None:
                logger.debug(
                    "Override loaded: DB ID %d → DELETE (remove from recommendation)  (%s)",
                    orig, row.get("notes", ""),
                )
            else:
                logger.debug(
                    "Override loaded: DB ID %d → eMed ID %d (parent=%s)  (%s)",
                    orig, mapped, parent, row.get("notes", ""),
                )

    logger.info("Manual ID overrides loaded: %d entry/entries from %s", len(overrides), _ID_OVERRIDES_CSV.name)
    return overrides


def load_profession_overrides() -> dict[int, dict]:
    """
    Load profession-level overrides from profession_overrides.csv.

    When a subscriber's specialty_ids contains a trigger_specialty_id listed here,
    the record is rerouted to a different profession entirely.  The specialty_ids
    are kept as-is from the original record and profession_type_id is set to NULL.

    Returns a dict mapping trigger_specialty_id → {
        "profession_parent_id": int,        # new parent_profession_id
        "profession_type_id": int | None, # new profession_type_id (None = NULL)
        "notes":              str,
    }

    Required columns : trigger_specialty_id, profession_parent_id
    Optional columns : profession_type_id (blank → None/NULL), notes
    """
    if not _PROFESSION_OVERRIDES_CSV.exists():
        logger.info("No profession_overrides.csv found — skipping profession overrides.")
        return {}

    df = pd.read_csv(_PROFESSION_OVERRIDES_CSV, dtype=str)
    df.columns = df.columns.str.strip()
    overrides: dict[int, dict] = {}
    for _, row in df.iterrows():
        trigger  = _to_int(row.get("trigger_specialty_id"))
        prof_id  = _to_int(row.get("profession_parent_id"))
        prof_type = _to_int(row.get("profession_type_id"))   # None when blank
        if trigger is not None:
            overrides[trigger] = {
                "profession_id":      prof_id,   # None means set profession_parent_id to NULL
                "profession_type_id": prof_type,
                "notes":              str(row.get("notes") or "").strip(),
            }
            logger.debug(
                "Profession override loaded: specialty ID %d → profession %s (type=%s)  (%s)",
                trigger, prof_id, prof_type, row.get("notes", ""),
            )

    logger.info(
        "Profession overrides loaded: %d entry/entries from %s",
        len(overrides), _PROFESSION_OVERRIDES_CSV.name,
    )
    return overrides


def _validate_id_overrides(
    id_overrides: dict,
    specialty_lookup: dict,
    subspecialty_lookup: dict,
) -> None:
    """
    Warn loudly (and abort) if any override CSV row references an ID that does
    not exist in the specialty or subspecialty map.

    Two failure modes prevented:
      - mapped_id not in map  → override silently ignored, record stays no_match
      - parent_id not in map  → phantom ID written into specialty_ids_recommended
    """
    bad = []
    for orig_id, ov in id_overrides.items():
        mapped = ov["mapped_id"]
        parent = ov.get("parent_id")

        if mapped is None:
            continue  # delete override — no target ID to validate

        if mapped not in specialty_lookup and mapped not in subspecialty_lookup:
            bad.append(
                f"  original_id={orig_id}: mapped_id={mapped} not found in specialty or subspecialty map"
            )

        if parent is not None and parent not in specialty_lookup and parent not in subspecialty_lookup:
            bad.append(
                f"  original_id={orig_id}: parent_id={parent} not found in specialty or subspecialty map"
            )

    if bad:
        msg = "\n".join(bad)
        logger.error(
            "specialty_id_overrides.csv contains %d invalid ID(s):\n%s\n"
            "Fix the CSV before running — aborting to prevent bad output.",
            len(bad), msg,
        )
        print(f"\n  ERROR: Invalid entries in specialty_id_overrides.csv:\n{msg}")
        print("  Fix the CSV and re-run.\n")
        raise SystemExit(1)

    logger.info("Override validation passed — all mapped_id / parent_id values exist in the map.")


def fetch_candidates(exclude_ids: set[int], limit: int | None, order_desc: bool = False) -> pd.DataFrame:
    """
    Return subscribers with profession_parent_id in _PROFESSION_PARENT_IDS,
    excluding any IDs already migrated to the physicians table.

    On the first run the data is fetched from the DB and saved to
    _CACHE_CSV (data/input/newsletter_table.csv) for subsequent runs.
    Delete that file to force a fresh DB query.
    """
    if _CACHE_CSV.exists():
        logger.info("Cache file found — loading from %s (delete to force fresh DB query)", _CACHE_CSV)
        df = pd.read_csv(
            _CACHE_CSV,
            dtype={"id": "Int64", "profession_parent_id": "Int64", "profession_type_id": "Int64",
                   "firstname": str, "lastname": str, "specialty_ids": str,
                   "subscription_status": "Int64", "email_status": str},
        )
        # Replace pandas <NA> strings that come from nullable int columns
        df["specialty_ids"] = df["specialty_ids"].where(df["specialty_ids"].notna(), other=None)
        logger.info("Loaded %d row(s) from cache", len(df))
    else:
        logger.info("No cache file — querying %s ...", _SOURCE_TABLE)
        pid_list = ", ".join(str(p) for p in _PROFESSION_PARENT_IDS)
        query = text(f"""
            SELECT id, firstname, lastname, profession_parent_id, profession_type_id,
                   specialty_ids, subscription_status, email_status
            FROM `{_SOURCE_TABLE}`
            WHERE profession_parent_id IN ({pid_list})
              AND user_type NOT IN (1, 2, 5, 7, 8, 10)
              AND country_id = 1
            ORDER BY id {'DESC' if order_desc else 'ASC'}
        """)
        with engine.connect() as conn:
            df = pd.read_sql(query, conn)

        _CACHE_CSV.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(_CACHE_CSV, index=False)
        logger.info(
            "Fetched %d row(s) — saved to %s for future runs", len(df), _CACHE_CSV
        )

    total = len(df)
    if exclude_ids:
        df = df[~df["id"].isin(exclude_ids)].reset_index(drop=True)

    logger.info(
        "%d total | %d skipped (already migrated) | %d new to process",
        total, total - len(df), len(df),
    )

    if limit is not None:
        df = df.head(limit)
        logger.info("Applying --limit %d: processing %d subscriber(s) this run", limit, len(df))

    return df


def fetch_db_specialties(ids: list[int]) -> dict[int, dict]:
    """Batch-fetch name and parent_id from tbl_master_specialities for the given IDs."""
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


def _name_strategies(db_first: str, db_last: str) -> list[tuple[str, str]]:
    """
    Return (first, last) pairs to try for NPPES, ordered by most likely match.

    Handles the common data-entry variation where users paste their full name
    into the firstname field (e.g. firstname="Swathi Mahesh Mahesh", lastname="").
    """
    db_first = db_first.strip()
    db_last  = db_last.strip()
    words    = db_first.split()

    pairs = []

    if db_first and db_last:
        # Strategy 1 (standard): first word of firstname + lastname as-is
        pairs.append((words[0], db_last))

    if len(words) >= 2:
        # Strategy 2: first word + last word of firstname
        # Covers "Swathi Mahesh Mahesh" → ("Swathi", "Mahesh")
        if (words[0], words[-1]) not in pairs:
            pairs.append((words[0], words[-1]))

    if db_first and db_last and (db_first, db_last) not in pairs:
        # Strategy 3 (fallback): full firstname + lastname verbatim
        pairs.append((db_first, db_last))

    return [(f, l) for f, l in pairs if f and l]


def _nppes_call(session: requests.Session, first: str, last: str) -> list:
    """Single NPPES API call; returns raw results list."""
    resp = session.get(
        _NPPES_URL,
        params={
            "first_name":       first,
            "last_name":        last,
            "enumeration_type": "NPI-1",
            "version":          "2.1",
            "limit":            5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results") or []


def nppes_lookup_single(session: requests.Session, db_first: str, db_last: str) -> dict:
    """
    Search NPPES by name, trying multiple first/last combinations to handle
    cases where the full name was entered in the firstname field.

    Returns a context dict:
        tried          : True if at least one call was attempted
        hit_count      : number of results from the winning strategy (or last tried)
        strategy_used  : (first, last) pair that produced a 1:1 hit
        strategies_tried: list of (first, last) pairs attempted
        data           : single-result dict (npi + taxonomies) if a 1:1 hit found, else None
    """
    ctx = {
        "tried": False,
        "hit_count": 0,
        "strategy_used": None,
        "strategies_tried": [],
        "data": None,
    }

    strategies = _name_strategies(db_first, db_last)
    if not strategies:
        return ctx

    for first, last in strategies:
        ctx["tried"] = True
        ctx["strategies_tried"].append((first, last))
        try:
            results = _nppes_call(session, first, last)
            ctx["hit_count"] = len(results)
            if len(results) == 1:
                ctx["strategy_used"] = (first, last)
                r = results[0]
                ctx["data"] = {
                    "npi":        str(r.get("number", "")),
                    "first_name": r.get("basic", {}).get("first_name", ""),
                    "last_name":  r.get("basic", {}).get("last_name", ""),
                    "taxonomies": [
                        {
                            "code":    t.get("code", "").strip().upper(),
                            "desc":    t.get("desc", ""),
                            "primary": t.get("primary", False),
                        }
                        for t in r.get("taxonomies", [])
                    ],
                }
                logger.debug(
                    "NPPES 1:1 hit for strategy ('%s', '%s')", first, last
                )
                return ctx  # found a definitive match — stop trying
            logger.debug(
                "NPPES strategy ('%s', '%s') → %d result(s), trying next",
                first, last, len(results),
            )
            time.sleep(_NPPES_DELAY)
        except Exception as exc:
            logger.warning("NPPES call failed for ('%s', '%s'): %s", first, last, exc)

    return ctx


def _analyze_nppes_path(nppes_data: dict, taxonomy_lookup: dict) -> dict:
    """Derive eMed specialty IDs from NPPES taxonomy codes via the map."""
    taxonomy_details     = []
    specialty_ids_out    = []
    subspecialty_ids_out = []
    md_signals = do_signals = dpm_signals = 0

    for t in nppes_data.get("taxonomies", []):
        code  = t["code"].upper()
        entry = taxonomy_lookup.get(code)

        if entry:
            sid  = entry.get("specialty_id")
            ssid = entry.get("subspecialty_id")
            if sid  and sid  not in specialty_ids_out:    specialty_ids_out.append(sid)
            if ssid and ssid not in subspecialty_ids_out: subspecialty_ids_out.append(ssid)
            if entry["md"]:  md_signals  += 1
            if entry["do"]:  do_signals  += 1
            if entry["dpm"]: dpm_signals += 1
            map_match = "matched"
        else:
            map_match = "no_match"
            entry     = None

        taxonomy_details.append({
            "nppes_code": code,
            "nppes_desc": t["desc"],
            "primary":    t["primary"],
            "map_match":  map_match,
            "map_entry":  entry,
        })

    matched    = sum(1 for d in taxonomy_details if d["map_match"] == "matched")
    total_tax  = len(taxonomy_details)
    if total_tax == 0:
        match_type = "no_specialty_data"
    elif matched == total_tax:
        match_type = "full_match"
    elif matched > 0:
        match_type = "partial_match"
    else:
        match_type = "no_match"

    # Combine specialty + subspecialty IDs, deduped, specialty-first
    recommended = specialty_ids_out + [i for i in subspecialty_ids_out if i not in specialty_ids_out]

    return {
        "taxonomy_details":       taxonomy_details,
        "recommended_ids":        recommended,
        "md_signals":             md_signals,
        "do_signals":             do_signals,
        "dpm_signals":            dpm_signals,
        "matched_count":          matched,
        "match_type":             match_type,
    }


def analyze_record(
    row,
    specialty_lookup: dict,
    subspecialty_lookup: dict,
    spec_name_lookup: dict,
    subspec_name_lookup: dict,
    db_specialties: dict,
    taxonomy_lookup: dict,
    nppes_context: dict | None = None,
    id_overrides: dict | None = None,
    profession_overrides: dict | None = None,
) -> dict:
    """
    Make an educated guess at the correct profession data for a subscriber.

    Rule 1 — Too many specialties (> _MAX_SPECIALTIES_TRUST):
        The DB specialty_ids are likely unreliable. Look the person up in NPPES
        by name. If there is exactly one hit, take the taxonomy code(s) from NPPES,
        map them to eMed IDs, and use those as the recommended specialty_ids.
        If NPPES returns 0 or multiple hits, fall back to Rules 2/3 with a note.

    Rule 2 — Direct / parent ID match:
        Check each specialty_id directly against specialty_lookup /
        subspecialty_lookup (direct match), or against the parent specialty of a
        subspecialty (parent match). Recommended ID = the DB specialty_id.

    Rule 3 — Name-based match:
        When Rules 1 & 2 fail, look up the specialty's name in tbl_master_specialities
        and search the map for an entry with the same name. When found, the
        recommended ID is the MAP's ID (not the DB's ID), since the DB may carry a
        duplicate or legacy eMed ID for the same concept.

    Rule 4 — Manual override (specialty_id_overrides.csv):
        When Rules 1–3 all fail, consult the manual override table. If the DB ID
        appears there, use the mapped eMed ID directly. Add new rows to
        data/input/specialty_id_overrides.csv to extend this without code changes.
    """
    specialty_ids = _parse_ids(row["specialty_ids"])

    # ── Profession override check (runs before all specialty rules) ───────────
    # When any specialty_id in this record matches a profession_overrides entry,
    # the entire record is rerouted to a different profession.  Specialty IDs
    # are kept verbatim from the original record; profession_type_id is forced
    # to NULL (e.g. Dentists have no MD/DO distinction).
    if profession_overrides:
        for sid in specialty_ids:
            if sid in profession_overrides:
                ov = profession_overrides[sid]
                return {
                    "method":                       "profession_override",
                    "match_type":                   "profession_override",
                    "details":                      [],
                    "nppes_taxonomy_details":       [],
                    "md_signals":                   0,
                    "do_signals":                   0,
                    "dpm_signals":                  0,
                    "matched_count":                0,
                    "recommended_specialty_ids":    specialty_ids,
                    "guessed_profession_parent_id": ov["profession_id"],
                    "guessed_profession_id":        ov.get("profession_type_id"),
                    "prof_type_note":               (
                        f"profession_override → profession_id={ov['profession_id']} "
                        f"(triggered by specialty_id={sid})"
                    ),
                    "nppes_context":                None,
                    "force_null_profession_type":   True,
                    "profession_override_trigger":  sid,
                    "profession_override_note":     ov.get("notes", ""),
                }

    # ── DB specialty match ────────────────────────────────────────────────────
    method = "db_match"

    details = []
    tax_details = []
    md_signals = do_signals = dpm_signals = matched_count = 0
    matched_ids = []

    # Pre-scan: identify which MAP specialty IDs are represented in this
    # subscriber's data. Used in Rule 3 to pick the right subspecialty
    # when the same name exists under multiple parent specialties.
    map_specialty_ids_present = set()
    for sid in specialty_ids:
        if specialty_lookup.get(sid):
            map_specialty_ids_present.add(sid)
        else:
            pre_info = db_specialties.get(sid, {})
            if pre_info:
                hit = spec_name_lookup.get((pre_info.get("name") or "").lower())
                if hit:
                    map_specialty_ids_present.add(hit[0])

    for sid in specialty_ids:
        # Delete override: blank mapped_id in specialty_id_overrides.csv → remove this ID entirely
        if id_overrides and sid in id_overrides and id_overrides[sid]["mapped_id"] is None:
            db_info   = db_specialties.get(sid, {})
            db_name   = db_info.get("name", "(not in DB)") if db_info else "(not in DB)"
            parent_id = db_info.get("parent_id") if db_info else None
            if not db_info:
                db_type = "not_in_db"
            elif parent_id == 0:
                db_type = "specialty"
            else:
                db_type = f"subspecialty (parent_id={parent_id})"
            details.append({
                "specialty_id":   sid,
                "recommended_id": None,
                "db_name":        db_name,
                "db_type":        db_type,
                "match_kind":     "override_remove",
                "match_note":     "manual override — removed from recommendation",
                "map_info":       None,
            })
            continue

        db_info   = db_specialties.get(sid, {})
        db_name   = db_info.get("name", "(not in DB)") if db_info else "(not in DB)"
        parent_id = db_info.get("parent_id") if db_info else None

        if not db_info:
            db_type = "not_in_db"
        elif parent_id == 0:
            db_type = "specialty"
        else:
            db_type = f"subspecialty (parent_id={parent_id})"

        # Rule 2a: direct ID match
        map_info           = specialty_lookup.get(sid) or subspecialty_lookup.get(sid)
        recommended_id     = sid
        match_note         = ""
        inject_parent      = True
        override_parent_id = None

        if map_info:
            match_kind = "direct"
        elif db_info:
            # Rule 3: name-based match
            name_key   = _norm_name(db_name)
            spec_hit   = spec_name_lookup.get(name_key)
            if spec_hit:
                match_kind     = "name_match"
                map_info       = spec_hit[1]
                recommended_id = spec_hit[0]
                match_note     = "specialty name match"
            else:
                subspec_hits = subspec_name_lookup.get(name_key, [])
                if subspec_hits:
                    # Prefer the hit whose parent specialty is present
                    # in this subscriber's confirmed data
                    preferred = next(
                        ((ssid, info) for ssid, parent_sid, info in subspec_hits
                         if parent_sid in map_specialty_ids_present),
                        None,
                    )
                    if preferred:
                        match_kind     = "name_match"
                        map_info       = preferred[1]
                        recommended_id = preferred[0]
                        # Find which parent was matched for the note
                        parent_name = next(
                            (info["name"] for ssid, parent_sid, info in subspec_hits
                             if ssid == preferred[0]),
                            ""
                        )
                        # Look up the parent specialty name from map
                        confirmed_parent = next(
                            (parent_sid for ssid, parent_sid, _ in subspec_hits
                             if ssid == preferred[0]),
                            None,
                        )
                        parent_entry = specialty_lookup.get(confirmed_parent)
                        match_note = (
                            f"subspecialty of {parent_entry['name']} "
                            f"(ID={confirmed_parent}) — parent confirmed in this record"
                            if parent_entry else "parent specialty confirmed"
                        )
                    else:
                        # No parent context — take first, flag ambiguity
                        first = subspec_hits[0]
                        match_kind     = "name_match"
                        map_info       = first[2]
                        recommended_id = first[0]
                        if len(subspec_hits) > 1:
                            inject_parent = False
                        match_note     = (
                            f"no parent context — first available match used"
                            + (f" ({len(subspec_hits)} candidates)" if len(subspec_hits) > 1 else "")
                        )
                else:
                    match_kind = "no_match"
                    map_info   = None
        else:
            match_kind = "no_match"
            map_info   = None

        # Rule 4: manual override — consult specialty_id_overrides.csv
        if match_kind == "no_match" and id_overrides and sid in id_overrides:
            ov              = id_overrides[sid]
            override_target = ov["mapped_id"]
            override_parent_id = ov.get("parent_id")
            override_info   = specialty_lookup.get(override_target) or subspecialty_lookup.get(override_target)
            if override_info:
                match_kind     = "override_match"
                map_info       = override_info
                recommended_id = override_target
                parent_note    = f", parent={override_parent_id}" if override_parent_id else ""
                match_note     = f"manual override → eMed ID {override_target} ({override_info['name']}{parent_note})"

        if map_info:
            matched_count += 1
            if inject_parent:
                for parent_sid, subspec_ids in _PARENT_SPECIALTY_INJECTION.items():
                    if recommended_id in subspec_ids and parent_sid not in matched_ids:
                        matched_ids.append(parent_sid)
                # For override matches: also inject parent explicitly declared in the CSV
                if override_parent_id and override_parent_id not in matched_ids:
                    matched_ids.append(override_parent_id)
            matched_ids.append(recommended_id)
            if map_info["md"]:  md_signals  += 1
            if map_info["do"]:  do_signals  += 1
            if map_info["dpm"]: dpm_signals += 1

        details.append({
            "specialty_id":   sid,
            "recommended_id": recommended_id if map_info else None,
            "db_name":        db_name,
            "db_type":        db_type,
            "match_kind":     match_kind,
            "match_note":     match_note,
            "map_info":       map_info,
        })

    removed_count  = sum(1 for d in details if d["match_kind"] == "override_remove")
    effective_total = len(specialty_ids) - removed_count
    if not specialty_ids:
        match_type = "no_specialty_data"
    elif matched_count == effective_total:
        match_type = "full_match"
    elif matched_count > 0:
        match_type = "partial_match"
    else:
        match_type = "no_match"

    recommended_ids = matched_ids  # only IDs that matched the map

    # ── Determine profession type from signals ────────────────────────────────
    if matched_count == 0 or match_type == "no_specialty_data":
        guessed_parent  = None
        guessed_prof_id = None
        prof_type_note  = "NULL  (no match — cannot determine)"
    else:
        guessed_parent = 167
        md, do, dpm = md_signals, do_signals, dpm_signals
        if md > 0 and do == 0 and dpm == 0:
            guessed_prof_id = _MD_PROFESSION_TYPE_ID
            prof_type_note  = f"{_MD_PROFESSION_TYPE_ID}  (MD, signals={md})"
        elif do > 0 and md == 0 and dpm == 0:
            guessed_prof_id = _DO_PROFESSION_TYPE_ID
            prof_type_note  = f"{_DO_PROFESSION_TYPE_ID}  (DO, signals={do})"
        elif dpm > 0 and md == 0 and do == 0:
            guessed_prof_id = _DPM_PROFESSION_TYPE_ID
            prof_type_note  = f"{_DPM_PROFESSION_TYPE_ID}  (DPM, signals={dpm})"
        else:
            guessed_prof_id = None
            prof_type_note  = f"NULL  (mixed — MD={md} DO={do} DPM={dpm})"

    return {
        "method":                       method,
        "match_type":                   match_type,
        "details":                      details,
        "nppes_taxonomy_details":       tax_details,
        "md_signals":                   md_signals,
        "do_signals":                   do_signals,
        "dpm_signals":                  dpm_signals,
        "matched_count":                matched_count,
        "recommended_specialty_ids":    recommended_ids,
        "guessed_profession_parent_id": guessed_parent,
        "guessed_profession_id":        guessed_prof_id,
        "prof_type_note":               prof_type_note,
        "nppes_context":                nppes_context,
    }


def _signal_flags(map_info: dict) -> str:
    parts = [
        "MD✓"  if map_info["md"]  else "MD✗",
        "DO✓"  if map_info["do"]  else "DO✗",
        "DPM✓" if map_info["dpm"] else "DPM✗",
    ]
    return "  ".join(parts)


def print_analysis(row, analysis: dict, index: int, total: int, out) -> None:
    W     = 68
    thick = "═" * W
    thin  = "─" * W
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    first = str(row.get("firstname") or "").strip()
    last  = str(row.get("lastname")  or "").strip()
    name  = f"{first} {last}".strip() or "(no name)"

    print(f"\n[{ts}]", file=out)
    print(f"{thick}", file=out)
    print(f"  [{index}/{total}]  Subscriber ID: {row['id']}  |  {name}", file=out)
    print(f"  profession_parent_id = {row['profession_parent_id']}", file=out)
    print(f"  DB specialty_ids     = {str(row['specialty_ids'] or '(none)').strip()}", file=out)

    # ── Analysis detail ───────────────────────────────────────────────────────
    print(thin, file=out)

    method = analysis["method"]

    if method == "profession_override":
        trigger  = analysis.get("profession_override_trigger")
        note     = analysis.get("profession_override_note", "")
        prof_id  = analysis.get("guessed_profession_parent_id")
        orig_ids = analysis.get("recommended_specialty_ids") or []
        print(f"  PROFESSION OVERRIDE: specialty ID {trigger} matched profession_overrides.csv", file=out)
        print(f"  → New profession_parent_id : {prof_id}", file=out)
        print(f"  → profession_type_id       : NULL (cleared — no MD/DO distinction for this profession)", file=out)
        print(f"  → specialty_ids kept as-is : {', '.join(str(i) for i in orig_ids) or '(none)'}", file=out)
        if note:
            print(f"  Note: {note}", file=out)

    elif method == "nppes_match":
        ctx = analysis["nppes_context"]
        npi = ctx["data"]["npi"]
        print(f"  RULE APPLIED: >3 DB specialties — NPPES lookup used (1 hit, NPI={npi})", file=out)
        print(file=out)
        tax_details = analysis["nppes_taxonomy_details"]
        if not tax_details:
            print("  (no taxonomy codes returned from NPPES)", file=out)
        else:
            for t in tax_details:
                primary_flag = " [PRIMARY]" if t["primary"] else ""
                print(f"  NPPES code: {t['nppes_code']}{primary_flag}  —  {t['nppes_desc']}", file=out)
                if t["map_match"] == "matched":
                    e = t["map_entry"]
                    print(f"             Map: MATCHED   {_signal_flags(e)}", file=out)
                    if e.get("specialty_id"):
                        print(f"             → specialty     : \"{e['specialty_name']}\" (ID={e['specialty_id']})", file=out)
                    if e.get("subspecialty_id"):
                        print(f"             → sub-specialty : \"{e['subspecialty_name']}\" (ID={e['subspecialty_id']})", file=out)
                else:
                    print(f"             Map: NO MATCH  (code not in physician_md_do.csv / physician_dpm.csv)", file=out)
                print(file=out)

    elif method in ("nppes_tried_no_hit", "nppes_skipped_no_name"):
        ctx = analysis.get("nppes_context") or {}
        if method == "nppes_tried_no_hit":
            hits       = ctx.get("hit_count", 0)
            tried_list = ", ".join(f"('{f}', '{l}')" for f, l in ctx.get("strategies_tried", []))
            print(f"  RULE APPLIED: >3 DB specialties — NPPES tried, no 1:1 hit (last result: {hits})", file=out)
            print(f"  Strategies tried: {tried_list or '(none)'}", file=out)
        else:
            print(f"  RULE APPLIED: >3 DB specialties — NPPES skipped (firstname/lastname missing)", file=out)
        print(f"  Falling back to DB specialty match:\n", file=out)
        _print_db_details(analysis["details"], out)

    else:
        _print_db_details(analysis["details"], out)

    # ── Signals ───────────────────────────────────────────────────────────────
    print(thin, file=out)
    print(f"  Signals    :  MD={analysis['md_signals']}  DO={analysis['do_signals']}  DPM={analysis['dpm_signals']}", file=out)
    print(f"  Match type :  {analysis['match_type']}", file=out)

    # ── Recommendation ────────────────────────────────────────────────────────
    print(thin, file=out)
    print(f"  RECOMMENDATION", file=out)
    rec_ids      = analysis["recommended_specialty_ids"]
    prof_type_id = row.get("profession_type_id")
    prof_type_display = (
        str(int(prof_type_id))
        if prof_type_id is not None and not pd.isna(prof_type_id)
        else "NULL"
    )
    print(f"  profession_parent_id : {analysis['guessed_profession_parent_id'] or 'NULL'}", file=out)
    print(f"  profession_type_id   : {prof_type_display}", file=out)
    print(f"  specialty_ids        : {', '.join(str(i) for i in rec_ids) if rec_ids else 'NULL'}", file=out)
    print(thick, file=out)


def _print_db_details(details: list, out) -> None:
    if not details:
        print("  (no specialty_ids to analyze)", file=out)
        return
    for d in details:
        sid        = d["specialty_id"]
        match_kind = d["match_kind"]
        map_info   = d["map_info"]
        print(f"  ID={sid:<6}  DB name  : {d['db_name']}", file=out)
        print(f"            DB type  : {d['db_type']}", file=out)
        if match_kind == "direct":
            print(f"            Map      : DIRECT MATCH   {_signal_flags(map_info)}", file=out)
            print(f"                       Map name: \"{map_info['name']}\"", file=out)
        elif match_kind == "parent_match":
            print(f"            Map      : PARENT MATCH   {_signal_flags(map_info)}", file=out)
            print(f"                       Parent name: \"{map_info['name']}\" (subspecialty not in map; parent matched)", file=out)
        elif match_kind == "name_match":
            rec_id     = d.get("recommended_id")
            match_note = d.get("match_note", "")
            print(f"            Map      : NAME MATCH → use ID={rec_id}   {_signal_flags(map_info)}", file=out)
            print(f"                       Map name: \"{map_info['name']}\"", file=out)
            if match_note:
                print(f"                       Note: {match_note}", file=out)
        elif match_kind == "override_match":
            rec_id     = d.get("recommended_id")
            match_note = d.get("match_note", "")
            print(f"            Map      : OVERRIDE MATCH → use ID={rec_id}   {_signal_flags(map_info)}", file=out)
            print(f"                       Map name: \"{map_info['name']}\"", file=out)
            if match_note:
                print(f"                       Note: {match_note}", file=out)
        elif match_kind == "override_remove":
            match_note = d.get("match_note", "")
            print(f"            Map      : OVERRIDE REMOVE — excluded from recommendation", file=out)
            if match_note:
                print(f"                       Note: {match_note}", file=out)
        else:
            print(f"            Map      : NO MATCH", file=out)
        print(file=out)


# ── Output logs ───────────────────────────────────────────────────────────────

def load_all_processed_ids() -> set[int]:
    """
    Read all four output CSVs and return the union of IDs already processed.
    Any ID found in any file is skipped on the next run.
    """
    all_ids: set[int] = set()
    for path in _ALL_OUTPUT_CSVS:
        if not path.exists():
            continue
        try:
            df      = pd.read_csv(path, dtype=str)
            file_ids = set(df["id"].dropna().astype(int).tolist())
            logger.info("  %-35s %d ID(s)", path.name, len(file_ids))
            all_ids |= file_ids
        except Exception as exc:
            logger.warning("Could not read %s (%s) — skipping", path.name, exc)
    logger.info("Total already-processed ID(s): %d", len(all_ids))
    return all_ids


def _append_to_csv(path: Path, row, analysis: dict, extra_cols: dict | None = None) -> None:
    """Write one record to the given CSV (creates with header if needed, else appends)."""
    def _safe_int(val) -> str:
        try:
            return str(int(val)) if val is not None and not pd.isna(val) else ""
        except (TypeError, ValueError):
            return ""

    prof_type_orig = _safe_int(row.get("profession_type_id"))

    if analysis.get("force_null_profession_type"):
        prof_type_rec = ""
    elif prof_type_orig:
        prof_type_rec = prof_type_orig
    elif analysis.get("guessed_profession_id") is not None:
        prof_type_rec = str(analysis["guessed_profession_id"])
    else:
        prof_type_rec = ""

    rec_ids = analysis.get("recommended_specialty_ids") or []

    raw_sub = row.get("subscription_status")
    try:
        sub_key = int(raw_sub) if raw_sub is not None and not pd.isna(raw_sub) else None
    except (TypeError, ValueError):
        sub_key = None
    sub_label = _SUBSCRIPTION_STATUS_MAP.get(sub_key, "Unknown") if sub_key is not None else "Unknown"

    raw_email = row.get("email_status")
    email_label = (
        "Good"
        if raw_email is None or (isinstance(raw_email, float) and pd.isna(raw_email)) or str(raw_email).strip() == ""
        else str(raw_email).strip()
    )

    record = {
        "id":                               _safe_int(row["id"]),
        "parent_profession_id_orig":        _safe_int(row.get("profession_parent_id")),
        "profession_type_id_orig":          prof_type_orig,
        "specialty_ids_orig":               str(row.get("specialty_ids") or "").strip(),
        "parent_profession_id_recommended": _safe_int(analysis.get("guessed_profession_parent_id")),
        "profession_type_id_recommended":   prof_type_rec,
        "specialty_ids_recommended":        ",".join(str(i) for i in rec_ids),
        "subscription_status":              sub_label,
        "email_status":                     email_label,
    }

    if extra_cols:
        record.update(extra_cols)

    columns = _OUTPUT_COLUMNS + (list(extra_cols.keys()) if extra_cols else [])
    df = pd.DataFrame([record], columns=columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    df.to_csv(path, mode="a", header=write_header, index=False)


def write_result(row, analysis: dict) -> None:
    """Route the analyzed record to the correct output CSV based on match_type."""
    match_type = analysis.get("match_type", "")
    if match_type == "profession_override":
        extra = {
            "trigger_specialty_id":  str(analysis.get("profession_override_trigger", "")),
            "profession_override_note": analysis.get("profession_override_note", ""),
        }
        _append_to_csv(_PROFESSION_OVERRIDE_CSV, row, analysis, extra_cols=extra)
    elif match_type == "full_match":
        _append_to_csv(_CLEAN_CSV, row, analysis)
    elif match_type == "partial_match":
        unmatched = [d for d in analysis.get("details", []) if d["match_kind"] == "no_match"]
        extra = {
            "unmatched_ids_orig": ",".join(str(d["specialty_id"]) for d in unmatched),
            "unmatched_names":    "|".join(d["db_name"] for d in unmatched),
        }
        _append_to_csv(_PARTIAL_CSV, row, analysis, extra_cols=extra)
    elif match_type == "no_match":
        _append_to_csv(_MISMATCH_CSV, row, analysis)
    else:
        _append_to_csv(_NO_SPECIALTY_CSV, row, analysis)


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(debug: bool = False) -> None:
    """Print a run summary with counts from every output file and a checksum."""
    def _count(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            return len(pd.read_csv(path, dtype=str))
        except Exception:
            return 0

    total_cache  = _count(_CACHE_CSV)
    n_clean      = _count(_CLEAN_CSV)
    n_partial    = _count(_PARTIAL_CSV)
    n_mismatch   = _count(_MISMATCH_CSV)
    n_no_spec    = _count(_NO_SPECIALTY_CSV)
    n_prof_ov    = _count(_PROFESSION_OVERRIDE_CSV)
    n_processed  = n_clean + n_partial + n_mismatch + n_no_spec + n_prof_ov
    n_remaining  = total_cache - n_processed
    checksum_ok  = (n_processed + n_remaining) == total_cache

    W   = 58
    sep = "─" * W
    print(f"\n{sep}")
    print(f"  RUN SUMMARY")
    print(sep)
    print(f"  {'Total records in newsletter_table.csv':<38} {total_cache:>6}")
    print(sep)
    print(f"  {'Records in Clean  (full_match)':<38} {n_clean:>6}")
    print(f"  {'Records in Partial  (partial_match)':<38} {n_partial:>6}")
    print(f"  {'Records in Mismatch  (no_match)':<38} {n_mismatch:>6}")
    print(f"  {'Records in No Specialty  (no_specialty_data)':<38} {n_no_spec:>6}")
    print(f"  {'Records in Profession Override':<38} {n_prof_ov:>6}")
    print(sep)
    print(f"  {'Total processed':<38} {n_processed:>6}")
    print(f"  {'Still not processed':<38} {n_remaining:>6}")
    print(sep)
    checksum_label = "✓ PASS" if checksum_ok else "✗ FAIL"
    print(f"  Checksum: {n_clean} + {n_partial} + {n_mismatch} + {n_no_spec} + {n_prof_ov} + {n_remaining} = {total_cache}  [{checksum_label}]")
    if debug:
        print(sep)
        print(f"  Detail log: {_LOG_FILE}")
    print(sep)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Review physician specialty maps interactively, then analyze "
            "subscriber records against those maps."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Max number of new subscriber records to analyze (default: all)",
    )
    parser.add_argument(
        "--order",
        type=str,
        default=None,
        metavar="DIR",
        help="Sort order for candidate IDs: DESC for newest-first (default: ASC)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Write detailed analysis to a log file (default: disabled)",
    )
    args = parser.parse_args()

    if not check_connection():
        raise SystemExit("Cannot reach the database — aborting.")

    combined = load_combined_map()

    if not combined:
        logger.error("No entries loaded — nothing to review.")
        sys.exit(1)

    pool    = list(range(len(combined)))
    shown   = 0

    print(f"\nLoaded {len(combined)} mapping entry/entries across both CSVs.")
    print("Press Enter to see a random entry, or type 'n' / 'no' to stop.\n")

    while pool:
        idx   = random.choice(pool)
        pool.remove(idx)
        shown += 1
        show_entry(combined[idx], shown, len(combined))

        if not pool:
            print("\nAll entries have been shown.")
            break

        try:
            answer = input("\n  Show another? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if answer in ("n", "no"):
            print("\nDone reviewing. Goodbye!")
            break

    print(f"\n{'─' * 62}")
    print("  Checking database for physician subscribers table ...")
    print(f"{'─' * 62}")
    ensure_physician_table()

    # ── Analysis step ──────────────────────────────────────────────────────────
    print(f"\n{'═' * 66}")
    print("  ANALYZING SUBSCRIBERS ...")
    print(f"{'═' * 66}\n")

    specialty_lookup, subspecialty_lookup = build_lookups(combined)
    spec_name_lookup, subspec_name_lookup = build_name_lookups(combined)
    taxonomy_lookup  = build_taxonomy_lookup(combined)
    id_overrides        = load_id_overrides()
    _validate_id_overrides(id_overrides, specialty_lookup, subspecialty_lookup)
    profession_overrides = load_profession_overrides()
    order_desc      = isinstance(args.order, str) and args.order.strip().upper() == "DESC"
    logger.info("Checking already-processed IDs across all output files ...")
    already_processed = load_all_processed_ids()
    candidates        = fetch_candidates(already_processed, args.limit, order_desc=order_desc)

    if candidates.empty:
        print("  No new subscriber records to analyze.")
        return

    all_spec_ids = sorted({
        sid
        for _, row in candidates.iterrows()
        for sid in _parse_ids(row["specialty_ids"])
    })
    db_specialties = fetch_db_specialties(all_spec_ids)
    logger.info("Fetched %d specialty name(s) from tbl_master_specialities", len(db_specialties))

    logger.info("Processing %d candidate(s)", len(candidates))

    if args.debug:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total  = len(candidates)
    log_target = _LOG_FILE if args.debug else os.devnull

    with open(log_target, "a", encoding="utf-8") as log:
        if args.debug:
            log.write(f"\n{'═' * 68}\n")
            log.write(f"  RUN STARTED: {run_ts}  |  {total} candidate(s) to process\n")
            log.write(f"{'═' * 68}\n")

        for i, (_, row) in enumerate(candidates.iterrows(), start=1):
            analysis = analyze_record(
                row, specialty_lookup, subspecialty_lookup,
                spec_name_lookup, subspec_name_lookup,
                db_specialties, taxonomy_lookup,
                id_overrides=id_overrides,
                profession_overrides=profession_overrides,
            )
            print_analysis(row, analysis, i, total, out=log)
            write_result(row, analysis)
            logger.info("[%d/%d] ID=%s → %s", i, total, row["id"], analysis["match_type"])

    print_summary(debug=args.debug)


if __name__ == "__main__":
    main()
