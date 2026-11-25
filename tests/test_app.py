"""End-to-end checks through the Flask test client, on a temporary fictional database."""
import csv
import io
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar import create_app
from radar.seed import DEMO_PASSWORD, build_demo

TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class RadarAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "test.db"
        cls.sample_dir = Path(cls.tmp.name) / "samples"
        cls.summary = build_demo(cls.db_path, students_per_cohort=60, sample_dir=cls.sample_dir)
        cls.app = create_app({"TESTING": True, "DATABASE": str(cls.db_path)})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.client = self.app.test_client()

    # helpers -----------------------------------------------------------
    def sign_in(self, email, password=DEMO_PASSWORD, client=None):
        client = client or self.client
        page = client.get("/login")
        token = TOKEN_RE.search(page.get_data(as_text=True)).group(1)
        return client.post("/login", data={"email": email, "password": password, "csrf_token": token})

    def post_form(self, url, data=None, client=None, files=None):
        client = client or self.client
        page = client.get("/")
        if page.status_code != 200:  # not signed in yet, so fetch any page that carries a token
            page = client.get("/login")
        token = TOKEN_RE.search(page.get_data(as_text=True)).group(1)
        payload = {**(data or {}), "csrf_token": token}
        if files:
            payload.update(files)
        return client.post(url, data=payload, content_type="multipart/form-data" if files else None)

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # authentication ----------------------------------------------------
    def test_correct_sign_in_reaches_overview(self):
        response = self.sign_in("admin@radar.example")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_wrong_password_and_unknown_email_look_identical(self):
        client_a, client_b = self.app.test_client(), self.app.test_client()
        wrong = self.sign_in("admin@radar.example", "not-the-password", client=client_a)
        unknown = self.sign_in("nobody@radar.example", "not-the-password", client=client_b)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertIn("Email or password is not correct.", wrong.get_data(as_text=True))
        self.assertIn("Email or password is not correct.", unknown.get_data(as_text=True))

    def test_account_locks_after_repeated_failures(self):
        client = self.app.test_client()
        for _ in range(5):
            self.sign_in("faculty@radar.example", "wrong-password-1", client=client)
        locked = self.sign_in("faculty@radar.example", DEMO_PASSWORD, client=client)
        self.assertEqual(locked.status_code, 423)
        conn = self.db()
        row = conn.execute("SELECT locked_until FROM users WHERE email = 'faculty@radar.example'").fetchone()
        conn.close()
        self.assertIsNotNone(row["locked_until"])
        # restore so later tests can sign in as this user
        conn = self.db()
        with conn:
            conn.execute("UPDATE users SET locked_until = NULL, failed_logins = 0 WHERE email = 'faculty@radar.example'")
        conn.close()

    def test_open_redirect_after_sign_in_is_blocked(self):
        client = self.app.test_client()
        page = client.get("/login")
        token = TOKEN_RE.search(page.get_data(as_text=True)).group(1)
        response = client.post("/login?next=//evil.example",
                               data={"email": "lecturer@radar.example", "password": DEMO_PASSWORD,
                                     "csrf_token": token})
        self.assertNotIn("evil.example", response.headers["Location"])

    def test_post_without_csrf_token_is_rejected(self):
        client = self.app.test_client()
        self.sign_in("admin@radar.example", client=client)
        response = client.post("/students/import", data={})
        self.assertEqual(response.status_code, 400)

    def test_session_times_out_after_inactivity(self):
        client = self.app.test_client()
        self.sign_in("lecturer@radar.example", client=client)
        with client.session_transaction() as session:
            session["last_seen"] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="seconds")
        response = client.get("/students")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_anonymous_users_are_sent_to_sign_in(self):
        response = self.app.test_client().get("/students")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_password_change_enforces_policy(self):
        client = self.app.test_client()
        self.sign_in("advisor@radar.example", client=client)
        response = self.post_form("/change-password", client=client, data={
            "current_password": DEMO_PASSWORD, "new_password": "short", "confirm_password": "short"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 10 characters", response.get_data(as_text=True))

    # roles -------------------------------------------------------------
    def test_lecturer_can_see_students_but_not_administration(self):
        client = self.app.test_client()
        self.sign_in("lecturer@radar.example", client=client)
        self.assertEqual(client.get("/students").status_code, 200)
        self.assertEqual(client.get("/admin/users").status_code, 403)
        self.assertEqual(client.get("/students/import").status_code, 403)

    def test_faculty_administrator_sees_aggregates_only(self):
        client = self.app.test_client()
        self.sign_in("faculty@radar.example", client=client)
        self.assertEqual(client.get("/students").status_code, 403)
        overview = client.get("/").get_data(as_text=True)
        self.assertIn("Faculty-level view", overview)
        self.assertEqual(client.get("/reports").status_code, 200)
        self.assertEqual(client.get("/reports/export?level=students").status_code, 403)
        self.assertEqual(client.get("/reports/export?level=summary").status_code, 200)

    def test_access_denials_are_audited(self):
        client = self.app.test_client()
        self.sign_in("faculty@radar.example", client=client)
        client.get("/students")
        conn = self.db()
        count = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'access_denied'").fetchone()[0]
        conn.close()
        self.assertGreater(count, 0)

    # records -----------------------------------------------------------
    def test_student_profile_shows_explanation_and_history(self):
        client = self.app.test_client()
        self.sign_in("advisor@radar.example", client=client)
        conn = self.db()
        student_id = conn.execute(
            "SELECT student_id FROM predictions ORDER BY failure_probability DESC LIMIT 1").fetchone()[0]
        conn.close()
        profile = client.get(f"/students/{student_id}").get_data(as_text=True)
        self.assertIn("Current risk", profile)
        self.assertIn("Prediction history", profile)
        explanation = client.get(f"/predictions/{student_id}/explanation")
        self.assertEqual(explanation.status_code, 200)
        self.assertIn("How this was calculated", explanation.get_data(as_text=True))

    def test_advisor_records_intervention_and_lecturer_cannot(self):
        advisor = self.app.test_client()
        self.sign_in("advisor@radar.example", client=advisor)
        conn = self.db()
        student_id = conn.execute("SELECT id FROM students ORDER BY id LIMIT 1").fetchone()[0]
        before = conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0]
        conn.close()
        response = self.post_form(f"/students/{student_id}/interventions", client=advisor, data={
            "intervention_type": "Tutoring referral", "description": "Referred to peer tutoring.",
            "follow_up_date": "2027-02-01"})
        self.assertEqual(response.status_code, 302)
        conn = self.db()
        after = conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0]
        conn.close()
        self.assertEqual(after, before + 1)

        lecturer = self.app.test_client()
        self.sign_in("lecturer@radar.example", client=lecturer)
        denied = self.post_form(f"/students/{student_id}/interventions", client=lecturer, data={
            "intervention_type": "Other", "description": "Should not save."})
        self.assertEqual(denied.status_code, 403)

    def test_intervention_status_can_be_updated(self):
        advisor = self.app.test_client()
        self.sign_in("advisor@radar.example", client=advisor)
        conn = self.db()
        intervention_id = conn.execute("SELECT id FROM interventions ORDER BY id LIMIT 1").fetchone()[0]
        conn.close()
        self.post_form(f"/interventions/{intervention_id}/status", client=advisor, data={"status": "Completed"})
        conn = self.db()
        status = conn.execute("SELECT status FROM interventions WHERE id = ?", (intervention_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "Completed")

    def test_exports_carry_expected_headers(self):
        client = self.app.test_client()
        self.sign_in("lecturer@radar.example", client=client)
        response = client.get("/reports/export?level=students")
        self.assertEqual(response.mimetype, "text/csv")
        header = next(csv.reader(io.StringIO(response.get_data(as_text=True))))
        self.assertIn("failure_probability", header)
        self.assertIn("top_drivers", header)

    # import and model --------------------------------------------------
    def test_admin_import_is_idempotent_and_validated(self):
        admin = self.app.test_client()
        self.sign_in("admin@radar.example", client=admin)
        sample = (self.sample_dir / "fictional_import_example.csv").read_bytes()
        response = self.post_form("/students/import", client=admin, files={
            "csv_file": (io.BytesIO(sample), "fictional_import_example.csv")})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Import complete", response.get_data(as_text=True))
        self.assertIn("0 new student", response.get_data(as_text=True))

    def test_invalid_import_saves_nothing(self):
        admin = self.app.test_client()
        self.sign_in("admin@radar.example", client=admin)
        conn = self.db()
        before = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        conn.close()
        bad = b"student_number,first_name,last_name,academic_year,gpa\nNEW1,Ada,One,2027,9.5\n"
        response = self.post_form("/students/import", client=admin, files={
            "csv_file": (io.BytesIO(bad), "bad.csv")})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Nothing was imported", response.get_data(as_text=True))
        conn = self.db()
        after = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)

    def test_retrain_and_rescore_through_the_admin_routes(self):
        admin = self.app.test_client()
        self.sign_in("admin@radar.example", client=admin)
        trained = self.post_form("/admin/model/train", client=admin)
        self.assertEqual(trained.status_code, 302)
        scored = self.post_form("/admin/model/score", client=admin)
        self.assertEqual(scored.status_code, 302)
        conn = self.db()
        versions = conn.execute("SELECT COUNT(*) FROM model_versions WHERE is_active = 1").fetchone()[0]
        conn.close()
        self.assertEqual(versions, 1)

    def test_only_admin_can_train(self):
        advisor = self.app.test_client()
        self.sign_in("advisor@radar.example", client=advisor)
        self.assertEqual(self.post_form("/admin/model/train", client=advisor).status_code, 403)

    # users and audit ---------------------------------------------------
    def test_admin_creates_user_with_policy_check_and_cannot_lock_self_out(self):
        admin = self.app.test_client()
        self.sign_in("admin@radar.example", client=admin)
        weak = self.post_form("/admin/users/new", client=admin, data={
            "full_name": "New Person", "email": "new.person@radar.example",
            "role": "lecturer", "faculty": "", "password": "weak"})
        self.assertEqual(weak.status_code, 422)
        strong = self.post_form("/admin/users/new", client=admin, data={
            "full_name": "New Person", "email": "new.person@radar.example",
            "role": "lecturer", "faculty": "", "password": "TempPassword1"})
        self.assertEqual(strong.status_code, 302)
        conn = self.db()
        admin_id = conn.execute("SELECT id FROM users WHERE email = 'admin@radar.example'").fetchone()[0]
        conn.close()
        self_demote = self.post_form(f"/admin/users/{admin_id}", client=admin, data={
            "full_name": "System Administrator (demo)", "role": "lecturer", "faculty": ""})
        self.assertEqual(self_demote.status_code, 422)

    def test_audit_log_is_admin_only_and_records_sign_ins(self):
        self.sign_in("lecturer@radar.example", client=self.app.test_client())
        admin = self.app.test_client()
        self.sign_in("admin@radar.example", client=admin)
        page = admin.get("/admin/audit").get_data(as_text=True)
        self.assertIn("login", page)
        self.assertEqual(self.app.test_client().get("/admin/audit").status_code, 302)

    def test_unknown_student_returns_not_found(self):
        client = self.app.test_client()
        self.sign_in("lecturer@radar.example", client=client)
        self.assertEqual(client.get("/students/999999").status_code, 404)

    def test_every_page_renders_for_each_permitted_role(self):
        conn = self.db()
        student_id = conn.execute("SELECT id FROM students ORDER BY id LIMIT 1").fetchone()[0]
        scored_id = conn.execute("SELECT student_id FROM predictions LIMIT 1").fetchone()[0]
        user_id = conn.execute("SELECT id FROM users WHERE role = 'lecturer'").fetchone()[0]
        conn.close()
        common = ["/", "/reports", "/reports/export?level=summary"]
        staff = common + ["/students", f"/students/{student_id}", "/predictions", "/predictions?risk=High",
                          f"/predictions/{scored_id}/explanation", "/interventions", "/reports/export?level=students"]
        admin = staff + ["/students/import", "/students/import/template", f"/students/{student_id}/edit",
                         "/admin/users", "/admin/users/new", f"/admin/users/{user_id}", "/admin/audit",
                         "/admin/model", "/change-password"]
        cases = {
            "admin@radar.example": admin,
            "lecturer@radar.example": staff,
            "advisor@radar.example": staff,
            "faculty@radar.example": common,
        }
        for email, urls in cases.items():
            client = self.app.test_client()
            self.assertEqual(self.sign_in(email, client=client).status_code, 302, email)
            for url in urls:
                with self.subTest(email=email, url=url):
                    self.assertEqual(client.get(url).status_code, 200)


if __name__ == "__main__":
    unittest.main()
