import os
import shutil
import tempfile

# Must run before any `app` import: point the app at throw-away storage.
_TMP = tempfile.mkdtemp(prefix="certtests-")
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ["DATA_DIR"] = _TMP
os.environ["CERT_DIR"] = f"{_TMP}/certs"
os.environ["TMP_DIR"] = f"{_TMP}/tmp"
os.environ["INLINE_PROCESSING"] = "true"  # deterministic: jobs finish inside the request
os.environ["CLEANUP_INTERVAL_MINUTES"] = "0"  # no background cleanup thread in tests
os.environ["LOG_LEVEL"] = "WARNING"
for _var in ("API_KEY", "CORS_ORIGINS"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.database import Base, engine  # noqa: E402


@pytest.fixture()
def client():
    from app import models  # noqa: F401
    from app.main import app
    from app.services import processor

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    shutil.rmtree(config.CERT_DIR, ignore_errors=True)
    shutil.rmtree(config.TMP_DIR, ignore_errors=True)
    with TestClient(app) as c:
        yield c
    processor.shutdown()


def make_payload(*names, **overrides):
    body = {
        "title": "Python Bootcamp 2026",
        "issued_by": "Aereo Academy",
        "issue_date": "2026-10-01",
        "recipients": [{"name": n, "email": f"user{i}@example.com"} for i, n in enumerate(names)],
    }
    body.update(overrides)
    return body


def create_job(client, *names, **overrides):
    """POST a job and return the response body (asserts it was accepted)."""
    r = client.post("/api/v1/jobs", json=make_payload(*names, **overrides))
    assert r.status_code == 202, r.text
    return r.json()


def list_certs(client, job_id, **params):
    return client.get(f"/api/v1/jobs/{job_id}/certificates", params=params).json()["results"]
