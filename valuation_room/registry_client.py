"""HTTP client for AssetProgramRegistry (GET /assets/<asset_id>/lineage)."""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import httpx

DEFAULT_REGISTRY_URL = "http://localhost:5101"
ASSET_ID_RE = re.compile(r"^AST-\d{6}$")


class RegistryUnavailable(Exception):
    """The registry could not be reached or answered with a server error."""


class AssetNotFound(Exception):
    """The registry answered 404 for this asset."""


def registry_url(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("ASSET_REGISTRY_URL") or DEFAULT_REGISTRY_URL).rstrip("/")


def fetch_lineage(asset_id: str, base_url: Optional[str] = None, timeout: float = 5.0) -> dict[str, Any]:
    """Fetch an asset lineage record; raises RegistryUnavailable / AssetNotFound."""
    if not ASSET_ID_RE.match(asset_id):
        raise ValueError(f"asset_id must match AST-NNNNNN, got {asset_id!r}")
    base = registry_url(base_url)
    url = f"{base}/assets/{asset_id}/lineage"
    try:
        resp = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RegistryUnavailable(f"AssetProgramRegistry is unreachable at {base} ({exc.__class__.__name__})") from exc
    if resp.status_code == 404:
        raise AssetNotFound(f"AssetProgramRegistry has no asset {asset_id}")
    if resp.status_code != 200:
        raise RegistryUnavailable(f"AssetProgramRegistry at {base} returned HTTP {resp.status_code} for {asset_id}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise RegistryUnavailable(f"AssetProgramRegistry at {base} returned a non-JSON lineage body") from exc
    if not isinstance(data, dict):
        raise RegistryUnavailable("AssetProgramRegistry lineage body is not a JSON object")
    return data
