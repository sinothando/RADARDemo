"""Validate and clean institutional CSV uploads (design §3.4 and §4.1).

Policy: an upload is all-or-nothing. If any row has a problem, nothing is imported and
every problem is listed with its line number, so the file can be corrected and re-uploaded.
"""
from __future__ import annotations

import io
import math
import re
from datetime import datetime

import pandas as pd

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
REQUIRED_COLUMNS = ("student_number", "first_name", "last_name", "academic_year")
STUDENT_COLUMNS = ("first_name", "last_name", "gender", "date_of_birth", "faculty", "programme", "year_level")
ACADEMIC_COLUMNS = ("semester", "gpa", "assignment_average", "test_average", "examination_mark", "academic_outcome")
ATTENDANCE_COLUMNS = ("attendance_percentage",)
LMS_COLUMNS = (
    "login_frequency",
    "assignment_submissions",
    "resource_downloads",
    "discussion_participation",
    "average_session_duration",
)
TEMPLATE_COLUMNS = (
    "student_number", "first_name", "last_name", "gender", "date_of_birth", "faculty", "programme",
    "year_level", "academic_year", "semester", "gpa", "assignment_average", "test_average",
    "examination_mark", "academic_outcome", "attendance_percentage", "login_frequency",
    "assignment_submissions", "resource_downloads", "discussion_participation",
    "average_session_duration",
)
KNOWN_COLUMNS = frozenset(TEMPLATE_COLUMNS)
TEXT_COLUMNS = frozenset({"student_number", "first_name", "last_name", "gender", "faculty", "programme", "semester"})
# column -> (minimum, maximum, kind); None means no bound.
RANGES = {
    "academic_year": (2000, 2100, int),
    "year_level": (1, 6, int),
    "gpa": (0, 4, float),
    "assignment_average": (0, 100, float),
    "test_average": (0, 100, float),
    "examination_mark": (0, 100, float),
    "attendance_percentage": (0, 100, float),
    "login_frequency": (0, None, int),
    "assignment_submissions": (0, None, int),
    "resource_downloads": (0, None, int),
    "discussion_participation": (0, None, int),
    "average_session_duration": (0, None, float),
}


def parse_date(value: str) -> str:
    """Accept YYYY-MM-DD or DD/MM/YYYY (South African style); return ISO text."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(value)


def _coerce(column: str, raw: str):
    """Return (value, problem). problem is None when the value is acceptable."""
    if column in TEXT_COLUMNS:
        return raw, None
    if column == "date_of_birth":
        try:
            return parse_date(raw), None
        except ValueError:
            return None, "use YYYY-MM-DD or DD/MM/YYYY"
    if column == "academic_outcome":
        if raw in ("0", "1"):
            return int(raw), None
        return None, "must be 0 (successful) or 1 (unsuccessful), or left blank"
    low, high, kind = RANGES[column]
    try:
        number = float(raw)
    except ValueError:
        return None, "must be a number"
    if not math.isfinite(number):
        return None, "must be a finite number"
    if kind is int:
        if number != int(number):
            return None, "must be a whole number"
        number = int(number)
    if (low is not None and number < low) or (high is not None and number > high):
        if high is None:
            return None, f"must be {low} or more"
        return None, f"must be between {low} and {high}"
    return number, None


def _failure(error: str, rows: int = 0, ignored=None) -> dict:
    return {
        "ok": False, "error": error, "rowsReceived": rows, "duplicateRowsRemoved": 0,
        "records": [], "issues": [], "missingValues": {}, "ignoredColumns": ignored or [],
    }


def prepare_csv(raw) -> dict:
    """Validate a CSV upload (bytes or text) and return cleaned records or a list of issues."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_UPLOAD_BYTES:
            return _failure("The CSV file exceeds the 5 MB upload limit.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _failure("The file must be a UTF-8 encoded CSV.")
    else:
        text = raw
    if not text.strip():
        return _failure("The CSV file is empty.")
    try:
        frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    except Exception:
        return _failure("The file could not be read as a valid CSV.")

    frame.columns = [re.sub(r"[\s\-]+", "_", str(c).strip().lower()) for c in frame.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    ignored = [c for c in frame.columns if c not in KNOWN_COLUMNS]
    if missing:
        return _failure(f"Required columns are missing: {', '.join(missing)}.", ignored=ignored)
    rows = len(frame)
    if rows == 0:
        return _failure("The CSV has no data rows.", ignored=ignored)
    if rows > MAX_ROWS:
        return _failure(f"The CSV has {rows:,} rows. The limit is {MAX_ROWS:,} rows per upload.", rows, ignored)

    issues: list[dict] = []
    accepted: list[dict] = []
    seen_rows: set = set()
    duplicates = 0
    year_seen: set = set()
    student_profile: dict = {}
    missing_counts = {c: 0 for c in TEMPLATE_COLUMNS}
    usable_columns = [c for c in frame.columns if c in KNOWN_COLUMNS]

    for index, raw_row in enumerate(frame.to_dict("records")):
        line = index + 2  # header is line 1
        record: dict = {}
        row_ok = True
        for column in usable_columns:
            text_value = str(raw_row.get(column, "")).strip()
            if text_value == "":
                record[column] = None
                continue
            value, problem = _coerce(column, text_value)
            if problem:
                issues.append({"row": line, "column": column, "message": problem})
                row_ok = False
            record[column] = value
        for column in REQUIRED_COLUMNS:
            if record.get(column) in (None, ""):
                issues.append({"row": line, "column": column, "message": "is required"})
                row_ok = False
        if not row_ok:
            continue

        for column in TEMPLATE_COLUMNS:
            record.setdefault(column, None)
        key = tuple(sorted(record.items()))
        if key in seen_rows:
            duplicates += 1
            continue
        seen_rows.add(key)

        student_number, year = record["student_number"], record["academic_year"]
        if (student_number, year) in year_seen:
            issues.append({
                "row": line, "column": "academic_year",
                "message": f"duplicates a different row for student {student_number} in {year}",
            })
            continue
        year_seen.add((student_number, year))

        profile = tuple(record[c] for c in STUDENT_COLUMNS)
        if student_number in student_profile and student_profile[student_number] != profile:
            issues.append({
                "row": line, "column": "student_number",
                "message": f"student {student_number} has different personal details in another row",
            })
            continue
        student_profile[student_number] = profile

        for column in TEMPLATE_COLUMNS:
            if record[column] is None:
                missing_counts[column] += 1
        accepted.append(record)

    if issues:
        return {
            "ok": False,
            "error": f"{len(issues)} problem(s) found. Nothing was imported. Correct the rows listed and upload again.",
            "rowsReceived": rows, "duplicateRowsRemoved": duplicates, "records": [],
            "issues": issues, "missingValues": {}, "ignoredColumns": ignored,
        }
    return {
        "ok": True, "error": None, "rowsReceived": rows, "duplicateRowsRemoved": duplicates,
        "records": accepted, "issues": [], "missingValues": missing_counts, "ignoredColumns": ignored,
    }
