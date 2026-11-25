"""Audit trail for logins, data changes, model runs, and access denials (design §9.8)."""
from __future__ import annotations

from datetime import datetime, timezone

from flask import has_request_context, request


def record(conn, action: str, detail: str = "", user_id=None, email=None) -> None:
    ip = request.remote_addr if has_request_context() else None
    conn.execute(
        "INSERT INTO audit_log (occurred_at, user_id, email, action, detail, ip_address) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            user_id,
            email,
            action,
            (detail or "")[:500],
            ip,
        ),
    )
    conn.commit()
