# Notes for Claude

## 3D model library

When a scene needs a ready-made model (prop, character, vehicle, furniture,
creature...), do not model it from primitives: take it from the
[Scenario 3D Collection](https://huggingface.co/datasets/scenario-labs/3d-collection)
(~1,300 models, CC BY 4.0, credit "Scenario").

1. `blender_search_library(query="dragon")` — find candidates; show the user the
   `preview` thumbnails when the choice matters.
2. `blender_import_library_asset(asset_id=..., name=..., location=...)` —
   downloads on demand (cached in `~/.cache/blender_mcp/3d-collection`) and imports.

Without the Blender MCP server, the same code is in `src/blender_mcp/library.py`
(`search`, `get`, `pick_file`, `download`). The Hugging Face MCP tools
(`hf_fs`, `hub_repo_details`) can also browse the dataset.
