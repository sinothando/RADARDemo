"""Authentication guards, role-based access control (design §9.4), CSRF protection,
session timeout (design §9.5) and HTTP security headers."""
from __future__ import annotations

import functools
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, current_app, flash, g, redirect, request, session, url_for

from . import audit
from .db import get_db

ROLE_LABELS = {
    "system_admin": "System Administrator",
    "lecturer": "Lecturer",
    "academic_advisor": "Academic Advisor",
    "faculty_admin": "Faculty Administrator",
}
LOCK_THRESHOLD = 5          # failed sign-ins before a lock
LOCK_MINUTES = 15
PASSWORD_MIN_LENGTH = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def password_problems(password: str) -> list[str]:
    """Return the complexity rules the password breaks (empty list = acceptable)."""
    problems = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"be at least {PASSWORD_MIN_LENGTH} characters long")
    if not re.search(r"[A-Z]", password):
        problems.append("include an uppercase letter")
    if not re.search(r"[a-z]", password):
        problems.append("include a lowercase letter")
    if not re.search(r"\d", password):
        problems.append("include a number")
    return problems


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def load_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    if not user_id:
        return
    timeout = timedelta(minutes=current_app.config["SESSION_TIMEOUT_MINUTES"])
    last_seen = session.get("last_seen")
    if not last_seen or utcnow() - datetime.fromisoformat(last_seen) > timeout:
        session.clear()
        flash("You were signed out after a period of inactivity. Sign in again.", "warning")
        return
    row = get_db().execute(
        "SELECT id, full_name, email, role, faculty, is_active FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None or not row["is_active"]:
        session.clear()
        return
    g.user = dict(row)
    session["last_seen"] = iso(utcnow())


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles: str):
    """Allow only the listed roles. Denials are written to the audit log."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("auth.login", next=request.path))
            if g.user["role"] not in roles:
                audit.record(get_db(), "access_denied", request.path, user_id=g.user["id"])
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def install_security(app) -> None:
    app.jinja_env.globals.update(csrf_token=csrf_token, ROLE_LABELS=ROLE_LABELS)

    @app.before_request
    def _guard():
        if request.endpoint == "static":
            return None
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            expected = session.get("csrf", "")
            if not expected or not secrets.compare_digest(sent, expected):
                abort(400, "Your form has expired. Refresh the page and try again.")
        load_user()
        return None

    @app.after_request
    def _headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response
