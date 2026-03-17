#!/usr/bin/env python3
"""
Bulk import entities (assets, episodes, sequences, shots) from CSV or Excel into Zeno API.

Usage:
  # From Excel: one sheet per entity type (sheet names: Assets, Episodes, Sequences, Shots)
  python scripts/bulk_import.py --file entities.xlsx --base-url http://127.0.0.1:8000

  # From CSV: specify entity type (one type per file)
  python scripts/bulk_import.py --file assets.csv --type assets --base-url http://127.0.0.1:8000

  # Dry run (no writes)
  python scripts/bulk_import.py --file entities.xlsx --dry-run

Required columns by sheet/type:
  Assets:    project_code, type, name, code  [metadata optional]
  Episodes:  project_code, episode_number, code  [title, status, air_date optional]
  Sequences: project_code, episode_code, name, code
  Shots:     project_code, episode_code, sequence_code, shot_code  [frame_start, frame_end, handle_in, handle_out, status optional]

Create vs update: if an entity with the same parent + code already exists, it is updated (PATCH); otherwise created (POST).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

try:
    import pandas as pd
except ImportError:
    print("Install pandas: pip install pandas", file=sys.stderr)
    sys.exit(1)
try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx", file=sys.stderr)
    sys.exit(1)


def _norm(s: Any) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip()


def _int(s: Any) -> Optional[int]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _date_str(s: Any) -> Optional[str]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).strip()
    return s if s else None


def _json_metadata(s: Any) -> dict:
    if s is None or (isinstance(s, float) and pd.isna(s)) or str(s).strip() == "":
        return {}
    try:
        v = json.loads(str(s))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _get_projects_by_code(base_url: str) -> dict[str, str]:
    """Return map project_code -> project_id."""
    r = httpx.get(f"{base_url.rstrip('/')}/api/v1/projects", timeout=30.0)
    r.raise_for_status()
    data = r.json()
    return {str(p["code"]).strip().lower(): str(p["id"]) for p in data}


def _get_episodes_by_code(base_url: str, project_id: str) -> dict[str, str]:
    """Return map episode_code -> episode_id."""
    r = httpx.get(f"{base_url.rstrip('/')}/api/v1/projects/{project_id}/episodes", timeout=30.0)
    r.raise_for_status()
    data = r.json()
    return {str(e["code"]).strip().lower(): str(e["id"]) for e in data}


def _get_sequences_by_code(base_url: str, episode_id: str) -> dict[str, str]:
    """Return map sequence_code -> sequence_id."""
    r = httpx.get(f"{base_url.rstrip('/')}/api/v1/episodes/{episode_id}/sequences", timeout=30.0)
    r.raise_for_status()
    data = r.json()
    return {str(s["code"]).strip().lower(): str(s["id"]) for s in data}


def _get_asset_id_by_code(base_url: str, project_id: str, code: str) -> Optional[str]:
    r = httpx.get(
        f"{base_url.rstrip('/')}/api/v1/projects/{project_id}/assets",
        params={"code": code},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    for a in data:
        if str(a.get("code", "")).strip().lower() == str(code).strip().lower():
            return str(a["id"])
    return None


def _get_shot_id_by_code(base_url: str, sequence_id: str, shot_code: str) -> Optional[str]:
    r = httpx.get(
        f"{base_url.rstrip('/')}/api/v1/sequences/{sequence_id}/shots",
        params={"shot_code": shot_code},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    for s in data:
        if str(s.get("shot_code", "")).strip().lower() == str(shot_code).strip().lower():
            return str(s["id"])
    return None


def import_assets(base_url: str, df: pd.DataFrame, dry_run: bool) -> tuple[int, int]:
    created, updated = 0, 0
    projects = _get_projects_by_code(base_url)
    for _, row in df.iterrows():
        project_code = _norm(row.get("project_code", ""))
        if not project_code:
            continue
        pid = projects.get(project_code.lower())
        if not pid:
            print(f"  [skip] project not found: {project_code}", file=sys.stderr)
            continue
        typ = _norm(row.get("type", ""))
        name = _norm(row.get("name", ""))
        code = _norm(row.get("code", ""))
        if not typ or not name or not code:
            continue
        metadata = _json_metadata(row.get("metadata"))
        existing = _get_asset_id_by_code(base_url, pid, code)
        if existing:
            if dry_run:
                print(f"  [would update] asset {code}")
                updated += 1
                continue
            r = httpx.patch(
                f"{base_url.rstrip('/')}/api/v1/assets/{existing}",
                json={"type": typ, "name": name, "metadata": metadata},
                timeout=30.0,
            )
            r.raise_for_status()
            updated += 1
        else:
            if dry_run:
                print(f"  [would create] asset {code}")
                created += 1
                continue
            r = httpx.post(
                f"{base_url.rstrip('/')}/api/v1/projects/{pid}/assets",
                json={"type": typ, "name": name, "code": code, "metadata": metadata},
                timeout=30.0,
            )
            r.raise_for_status()
            created += 1
    return created, updated


def import_episodes(base_url: str, df: pd.DataFrame, dry_run: bool) -> tuple[int, int]:
    created, updated = 0, 0
    projects = _get_projects_by_code(base_url)
    for _, row in df.iterrows():
        project_code = _norm(row.get("project_code", ""))
        if not project_code:
            continue
        pid = projects.get(project_code.lower())
        if not pid:
            print(f"  [skip] project not found: {project_code}", file=sys.stderr)
            continue
        episode_number = _int(row.get("episode_number"))
        code = _norm(row.get("code", ""))
        if code == "" or episode_number is None:
            continue
        title = _norm(row.get("title", "")) or None
        status = _norm(row.get("status", "")) or "in_production"
        air_date = _date_str(row.get("air_date"))
        metadata = _json_metadata(row.get("metadata"))
        episodes = _get_episodes_by_code(base_url, pid)
        existing_id = episodes.get(code.lower())
        if existing_id:
            if dry_run:
                print(f"  [would update] episode {code}")
                updated += 1
                continue
            r = httpx.patch(
                f"{base_url.rstrip('/')}/api/v1/episodes/{existing_id}",
                json={"episode_number": episode_number, "title": title, "code": code, "status": status, "air_date": air_date, "metadata": metadata},
                timeout=30.0,
            )
            r.raise_for_status()
            updated += 1
        else:
            if dry_run:
                print(f"  [would create] episode {code}")
                created += 1
                continue
            r = httpx.post(
                f"{base_url.rstrip('/')}/api/v1/projects/{pid}/episodes",
                json={"episode_number": episode_number, "title": title, "code": code, "status": status, "air_date": air_date, "metadata": metadata},
                timeout=30.0,
            )
            r.raise_for_status()
            created += 1
    return created, updated


def import_sequences(base_url: str, df: pd.DataFrame, dry_run: bool) -> tuple[int, int]:
    created, updated = 0, 0
    projects = _get_projects_by_code(base_url)
    for _, row in df.iterrows():
        project_code = _norm(row.get("project_code", ""))
        episode_code = _norm(row.get("episode_code", ""))
        if not project_code or not episode_code:
            continue
        pid = projects.get(project_code.lower())
        if not pid:
            print(f"  [skip] project not found: {project_code}", file=sys.stderr)
            continue
        episodes = _get_episodes_by_code(base_url, pid)
        eid = episodes.get(episode_code.lower())
        if not eid:
            print(f"  [skip] episode not found: {episode_code} (project {project_code})", file=sys.stderr)
            continue
        name = _norm(row.get("name", ""))
        code = _norm(row.get("code", ""))
        if not name or not code:
            continue
        metadata = _json_metadata(row.get("metadata"))
        seqs = _get_sequences_by_code(base_url, eid)
        existing_id = seqs.get(code.lower())
        if existing_id:
            if dry_run:
                print(f"  [would update] sequence {code}")
                updated += 1
                continue
            r = httpx.patch(
                f"{base_url.rstrip('/')}/api/v1/sequences/{existing_id}",
                json={"name": name, "code": code, "metadata": metadata},
                timeout=30.0,
            )
            r.raise_for_status()
            updated += 1
        else:
            if dry_run:
                print(f"  [would create] sequence {code}")
                created += 1
                continue
            r = httpx.post(
                f"{base_url.rstrip('/')}/api/v1/episodes/{eid}/sequences",
                json={"name": name, "code": code, "metadata": metadata},
                timeout=30.0,
            )
            r.raise_for_status()
            created += 1
    return created, updated


def import_shots(base_url: str, df: pd.DataFrame, dry_run: bool) -> tuple[int, int]:
    created, updated = 0, 0
    projects = _get_projects_by_code(base_url)
    for _, row in df.iterrows():
        project_code = _norm(row.get("project_code", ""))
        episode_code = _norm(row.get("episode_code", ""))
        sequence_code = _norm(row.get("sequence_code", ""))
        shot_code = _norm(row.get("shot_code", ""))
        if not project_code or not episode_code or not sequence_code or not shot_code:
            continue
        pid = projects.get(project_code.lower())
        if not pid:
            print(f"  [skip] project not found: {project_code}", file=sys.stderr)
            continue
        episodes = _get_episodes_by_code(base_url, pid)
        eid = episodes.get(episode_code.lower())
        if not eid:
            print(f"  [skip] episode not found: {episode_code}", file=sys.stderr)
            continue
        seqs = _get_sequences_by_code(base_url, eid)
        sid = seqs.get(sequence_code.lower())
        if not sid:
            print(f"  [skip] sequence not found: {sequence_code}", file=sys.stderr)
            continue
        frame_start = _int(row.get("frame_start"))
        frame_end = _int(row.get("frame_end"))
        handle_in = _int(row.get("handle_in")) or 0
        handle_out = _int(row.get("handle_out")) or 0
        status = _norm(row.get("status", "")) or "pending"
        metadata = _json_metadata(row.get("metadata"))
        existing_id = _get_shot_id_by_code(base_url, sid, shot_code)
        if existing_id:
            if dry_run:
                print(f"  [would update] shot {shot_code}")
                updated += 1
                continue
            body = {"frame_start": frame_start, "frame_end": frame_end, "handle_in": handle_in, "handle_out": handle_out, "status": status, "metadata": metadata}
            body = {k: v for k, v in body.items() if v is not None or k == "metadata"}
            r = httpx.patch(
                f"{base_url.rstrip('/')}/api/v1/shots/{existing_id}",
                json=body,
                timeout=30.0,
            )
            r.raise_for_status()
            updated += 1
        else:
            if dry_run:
                print(f"  [would create] shot {shot_code}")
                created += 1
                continue
            r = httpx.post(
                f"{base_url.rstrip('/')}/api/v1/sequences/{sid}/shots",
                json={
                    "shot_code": shot_code,
                    "frame_start": frame_start,
                    "frame_end": frame_end,
                    "handle_in": handle_in,
                    "handle_out": handle_out,
                    "status": status,
                    "metadata": metadata,
                },
                timeout=30.0,
            )
            r.raise_for_status()
            created += 1
    return created, updated


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk import assets, episodes, sequences, shots from CSV or Excel")
    ap.add_argument("--file", "-f", required=True, help="Path to .csv or .xlsx file")
    ap.add_argument("--type", "-t", choices=["assets", "episodes", "sequences", "shots"], help="For CSV: entity type (required if file is CSV)")
    ap.add_argument("--base-url", "-u", default="http://127.0.0.1:8000", help="Zeno API base URL")
    ap.add_argument("--dry-run", action="store_true", help="Do not write; only report what would be done")
    args = ap.parse_args()

    path = args.file
    base_url = args.base_url.rstrip("/")
    dry_run = args.dry_run

    if path.lower().endswith(".xlsx") or path.lower().endswith(".xls"):
        try:
            xl = pd.ExcelFile(path)
        except Exception as e:
            print(f"Failed to read Excel: {e}", file=sys.stderr)
            sys.exit(1)
        sheet_names = [s for s in xl.sheet_names if s]
        # Normalize: Assets, assets, ASSETS -> assets
        sheets_to_process: list[tuple[str, str]] = []
        for name in sheet_names:
            key = name.strip().lower()
            if key in ("assets", "episodes", "sequences", "shots"):
                sheets_to_process.append((key, name))
        if not sheets_to_process:
            print("No sheets named Assets, Episodes, Sequences, or Shots found.", file=sys.stderr)
            sys.exit(1)
        for key, orig_name in sheets_to_process:
            df = pd.read_excel(xl, sheet_name=orig_name)
            df = df.dropna(how="all").reset_index(drop=True)
            if df.empty:
                continue
            print(f"Sheet '{orig_name}' ({key}): {len(df)} rows")
            if key == "assets":
                c, u = import_assets(base_url, df, dry_run)
            elif key == "episodes":
                c, u = import_episodes(base_url, df, dry_run)
            elif key == "sequences":
                c, u = import_sequences(base_url, df, dry_run)
            else:
                c, u = import_shots(base_url, df, dry_run)
            print(f"  created={c}, updated={u}")
    else:
        # CSV: single type
        if not args.type:
            print("For CSV files, --type is required (assets, episodes, sequences, or shots).", file=sys.stderr)
            sys.exit(1)
        try:
            df = pd.read_csv(path)
        except Exception as e:
            print(f"Failed to read CSV: {e}", file=sys.stderr)
            sys.exit(1)
        df = df.dropna(how="all").reset_index(drop=True)
        if df.empty:
            print("CSV is empty or has no data rows.", file=sys.stderr)
            sys.exit(1)
        print(f"CSV ({args.type}): {len(df)} rows")
        if args.type == "assets":
            c, u = import_assets(base_url, df, dry_run)
        elif args.type == "episodes":
            c, u = import_episodes(base_url, df, dry_run)
        elif args.type == "sequences":
            c, u = import_sequences(base_url, df, dry_run)
        else:
            c, u = import_shots(base_url, df, dry_run)
        print(f"  created={c}, updated={u}")

    if dry_run:
        print("(dry run — no changes made)")


if __name__ == "__main__":
    main()
