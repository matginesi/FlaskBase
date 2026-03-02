from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("FLASK_ENV", "test")
os.environ.setdefault("ENV", "test")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("REMEMBER_COOKIE_SECURE", "false")
os.environ.setdefault("RATELIMIT_STORAGE_URI", "memory://")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def database_url() -> str:
    uri = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not uri:
        pytest.skip("TEST_DATABASE_URL or DATABASE_URL is required for PostgreSQL integration tests.")
    return uri


@pytest.fixture()
def app(database_url: str):
    try:
        from app import create_app
        from app.extensions import db
        from app.services.seed_service import seed_runtime_settings, seed_users
    except ModuleNotFoundError as exc:
        pytest.skip(f"Missing test dependency: {exc.name}")

    application = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": database_url,
        }
    )

    with application.app_context():
        db.drop_all()
        db.create_all()
        seed_runtime_settings()
        seed_users()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(client):
    response = client.post(
        "/auth/login",
        data={"email": "admin@test.com", "password": "admin"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    return client


@pytest.fixture()
def user_client(app):
    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"email": "user@test.com", "password": "user"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    return client
