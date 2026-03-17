"""Resolver API: GET/POST /api/v1/resolve?uri=asset://project/asset/version/representation."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app.resolver.service import resolve
from app.resolver.uri_parser import parse_asset_uri

router = APIRouter(prefix="/api/v1", tags=["resolver"])


@router.get("/resolve")
async def get_resolve(uri: str) -> dict:
    """Resolve asset URI to content_id, filename, size. Query param: uri=asset://project/asset/version/representation."""
    try:
        project_spec, asset_spec, version_spec, representation = parse_asset_uri(uri)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        result = await resolve(project_spec, asset_spec, version_spec, representation)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Resolver error: {str(e)}") from e
    if result is None:
        raise HTTPException(status_code=404, detail="No matching project, asset, or version")
    return result


@router.post("/resolve")
async def post_resolve(body: dict = Body(...)) -> dict:
    """Resolve asset URI to content_id, filename, size. Body: {"uri": "asset://project/asset/version/representation"}."""
    uri = body.get("uri")
    if uri is None or not isinstance(uri, str):
        raise HTTPException(status_code=400, detail="Body must contain 'uri' (string)")
    try:
        project_spec, asset_spec, version_spec, representation = parse_asset_uri(uri)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        result = await resolve(project_spec, asset_spec, version_spec, representation)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Resolver error: {str(e)}") from e
    if result is None:
        raise HTTPException(status_code=404, detail="No matching project, asset, or version")
    return result
