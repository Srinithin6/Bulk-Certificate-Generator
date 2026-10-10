"""Retention: expired files are deleted, history is kept."""

import os
import time
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.database import SessionLocal
from app.models import Certificate, Job, utcnow
from app.services.cleanup import purge_expired, purge_stale_tmp_files
from tests.conftest import create_job, list_certs


def _expire(job_id):
    with SessionLocal() as db:
        db.get(Job, job_id).expires_at = utcnow() - timedelta(minutes=1)
        db.commit()


def test_expiry_is_set_from_retention_days(client, monkeypatch):
    monkeypatch.setattr(config, "RETENTION_DAYS", 3)
    job = create_job(client, "A")
    with SessionLocal() as db:
        stored = db.get(Job, job["id"])
        assert abs((stored.expires_at - stored.finished_at) - timedelta(days=3)) < timedelta(seconds=1)


def test_expired_job_files_are_purged_but_history_is_kept(client):
    job = create_job(client, "A One", "B Two")
    cert = list_certs(client, job["id"])[0]
    folder = config.CERT_DIR / job["id"]
    assert folder.exists()

    _expire(job["id"])
    assert purge_expired() == 1

    assert not folder.exists()
    with SessionLocal() as db:
        assert db.get(Job, job["id"]).purged_at is not None
        assert all(c.storage_key is None for c in db.scalars(select(Certificate)))
    status = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert status["files_available"] is False and status["completed"] == 2  # tallies survive
    assert list_certs(client, job["id"])[0]["download_url"] is None
    for path in (f"/api/v1/certificates/{cert['id']}/download", f"/api/v1/jobs/{job['id']}/download"):
        r = client.get(path)
        assert r.status_code == 410 and r.json()["error"]["code"] == "gone"
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 410
    assert purge_expired() == 0  # idempotent


def test_unexpired_and_unfinished_jobs_are_not_purged(client):
    fresh = create_job(client, "A")
    unfinished = create_job(client, "B")
    with SessionLocal() as db:  # pretend the second job is still running but its date has passed
        j = db.get(Job, unfinished["id"])
        j.status, j.expires_at = "PROCESSING", utcnow() - timedelta(days=1)
        db.commit()
    assert purge_expired() == 0
    assert (config.CERT_DIR / fresh["id"]).exists() and (config.CERT_DIR / unfinished["id"]).exists()


def test_stale_temporary_zips_are_removed(client):
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    old, new = config.TMP_DIR / "old.zip", config.TMP_DIR / "new.zip"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert purge_stale_tmp_files() == 1
    assert not old.exists() and new.exists()
