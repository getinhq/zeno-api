# zeno-api

Zeno backend API (FastAPI). Production tracking, CAS, resolver, and core REST API.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn main:app --reload
```

API: http://127.0.0.1:8000  
Docs: http://127.0.0.1:8000/docs

## CAS (Content-Addressable Storage)

Phase 0.1 CAS is **NAS-only**: blobs are stored on a local or NAS path keyed by SHA-256 hash.

- **Env:** Set `ZENO_CAS_ROOT` (or `CAS_ROOT`) to an absolute path, e.g. `/mnt/zeno-cas` or `./data/cas` for dev. If unset, CAS endpoints return 503.
- **Layout:** `{CAS_ROOT}/{first2}/{next2}/{full_64char_hash}` (e.g. `ab/cd/abcd...`).
- **Endpoints:** `PUT /api/v1/cas/blobs/<hash>` (stream body, server verifies SHA-256); `GET /api/v1/cas/blobs/<hash>`; `HEAD /api/v1/cas/blobs/<hash>`.

S3/MinIO is not used for CAS in 0.1.

## Tech

- Python 3.11+, FastAPI. See [docs/DECISION_LOG.md](docs/DECISION_LOG.md) for full stack.
