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

## Tech

- Python 3.11+, FastAPI. See [docs/DECISION_LOG.md](docs/DECISION_LOG.md) for full stack.
