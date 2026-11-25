"""Dashboard, report, and chart data. Charts are returned as Plotly figure specs (plain JSON)
and rendered in the browser by Plotly.js."""
from __future__ import annotations

import json

RISK_LEVELS = ("High", "Medium", "Low")
RISK_COLORS = {"High": "#c7655a", "Medium": "#d2a44f", "Low": "#55a99d"}
NAMED_ROLES = ("system_admin", "lecturer", "academic_advisor")  # roles allowed to see individual students


def latest_run(conn):
    row = conn.execute(
        "SELECT run_id, prediction_date FROM predictions ORDER BY prediction_date DESC, id DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else None


def active_model(conn):
    return conn.execute(
        "SELECT id, version, algorithm, implementation, metrics, validation, created_at "
        "FROM model_versions WHERE is_active = 1"
    ).fetchone()


def _top_drivers(explanation_json: str, count: int = 2) -> str:
    drivers = json.loads(explanation_json).get("drivers", [])[:count]
    return "; ".join(f"{d['feature']} ({d['direction']})" for d in drivers)


def latest_predictions(conn, risk: str | None = None) -> list[dict]:
    run = latest_run(conn)
    if run is None:
        return []
    sql = (
        "SELECT p.student_id, p.risk_level, p.failure_probability, p.explanation, p.academic_year, "
        "s.student_number, s.first_name, s.last_name, s.faculty, s.programme "
        "FROM predictions p JOIN students s ON s.id = p.student_id WHERE p.run_id = ?"
    )
    params: list = [run]
    if risk:
        sql += " AND p.risk_level = ?"
        params.append(risk)
    sql += " ORDER BY p.failure_probability DESC, s.last_name"
    out = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["drivers"] = _top_drivers(item.pop("explanation"))
        out.append(item)
    return out


def _risk_chart(counts: dict) -> dict:
    labels = list(counts)
    return {
        "data": [{
            "type": "pie", "labels": labels, "values": [counts[k] for k in labels], "hole": 0.45,
            "marker": {"colors": [RISK_COLORS[k] for k in labels]},
        }],
        "layout": {"height": 300, "margin": {"t": 10, "b": 10, "l": 10, "r": 10}, "showlegend": True},
    }


def _faculty_chart(table: dict) -> dict:
    faculties = sorted(table)
    traces = [{
        "type": "bar", "name": level,
        "x": faculties, "y": [table[f].get(level, 0) for f in faculties],
        "marker": {"color": RISK_COLORS[level]},
    } for level in RISK_LEVELS]
    return {"data": traces,
            "layout": {"barmode": "stack", "height": 320, "margin": {"t": 10, "b": 80, "l": 40, "r": 10},
                       "yaxis": {"title": "Students"}}}


def _outcome_chart(rows: list[dict]) -> dict:
    return {
        "data": [{"type": "scatter", "mode": "lines+markers", "name": "Unsuccessful (%)",
                  "x": [r["academic_year"] for r in rows], "y": [round(r["rate"], 1) for r in rows]}],
        "layout": {"height": 300, "margin": {"t": 10, "b": 40, "l": 50, "r": 10},
                   "yaxis": {"title": "% unsuccessful", "range": [0, 100]}, "xaxis": {"title": "Academic year"}},
    }


def dashboard(conn, role: str) -> dict:
    run = latest_run(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
    counts = {level: 0 for level in RISK_LEVELS}
    faculty_table: dict = {}
    if run:
        for row in conn.execute(
            "SELECT COALESCE(s.faculty, 'Unassigned') AS faculty, p.risk_level, COUNT(*) AS n "
            "FROM predictions p JOIN students s ON s.id = p.student_id WHERE p.run_id = ? "
            "GROUP BY 1, 2", (run,)):
            counts[row["risk_level"]] += row["n"]
            faculty_table.setdefault(row["faculty"], {})[row["risk_level"]] = row["n"]
    scored = sum(counts.values())
    outcome_rows = [dict(r) for r in conn.execute(
        "SELECT academic_year, 100.0 * AVG(academic_outcome) AS rate, COUNT(*) AS n "
        "FROM academic_records WHERE academic_outcome IS NOT NULL GROUP BY academic_year ORDER BY academic_year")]
    model = active_model(conn)
    high_risk = []
    if run and role in NAMED_ROLES:
        high_risk = latest_predictions(conn, risk="High")[:10]
    return {
        "total_students": total,
        "scored": scored,
        "risk_counts": counts,
        "high_risk": high_risk,
        "outcome_rows": outcome_rows,
        "model": dict(model) if model else None,
        "model_metrics": json.loads(model["metrics"]) if model else [],
        "run_id": run,
        "charts": {
            "risk": _risk_chart(counts) if scored else None,
            "faculty": _faculty_chart(faculty_table) if faculty_table else None,
            "outcome": _outcome_chart(outcome_rows) if outcome_rows else None,
        },
    }


def faculty_summary(conn) -> list[dict]:
    run = latest_run(conn)
    rows = []
    students_by_faculty = {r["faculty"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(faculty, 'Unassigned') AS faculty, COUNT(*) AS n FROM students GROUP BY 1")}
    risk_by_faculty: dict = {}
    if run:
        for r in conn.execute(
            "SELECT COALESCE(s.faculty, 'Unassigned') AS faculty, p.risk_level, COUNT(*) AS n, "
            "AVG(p.failure_probability) AS mean_p FROM predictions p JOIN students s ON s.id = p.student_id "
            "WHERE p.run_id = ? GROUP BY 1, 2", (run,)):
            entry = risk_by_faculty.setdefault(r["faculty"], {"High": 0, "Medium": 0, "Low": 0, "mean": []})
            entry[r["risk_level"]] += r["n"]
            entry["mean"].append((r["mean_p"], r["n"]))
    open_interventions = {r["faculty"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(s.faculty, 'Unassigned') AS faculty, COUNT(*) AS n FROM interventions i "
        "JOIN students s ON s.id = i.student_id WHERE i.status != 'Completed' GROUP BY 1")}
    for faculty in sorted(students_by_faculty):
        entry = risk_by_faculty.get(faculty, {"High": 0, "Medium": 0, "Low": 0, "mean": []})
        scored = entry["High"] + entry["Medium"] + entry["Low"]
        weighted = sum(p * n for p, n in entry["mean"])
        rows.append({
            "faculty": faculty, "students": students_by_faculty[faculty], "scored": scored,
            "high": entry["High"], "medium": entry["Medium"], "low": entry["Low"],
            "mean_probability": round(weighted / scored, 3) if scored else None,
            "open_interventions": open_interventions.get(faculty, 0),
        })
    return rows


def summary_csv_rows(conn) -> tuple[list[str], list[list]]:
    header = ["faculty", "students", "scored", "high", "medium", "low", "mean_probability", "open_interventions"]
    return header, [[r[h] for h in header] for r in faculty_summary(conn)]


def student_csv_rows(conn) -> tuple[list[str], list[list]]:
    header = ["student_number", "first_name", "last_name", "faculty", "programme",
              "academic_year", "risk_level", "failure_probability", "top_drivers"]
    rows = [[p["student_number"], p["first_name"], p["last_name"], p["faculty"], p["programme"],
             p["academic_year"], p["risk_level"], p["failure_probability"], p["drivers"]]
            for p in latest_predictions(conn)]
    return header, rows
