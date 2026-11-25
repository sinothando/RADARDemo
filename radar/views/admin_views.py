"""System administration: user accounts and roles (design FR11, §9.4), the model page, and the audit trail."""
from __future__ import annotations

import json
import re
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from .. import audit, services
from ..db import get_db
from ..security import ROLE_LABELS, password_problems, role_required, utcnow

bp = Blueprint("admin", __name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _user_errors(values: dict, password: str | None, creating: bool) -> list[str]:
    errors = []
    if not values["full_name"]:
        errors.append("Enter the person's full name.")
    if not EMAIL_RE.match(values["email"] or ""):
        errors.append("Enter a valid email address.")
    if values["role"] not in ROLE_LABELS:
        errors.append("Choose a valid role.")
    if creating or password:
        problems = password_problems(password or "")
        if problems:
            errors.append("The password must " + ", ".join(problems) + ".")
    return errors


@bp.route("/admin/users")
@role_required("system_admin")
def users():
    rows = get_db().execute(
        "SELECT id, full_name, email, role, faculty, is_active, last_login, locked_until "
        "FROM users ORDER BY is_active DESC, full_name"
    ).fetchall()
    return render_template("users.html", rows=rows)


@bp.route("/admin/users/new", methods=["GET", "POST"])
@role_required("system_admin")
def user_new():
    conn = get_db()
    if request.method == "GET":
        return render_template("user_form.html", user=None, values={"role": "lecturer"})
    values = {
        "full_name": (request.form.get("full_name") or "").strip(),
        "email": (request.form.get("email") or "").strip().lower(),
        "role": request.form.get("role", ""),
        "faculty": (request.form.get("faculty") or "").strip() or None,
    }
    password = request.form.get("password") or ""
    errors = _user_errors(values, password, creating=True)
    if errors:
        for message in errors:
            flash(message, "danger")
        return render_template("user_form.html", user=None, values=values), 422
    try:
        with conn:
            conn.execute(
                "INSERT INTO users (full_name, email, password_hash, role, faculty, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (values["full_name"], values["email"], generate_password_hash(password),
                 values["role"], values["faculty"], utcnow().isoformat(timespec="seconds")),
            )
    except sqlite3.IntegrityError:
        flash("An account with that email address already exists.", "danger")
        return render_template("user_form.html", user=None, values=values), 409
    audit.record(conn, "user_created", f"{values['email']} as {values['role']}", user_id=g.user["id"])
    flash("User account created. Share the temporary password securely.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/admin/users/<int:user_id>", methods=["GET", "POST"])
@role_required("system_admin")
def user_edit(user_id: int):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    if request.method == "GET":
        return render_template("user_form.html", user=user, values=dict(user))
    values = {
        "full_name": (request.form.get("full_name") or "").strip(),
        "email": user["email"],
        "role": request.form.get("role", ""),
        "faculty": (request.form.get("faculty") or "").strip() or None,
    }
    is_active = 1 if request.form.get("is_active") == "on" else 0
    password = request.form.get("password") or None
    errors = _user_errors(values, password, creating=False)
    if user_id == g.user["id"] and (values["role"] != "system_admin" or not is_active):
        errors.append("You cannot remove your own administrator access or deactivate your own account.")
    if errors:
        for message in errors:
            flash(message, "danger")
        return render_template("user_form.html", user=user, values={**values, "is_active": is_active}), 422

    changes = []
    for field, new in (("full_name", values["full_name"]), ("role", values["role"]),
                       ("faculty", values["faculty"]), ("is_active", is_active)):
        if user[field] != new:
            changes.append(f"{field}: {user[field]} -> {new}")
    with conn:
        conn.execute(
            "UPDATE users SET full_name = ?, role = ?, faculty = ?, is_active = ? WHERE id = ?",
            (values["full_name"], values["role"], values["faculty"], is_active, user_id),
        )
        if password:
            conn.execute("UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL WHERE id = ?",
                         (generate_password_hash(password), user_id))
            changes.append("password reset")
    audit.record(conn, "user_updated", f"{user['email']}: " + ("; ".join(changes) or "no field changes"),
                 user_id=g.user["id"])
    flash("User account saved.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/admin/audit")
@role_required("system_admin")
def audit_log():
    conn = get_db()
    action = request.args.get("action", "")
    sql = ("SELECT a.*, u.full_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id")
    params: list = []
    if action:
        sql += " WHERE a.action = ?"
        params.append(action)
    sql += " ORDER BY a.id DESC LIMIT 300"
    rows = conn.execute(sql, params).fetchall()
    actions = [r["action"] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return render_template("audit.html", rows=rows, actions=actions, action=action)


@bp.route("/admin/model")
@role_required("system_admin")
def model():
    conn = get_db()
    versions = []
    for row in conn.execute(
            "SELECT id, version, algorithm, implementation, metrics, validation, is_active, created_at "
            "FROM model_versions ORDER BY id DESC"):
        item = dict(row)
        item["metrics"] = json.loads(item["metrics"])
        item["validation"] = json.loads(item["validation"])
        versions.append(item)
    return render_template(
        "model.html",
        versions=versions,
        has_run=conn.execute("SELECT 1 FROM predictions LIMIT 1").fetchone() is not None,
    )


@bp.route("/admin/model/train", methods=["POST"])
@role_required("system_admin")
def train_model():
    try:
        summary = services.train_and_store(get_db(), g.user["id"])
    except services.ServiceError as error:
        flash(str(error), "danger")
        return redirect(url_for("admin.model"))
    flash(f"Trained {summary['version']}. Selected {summary['selectedAlgorithm']} by F1 on "
          f"{summary['validationAcademicYear']} data. Score the cohort to refresh predictions.", "success")
    return redirect(url_for("admin.model"))


@bp.route("/admin/model/score", methods=["POST"])
@role_required("system_admin")
def score_model():
    try:
        summary = services.score_cohort(get_db(), g.user["id"])
    except services.ServiceError as error:
        flash(str(error), "danger")
        return redirect(url_for("admin.model"))
    flash(f"Scored {summary['scored']} students for {summary['academicYear']}"
          f" ({summary['unscored']} had no data).", "success")
    return redirect(url_for("admin.model"))


@bp.route("/admin/model/<int:version_id>/activate", methods=["POST"])
@role_required("system_admin")
def activate_model(version_id: int):
    conn = get_db()
    with conn:
        cursor = conn.execute("UPDATE model_versions SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END",
                              (version_id,))
    if cursor.rowcount == 0:
        abort(404)
    audit.record(conn, "model_activated", f"model version id {version_id}", user_id=g.user["id"])
    flash("Model version activated. Score the cohort to refresh predictions.", "success")
    return redirect(url_for("admin.model"))
