-- RADAR database schema (SQLite). Column names follow the design data dictionary (Chapter 5),
-- written in snake_case. Tables use plain SQL types so the schema can move to MySQL later.

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name       TEXT    NOT NULL,
    email           TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('system_admin', 'lecturer', 'academic_advisor', 'faculty_admin')),
    faculty         TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    last_login      TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_number  TEXT    NOT NULL UNIQUE,
    first_name      TEXT    NOT NULL,
    last_name       TEXT    NOT NULL,
    gender          TEXT,
    date_of_birth   TEXT,
    faculty         TEXT,
    programme       TEXT,
    year_level      INTEGER CHECK (year_level IS NULL OR year_level BETWEEN 1 AND 6),
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS academic_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id          INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    academic_year       INTEGER NOT NULL,
    semester            TEXT,
    gpa                 REAL CHECK (gpa IS NULL OR gpa BETWEEN 0 AND 4),
    assignment_average  REAL CHECK (assignment_average IS NULL OR assignment_average BETWEEN 0 AND 100),
    test_average        REAL CHECK (test_average IS NULL OR test_average BETWEEN 0 AND 100),
    examination_mark    REAL CHECK (examination_mark IS NULL OR examination_mark BETWEEN 0 AND 100),
    academic_outcome    INTEGER CHECK (academic_outcome IS NULL OR academic_outcome IN (0, 1)),
    UNIQUE (student_id, academic_year)
);

CREATE TABLE IF NOT EXISTS attendance (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id            INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    academic_year         INTEGER NOT NULL,
    attendance_percentage REAL CHECK (attendance_percentage IS NULL OR attendance_percentage BETWEEN 0 AND 100),
    UNIQUE (student_id, academic_year)
);

CREATE TABLE IF NOT EXISTS lms_activity (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id                INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    academic_year             INTEGER NOT NULL,
    login_frequency           INTEGER CHECK (login_frequency IS NULL OR login_frequency >= 0),
    assignment_submissions    INTEGER CHECK (assignment_submissions IS NULL OR assignment_submissions >= 0),
    resource_downloads        INTEGER CHECK (resource_downloads IS NULL OR resource_downloads >= 0),
    discussion_participation  INTEGER CHECK (discussion_participation IS NULL OR discussion_participation >= 0),
    average_session_duration  REAL CHECK (average_session_duration IS NULL OR average_session_duration >= 0),
    UNIQUE (student_id, academic_year)
);

CREATE TABLE IF NOT EXISTS model_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version         TEXT    NOT NULL UNIQUE,
    algorithm       TEXT    NOT NULL,
    implementation  TEXT    NOT NULL,
    artifact        BLOB    NOT NULL,
    metrics         TEXT    NOT NULL,
    validation      TEXT    NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    NOT NULL,
    student_id          INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    academic_year       INTEGER NOT NULL,
    risk_level          TEXT    NOT NULL CHECK (risk_level IN ('Low', 'Medium', 'High')),
    failure_probability REAL    NOT NULL,
    model_version_id    INTEGER NOT NULL REFERENCES model_versions(id),
    explanation         TEXT    NOT NULL,
    prediction_date     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_run ON predictions (run_id);
CREATE INDEX IF NOT EXISTS idx_predictions_student ON predictions (student_id, prediction_date);

CREATE TABLE IF NOT EXISTS interventions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id        INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    user_id           INTEGER REFERENCES users(id),
    intervention_type TEXT    NOT NULL,
    description       TEXT,
    status            TEXT    NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'In progress', 'Completed')),
    intervention_date TEXT    NOT NULL,
    follow_up_date    TEXT,
    updated_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT    NOT NULL,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    email       TEXT,
    action      TEXT    NOT NULL,
    detail      TEXT,
    ip_address  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (occurred_at);
