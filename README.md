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

CAS supports **NAS or S3/MinIO** (selected via `CAS_STORAGE_BACKEND`), and uses
64-char lowercase hex **BLAKE3** content IDs.

- **Env (NAS):** set `ZENO_CAS_ROOT` (or `CAS_ROOT`) to an absolute path.
- **Env (MinIO/S3):** set `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET_CAS`.
- **Mode:** `CAS_STORAGE_BACKEND=nas|s3|auto`.
- **Layout:** `{CAS_ROOT}/{first2}/{next2}/{full_64char_hash}` (e.g. `ab/cd/abcd...`).
- **Endpoints:** `PUT /api/v1/cas/blobs/<hash>` (stream body, server verifies BLAKE3); `GET /api/v1/cas/blobs/<hash>`; `HEAD /api/v1/cas/blobs/<hash>`.
- **Exists cache:** `GET /api/v1/cas/blobs/<hash>/exists` is Redis-cached (TTL via `MANIFEST_CACHE_TTL_SECONDS`).

### MinIO production notes

- Use bucket prefixes for operations clarity:
  - `chunks/`
  - `manifests/`
  - `patches/`
- Enable MinIO erasure coding at deployment topology level.
- Enable Object Lock (WORM) for immutable CAS objects.
- Keep multipart upload limits tuned for large patch and manifest objects.

### Parent resolution for Omni publishers

`GET /api/v1/versions/latest-content?project=<>&asset=<>&representation=<>`

- Returns latest `content_id` to let clients compute patch/delta against parent versions.

### Manifest migration utility

To migrate legacy `chimera.manifest.v1` content IDs to BLAKE3:

```bash
python -m scripts.migrate_manifest_hashes_to_blake3
```

### CAS scrubber (hash verification)

Run sample scrubber with manifest-aware child verification:

```bash
python -m scripts.cas_scrub_sample --sample-size 200 --seed 42
```

## Tech

- Python 3.11+, FastAPI. See [docs/DECISION_LOG.md](docs/DECISION_LOG.md) for full stack.
