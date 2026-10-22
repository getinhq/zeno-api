"""Shared test fixtures."""
import pytest

import app.db as app_db
import app.redis_conn as app_redis


@pytest.fixture(autouse=True)
def _fresh_connection_singletons():
    """
    The asyncpg pool and Redis client are process-wide singletons bound to the event loop
    that created them. Each TestClient / async test runs its own loop, so a singleton left
    over from an earlier test fails with "Event loop is closed". Start every test without one.
    """
    app_db._pool = None
    app_redis._redis = None
    yield
    app_db._pool = None
    app_redis._redis = None
