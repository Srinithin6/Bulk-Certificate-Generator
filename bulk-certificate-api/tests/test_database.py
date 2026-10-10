"""Schema guarantees: constraints, indexes, cascade."""

import pytest
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, engine
from app.models import Certificate, Job
from tests.conftest import create_job


def _job(**kw):
    return Job(title="T", issued_by="O", issue_date=__import__("datetime").date(2026, 1, 1), total=1, **kw)


def test_idempotency_key_is_unique():
    with SessionLocal() as db:
        db.add_all([_job(idempotency_key="k1"), _job(idempotency_key="k1")])
        with pytest.raises(IntegrityError):
            db.commit()


def test_job_without_key_can_repeat():
    with SessionLocal() as db:
        db.add_all([_job(), _job()])
        db.commit()
        assert db.scalar(select(func.count()).select_from(Job)) == 2


def test_certificate_position_and_number_are_unique_per_job(client):
    with SessionLocal() as db:
        job = _job()
        db.add(job)
        db.flush()
        db.add(Certificate(job_id=job.id, seq=0, payload={}, certificate_number="N-1"))
        db.commit()
        db.add(Certificate(job_id=job.id, seq=0, payload={}))  # same (job, seq)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Certificate(job_id=job.id, seq=1, payload={}, certificate_number="N-1"))  # same number
        with pytest.raises(IntegrityError):
            db.commit()


def test_useful_indexes_exist(client):
    insp = inspect(engine)
    cert_indexes = {tuple(i["column_names"]) for i in insp.get_indexes("certificates")}
    assert ("job_id", "status") in cert_indexes and ("job_id",) in cert_indexes
    assert ("status",) in {tuple(i["column_names"]) for i in insp.get_indexes("jobs")}
    assert ("expires_at",) in {tuple(i["column_names"]) for i in insp.get_indexes("jobs")}


def test_deleting_a_job_removes_its_certificates(client):
    job = create_job(client, "A", "B")
    with SessionLocal() as db:
        db.execute(delete(Job).where(Job.id == job["id"]))
        db.commit()
        assert db.scalar(select(func.count()).select_from(Certificate)) == 0


def test_sessions_are_returned_to_the_pool(client):
    for _ in range(30):  # would exhaust a leaking pool (default 5 + 10 overflow)
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/jobs/nope").status_code == 404
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
