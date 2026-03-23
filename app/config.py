"""Load config from environment."""
import os

from dotenv import load_dotenv

# Load .env from zeno-api project root (where main.py lives), not cwd
_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_here)
load_dotenv(os.path.join(_project_root, ".env"))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None
REDIS_URL = os.environ.get("REDIS_URL", "").strip() or None
MONGO_URI = os.environ.get("MONGO_URI", "").strip() or None
# MongoDB database name when URI has no path (e.g. mongodb+srv://...net/). Use same as Postgres if you like.
MONGO_DB_NAME = os.environ.get("ZENO_MONGO_DB", "zeno_db").strip() or "zeno_db"

# S3 / MinIO — used when CAS_STORAGE_BACKEND=s3 or auto (with credentials set)
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "").strip() or None
S3_BUCKET_CAS = os.environ.get("S3_BUCKET_CAS", "zeno-cas").strip() or "zeno-cas"
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "").strip() or None
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "").strip() or None

# CAS backend: nas | s3 | auto (default). auto → S3 if endpoint + keys set, else filesystem CAS_ROOT.
CAS_STORAGE_BACKEND = os.environ.get("CAS_STORAGE_BACKEND", "auto").strip().lower() or "auto"

# CAS storage (filesystem). Set ZENO_CAS_ROOT or CAS_ROOT when using nas or auto without S3.
CAS_ROOT = os.environ.get("ZENO_CAS_ROOT", os.environ.get("CAS_ROOT", "")).strip() or None
