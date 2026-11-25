# RADAR Early Warning System (prototype)

Machine-learning early-warning prototype for first-year academic risk, built from the Design Phase
document (CPT580S). Flask + SQLite + scikit-learn, with a Plotly.js dashboard. **All records are fictional.**

## Run it in VS Code (Windows, macOS or Linux)

1. Install **Python 3.11 or newer** and the **Python** extension in VS Code.
2. Open the `RADAR` folder in VS Code (File → Open Folder).
3. Open a terminal (Terminal → New Terminal) and create an environment:
   ```bash
   python -m venv .venv
   # Windows PowerShell:  .venv\Scripts\Activate.ps1
   # macOS / Linux:       source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```
   If `xgboost` or `shap` fails to install, delete those two lines from `requirements.txt`. The app still runs (see *Known limits*).
4. Build the fictional demo data (students, three cohorts, a trained model, predictions):
   ```bash
   python -m radar.seed --force
   ```
5. Start the app with **Run → Start Debugging** (choose *RADAR: run web app*) or:
   ```bash
   python run.py
   ```
   Open **http://127.0.0.1:5000**.
6. Optional but recommended: set a secret key first so sessions survive restarts.
   PowerShell: `$env:RADAR_SECRET_KEY="a-long-random-string"` · macOS/Linux: `export RADAR_SECRET_KEY="..."`

### Demo accounts (password for all: `RadarDemo#2026`)

| Email | Role | Can see |
|---|---|---|
| admin@radar.example | System Administrator | Everything: import, model, users, audit |
| lecturer@radar.example | Lecturer | Students, predictions, interventions, reports |
| advisor@radar.example | Academic Advisor | As lecturer, plus record and update interventions |
| faculty@radar.example | Faculty Administrator | Overview and faculty-level reports only (no names) |

Change these passwords before any shared use. Admins can add accounts under **Users**.

### Run the tests

```bash
python -m unittest discover -t . -s tests -v
```
47 tests cover CSV validation, the model rules, sign-in and lockout, CSRF, roles, session timeout, import,
scoring, interventions, exports, audit logging, and every page for every role.

### Back up the database

```bash
python -m radar.backup
```
Writes `backups/radar-<timestamp>.db` and checks row counts against the live database.

## Importing real data (only with approval)

Use **Import data** (System Administrator). The CSV template is in `sample_data/import_template.csv`.
- Required columns: `student_number`, `first_name`, `last_name`, `academic_year`.
- `academic_outcome`: `0` = successful, `1` = unsuccessful, blank = not yet known.
- Dates: `YYYY-MM-DD` or `DD/MM/YYYY`. Limits: 5 MB and 5,000 rows.
- All-or-nothing: if any row fails, nothing is saved and every problem is listed with its line number.
- Blank cells never overwrite values already stored.

Do not load real student data until the institution's ethics approval and POPIA processes are complete.

## How the design is met

| Design requirement | Where it lives |
|---|---|
| FR01 Authentication | `views/auth_views.py`, `security.py`: hashed passwords, lockout after 5 failures, generic error messages |
| FR02 Student records | Student list, profile, edit (`views/records_views.py`) |
| FR03/FR04 Import and preprocessing | `preprocessing.py`, `services.import_records` |
| FR05/FR06 Predictions and Low/Medium/High bands | `ml.py`, `services.score_cohort` (bands: <30%, 30–59%, ≥60%) |
| FR07 Explanations (SHAP) | `ml.explain`, shown on the Risk explanation page |
| FR08 Dashboard | `analytics.py`, `templates/dashboard.html` (Plotly.js) |
| FR09 Reports | Reports page with CSV export; faculty-only role gets aggregates |
| FR10 Interventions | Record and update on each student; interventions list |
| FR11 User accounts | Users page (create, change role, deactivate, reset password) |
| FR12 Prediction history | `predictions` table; history on each student profile |
| §9.4 Roles (RBAC) | `role_required` on every route; denials are audited |
| §9.5 Password security | Hashing, complexity rules, lockout, 30-minute inactivity timeout |
| §9.8 Audit logging | `audit_log` table and Audit log page |
| §9.9 Backup | `radar/backup.py`, verified copy |
| §9.10 Threats | Parameterised SQL, CSRF tokens, output escaping, security headers |
| §3.4 Preprocessing | Duplicate removal, validation, median imputation fitted on training data only |
| §3.5/3.6 Four models, metrics | Logistic Regression, Decision Tree, Random Forest, XGBoost; F1 selection |

## Known limits (challenges)

1. **Database is SQLite, not MySQL.** It needs no server, so the app runs straight from VS Code. Moving to MySQL means changing the connection in `db.py` and the `ON CONFLICT` upserts in `services.py`. The schema is plain SQL.
2. **SHAP and XGBoost may not be installed on your machine.** When SHAP is missing, the app uses an *occlusion estimate* and labels it as such. When XGBoost is missing, it uses scikit-learn's HistGradientBoosting and labels that too. Exact linear SHAP is used for Logistic Regression. The tree-model SHAP path is written but was not run against the real `shap` library during the build, so check it once on your machine.
3. **Plotly charts** are specified in Python and drawn by Plotly.js in the browser (from a CDN). Python's `plotly` package is not used.
4. **Data is fictional.** It is generated by `radar/seed.py` with a fixed random seed. Real results depend on real institutional data.
5. **Sign-in is local accounts, not university single sign-on.** There is no multi-factor authentication. Both are needed for production.
6. **Scope:** email notifications, counselling booking and university system integration are excluded, as the design states.
7. **Build environment:** tested on Linux with Python 3.12 (unit, integration and page tests all pass). Not yet run on Windows.

## Project layout

```
run.py                  start the web app
requirements.txt        Python dependencies
radar/                  application package
  __init__.py           app factory and error pages
  schema.sql            database tables (design data dictionary)
  db.py  audit.py  security.py  preprocessing.py  ml.py  services.py  analytics.py
  views/                auth, records, admin routes
  templates/  static/   HTML (Bootstrap 5) and CSS
  seed.py  backup.py    fictional demo data; database backup
tests/                  unit and application tests
sample_data/            import template and fictional example file
instance/radar.db       database file (created by the seed script)
```
