"""Pydantic models for launch_context_v1 (aligned with schemas/launch_context_v1.json)."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class LaunchContextV1(BaseModel):
    version: Literal["1"]
    intent: Literal["open_asset", "open_shot", "open_task", "open_project"]
    project_id: str
    project_code: Optional[str] = None
    asset_id: Optional[str] = None
    shot_id: Optional[str] = None
    task_id: Optional[str] = None
    representation: Optional[str] = None
    version_spec: Optional[dict[str, Any]] = None
    dcc: str = Field(..., description="Target DCC id, e.g. blender, maya, nuke")
    dcc_label: Optional[str] = Field(
        None,
        description="Label from Application Settings (e.g. Blender 4.2) to disambiguate versions",
    )
    dcc_executable_path: Optional[str] = Field(
        None,
        description="Absolute path to DCC binary; filled by API from settings when possible",
    )
    resolved_path: Optional[str] = None
    api_base_url: Optional[str] = None


class MintLaunchTokenBody(BaseModel):
    context: LaunchContextV1


class MintLaunchTokenResponse(BaseModel):
    token: str
    expires_at: str


class ExchangeLaunchTokenResponse(BaseModel):
    context: LaunchContextV1
