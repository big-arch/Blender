#!/usr/bin/env python3
"""MCP server for Blender.

Exposes a running Blender instance (with the ``addon/blender_mcp_addon.py``
add-on enabled and its server started) as MCP tools: scene inspection, object
creation and editing, materials, rendering, viewport screenshots and an
arbitrary ``bpy`` code escape hatch.

Run with stdio transport (the default) — MCP clients launch it as a subprocess:

    python -m blender_mcp
"""

from __future__ import annotations

import base64
import json
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp.types import ToolAnnotations

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Image, MCPServer
except ImportError:  # pragma: no cover - mcp 1.x compatibility
    from mcp.server.fastmcp import FastMCP as MCPServer, Image  # type: ignore[no-redef]

from .connection import (
    BlenderCommandError,
    BlenderConnectionError,
    get_connection,
    send_command,
)

mcp = MCPServer(
    "blender_mcp",
    instructions=(
        "Tools for driving a running Blender instance. Start with "
        "blender_get_scene_info to see what exists before creating or editing "
        "objects. blender_execute_python is the escape hatch for anything the "
        "dedicated tools do not cover."
    ),
)

XYZ = Tuple[float, float, float]


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _call(command: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
    """Send a command, converting connection/command failures into tool errors.

    Returns either the result dict from Blender or an ``"Error: ..."`` string
    that the caller passes straight back to the model. Failures are returned as
    text rather than raised on purpose: the SDK replaces an exception with a
    generic "Error executing tool <name>", which would throw away the part that
    actually tells the caller what to fix.
    """
    try:
        return await send_command(command, params, timeout)
    except BlenderCommandError as exc:
        detail = f"Error: Blender rejected '{command}': {exc}"
        if exc.blender_traceback:
            detail += f"\n\nBlender traceback:\n{exc.blender_traceback}"
        return detail
    except BlenderConnectionError as exc:
        return f"Error: {exc}"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unset optional fields so Blender only applies what was asked for."""
    return {key: value for key, value in params.items() if value is not None}


def _image_result(payload: Dict[str, Any], summary: Dict[str, Any]) -> List[Any]:
    """Turn an encoded image from the add-on into MCP image + text content."""
    image = payload.get("image")
    content: List[Any] = [_dump(summary)]
    if image and image.get("data"):
        content.append(Image(data=base64.b64decode(image["data"]), format=image.get("format", "png")))
    return content


def _markdown_object(obj: Dict[str, Any]) -> List[str]:
    lines = [f"### {obj['name']} ({obj['type']})"]
    lines.append(f"- location: {obj['location']}")
    if obj.get("dimensions"):
        lines.append(f"- dimensions: {obj['dimensions']}")
    if obj.get("materials"):
        lines.append(f"- materials: {', '.join(obj['materials'])}")
    if obj.get("parent"):
        lines.append(f"- parent: {obj['parent']}")
    if not obj.get("visible", True):
        lines.append("- hidden in viewport")
    return lines


# --------------------------------------------------------------------------
# input models
# --------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")


class PingInput(StrictModel):
    """No parameters — checks that Blender is reachable."""


class SceneInfoInput(StrictModel):
    """Input model for scene inspection."""

    limit: int = Field(default=50, description="Maximum objects to return", ge=1, le=500)
    offset: int = Field(default=0, description="Objects to skip, for pagination", ge=0)
    type_filter: Optional[str] = Field(
        default=None,
        description="Only list objects of this Blender type (e.g. 'MESH', 'LIGHT', 'CAMERA', 'EMPTY')",
    )
    name_contains: Optional[str] = Field(
        default=None, description="Only list objects whose name contains this text (case-insensitive)"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' for a readable summary, 'json' for full data"
    )


class ObjectInfoInput(StrictModel):
    """Input model for reading one object."""

    name: str = Field(..., description="Exact object name, e.g. 'Cube'", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' for a readable summary, 'json' for full data"
    )


class CreateObjectInput(StrictModel):
    """Input model for creating an object."""

    type: str = Field(
        default="CUBE",
        description=(
            "What to create: CUBE, PLANE, SPHERE, ICO_SPHERE, CYLINDER, CONE, TORUS, "
            "CIRCLE, MONKEY, EMPTY, CAMERA, LIGHT or TEXT"
        ),
    )
    name: Optional[str] = Field(default=None, description="Name for the new object (e.g. 'Table_top')")
    location: Optional[XYZ] = Field(default=None, description="World location [x, y, z], default [0, 0, 0]")
    rotation: Optional[XYZ] = Field(default=None, description="Euler rotation in radians [x, y, z]")
    scale: Optional[XYZ] = Field(default=None, description="Scale factors [x, y, z], default [1, 1, 1]")
    size: Optional[float] = Field(default=None, description="Edge size for CUBE/PLANE/MONKEY (default 2.0)", gt=0)
    radius: Optional[float] = Field(
        default=None, description="Radius for spheres/cylinder/cone/torus/circle (default 1.0)", gt=0
    )
    depth: Optional[float] = Field(default=None, description="Height of CYLINDER/CONE (default 2.0)", gt=0)
    segments: Optional[int] = Field(
        default=None, description="Radial segments for round primitives (default 32)", ge=3, le=512
    )
    light_type: Optional[str] = Field(
        default=None, description="For type='LIGHT': POINT, SUN, SPOT or AREA (default POINT)"
    )
    energy: Optional[float] = Field(default=None, description="For type='LIGHT': light power in watts", ge=0)
    text: Optional[str] = Field(default=None, description="For type='TEXT': the string to display")
    collection: Optional[str] = Field(
        default=None, description="Link the object into this collection, creating it if needed"
    )

    @field_validator("type", "light_type")
    @classmethod
    def upper(cls, value: Optional[str]) -> Optional[str]:
        return value.upper() if value else value


class ModifyObjectInput(StrictModel):
    """Input model for editing an existing object. Only the fields you set change."""

    name: str = Field(..., description="Exact name of the object to modify", min_length=1)
    new_name: Optional[str] = Field(default=None, description="Rename the object to this")
    location: Optional[XYZ] = Field(default=None, description="New world location [x, y, z]")
    rotation: Optional[XYZ] = Field(default=None, description="New euler rotation in radians [x, y, z]")
    scale: Optional[XYZ] = Field(default=None, description="New scale [x, y, z]")
    visible: Optional[bool] = Field(default=None, description="Show (true) or hide (false) in viewport and render")
    parent: Optional[str] = Field(
        default=None, description="Name of the new parent object, or '' to clear the parent"
    )


class DeleteObjectInput(StrictModel):
    """Input model for deleting an object."""

    name: str = Field(..., description="Exact name of the object to delete", min_length=1)


class SetMaterialInput(StrictModel):
    """Input model for creating/assigning a Principled BSDF material."""

    object_name: str = Field(..., description="Object that receives the material", min_length=1)
    material_name: Optional[str] = Field(
        default=None,
        description="Material to create or reuse. Defaults to '<object_name>_material'",
    )
    color: Optional[List[float]] = Field(
        default=None,
        description="Base colour as RGB or RGBA floats in 0..1, e.g. [0.8, 0.1, 0.1] for red",
        min_length=3,
        max_length=4,
    )
    metallic: Optional[float] = Field(default=None, description="Metallic, 0..1", ge=0, le=1)
    roughness: Optional[float] = Field(default=None, description="Roughness, 0..1", ge=0, le=1)
    alpha: Optional[float] = Field(default=None, description="Opacity, 0 (transparent) .. 1 (opaque)", ge=0, le=1)
    emission_color: Optional[List[float]] = Field(
        default=None, description="Emission colour as RGB or RGBA floats in 0..1", min_length=3, max_length=4
    )
    emission_strength: Optional[float] = Field(default=None, description="Emission strength (0 = off)", ge=0)


class ExecutePythonInput(StrictModel):
    """Input model for running bpy code inside Blender."""

    code: str = Field(
        ...,
        description=(
            "Python source executed inside Blender. 'bpy' and 'mathutils' are in scope. "
            "Assign to a variable named 'result' to return a value; printed output is captured."
        ),
        min_length=1,
    )
    timeout: Optional[float] = Field(
        default=None, description="Seconds to wait for the code to finish (default 60)", gt=0, le=3600
    )


class RenderImageInput(StrictModel):
    """Input model for rendering through the active camera."""

    output_path: Optional[str] = Field(
        default=None,
        description="Where to write the PNG inside Blender's filesystem. Defaults to a temp file.",
    )
    resolution_x: Optional[int] = Field(default=None, description="Render width in pixels", ge=1, le=16384)
    resolution_y: Optional[int] = Field(default=None, description="Render height in pixels", ge=1, le=16384)
    engine: Optional[str] = Field(
        default=None, description="Render engine: BLENDER_EEVEE_NEXT, CYCLES or BLENDER_WORKBENCH"
    )
    samples: Optional[int] = Field(default=None, description="Render samples (quality vs speed)", ge=1, le=16384)
    return_image: bool = Field(default=True, description="Also return the render as an image the model can see")
    max_size: int = Field(default=1024, description="Longest side of the returned preview in pixels", ge=64, le=2048)
    timeout: Optional[float] = Field(
        default=300, description="Seconds to wait for the render (default 300)", gt=0, le=3600
    )

    @field_validator("engine")
    @classmethod
    def upper_engine(cls, value: Optional[str]) -> Optional[str]:
        return value.upper() if value else value


class ViewportScreenshotInput(StrictModel):
    """Input model for grabbing the 3D viewport."""

    max_size: int = Field(default=800, description="Longest side of the returned image in pixels", ge=64, le=2048)
    timeout: Optional[float] = Field(default=120, description="Seconds to wait (default 120)", gt=0, le=3600)


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


@mcp.tool(
    name="blender_ping",
    annotations=ToolAnnotations(
        title="Check Blender Connection",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def blender_ping(params: PingInput) -> str:
    """Check that the Blender add-on is reachable and report its version.

    Use this first when any other Blender tool fails, to tell "Blender is not
    running" apart from "the command itself was wrong".

    Args:
        params (PingInput): No fields.

    Returns:
        str: JSON with the schema:
        {
            "connected": bool,          # True when Blender answered
            "address": str,             # e.g. "127.0.0.1:9876"
            "blender_version": str,     # e.g. "4.2.1"
            "file": str | null,         # path of the open .blend file
            "python_execution_allowed": bool
        }
        On failure: "Error: <what to fix>".

    Examples:
        - Use when: a tool returned a connection error and you want to confirm the cause.
        - Don't use when: you already know Blender is connected — go straight to the task.
    """
    result = await _call("ping", {}, timeout=10)
    if isinstance(result, str):
        return result
    return _dump({"connected": True, "address": get_connection().address, **result})


@mcp.tool(
    name="blender_get_scene_info",
    annotations=ToolAnnotations(
        title="Get Blender Scene Info",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def blender_get_scene_info(params: SceneInfoInput) -> str:
    """List the current Blender scene: render settings and its objects.

    This is the starting point for almost every task — it gives the exact
    object names the other tools need. Results are paginated and can be
    filtered by object type or name.

    Args:
        params (SceneInfoInput): Validated input containing:
            - limit (int): Max objects per page, 1..500 (default 50)
            - offset (int): Objects to skip (default 0)
            - type_filter (Optional[str]): e.g. "MESH", "LIGHT", "CAMERA"
            - name_contains (Optional[str]): case-insensitive name filter
            - response_format (ResponseFormat): "markdown" (default) or "json"

    Returns:
        str: Markdown summary, or JSON with the schema:
        {
            "scene": str, "engine": str, "frame_current": int,
            "frame_start": int, "frame_end": int,
            "resolution": [int, int], "active_camera": str | null,
            "unit_system": str, "collections": [str], "material_count": int,
            "total": int, "count": int, "offset": int,
            "has_more": bool, "next_offset": int | null,
            "objects": [
                {"name": str, "type": str, "location": [float, float, float],
                 "dimensions": [float, float, float], "visible": bool,
                 "materials": [str], "parent": str}    # parent only when set
            ]
        }
        On failure: "Error: <what to fix>".

    Examples:
        - Use when: "what's in the scene?" -> no arguments.
        - Use when: "list only the lights" -> type_filter="LIGHT".
        - Don't use when: you need mesh statistics or modifiers for one object
          (use blender_get_object_info).
    """
    payload = _clean(
        {
            "limit": params.limit,
            "offset": params.offset,
            "type_filter": params.type_filter,
            "name_contains": params.name_contains,
        }
    )
    result = await _call("get_scene_info", payload)
    if isinstance(result, str):
        return result
    if params.response_format is ResponseFormat.JSON:
        return _dump(result)

    lines = [
        f"# Scene '{result['scene']}'",
        "",
        f"- engine: {result['engine']}",
        f"- frame: {result['frame_current']} (range {result['frame_start']}–{result['frame_end']})",
        f"- resolution: {result['resolution'][0]}x{result['resolution'][1]}",
        f"- active camera: {result['active_camera'] or 'none'}",
        f"- collections: {', '.join(result['collections']) or 'none'}",
        f"- materials in file: {result['material_count']}",
        "",
        f"## Objects ({result['count']} of {result['total']}, offset {result['offset']})",
        "",
    ]
    if not result["objects"]:
        lines.append("_No objects match._")
    for obj in result["objects"]:
        lines.extend(_markdown_object(obj))
        lines.append("")
    if result["has_more"]:
        lines.append(f"_More objects available — call again with offset={result['next_offset']}._")
    return "\n".join(lines)


@mcp.tool(
    name="blender_get_object_info",
    annotations=ToolAnnotations(
        title="Get Blender Object Info",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def blender_get_object_info(params: ObjectInfoInput) -> str:
    """Read every detail of one object: transform, mesh stats, modifiers, materials.

    Args:
        params (ObjectInfoInput): Validated input containing:
            - name (str): Exact object name, e.g. "Cube"
            - response_format (ResponseFormat): "markdown" (default) or "json"

    Returns:
        str: Markdown summary, or JSON with the schema:
        {
            "name": str, "type": str,
            "location": [float x3], "rotation_euler": [float x3],
            "rotation_mode": str, "scale": [float x3], "dimensions": [float x3],
            "visible": bool, "hide_render": bool,
            "materials": [str], "collections": [str], "children": [str],
            "parent": str,                                  # only when set
            "modifiers": [{"name": str, "type": str}],
            "custom_properties": {str: str},
            "world_bound_box": [[float x3] x8],
            "mesh": {"vertices": int, "edges": int, "polygons": int,
                     "uv_layers": [str], "shape_keys": bool},   # MESH only
            "light": {"light_type": str, "energy": float, "color": [float x3]},  # LIGHT only
            "camera": {"lens": float, "camera_type": str, "clip_start": float,
                       "clip_end": float, "is_scene_camera": bool}               # CAMERA only
        }
        On failure: "Error: object '<name>' not found. Existing objects: ...".

    Examples:
        - Use when: "how many polygons does Suzanne have?" -> name="Suzanne".
        - Don't use when: you don't know the object's exact name yet
          (use blender_get_scene_info first).
    """
    result = await _call("get_object_info", {"name": params.name})
    if isinstance(result, str):
        return result
    if params.response_format is ResponseFormat.JSON:
        return _dump(result)

    lines = _markdown_object(result)
    lines.insert(1, f"- rotation (rad): {result['rotation_euler']}")
    lines.insert(2, f"- scale: {result['scale']}")
    if result.get("mesh"):
        mesh = result["mesh"]
        lines.append(
            f"- mesh: {mesh['vertices']} verts / {mesh['edges']} edges / {mesh['polygons']} faces"
        )
    if result.get("light"):
        light = result["light"]
        lines.append(f"- light: {light['light_type']}, {light['energy']} W, colour {light['color']}")
    if result.get("camera"):
        camera = result["camera"]
        lines.append(
            f"- camera: {camera['lens']}mm, active scene camera: {camera['is_scene_camera']}"
        )
    if result.get("modifiers"):
        lines.append(
            "- modifiers: " + ", ".join(f"{m['name']} ({m['type']})" for m in result["modifiers"])
        )
    if result.get("children"):
        lines.append(f"- children: {', '.join(result['children'])}")
    lines.append(f"- collections: {', '.join(result['collections'])}")
    return "\n".join(lines)


@mcp.tool(
    name="blender_create_object",
    annotations=ToolAnnotations(
        title="Create Blender Object",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def blender_create_object(params: CreateObjectInput) -> str:
    """Add a primitive, camera, light, empty or text object to the scene.

    Repeated calls add repeated objects — Blender appends ".001", ".002" to
    duplicate names, so check the returned name before referring to it.

    Args:
        params (CreateObjectInput): Validated input containing:
            - type (str): CUBE, PLANE, SPHERE, ICO_SPHERE, CYLINDER, CONE, TORUS,
              CIRCLE, MONKEY, EMPTY, CAMERA, LIGHT, TEXT (default "CUBE")
            - name (Optional[str]): name for the new object
            - location/rotation/scale (Optional[[float, float, float]]): transform
            - size, radius, depth, segments (Optional): primitive dimensions
            - light_type (Optional[str]), energy (Optional[float]): for lights
            - text (Optional[str]): body for TEXT objects
            - collection (Optional[str]): collection to link into (created if missing)

    Returns:
        str: JSON {"created": {...}} using the same object schema as
        blender_get_object_info. On failure: "Error: <what to fix>".

    Examples:
        - Use when: "add a red-ish sphere at z=2" -> type="SPHERE", location=[0,0,2],
          then blender_set_material for the colour.
        - Use when: "the scene needs a camera" -> type="CAMERA" (it becomes the
          active scene camera automatically).
        - Don't use when: you need a non-primitive shape or a modifier stack
          (use blender_execute_python).
    """
    payload = _clean(params.model_dump())
    result = await _call("create_object", payload)
    return result if isinstance(result, str) else _dump(result)


@mcp.tool(
    name="blender_modify_object",
    annotations=ToolAnnotations(
        title="Modify Blender Object",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def blender_modify_object(params: ModifyObjectInput) -> str:
    """Move, rotate, scale, rename, re-parent, show or hide an existing object.

    Only the fields you pass are changed; everything else keeps its value.
    Transforms are absolute, not relative — read the current values with
    blender_get_object_info first if you need an offset.

    Args:
        params (ModifyObjectInput): Validated input containing:
            - name (str): object to modify
            - new_name (Optional[str]): rename to this
            - location/rotation/scale (Optional[[float, float, float]]): absolute values
            - visible (Optional[bool]): show or hide in viewport and render
            - parent (Optional[str]): new parent's name, or "" to unparent

    Returns:
        str: JSON {"modified": {...}} using the same object schema as
        blender_get_object_info. On failure: "Error: object '<name>' not found...".

    Examples:
        - Use when: "move the cube 3 units up" -> read its location, then
          name="Cube", location=[x, y, z+3].
        - Don't use when: the object does not exist yet (use blender_create_object).
    """
    payload = _clean(params.model_dump())
    result = await _call("modify_object", payload)
    return result if isinstance(result, str) else _dump(result)


@mcp.tool(
    name="blender_delete_object",
    annotations=ToolAnnotations(
        title="Delete Blender Object",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def blender_delete_object(params: DeleteObjectInput) -> str:
    """Permanently remove one object from the .blend file.

    This cannot be undone from the MCP side — the user would have to press
    Ctrl+Z in Blender. Confirm the exact name before calling.

    Args:
        params (DeleteObjectInput): Validated input containing:
            - name (str): exact name of the object to delete

    Returns:
        str: JSON {"deleted": str, "remaining_objects": int}.
        On failure: "Error: object '<name>' not found. Existing objects: ...".

    Examples:
        - Use when: "remove the default cube" -> name="Cube".
        - Don't use when: the object should only be hidden
          (use blender_modify_object with visible=false).
    """
    result = await _call("delete_object", {"name": params.name})
    return result if isinstance(result, str) else _dump(result)


@mcp.tool(
    name="blender_set_material",
    annotations=ToolAnnotations(
        title="Set Blender Material",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def blender_set_material(params: SetMaterialInput) -> str:
    """Create or reuse a Principled BSDF material and assign it to an object.

    An existing material with the same name is reused and updated, so calling
    this twice with the same material_name edits one material instead of
    creating two.

    Args:
        params (SetMaterialInput): Validated input containing:
            - object_name (str): object that receives the material
            - material_name (Optional[str]): defaults to "<object_name>_material"
            - color (Optional[List[float]]): RGB or RGBA in 0..1
            - metallic, roughness, alpha (Optional[float]): 0..1
            - emission_color (Optional[List[float]]), emission_strength (Optional[float])

    Returns:
        str: JSON with the schema:
        {
            "object": str, "material": str, "created_material": bool,
            "slot_index": int, "applied": {str: value},
            "principled_node_found": bool
        }
        On failure: "Error: <what to fix>".

    Examples:
        - Use when: "make the cube red" -> object_name="Cube", color=[0.8, 0.05, 0.05].
        - Use when: "make it glow" -> emission_color=[1,0.6,0.2], emission_strength=5.
        - Don't use when: you need textures or a custom node graph
          (use blender_execute_python).
    """
    payload = _clean(params.model_dump())
    result = await _call("set_material", payload)
    return result if isinstance(result, str) else _dump(result)


@mcp.tool(
    name="blender_execute_python",
    annotations=ToolAnnotations(
        title="Execute Python in Blender",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def blender_execute_python(params: ExecutePythonInput) -> str:
    """Run arbitrary Python inside Blender with full access to the bpy API.

    The escape hatch for anything the dedicated tools do not cover: modifiers,
    node graphs, animation, imports, exports, mesh editing. The code runs in
    the user's live Blender session and can change or destroy their work, so
    keep snippets small and prefer the dedicated tools when they fit. The user
    can switch this off in the add-on's sidebar panel.

    Args:
        params (ExecutePythonInput): Validated input containing:
            - code (str): source to execute; 'bpy' and 'mathutils' are in scope.
              Assign to a variable named 'result' to return a value.
            - timeout (Optional[float]): seconds to wait (default 60)

    Returns:
        str: JSON with the schema:
        {
            "stdout": str,     # captured print() output
            "stderr": str,     # captured error output
            "result": str | int | float | bool | list | dict | null
        }
        On failure: "Error: Blender rejected 'execute_python': <exception>" plus
        the Blender traceback, or a PermissionError message when the user has
        disabled Python execution in the add-on panel.

    Examples:
        - Use when: "add a subdivision modifier to Cube" ->
          code="obj = bpy.data.objects['Cube']; obj.modifiers.new('Subdiv', 'SUBSURF')".
        - Use when: "how many materials use nodes?" ->
          code="result = sum(1 for m in bpy.data.materials if m.use_nodes)".
        - Don't use when: a dedicated tool does the same job — they validate
          inputs and return structured data.
    """
    result = await _call("execute_python", {"code": params.code}, timeout=params.timeout)
    return result if isinstance(result, str) else _dump(result)


@mcp.tool(
    name="blender_render_image",
    annotations=ToolAnnotations(
        title="Render Blender Image",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def blender_render_image(params: RenderImageInput) -> Any:
    """Render the scene through its active camera and return the image.

    Rendering writes a PNG on the machine running Blender and, by default,
    returns a downscaled copy so the result can actually be looked at. Cycles
    renders at high sample counts can take minutes — raise ``timeout`` for
    those, or drop ``samples`` for a quick preview.

    Args:
        params (RenderImageInput): Validated input containing:
            - output_path (Optional[str]): PNG destination (default: temp file)
            - resolution_x, resolution_y (Optional[int]): pixel size for this render
            - engine (Optional[str]): BLENDER_EEVEE_NEXT, CYCLES, BLENDER_WORKBENCH
            - samples (Optional[int]): render samples
            - return_image (bool): return the picture as well (default True)
            - max_size (int): longest side of the returned preview (default 1024)
            - timeout (Optional[float]): seconds to wait (default 300)

    Returns:
        Any: A list of [JSON summary, image] when return_image is True, else the
        JSON summary alone. The summary schema is:
        {
            "output_path": str,      # PNG path on Blender's machine
            "resolution": [int, int],
            "engine": str,
            "camera": str
        }
        On failure: "Error: the scene has no active camera..." or another
        actionable message.

    Examples:
        - Use when: "show me what this looks like" -> no arguments.
        - Use when: "render it in 4K with Cycles" -> resolution_x=3840,
          resolution_y=2160, engine="CYCLES", timeout=1800.
        - Don't use when: you want the working view rather than the camera
          (use blender_get_viewport_screenshot).
    """
    payload = _clean(
        {
            "output_path": params.output_path,
            "resolution_x": params.resolution_x,
            "resolution_y": params.resolution_y,
            "engine": params.engine,
            "samples": params.samples,
            "return_image": params.return_image,
            "max_size": params.max_size,
        }
    )
    result = await _call("render_image", payload, timeout=params.timeout)
    if isinstance(result, str):
        return result
    summary = {key: value for key, value in result.items() if key != "image"}
    if not params.return_image:
        return _dump(summary)
    return _image_result(result, summary)


@mcp.tool(
    name="blender_get_viewport_screenshot",
    annotations=ToolAnnotations(
        title="Screenshot Blender Viewport",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def blender_get_viewport_screenshot(params: ViewportScreenshotInput) -> Any:
    """Capture the 3D viewport as the user currently sees it.

    Faster than a render and useful for checking placement, proportions and
    what the user is actually looking at. Requires a Blender window with an
    open 3D viewport (it fails in headless/background Blender).

    Args:
        params (ViewportScreenshotInput): Validated input containing:
            - max_size (int): longest side of the returned image (default 800)
            - timeout (Optional[float]): seconds to wait (default 120)

    Returns:
        Any: A list of [JSON summary, image]. The summary schema is
        {"output_path": str}. On failure: "Error: no 3D viewport is open in
        Blender (running headless?)..." or another actionable message.

    Examples:
        - Use when: "does this look right?" after moving objects around.
        - Don't use when: you need the final camera framing with materials and
          lighting (use blender_render_image).
    """
    result = await _call("viewport_screenshot", {"max_size": params.max_size}, timeout=params.timeout)
    if isinstance(result, str):
        return result
    summary = {key: value for key, value in result.items() if key != "image"}
    return _image_result(result, summary)


def main() -> None:
    """Entry point for the ``blender-mcp`` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
