# Database schema and API testing

## One-command dev (Docker Compose)

From **zeno-infra** you can start the full stack (Postgres, MongoDB, Redis, API) with one command:

```bash
cd zeno-infra
cp .env.example .env   # optional: edit .env to set POSTGRES_PASSWORD etc.
docker compose up -d
```

- **API:** [http://localhost:8000](http://localhost:8000) — try `GET /health` and `GET /api/v1/projects`.
- **Schema** is applied automatically on first run via the `schema-init` service (idempotent; safe to re-run).
- **CAS** uses a named volume at `/app/cas_data` inside the API container (`ZENO_CAS_ROOT` is set by Compose).

To include MinIO (S3-compatible): `docker compose --profile full up -d`. Required env vars for the API (`DATABASE_URL`, `MONGO_URI`, `REDIS_URL`, `ZENO_CAS_ROOT`) are set from `zeno-infra/.env`; see `zeno-infra/.env.example`.

## Schema (Postgres)

The pipeline schema lives in `schema/init.sql` and is applied once to `zeno_db`:

- **Tables:** users, projects, episodes, sequences, shots, assets, tasks, versions, shot_assets, render_jobs, workflows, reviews
- **CAS:** `versions` has `content_id`, `filename`, `representation`; unique on `(asset_id, representation, version_number)`

### Apply schema

With Docker Postgres (zeno-infra) running and `DATABASE_URL` set:

```bash
cd zeno-api
export DATABASE_URL="postgresql://zeno_admin:ZenoChimera%4025@localhost:5432/zeno_db"
python -m schema.apply_schema
```

Or from zeno-infra (pipe SQL into container):

```bash
cd zeno-infra
docker compose exec -T postgres psql -U zeno_admin -d zeno_db < ../zeno-api/schema/init.sql
```

## Settings (MongoDB)

Settings are in MongoDB (see [Settings Storage Design](.cursor/plans/settings_storage_design_64d012c4.plan.md)):

- **settings_global:** one document per env (`production`, `staging`, `development`)
- **settings_project:** one document per project (overrides keyed by `project_id`)

Indexes are created at API startup when `MONGO_URI` is set. If the URI has no database (e.g. `mongodb://localhost:27017`), the database `zeno` is used.

## Run API and test endpoints

1. Start Postgres and MongoDB (e.g. `cd zeno-infra && docker compose up -d`).

2. Set env and run the API:

   ```bash
   cd zeno-api
   export DATABASE_URL="postgresql://zeno_admin:ZenoChimera%4025@localhost:5432/zeno_db"
   export MONGO_URI="mongodb://localhost:27017"
   python -m uvicorn main:app --host 127.0.0.1 --port 8000
   ```

3. Try:

   - `GET /` — service info
   - `GET /health` — Postgres, Mongo, Redis, MinIO status
   - `GET /projects` — list projects (Postgres)
   - `POST /projects` — create project (body: `{"name":"My Show","code":"MS01"}`)
   - `GET /projects/{project_id}` — get one project
   - `GET /settings/global?env=development` — global settings (Mongo)
   - `GET /settings/effective?env=development&project_id=<uuid>` — merged settings
   - `GET /settings/project/{project_id}` — project overrides (404 if none)
   - **Resolver:** `GET /api/v1/resolve?uri=asset://project/asset/version/representation` or `POST /api/v1/resolve` with body `{"uri": "asset://..."}` — returns `content_id`, `filename`, `size` (400 invalid URI, 404 not found, 503 DB unavailable)
   - **Register-Version:** `POST /api/v1/versions` — register a new version row for an existing project/asset, pointing to an existing CAS blob.
   - **Redis workflow:** `POST /api/v1/presence/heartbeat`, `GET /api/v1/presence/sessions`, `POST /api/v1/locks/acquire`, `POST /api/v1/locks/release`, `GET /api/v1/locks/status` — presence and lock primitives backed by Redis.
   - **Core REST API (0.7):** `GET/POST/PATCH/DELETE /api/v1/projects`, `GET/POST/PATCH /api/v1/projects/{project_id}/assets` and `/api/v1/assets/{asset_id}`, `GET/POST/PATCH /api/v1/sequences/{sequence_id}/shots` and `/api/v1/shots/{shot_id}`, `GET/POST/PATCH /api/v1/tasks` and `/api/v1/tasks/{task_id}` — core CRUD for projects, assets, shots, and tasks.

## Resolver API (Phase 0.3)

- **URI format:** `asset://{project}/{asset}/{version}/{representation}`
  - **project:** project code (e.g. `MS01`) or UUID
  - **asset:** asset code (e.g. `hero_model`) or UUID
  - **version:** `latest` (max version) or integer (e.g. `3`)
  - **representation:** e.g. `model`, `fbx`, `abc`
- **Response 200:** `{"content_id": "<64-char hex>", "filename": "<name>", "size": <bytes or null>}`
- **Errors:** 400 (malformed URI or invalid version), 404 (no matching project/asset/version), 503 (DATABASE_URL not set or connection error)

Example: `GET /api/v1/resolve?uri=asset://MS01/hero_model/latest/fbx`

## Register-Version API (Phase 0.5)

- **Endpoint:** `POST /api/v1/versions`
- **Body (JSON):**
  - `project`: project code or UUID
  - `asset`: asset code or UUID under that project
  - `representation`: representation key (e.g. `model`, `fbx`, `abc`)
  - `version`: `"next"` or explicit positive integer (string)
  - `content_id`: 64-char lowercase SHA-256 hex; must already exist in CAS
  - `filename` (optional): display name
  - `size` (optional): bytes
- **Response 201:** `{"project_id": "...", "asset_id": "...", "version_id": "...", "version_number": 1, "content_id": "...", "filename": "...", "size": 123}`.
- **Errors:** 400 (invalid payload), 404 (project/asset not found), 409 (explicit version collision or CAS content missing), 503 (DB unavailable).

## CAS API (blobs)

Content-addressable blob storage under `/api/v1/cas`. Requires `ZENO_CAS_ROOT`; otherwise endpoints return 503.

- **POST** `/api/v1/cas/blobs`  
  Upload a blob with hash in header. **Header:** `X-Content-Hash: <sha256>` (64 lowercase hex). **Body:** raw bytes (streamed). Server streams the body, computes SHA-256, and compares to the header; on mismatch returns 400. On match, writes to CAS: **201** if the blob was created, **200** if it already existed (idempotent). Errors: 400 (missing/invalid hash or content hash mismatch), 503 (CAS not configured).

- **PUT** `/api/v1/cas/blobs/{hash}`  
  Upload with hash in path; same stream-and-verify behaviour; 201 created, 200 if exists, 400 on mismatch.

- **GET** `/api/v1/cas/blobs/{hash}`  
  Stream blob bytes; 404 if not found.

- **HEAD** `/api/v1/cas/blobs/{hash}`  
  200 with Content-Length if exists, 404 if not.

- **GET** `/api/v1/cas/blobs/{hash}/exists`  
  For client-side dedup. **200** with body `{"exists": true}` if the blob exists; **404** if not. Errors: 400 (invalid hash), 503 (CAS not configured).

## Redis workflow (presence and locks)

These endpoints use Redis (configured via `REDIS_URL`) to track user sessions and short-lived asset locks.

- **Presence**
  - `POST /api/v1/presence/heartbeat`  
    Body: `{"user_id": "...", "session_id": "...", "project": "...", "asset": "...", "representation": "...", "metadata": {...}}` (project/asset/representation and metadata are optional).  
    Writes `presence:<user_id>:<session_id>` with TTL ~60s and refreshes it on each call; also adds `session_id` to `presence_index:<user_id>`.
  - `GET /api/v1/presence/sessions?user_id=...`  
    Returns a list of sessions for that user with `user_id`, `session_id`, `updated_at`, and optional metadata.
  - `GET /api/v1/presence/asset?project=...&asset=...&representation=...`  
    Returns `{"sessions": [{"session_id": "..."}]}` for sessions that sent a heartbeat with that asset triple.

- **Locks**
  - `POST /api/v1/locks/acquire`  
    Body: `{"user_id": "...", "session_id": "...", "project": "...", "asset": "...", "representation": "..."}`.  
    Creates or refreshes `lock:<project>:<asset>:<representation>` with owner info and TTL (default ~10 minutes).  
    - 200 when acquired or when the same user/session re-acquires (idempotent).  
    - 409 when another session holds the lock.  
    - 503 on Redis errors.
  - `POST /api/v1/locks/release`  
    Same body as acquire.  
    - 200 when the caller owns the lock and it is released.  
    - 403 when another session owns the lock.  
    - 404 when no lock exists.  
    - 503 on Redis errors.
  - `GET /api/v1/locks/status?project=...&asset=...&representation=...`  
    - 200 with `{"project": "...", "asset": "...", "representation": "...", "owner_user_id": "...", "owner_session_id": "...", "acquired_at": "..."}` when locked.  
    - 404 if no lock exists.  
    - 503 on Redis errors.

## Run tests

```bash
cd zeno-api
# Optional: set DATABASE_URL and MONGO_URI to run DB-dependent tests
pytest tests/ -v
```

Tests in `tests/test_projects_and_settings.py` are skipped when `DATABASE_URL` or `MONGO_URI` is not set. `test_projects_create` is always skipped (TestClient + asyncpg pool); test POST manually with curl as above.
