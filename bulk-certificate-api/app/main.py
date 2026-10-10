import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.api import certificates, jobs
from app.database import SessionLocal, init_db
from app.errors import AppError, error_body
from app.logging_config import setup_logging
from app.schemas import HealthOut
from app.security import RequestSizeLimitMiddleware
from app.services import processor
from app.services.cleanup import CleanupLoop

setup_logging()
logger = logging.getLogger(__name__)
cleanup_loop = CleanupLoop()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    config.CERT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    processor.recover_unfinished_jobs()  # resume anything interrupted by a restart
    cleanup_loop.start()
    yield
    cleanup_loop.stop()
    processor.shutdown()


def create_app() -> FastAPI:
    """Build the application (a factory so tests can create apps with different settings)."""
    app = FastAPI(
        title="Bulk Certificate Generator",
        description=(
            "Submit a list of recipients, track progress, and download PDF certificates.\n\n"
            "Authentication: if the server has `API_KEY` set, send it in the `X-API-Key` header "
            "(use the Authorize button)."
        ),
        version="2.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestSizeLimitMiddleware)
    if config.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.CORS_ORIGINS,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key", "Idempotency-Key"],
        )
    app.include_router(jobs.router)
    app.include_router(certificates.router)

    # ----------------------------------------------------------------- consistent error responses
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            error_body(codes.get(exc.status_code, "http_error"), str(exc.detail)), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details: list[dict[str, Any]] = [
            {"field": ".".join(str(p) for p in e["loc"][1:]) or str(e["loc"][0]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(error_body("validation_error", "Invalid request", details), status_code=422)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Full details go to the log only; the client never sees stack traces or internal paths.
        logger.error("Unhandled error", exc_info=exc, extra={"path": request.url.path})
        return JSONResponse(error_body("internal_error", "Internal server error"), status_code=500)

    @app.get(
        "/health",
        response_model=HealthOut,
        tags=["meta"],
        summary="Liveness + database check",
        responses={503: {"model": HealthOut, "description": "Database unavailable"}},
    )
    def health() -> Any:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Health check: database unavailable")
            return JSONResponse({"status": "degraded", "database": "unavailable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    return app


app = create_app()
