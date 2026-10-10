"""API key, CORS, path safety, consistent errors, health."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.database import SessionLocal
from app.main import create_app
from app.models import Certificate
from app.storage import storage
from tests.conftest import create_job, list_certs, make_payload


# ---------------------------------------------------------------- API key
def test_api_key_required_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "s3cret-key")
    body = make_payload("A")
    missing = client.post("/api/v1/jobs", json=body)
    wrong = client.post("/api/v1/jobs", json=body, headers={"X-API-Key": "nope"})
    ok = client.post("/api/v1/jobs", json=body, headers={"X-API-Key": "s3cret-key"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert ok.status_code == 202
    assert client.get("/health").status_code == 200  # health stays public
    assert client.get("/api/v1/certificates/x").status_code == 401  # every /api route is protected


def test_api_open_when_no_key_configured(client):
    assert client.get("/api/v1/jobs/nope").status_code == 404  # reaches the handler, no 401


# ---------------------------------------------------------------- CORS
def test_cors_only_for_configured_origins(monkeypatch):
    monkeypatch.setattr(config, "CORS_ORIGINS", ["https://app.example.com"])
    c = TestClient(create_app())
    allowed = c.get("/health", headers={"Origin": "https://app.example.com"})
    denied = c.get("/health", headers={"Origin": "https://evil.example.com"})
    assert allowed.headers.get("access-control-allow-origin") == "https://app.example.com"
    assert "access-control-allow-origin" not in denied.headers


def test_cors_disabled_by_default(client):
    r = client.get("/health", headers={"Origin": "https://app.example.com"})
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------- path safety
@pytest.mark.parametrize("key", ["../outside.pdf", "../../etc/passwd", "/etc/passwd", "a/../../b.pdf"])
def test_storage_refuses_paths_outside_the_certificate_folder(key):
    with pytest.raises(ValueError):
        storage.resolve(key)
    assert storage.exists(key) is False


def test_storage_delete_requires_plain_hex_job_id():
    with pytest.raises(ValueError):
        storage.delete_job("../../")


def test_tampered_storage_key_in_db_is_never_served(client):
    job = create_job(client, "A")
    cert_id = list_certs(client, job["id"])[0]["id"]
    with SessionLocal() as db:
        db.scalars(select(Certificate)).one().storage_key = "../../../../etc/passwd"
        db.commit()
    r = client.get(f"/api/v1/certificates/{cert_id}/download")
    assert r.status_code == 410 and "passwd" not in r.text


# ---------------------------------------------------------------- error format
def test_every_error_uses_the_same_json_shape(client):
    cases = [
        client.get("/api/v1/jobs/nope"),  # 404 AppError
        client.get("/no/such/route"),  # 404 framework
        client.put("/api/v1/jobs"),  # 405 framework
        client.post("/api/v1/jobs", json={}),  # 422
    ]
    assert [r.status_code for r in cases] == [404, 404, 405, 422]
    for r in cases:
        err = r.json()["error"]
        assert set(err) >= {"code", "message"} and isinstance(err["message"], str)


def test_unhandled_exception_returns_generic_500_without_details(client, monkeypatch):
    from app.api import jobs as jobs_api

    job = create_job(client, "A")  # create first: the POST itself uses job_status

    def explode(*a, **k):
        raise RuntimeError("db password is hunter2 at /srv/internal/path")

    monkeypatch.setattr(jobs_api.jobs_service, "job_status", explode)
    safe_client = TestClient(client.app, raise_server_exceptions=False)
    r = safe_client.get(f"/api/v1/jobs/{job['id']}")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal_error", "message": "Internal server error"}}
    assert "hunter2" not in r.text and "/srv" not in r.text


# ---------------------------------------------------------------- health
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": "ok"}


def test_health_reports_database_outage(client, monkeypatch):
    import app.main as main

    def broken():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main, "SessionLocal", broken)
    r = client.get("/health")
    assert r.status_code == 503 and r.json() == {"status": "degraded", "database": "unavailable"}
