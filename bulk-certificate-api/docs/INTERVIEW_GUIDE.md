# Interview guide

Questions below are based on the code in this repository, with short model answers in simple words.
Read the code named in each answer once so you can explain it in your own words.

## Git and GitHub

```powershell
cd bulk-certificate-api
git init
git add .
git commit -m "Bulk certificate generator API"
git branch -M main
# create an EMPTY public repo on github.com first (no README), then:
git remote add origin https://github.com/<your-username>/bulk-certificate-api.git
git push -u origin main
```
Tip: make several small commits instead of one (for example: "core API", "retry and idempotency", "retention", "Docker and CI") so the history shows how you built it.
Check `git status` shows no `.env` file before pushing. After pushing, open the **Actions** tab: the CI workflow runs automatically.
(The CI and Docker files were not executed while this project was prepared, so if a step fails, read its log and fix it. This is normal.)

## Resume description (honest version)

> **Bulk Certificate Generator API** (Python, FastAPI, SQLAlchemy, PostgreSQL, Docker)
> Built a REST backend that generates PDF certificates in bulk from a single request. Recipients are validated individually, jobs run in a background worker pool with live progress tracking, per-certificate failure isolation, manual retry, idempotent submission and automatic recovery after restarts. Files are downloadable individually or as ZIPs and expire after a retention period. Includes API-key auth, request limits, path-traversal protection, structured logging, 76 automated tests (SQLite and PostgreSQL), a Docker Compose setup and a GitHub Actions CI pipeline.

Do not claim: "production-ready", "scales horizontally", or experience with Celery/Redis/S3: the project does not use them.

## Questions and model answers

**1. Walk me through what happens when a client POSTs 1000 recipients.**
The route checks the batch size, then `create_job` validates each recipient with Pydantic and saves one `jobs` row and 1000 `certificates` rows (`PENDING` or `INVALID`). The route calls `dispatch(job_id)`, which hands the job to a thread pool, and returns `202` with the job id straight away. A worker then generates the PDFs one by one while the client polls the status endpoint.

**2. Why background processing instead of doing it inside the request?**
1000 PDFs take several seconds. A long request risks timeouts and ties up a server connection. Returning a job id immediately and letting the client poll is the standard pattern for long work.

**3. Why a thread pool and not Celery + Redis?**
For this size (milliseconds per PDF, one server) a thread pool is simpler: no extra services to run. Reliability does not depend on it, because the database holds all state and the job can be resumed. I would move to Celery when I need several worker machines, scheduling or priorities. Only `dispatch()` in `processor.py` would change.

**4. What happens if the server crashes in the middle of a job?**
At startup, `recover_unfinished_jobs` finds jobs that are `PENDING`/`PROCESSING`, resets certificates stuck in `PROCESSING` to `PENDING`, and dispatches them again. Finished certificates stay `COMPLETED` and are not regenerated. I tested this by killing the process with `kill -9` at 231 of 1500 certificates: after restart all 1500 existed, no duplicates.

**5. How do you stop two workers generating the same certificate?**
Before rendering, a worker runs `UPDATE certificates SET status='PROCESSING' WHERE id=? AND status='PENDING'` and checks that exactly one row changed. The database makes this atomic, so only one worker "wins". There is a test with 6 threads racing on one certificate: the renderer runs once.

**6. How does one failure not stop the others?**
Each certificate is processed in its own `try/except` and its own short transaction. A failure marks only that row `FAILED` with a safe message, and the loop continues. At the end the job is `COMPLETED_WITH_ERRORS`.

**7. What is the difference between `INVALID` and `FAILED`?**
`INVALID`: the input was wrong (empty name, bad email, duplicate). We never tried, and the client must fix the data. `FAILED`: the input was valid but generation failed (for example unsupported characters or an unexpected error). It can be retried.

**8. How does retry avoid duplicates?**
`POST /jobs/{id}/retry` only changes certificates with status `FAILED` (and `attempts < MAX_ATTEMPTS`) back to `PENDING`. Completed ones are never touched. The file path comes from the certificate id, so a retry overwrites the same file instead of making a new one. `attempts` stops infinite retrying.

**9. What is idempotency and how did you implement it?**
If a client's request times out and it sends the same request again, we should not create a second job. The client sends an `Idempotency-Key` header. I store the key (unique column) and a hash of the body. Same key + same body returns the existing job; same key + different body is `409`. If two identical requests arrive at the same moment, the unique constraint lets only one insert succeed and the other returns the existing job.

**10. What does `finalize_job` do and why store counts on the job?**
When no certificate is `PENDING`/`PROCESSING`, it decides the final status, saves the final counts and sets `expires_at`. Finished jobs then answer status requests from the stored numbers without counting rows; running jobs are counted live with `GROUP BY status`, so progress is always accurate.

**11. How do you protect against path traversal?**
File paths are never taken from the client. The database holds a server-generated `storage_key`, and `LocalStorage.resolve` joins it to the storage folder, resolves it, and refuses anything outside the folder. ZIP entry names are built from the sequence number and a sanitized name. There are tests with `../` keys, and with a tampered key in the database.

**12. How do you avoid leaking internal details in errors?**
Expected problems raise our own errors with safe messages. Unexpected exceptions are logged on the server (with the job and certificate id) and the client sees only "Unexpected error while generating the certificate" or `500 Internal server error`. One handler produces the same JSON shape for every error.

**13. How are files cleaned up?**
When a job finishes, `expires_at = finished_at + RETENTION_DAYS`. A background loop (and `python -m app.services.cleanup`) deletes the files of expired jobs and sets `purged_at`. The rows stay, so history and counts remain visible. Downloads of purged files return `410 Gone`. Temporary ZIPs are deleted right after the download, and stale ones are removed by the cleanup.

**14. Why don't you store the PDFs in the database?**
They are large and make the database slow and big. Databases are good for metadata and queries; files belong on disk or object storage (S3). The database keeps only the `storage_key`.

**15. How do you support both SQLite and PostgreSQL?**
SQLAlchemy abstracts the database; `DATABASE_URL` chooses it. SQLite is for easy local use (with WAL mode so readers do not block the writer); PostgreSQL is for Docker/production. I run the whole test suite on both.

**16. What are the limitations of your design?**
Workers run inside the API process, so I run one replica; no migrations (Alembic is the next step); one shared API key and no rate limiting; certificates stored unencrypted for the retention period; Latin-1 fonts only unless a font file is configured. They are all listed in the README.

**17. What would you do next?**
Alembic migrations, S3 storage with presigned URLs, Celery/Redis workers if the load needs multiple machines, per-client keys and rate limits, a webhook when a job finishes.

**18. A bug I found while building (good story).**
My first request-size middleware raised an exception when a chunked body was too big, but FastAPI turned it into a generic `400`. A test caught it. I changed the middleware to send the `413` itself and tell the app the client disconnected.

## Live-coding warm-ups (practise these)

- Change the batch limit to 100 (`MAX_RECIPIENTS`), and show the `413` response.
- Add a `phone` field to `RecipientIn` and store it (model + schema + migration thought).
- Add a third template ("minimal"): add an entry to `THEMES` in `pdf.py` and to `TemplateName` in `schemas.py`.
- Make duplicate emails (not just name+email) invalid: edit the `key` in `_validate_recipients`.
- Add `GET /api/v1/jobs` (list jobs with pagination): copy the pattern of `list_certificates`.
