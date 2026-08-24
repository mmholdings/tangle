#!/usr/bin/env python3
"""Build Tangle's deterministic Claude Desktop MCP Bundle."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "dist" / "tangle.mcpb"
MANIFEST = PROJECT_ROOT / "extension" / "manifest.json"
FILES = {
    MANIFEST: "manifest.json",
    PROJECT_ROOT / "extension" / "icon.png": "icon.png",
    PROJECT_ROOT / "scripts" / "tangle_mcp_server.py": "server/tangle_mcp_server.py",
    PROJECT_ROOT / "scripts" / "tangle_orchestrator.py": "server/tangle_orchestrator.py",
    PROJECT_ROOT / "scripts" / "tangle_dashboard.py": "server/tangle_dashboard.py",
}
REQUIRED_MANIFEST_FIELDS = {
    "manifest_version",
    "name",
    "version",
    "description",
    "author",
    "server",
}


class BuildError(RuntimeError):
    """Expected bundle build failure."""


def read_manifest() -> dict[str, Any]:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read extension manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BuildError("Extension manifest must be a JSON object")
    missing = sorted(REQUIRED_MANIFEST_FIELDS - set(manifest))
    if missing:
        raise BuildError("Extension manifest is missing: " + ", ".join(missing))
    if manifest.get("manifest_version") != "0.3":
        raise BuildError("Extension manifest_version must be 0.3")
    server = manifest.get("server")
    if not isinstance(server, dict) or server.get("entry_point") not in FILES.values():
        raise BuildError("Extension server entry_point is not bundled")
    declared_tools = manifest.get("tools")
    if not isinstance(declared_tools, list) or not declared_tools:
        raise BuildError("Extension manifest must declare its Tangle tools")
    return manifest


def validate_archive_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise BuildError(f"Unsafe bundle path: {value}")


def build(output: Path) -> Path:
    read_manifest()
    for source, archive_path in FILES.items():
        validate_archive_path(archive_path)
        if not source.is_file():
            raise BuildError(f"Required bundle file does not exist: {source}")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for source, archive_path in sorted(FILES.items(), key=lambda item: item[1]):
                info = zipfile.ZipInfo(archive_path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if archive_path.endswith(".py") else 0o644) << 16
                bundle.writestr(info, source.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return result


def main() -> int:
    try:
        path = build(parser().parse_args().output)
        print(path)
        return 0
    except (BuildError, OSError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
