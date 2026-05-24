import argparse
import sys
import time
from datetime import date, datetime

import pandas as pd
import requests
from sqlalchemy import text

from src.db import engine
from src.logger import get_logger
from src.quality import run_quality_checks
from src.zerobounce.client import ZeroBounceClient

logger = get_logger(__name__)

_QUERY = text("""
    SELECT firstname, lastname, email, user_type, npi,
           subscription_status, country_id, specialty_ids, created_date
    FROM tbl_newsletter_subscribers
    WHERE DATE(created_date) = :query_date
    ORDER BY created_date
""")

_NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"
_NPPES_DELAY = 0.4  # seconds between calls — stays within ~2 req/sec limit


def fetch_subscribers(query_date: "date | str") -> pd.DataFrame:
    """
    Return newsletter subscribers whose record was created on query_date.

    Args:
        query_date: 'YYYY-MM-DD' string or a date/datetime object.
    """
    if isinstance(query_date, str):
        query_date = datetime.strptime(query_date, "%Y-%m-%d").date()
    elif isinstance(query_date, datetime):
        query_date = query_date.date()

    logger.info("Querying tbl_newsletter_subscribers for created_date = %s", query_date)

    with engine.connect() as conn:
        df = pd.read_sql(_QUERY, conn, params={"query_date": str(query_date)})

    for col in ("npi", "country_id"):
        if col in df.columns:
            df[col] = df[col].astype("Int64").astype(str).replace("<NA>", "")

    logger.info("Found %d row(s)", len(df))
    return df


def _nppes_lookup(session: requests.Session, first: str, last: str) -> list[dict]:
    """Single NPPES name lookup; returns list of {npi, first_name, last_name} dicts."""
    resp = session.get(
        _NPPES_URL,
        params={
            "first_name": first,
            "last_name": last,
            "enumeration_type": "NPI-1",
            "version": "2.1",
            "limit": 200,
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return [
        {
            "npi": str(r.get("number", "")),
            "first_name": r.get("basic", {}).get("first_name", ""),
            "last_name": r.get("basic", {}).get("last_name", ""),
        }
        for r in results
    ]


def enrich_with_nppes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Look up each subscriber by first/last name in NPPES and append match columns.

    Added columns:
        nppes_match_count  — number of NPI-1 records found for that name
        nppes_matches      — list of {npi, first_name, last_name} dicts from NPPES
    """
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    match_counts: list[int] = []
    match_lists: list[list[dict]] = []
    total = len(df)

    for i, (_, row) in enumerate(df.iterrows(), start=1):
        first = str(row.get("firstname") or "").strip()
        last = str(row.get("lastname") or "").strip()

        if not first or not last or first.lower() == "nan" or last.lower() == "nan":
            match_counts.append(0)
            match_lists.append([])
            continue

        try:
            matches = _nppes_lookup(session, first, last)
            match_counts.append(len(matches))
            match_lists.append(matches)
            logger.debug("[%d/%d] %s %s → %d match(es)", i, total, first, last, len(matches))
        except Exception as exc:
            logger.warning("[%d/%d] NPPES lookup failed for '%s %s': %s", i, total, first, last, exc)
            match_counts.append(-1)
            match_lists.append([])

        if i < total:
            time.sleep(_NPPES_DELAY)

    enriched = df.copy().reset_index(drop=True)
    enriched["nppes_match_count"] = match_counts
    enriched["nppes_matches"] = match_lists

    matched = sum(c > 0 for c in match_counts)
    no_match = sum(c == 0 for c in match_counts)
    errors = sum(c == -1 for c in match_counts)
    logger.info(
        "NPPES enrichment done — %d matched, %d no match, %d skipped/error",
        matched, no_match, errors,
    )
    return enriched


def enrich_with_zerobounce(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validate subscriber email addresses via ZeroBounce and append zb_* columns.

    Added columns (prefixed zb_):
        zb_status        — valid | invalid | catch-all | unknown | spamtrap | abuse | do_not_mail
        zb_sub_status    — granular reason (e.g. mailbox_not_found, role_based, etc.)
        zb_free_email    — True if the domain is a free provider (gmail, yahoo, …)
        zb_did_you_mean  — suggested correction when the API detects a typo
        zb_mx_found      — whether a valid MX record exists for the domain
        zb_error         — set only when a lookup failed
    """
    client = ZeroBounceClient()
    credits = client.get_credits()
    logger.info("ZeroBounce credits available: %d", credits)

    emails = [str(row.get("email") or "").strip() for _, row in df.iterrows()]
    non_empty = [(i, e) for i, e in enumerate(emails) if e and e.lower() != "nan"]

    if not non_empty:
        logger.warning("No valid email addresses found; skipping ZeroBounce enrichment")
        return df

    if credits < len(non_empty):
        logger.warning(
            "Insufficient ZeroBounce credits (%d) for %d emails — skipping enrichment",
            credits,
            len(non_empty),
        )
        return df

    indices, addresses = zip(*non_empty)
    logger.info("Validating %d email(s) via ZeroBounce batch API", len(addresses))
    results = client.validate_batch(list(addresses))

    result_map = dict(zip(indices, results))

    zb_rows = []
    for i in range(len(df)):
        if i in result_map:
            zb_rows.append(result_map[i].to_record())
        else:
            zb_rows.append({
                "zb_address": emails[i],
                "zb_status": "",
                "zb_sub_status": "",
                "zb_free_email": None,
                "zb_did_you_mean": "",
                "zb_mx_found": None,
                "zb_mx_record": "",
                "zb_smtp_provider": "",
                "zb_domain_age_days": "",
                "zb_country": "",
                "zb_region": "",
                "zb_city": "",
                "zb_firstname": "",
                "zb_lastname": "",
                "zb_gender": "",
                "zb_error": "no email",
            })

    enriched = df.copy().reset_index(drop=True)
    zb_df = pd.DataFrame(zb_rows)
    for col in zb_df.columns:
        enriched[col] = zb_df[col].values

    valid = sum(1 for r in result_map.values() if r.status == "valid")
    invalid = sum(1 for r in result_map.values() if r.status == "invalid")
    other = len(result_map) - valid - invalid
    logger.info("ZeroBounce done — %d valid, %d invalid, %d other", valid, invalid, other)
    return enriched


_ZB_EXPLANATIONS = {
    # top-level statuses
    "valid":       "Address is valid and deliverable.",
    "invalid":     "Address does not exist or cannot receive mail.",
    "catch-all":   "Domain accepts all addresses — individual mailbox cannot be confirmed.",
    "unknown":     "Could not determine validity (server unresponsive or timeout).",
    "spamtrap":    "Address is a known spam trap. Do not email.",
    "abuse":       "Address belongs to a known complainer/abuser. Do not email.",
    "do_not_mail": "Address should not be emailed.",
    # sub-statuses
    "global_suppression":          "Address is on a global do-not-email suppression list.",
    "mailbox_not_found":           "Mailbox does not exist on this domain.",
    "no_dns_entries":              "Domain has no DNS/MX records — unlikely to receive mail.",
    "failed_smtp_connection":      "Could not connect to the mail server to verify.",
    "mailbox_quota_exceeded":      "Mailbox is full and cannot accept new messages.",
    "role_based":                  "Generic role address (e.g. info@, admin@, support@).",
    "disposable":                  "Temporary / disposable email address.",
    "toxic":                       "Domain is associated with abuse, bots, or fraud.",
    "does_not_accept_mail":        "Domain exists but does not accept inbound email.",
    "possible_trap":               "Address may be a spam trap — treat with caution.",
    "mail_server_temporary_error": "Mail server returned a temporary error — result may change.",
    "antispam_system":             "Request was blocked by an anti-spam system.",
    "exception_occurred":          "An unexpected error occurred during validation.",
}


def _fmt_country(country_id: str, country_map: dict) -> str:
    if not country_id:
        return "N/A"
    name = country_map.get(country_id)
    return f"{country_id}({name})" if name else country_id


def _print_quality_report(df: pd.DataFrame, issues_map: dict) -> None:
    print("\n" + "!" * 70)
    print(f"  CRITICAL QUALITY ISSUES — {len(issues_map)} record(s) failed and will be skipped")
    print("!" * 70)
    for pos, issues in issues_map.items():
        row = df.iloc[pos]
        first = str(row.get("firstname") or "").strip() or "<no firstname>"
        last = str(row.get("lastname") or "").strip() or "<no lastname>"
        email = str(row.get("email") or "").strip() or "<no email>"
        print(f"\n  [{pos + 1}] {first} {last}  |  email: {email}")
        for issue in issues:
            print(f"       CRITICAL [{issue.field}] {issue.message}")
    print("!" * 70 + "\n")


def _print_results(df: pd.DataFrame, query_date: str, country_map: dict | None = None) -> None:
    country_map = country_map or {}
    if df.empty:
        print(f"\nNo subscribers passed quality checks for {query_date}.")
        return

    has_nppes = "nppes_matches" in df.columns
    print(f"\nSubscribers created on {query_date}  ({len(df)} row(s))\n")
    print("=" * 70)

    for _, row in df.iterrows():
        sub_npi = str(row.get("npi", "")).strip()
        name = f"{row['firstname']} {row['lastname']}"
        country_display = _fmt_country(str(row.get("country_id", "") or ""), country_map)
        print(
            f"{name:<30}  type={row['user_type']}  "
            f"npi={sub_npi or 'N/A':<12}  "
            f"status={row['subscription_status']}  "
            f"country={country_display}"
        )

        if has_nppes:
            matches: list[dict] = row["nppes_matches"]
            count = row["nppes_match_count"]

            if count == -1:
                print("  NPPES: lookup error")
            elif count == 0:
                print("  NPPES: no match")
            else:
                print(f"  NPPES: {count} match(es)")
                for m in matches:
                    match_npi = m["npi"]
                    marker = " ** NPI MATCH **" if match_npi == sub_npi else ""
                    print(
                        f"    -> {m['first_name']} {m['last_name']}"
                        f"  (NPI: {match_npi}){marker}"
                    )

        if "zb_status" in df.columns:
            zb_email = str(row.get("email") or "").strip() or "<no email>"
            zb_status = str(row.get("zb_status") or "").strip()
            zb_sub = str(row.get("zb_sub_status") or "").strip()
            zb_free = row.get("zb_free_email")
            zb_err = row.get("zb_error")
            zb_suggestion = str(row.get("zb_did_you_mean") or "").strip()

            status_label = zb_status.upper() if zb_status else "NOT CHECKED"
            sub_label = f" / {zb_sub}" if zb_sub else ""
            explanation = _ZB_EXPLANATIONS.get(zb_sub) or _ZB_EXPLANATIONS.get(zb_status, "")

            print(f"  Email:       {zb_email}")
            print(f"  ZeroBounce:  {status_label}{sub_label}")
            if explanation:
                print(f"               {explanation}")
            if zb_free:
                print("               (free email provider — gmail, yahoo, aol, etc.)")
            if zb_suggestion:
                print(f"               Did you mean: {zb_suggestion}?")
            if zb_err and str(zb_err).lower() not in ("none", ""):
                print(f"               Lookup error: {zb_err}")

        print("-" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Query newsletter subscribers by created date and enrich with NPPES data."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=str(date.today()),
        metavar="YYYY-MM-DD",
        help="Date to query (default: today)",
    )
    parser.add_argument(
        "--skip-zerobounce",
        action="store_true",
        help="Skip ZeroBounce email validation",
    )
    parser.add_argument(
        "--skip-nppes",
        action="store_true",
        help="Skip NPPES enrichment",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N subscribers (default: all)",
    )
    args = parser.parse_args()

    try:
        df = fetch_subscribers(args.date)
        if df.empty:
            print(f"\nNo subscribers found for {args.date}.")
            sys.exit(0)

        if args.limit is not None:
            total = len(df)
            df = df.head(args.limit).reset_index(drop=True)
            logger.info("--limit %d applied: processing %d of %d row(s)", args.limit, len(df), total)

        logger.info("Running quality checks on %d row(s)...", len(df))
        valid_df, invalid_df, issues_map, country_map = run_quality_checks(df, engine)

        if issues_map:
            _print_quality_report(df, issues_map)

        if valid_df.empty:
            print(f"\nAll {len(df)} record(s) failed quality checks. Pipeline stopped.")
            sys.exit(1)

        logger.info("%d record(s) passed quality checks, proceeding.", len(valid_df))
        df = valid_df

        if not args.skip_zerobounce:
            logger.info("Starting ZeroBounce validation for %d rows...", len(df))
            df = enrich_with_zerobounce(df)

        if not args.skip_nppes:
            logger.info("Starting NPPES enrichment for %d rows...", len(df))
            df = enrich_with_nppes(df)

        _print_results(df, args.date, country_map)
    except Exception as exc:
        logger.error("Failed: %s", exc)
        sys.exit(1)
