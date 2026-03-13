"""Load config from environment."""
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None
REDIS_URL = os.environ.get("REDIS_URL", "").strip() or None
MONGO_URI = os.environ.get("MONGO_URI", "").strip() or None
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "").strip() or None
S3_BUCKET_CAS = os.environ.get("S3_BUCKET_CAS", "zeno-cas")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
