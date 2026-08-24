#!/usr/bin/env python3
"""A dependency-free MCP stdio adapter for the local Tangle runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

VERSION = "0.3.0"
LATEST_PROTOCOL = "2025-11-25"
SUPPORTED_PROTOCOLS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
MAX_MESSAGE_BYTES = 1_000_000


def annotations(
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> dict[str, bool]:
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def object_schema(
    properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


STRING = {"type": "string", "minLength": 1}
STRING_LIST = {"type": "array", "items": STRING, "minItems": 1}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "tangle_doctor",
        "title": "Check Tangle",
        "description": "Check Git, Python, Codex authentication, configuration, and local Tangle state for the selected project.",
        "inputSchema": object_schema(),
        "annotations": annotations(read_only=True, idempotent=True),
    },
    {
        "name": "tangle_status",
        "title": "Show Tangle status",
        "description": "Read the selected project's branch, session snapshot, and worker task state without changing anything.",
        "inputSchema": object_schema(),
        "annotations": annotations(read_only=True, idempotent=True),
    },
    {
        "name": "tangle_initialize",
        "title": "Initialize Tangle",
        "description": "Initialize local Tangle state in the selected Git project. Repeated calls preserve existing tasks.",
        "inputSchema": object_schema(),
        "annotations": annotations(idempotent=True),
    },
    {
        "name": "tangle_snapshot",
        "title": "Snapshot active session",
        "description": "Capture the selected project's current clean or dirty working view in a private Git ref without stashing, staging, committing, checking out, or changing the active files.",
        "inputSchema": object_schema(
            {"label": {"type": "string", "default": "active-session"}}
        ),
        "annotations": annotations(),
    },
    {
        "name": "tangle_create_worker",
        "title": "Create Codex worker",
        "description": "Create one isolated worktree after validating its dependencies and non-overlapping file ownership.",
        "inputSchema": object_schema(
            {
                "task_id": STRING,
                "title": STRING,
                "owns": STRING_LIST,
                "depends_on": {"type": "array", "items": STRING, "default": []},
                "acceptance": {"type": "array", "items": STRING, "default": []},
                "tests": {"type": "array", "items": STRING, "default": []},
            },
            ["task_id", "title", "owns"],
        ),
        "annotations": annotations(),
    },
    {
        "name": "tangle_launch_worker",
        "title": "Launch Codex worker",
        "description": "Launch an already-created worker asynchronously. This sends its scoped task contract and relevant worktree content through the user's authenticated Codex CLI.",
        "inputSchema": object_schema(
            {"task_id": STRING, "feedback": {"type": "string"}}, ["task_id"]
        ),
        "annotations": annotations(destructive=True, open_world=True),
    },
    {
        "name": "tangle_poll",
        "title": "Poll Codex workers",
        "description": "Collect completed worker outcomes and mechanically validate their committed changes. It never accepts or integrates a result.",
        "inputSchema": object_schema({"task_id": STRING}),
        "annotations": annotations(idempotent=True),
    },
    {
        "name": "tangle_resume_worker",
        "title": "Resume Codex worker",
        "description": "Resume a failed or review-stage worker with bounded reviewer feedback, subject to the configured retry limit.",
        "inputSchema": object_schema(
            {"task_id": STRING, "feedback": STRING}, ["task_id", "feedback"]
        ),
        "annotations": annotations(destructive=True, open_world=True),
    },
    {
        "name": "tangle_accept_worker",
        "title": "Accept reviewed worker",
        "description": "Record Claude's completed review of a worker. Call only after inspecting the exact diff and applicable test results; this still does not integrate files.",
        "inputSchema": object_schema(
            {
                "task_id": STRING,
                "review_note": {"type": "string", "default": "Diff and tests reviewed by Claude"},
                "allow_unresolved": {"type": "boolean", "default": False},
            },
            ["task_id"],
        ),
        "annotations": annotations(),
    },
    {
        "name": "tangle_integrate_worker",
        "title": "Integrate accepted worker",
        "description": "Apply only an accepted worker's reviewed delta as unstaged changes on the invoking branch while preserving the existing Git index.",
        "inputSchema": object_schema({"task_id": STRING}, ["task_id"]),
        "annotations": annotations(destructive=True),
    },
    {
        "name": "tangle_cancel_worker",
        "title": "Cancel Codex worker",
        "description": "Stop or deliberately abandon a worker after verifying its recorded process identity. The worktree and branch remain for diagnosis until cleaned up.",
        "inputSchema": object_schema(
            {"task_id": STRING, "reason": {"type": "string"}}, ["task_id"]
        ),
        "annotations": annotations(destructive=True),
    },
    {
        "name": "tangle_cleanup_worker",
        "title": "Clean up terminal worker",
        "description": "Remove a clean terminal worker worktree. Branch deletion is optional and restricted to Tangle worker branches.",
        "inputSchema": object_schema(
            {
                "task_id": STRING,
                "delete_branch": {"type": "boolean", "default": False},
            },
            ["task_id"],
        ),
        "annotations": annotations(destructive=True, idempotent=True),
    },
    {
        "name": "tangle_open_dashboard",
        "title": "Open local Tangle dashboard",
        "description": "Start or reuse the localhost-only dashboard for this project and open it in the default browser. The dashboard can poll and reconcile but cannot accept or integrate work.",
        "inputSchema": object_schema(
            {
                "port": {"type": "integer", "minimum": 0, "maximum": 65535, "default": 0},
                "open_browser": {"type": "boolean", "default": True},
            }
        ),
        "annotations": annotations(idempotent=True),
    },
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}
TOOL_ARGUMENTS = {
    tool["name"]: set(tool["inputSchema"].get("properties", {})) for tool in TOOLS
}


class McpFailure(RuntimeError):
    """Expected tool or protocol failure."""


class TangleMcpServer:
    def __init__(
        self,
        repo: Path,
        orchestrator: Path,
        dashboard: Path,
        *,
        command_timeout: int = 120,
    ) -> None:
        self.repo = self._resolve_repo(repo)
        self.orchestrator = orchestrator.resolve()
        self.dashboard = dashboard.resolve()
        self.command_timeout = command_timeout
        if not self.orchestrator.is_file():
            raise McpFailure(f"Tangle orchestrator does not exist: {self.orchestrator}")

    @staticmethod
    def _resolve_repo(candidate: Path) -> Path:
        try:
            selected = candidate.expanduser().resolve(strict=True)
        except OSError as exc:
            raise McpFailure(f"Selected project directory is unavailable: {candidate}") from exc
        if not selected.is_dir():
            raise McpFailure(f"Selected project path is not a directory: {selected}")
        process = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=selected,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode:
            raise McpFailure(process.stderr.strip() or "Selected directory is not in a Git repository")
        root = Path(process.stdout.strip()).resolve()
        if root != selected:
            raise McpFailure(
                f"Select the Git repository root ({root}), not its subdirectory ({selected})"
            )
        return root

    def run_tangle(self, arguments: list[str]) -> dict[str, Any]:
        try:
            process = subprocess.run(
                [sys.executable, str(self.orchestrator), *arguments],
                cwd=self.repo,
                text=True,
                capture_output=True,
                timeout=self.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise McpFailure(
                f"Tangle command exceeded the {self.command_timeout}-second MCP limit"
            ) from exc
        if process.returncode:
            message = process.stderr.strip() or process.stdout.strip() or "Tangle command failed"
            if len(message) > 20_000:
                message = message[:20_000] + "\n… output truncated"
            raise McpFailure(message)
        output = process.stdout.strip()
        if not output:
            return {"ok": True}
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {"ok": True, "output": output}
        return {"ok": True, "result": parsed}

    @staticmethod
    def _strings(value: Any, field: str, *, required: bool = False) -> list[str]:
        if value is None:
            if required:
                raise McpFailure(f"{field} is required")
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise McpFailure(f"{field} must be a list of non-empty strings")
        if required and not value:
            raise McpFailure(f"{field} must contain at least one item")
        return value

    @staticmethod
    def _string(value: Any, field: str, *, required: bool = True) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str) or (required and not value.strip()):
            raise McpFailure(f"{field} must be a non-empty string")
        return value

    def call_tool(self, name: str, supplied: Any) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            raise McpFailure(f"Unknown Tangle tool: {name}")
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, dict):
            raise McpFailure("Tool arguments must be a JSON object")
        unknown = sorted(set(supplied) - TOOL_ARGUMENTS[name])
        if unknown:
            raise McpFailure("Unknown tool argument(s): " + ", ".join(unknown))

        if name == "tangle_doctor":
            return self.run_tangle(["doctor", "--config", "tangle.json"])
        if name == "tangle_status":
            return self.run_tangle(["status"])
        if name == "tangle_initialize":
            return self.run_tangle(["init", "--config", "tangle.json"])
        if name == "tangle_snapshot":
            label = supplied.get("label", "active-session")
            self._string(label, "label")
            return self.run_tangle(["snapshot", "--label", label])
        if name == "tangle_create_worker":
            task = self._string(supplied.get("task_id"), "task_id")
            title = self._string(supplied.get("title"), "title")
            owns = self._strings(supplied.get("owns"), "owns", required=True)
            command = ["create-worker", task or "", "--title", title or ""]
            for item in owns:
                command.extend(["--owns", item])
            for field, flag in (
                ("depends_on", "--depends-on"),
                ("acceptance", "--acceptance"),
                ("tests", "--test"),
            ):
                for item in self._strings(supplied.get(field), field):
                    command.extend([flag, item])
            return self.run_tangle(command)
        if name == "tangle_launch_worker":
            task = self._string(supplied.get("task_id"), "task_id")
            command = ["launch", task or ""]
            feedback = self._string(supplied.get("feedback"), "feedback", required=False)
            if feedback:
                command.extend(["--prompt", feedback])
            return self.run_tangle(command)
        if name == "tangle_poll":
            command = ["poll"]
            task = self._string(supplied.get("task_id"), "task_id", required=False)
            if task:
                command.append(task)
            return self.run_tangle(command)
        if name == "tangle_resume_worker":
            task = self._string(supplied.get("task_id"), "task_id")
            feedback = self._string(supplied.get("feedback"), "feedback")
            return self.run_tangle(["resume", task or "", "--feedback", feedback or ""])
        if name == "tangle_accept_worker":
            task = self._string(supplied.get("task_id"), "task_id")
            note = supplied.get("review_note", "Diff and tests reviewed by Claude")
            self._string(note, "review_note")
            command = ["accept", task or "", "--review-note", note]
            allow_unresolved = supplied.get("allow_unresolved", False)
            if not isinstance(allow_unresolved, bool):
                raise McpFailure("allow_unresolved must be a boolean")
            if allow_unresolved:
                command.append("--allow-unresolved")
            return self.run_tangle(command)
        if name == "tangle_integrate_worker":
            task = self._string(supplied.get("task_id"), "task_id")
            return self.run_tangle(["integrate", task or ""])
        if name == "tangle_cancel_worker":
            task = self._string(supplied.get("task_id"), "task_id")
            reason = self._string(supplied.get("reason"), "reason", required=False)
            command = ["cancel", task or ""]
            if reason:
                command.extend(["--reason", reason])
            return self.run_tangle(command)
        if name == "tangle_cleanup_worker":
            task = self._string(supplied.get("task_id"), "task_id")
            delete = supplied.get("delete_branch", False)
            if not isinstance(delete, bool):
                raise McpFailure("delete_branch must be a boolean")
            command = ["cleanup", task or ""]
            if delete:
                command.append("--delete-branch")
            return self.run_tangle(command)
        if name == "tangle_open_dashboard":
            port = supplied.get("port", 0)
            open_browser = supplied.get("open_browser", True)
            if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
                raise McpFailure("port must be an integer between 0 and 65535")
            if not isinstance(open_browser, bool):
                raise McpFailure("open_browser must be a boolean")
            return self.start_dashboard(port=port, open_browser=open_browser)
        raise McpFailure(f"Unhandled Tangle tool: {name}")

    def start_dashboard(self, *, port: int, open_browser: bool) -> dict[str, Any]:
        if not self.dashboard.is_file():
            raise McpFailure(f"Tangle dashboard does not exist: {self.dashboard}")
        runtime = self.repo / ".tangle"
        runtime.mkdir(parents=True, exist_ok=True)
        info = runtime / "dashboard.json"
        if info.is_file():
            try:
                existing = json.loads(info.read_text(encoding="utf-8"))
                url = str(existing["url"])
                request = urllib.request.Request(url + "api/health", method="GET")
                with urllib.request.urlopen(request, timeout=1) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if health.get("repo") == str(self.repo):
                    if open_browser:
                        webbrowser.open(url)
                    return {"ok": True, "result": {"url": url, "reused": True}}
            except (OSError, KeyError, ValueError, urllib.error.URLError):
                pass

        logs = runtime / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(self.dashboard),
            "--repo",
            str(self.repo),
            "--orchestrator",
            str(self.orchestrator),
            "--port",
            str(port),
            "--write-info",
            str(info),
        ]
        if open_browser:
            command.append("--open")
        stderr_handle = (logs / "dashboard.stderr.log").open("ab")
        try:
            subprocess.Popen(
                command,
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                start_new_session=True,
            )
        finally:
            stderr_handle.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                details = json.loads(info.read_text(encoding="utf-8"))
                url = str(details["url"])
                with urllib.request.urlopen(url + "api/health", timeout=0.5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if health.get("repo") == str(self.repo):
                    return {"ok": True, "result": {"url": url, "reused": False}}
            except (OSError, KeyError, ValueError, urllib.error.URLError):
                time.sleep(0.05)
        raise McpFailure("The local dashboard did not become ready; inspect .tangle/logs/dashboard.stderr.log")

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise McpFailure("Invalid JSON-RPC request")
        method = request["method"]
        request_id = request.get("id")
        if request_id is None:
            return None
        if method == "initialize":
            params = request.get("params") or {}
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            protocol = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
            result = {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "tangle", "title": "Tangle", "version": VERSION},
                "instructions": (
                    "Tangle is Codex-first, not Codex-only. Claude remains engineering lead, may code directly, "
                    "and must inspect a worker diff and tests before calling tangle_accept_worker. Never accept "
                    "or integrate merely because a worker process exited successfully."
                ),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise McpFailure("tools/call requires a tool name")
            try:
                payload = self.call_tool(params["name"], params.get("arguments", {}))
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}],
                    "structuredContent": payload,
                    "isError": False,
                }
            except McpFailure as exc:
                payload = {"ok": False, "error": str(exc)}
                result = {
                    "content": [{"type": "text", "text": str(exc)}],
                    "structuredContent": payload,
                    "isError": True,
                }
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def write_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def serve(server: TangleMcpServer) -> int:
    for raw in sys.stdin.buffer:
        request: dict[str, Any] | None = None
        if len(raw) > MAX_MESSAGE_BYTES:
            print("Tangle MCP: message exceeds size limit", file=sys.stderr)
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Message exceeds size limit"},
                }
            )
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise McpFailure("JSON-RPC message must be an object")
            response = server.handle(request)
            if response is not None:
                write_message(response)
        except (UnicodeDecodeError, json.JSONDecodeError, McpFailure) as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32600, "message": str(exc)},
                }
            )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", required=True, type=Path, help="fixed Git project root")
    result.add_argument(
        "--orchestrator",
        type=Path,
        default=Path(__file__).with_name("tangle_orchestrator.py"),
    )
    result.add_argument(
        "--dashboard",
        type=Path,
        default=Path(__file__).with_name("tangle_dashboard.py"),
    )
    result.add_argument("--command-timeout", type=int, default=120)
    result.add_argument("--version", action="version", version=f"Tangle MCP {VERSION}")
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        if not 1 <= args.command_timeout <= 600:
            raise McpFailure("--command-timeout must be between 1 and 600 seconds")
        return serve(
            TangleMcpServer(
                args.repo,
                args.orchestrator,
                args.dashboard,
                command_timeout=args.command_timeout,
            )
        )
    except (McpFailure, OSError) as exc:
        print(f"Tangle MCP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
