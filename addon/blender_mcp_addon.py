"""Blender MCP — command server add-on.

Opens a small JSON-over-TCP server inside Blender that the ``blender_mcp``
MCP server (see ``src/blender_mcp``) connects to. Every command is executed on
Blender's main thread from an ``bpy.app.timers`` callback, which is the only
safe way to touch ``bpy`` data.

Protocol: newline-delimited JSON.

    request  -> {"type": "get_scene_info", "params": {...}}\n
    response <- {"status": "success", "result": {...}}\n
                {"status": "error", "message": "...", "traceback": "..."}\n

Install: Edit > Preferences > Add-ons > Install..., pick this file, enable
"Interface: Blender MCP", then open the 3D viewport sidebar (N) > MCP tab and
press "Start server".
"""

import base64
import contextlib
import io
import json
import os
import socket
import tempfile
import traceback
from typing import Any, Callable, Dict, List, Optional

import bpy
import mathutils

bl_info = {
    "name": "Blender MCP",
    "author": "Blender MCP contributors",
    "version": (1, 0, 0),
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar (N) > MCP",
    "description": "JSON command server that exposes Blender to an MCP client",
    "category": "Interface",
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
TICK_INTERVAL = 0.05  # seconds between polls of the listening socket
RECV_CHUNK = 65536
SEND_TIMEOUT = 60.0
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _vec(value) -> List[float]:
    """Convert a Blender vector/euler/color to a plain list of floats."""
    return [round(float(component), 6) for component in value]


def _sync() -> None:
    """Flush pending dependency-graph updates.

    ``dimensions``, ``matrix_world`` and ``bound_box`` keep their previous
    values until the view layer is evaluated, so responses built right after a
    transform would report stale numbers without this.
    """
    bpy.context.view_layer.update()


def _as_tuple3(value, fallback=None):
    """Validate an optional XYZ triple coming from the client."""
    if value is None:
        return fallback
    if len(value) != 3:
        raise ValueError(f"expected 3 numbers, got {len(value)}")
    return tuple(float(component) for component in value)


def _object_summary(obj: bpy.types.Object) -> Dict[str, Any]:
    """Small, cheap description of an object — used in scene listings."""
    summary = {
        "name": obj.name,
        "type": obj.type,
        "location": _vec(obj.location),
        "dimensions": _vec(obj.dimensions),
        "visible": not obj.hide_viewport,
        "materials": [slot.material.name for slot in obj.material_slots if slot.material],
    }
    if obj.parent:
        summary["parent"] = obj.parent.name
    return summary


def _object_details(obj: bpy.types.Object) -> Dict[str, Any]:
    """Full description of a single object."""
    details = _object_summary(obj)
    details.update(
        {
            "rotation_euler": _vec(obj.rotation_euler),
            "rotation_mode": obj.rotation_mode,
            "scale": _vec(obj.scale),
            "hide_render": obj.hide_render,
            "collections": [collection.name for collection in obj.users_collection],
            "children": [child.name for child in obj.children],
            "modifiers": [
                {"name": modifier.name, "type": modifier.type} for modifier in obj.modifiers
            ],
            "custom_properties": {
                key: str(obj[key]) for key in obj.keys() if not key.startswith("_")
            },
            "world_bound_box": [_vec(obj.matrix_world @ mathutils.Vector(corner)) for corner in obj.bound_box],
        }
    )

    data = obj.data
    if obj.type == "MESH" and data is not None:
        details["mesh"] = {
            "vertices": len(data.vertices),
            "edges": len(data.edges),
            "polygons": len(data.polygons),
            "uv_layers": [layer.name for layer in data.uv_layers],
            "shape_keys": bool(data.shape_keys),
        }
    elif obj.type == "LIGHT" and data is not None:
        details["light"] = {
            "light_type": data.type,
            "energy": round(float(data.energy), 6),
            "color": _vec(data.color),
        }
    elif obj.type == "CAMERA" and data is not None:
        details["camera"] = {
            "lens": round(float(data.lens), 6),
            "camera_type": data.type,
            "clip_start": round(float(data.clip_start), 6),
            "clip_end": round(float(data.clip_end), 6),
            "is_scene_camera": bpy.context.scene.camera is obj,
        }
    return details


def _find_object(name: str) -> bpy.types.Object:
    obj = bpy.data.objects.get(name)
    if obj is None:
        available = ", ".join(sorted(o.name for o in bpy.data.objects)[:30]) or "<scene is empty>"
        raise KeyError(f"object '{name}' not found. Existing objects: {available}")
    return obj


def _ensure_nodes(material: bpy.types.Material) -> None:
    """Make sure the material has a node tree.

    Materials are always node-based from Blender 5.0 on, where ``use_nodes`` is
    deprecated — so only touch it when the node tree is genuinely missing.
    """
    if material.node_tree is None and hasattr(material, "use_nodes"):
        material.use_nodes = True


def _principled_node(material: bpy.types.Material):
    """Return the Principled BSDF node of a node-based material, if present."""
    if material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _set_input(node, names: List[str], value) -> bool:
    """Set the first input that exists under any of ``names``.

    Principled BSDF socket names moved around between Blender versions
    (``Emission`` became ``Emission Color`` in 4.0), so callers pass every
    spelling they know about.
    """
    for name in names:
        socket_input = node.inputs.get(name)
        if socket_input is not None:
            socket_input.default_value = value
            return True
    return False


def _downscale_png(path: str, max_size: int) -> str:
    """Scale a PNG down so its longest side is ``max_size``, in place.

    Uses Blender's own image API so the add-on stays dependency free.
    """
    image = bpy.data.images.load(path)
    try:
        width, height = image.size
        longest = max(width, height)
        if longest > max_size:
            factor = max_size / longest
            image.scale(max(1, int(width * factor)), max(1, int(height * factor)))
            image.file_format = "PNG"
            image.save_render(path)
        return path
    finally:
        bpy.data.images.remove(image)


def _encode_png(path: str) -> Dict[str, Any]:
    with open(path, "rb") as handle:
        data = handle.read()
    return {
        "format": "png",
        "bytes": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


# --------------------------------------------------------------------------
# command handlers — every one runs on Blender's main thread
# --------------------------------------------------------------------------


def cmd_ping(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pong": True,
        "blender_version": bpy.app.version_string,
        "file": bpy.data.filepath or None,
        "python_execution_allowed": bool(bpy.context.scene.blendermcp_allow_python),
    }


def cmd_get_scene_info(params: Dict[str, Any]) -> Dict[str, Any]:
    _sync()
    scene = bpy.context.scene
    offset = max(0, int(params.get("offset", 0)))
    limit = min(500, max(1, int(params.get("limit", 50))))
    type_filter = params.get("type_filter")
    name_contains = (params.get("name_contains") or "").lower()

    objects = list(scene.objects)
    if type_filter:
        objects = [obj for obj in objects if obj.type == type_filter]
    if name_contains:
        objects = [obj for obj in objects if name_contains in obj.name.lower()]

    page = objects[offset : offset + limit]
    has_more = len(objects) > offset + len(page)
    return {
        "scene": scene.name,
        "engine": scene.render.engine,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "active_camera": scene.camera.name if scene.camera else None,
        "unit_system": scene.unit_settings.system,
        "collections": [collection.name for collection in bpy.data.collections],
        "material_count": len(bpy.data.materials),
        "total": len(objects),
        "count": len(page),
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
        "objects": [_object_summary(obj) for obj in page],
    }


def cmd_get_object_info(params: Dict[str, Any]) -> Dict[str, Any]:
    obj = _find_object(params["name"])
    _sync()
    return _object_details(obj)


def cmd_create_object(params: Dict[str, Any]) -> Dict[str, Any]:
    object_type = str(params.get("type", "CUBE")).upper()
    location = _as_tuple3(params.get("location"), (0.0, 0.0, 0.0))
    rotation = _as_tuple3(params.get("rotation"), (0.0, 0.0, 0.0))
    scale = _as_tuple3(params.get("scale"))
    size = float(params.get("size", 2.0))
    radius = float(params.get("radius", 1.0))
    depth = float(params.get("depth", 2.0))
    segments = int(params.get("segments", 32))

    common = {"location": location, "rotation": rotation, "align": "WORLD"}

    if object_type == "CUBE":
        bpy.ops.mesh.primitive_cube_add(size=size, **common)
    elif object_type == "PLANE":
        bpy.ops.mesh.primitive_plane_add(size=size, **common)
    elif object_type in {"SPHERE", "UV_SPHERE"}:
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=radius, segments=segments, ring_count=max(3, segments // 2), **common
        )
    elif object_type == "ICO_SPHERE":
        bpy.ops.mesh.primitive_ico_sphere_add(
            radius=radius, subdivisions=int(params.get("subdivisions", 2)), **common
        )
    elif object_type == "CYLINDER":
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, vertices=segments, **common)
    elif object_type == "CONE":
        bpy.ops.mesh.primitive_cone_add(
            radius1=radius,
            radius2=float(params.get("radius2", 0.0)),
            depth=depth,
            vertices=segments,
            **common,
        )
    elif object_type == "TORUS":
        bpy.ops.mesh.primitive_torus_add(
            major_radius=radius,
            minor_radius=float(params.get("minor_radius", 0.25)),
            **common,
        )
    elif object_type == "CIRCLE":
        bpy.ops.mesh.primitive_circle_add(radius=radius, vertices=segments, **common)
    elif object_type == "MONKEY":
        bpy.ops.mesh.primitive_monkey_add(size=size, **common)
    elif object_type == "EMPTY":
        bpy.ops.object.empty_add(
            type=str(params.get("empty_type", "PLAIN_AXES")).upper(), radius=radius, **common
        )
    elif object_type == "CAMERA":
        bpy.ops.object.camera_add(**common)
    elif object_type == "LIGHT":
        bpy.ops.object.light_add(type=str(params.get("light_type", "POINT")).upper(), **common)
    elif object_type == "TEXT":
        bpy.ops.object.text_add(**common)
    else:
        raise ValueError(
            f"unsupported object type '{object_type}'. Supported: CUBE, PLANE, SPHERE, "
            "ICO_SPHERE, CYLINDER, CONE, TORUS, CIRCLE, MONKEY, EMPTY, CAMERA, LIGHT, TEXT"
        )

    obj = bpy.context.active_object
    if obj is None:  # pragma: no cover - Blender always sets this for the ops above
        raise RuntimeError("Blender did not report a newly created active object")

    if params.get("name"):
        obj.name = str(params["name"])
    if scale is not None:
        obj.scale = scale
    if object_type == "TEXT" and params.get("text"):
        obj.data.body = str(params["text"])
    if object_type == "LIGHT" and params.get("energy") is not None:
        obj.data.energy = float(params["energy"])
    if object_type == "CAMERA" and params.get("set_active_camera", True):
        bpy.context.scene.camera = obj

    collection_name = params.get("collection")
    if collection_name:
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(collection)
        for current in list(obj.users_collection):
            current.objects.unlink(obj)
        collection.objects.link(obj)

    _sync()
    return {"created": _object_details(obj)}


def cmd_modify_object(params: Dict[str, Any]) -> Dict[str, Any]:
    obj = _find_object(params["name"])

    location = _as_tuple3(params.get("location"))
    if location is not None:
        obj.location = location
    rotation = _as_tuple3(params.get("rotation"))
    if rotation is not None:
        obj.rotation_euler = rotation
    scale = _as_tuple3(params.get("scale"))
    if scale is not None:
        obj.scale = scale

    if params.get("visible") is not None:
        obj.hide_viewport = not bool(params["visible"])
        obj.hide_render = not bool(params["visible"])
    if params.get("parent") is not None:
        parent_name = params["parent"]
        obj.parent = _find_object(parent_name) if parent_name else None
    if params.get("new_name"):
        obj.name = str(params["new_name"])

    _sync()
    return {"modified": _object_details(obj)}


def cmd_delete_object(params: Dict[str, Any]) -> Dict[str, Any]:
    obj = _find_object(params["name"])
    name = obj.name
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name, "remaining_objects": len(bpy.data.objects)}


def cmd_set_material(params: Dict[str, Any]) -> Dict[str, Any]:
    obj = _find_object(params["object_name"])
    if not hasattr(obj.data, "materials"):
        raise TypeError(f"object '{obj.name}' of type {obj.type} cannot hold materials")

    material_name = params.get("material_name") or f"{obj.name}_material"
    material = bpy.data.materials.get(material_name)
    created = material is None
    if created:
        material = bpy.data.materials.new(name=material_name)
    _ensure_nodes(material)

    node = _principled_node(material)
    applied: Dict[str, Any] = {}
    if node is not None:
        color = params.get("color")
        if color is not None:
            rgba = list(color) + [1.0] * (4 - len(color))
            _set_input(node, ["Base Color"], tuple(rgba[:4]))
            material.diffuse_color = tuple(rgba[:4])
            applied["color"] = rgba[:4]
        for key, sockets in (
            ("metallic", ["Metallic"]),
            ("roughness", ["Roughness"]),
            ("alpha", ["Alpha"]),
            ("emission_strength", ["Emission Strength"]),
        ):
            if params.get(key) is not None:
                if _set_input(node, sockets, float(params[key])):
                    applied[key] = float(params[key])
        if params.get("emission_color") is not None:
            emission = list(params["emission_color"])
            emission = emission + [1.0] * (4 - len(emission))
            if _set_input(node, ["Emission Color", "Emission"], tuple(emission[:4])):
                applied["emission_color"] = emission[:4]

    slot_index = None
    for index, slot in enumerate(obj.material_slots):
        if slot.material is material:
            slot_index = index
            break
    if slot_index is None:
        obj.data.materials.append(material)
        slot_index = len(obj.data.materials) - 1
    obj.active_material_index = slot_index

    return {
        "object": obj.name,
        "material": material.name,
        "created_material": created,
        "slot_index": slot_index,
        "applied": applied,
        "principled_node_found": node is not None,
    }


def cmd_execute_python(params: Dict[str, Any]) -> Dict[str, Any]:
    if not bpy.context.scene.blendermcp_allow_python:
        raise PermissionError(
            "Python execution is disabled in this Blender session. Enable "
            "'Allow Python execution' in the MCP sidebar panel to use this tool."
        )

    code = params.get("code")
    if not code:
        raise ValueError("'code' parameter is required and must be a non-empty string")

    namespace: Dict[str, Any] = {"bpy": bpy, "mathutils": mathutils, "__name__": "__blender_mcp__"}
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exec(compile(code, "<blender_mcp>", "exec"), namespace)

    result = namespace.get("result")
    if result is not None and not isinstance(result, (str, int, float, bool, list, dict)):
        result = repr(result)
    return {
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "result": result,
    }


def _apply_render_settings(scene, params: Dict[str, Any]) -> Dict[str, Any]:
    """Apply optional render overrides, returning the previous values."""
    previous = {
        "engine": scene.render.engine,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
        "filepath": scene.render.filepath,
        "file_format": scene.render.image_settings.file_format,
    }
    if params.get("engine"):
        scene.render.engine = str(params["engine"]).upper()
    if params.get("resolution_x"):
        scene.render.resolution_x = int(params["resolution_x"])
    if params.get("resolution_y"):
        scene.render.resolution_y = int(params["resolution_y"])
    samples = params.get("samples")
    if samples:
        if scene.render.engine == "CYCLES" and hasattr(scene, "cycles"):
            previous["cycles_samples"] = scene.cycles.samples
            scene.cycles.samples = int(samples)
        elif hasattr(scene, "eevee"):
            previous["eevee_samples"] = scene.eevee.taa_render_samples
            scene.eevee.taa_render_samples = int(samples)
    return previous


def _restore_render_settings(scene, previous: Dict[str, Any]) -> None:
    scene.render.engine = previous["engine"]
    scene.render.resolution_x = previous["resolution_x"]
    scene.render.resolution_y = previous["resolution_y"]
    scene.render.filepath = previous["filepath"]
    scene.render.image_settings.file_format = previous["file_format"]
    if "cycles_samples" in previous:
        scene.cycles.samples = previous["cycles_samples"]
    if "eevee_samples" in previous:
        scene.eevee.taa_render_samples = previous["eevee_samples"]


def cmd_render_image(params: Dict[str, Any]) -> Dict[str, Any]:
    scene = bpy.context.scene
    if scene.camera is None:
        raise RuntimeError(
            "the scene has no active camera. Create one with blender_create_object "
            "(type='CAMERA') or set scene.camera first."
        )

    output_path = params.get("output_path")
    temporary = output_path is None
    if temporary:
        output_path = os.path.join(tempfile.gettempdir(), "blender_mcp_render.png")
    output_path = bpy.path.abspath(str(output_path))
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    previous = _apply_render_settings(scene, params)
    try:
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = output_path
        # Read back what is actually in effect: the settings are restored below,
        # so the response has to be built from the values used for this render.
        used = {
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "engine": scene.render.engine,
        }
        bpy.ops.render.render(write_still=True)
    finally:
        _restore_render_settings(scene, previous)

    # Blender appends the extension when the path has none.
    if not os.path.exists(output_path) and os.path.exists(output_path + ".png"):
        output_path += ".png"

    response: Dict[str, Any] = {
        "output_path": output_path,
        "camera": scene.camera.name,
        **used,
    }
    if params.get("return_image", True):
        max_size = int(params.get("max_size", 1024))
        preview_path = os.path.join(tempfile.gettempdir(), "blender_mcp_render_preview.png")
        with open(output_path, "rb") as source, open(preview_path, "wb") as target:
            target.write(source.read())
        response["image"] = _encode_png(_downscale_png(preview_path, max_size))
    return response


def _find_view3d():
    """Return (window, area, region) of the first 3D viewport, or None."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    return window, area, region
    return None


def cmd_viewport_screenshot(params: Dict[str, Any]) -> Dict[str, Any]:
    view3d = None if bpy.app.background else _find_view3d()
    if view3d is None:
        raise RuntimeError(
            "no 3D viewport is open in Blender (running headless?). Use "
            "blender_render_image for a camera render instead."
        )
    window, area, region = view3d

    scene = bpy.context.scene
    max_size = int(params.get("max_size", 800))
    output_path = os.path.join(tempfile.gettempdir(), "blender_mcp_viewport.png")

    previous = _apply_render_settings(scene, params)
    try:
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = output_path
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    finally:
        _restore_render_settings(scene, previous)

    if not os.path.exists(output_path) and os.path.exists(output_path + ".png"):
        output_path += ".png"

    return {
        "output_path": output_path,
        "image": _encode_png(_downscale_png(output_path, max_size)),
    }


HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "ping": cmd_ping,
    "get_scene_info": cmd_get_scene_info,
    "get_object_info": cmd_get_object_info,
    "create_object": cmd_create_object,
    "modify_object": cmd_modify_object,
    "delete_object": cmd_delete_object,
    "set_material": cmd_set_material,
    "execute_python": cmd_execute_python,
    "render_image": cmd_render_image,
    "viewport_screenshot": cmd_viewport_screenshot,
}


def dispatch(message: Dict[str, Any]) -> Dict[str, Any]:
    """Run one command and wrap the outcome in a protocol response."""
    command = message.get("type")
    params = message.get("params") or {}
    handler = HANDLERS.get(command)
    if handler is None:
        return {
            "status": "error",
            "message": f"unknown command '{command}'. Known commands: {', '.join(sorted(HANDLERS))}",
        }
    try:
        return {"status": "success", "result": handler(params)}
    except Exception as exc:  # noqa: BLE001 - every failure must reach the client
        return {
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


# --------------------------------------------------------------------------
# socket server
# --------------------------------------------------------------------------


class BlenderMCPServer:
    """Non-blocking TCP server polled from Blender's main thread."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.running = False
        self._socket: Optional[socket.socket] = None
        self._clients: Dict[socket.socket, bytearray] = {}

    def start(self) -> None:
        if self.running:
            return
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, self.port))
        self._socket.listen(5)
        self._socket.setblocking(False)
        self.running = True
        bpy.app.timers.register(self._tick, first_interval=TICK_INTERVAL, persistent=True)
        print(f"[blender-mcp] listening on {self.host}:{self.port}")

    def stop(self) -> None:
        self.running = False
        for client in list(self._clients):
            self._drop_client(client)
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None
        if bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.unregister(self._tick)
        print("[blender-mcp] server stopped")

    # -- internals ---------------------------------------------------------

    def _drop_client(self, client: socket.socket) -> None:
        self._clients.pop(client, None)
        with contextlib.suppress(OSError):
            client.close()

    def _accept(self) -> None:
        while True:
            try:
                client, address = self._socket.accept()
            except BlockingIOError:
                return
            except OSError:
                return
            client.setblocking(False)
            self._clients[client] = bytearray()
            print(f"[blender-mcp] client connected from {address[0]}:{address[1]}")

    def _send(self, client: socket.socket, response: Dict[str, Any]) -> None:
        payload = json.dumps(response).encode("utf-8") + b"\n"
        client.setblocking(True)
        client.settimeout(SEND_TIMEOUT)
        try:
            client.sendall(payload)
        finally:
            with contextlib.suppress(OSError):
                client.setblocking(False)

    def _pump(self, client: socket.socket) -> None:
        buffer = self._clients[client]
        try:
            chunk = client.recv(RECV_CHUNK)
        except BlockingIOError:
            chunk = b""
        except (ConnectionResetError, OSError):
            self._drop_client(client)
            return

        if chunk == b"" and client in self._clients:
            try:
                # recv returning b"" on a readable socket means the peer closed.
                if client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                    self._drop_client(client)
                    return
            except BlockingIOError:
                pass
            except OSError:
                self._drop_client(client)
                return
        buffer.extend(chunk)

        if len(buffer) > MAX_MESSAGE_BYTES:
            self._send(client, {"status": "error", "message": "request too large"})
            self._drop_client(client)
            return

        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer.clear()
            buffer.extend(rest)
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                response = {"status": "error", "message": f"invalid JSON request: {exc}"}
            else:
                response = dispatch(message)
            try:
                self._send(client, response)
            except OSError:
                self._drop_client(client)
                return

    def _tick(self) -> Optional[float]:
        if not self.running:
            return None
        self._accept()
        for client in list(self._clients):
            self._pump(client)
        return TICK_INTERVAL


_server: Optional[BlenderMCPServer] = None


# --------------------------------------------------------------------------
# operators, panel, registration
# --------------------------------------------------------------------------


class BLENDERMCP_OT_start_server(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Start server"
    bl_description = "Start the MCP command server"

    def execute(self, context):
        global _server
        if _server is not None and _server.running:
            self.report({"INFO"}, "MCP server already running")
            return {"CANCELLED"}
        _server = BlenderMCPServer(port=context.scene.blendermcp_port)
        try:
            _server.start()
        except OSError as exc:
            _server = None
            self.report({"ERROR"}, f"Could not start MCP server: {exc}")
            return {"CANCELLED"}
        context.scene.blendermcp_server_running = True
        self.report({"INFO"}, f"MCP server listening on port {context.scene.blendermcp_port}")
        return {"FINISHED"}


class BLENDERMCP_OT_stop_server(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop server"
    bl_description = "Stop the MCP command server"

    def execute(self, context):
        global _server
        if _server is not None:
            _server.stop()
            _server = None
        context.scene.blendermcp_server_running = False
        self.report({"INFO"}, "MCP server stopped")
        return {"FINISHED"}


class BLENDERMCP_PT_panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "blendermcp_port")
        layout.prop(scene, "blendermcp_allow_python")

        if scene.blendermcp_server_running:
            layout.operator("blendermcp.stop_server", icon="PAUSE")
            layout.label(text=f"Listening on 127.0.0.1:{scene.blendermcp_port}", icon="CHECKMARK")
        else:
            layout.operator("blendermcp.start_server", icon="PLAY")
            layout.label(text="Server stopped", icon="X")

        if scene.blendermcp_allow_python:
            box = layout.box()
            box.label(text="Python execution is ON", icon="ERROR")
            box.label(text="The client can run any bpy code.")


_CLASSES = (
    BLENDERMCP_OT_start_server,
    BLENDERMCP_OT_stop_server,
    BLENDERMCP_PT_panel,
)


def register() -> None:
    bpy.types.Scene.blendermcp_port = bpy.props.IntProperty(
        name="Port",
        description="TCP port the MCP command server listens on (localhost only)",
        default=DEFAULT_PORT,
        min=1024,
        max=65535,
    )
    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(default=False)
    bpy.types.Scene.blendermcp_allow_python = bpy.props.BoolProperty(
        name="Allow Python execution",
        description="Let the connected MCP client run arbitrary bpy code in this session",
        default=True,
    )
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running
    del bpy.types.Scene.blendermcp_allow_python


if __name__ == "__main__":
    register()
