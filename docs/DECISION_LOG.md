# Zeno tech stack — decision log

Single source of truth for stack and versions. Aligned with Development Roadmap and page.tsx §12.2.

## Stack

| Area | Choice | Notes |
|------|--------|--------|
| Backend | Python 3.11+, FastAPI | Async, OpenAPI |
| Relational DB | PostgreSQL 16 | Production tracking, CTEs for graph |
| Document store | MongoDB 7 | Scene metadata, flexible schemas |
| Cache / sessions / locks | Redis 7 | TTL, presence, lock keys |
| Event bus | NATS or Redis Streams | Decide one; roadmap suggests NATS |
| Web dashboard | React 18, Vite, Tailwind | Per roadmap |
| DCC plugins | Python: cmds/OpenMaya2, hou, nuke, bpy | Thin layer, ~350–500 lines per plugin |
| CAS storage (0.1) | NAS only (local/NAS path) | Hash-keyed blobs; server-side verify; no S3 in 0.1 |

## Future (Phase 2+)

- **Task queue:** Celery + Redis (render queue, background jobs).
- **Vector:** pgvector on PostgreSQL (semantic search).
- **Graph:** PostgreSQL recursive CTEs for dependency graph (or Neo4j if scale demands).

## Rationale

- FastAPI: async, automatic OpenAPI, fast iteration.
- Postgres 16 + MongoDB 7 + Redis 7: production-grade, well-documented versions.
- React 18 + Vite + Tailwind: matches roadmap and dashboard needs.
- DCC plugins: Python-only, thin wrappers around DCC APIs.

Reviewed from roadmap + page.tsx 12.2.
