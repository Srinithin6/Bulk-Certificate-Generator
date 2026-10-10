"""Retry of failed certificates and duplicate-request protection."""

from sqlalchemy import func, select

from app import config
from app.database import SessionLocal
from app.models import Certificate, Job
from app.schemas import JobCreate
from app.services import jobs as jobs_service
from app.services import processor
from tests.conftest import create_job, list_certs, make_payload


def flaky_until(monkeypatch, name, succeed_on_attempt):
    """Make `name` fail until its Nth attempt; return a dict counting calls per recipient."""
    real = processor.generate_certificate_pdf
    calls: dict[str, int] = {}

    def fake(dest, **kw):
        who = kw["recipient_name"]
        calls[who] = calls.get(who, 0) + 1
        if who == name and calls[who] < succeed_on_attempt:
            raise RuntimeError("transient")
        return real(dest, **kw)

    monkeypatch.setattr(processor, "generate_certificate_pdf", fake)
    return calls


# ---------------------------------------------------------------- retry
def test_retry_regenerates_only_failed_certificates(client, monkeypatch):
    calls = flaky_until(monkeypatch, "Boom", succeed_on_attempt=3)
    job = create_job(client, "Alpha", "Boom", "Gamma")
    assert job["status"] == "COMPLETED_WITH_ERRORS"
    ids_before = {c["recipient_name"]: c["id"] for c in list_certs(client, job["id"])}

    r = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert r.status_code == 202 and r.json()["requeued"] == 1
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["failed"] == 1  # attempt 2 failed again
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 202  # attempt 3 succeeds

    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert final["status"] == "COMPLETED" and final["completed"] == 3 and final["failed"] == 0
    assert calls == {"Alpha": 1, "Boom": 3, "Gamma": 1}  # successful ones were never regenerated
    certs = list_certs(client, job["id"])
    assert {c["recipient_name"]: c["id"] for c in certs} == ids_before  # same records, no duplicates
    assert [c["attempts"] for c in certs] == [1, 3, 1]
    assert client.get(certs[1]["download_url"]).content.startswith(b"%PDF")


def test_retry_stops_at_max_attempts(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_ATTEMPTS", 2)
    flaky_until(monkeypatch, "Boom", succeed_on_attempt=99)
    job = create_job(client, "Boom")
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 202  # attempt 2
    r = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert r.status_code == 409 and "attempt" in r.json()["error"]["message"]


def test_retry_with_nothing_failed_is_conflict(client):
    job = create_job(client, "A", "B")
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 409


def test_retry_while_job_running_is_conflict(client):
    with SessionLocal() as db:  # a job that was accepted but not started
        job, _ = jobs_service.create_job(db, JobCreate(**make_payload("A")))
    r = client.post(f"/api/v1/jobs/{job.id}/retry")
    assert r.status_code == 409 and "still running" in r.json()["error"]["message"]


def test_retry_unknown_job_404(client):
    assert client.post("/api/v1/jobs/nope/retry").status_code == 404


# ---------------------------------------------------------------- idempotency
def test_same_idempotency_key_returns_existing_job(client):
    headers = {"Idempotency-Key": "order-1001"}
    first = client.post("/api/v1/jobs", json=make_payload("A", "B"), headers=headers)
    second = client.post("/api/v1/jobs", json=make_payload("A", "B"), headers=headers)
    assert first.status_code == 202 and second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["id"] == second.json()["id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(func.count()).select_from(Certificate)) == 2  # nothing duplicated


def test_idempotency_key_reused_with_different_body_is_conflict(client):
    headers = {"Idempotency-Key": "order-1001"}
    client.post("/api/v1/jobs", json=make_payload("A"), headers=headers)
    r = client.post("/api/v1/jobs", json=make_payload("A", "B"), headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


def test_without_key_each_request_creates_a_job(client):
    a, b = create_job(client, "A"), create_job(client, "A")
    assert a["id"] != b["id"]
