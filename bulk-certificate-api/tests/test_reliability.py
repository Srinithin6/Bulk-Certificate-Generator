"""Interrupted jobs, restarts, concurrent workers."""

import threading

from sqlalchemy import select

from app import config
from app.database import SessionLocal
from app.models import Certificate, CertStatus, Job, JobStatus
from app.services import processor
from tests.conftest import create_job, list_certs


def _certs(db, job_id):
    return db.scalars(select(Certificate).where(Certificate.job_id == job_id).order_by(Certificate.seq)).all()


def test_interrupted_job_resumes_without_regenerating_finished_certs(client):
    job_id = create_job(client, "A One", "B Two", "C Three")["id"]
    # Simulate a crash mid-job: job still PROCESSING, one cert stuck PROCESSING, one never started
    with SessionLocal() as db:
        certs = _certs(db, job_id)
        first_key = certs[0].storage_key
        mtime = (config.CERT_DIR / first_key).stat().st_mtime_ns
        certs[1].status, certs[1].storage_key = CertStatus.PROCESSING, None
        certs[2].status, certs[2].storage_key = CertStatus.PENDING, None
        job = db.get(Job, job_id)
        job.status, job.finished_at, job.expires_at = JobStatus.PROCESSING, None, None
        db.commit()

    assert processor.recover_unfinished_jobs() == 1  # inline mode -> finishes immediately

    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["status"] == "COMPLETED" and status["completed"] == 3
    assert (config.CERT_DIR / first_key).stat().st_mtime_ns == mtime  # untouched


def test_certificate_that_keeps_crashing_is_failed_not_retried_forever(client):
    job_id = create_job(client, "A One", "Poison")["id"]
    with SessionLocal() as db:
        poison = _certs(db, job_id)[1]
        poison.status, poison.attempts, poison.storage_key = CertStatus.PROCESSING, config.MAX_ATTEMPTS, None
        job = db.get(Job, job_id)
        job.status, job.finished_at, job.expires_at = JobStatus.PROCESSING, None, None
        db.commit()

    processor.recover_unfinished_jobs()

    certs = list_certs(client, job_id)
    assert certs[1]["status"] == "FAILED" and "interrupted too many times" in certs[1]["error"]
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "COMPLETED_WITH_ERRORS"


def test_recovery_ignores_finished_jobs(client):
    create_job(client, "A")
    assert processor.recover_unfinished_jobs() == 0


def test_two_workers_never_generate_the_same_certificate(client, monkeypatch):
    job_id = create_job(client, "Solo")["id"]
    with SessionLocal() as db:  # put the certificate back in the queue
        cert = _certs(db, job_id)[0]
        cert.status = CertStatus.PENDING
        db.commit()
        cert_id = cert.id

    calls, lock = [], threading.Lock()
    real = processor.generate_certificate_pdf

    def counting(dest, **kw):
        with lock:
            calls.append(kw["recipient_name"])
        return real(dest, **kw)

    monkeypatch.setattr(processor, "generate_certificate_pdf", counting)
    template = dict(
        title="T",
        issued_by="O",
        issue_date=__import__("datetime").date(2026, 1, 1),
        template="classic",
        signatory_name=None,
        signatory_title=None,
    )
    threads = [threading.Thread(target=processor._process_certificate, args=(cert_id, template)) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert calls == ["Solo"]  # exactly one worker won the atomic claim
    with SessionLocal() as db:
        assert db.get(Certificate, cert_id).attempts == 2  # 1 original + 1 from this run


def test_worker_error_outside_a_certificate_does_not_crash_the_thread(client, monkeypatch):
    monkeypatch.setattr(processor, "finalize_job", lambda *a, **k: 1 / 0)
    job_id = create_job(client, "A")["id"]  # process_job swallows + logs the unexpected error
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
