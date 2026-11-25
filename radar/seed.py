"""Build a FICTIONAL demo database: demo accounts, three cohorts, a trained model and predictions.

Run from the project folder:   python -m radar.seed --force
Never point this at real institutional data: it deletes the database file first.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
from werkzeug.security import generate_password_hash

from . import preprocessing, services
from .db import connect, init_schema

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_PASSWORD = "RadarDemo#2026"
DEMO_USERS = [
    ("System Administrator (demo)", "admin@radar.example", "system_admin", None),
    ("Lecturer (demo)", "lecturer@radar.example", "lecturer", "Applied Sciences"),
    ("Academic Advisor (demo)", "advisor@radar.example", "academic_advisor", "Applied Sciences"),
    ("Faculty Administrator (demo)", "faculty@radar.example", "faculty_admin", None),
]
FACULTIES = {
    "Applied Sciences": ["Diploma in Analytical Chemistry", "BSc Biotechnology"],
    "Business & Management Sciences": ["BCom Accounting", "BCom Financial Information Systems"],
    "Informatics & Design": ["BTech Information Technology", "BSc Computer Science"],
    "Education": ["BEd Foundation Phase", "BEd Senior Phase"],
}
FIRST_NAMES = ["Amahle", "Thabo", "Lerato", "Sipho", "Naledi", "Johan", "Ayanda", "Zanele", "Pieter", "Kagiso"]
LAST_NAMES = ["Mthembu", "Nkosi", "Dlamini", "Jacobs", "Petersen", "Khumalo", "Van Wyk", "Adams", "Mokoena"]
COHORTS = ((2024, True), (2025, True), (2026, False))  # the newest cohort has no outcome yet


def _clip(value: float, low: float, high: float) -> float:
    return float(min(high, max(low, value)))


def synthetic_cohort(rng, count: int, year: int, labelled: bool) -> list[dict]:
    """Generate fictional first-year records. Lower ability raises failure risk; values are invented."""
    records = []
    for index in range(count):
        ability = rng.normal()
        faculty = list(FACULTIES)[rng.integers(len(FACULTIES))]
        programme = FACULTIES[faculty][rng.integers(2)]
        failure_chance = 1 / (1 + np.exp(-(-0.9 - 1.1 * ability + rng.normal(0, 0.5))))
        outcome = int(rng.random() < failure_chance) if labelled else None
        records.append({
            "student_number": f"{year}{index + 1:04d}",
            "first_name": FIRST_NAMES[rng.integers(len(FIRST_NAMES))],
            "last_name": LAST_NAMES[rng.integers(len(LAST_NAMES))],
            "gender": ["Female", "Male"][rng.integers(2)],
            "date_of_birth": f"{year - 18 - rng.integers(3)}-{rng.integers(1, 13):02d}-{rng.integers(1, 28):02d}",
            "faculty": faculty,
            "programme": programme,
            "year_level": 1,
            "academic_year": year,
            "semester": "Full year",
            "gpa": round(_clip(2.6 + 0.6 * ability + rng.normal(0, 0.3), 0, 4), 2),
            "assignment_average": round(_clip(60 + 10 * ability + rng.normal(0, 8), 0, 100), 1),
            "test_average": round(_clip(58 + 11 * ability + rng.normal(0, 9), 0, 100), 1),
            "examination_mark": round(_clip(57 + 11 * ability + rng.normal(0, 9), 0, 100), 1),
            "academic_outcome": outcome,
            "attendance_percentage": round(_clip(78 + 8 * ability + rng.normal(0, 7), 0, 100), 1),
            "login_frequency": int(max(0, round(30 + 8 * ability + rng.normal(0, 6)))),
            "assignment_submissions": int(max(0, round(9 + 2 * ability + rng.normal(0, 1.5)))),
            "resource_downloads": int(max(0, round(20 + 5 * ability + rng.normal(0, 4)))),
            "discussion_participation": int(max(0, round(5 + 2 * ability + rng.normal(0, 2)))),
            "average_session_duration": round(_clip(35 + 6 * ability + rng.normal(0, 5), 5, 120), 1),
        })
    return records


def _as_csv_text(records: list[dict]) -> list[list]:
    rows = [list(preprocessing.TEMPLATE_COLUMNS)]
    for record in records:
        rows.append(["" if record.get(c) is None else record.get(c) for c in preprocessing.TEMPLATE_COLUMNS])
    return rows


def build_demo(db_path: Path, students_per_cohort: int = 250, seed: int = 42,
               sample_dir: Path | None = None) -> dict:
    if db_path.exists():
        db_path.unlink()
    init_schema(db_path)
    conn = connect(db_path)
    now = services.utc_now_iso()
    with conn:
        for full_name, email, role, faculty in DEMO_USERS:
            conn.execute(
                "INSERT INTO users (full_name, email, password_hash, role, faculty, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (full_name, email, generate_password_hash(DEMO_PASSWORD), role, faculty, now),
            )
    admin_id = conn.execute("SELECT id FROM users WHERE role = 'system_admin'").fetchone()["id"]
    advisor_id = conn.execute("SELECT id FROM users WHERE role = 'academic_advisor'").fetchone()["id"]

    rng = np.random.default_rng(seed)
    records: list[dict] = []
    for year, labelled in COHORTS:
        records += synthetic_cohort(rng, students_per_cohort, year, labelled)

    if sample_dir is not None:
        sample_dir.mkdir(parents=True, exist_ok=True)
        with open(sample_dir / "fictional_import_example.csv", "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(_as_csv_text(records))
        with open(sample_dir / "import_template.csv", "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(preprocessing.TEMPLATE_COLUMNS)

    imported = services.import_records(conn, records, admin_id)
    trained = services.train_and_store(conn, admin_id)
    scored = services.score_cohort(conn, admin_id)

    # Fictional follow-up actions for the highest-risk students of the current cohort.
    top = conn.execute(
        "SELECT student_id FROM predictions WHERE run_id = ? AND risk_level = 'High' "
        "ORDER BY failure_probability DESC LIMIT 5", (scored["runId"],)).fetchall()
    for index, row in enumerate(top):
        services.add_intervention(
            conn, row["student_id"], advisor_id,
            ["Academic check-in", "Tutoring referral", "Study-skills workshop"][index % 3],
            "Fictional demo action: invited to a check-in to plan support.", None,
        )
    conn.close()
    return {"import": imported, "training": trained, "scoring": scored, "database": str(db_path)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the fictional RADAR demo database.")
    parser.add_argument("--force", action="store_true", help="replace the existing database file")
    parser.add_argument("--students", type=int, default=250, help="students per cohort (default 250)")
    args = parser.parse_args(argv)
    db_path = PROJECT_ROOT / "instance" / "radar.db"
    if db_path.exists() and not args.force:
        print(f"{db_path} already exists. Re-run with --force to replace it with fictional demo data.")
        return 1
    summary = build_demo(db_path, students_per_cohort=args.students,
                         sample_dir=PROJECT_ROOT / "sample_data")
    print("Fictional demo database ready.")
    print(f"  Students imported: {summary['import']['studentsCreated']}")
    print(f"  Model selected:    {summary['training']['selectedAlgorithm']} ({summary['training']['version']})")
    print(f"  Students scored:   {summary['scoring']['scored']} for {summary['scoring']['academicYear']}")
    print(f"  Demo password for all demo accounts: {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
