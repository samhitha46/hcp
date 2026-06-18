"""
Identify all Physician-variant records in tbl_system_params and report
their subscriber impact, plus flag unmatched profession IDs as candidates
for review.

Part A — physician_variants.csv
  Fetches every active Profession entry under MedicalResources, then flags
  any record whose KeyValue contains or closely resembles "physician":
    direct    — "physician", "surgeon", or "anesthesiolog" appears as a
                substring (case-insensitive)
    title     — a token exactly matches a known physician title
                (M.D., D.O., doctor, hospitalist)
    fuzzy     — a token scores >= FUZZY_THRESHOLD against "physician" via
                difflib.SequenceMatcher (catches typos like "Physican")

Part B — shadow_physician_impact.csv
  Takes the shadow physician IDs (all variants except the canonical id=167),
  queries tbl_newsletter_subscribers for records where profession_parent_id
  is in that set, and reports the count grouped by user_type.

Part C — physician_candidates_for_review.csv
  Pulls every distinct profession_parent_id actually used by subscribers,
  excludes IDs already captured as physician variants, and reports the
  remainder with subscriber counts and candidate flags so a human can decide
  whether any additional IDs should be treated as physicians.  Candidate
  flags highlight:
    specialty_suffix     — token ends with a medical specialty suffix
                           (-ologist, -iatrist, -iatrician); catches
                           Cardiologist, Dermatologist, Neurologist, etc.
    medical_title        — token is an informal physician title not caught
                           by Part A (e.g. "dr", "dr.")
    loose_fuzzy(N)       — token is loosely similar to "physician" (score N,
                           below the definitive threshold)
    id_not_in_system_params — the ID has no matching row in tbl_system_params

Output:
  data/processed/physician/physician_variants.csv
  data/processed/physician/shadow_physician_impact.csv
  data/processed/physician/physician_candidates_active.csv
  data/processed/physician/physician_candidates_other_status.csv
  data/processed/physician/canonical_physician_breakdown.csv

Run from the repo root:
    python migrations/validate_physicians.py
"""

import os
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

FUZZY_THRESHOLD = 0.82
CANDIDATE_FUZZY_THRESHOLD = 0.65  # Lower bar used only for candidate flagging

# Known professions that would otherwise be caught by the physician match logic
# but are distinct roles and must not be included.
_EXCLUSIONS = {
    "physician assistant",
    "physical therapist",
    "physical therapist assistants",
    "physical therapy assistants",
    "licensed physical therapists and pt assistants",
}

CANONICAL_PHYSICIAN_ID = 167

# Substrings whose presence anywhere in a KeyValue is a direct physician match.
# "anesthesiolog" covers both "anesthesiologist" and "anesthesiologists".
_PHYSICIAN_DIRECT_KEYWORDS = ("physician", "surgeon", "anesthesiolog")

# Tokens that unambiguously indicate a physician role and are not caught by the
# direct keyword check above.
_PHYSICIAN_TITLES = frozenset({"m.d.", "d.o.", "doctor", "hospitalist"})

# Medical specialty suffixes used only for CANDIDATE flagging (not definitive
# matching) because they also appear in non-physician roles like audiologist.
_CANDIDATE_SPECIALTY_SUFFIXES = ("ologist", "iatrist", "iatrician")

# Profession IDs confirmed as physician roles through domain knowledge but not
# caught by keyword/fuzzy matching.  These are promoted to physician variants
# with match_type="curated".
_KNOWN_PHYSICIAN_IDS = frozenset({
    # Doctors by title / seniority
    1391,  # Senior and Junior Doctors
    1575,  # Medical Practitioners
    1373,  # Assistant Doctors
    1033,  # Medical Directors
    1400,  # Chief Medical Officers
    # Training grades
    1355,  # Dermatology Residents
    1010,  # Cardiology Fellows
    1307,  # Fellows
    1296,  # Senior House Officer
    1289,  # Junior House Officer
    1344,  # Consultants in training
    # Specialties / practice types
    1018,  # Family Practitioners
    1656,  # Family Medicine Practitioners
    1162,  # Surgical specialists
     970,  # Emergency Medicine
    1229,  # Pathologists
    1292,  # Anaesthetist
    1029,  # Anaesthetists and Intensivists
    1300,  # Gynaecologist
    1044,  # Obstetricians
    1690,  # OB/GYN Specialists
    1026,  # Paediatric Consultants
    1524,  # General Pediatricians
    1034,  # General Internists
    1178,  # Child and Adolescent Psychiatrists
     711,  # Ophthalmologist
    1345,  # Consultant Cardiologists
    1315,  # Interventional Radiologists
    1366,  # Radiation Oncologists
    1367,  # Medical Oncologists
    1006,  # Interventional Cardiologists
    1356,  # Rheumatologists
    1007,  # Pediatric Cardiologists
     971,  # Neuro-Interventionist
    # Generalist / primary care
    1291,  # Rural Generalist
    1035,  # Primary Care Practitioners
    1388,  # Primary Care Providers
    1154,  # Urgent Care Providers
    1039,  # Cardiothoracic
    # Radiology / imaging
    1030,  # DGH Radiologists (District General Hospital)
    1306,  # Cardiac Radiologists
    # Neurology / neuroscience
     972,  # Adult & Pediatric Neurology
    1177,  # Paediatric Neurologists
    1379,  # Pediatric Neurologists
    1333,  # Neuro-Oncologists
    # Critical care / intensive care
    1068,  # Critical Care Medicine
    1046,  # Intensivists
    # Subspecialties
     934,  # Geriatrics
    1017,  # Trauma
    1009,  # ACHD Specialists (Adult Congenital Heart Disease)
    1226,  # IVF / Infertility Specialists
    1339,  # Uro-Gynecologists
    1014,  # Paediatric Urology
    1038,  # Vascular Medicine Specialists
    1362,  # Kidney Specialists (Nephrologists)
    1302,  # Pain Management Specialist
    # Adjacent physician roles
     554,  # Hyperbaric Medicine
    1406,  # Primary Clinicians
    1282,  # Clinician Leaders
    # Neonatal / perinatal / sleep
    1117,  # Neonatal-Perinatal Medicine
     901,  # Sleep Medicine
    # Pediatric subspecialties
    1118,  # Developmental-Behavioral Pediatrics
    1466,  # Pediatric Gastroenterologists
    1494,  # Pediatric Intensivists
    # Cardiology / electrophysiology
    1008,  # Electrophysiologists
    # Urology subspecialties
    1012,  # Andrologists
    1016,  # Reconstructive Urology
})

_SUBSCRIPTION_STATUS_LABELS = {
    1: "Subscribed",
    2: "Unsubscribed",
    3: "Bounced",
    4: "Dropped",
}

_USER_TYPE_LABELS = {
    0:  "Unknown",
    1:  "Admin",
    2:  "Organizer",
    3:  "Speaker",
    4:  "Attendee",
    5:  "CMS User",
    6:  "Unclaimed Speaker",
    7:  "Unclaimed Organizer",
    8:  "Admin Team",
    9:  "Non NPI User",
    10: "Contacts",
    11: "Student",
    12: "Exhibitors",
    13: "Investors",
    14: "Others",
    15: "Bulk Upload",
}

_OUTPUT_DIR                  = Path(__file__).parent.parent / "data" / "processed" / "physician"
_OUTPUT_PATH                 = _OUTPUT_DIR / "physician_variants.csv"
_IMPACT_PATH                 = _OUTPUT_DIR / "shadow_physician_impact.csv"
_CANDIDATES_ACTIVE_PATH      = _OUTPUT_DIR / "physician_candidates_active.csv"
_CANDIDATES_OTHER_PATH       = _OUTPUT_DIR / "physician_candidates_other_status.csv"
_CANONICAL_BREAKDOWN_PATH    = _OUTPUT_DIR / "canonical_physician_breakdown.csv"

_QUERY = text("""
    SELECT IdSystem_Params, Category, SubCategory, KeyDescription, KeyValue, Status
    FROM tbl_system_params
    WHERE Category    = 'MedicaLResources'
      AND SubCategory = 'Profession'
      AND Status      = 1
""")


# ── Matching helpers ──────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _check_physician(key_value) -> tuple[bool, str, float]:
    """Return (is_match, match_type, best_score) for a single KeyValue."""
    if key_value is None:
        return False, "", 0.0

    kv_lower = str(key_value).strip().lower()

    if kv_lower in _EXCLUSIONS:
        return False, "", 0.0

    # 1. Direct substring match — any core physician keyword appears in the value
    if any(kw in kv_lower for kw in _PHYSICIAN_DIRECT_KEYWORDS):
        return True, "direct", 1.0

    tokens = kv_lower.split()

    # 2. Title match — a token exactly matches a known physician title
    if any(t in _PHYSICIAN_TITLES for t in tokens):
        return True, "title", 1.0

    # 3. Fuzzy match — a token scores >= FUZZY_THRESHOLD against "physician"
    best = max((_similarity(t, "physician") for t in tokens), default=0.0)
    if best >= FUZZY_THRESHOLD:
        return True, "fuzzy", round(best, 4)

    return False, "", round(best, 4)


def _candidate_flags(key_value) -> str:
    """
    Return a comma-separated string of candidate flag labels for a KeyValue
    that was NOT already identified as a physician variant.  Empty string
    means no flags were triggered.
    """
    if key_value is None or (isinstance(key_value, float) and pd.isna(key_value)):
        return "id_not_in_system_params"

    kv_lower = str(key_value).strip().lower()
    tokens = kv_lower.split()
    flags = []

    # Specialty suffix — catches Cardiologist, Neurologist, Dermatologist, etc.
    if any(t.endswith(_CANDIDATE_SPECIALTY_SUFFIXES) for t in tokens):
        flags.append("specialty_suffix")

    # Informal physician title tokens not already in _PHYSICIAN_TITLES
    if any(t in {"dr", "dr."} for t in tokens):
        flags.append("medical_title")

    # Loose fuzzy similarity to "physician" below the definitive threshold
    best = max((_similarity(t, "physician") for t in tokens), default=0.0)
    if CANDIDATE_FUZZY_THRESHOLD <= best < FUZZY_THRESHOLD:
        flags.append(f"loose_fuzzy({round(best, 2)})")

    return ", ".join(flags)


# ── Core functions ────────────────────────────────────────────────────────────

def fetch_profession_records() -> pd.DataFrame:
    logger.info(
        "Querying tbl_system_params "
        "(Category='MedicaLResources', SubCategory='Profession', status=1) ..."
    )
    with engine.connect() as conn:
        df = pd.read_sql(_QUERY, conn)
    df["Status"] = df["Status"].astype("Int64")
    logger.info("Found %d active Profession record(s)", len(df))
    return df


def identify_physician_variants(df: pd.DataFrame) -> pd.DataFrame:
    matches = df["KeyValue"].apply(
        lambda v: pd.Series(
            _check_physician(v),
            index=["is_physician", "match_type", "similarity_score"],
        )
    )
    df = pd.concat([df, matches], axis=1)

    # Promote curated IDs not already caught by keyword/fuzzy matching
    known_mask = df["IdSystem_Params"].isin(_KNOWN_PHYSICIAN_IDS) & ~df["is_physician"]
    df.loc[known_mask, "is_physician"]      = True
    df.loc[known_mask, "match_type"]        = "curated"
    df.loc[known_mask, "similarity_score"]  = 1.0

    variants = df[df["is_physician"]].drop(columns=["is_physician"]).reset_index(drop=True)

    direct_count   = (variants["match_type"] == "direct").sum()
    title_count    = (variants["match_type"] == "title").sum()
    fuzzy_count    = (variants["match_type"] == "fuzzy").sum()
    curated_count  = (variants["match_type"] == "curated").sum()
    logger.info(
        "Identified %d physician variant(s)  (direct=%d  title=%d  fuzzy=%d  curated=%d)",
        len(variants), direct_count, title_count, fuzzy_count, curated_count,
    )
    return variants


def fetch_subscriber_impact(shadow_ids: list[int], variants: pd.DataFrame) -> pd.DataFrame:
    logger.info(
        "Querying tbl_newsletter_subscribers for records where "
        "profession_parent_id is in %d shadow physician ID(s) ...",
        len(shadow_ids),
    )
    id_list = ", ".join(str(i) for i in shadow_ids)
    query = text(f"""
        SELECT user_type, profession_parent_id, COUNT(*) AS subscriber_count
        FROM tbl_newsletter_subscribers
        WHERE profession_parent_id IN ({id_list})
        GROUP BY user_type, profession_parent_id
        ORDER BY user_type, profession_parent_id
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)

    key_map = variants.set_index("IdSystem_Params")["KeyValue"]
    df["KeyValue"] = df["profession_parent_id"].map(key_map)

    df["user_type_label"] = df["user_type"].map(
        lambda v: _USER_TYPE_LABELS.get(int(v), f"Unknown({v})")
    )

    df = df.rename(columns={"profession_parent_id": "id"})
    df = df[["user_type", "user_type_label", "id", "KeyValue", "subscriber_count"]]

    total = df["subscriber_count"].sum()
    logger.info(
        "Impact: %d subscriber(s) across %d user_type/id combination(s)",
        total, len(df),
    )
    return df


def fetch_unmatched_candidates(physician_variant_ids: set[int]) -> pd.DataFrame:
    """
    Pull every distinct profession_parent_id used by subscribers, exclude the
    known physician variants, and return the remainder annotated with their
    KeyValue (from tbl_system_params across all statuses), subscriber count,
    and candidate flags.
    """
    logger.info(
        "Scanning all subscriber profession_parent_ids for unmatched candidates ..."
    )

    sub_query = text("""
        SELECT profession_parent_id AS id, COUNT(*) AS subscriber_count
        FROM tbl_newsletter_subscribers
        WHERE profession_parent_id IS NOT NULL
        GROUP BY profession_parent_id
        ORDER BY subscriber_count DESC
    """)

    # Include inactive (Status=0) rows so the candidate report has full coverage.
    params_query = text("""
        SELECT IdSystem_Params, KeyValue, Status
        FROM tbl_system_params
        WHERE Category    = 'MedicaLResources'
          AND SubCategory = 'Profession'
    """)

    with engine.connect() as conn:
        sub_df    = pd.read_sql(sub_query, conn)
        params_df = pd.read_sql(params_query, conn)

    logger.info(
        "Subscriber data: %d distinct profession_parent_id(s) in use",
        len(sub_df),
    )

    unmatched = sub_df[~sub_df["id"].isin(physician_variant_ids)].copy()

    key_map    = params_df.set_index("IdSystem_Params")["KeyValue"]
    status_map = params_df.set_index("IdSystem_Params")["Status"]
    unmatched["KeyValue"] = unmatched["id"].map(key_map)
    unmatched["Status"]   = unmatched["id"].map(status_map).astype("Int64")

    unmatched["candidate_flags"] = unmatched["KeyValue"].apply(_candidate_flags)

    flagged_count = (unmatched["candidate_flags"] != "").sum()
    logger.info(
        "Unmatched candidates: %d total  (%d with candidate flags)",
        len(unmatched), flagged_count,
    )

    return unmatched[["id", "KeyValue", "Status", "subscriber_count", "candidate_flags"]]


def fetch_variant_subscriber_counts(variant_ids: list[int]) -> pd.Series:
    """Return a Series mapping variant ID → total subscriber count."""
    id_list = ", ".join(str(i) for i in variant_ids)
    query = text(f"""
        SELECT profession_parent_id AS id, COUNT(*) AS subscriber_count
        FROM tbl_newsletter_subscribers
        WHERE profession_parent_id IN ({id_list})
        GROUP BY profession_parent_id
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    return df.set_index("id")["subscriber_count"]


def write_csv(df: pd.DataFrame) -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_OUTPUT_PATH, index=False)
    logger.info("Wrote %d row(s) to %s", len(df), _OUTPUT_PATH)


def write_impact_csv(df: pd.DataFrame) -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_IMPACT_PATH, index=False)
    logger.info("Wrote impact report (%d row(s)) to %s", len(df), _IMPACT_PATH)


def write_candidates_csvs(df: pd.DataFrame) -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Use .eq() so that NA Status values (id_not_in_system_params rows) fall
    # into "other" rather than being silently dropped by == / != comparisons.
    active = df[df["Status"].eq(1)].reset_index(drop=True)
    other  = df[~df["Status"].eq(1)].reset_index(drop=True)

    active.to_csv(_CANDIDATES_ACTIVE_PATH, index=False)
    logger.info(
        "Wrote %d active-status candidate(s) to %s", len(active), _CANDIDATES_ACTIVE_PATH
    )

    other.to_csv(_CANDIDATES_OTHER_PATH, index=False)
    logger.info(
        "Wrote %d other-status candidate(s) to %s", len(other), _CANDIDATES_OTHER_PATH
    )


def fetch_canonical_physician_breakdown() -> pd.DataFrame:
    """
    Group subscribers where profession_parent_id = CANONICAL_PHYSICIAN_ID
    by user_type and email_status and return the counts.
    """
    logger.info(
        "Querying tbl_newsletter_subscribers for canonical physician "
        "(profession_parent_id=%d) breakdown by user_type / email_status ...",
        CANONICAL_PHYSICIAN_ID,
    )
    query = text("""
        SELECT user_type, email_status, COUNT(*) AS subscriber_count
        FROM tbl_newsletter_subscribers
        WHERE profession_parent_id = :pid
        GROUP BY user_type, email_status
        ORDER BY user_type, email_status
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"pid": CANONICAL_PHYSICIAN_ID})

    df["user_type_label"] = df["user_type"].map(
        lambda v: _USER_TYPE_LABELS.get(int(v), f"Unknown({v})")
    )
    df = df[["user_type", "user_type_label", "email_status", "subscriber_count"]]

    total = int(df["subscriber_count"].sum())
    logger.info(
        "Canonical physician: %d subscriber(s) across %d user_type/email_status combination(s)",
        total, len(df),
    )
    return df


def write_canonical_breakdown_csv(df: pd.DataFrame) -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_CANONICAL_BREAKDOWN_PATH, index=False)
    logger.info("Wrote canonical breakdown (%d row(s)) to %s", len(df), _CANONICAL_BREAKDOWN_PATH)


def log_canonical_summary(breakdown: pd.DataFrame) -> None:
    if breakdown.empty:
        return

    total = int(breakdown["subscriber_count"].sum())

    def _sum_status(*labels: str) -> int:
        mask = breakdown["email_status"].str.strip().str.lower().isin(
            [l.lower() for l in labels]
        )
        return int(breakdown.loc[mask, "subscriber_count"].sum())

    def _sum_empty() -> int:
        mask = (
            breakdown["email_status"].isna() |
            (breakdown["email_status"].str.strip() == "")
        )
        return int(breakdown.loc[mask, "subscriber_count"].sum())

    bounced      = _sum_status("bounced")
    empty        = _sum_empty()
    not_verified = _sum_status("not verified", "not_verified")
    blocked      = _sum_status("blocked")

    logger.info("─" * 55)
    logger.info("  CANONICAL PHYSICIAN SUMMARY  (profession_parent_id = %d)", CANONICAL_PHYSICIAN_ID)
    logger.info("  %-38s %10s", f"Total Physicians with id={CANONICAL_PHYSICIAN_ID}:", f"{total:,}")
    logger.info("  %-38s %10s", "email_status: Bounced:",        f"{bounced:,}")
    logger.info("  %-38s %10s", "email_status: Empty or NaN:",   f"{empty:,}")
    logger.info("  %-38s %10s", "email_status: Not Verified:",   f"{not_verified:,}")
    logger.info("  %-38s %10s", "email_status: Blocked:",        f"{blocked:,}")
    logger.info("─" * 55)


def log_canonical_subscription_summary() -> None:
    query = text("""
        SELECT subscription_status, COUNT(*) AS subscriber_count
        FROM tbl_newsletter_subscribers
        WHERE profession_parent_id = :pid
        GROUP BY subscription_status
        ORDER BY subscription_status
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"pid": CANONICAL_PHYSICIAN_ID})

    total = int(df["subscriber_count"].sum())

    logger.info("─" * 55)
    logger.info(
        "  CANONICAL PHYSICIAN SUBSCRIPTION STATUS  (profession_parent_id = %d)",
        CANONICAL_PHYSICIAN_ID,
    )
    logger.info("  %-38s %10s", f"Total Physicians with id={CANONICAL_PHYSICIAN_ID}:", f"{total:,}")
    for _, row in df.iterrows():
        ss    = int(row["subscription_status"])
        label = _SUBSCRIPTION_STATUS_LABELS.get(ss, str(ss))
        logger.info(
            "  %-38s %10s",
            f"subscription_status: {label}:",
            f"{int(row['subscriber_count']):,}",
        )
    logger.info("─" * 55)


def fetch_total_subscriber_count() -> int:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM tbl_newsletter_subscribers"))
        return int(result.scalar())


def log_summary(variants: pd.DataFrame, candidates: pd.DataFrame, total: int) -> None:
    physician_count = int(variants["subscriber_count"].sum())
    active_count    = int(candidates[candidates["Status"].eq(1)]["subscriber_count"].sum())
    other_count     = int(candidates[~candidates["Status"].eq(1)]["subscriber_count"].sum())
    # Subscribers whose profession_parent_id is NULL are not in any of the above buckets
    unclassified    = total - physician_count - active_count - other_count

    checksum = physician_count + active_count + other_count + unclassified
    status   = "PASS" if checksum == total else f"FAIL (gap={abs(total - checksum):,})"

    logger.info("─" * 55)
    logger.info("  SUMMARY")
    logger.info("  %-38s %10s", "Total Subscribers count:",          f"{total:,}")
    logger.info("  %-38s %10s", "Physician & its variants count:",   f"{physician_count:,}")
    logger.info("  %-38s %10s", "Active review count:",              f"{active_count:,}")
    logger.info("  %-38s %10s", "Other status review count:",        f"{other_count:,}")
    logger.info("  %-38s %10s", "Unclassified (null profession):",   f"{unclassified:,}")
    logger.info("─" * 55)
    logger.info("  Checksum: %s  [%s]", f"{checksum:,}", status)
    logger.info("─" * 55)


def main() -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database -- aborting.")

    df = fetch_profession_records()

    if df.empty:
        logger.info("No profession records found -- nothing to do.")
        return

    variants = identify_physician_variants(df)

    if variants.empty:
        logger.info("No physician variants identified.")
        return

    # Attach total subscriber count for each variant from the newsletter table
    variant_ids = [int(i) for i in variants["IdSystem_Params"]]
    counts = fetch_variant_subscriber_counts(variant_ids)
    variants["subscriber_count"] = (
        variants["IdSystem_Params"].map(counts).fillna(0).astype(int)
    )

    write_csv(variants)

    # Part B: subscriber impact for shadow IDs (all variants except the canonical one)
    shadow_ids = [
        int(i) for i in variants["IdSystem_Params"]
        if int(i) != CANONICAL_PHYSICIAN_ID
    ]
    logger.info(
        "Canonical physician ID: %d  |  Shadow IDs to remove: %s",
        CANONICAL_PHYSICIAN_ID, shadow_ids,
    )

    if shadow_ids:
        impact = fetch_subscriber_impact(shadow_ids, variants)
        write_impact_csv(impact)
    else:
        logger.info("No shadow IDs found -- skipping impact report.")

    # Part C: scan all subscriber profession_parent_ids for unmatched candidates
    physician_ids = {int(i) for i in variants["IdSystem_Params"]}
    candidates = fetch_unmatched_candidates(physician_ids)
    if not candidates.empty:
        write_candidates_csvs(candidates)

    # Part D: canonical physician breakdown by user_type / email_status
    breakdown = fetch_canonical_physician_breakdown()
    if not breakdown.empty:
        write_canonical_breakdown_csv(breakdown)
        logger.info("Canonical physician breakdown by user_type / email_status:")
        for _, row in breakdown.iterrows():
            logger.info(
                "  user_type=%-3s %-22s  email_status=%-15s  count=%d",
                row["user_type"], f"({row['user_type_label']})",
                row["email_status"], row["subscriber_count"],
            )

    # Summary + checksum
    total = fetch_total_subscriber_count()
    log_summary(variants, candidates, total)
    log_canonical_summary(breakdown)
    log_canonical_subscription_summary()


if __name__ == "__main__":
    main()
