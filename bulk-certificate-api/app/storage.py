"""File storage for generated PDFs.

The database stores only a *storage key* (``<job_id>/<certificate_id>.pdf``), never the
file. This class is the single place that turns a key into a path on disk, and it refuses
anything that would escape the storage folder (path traversal). To move to S3, implement
the same four methods with boto3 and nothing else in the app changes.
"""

import re
import shutil
from pathlib import Path

from app import config

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class LocalStorage:
    def key_for(self, job_id: str, certificate_id: str) -> str:
        return f"{job_id}/{certificate_id}.pdf"

    def resolve(self, key: str) -> Path:
        """Absolute path for ``key``; raises ValueError if it would leave CERT_DIR."""
        root = config.CERT_DIR.resolve()
        path = (root / key).resolve()
        if root not in path.parents:
            raise ValueError("unsafe storage key")
        return path

    def exists(self, key: str) -> bool:
        try:
            return self.resolve(key).is_file()
        except ValueError:
            return False

    def delete_job(self, job_id: str) -> None:
        """Remove every file of a job. ``job_id`` must be a plain hex id."""
        if not _ID_RE.match(job_id):
            raise ValueError("invalid job id")
        shutil.rmtree(self.resolve(job_id), ignore_errors=True)


storage = LocalStorage()
