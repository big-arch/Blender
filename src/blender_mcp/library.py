"""Search and download models from the Scenario 3D Collection on Hugging Face.

https://huggingface.co/datasets/scenario-labs/3d-collection — ~1,300
AI-generated models (GLB, .blend, FBX, PLY, ...), CC BY 4.0, credit "Scenario".

Standard library only. Search uses the Hugging Face dataset-viewer API, files
are fetched on demand from the repo and cached locally, so nothing of the
39 GB collection is downloaded until a model is actually needed.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

DATASET = "scenario-labs/3d-collection"
FILE_URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main/"
API_URL = "https://datasets-server.huggingface.co/"

# Formats Blender can import, best first.
IMPORT_PRIORITY = ["blend", "glb", "fbx", "ply"]


def cache_dir() -> Path:
    root = os.environ.get("BLENDER_MCP_ASSET_CACHE") or Path.home() / ".cache" / "blender_mcp" / "3d-collection"
    return Path(root)


def _get_json(url: str, timeout: float = 30) -> Any:
    request = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _headers() -> Dict[str, str]:
    token = os.environ.get("HF_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _summarize(row: Dict[str, Any]) -> Dict[str, Any]:
    outputs = row.get("outputs") or []
    return {
        "id": row.get("id"),
        "prompt": row.get("prompt"),
        "workflow": row.get("workflow"),
        "model": row.get("model"),
        "formats": sorted({o.get("format") for o in outputs if o.get("format")}),
        "files": [o.get("path") for o in outputs if o.get("path")],
        "preview": FILE_URL + row["preview_file"]["path"] if row.get("preview_file") else None,
    }


def search(query: Optional[str] = None, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
    """Full-text search over prompts and metadata; no query lists rows in order."""
    params = {"dataset": DATASET, "config": "default", "split": "train", "offset": offset, "length": limit}
    if query:
        params["query"] = query
    endpoint = "search" if query else "rows"
    payload = _get_json(API_URL + endpoint + "?" + urllib.parse.urlencode(params))
    return [_summarize(item["row"]) for item in payload.get("rows", [])]


def get(asset_id: str) -> Dict[str, Any]:
    """Look up one example by its id (e.g. 'scenario:asset_13ERk...' or the bare asset_ part)."""
    needle = asset_id.split(":")[-1]
    for row in search(needle, limit=20):
        if row["id"] and row["id"].split(":")[-1] == needle:
            return row
    raise LookupError(f"No asset '{asset_id}' in {DATASET}")


def pick_file(files: List[str], fmt: Optional[str] = None) -> str:
    by_ext = {Path(f).suffix.lstrip(".").lower(): f for f in files}
    for ext in [fmt.lower()] if fmt else IMPORT_PRIORITY:
        if ext in by_ext:
            return by_ext[ext]
    raise LookupError(f"No {'.' + fmt if fmt else 'importable'} file among {files}")


def download(path: str, timeout: float = 600) -> Path:
    """Download a repo-relative file into the cache (skipped when already there)."""
    target = cache_dir() / path
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(FILE_URL + urllib.parse.quote(path), headers=_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response, open(partial, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    partial.replace(target)
    return target
