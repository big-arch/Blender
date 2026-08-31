"""One-click installer for the Blender MCP add-on.

Open this file in Blender (Scripting workspace > Open > pick this file) and
press "Run Script" (Alt+P). It installs the add-on from the repository next to
this script, enables it, makes the choice permanent and reports where the panel
lives — or exactly what went wrong.

If Blender cannot work out where this script lives, set ADDON_PATH below to the
full path of addon/blender_mcp_addon.py and run it again.
"""

import os
import traceback

import addon_utils
import bpy

ADDON_PATH = ""  # optional override, e.g. r"C:\Users\me\Blender\addon\blender_mcp_addon.py"
MODULE_NAME = "blender_mcp_addon"


def _script_directory():
    """Where this script lives — works both as a file and as a text datablock."""
    path = globals().get("__file__") or ""
    if path:
        return os.path.dirname(os.path.abspath(path))
    for text in bpy.data.texts:
        if text.filepath and text.filepath.endswith("install_in_blender.py"):
            return os.path.dirname(os.path.abspath(bpy.path.abspath(text.filepath)))
    return ""


def _locate_addon():
    if ADDON_PATH:
        return os.path.abspath(ADDON_PATH)
    directory = _script_directory()
    if not directory:
        return ""
    candidates = [
        os.path.join(directory, os.pardir, "addon", "blender_mcp_addon.py"),
        os.path.join(directory, "addon", "blender_mcp_addon.py"),
        os.path.join(directory, "blender_mcp_addon.py"),
    ]
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if os.path.isfile(candidate):
            return candidate
    return ""


def main():
    print("\n" + "=" * 62)
    print("Установка аддона Blender MCP")
    print("=" * 62)
    print(f"Blender: {bpy.app.version_string}")

    source = _locate_addon()
    if not source:
        print("[X] Не нашёл blender_mcp_addon.py рядом со скриптом.")
        print("    Впишите полный путь к файлу в ADDON_PATH вверху скрипта")
        print("    и запустите ещё раз.")
        return
    print(f"Файл аддона: {source}")

    try:
        bpy.ops.preferences.addon_install(filepath=source, overwrite=True)
    except Exception:
        print("[X] Установка не удалась:")
        traceback.print_exc()
        return

    try:
        bpy.ops.preferences.addon_enable(module=MODULE_NAME)
    except Exception:
        print("[X] Не удалось включить аддон:")
        traceback.print_exc()
        return

    enabled = addon_utils.check(MODULE_NAME)[1]
    panel = hasattr(bpy.types, "BLENDERMCP_PT_panel")
    props = hasattr(bpy.context.scene, "blendermcp_port")

    print(f"[{'V' if enabled else 'X'}] аддон включён")
    print(f"[{'V' if panel else 'X'}] панель зарегистрирована")
    print(f"[{'V' if props else 'X'}] свойства сцены на месте")

    if not (enabled and panel and props):
        print("\nЧто-то не зарегистрировалось — смотрите ошибки выше.")
        return

    try:
        bpy.ops.wm.save_userpref()
        print("[V] настройки сохранены — аддон останется включённым после перезапуска")
    except Exception:
        print("[!] не смог сохранить настройки, включите аддон вручную в Preferences")

    print("\nГде искать панель:")
    print("  1. Перейдите в 3D-вьюпорт (окно с кубом).")
    print("  2. Нажмите N — справа откроется боковая панель.")
    print("  3. Вертикальные вкладки на её правом краю: Item / Tool / View / MCP.")
    print("  4. Вкладка MCP → кнопка 'Start server'.")

    scene = bpy.context.scene
    print(f"\nПорт по умолчанию: {scene.blendermcp_port}")
    print("Запустить сервер прямо сейчас можно так:")
    print("  bpy.ops.blendermcp.start_server()")
    print("=" * 62 + "\n")


main()
