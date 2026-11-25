"""RADAR Early Warning System: Flask application factory."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask, abort, render_template

from . import db
from .security import install_security
from .views import admin_views, auth_views, records_views

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    secret = os.environ.get("RADAR_SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)  # sessions reset on restart when no key is configured
        print("RADAR: RADAR_SECRET_KEY is not set; using a temporary key. Set it before real use.")
    app.config.update(
        SECRET_KEY=secret,
        DATABASE=os.environ.get("RADAR_DATABASE", str(PROJECT_ROOT / "instance" / "radar.db")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("RADAR_HTTPS") == "1",
        SESSION_TIMEOUT_MINUTES=30,
        MAX_CONTENT_LENGTH=5 * 1024 * 1024 + 64 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    install_security(app)
    app.register_blueprint(auth_views.bp)
    app.register_blueprint(records_views.bp)
    app.register_blueprint(admin_views.bp)

    @app.errorhandler(400)
    def bad_request(error):
        return render_template("error.html", code=400, message=error.description), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", code=403,
                               message="Your role does not allow this page."), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="We could not find that page."), 404

    @app.errorhandler(413)
    def too_large(_error):
        return render_template("error.html", code=413,
                               message="That file is larger than the 5 MB limit."), 413

    return app
