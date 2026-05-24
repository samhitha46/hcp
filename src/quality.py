import re
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import text

from src.logger import get_logger

_VALID_USER_TYPES = set(range(16))   # 0 – 15
_VALID_SUB_STATUSES = {1, 2, 3, 4}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SPECIALTY_RE = re.compile(r"^\d+(,\d+)*$")
_ZERO_DATE_PREFIXES = ("0000-00-00", "0001-01-01")

logger = get_logger(__name__)


@dataclass
class QualityIssue:
    field: str
    message: str


def _val(raw) -> str:
    """Convert any pandas/Python value to a clean string; return '' for nulls."""
    if raw is None:
        return ""
    try:
        if pd.isnull(raw):
            return ""
    except (TypeError, ValueError):
        pass
    return str(raw).strip()


def check_record(row: dict, country_map: dict) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    # 1. firstname / lastname
    first = _val(row.get("firstname"))
    last = _val(row.get("lastname"))
    missing = [f for f, v in [("firstname", first), ("lastname", last)] if not v]
    if missing:
        issues.append(QualityIssue("name", f"{' and '.join(missing)} is null/empty"))

    # 2. email format
    email = _val(row.get("email"))
    if not email or not _EMAIL_RE.match(email):
        issues.append(QualityIssue("email", f"invalid format: '{email}'"))

    # 3. user_type  must be 0 – 15
    ut_raw = _val(row.get("user_type"))
    try:
        if not ut_raw:
            raise ValueError
        ut = int(float(ut_raw))
        if ut not in _VALID_USER_TYPES:
            issues.append(QualityIssue("user_type", f"value {ut} is outside valid range 0–15"))
    except (ValueError, TypeError):
        issues.append(QualityIssue("user_type", f"non-numeric value '{ut_raw}'"))

    # 4. subscription_status must be 1 – 4
    ss_raw = _val(row.get("subscription_status"))
    try:
        if not ss_raw:
            raise ValueError
        ss = int(float(ss_raw))
        if ss not in _VALID_SUB_STATUSES:
            issues.append(QualityIssue("subscription_status", f"value {ss} not in {{1, 2, 3, 4}}"))
    except (ValueError, TypeError):
        issues.append(QualityIssue("subscription_status", f"non-numeric value '{ss_raw}'"))

    # 5. country_id must be present
    country_id = _val(row.get("country_id"))
    if not country_id:
        issues.append(QualityIssue("country_id", "null or empty"))

    # 6. created_date must not be null / zero
    created = _val(row.get("created_date"))
    if not created or any(created.startswith(p) for p in _ZERO_DATE_PREFIXES):
        issues.append(QualityIssue("created_date", f"null/empty/zero date: '{created}'"))

    # 7. specialty_ids must be comma-separated numbers
    spec = _val(row.get("specialty_ids"))
    if not spec:
        issues.append(QualityIssue("specialty_ids", "null or empty"))
    elif not _SPECIALTY_RE.match(spec):
        issues.append(QualityIssue("specialty_ids", f"'{spec}' is not a comma-separated number list"))

    return issues


def fetch_country_map(engine, country_ids: list) -> dict:
    """
    Bulk-fetch {country_id_str: country_name} for all non-null ids.
    Uses a single query — no per-row round-trips.
    """
    unique = list({int(c) for c in country_ids if _val(c).isdigit()})
    if not unique:
        return {}

    placeholders = ", ".join(str(i) for i in unique)
    query = text(f"SELECT id, country_name FROM tbl_master_countries WHERE id IN ({placeholders})")
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
    return {str(r[0]): r[1] for r in rows}


def run_quality_checks(df: pd.DataFrame, engine) -> tuple:
    """
    Validate every row and split into (valid_df, invalid_df, issues_map, country_map).

    issues_map  — {positional_index_in_original_df: list[QualityIssue]}
    country_map — {country_id_str: country_name}  (used downstream for display)
    """
    country_ids = [_val(row.get("country_id")) for _, row in df.iterrows()]
    try:
        country_map = fetch_country_map(engine, country_ids)
        logger.info("Fetched %d country name(s)", len(country_map))
    except Exception as exc:
        logger.warning("Country lookup failed (%s) — IDs will display without names", exc)
        country_map = {}

    valid_idx, invalid_idx = [], []
    issues_map: dict[int, list[QualityIssue]] = {}

    for i, (_, row) in enumerate(df.iterrows()):
        issues = check_record(row.to_dict(), country_map)
        if issues:
            invalid_idx.append(i)
            issues_map[i] = issues
        else:
            valid_idx.append(i)

    valid_df = df.iloc[valid_idx].reset_index(drop=True)
    invalid_df = df.iloc[invalid_idx].reset_index(drop=True)

    logger.info("Quality check complete — %d passed, %d failed", len(valid_df), len(invalid_df))
    return valid_df, invalid_df, issues_map, country_map
