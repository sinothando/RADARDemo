"""Scheduled-style database backup with verification (design §9.9).

Run:  python -m radar.backup            (writes backups/radar-YYYYmmdd-HHMMSS.db and verifies it)
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

TABLES = ("users", "students", "academic_records", "predictions", "interventions", "audit_log")


def backup(source: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"radar-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)  # consistent copy even while the app is running
    finally:
        dst.close()
        src.close()
    verify(source, target)
    return target


def verify(source: Path, backup_file: Path) -> None:
    """Compare row counts so a silently incomplete backup is caught immediately."""
    with sqlite3.connect(str(source)) as a, sqlite3.connect(str(backup_file)) as b:
        for table in TABLES:
            expected = a.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            actual = b.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if expected != actual:
                raise RuntimeError(f"Backup verification failed for {table}: {actual} of {expected} rows")


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "instance" / "radar.db"
    if not db.exists():
        print(f"No database found at {db}")
        sys.exit(1)
    created = backup(db, PROJECT_ROOT / "backups")
    print(f"Backup written and verified: {created}")
