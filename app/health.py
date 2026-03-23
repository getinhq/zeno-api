"""Health checks for Postgres, Redis, Mongo, optional MinIO."""
import asyncio
from typing import Any

from app.config import (
    DATABASE_URL,
    MONGO_URI,
    REDIS_URL,
    S3_ACCESS_KEY,
    S3_BUCKET_CAS,
    S3_ENDPOINT_URL,
    S3_SECRET_KEY,
)


async def check_postgres() -> dict[str, Any]:
    if not DATABASE_URL:
        return {"status": "skipped", "reason": "DATABASE_URL not set"}
    try:
        import asyncpg
        conn = await asyncio.wait_for(
            asyncpg.connect(DATABASE_URL),
            timeout=3.0,
        )
        await conn.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def check_redis() -> dict[str, Any]:
    if not REDIS_URL:
        return {"status": "skipped", "reason": "REDIS_URL not set"}
    try:
        import redis.asyncio as redis
        r = redis.from_url(REDIS_URL)
        await asyncio.wait_for(r.ping(), timeout=3.0)
        await r.aclose()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def check_mongo() -> dict[str, Any]:
    if not MONGO_URI:
        return {"status": "skipped", "reason": "MONGO_URI not set"}
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        client.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def check_minio() -> dict[str, Any]:
    if not S3_ENDPOINT_URL or not (S3_ACCESS_KEY or "").strip():
        return {"status": "skipped", "reason": "S3_ENDPOINT_URL / S3_ACCESS_KEY not set"}
    try:
        import boto3
        from botocore.config import Config
        client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        client.head_bucket(Bucket=S3_BUCKET_CAS)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _norm(r: Any) -> dict[str, Any]:
    if isinstance(r, BaseException):
        return {"status": "error", "detail": str(r)}
    return r


async def run_health_checks() -> dict[str, Any]:
    results = await asyncio.gather(
        check_postgres(),
        check_redis(),
        check_mongo(),
        check_minio(),
        return_exceptions=True,
    )
    return {
        "postgres": _norm(results[0]),
        "redis": _norm(results[1]),
        "mongo": _norm(results[2]),
        "minio": _norm(results[3]),
    }
