"""
Identifies junk records in tbl_newsletter_subscribers and writes them to a CSV.

Check 1 & 2 — null/empty names with data present:
  A. No name, has profession data, no specialty data
  B. No name, no profession data, has specialty_ids
  C. No name, has profession data AND has specialty_ids

Check 3 — null/empty names with no data at all:
  D. No name, no profession data, no specialty data
     → "firstname and lastname still empty after the 2 checks"

Check 4 — names that exist but are excessively long (> 9 words):
  E. firstname word count > 9
  F. lastname word count > 9
  G. both firstname and lastname word count > 9

Check 5 — disposable/temporary email address:
  H. email domain matches a known throwaway email service
     (yopmail.com, mailinator.com, maildrop.cc, guerrillamail.*, etc.)

"Profession data" means profession_parent_id is not null/0
                           OR profession_id      is not null/0.

Output: data/processed/junk_data/junk_data.csv  (overwritten on each run)

Run from the repo root:
    python -m src.identify_junk_records
    python src/identify_junk_records.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

from src.db import engine, check_connection
from src.logger import get_logger

logger = get_logger(__name__)

_OUTPUT_PATH = Path(__file__).parent.parent / "data" / "processed" / "junk_data" / "junk_data.csv"

# Check 1-3: every record that has no firstname AND no lastname.
_QUERY = text("""
    SELECT id, email, firstname, lastname, specialty_ids, profession_parent_id, profession_id
    FROM tbl_newsletter_subscribers
    WHERE (firstname IS NULL OR TRIM(firstname) = '')
      AND (lastname  IS NULL OR TRIM(lastname)  = '')
""")

# Check 4: records where either name is non-empty but has more than 5 words.
_QUERY_LONG_NAMES = text("""
    SELECT id, email, firstname, lastname, specialty_ids, profession_parent_id, profession_id
    FROM tbl_newsletter_subscribers
    WHERE (firstname IS NOT NULL AND TRIM(firstname) != ''
           AND (CHAR_LENGTH(TRIM(firstname)) - CHAR_LENGTH(REPLACE(TRIM(firstname), ' ', '')) + 1) > 9)
       OR (lastname  IS NOT NULL AND TRIM(lastname)  != ''
           AND (CHAR_LENGTH(TRIM(lastname))  - CHAR_LENGTH(REPLACE(TRIM(lastname),  ' ', '')) + 1) > 9)
""")

# Check 5: known disposable / throwaway email domains.
_TEMP_EMAIL_DOMAINS = frozenset({
    # --- yopmail family ---
    "yopmail.com",
    "yopmail.fr",
    # --- mailinator family ---
    "mailinator.com",
    "mailnator.com",       # common misspelling variant
    "suremail.info",
    "safetymail.info",
    "beefmilk.com",
    "chogmail.com",
    # --- maildrop ---
    "maildrop.cc",
    # --- Guerrilla Mail network ---
    "guerrillamail.com",
    "guerrillamail.info",
    "guerrillamail.biz",
    "guerrillamail.de",
    "guerrillamail.net",
    "guerrillamail.org",
    "grr.la",
    "sharklasers.com",
    "guerrillamailblock.com",
    "spam4.me",
    # --- Trash Mail family ---
    "trashmail.com",
    "trashmail.at",
    "trashmail.io",
    "trashmail.me",
    "trashmail.net",
    "trashmail.org",
    "trashmailer.com",
    # --- 10 Minute Mail ---
    "10minutemail.com",
    "10minutemail.net",
    "10minutemail.org",
    "10minutemail.de",
    "10minutemail.co.uk",
    # --- other well-known services ---
    "fakeinbox.com",
    "mailnull.com",
    "dispostable.com",
    "discard.email",
    "throwaway.email",
    "tempmail.com",
    "temp-mail.org",
    "getnada.com",
    "mailnesia.com",
    "spamgourmet.com",
    "spamgourmet.net",
    "spamgourmet.org",
    "getairmail.com",
    "throwam.com",
    "moakt.com",
    "tempail.com",
    "mailtemp.info",
    "tempr.email",
    "tempm.com",
    "crazymailing.com",
    "filzmail.com",
    "jetable.fr",
    "jetable.net",
    "jetable.org",
    "notsharingmy.info",
    "objectmail.com",
    "ownmail.net",
    "petml.com",
    "spamfree24.org",
    "spamgob.com",
    "spamhole.com",
    "spamoff.de",
    "spaml.com",
    "tempemail.net",
    "tempinbox.com",
    "tempinbox.co.uk",
    "trbvm.com",
    "wegwerfmail.de",
    "wegwerfmail.net",
    "wegwerfmail.org",
})


def _make_temp_email_query() -> text:
    domain_list = ", ".join(f"'{d}'" for d in sorted(_TEMP_EMAIL_DOMAINS))
    return text(f"""
        SELECT id, email, firstname, lastname, specialty_ids, profession_parent_id, profession_id
        FROM tbl_newsletter_subscribers
        WHERE email IS NOT NULL
          AND TRIM(email) != ''
          AND LOWER(SUBSTRING_INDEX(TRIM(email), '@', -1)) IN ({domain_list})
    """)


_QUERY_TEMP_EMAIL = _make_temp_email_query()


# ── Classification helpers ────────────────────────────────────────────────────

def _has_profession(row) -> bool:
    pid   = row["profession_parent_id"]
    ptype = row["profession_id"]
    return (
        (pid   is not None and pid   != 0) or
        (ptype is not None and ptype != 0)
    )


def _has_specialty(row) -> bool:
    s = row["specialty_ids"]
    return s is not None and str(s).strip() != ""


def _classify(row) -> str:
    prof = _has_profession(row)
    spec = _has_specialty(row)

    if prof and spec:
        return (
            "firstname and lastname are null/empty; "
            "profession_parent_id or profession_id is set; "
            "specialty_ids is set"
        )
    if prof:
        return (
            "firstname and lastname are null/empty; "
            "profession_parent_id or profession_id is set; "
            "no specialty data"
        )
    return (
        "firstname and lastname are null/empty; "
        "specialty_ids is set; "
        "no profession data"
    )


def _count_words(val) -> int:
    if val is None:
        return 0
    return len(str(val).strip().split())


def _classify_long_name(row) -> str:
    fn_words = _count_words(row["firstname"])
    ln_words = _count_words(row["lastname"])
    parts = []
    if fn_words > 9:
        parts.append(f"firstname has {fn_words} words")
    if ln_words > 9:
        parts.append(f"lastname has {ln_words} words")
    return "; ".join(parts) + "; exceeds 9-word limit"


# ── Core functions ────────────────────────────────────────────────────────────

def fetch_junk_records() -> pd.DataFrame:
    logger.info(
        "Querying tbl_newsletter_subscribers for records "
        "where firstname and lastname are both null/empty ..."
    )
    with engine.connect() as conn:
        df = pd.read_sql(_QUERY, conn)
    logger.info("Found %d record(s) with no name", len(df))
    return df


def fetch_long_name_records() -> pd.DataFrame:
    logger.info(
        "Querying tbl_newsletter_subscribers for records "
        "where firstname or lastname exceeds 9 words ..."
    )
    with engine.connect() as conn:
        df = pd.read_sql(_QUERY_LONG_NAMES, conn)
    logger.info("Found %d record(s) with names exceeding 9 words", len(df))
    return df


def fetch_temp_email_records() -> pd.DataFrame:
    logger.info(
        "Querying tbl_newsletter_subscribers for records "
        "with a disposable/temporary email domain ..."
    )
    with engine.connect() as conn:
        df = pd.read_sql(_QUERY_TEMP_EMAIL, conn)
    logger.info("Found %d record(s) with a temp email domain", len(df))
    return df


def write_csv(df: pd.DataFrame) -> None:
    # Log a breakdown by reason
    counts = df["junk_reason"].value_counts()
    logger.info("Breakdown by junk reason:")
    for reason, count in counts.items():
        logger.info("  %d  -- %s", count, reason)

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(_OUTPUT_PATH, index=False)
    logger.info("Wrote %d row(s) to %s", len(df), _OUTPUT_PATH)


def main() -> None:
    if not check_connection():
        raise SystemExit("Cannot reach the database -- aborting.")

    frames = []

    # Checks 1 & 2: null/empty names that also have profession or specialty data
    df_null = fetch_junk_records()
    if not df_null.empty:
        has_data = df_null.apply(
            lambda r: _has_profession(r) or _has_specialty(r), axis=1
        )

        df_12 = df_null[has_data].copy()
        if not df_12.empty:
            df_12["junk_reason"] = df_12.apply(_classify, axis=1)
            frames.append(df_12)

        # Check 3: null/empty names with no further conditions
        df_3 = df_null[~has_data].copy()
        if not df_3.empty:
            df_3["junk_reason"] = "firstname and lastname still empty after the 2 checks"
            frames.append(df_3)

    # Check 4: names that exist but exceed 9 words
    df_long = fetch_long_name_records()
    if not df_long.empty:
        df_long = df_long.copy()
        # SQL counts raw spaces, so multiple consecutive spaces cause false positives.
        # Re-filter here with split() which collapses whitespace correctly.
        accurate_mask = df_long.apply(
            lambda r: _count_words(r["firstname"]) > 9 or _count_words(r["lastname"]) > 9,
            axis=1,
        )
        df_long = df_long[accurate_mask]
        if not df_long.empty:
            df_long["junk_reason"] = df_long.apply(_classify_long_name, axis=1)
            frames.append(df_long)

    # Check 5: disposable / temporary email domain
    df_temp = fetch_temp_email_records()
    if not df_temp.empty:
        df_temp = df_temp.copy()
        df_temp["junk_reason"] = df_temp["email"].apply(
            lambda e: f"disposable/temporary email domain: {str(e).split('@')[-1].strip().lower()}"
        )
        frames.append(df_temp)

    if not frames:
        logger.info("No junk records found -- nothing to write.")
        return

    df_all = pd.concat(frames, ignore_index=True)
    write_csv(df_all)


if __name__ == "__main__":
    main()
