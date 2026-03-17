"""Zeno API entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import DATABASE_URL, MONGO_URI
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
from app.settings.store import ensure_settings_indexes


@asynccontextmanager
async def lifespan(app: FastAPI):
    if MONGO_URI:
        try:
            ensure_settings_indexes()
        except Exception:
            pass
    if DATABASE_URL:
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
