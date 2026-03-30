"""Zeno API entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import app.config as app_config
from app.cas.router import router as cas_router
from app.db import close_pool, get_pool
from app.health import run_health_checks
from app.redis_conn import close_redis
from app.workflow.locks_router import router as locks_router
from app.workflow.presence_router import router as presence_router
from app.versions.router import router as versions_router
from app.projects.router import router as projects_router
from app.episodes.router import router as episodes_router
from app.sequences.router import router as sequences_router
from app.assets.router import router as assets_router
from app.shots.router import router as shots_router
from app.tasks.router import router as tasks_router
from app.resolver.router import router as resolver_router
from app.settings.router import router as settings_router
from app.manifests.store import ensure_manifest_indexes
from app.settings.store import ensure_settings_indexes


def _validate_runtime_config() -> None:
    """
    Fail fast in production-like environments when CAS is not MinIO/S3-backed.
    """
    env = app_config.APP_ENV
    if env in ("production", "staging"):
        if app_config.CAS_STORAGE_BACKEND != "s3":
            raise RuntimeError("CAS_STORAGE_BACKEND must be 's3' in production/staging")
        if not (app_config.S3_ENDPOINT_URL and app_config.S3_ACCESS_KEY and app_config.S3_SECRET_KEY):
            raise RuntimeError("S3_ENDPOINT_URL, S3_ACCESS_KEY, and S3_SECRET_KEY are required in production/staging")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_runtime_config()
    if app_config.MONGO_URI:
        try:
            ensure_settings_indexes()
            ensure_manifest_indexes()
        except Exception:
            pass
    if app_config.DATABASE_URL:
        await get_pool()
    yield
    await close_pool()
    await close_redis()


app = FastAPI(title="Zeno API", version="0.1.0", lifespan=lifespan)

app.include_router(cas_router)
app.include_router(projects_router)
app.include_router(episodes_router)
app.include_router(sequences_router)
app.include_router(assets_router)
app.include_router(shots_router)
app.include_router(tasks_router)
app.include_router(resolver_router)
app.include_router(settings_router)
app.include_router(versions_router)
app.include_router(presence_router)
app.include_router(locks_router)


@app.get("/")
def root() -> dict:
    """Root endpoint."""
    return {"service": "zeno-api", "version": "0.1.0"}


@app.get("/health")
async def health() -> dict:
    """Health checks for Postgres, Redis, Mongo, MinIO (skipped if env not set)."""
    return await run_health_checks()
