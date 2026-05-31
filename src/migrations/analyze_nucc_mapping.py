#!/usr/bin/env python3
"""Analyze the NUCC mapping CSV and produce lookup output files."""

import csv
import os
import sys

# Allow running as a plain script from the repo root or any working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rapidfuzz import fuzz
from sqlalchemy import text

from src.db import engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(
    BASE_DIR, 'input_data',
    'Finilize Profession,Speciality and SubSpeciality List - NUCC Mapping.csv'
)
OUTPUT_DIR = os.path.join(BASE_DIR, 'data')

STATUS_MAP = {
    1: "Active",
    2: "Inactive",
    3: "FE User Created",
    4: "Deleted",
    5: "Others",
}

FUZZY_THRESHOLD = 85  # rapidfuzz scores are 0-100


def clean(val):
    return val.strip() if val else ''


# ── Database helpers ──────────────────────────────────────────────────────────

def fetch_db_professions():
    """Return all profession rows from tbl_system_params."""
    query = text("""
        SELECT IdSystem_Params, Category, SubCategory, KeyDescription, KeyValue, Status
        FROM tbl_system_params
        WHERE Category = 'MedicalResources'
          AND SubCategory = 'Profession'
    """)
    with engine.connect() as conn:
        result = conn.execute(query)
        return [dict(r._mapping) for r in result]


# ── Matching logic ────────────────────────────────────────────────────────────

def match_profession(profession_name, db_rows):
    """
    Match a profession name against DB rows with four priority levels:
      1. Exact match  + status = 1 (Active)
      2. Exact match  + status ≠ 1
      3. Fuzzy match  + status = 1
      4. Fuzzy match  + status ≠ 1
      5. No match
    Returns (id, db_name, type_of_match).
    """
    name_lower = profession_name.lower()

    exact = [r for r in db_rows if clean(str(r['KeyValue'])).lower() == name_lower]

    # 1. Exact + Active
    for r in exact:
        if int(r['Status']) == 1:
            return (r['IdSystem_Params'], r['KeyValue'], "Exact Match - Active")

    # 2. Exact + other status
    for r in exact:
        label = STATUS_MAP.get(int(r['Status']), f"Unknown ({r['Status']})")
        return (r['IdSystem_Params'], r['KeyValue'], f"Exact Match - {label}")

    # Fuzzy scan
    best_active = (0, None)
    best_other = (0, None)
    for r in db_rows:
        db_lower = clean(str(r['KeyValue'])).lower()
        # Take the higher of direct-string similarity and word-order-insensitive similarity.
        # Avoids WRatio's aggressive partial-match strategy that causes false positives.
        score = max(fuzz.ratio(name_lower, db_lower), fuzz.token_sort_ratio(name_lower, db_lower))
        if score >= FUZZY_THRESHOLD:
            if int(r['Status']) == 1:
                if score > best_active[0]:
                    best_active = (score, r)
            else:
                if score > best_other[0]:
                    best_other = (score, r)

    # 3. Fuzzy + Active
    if best_active[1]:
        r = best_active[1]
        return (r['IdSystem_Params'], r['KeyValue'], "Fuzzy Match - Active")

    # 4. Fuzzy + other status
    if best_other[1]:
        r = best_other[1]
        label = STATUS_MAP.get(int(r['Status']), f"Unknown ({r['Status']})")
        return (r['IdSystem_Params'], r['KeyValue'], f"Fuzzy Match - {label}")

    # 5. No match
    return ("Not Found", "", "Not Found")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    rows = []
    with open(INPUT_FILE, newline='', encoding='cp1252') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # ── Unique professions ────────────────────────────────────────────────────
    unique_professions = sorted(
        {clean(r.get('eMed_Profession')) for r in rows if clean(r.get('eMed_Profession'))}
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    prof_path = os.path.join(OUTPUT_DIR, 'unique_professions.csv')
    with open(prof_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['profession_name'])
        for name in unique_professions:
            writer.writerow([name])
    print(f"Written {len(unique_professions)} unique professions -> {prof_path}")

    # ── Unique profession + speciality combinations ───────────────────────────
    unique_specialities = sorted(
        {
            (clean(r.get('eMed_Profession')), clean(r.get('eMed_Speciality')))
            for r in rows
            if clean(r.get('eMed_Profession')) and clean(r.get('eMed_Speciality'))
        }
    )

    spec_path = os.path.join(OUTPUT_DIR, 'unique_speciality.csv')
    with open(spec_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['profession_name', 'speciality_name'])
        for profession, speciality in unique_specialities:
            writer.writerow([profession, speciality])
    print(f"Written {len(unique_specialities)} unique profession/speciality combos -> {spec_path}")

    # ── Match professions against the database ────────────────────────────────
    print("Querying database ...")
    db_rows = fetch_db_professions()
    print(f"Found {len(db_rows)} profession row(s) in tbl_system_params.")

    emed_path = os.path.join(OUTPUT_DIR, 'profession_emed.csv')
    with open(emed_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'profession_name', 'db_profession_name', 'type_of_match'])
        for name in unique_professions:
            pid, db_name, match_type = match_profession(name, db_rows)
            writer.writerow([pid, name, db_name, match_type])

    print(f"Written {len(unique_professions)} rows -> {emed_path}")


if __name__ == '__main__':
    main()
