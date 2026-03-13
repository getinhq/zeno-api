"""Zeno API entrypoint."""
from fastapi import FastAPI

from app.health import run_health_checks

app = FastAPI(title="Zeno API", version="0.1.0")


@app.get("/")
def root() -> dict:
    """Root endpoint."""
    return {"service": "zeno-api", "version": "0.1.0"}


@app.get("/health")
async def health() -> dict:
    """Health checks for Postgres, Redis, Mongo, MinIO (skipped if env not set)."""
    return await run_health_checks()
