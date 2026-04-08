from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Increase sqlite timeouts to reduce lock errors from third-party clients.
_original_sqlite3_connect = sqlite3.connect


def _patched_sqlite3_connect(*args, **kwargs):
    if "timeout" in kwargs:
        if kwargs["timeout"] < 10:
            kwargs["timeout"] = 10
    else:
        kwargs["timeout"] = 30
    return _original_sqlite3_connect(*args, **kwargs)


sqlite3.connect = _patched_sqlite3_connect

import backend.models  # noqa: E402,F401
from backend.api import router as api_router  # noqa: E402
from backend.core.config import get_settings  # noqa: E402
from backend.core.database import (  # noqa: E402
    Base,
    ensure_schema_compat,
    get_engine,
    get_session_local,
    init_engine,
)
from backend.scheduler import (  # noqa: E402
    init_scheduler,
    shutdown_scheduler,
    sync_jobs,
)
from backend.services.users import ensure_admin  # noqa: E402
from backend.utils.paths import ensure_data_dirs  # noqa: E402


class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/healthz" not in msg and "/readyz" not in msg


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

settings = get_settings()
WEB_DIR = Path("/web")
NEXT_STATIC_DIR = WEB_DIR / "_next"
INDEX_HTML = WEB_DIR / "index.html"

app = FastAPI(title=settings.app_name, version="0.1.0")
app.state.ready = False

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")

if NEXT_STATIC_DIR.exists():
    app.mount("/_next", StaticFiles(directory=NEXT_STATIC_DIR), name="nextjs_static")
else:
    logging.getLogger("backend.startup").warning(
        "Next.js static directory %s not found; frontend assets will not be served.",
        NEXT_STATIC_DIR,
    )


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
def health_checkz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def ready_check(response: Response) -> dict[str, str]:
    if app.state.ready:
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "starting"}


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    file_path = WEB_DIR / full_path
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)

    html_path = WEB_DIR / f"{full_path}.html"
    if html_path.exists() and html_path.is_file():
        return FileResponse(html_path)

    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML)

    raise HTTPException(status_code=404, detail="Frontend not built")


@app.on_event("startup")
async def on_startup() -> None:
    ensure_data_dirs(settings)
    init_engine()
    Base.metadata.create_all(bind=get_engine())
    ensure_schema_compat()
    with get_session_local()() as db:
        ensure_admin(db)
    await init_scheduler(sync_on_startup=False)

    async def _post_startup() -> None:
        try:
            await sync_jobs()
        except Exception as exc:
            logging.getLogger("backend.startup").error(
                "Delayed scheduler sync failed: %s",
                exc,
            )
        finally:
            app.state.ready = True

    asyncio.create_task(_post_startup())


@app.on_event("shutdown")
def on_shutdown() -> None:
    shutdown_scheduler()
