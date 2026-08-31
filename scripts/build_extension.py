#!/usr/bin/env python3
"""Package the add-on as a Blender 4.2+ extension zip.

    python3 scripts/build_extension.py

Produces dist/blender_mcp-<version>.zip, which installs by dragging it into a
Blender window — the path that cannot go wrong on modern Blender.
"""

import pathlib
import re
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "addon" / "blender_mcp_addon.py"
MANIFEST = ROOT / "blender_manifest.toml"
DIST = ROOT / "dist"


def main() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', manifest, re.M).group(1)
    DIST.mkdir(exist_ok=True)
    target = DIST / f"blender_mcp-{version}.zip"

    # Extensions are directories: the manifest and __init__.py sit inside a
    # folder named after the extension id.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("blender_mcp/blender_manifest.toml", manifest)
        archive.writestr("blender_mcp/__init__.py", ADDON.read_text(encoding="utf-8"))

    print(f"built {target.relative_to(ROOT)} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
