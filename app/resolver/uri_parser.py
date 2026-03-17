"""Parse and validate asset URIs: asset://project/asset/version/representation."""
from __future__ import annotations

import re
from typing import Tuple, Union

# UUID pattern (8-4-4-4-12 hex)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def parse_asset_uri(uri: str) -> Tuple[str, str, Union[str, int], str]:
    """Parse asset URI into (project_spec, asset_spec, version_spec, representation).

    URI form: asset://project/asset/version/representation
    - project: code or UUID
    - asset: code or UUID
    - version: 'latest' (case-insensitive) or integer string
    - representation: e.g. model, fbx, abc

    Returns:
        (project_spec, asset_spec, version_spec, representation)
        version_spec is either "latest" or int.

    Raises:
        ValueError: if scheme is not asset, segment count != 4, or version invalid.
    """
    if not uri or not isinstance(uri, str):
        raise ValueError("URI is required")
    s = uri.strip()
    if not s.lower().startswith("asset://"):
        raise ValueError("URI scheme must be asset")
    rest = s[8:].lstrip("/")  # after "asset://"
    parts = rest.split("/")
    if len(parts) != 4:
        raise ValueError("URI must have exactly 4 segments: project/asset/version/representation")
    project_spec, asset_spec, version_spec_raw, representation = parts
    if not project_spec:
        raise ValueError("Project segment is empty")
    if not asset_spec:
        raise ValueError("Asset segment is empty")
    if not representation:
        raise ValueError("Representation segment is empty")

    # Normalize version: "latest" or integer
    version_spec_raw = version_spec_raw.strip()
    if version_spec_raw.lower() == "latest":
        version_spec: Union[str, int] = "latest"
    else:
        try:
            version_spec = int(version_spec_raw)
            if version_spec < 0:
                raise ValueError("Version number must be non-negative")
        except ValueError:
            raise ValueError("Version must be 'latest' or a non-negative integer")

    return (project_spec.strip(), asset_spec.strip(), version_spec, representation.strip())


def is_uuid(s: str) -> bool:
    """Return True if s looks like a UUID."""
    return bool(_UUID_RE.match(s.strip()))
