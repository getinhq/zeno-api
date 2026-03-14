# Database schema and API testing

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

## Run tests

```bash
cd zeno-api
# Optional: set DATABASE_URL and MONGO_URI to run DB-dependent tests
pytest tests/ -v
```

Tests in `tests/test_projects_and_settings.py` are skipped when `DATABASE_URL` or `MONGO_URI` is not set. `test_projects_create` is always skipped (TestClient + asyncpg pool); test POST manually with curl as above.
