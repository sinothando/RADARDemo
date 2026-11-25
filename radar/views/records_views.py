"""Overview, student records, imports, predictions, interventions, and reports."""
from __future__ import annotations

import csv
import io
import json

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .. import analytics, audit, preprocessing, services
from ..db import get_db
from ..security import login_required, role_required

bp = Blueprint("records", __name__)
STAFF = ("system_admin", "lecturer", "academic_advisor")
ADVISING = ("system_admin", "academic_advisor")
INTERVENTION_STATUSES = ("Open", "In progress", "Completed")
INTERVENTION_TYPES = (
    "Academic check-in", "Tutoring referral", "Study-skills workshop",
    "Wellbeing referral", "Financial aid review", "Other",
)
EDITABLE_FIELDS = ("first_name", "last_name", "gender", "date_of_birth", "faculty", "programme", "year_level")
RECORDS_SQL = """
SELECT a.academic_year, a.semester, a.gpa, a.assignment_average, a.test_average, a.academic_outcome,
       t.attendance_percentage, l.login_frequency, l.assignment_submissions, l.resource_downloads,
       l.discussion_participation, l.average_session_duration
FROM academic_records a
LEFT JOIN attendance t ON t.student_id = a.student_id AND t.academic_year = a.academic_year
LEFT JOIN lms_activity l ON l.student_id = a.student_id AND l.academic_year = a.academic_year
WHERE a.student_id = ? ORDER BY a.academic_year
"""


def _student_or_404(conn, student_id: int):
    row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/")
@login_required
def dashboard():
    data = analytics.dashboard(get_db(), g.user["role"])
    return render_template("dashboard.html", data=data)


@bp.route("/students")
@role_required(*STAFF)
def students():
    conn = get_db()
    query = request.args.get("q", "").strip()[:100]
    faculty = request.args.get("faculty", "")
    run = analytics.latest_run(conn)
    sql = (
        "SELECT s.*, p.risk_level, p.failure_probability FROM students s "
        "LEFT JOIN predictions p ON p.student_id = s.id AND p.run_id = ? WHERE 1 = 1"
    )
    params: list = [run]
    if query:
        sql += " AND (s.student_number LIKE ? OR s.first_name LIKE ? OR s.last_name LIKE ?)"
        params += [f"%{query}%"] * 3
    if faculty:
        sql += " AND s.faculty = ?"
        params.append(faculty)
    sql += " ORDER BY s.last_name, s.first_name LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    faculties = [r["faculty"] for r in conn.execute(
        "SELECT DISTINCT faculty FROM students WHERE faculty IS NOT NULL ORDER BY faculty")]
    return render_template("students.html", rows=rows, faculties=faculties, query=query, faculty=faculty)


@bp.route("/students/<int:student_id>")
@role_required(*STAFF)
def student_detail(student_id: int):
    conn = get_db()
    student = _student_or_404(conn, student_id)
    predictions = conn.execute(
        "SELECT p.*, mv.version, mv.algorithm FROM predictions p "
        "JOIN model_versions mv ON mv.id = p.model_version_id "
        "WHERE p.student_id = ? ORDER BY p.prediction_date DESC, p.id DESC LIMIT 12", (student_id,)
    ).fetchall()
    latest = predictions[0] if predictions else None
    interventions = conn.execute(
        "SELECT i.*, u.full_name AS recorded_by FROM interventions i LEFT JOIN users u ON u.id = i.user_id "
        "WHERE i.student_id = ? ORDER BY i.intervention_date DESC, i.id DESC", (student_id,)
    ).fetchall()
    return render_template(
        "student_detail.html",
        student=student,
        records=conn.execute(RECORDS_SQL, (student_id,)).fetchall(),
        predictions=predictions,
        latest=latest,
        explanation=json.loads(latest["explanation"]) if latest else None,
        interventions=interventions,
        intervention_types=INTERVENTION_TYPES,
        can_record=g.user["role"] in ADVISING,
    )


@bp.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@role_required("system_admin")
def student_edit(student_id: int):
    conn = get_db()
    student = _student_or_404(conn, student_id)
    if request.method == "GET":
        return render_template("student_form.html", student=student, values=dict(student))
    values = {f: (request.form.get(f) or "").strip() or None for f in EDITABLE_FIELDS}
    errors = []
    if not values["first_name"] or not values["last_name"]:
        errors.append("First name and last name are required.")
    if values["year_level"] is not None and values["year_level"] not in {"1", "2", "3", "4", "5", "6"}:
        errors.append("Year level must be a number from 1 to 6.")
    if values["date_of_birth"] is not None:
        try:
            values["date_of_birth"] = preprocessing.parse_date(values["date_of_birth"])
        except ValueError:
            errors.append("Date of birth must be YYYY-MM-DD or DD/MM/YYYY.")
    if errors:
        for message in errors:
            flash(message, "danger")
        return render_template("student_form.html", student=student, values=values), 422
    if values["year_level"] is not None:
        values["year_level"] = int(values["year_level"])
    with conn:
        conn.execute(
            "UPDATE students SET first_name = ?, last_name = ?, gender = ?, date_of_birth = ?, faculty = ?, "
            "programme = ?, year_level = ? WHERE id = ?",
            (*[values[f] for f in EDITABLE_FIELDS], student_id),
        )
    audit.record(conn, "student_updated", f"student {student_id}", user_id=g.user["id"])
    flash("Student details saved.", "success")
    return redirect(url_for("records.student_detail", student_id=student_id))


@bp.route("/students/import", methods=["GET", "POST"])
@role_required("system_admin")
def import_students():
    conn = get_db()
    if request.method == "GET":
        return render_template("import.html", result=None)
    upload = request.files.get("csv_file")
    if upload is None or not upload.filename:
        return render_template("import.html", result={"ok": False, "error": "Choose a CSV file to import."}), 400
    if not upload.filename.lower().endswith(".csv"):
        return render_template("import.html", result={"ok": False, "error": "The file must have a .csv extension."}), 400
    result = preprocessing.prepare_csv(upload.read(preprocessing.MAX_UPLOAD_BYTES + 1))
    if not result["ok"]:
        audit.record(conn, "import_rejected", f"{upload.filename}: {result['error']}", user_id=g.user["id"])
        return render_template("import.html", result=result), 422
    result["summary"] = services.import_records(conn, result["records"], g.user["id"])
    flash(f"Imported {result['summary']['recordsSaved']} records. "
          "Run Score the cohort on the Model page to refresh predictions.", "success")
    return render_template("import.html", result=result)


@bp.route("/students/import/template")
@role_required("system_admin")
def import_template():
    buffer = io.StringIO()
    csv.writer(buffer).writerow(preprocessing.TEMPLATE_COLUMNS)
    return Response(buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="radar-import-template.csv"'})


@bp.route("/students/<int:student_id>/interventions", methods=["POST"])
@role_required(*ADVISING)
def add_intervention(student_id: int):
    conn = get_db()
    _student_or_404(conn, student_id)
    kind = request.form.get("intervention_type", "")
    description = (request.form.get("description") or "").strip()[:1000]
    follow_up_raw = (request.form.get("follow_up_date") or "").strip()
    target = url_for("records.student_detail", student_id=student_id)
    if kind not in INTERVENTION_TYPES:
        flash("Choose a valid intervention type.", "danger")
        return redirect(target)
    if not description:
        flash("Add a short description of the action taken.", "danger")
        return redirect(target)
    try:
        follow_up = preprocessing.parse_date(follow_up_raw) if follow_up_raw else None
    except ValueError:
        flash("Enter the follow-up date as YYYY-MM-DD or DD/MM/YYYY.", "danger")
        return redirect(target)
    services.add_intervention(conn, student_id, g.user["id"], kind, description, follow_up)
    flash("Intervention recorded.", "success")
    return redirect(target)


@bp.route("/interventions")
@role_required(*STAFF)
def interventions():
    status = request.args.get("status", "")
    sql = (
        "SELECT i.*, s.student_number, s.first_name, s.last_name, s.faculty, u.full_name AS recorded_by "
        "FROM interventions i JOIN students s ON s.id = i.student_id LEFT JOIN users u ON u.id = i.user_id"
    )
    params: list = []
    if status in INTERVENTION_STATUSES:
        sql += " WHERE i.status = ?"
        params.append(status)
    sql += " ORDER BY i.status = 'Completed', i.follow_up_date IS NULL, i.follow_up_date, i.id DESC"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("interventions.html", rows=rows, status=status,
                           statuses=INTERVENTION_STATUSES, can_update=g.user["role"] in ADVISING)


@bp.route("/interventions/<int:intervention_id>/status", methods=["POST"])
@role_required(*ADVISING)
def update_intervention(intervention_id: int):
    conn = get_db()
    status = request.form.get("status", "")
    if status not in INTERVENTION_STATUSES:
        flash("Choose a valid status.", "danger")
        return redirect(url_for("records.interventions"))
    with conn:
        cursor = conn.execute("UPDATE interventions SET status = ?, updated_at = ? WHERE id = ?",
                              (status, services.utc_now_iso(), intervention_id))
    if cursor.rowcount == 0:
        abort(404)
    audit.record(conn, "intervention_updated", f"intervention {intervention_id}: {status}", user_id=g.user["id"])
    flash("Intervention status updated.", "success")
    return redirect(url_for("records.interventions"))


@bp.route("/predictions")
@role_required(*STAFF)
def predictions():
    conn = get_db()
    risk = request.args.get("risk", "")
    risk = risk if risk in analytics.RISK_LEVELS else None
    return render_template(
        "predictions.html",
        rows=analytics.latest_predictions(conn, risk=risk),
        risk=risk or "",
        model=analytics.active_model(conn),
        has_run=analytics.latest_run(conn) is not None,
    )


@bp.route("/predictions/<int:student_id>/explanation")
@role_required(*STAFF)
def explanation(student_id: int):
    conn = get_db()
    student = _student_or_404(conn, student_id)
    latest = conn.execute(
        "SELECT p.*, mv.version FROM predictions p JOIN model_versions mv ON mv.id = p.model_version_id "
        "WHERE p.student_id = ? ORDER BY p.prediction_date DESC, p.id DESC LIMIT 1", (student_id,)
    ).fetchone()
    if latest is None:
        flash("This student has not been scored yet.", "warning")
        return redirect(url_for("records.student_detail", student_id=student_id))
    return render_template("explanation.html", student=student, latest=latest,
                           explanation=json.loads(latest["explanation"]))


@bp.route("/reports")
@login_required
def reports():
    conn = get_db()
    return render_template(
        "reports.html",
        rows=analytics.faculty_summary(conn),
        can_detail=g.user["role"] in STAFF,
        has_run=analytics.latest_run(conn) is not None,
    )


@bp.route("/reports/export")
@login_required
def export_report():
    conn = get_db()
    level = request.args.get("level", "summary")
    if level == "students":
        if g.user["role"] not in STAFF:
            audit.record(conn, "access_denied", "student-level export", user_id=g.user["id"])
            abort(403)
        header, rows = analytics.student_csv_rows(conn)
    elif level == "summary":
        header, rows = analytics.summary_csv_rows(conn)
    else:
        abort(400)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    audit.record(conn, "report_exported", level, user_id=g.user["id"])
    return Response(buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="radar-{level}-report.csv"'})
