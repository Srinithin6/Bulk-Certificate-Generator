"""Creating jobs, request validation, limits."""

import time

import pytest

from app import config
from app.models import JobStatus
from app.services import processor
from tests.conftest import create_job, make_payload


def test_create_job_returns_accepted_job(client):
    job = create_job(client, "Asha Rao", "Ben Carter", "Chen Wei")
    assert job["id"] and job["total"] == 3
    assert job["title"] == "Python Bootcamp 2026" and job["issued_by"] == "Aereo Academy"
    assert job["status"] == "COMPLETED" and job["completed"] == 3 and job["progress"] == 100.0  # inline mode
    assert job["files_available"] is True and job["expires_at"]
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["completed"] == 3


def test_defaults_applied(client, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_SIGNATORY_NAME", "Dr. Default")
    job = client.post("/api/v1/jobs", json={"title": "Workshop", "recipients": [{"name": "Asha"}]}).json()
    assert job["issued_by"] == config.DEFAULT_ISSUER and job["issue_date"]
    assert job["template"] == "classic" and job["signatory_name"] == "Dr. Default"


def test_background_mode_end_to_end(client, monkeypatch):
    """Same flow with real worker threads: POST returns at once, the client polls."""
    monkeypatch.setattr(config, "INLINE_PROCESSING", False)
    try:
        r = client.post("/api/v1/jobs", json=make_payload(*[f"Person {i}" for i in range(20)]))
        assert r.status_code == 202
        job_id = r.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in JobStatus.FINAL:
                break
            time.sleep(0.1)
        assert job["status"] == "COMPLETED" and job["completed"] == 20
    finally:
        processor.shutdown()


@pytest.mark.parametrize(
    "body",
    [
        {"recipients": [{"name": "A"}]},  # missing title
        {"title": "T"},  # missing recipients
        {"title": "   ", "recipients": [{"name": "A"}]},  # blank title
        {"title": "T\nX", "recipients": [{"name": "A"}]},  # line break in title
        {"title": "T", "recipients": "nope"},  # wrong type
        {"title": "T", "issue_date": "not-a-date", "recipients": [{"name": "A"}]},
        {"title": "T", "template": "fancy", "recipients": [{"name": "A"}]},  # unknown template
    ],
)
def test_invalid_request_is_rejected_with_details(client, body):
    r = client.post("/api/v1/jobs", json=body)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"]


def test_empty_batch_rejected(client):
    r = client.post("/api/v1/jobs", json={"title": "T", "recipients": []})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "recipients"


def test_malformed_json_rejected(client):
    r = client.post("/api/v1/jobs", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_oversized_batch_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_RECIPIENTS", 2)
    r = client.post("/api/v1/jobs", json=make_payload("A", "B", "C"))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large" and "limit is 2" in r.json()["error"]["message"]


def test_oversized_body_rejected_by_content_length(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 500)
    r = client.post("/api/v1/jobs", json=make_payload(*[f"Person Number {i}" for i in range(50)]))
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


def test_oversized_chunked_body_rejected_without_content_length(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 500)

    def chunks():  # a generator body is sent chunked: there is no Content-Length header
        yield b'{"title": "T", "recipients": ['
        for _ in range(100):
            yield b'{"name": "Someone Long Enough"},'
        yield b'{"name": "x"}]}'

    r = client.post("/api/v1/jobs", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_invalid_recipients_are_reported_not_fatal(client):
    body = make_payload("Good One")
    body["recipients"] += [
        {"email": "noname@example.com"},  # no name
        {"name": "   "},  # blank name
        {"name": "Bad Email", "email": "not-an-email"},
        {"name": "Line\nBreak"},
        {"name": "x" * 101},
        "just a string",
        {"name": "Good One", "email": "user0@example.com"},  # duplicate of #0
    ]
    job = client.post("/api/v1/jobs", json=body).json()
    assert job["total"] == 8
    assert (job["completed"], job["invalid"], job["failed"]) == (1, 7, 0)
    assert job["status"] == "COMPLETED_WITH_ERRORS" and job["progress"] == 100.0

    items = client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"status": "INVALID"}).json()
    assert items["total"] == 7
    errors = {i["index"]: i["error"] for i in items["results"]}
    assert "name" in errors[1] and "email" in errors[3]
    assert "object" in errors[6] and "duplicate" in errors[7]
    assert all(i["download_url"] is None for i in items["results"])


def test_all_invalid_job_finishes_failed_immediately(client):
    job = client.post("/api/v1/jobs", json={"title": "T", "recipients": [{}, {"name": ""}]}).json()
    assert job["status"] == "FAILED" and job["invalid"] == 2 and job["completed"] == 0
    assert job["files_available"] is False


def test_idempotency_key_format_validated(client):
    r = client.post("/api/v1/jobs", json=make_payload("A"), headers={"Idempotency-Key": "bad key!"})
    assert r.status_code == 422
