"""FastAPI entrypoint. Hello-world for Phase 0; routers added in Phase 3."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.db import get_session
from api.observability import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    configure_logging()
    log = get_logger("api")
    settings = get_settings()
    log.info("api.startup", env=settings.env, org_id=settings.org_id)
    yield
    log.info("api.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="Wound IQ API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await session.execute(text("SELECT 1"))
        return {"status": "ready", "db": "ok"}

    return app


app = create_app()
