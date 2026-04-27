"""Bootstrap one Pipeline user so nobody is locked out after enabling auth.

Usage (from repo root):

    python -m scripts.seed_users --username admin --email admin@studio.local --password changeme

Environment fallbacks:
    ZENO_SEED_USERNAME / ZENO_SEED_EMAIL / ZENO_SEED_PASSWORD / ZENO_SEED_APP_ROLE

Idempotent: if the username already exists the script exits 0 without
touching the row.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

from app.auth import service as auth_service


async def _run(username: str, email: str, password: str, app_role: str, name: str | None) -> int:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(url)
    try:
        existing = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
        if existing:
            print(f"User '{username}' already exists (id={existing['id']}); skipping.")
            return 0
        pw_hash = auth_service.hash_password(password)
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, username, password_hash, name, app_role, role, is_active)
            VALUES ($1, $2, $3, $4, $5, 'admin', TRUE)
            RETURNING id
            """,
            email,
            username,
            pw_hash,
            name or username,
            app_role,
        )
        print(f"Seeded user '{username}' (id={row['id']}) with app_role={app_role}")
        return 0
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a user into the zeno-api DB.")
    parser.add_argument("--username", default=os.environ.get("ZENO_SEED_USERNAME"))
    parser.add_argument("--email", default=os.environ.get("ZENO_SEED_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("ZENO_SEED_PASSWORD"))
    parser.add_argument(
        "--app-role",
        default=os.environ.get("ZENO_SEED_APP_ROLE", "pipeline"),
        choices=["artist", "pipeline", "supervisor", "production"],
    )
    parser.add_argument("--name", default=os.environ.get("ZENO_SEED_NAME"))
    args = parser.parse_args()

    if not (args.username and args.email and args.password):
        print(
            "Missing arguments. Provide --username, --email and --password (or "
            "ZENO_SEED_* env vars).",
            file=sys.stderr,
        )
        sys.exit(2)

    rc = asyncio.run(_run(args.username, args.email, args.password, args.app_role, args.name))
    sys.exit(rc)


if __name__ == "__main__":
    main()
