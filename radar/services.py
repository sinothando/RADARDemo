"""Business operations shared by the web views, the seed script, and the tests."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from . import audit, ml
from .ml import FEATURES, TERM_WEEKS


class ServiceError(Exception):
    """A user-facing failure (for example, no model has been trained yet)."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_UPSERT_STUDENT = """
INSERT INTO students (student_number, first_name, last_name, gender, date_of_birth, faculty, programme,
                      year_level, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (student_number) DO UPDATE SET
    first_name = excluded.first_name,
    last_name = excluded.last_name,
    gender = COALESCE(excluded.gender, students.gender),
    date_of_birth = COALESCE(excluded.date_of_birth, students.date_of_birth),
    faculty = COALESCE(excluded.faculty, students.faculty),
    programme = COALESCE(excluded.programme, students.programme),
    year_level = COALESCE(excluded.year_level, students.year_level)
"""
_UPSERT_ACADEMIC = """
INSERT INTO academic_records (student_id, academic_year, semester, assignment_average, test_average,
                              examination_mark, gpa, academic_outcome)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (student_id, academic_year) DO UPDATE SET
    semester = COALESCE(excluded.semester, academic_records.semester),
    gpa = COALESCE(excluded.gpa, academic_records.gpa),
    assignment_average = COALESCE(excluded.assignment_average, academic_records.assignment_average),
    test_average = COALESCE(excluded.test_average, academic_records.test_average),
    examination_mark = COALESCE(excluded.examination_mark, academic_records.examination_mark),
    academic_outcome = COALESCE(excluded.academic_outcome, academic_records.academic_outcome)
"""
_UPSERT_ATTENDANCE = """
INSERT INTO attendance (student_id, academic_year, attendance_percentage) VALUES (?, ?, ?)
ON CONFLICT (student_id, academic_year) DO UPDATE SET
    attendance_percentage = COALESCE(excluded.attendance_percentage, attendance.attendance_percentage)
"""
_UPSERT_LMS = """
INSERT INTO lms_activity (student_id, academic_year, login_frequency, assignment_submissions,
                          resource_downloads, discussion_participation, average_session_duration)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (student_id, academic_year) DO UPDATE SET
    login_frequency = COALESCE(excluded.login_frequency, lms_activity.login_frequency),
    assignment_submissions = COALESCE(excluded.assignment_submissions, lms_activity.assignment_submissions),
    resource_downloads = COALESCE(excluded.resource_downloads, lms_activity.resource_downloads),
    discussion_participation = COALESCE(excluded.discussion_participation, lms_activity.discussion_participation),
    average_session_duration = COALESCE(excluded.average_session_duration, lms_activity.average_session_duration)
"""


def import_records(conn, records: list[dict], user_id: int) -> dict:
    """Save validated records in one transaction. Blank cells never overwrite stored values."""
    created = updated = 0
    student_ids: dict[str, int] = {}
    now = utc_now_iso()
    with conn:
        for rec in records:
            number = rec["student_number"]
            if number not in student_ids:
                existed = conn.execute(
                    "SELECT 1 FROM students WHERE student_number = ?", (number,)
                ).fetchone() is not None
                conn.execute(_UPSERT_STUDENT, (
                    number, rec["first_name"], rec["last_name"], rec["gender"], rec["date_of_birth"],
                    rec["faculty"], rec["programme"], rec["year_level"], now,
                ))
                student_ids[number] = conn.execute(
                    "SELECT id FROM students WHERE student_number = ?", (number,)
                ).fetchone()["id"]
                if existed:
                    updated += 1
                else:
                    created += 1
            sid, year = student_ids[number], rec["academic_year"]
            conn.execute(_UPSERT_ACADEMIC, (
                sid, year, rec["semester"], rec["assignment_average"], rec["test_average"],
                rec["examination_mark"], rec["gpa"], rec["academic_outcome"],
            ))
            if rec["attendance_percentage"] is not None:
                conn.execute(_UPSERT_ATTENDANCE, (sid, year, rec["attendance_percentage"]))
            lms_values = [rec[c] for c in (
                "login_frequency", "assignment_submissions", "resource_downloads",
                "discussion_participation", "average_session_duration")]
            if any(v is not None for v in lms_values):
                conn.execute(_UPSERT_LMS, (sid, year, *lms_values))
    summary = {"studentsCreated": created, "studentsUpdated": updated, "recordsSaved": len(records)}
    audit.record(conn, "import_completed", json.dumps(summary), user_id=user_id)
    return summary


_RECORD_ROWS_SQL = """
SELECT s.id AS student_id, a.academic_year, a.academic_outcome AS outcome,
       a.gpa, a.assignment_average, a.test_average,
       t.attendance_percentage, l.login_frequency, l.assignment_submissions,
       l.resource_downloads, l.discussion_participation, l.average_session_duration
FROM academic_records a
JOIN students s ON s.id = a.student_id
LEFT JOIN attendance t ON t.student_id = a.student_id AND t.academic_year = a.academic_year
LEFT JOIN lms_activity l ON l.student_id = a.student_id AND l.academic_year = a.academic_year
"""


def _features(row) -> dict:
    weekly = None if row["login_frequency"] is None else row["login_frequency"] / TERM_WEEKS
    return {
        "gpa": row["gpa"],
        "assignment_average": row["assignment_average"],
        "test_average": row["test_average"],
        "attendance_percentage": row["attendance_percentage"],
        "weekly_login_average": weekly,
        "assignment_submissions": row["assignment_submissions"],
        "resource_downloads": row["resource_downloads"],
        "discussion_participation": row["discussion_participation"],
        "average_session_duration": row["average_session_duration"],
    }


def load_training_rows(conn) -> list[dict]:
    rows = conn.execute(_RECORD_ROWS_SQL + " WHERE a.academic_outcome IS NOT NULL ORDER BY a.academic_year, s.id").fetchall()
    return [
        {"student_id": r["student_id"], "academic_year": r["academic_year"],
         "label": r["outcome"], "features": _features(r)}
        for r in rows
    ]


def load_current_rows(conn) -> tuple[int | None, list[dict]]:
    latest = conn.execute("SELECT MAX(academic_year) AS y FROM academic_records").fetchone()["y"]
    if latest is None:
        return None, []
    rows = conn.execute(_RECORD_ROWS_SQL + " WHERE a.academic_year = ? ORDER BY s.id", (latest,)).fetchall()
    return latest, [{"student_id": r["student_id"], "features": _features(r)} for r in rows]


def train_and_store(conn, user_id: int) -> dict:
    try:
        result = ml.train(load_training_rows(conn))
    except ml.ModelError as error:
        raise ServiceError(str(error)) from error
    version = "radar-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    validation = {k: v for k, v in result.items() if k not in ("artifact", "metrics")}
    with conn:
        conn.execute("UPDATE model_versions SET is_active = 0")
        conn.execute(
            "INSERT INTO model_versions (version, algorithm, implementation, artifact, metrics, validation, "
            "is_active, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (version, result["selectedAlgorithm"],
             next(m["implementation"] for m in result["metrics"] if m["algorithm"] == result["selectedAlgorithm"]),
             result["artifact"], json.dumps(result["metrics"]), json.dumps(validation), user_id, utc_now_iso()),
        )
    audit.record(conn, "model_trained", f"{version}: selected {result['selectedAlgorithm']}", user_id=user_id)
    return {"version": version, "selectedAlgorithm": result["selectedAlgorithm"], "metrics": result["metrics"],
            "validationAcademicYear": result["validationAcademicYear"],
            "trainingRows": result["trainingRows"], "validationRows": result["validationRows"],
            "warnings": result["warnings"]}


def score_cohort(conn, user_id: int) -> dict:
    active = conn.execute("SELECT * FROM model_versions WHERE is_active = 1").fetchone()
    if active is None:
        raise ServiceError("Train a model before scoring the cohort.")
    academic_year, rows = load_current_rows(conn)
    if academic_year is None or not rows:
        raise ServiceError("There are no academic records for the newest academic year to score.")
    scorable = [r for r in rows if any(v is not None for v in r["features"].values())]
    unscored = len(rows) - len(scorable)
    if not scorable:
        raise ServiceError("None of the current students has academic or engagement data to score.")

    bundle = ml.load_bundle(active["artifact"])
    probabilities = ml.predict_probabilities(bundle, [r["features"] for r in scorable])
    run_id = uuid.uuid4().hex
    now = utc_now_iso()
    with conn:
        for row, probability in zip(scorable, probabilities):
            explanation = ml.explain(bundle, row["features"])
            conn.execute(
                "INSERT INTO predictions (run_id, student_id, academic_year, risk_level, failure_probability, "
                "model_version_id, explanation, prediction_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, row["student_id"], academic_year, ml.risk_level(float(probability)),
                 round(float(probability), 4), active["id"], json.dumps(explanation), now),
            )
    summary = {"runId": run_id, "academicYear": academic_year, "scored": len(scorable),
               "unscored": unscored, "modelVersion": active["version"]}
    audit.record(conn, "cohort_scored", json.dumps(summary), user_id=user_id)
    return summary


def add_intervention(conn, student_id: int, user_id: int, intervention_type: str,
                     description: str, follow_up_date: str | None) -> int:
    now = utc_now_iso()
    with conn:
        cursor = conn.execute(
            "INSERT INTO interventions (student_id, user_id, intervention_type, description, status, "
            "intervention_date, follow_up_date, updated_at) VALUES (?, ?, ?, ?, 'Open', ?, ?, ?)",
            (student_id, user_id, intervention_type, description, now, follow_up_date, now),
        )
    audit.record(conn, "intervention_created", f"student {student_id}: {intervention_type}", user_id=user_id)
    return cursor.lastrowid
