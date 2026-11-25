"""Sign-in, sign-out, and password change (design §9.3, §9.5)."""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import audit
from ..db import get_db
from ..security import LOCK_MINUTES, LOCK_THRESHOLD, iso, login_required, password_problems, utcnow

bp = Blueprint("auth", __name__)
_DUMMY_HASH = generate_password_hash("timing-equaliser")  # keeps unknown-email responses as slow as real ones


def _safe_next(target: str | None) -> str | None:
    """Only allow same-site relative redirects after sign-in."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", email="")

    email = (request.form.get("email") or "").strip().lower()[:255]
    password = request.form.get("password") or ""
    conn = get_db()
    now = utcnow()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if user is None:
        check_password_hash(_DUMMY_HASH, password)
        audit.record(conn, "login_failed", "unknown account", email=email)
        return _failed(email)

    if user["locked_until"] and datetime.fromisoformat(user["locked_until"]) > now:
        audit.record(conn, "login_locked", "attempt during lock", user_id=user["id"], email=email)
        flash("This account is temporarily locked after repeated failed sign-ins. Try again later.", "danger")
        return render_template("login.html", email=email), 423

    if not user["is_active"]:
        audit.record(conn, "login_inactive", "deactivated account", user_id=user["id"], email=email)
        return _failed(email)

    if not check_password_hash(user["password_hash"], password):
        failures = user["failed_logins"] + 1
        locked_until = iso(now + timedelta(minutes=LOCK_MINUTES)) if failures >= LOCK_THRESHOLD else None
        with conn:
            conn.execute(
                "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                (0 if locked_until else failures, locked_until, user["id"]),
            )
        audit.record(conn, "login_failed", f"failed attempt {failures}", user_id=user["id"], email=email)
        return _failed(email)

    with conn:
        conn.execute(
            "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login = ? WHERE id = ?",
            (iso(now), user["id"]),
        )
    session.clear()
    session["user_id"] = user["id"]
    session["last_seen"] = iso(now)
    audit.record(conn, "login", user_id=user["id"], email=email)
    return redirect(_safe_next(request.args.get("next")) or url_for("records.dashboard"))


def _failed(email: str):
    flash("Email or password is not correct.", "danger")
    return render_template("login.html", email=email), 401


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.record(get_db(), "logout", user_id=g.user["id"], email=g.user["email"])
    session.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template("change_password.html")
    conn = get_db()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if not check_password_hash(row["password_hash"], current):
        flash("The current password is not correct.", "danger")
    elif new != confirm:
        flash("The new passwords do not match.", "danger")
    elif password_problems(new):
        flash("The new password must " + ", ".join(password_problems(new)) + ".", "danger")
    else:
        with conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (generate_password_hash(new), g.user["id"]))
        audit.record(conn, "password_changed", user_id=g.user["id"], email=g.user["email"])
        flash("Your password has been changed.", "success")
        return redirect(url_for("records.dashboard"))
    return render_template("change_password.html"), 400
