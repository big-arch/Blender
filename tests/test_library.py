"""Scenario 3D Collection helpers, with the network stubbed out."""

from __future__ import annotations

import io

import pytest

from blender_mcp import library, server

ROW = {
    "id": "scenario:asset_ABC",
    "prompt": "a red dragon",
    "workflow": "text-to-3d",
    "model": "m",
    "outputs": [
        {"format": "glb", "path": "assets/scenario_asset_ABC/output.glb"},
        {"format": "fbx", "path": "assets/scenario_asset_ABC/output.fbx"},
    ],
    "preview_file": {"path": "assets/scenario_asset_ABC/preview.jpg"},
}


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake_get_json(url, timeout=30):
        calls.append(url)
        return {"rows": [{"row": ROW}]}

    monkeypatch.setattr(library, "_get_json", fake_get_json)
    return calls


def test_search_uses_search_endpoint_and_summarizes(api):
    rows = library.search("dragon", limit=3)
    assert "/search?" in api[0] and "query=dragon" in api[0] and "length=3" in api[0]
    assert rows[0]["formats"] == ["fbx", "glb"]
    assert rows[0]["preview"].endswith("assets/scenario_asset_ABC/preview.jpg")


def test_browse_without_query_uses_rows_endpoint(api):
    library.search()
    assert "/rows?" in api[0]


def test_get_matches_bare_or_prefixed_id(api):
    assert library.get("asset_ABC")["id"] == "scenario:asset_ABC"
    with pytest.raises(LookupError):
        library.get("asset_NOPE")


def test_pick_file_prefers_importable_formats():
    files = ["a/output.vox", "a/output.fbx", "a/output.glb"]
    assert library.pick_file(files) == "a/output.glb"
    assert library.pick_file(files, "fbx") == "a/output.fbx"
    with pytest.raises(LookupError):
        library.pick_file(["a/output.vox"])


def test_download_caches(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_MCP_ASSET_CACHE", str(tmp_path))
    hits = []

    def fake_urlopen(request, timeout=0):
        hits.append(request.full_url)
        return io.BytesIO(b"glTF")

    monkeypatch.setattr(library.urllib.request, "urlopen", fake_urlopen)
    first = library.download("assets/x/output.glb")
    second = library.download("assets/x/output.glb")
    assert first == second == tmp_path / "assets/x/output.glb"
    assert first.read_bytes() == b"glTF" and len(hits) == 1


def test_import_code_is_valid_python():
    code = server._IMPORT_CODE.format(path="/tmp/a.glb", fmt="glb", name="Dragon", location=(1.0, 2.0, 0.0))
    compile(code, "<import>", "exec")


async def test_download_only(api, monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_MCP_ASSET_CACHE", str(tmp_path))
    monkeypatch.setattr(library.urllib.request, "urlopen", lambda request, timeout=0: io.BytesIO(b"x"))
    out = await server.blender_import_library_asset(
        server.ImportLibraryAssetInput(asset_id="asset_ABC", import_into_blender=False)
    )
    assert "output.glb" in out and str(tmp_path) in out
