# Bulk import (CSV / Excel)

Import or update **assets**, **episodes**, **sequences**, and **shots** in bulk from a CSV file or an Excel workbook.

## Install extra deps (for the script)

```bash
pip install -r scripts/requirements.txt
```

Or: `pip install pandas httpx openpyxl`

## Usage

**Excel (multiple sheets)**  
Use one sheet per entity type. Sheet names must be exactly: `Assets`, `Episodes`, `Sequences`, `Shots` (case-insensitive).

```bash
python scripts/bulk_import.py --file path/to/entities.xlsx --base-url http://127.0.0.1:8000
```

**CSV (single entity type)**  
One file = one type. Require `--type`.

```bash
python scripts/bulk_import.py --file path/to/assets.csv --type assets --base-url http://127.0.0.1:8000
python scripts/bulk_import.py --file path/to/shots.csv --type shots --base-url http://127.0.0.1:8000
```

**Dry run (no writes)**  
See what would be created/updated without calling POST/PATCH:

```bash
python scripts/bulk_import.py --file entities.xlsx --dry-run
```

## Column requirements

| Sheet/Type   | Required columns                          | Optional |
|-------------|--------------------------------------------|----------|
| **Assets**  | `project_code`, `type`, `name`, `code`     | `metadata` (JSON string) |
| **Episodes**| `project_code`, `episode_number`, `code`    | `title`, `status`, `air_date`, `metadata` |
| **Sequences** | `project_code`, `episode_code`, `name`, `code` | `metadata` |
| **Shots**   | `project_code`, `episode_code`, `sequence_code`, `shot_code` | `frame_start`, `frame_end`, `handle_in`, `handle_out`, `status`, `metadata` |

- **project_code**, **episode_code**, **sequence_code**: must match existing project/episode/sequence codes (create episodes/sequences first, or use the same file with Episodes and Sequences sheets before Shots).
- **Asset type** must be one of: `character`, `prop`, `environment`, `fx`, `rig`, `texture_set`, `groom`, `shader`.
- **Shot status** must be one of: `pending`, `in_progress`, `review`, `approved`, `final`.

## Create vs update

- If an entity with the same **parent + code** already exists → **PATCH** (update).
- Otherwise → **POST** (create).

So re-running the same file is safe: it will update existing rows and create new ones.
