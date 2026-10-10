"""Background processing of jobs.

Model: a bounded thread pool; one task = one job; the certificates of a job are generated one
by one, each in its own short transaction. That gives
  * live progress (every certificate is committed as soon as it finishes),
  * failure isolation (an exception is caught per certificate),
  * crash safety (all state lives in the database; see ``recover_unfinished_jobs``),
  * no duplicates (a certificate is claimed with an atomic UPDATE before it is rendered).
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select, update

from app import config
from app.database import SessionLocal
from app.errors import GenerationError
from app.models import Certificate, CertStatus, Job, JobStatus, utcnow
from app.services.jobs import finalize_job
from app.services.pdf import generate_certificate_pdf
from app.storage import storage

logger = logging.getLogger(__name__)
_executor: ThreadPoolExecutor | None = None


def dispatch(job_id: str) -> None:
    """Hand a job to the worker pool (or run it inline when INLINE_PROCESSING is on)."""
    global _executor
    if config.INLINE_PROCESSING:
        process_job(job_id)
        return
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=config.WORKERS, thread_name_prefix="certgen")
    _executor.submit(process_job, job_id)


def shutdown() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


def process_job(job_id: str) -> None:
    try:
        _process_job(job_id)
    except Exception:  # never let a worker thread die silently
        logger.exception("Unexpected error while processing job", extra={"job_id": job_id})


def _process_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status in JobStatus.FINAL:
            return
        job.status = JobStatus.PROCESSING
        db.commit()
        ids = db.scalars(
            select(Certificate.id)
            .where(Certificate.job_id == job_id, Certificate.status == CertStatus.PENDING)
            .order_by(Certificate.seq)
        ).all()
        template: dict[str, Any] = dict(
            title=job.title,
            issued_by=job.issued_by,
            issue_date=job.issue_date,
            template=job.template,
            signatory_name=job.signatory_name,
            signatory_title=job.signatory_title,
        )
    logger.info("Job started", extra={"job_id": job_id, "certificates": len(ids)})

    for cert_id in ids:
        _process_certificate(cert_id, template)

    with SessionLocal() as db:
        finalize_job(db, job_id)
        final = db.get(Job, job_id)
        if final is not None:
            logger.info("Job finished", extra={"job_id": job_id, "status": final.status})


def _process_certificate(cert_id: str, template: dict[str, Any]) -> None:
    with SessionLocal() as db:
        # Atomic claim: if two workers ever see the same certificate, only one wins.
        claimed = db.execute(
            update(Certificate)
            .where(Certificate.id == cert_id, Certificate.status == CertStatus.PENDING)
            .values(status=CertStatus.PROCESSING, attempts=Certificate.attempts + 1)
        ).rowcount  # type: ignore[attr-defined]
        db.commit()
        if not claimed:
            return

        cert = db.get(Certificate, cert_id)
        if cert is None or cert.recipient_name is None or cert.certificate_number is None:
            return
        key = storage.key_for(cert.job_id, cert.id)  # same key on every attempt -> a retry overwrites
        try:
            generate_certificate_pdf(
                storage.resolve(key),
                recipient_name=cert.recipient_name,
                certificate_number=cert.certificate_number,
                remarks=cert.remarks,
                **template,
            )
            cert.status, cert.storage_key, cert.error = CertStatus.COMPLETED, key, None
        except GenerationError as exc:  # expected problem with a safe message
            logger.warning("Certificate failed: %s", exc, extra={"job_id": cert.job_id, "certificate_id": cert.id})
            cert.status, cert.error = CertStatus.FAILED, str(exc)[:500]
        except Exception:  # unexpected: log everything, show the client nothing internal
            logger.exception(
                "Unexpected error generating certificate",
                extra={"job_id": cert.job_id, "certificate_id": cert.id},
            )
            cert.status, cert.error = CertStatus.FAILED, "Unexpected error while generating the certificate"
        cert.completed_at = utcnow()
        db.commit()


def recover_unfinished_jobs() -> int:
    """Call once at startup. Jobs interrupted by a crash/restart are resumed: certificates stuck
    in PROCESSING go back to PENDING (finished ones are never regenerated). A certificate that
    has already used up all its attempts is failed instead, so a certificate that crashes the
    process cannot cause an endless restart loop."""
    with SessionLocal() as db:
        job_ids = list(db.scalars(select(Job.id).where(Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))))
        for job_id in job_ids:
            stuck = Certificate.job_id == job_id, Certificate.status == CertStatus.PROCESSING
            db.execute(
                update(Certificate)
                .where(*stuck, Certificate.attempts >= config.MAX_ATTEMPTS)
                .values(status=CertStatus.FAILED, error="Generation was interrupted too many times")
            )
            db.execute(update(Certificate).where(*stuck).values(status=CertStatus.PENDING))
        db.commit()
    for job_id in job_ids:
        logger.info("Resuming interrupted job", extra={"job_id": job_id})
        dispatch(job_id)
    return len(job_ids)
