"""Retention: delete generated files of expired jobs, and stale temporary ZIP files.

Rows are kept (so the job history and its tallies stay visible); only the files go away and
``jobs.purged_at`` is set. Runs periodically inside the API process and can also be run by
hand / from cron:  ``python -m app.services.cleanup``
"""

import logging
import threading
import time
from datetime import datetime

from sqlalchemy import select, update

from app import config
from app.database import SessionLocal
from app.models import Certificate, Job, JobStatus, utcnow
from app.storage import storage

logger = logging.getLogger(__name__)
STALE_TMP_SECONDS = 3600


def purge_expired(now: datetime | None = None) -> int:
    """Delete the files of finished jobs whose retention has passed. Returns jobs purged."""
    now = now or utcnow()
    with SessionLocal() as db:
        jobs = db.scalars(
            select(Job).where(Job.expires_at <= now, Job.purged_at.is_(None), Job.status.in_(JobStatus.FINAL))
        ).all()
        for job in jobs:
            storage.delete_job(job.id)
            db.execute(update(Certificate).where(Certificate.job_id == job.id).values(storage_key=None))
            job.purged_at = now
            logger.info("Purged expired job files", extra={"job_id": job.id})
        db.commit()
        count = len(jobs)
    purge_stale_tmp_files()
    return count


def purge_stale_tmp_files() -> int:
    """Remove temporary ZIPs left behind by a crash (normal ones are deleted after download)."""
    if not config.TMP_DIR.exists():
        return 0
    removed = 0
    cutoff = time.time() - STALE_TMP_SECONDS
    for path in config.TMP_DIR.glob("*.zip"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


class CleanupLoop:
    """Calls ``purge_expired`` every CLEANUP_INTERVAL_MINUTES in a daemon thread."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if config.CLEANUP_INTERVAL_MINUTES <= 0:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cleanup", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = config.CLEANUP_INTERVAL_MINUTES * 60
        while not self._stop.wait(interval):
            try:
                purge_expired()
            except Exception:
                logger.exception("Cleanup run failed")

    def stop(self) -> None:
        self._stop.set()


if __name__ == "__main__":
    from app.logging_config import setup_logging

    setup_logging()
    print(f"Purged {purge_expired()} expired job(s)")
