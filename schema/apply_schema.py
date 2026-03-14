"""Apply schema/init.sql to the database. Run from repo root with DATABASE_URL set.

  python -m schema.apply_schema

Or: uv run python -m schema.apply_schema
"""
from __future__ import annotations

import os
import sys

def main() -> None:
    try:
        import asyncpg
    except ImportError:
        print("asyncpg required: pip install asyncpg", file=sys.stderr)
        sys.exit(1)

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    schema_dir = os.path.dirname(os.path.abspath(__file__))
    init_sql = os.path.join(schema_dir, "init.sql")
    if not os.path.isfile(init_sql):
        print(f"Schema file not found: {init_sql}", file=sys.stderr)
        sys.exit(1)

    sql = open(init_sql, "r").read()

    async def run() -> None:
        conn = await asyncpg.connect(url)
        try:
            # Execute entire file (asyncpg supports multiple statements in one call)
            await conn.execute(sql)
            print("Schema applied successfully.")
        finally:
            await conn.close()

    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    main()
