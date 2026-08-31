"""Add-on tests driven against a real ``bpy``.

Skipped automatically when the ``bpy`` package is not installed; the protocol
tests in ``test_connection.py`` still cover the client side there.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from blender_mcp.connection import BlenderConnection
from tests.conftest import free_port


def ok(addon, command: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = addon.dispatch({"type": command, "params": params or {}})
    assert response["status"] == "success", response
    return response["result"]


def err(addon, command: str, params: Dict[str, Any] | None = None) -> str:
    response = addon.dispatch({"type": command, "params": params or {}})
    assert response["status"] == "error", response
    return response["message"]


def test_ping_reports_blender_version(addon):
    result = ok(addon, "ping")
    assert result["pong"] is True
    assert result["blender_version"]
    assert result["python_execution_allowed"] is True


def test_scene_info_lists_objects_with_pagination(addon):
    everything = ok(addon, "get_scene_info", {"limit": 500})
    names = {obj["name"] for obj in everything["objects"]}
    assert {"Camera", "Cube", "Light"} <= names
    assert everything["has_more"] is False

    page = ok(addon, "get_scene_info", {"limit": 1})
    assert page["count"] == 1
    assert page["has_more"] is True
    assert page["next_offset"] == 1

    lights = ok(addon, "get_scene_info", {"type_filter": "LIGHT"})
    assert {obj["type"] for obj in lights["objects"]} == {"LIGHT"}

    named = ok(addon, "get_scene_info", {"name_contains": "cub"})
    assert "Cube" in {obj["name"] for obj in named["objects"]}


def test_object_info_includes_mesh_statistics(addon):
    result = ok(addon, "get_object_info", {"name": "Cube"})
    assert result["type"] == "MESH"
    assert result["mesh"]["vertices"] == 8
    assert result["mesh"]["polygons"] == 6
    assert len(result["world_bound_box"]) == 8


def test_missing_object_error_lists_candidates(addon):
    message = err(addon, "get_object_info", {"name": "DoesNotExist"})
    assert "not found" in message
    assert "Cube" in message


def test_create_modify_delete_cycle(addon):
    created = ok(
        addon,
        "create_object",
        {"type": "SPHERE", "name": "MCP_Ball", "location": [1, 2, 3], "radius": 0.5},
    )["created"]
    assert created["name"] == "MCP_Ball"
    assert created["location"] == [1.0, 2.0, 3.0]

    modified = ok(
        addon,
        "modify_object",
        {"name": "MCP_Ball", "location": [0, 0, 5], "scale": [2, 2, 2], "new_name": "MCP_Ball2"},
    )["modified"]
    assert modified["name"] == "MCP_Ball2"
    assert modified["location"] == [0.0, 0.0, 5.0]
    # The evaluated data must be refreshed: dimensions and the bounding box
    # would still describe the old transform without a depsgraph update.
    assert modified["dimensions"] == pytest.approx([2.0, 2.0, 2.0], abs=1e-3)
    assert modified["world_bound_box"][0] == pytest.approx([-1.0, -1.0, 4.0], abs=1e-3)

    deleted = ok(addon, "delete_object", {"name": "MCP_Ball2"})
    assert deleted["deleted"] == "MCP_Ball2"
    assert "not found" in err(addon, "delete_object", {"name": "MCP_Ball2"})


def test_create_object_reports_supported_types(addon):
    message = err(addon, "create_object", {"type": "BANANA"})
    assert "unsupported object type 'BANANA'" in message
    assert "MONKEY" in message


def test_create_light_and_collection(addon):
    created = ok(
        addon,
        "create_object",
        {"type": "LIGHT", "light_type": "SUN", "energy": 7, "name": "MCP_Sun", "collection": "MCP_Test"},
    )["created"]
    try:
        assert created["light"] == {"light_type": "SUN", "energy": 7.0, "color": [1.0, 1.0, 1.0]}
        assert created["collections"] == ["MCP_Test"]
    finally:
        ok(addon, "delete_object", {"name": "MCP_Sun"})


def test_set_material_creates_then_reuses(addon):
    ok(addon, "create_object", {"type": "CUBE", "name": "MCP_Painted"})
    try:
        first = ok(
            addon,
            "set_material",
            {
                "object_name": "MCP_Painted",
                "material_name": "MCP_Red",
                "color": [0.8, 0.1, 0.1],
                "metallic": 0.5,
                "roughness": 0.2,
            },
        )
        assert first["created_material"] is True
        assert first["principled_node_found"] is True
        assert first["applied"]["color"] == [0.8, 0.1, 0.1, 1.0]

        second = ok(
            addon,
            "set_material",
            {"object_name": "MCP_Painted", "material_name": "MCP_Red", "roughness": 0.9},
        )
        assert second["created_material"] is False
        assert second["slot_index"] == first["slot_index"]

        info = ok(addon, "get_object_info", {"name": "MCP_Painted"})
        assert info["materials"] == ["MCP_Red"]
    finally:
        ok(addon, "delete_object", {"name": "MCP_Painted"})


def test_set_material_emission_uses_version_specific_socket(addon):
    ok(addon, "create_object", {"type": "CUBE", "name": "MCP_Glowing"})
    try:
        result = ok(
            addon,
            "set_material",
            {
                "object_name": "MCP_Glowing",
                "material_name": "MCP_Glow",
                "emission_color": [1, 0.5, 0],
                "emission_strength": 4,
                "alpha": 0.9,
            },
        )
        # "Emission" was renamed to "Emission Color" in Blender 4.0 — one of the
        # two spellings has to match on every supported version.
        assert result["applied"]["emission_color"] == [1, 0.5, 0, 1.0]
        assert result["applied"]["emission_strength"] == 4.0
        assert result["applied"]["alpha"] == 0.9
    finally:
        ok(addon, "delete_object", {"name": "MCP_Glowing"})


def test_execute_python_captures_output_and_result(addon):
    result = ok(addon, "execute_python", {"code": "print('hello'); result = 6 * 7"})
    assert result["stdout"] == "hello\n"
    assert result["result"] == 42


def test_execute_python_reports_exceptions(addon):
    message = err(addon, "execute_python", {"code": "raise ValueError('boom')"})
    assert "ValueError: boom" in message


def test_execute_python_can_be_disabled(addon):
    import bpy

    bpy.context.scene.blendermcp_allow_python = False
    try:
        message = err(addon, "execute_python", {"code": "result = 1"})
        assert "Python execution is disabled" in message
        # Everything else keeps working while the switch is off.
        assert ok(addon, "ping")["python_execution_allowed"] is False
    finally:
        bpy.context.scene.blendermcp_allow_python = True


def test_unknown_command_lists_known_commands(addon):
    message = err(addon, "teleport")
    assert "unknown command 'teleport'" in message
    assert "get_scene_info" in message


def test_viewport_screenshot_without_a_window_points_at_rendering(addon):
    message = err(addon, "viewport_screenshot")
    assert "blender_render_image" in message


@pytest.mark.slow
def test_render_image_reports_the_settings_it_used(addon):
    result = ok(
        addon,
        "render_image",
        {
            "engine": "CYCLES",
            "samples": 1,
            "resolution_x": 32,
            "resolution_y": 24,
            "max_size": 64,
        },
    )
    # Render settings are restored afterwards, so the response must describe
    # the render that happened, not the scene's defaults.
    assert result["engine"] == "CYCLES"
    assert result["resolution"] == [32, 24]
    assert result["camera"] == "Camera"
    assert result["image"]["format"] == "png"
    assert result["image"]["bytes"] > 0

    import bpy

    assert bpy.context.scene.render.resolution_x != 32


def test_socket_server_end_to_end(addon):
    """Real client, real socket, real handlers — only the event loop is faked."""
    port = free_port()
    server = addon.BlenderMCPServer(port=port)
    server.start()
    client = BlenderConnection(port=port, timeout=15)
    results: List[Any] = []
    failure: List[BaseException] = []

    def worker() -> None:
        try:
            results.append(client.execute("ping"))
            results.append(client.execute("get_scene_info", {"limit": 2}))
            results.append(client.execute("execute_python", {"code": "result = 'y' * 300000"}))
            try:
                client.execute("get_object_info", {"name": "Ghost"})
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
                results.append(exc)
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    # Blender would call _tick from bpy.app.timers on its main thread; headless
    # bpy has no event loop, so the test drives it instead.
    deadline = time.monotonic() + 30
    while thread.is_alive() and time.monotonic() < deadline:
        server._tick()
        time.sleep(0.005)
    thread.join(timeout=5)

    client.close()
    server.stop()

    assert not failure, failure
    assert results[0]["pong"] is True
    assert results[1]["count"] == 2
    assert results[2]["result"] == "y" * 300000
    assert "not found" in str(results[3])


def test_register_and_unregister_survive_a_second_copy(addon):
    """Installing the legacy file and the extension must not break enabling."""
    import bpy

    # A second copy taking over the same class names, then both being disabled,
    # used to raise out of unregister() and spam Blender's console.
    addon.register()
    assert hasattr(bpy.types, "BLENDERMCP_PT_panel")

    addon.unregister()
    addon.unregister()
    assert not hasattr(bpy.types, "BLENDERMCP_PT_panel")
    assert not hasattr(bpy.context.scene, "blendermcp_port")

    addon.register()
    assert hasattr(bpy.context.scene, "blendermcp_port")
