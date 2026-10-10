"""Job creation, validation, idempotency, retry and status reporting."""

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.errors import ConflictError, GoneError, NotFoundError
from app.models import Certificate, CertStatus, Job, JobStatus, utcnow
from app.schemas import JobCreate, RecipientIn


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'recipient'}: {e['msg']}" for e in exc.errors())[:500]


def request_hash(payload: JobCreate) -> str:
    """Stable fingerprint of a request body, used to detect a reused Idempotency-Key."""
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def get_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")
    return job


def _validate_recipients(job: Job, recipients: list[Any]) -> list[Certificate]:
    """One Certificate row per recipient; bad recipients become INVALID with the reason."""
    seen: dict[tuple[str, str], int] = {}
    certs: list[Certificate] = []
    for seq, raw in enumerate(recipients):
        cert = Certificate(job_id=job.id, seq=seq, payload=raw)
        try:
            if not isinstance(raw, dict):
                raise ValueError("recipient must be a JSON object")
            r = RecipientIn.model_validate(raw)
        except ValidationError as exc:
            cert.status, cert.error = CertStatus.INVALID, _format_errors(exc)
        except ValueError as exc:
            cert.status, cert.error = CertStatus.INVALID, str(exc)
        else:
            cert.recipient_name, cert.email, cert.remarks = r.name, r.email, r.remarks
            key = (r.name.lower(), (r.email or "").lower())
            if key in seen:
                cert.status = CertStatus.INVALID
                cert.error = f"duplicate of recipient #{seen[key]}"
            else:
                seen[key] = seq
                cert.status = CertStatus.PENDING
                cert.certificate_number = f"CERT-{job.id[:8].upper()}-{seq + 1:05d}"
        certs.append(cert)
    return certs


def create_job(db: Session, payload: JobCreate, idempotency_key: str | None = None) -> tuple[Job, bool]:
    """Validate every recipient and persist the job + one certificate row per recipient.

    Returns ``(job, created)``. With an ``idempotency_key`` that was already used with the
    same body, the existing job is returned and ``created`` is False (no duplicate work).
    """
    fingerprint = request_hash(payload)
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return _replay(existing, fingerprint)

    job = Job(
        title=payload.title,
        issued_by=payload.issued_by or config.DEFAULT_ISSUER,
        issue_date=payload.issue_date or date.today(),
        template=payload.template,
        signatory_name=payload.signatory_name or config.DEFAULT_SIGNATORY_NAME,
        signatory_title=payload.signatory_title or config.DEFAULT_SIGNATORY_TITLE,
        total=len(payload.recipients),
        status=JobStatus.PENDING,
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
    )
    db.add(job)
    db.flush()  # assigns job.id
    certs = _validate_recipients(job, payload.recipients)
    db.add_all(certs)

    if not any(c.status == CertStatus.PENDING for c in certs):  # nothing to generate
        db.flush()
        finalize_job(db, job.id, commit=False)
    try:
        db.commit()
    except IntegrityError:
        # Two identical requests raced on the unique idempotency key: the other one won.
        db.rollback()
        if idempotency_key:
            existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if existing is not None:
                return _replay(existing, fingerprint)
        raise
    return job, True


def _replay(existing: Job, fingerprint: str) -> tuple[Job, bool]:
    if existing.request_hash != fingerprint:
        raise ConflictError("This Idempotency-Key was already used with a different request body")
    return existing, False


def count_by_status(db: Session, job_id: str) -> dict[str, int]:
    rows = db.execute(
        select(Certificate.status, func.count()).where(Certificate.job_id == job_id).group_by(Certificate.status)
    ).all()
    return {status: n for status, n in rows}


def finalize_job(db: Session, job_id: str, commit: bool = True) -> None:
    """If no certificate is left to process, set the final job status, store the final
    tallies and start the retention clock. Safe to call repeatedly."""
    job = db.get(Job, job_id)
    if job is None:
        return
    counts = count_by_status(db, job_id)
    if counts.get(CertStatus.PENDING, 0) or counts.get(CertStatus.PROCESSING, 0):
        return  # another worker is still on it
    ok = counts.get(CertStatus.COMPLETED, 0)
    job.succeeded_count = ok
    job.failed_count = counts.get(CertStatus.FAILED, 0)
    job.invalid_count = counts.get(CertStatus.INVALID, 0)
    if ok == job.total:
        job.status = JobStatus.COMPLETED
    elif ok == 0:
        job.status = JobStatus.FAILED
    else:
        job.status = JobStatus.COMPLETED_WITH_ERRORS
    job.finished_at = utcnow()
    job.expires_at = job.finished_at + timedelta(days=config.RETENTION_DAYS)
    if commit:
        db.commit()


def job_status(db: Session, job: Job) -> dict[str, Any]:
    """Status payload. Finished jobs use the stored tallies; running jobs are counted live."""
    if job.status in JobStatus.FINAL:
        completed, failed, invalid, pending = job.succeeded_count, job.failed_count, job.invalid_count, 0
    else:
        counts = count_by_status(db, job.id)
        completed = counts.get(CertStatus.COMPLETED, 0)
        failed = counts.get(CertStatus.FAILED, 0)
        invalid = counts.get(CertStatus.INVALID, 0)
        pending = counts.get(CertStatus.PENDING, 0) + counts.get(CertStatus.PROCESSING, 0)
    done = completed + failed + invalid
    return {
        "id": job.id,
        "title": job.title,
        "issued_by": job.issued_by,
        "issue_date": job.issue_date,
        "template": job.template,
        "signatory_name": job.signatory_name,
        "signatory_title": job.signatory_title,
        "status": job.status,
        "total": job.total,
        "completed": completed,
        "failed": failed,
        "invalid": invalid,
        "pending": pending,
        "progress": round(100 * done / job.total, 1) if job.total else 100.0,
        "files_available": job.purged_at is None and completed > 0,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "expires_at": job.expires_at,
    }


def requeue_failed(db: Session, job_id: str) -> int:
    """Put FAILED certificates (that still have attempts left) back in the queue.

    Completed certificates are never touched, so a retry cannot create duplicates.
    Returns the number re-queued; the caller dispatches the job."""
    job = get_job(db, job_id)
    if job.status not in JobStatus.FINAL:
        raise ConflictError("Job is still running; retry after it has finished")
    if job.purged_at is not None:
        raise GoneError("Job files have expired and were removed")
    result = db.execute(
        update(Certificate)
        .where(
            Certificate.job_id == job_id,
            Certificate.status == CertStatus.FAILED,
            Certificate.attempts < config.MAX_ATTEMPTS,
        )
        .values(status=CertStatus.PENDING)
    )
    requeued: int = result.rowcount  # type: ignore[attr-defined]
    if requeued == 0:
        raise ConflictError(
            "No failed certificates can be retried "
            f"(none failed, or the {config.MAX_ATTEMPTS}-attempt limit was reached)"
        )
    job.status = JobStatus.PENDING
    job.finished_at = None
    job.expires_at = None
    db.commit()
    return requeued
