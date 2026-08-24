#!/usr/bin/env python3
"""Tangle's local, review-gated Codex worker orchestrator."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

VERSION = "0.2.0"
STATE_VERSION = 2
ACTIVE_STATUSES = {"ready", "running", "review"}
TERMINAL_STATUSES = {"integrated", "canceled", "failed"}
ALLOWED_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}
ALLOWED_APPROVAL_POLICIES = {"on-request", "never", "auto-review"}
ALLOWED_REASONING = {"none", "low", "medium", "high", "xhigh", "max"}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "execution_policy": "codex-first-hybrid",
    "max_workers": 4,
    "allow_claude_direct_edits": True,
    "preserve_selected_claude_model": True,
    "active_session": {
        "attach_in_place": True,
        "snapshot_dirty_tree": True,
        "include_untracked_nonignored": True,
        "merge_target": "invoking-session-branch",
    },
    "workers": {
        "adapter": "codex-cli",
        "command": "codex",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "service_tier": "fast",
        "sandbox": "workspace-write",
        "approval_policy": "on-request",
        "worktree_root": ".tangle/worktrees",
        "timeout_seconds": 1800,
        "max_retries": 1,
        "allow_noop": False,
    },
    "claude_direct_edit_reasons": [
        "tiny-change",
        "architecture-sensitive",
        "codex-repeated-failure",
        "integration-conflict",
        "high-risk-change",
        "explicit-user-request",
    ],
}


class TangleError(RuntimeError):
    """Expected user-facing failure."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        command,
        cwd=cwd,
        env=merged,
        input=input_bytes,
        capture_output=True,
        text=input_bytes is None,
        check=False,
    )


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = run(["git", *args], cwd=root, env=env)
    if proc.returncode:
        stderr = str(proc.stderr).strip()
        raise TangleError(stderr or f"git {' '.join(args)} failed")
    return str(proc.stdout).strip()


def git_bytes(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if proc.returncode:
        raise TangleError(
            proc.stderr.decode("utf-8", "replace").strip()
            or f"git {' '.join(args)} failed"
        )
    return proc.stdout


def git_apply(
    root: Path,
    patch: bytes,
    *,
    check: bool = False,
    index: bool = False,
    reverse: bool = False,
) -> None:
    args = ["git", "apply", "--whitespace=nowarn"]
    if check:
        args.append("--check")
    if index:
        args.append("--index")
    if reverse:
        args.append("--reverse")
    proc = run(args, cwd=root, input_bytes=patch)
    if proc.returncode:
        stderr = bytes(proc.stderr).decode("utf-8", "replace").strip()
        raise TangleError(stderr or "worker patch could not be applied")


def repo_root() -> Path:
    return Path(git(Path.cwd(), "rev-parse", "--show-toplevel")).resolve()


def state_paths(root: Path) -> dict[str, Path]:
    runtime = root / ".tangle"
    state_dir = runtime / "orchestrator"
    return {
        "runtime": runtime,
        "state_dir": state_dir,
        "state": state_dir / "state.json",
        "lock": state_dir / "state.lock",
        "jobs": state_dir / "jobs",
        "prompts": state_dir / "prompts",
        "logs": runtime / "logs",
        "results": runtime / "results",
    }


@contextlib.contextmanager
def state_lock(root: Path, *, shared: bool = False) -> Iterator[None]:
    locations = state_paths(root)
    locations["state_dir"].mkdir(parents=True, exist_ok=True)
    with locations["lock"].open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TangleError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TangleError(
            f"invalid JSON in {label} at line {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise TangleError(f"{label} must contain a JSON object")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.stem}-", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def merge_config(base: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


def expect_type(value: Any, expected: type, field: str) -> None:
    if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
        raise TangleError(f"config field {field} must be an integer")
    if expected is not int and not isinstance(value, expected):
        raise TangleError(f"config field {field} must be {expected.__name__}")


def safe_relative_path(value: str, field: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise TangleError(f"config field {field} must be a safe repo-relative path")
    if candidate.parts[0] == ".git":
        raise TangleError(f"config field {field} cannot be inside .git")
    return candidate


def validate_config(supplied: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(supplied) - set(DEFAULT_CONFIG))
    if unknown:
        raise TangleError(f"unknown config field(s): {', '.join(unknown)}")
    config = merge_config(DEFAULT_CONFIG, supplied)
    expect_type(config["version"], int, "version")
    if config["version"] != 1:
        raise TangleError("config field version must be 1")
    expect_type(config["execution_policy"], str, "execution_policy")
    if config["execution_policy"] != "codex-first-hybrid":
        raise TangleError("execution_policy must be codex-first-hybrid")
    expect_type(config["max_workers"], int, "max_workers")
    if not 1 <= config["max_workers"] <= 32:
        raise TangleError("max_workers must be between 1 and 32")
    for field in ("allow_claude_direct_edits", "preserve_selected_claude_model"):
        expect_type(config[field], bool, field)
    if (
        not config["allow_claude_direct_edits"]
        or not config["preserve_selected_claude_model"]
    ):
        raise TangleError(
            "Tangle's hybrid policy requires Claude direct edits and selected-model preservation"
        )
    expect_type(config["active_session"], dict, "active_session")
    active = config["active_session"]
    active_unknown = sorted(set(active) - set(DEFAULT_CONFIG["active_session"]))
    if active_unknown:
        raise TangleError(
            f"unknown active_session field(s): {', '.join(active_unknown)}"
        )
    for field in (
        "attach_in_place",
        "snapshot_dirty_tree",
        "include_untracked_nonignored",
    ):
        expect_type(active[field], bool, f"active_session.{field}")
    if not active["attach_in_place"]:
        raise TangleError("active_session.attach_in_place must remain true")
    if active["merge_target"] != "invoking-session-branch":
        raise TangleError("active_session.merge_target must be invoking-session-branch")
    expect_type(config["workers"], dict, "workers")
    workers = config["workers"]
    worker_unknown = sorted(set(workers) - set(DEFAULT_CONFIG["workers"]))
    if worker_unknown:
        raise TangleError(f"unknown workers field(s): {', '.join(worker_unknown)}")
    if workers["adapter"] != "codex-cli":
        raise TangleError("workers.adapter currently supports only codex-cli")
    string_fields = (
        "command",
        "model",
        "reasoning_effort",
        "service_tier",
        "sandbox",
        "approval_policy",
        "worktree_root",
    )
    for field in string_fields:
        expect_type(workers[field], str, f"workers.{field}")
        if not workers[field].strip():
            raise TangleError(f"config field workers.{field} cannot be empty")
    if workers["reasoning_effort"] not in ALLOWED_REASONING:
        raise TangleError(
            "workers.reasoning_effort must be one of "
            + ", ".join(sorted(ALLOWED_REASONING))
        )
    if workers["sandbox"] not in ALLOWED_SANDBOXES:
        raise TangleError("workers.sandbox has an unsupported value")
    if workers["approval_policy"] not in ALLOWED_APPROVAL_POLICIES:
        raise TangleError("workers.approval_policy has an unsupported value")
    if workers["sandbox"] == "danger-full-access":
        raise TangleError("danger-full-access is not allowed for Tangle workers")
    safe_relative_path(workers["worktree_root"], "workers.worktree_root")
    for field, minimum, maximum in (
        ("timeout_seconds", 0, 86400),
        ("max_retries", 0, 10),
    ):
        expect_type(workers[field], int, f"workers.{field}")
        if not minimum <= workers[field] <= maximum:
            raise TangleError(
                f"workers.{field} must be between {minimum} and {maximum}"
            )
    expect_type(workers["allow_noop"], bool, "workers.allow_noop")
    expect_type(
        config["claude_direct_edit_reasons"], list, "claude_direct_edit_reasons"
    )
    if not all(
        isinstance(item, str) and item for item in config["claude_direct_edit_reasons"]
    ):
        raise TangleError("claude_direct_edit_reasons must contain non-empty strings")
    return config


def load_config(root: Path, config_arg: str) -> tuple[dict[str, Any], str | None]:
    path = Path(config_arg)
    if not path.is_absolute():
        path = root / path
    if path.exists():
        return validate_config(read_json(path, label="config")), str(path.resolve())
    if config_arg != "tangle.json":
        raise TangleError(f"config does not exist: {path}")
    return validate_config({}), None


def load_state(root: Path) -> dict[str, Any]:
    path = state_paths(root)["state"]
    if not path.exists():
        raise TangleError("not initialized; run 'tangle_orchestrator.py init' first")
    state = read_json(path, label="state")
    if state.get("version") not in {1, STATE_VERSION}:
        raise TangleError(f"unsupported state version: {state.get('version')!r}")
    if Path(str(state.get("repo", ""))).resolve() != root:
        raise TangleError("state belongs to a different repository")
    if not isinstance(state.get("tasks"), dict):
        raise TangleError("state tasks must be a JSON object")
    state["config"] = validate_config(state.get("config", {}))
    return state


def save_state(root: Path, state: dict[str, Any]) -> None:
    state["version"] = STATE_VERSION
    state["updated_at"] = now()
    atomic_json(state_paths(root)["state"], state)


def ensure_runtime_layout(root: Path, config: dict[str, Any]) -> None:
    locations = state_paths(root)
    worker_root = worktree_root(root, config)
    for directory in (
        locations["state_dir"],
        locations["jobs"],
        locations["prompts"],
        locations["logs"],
        locations["results"],
        worker_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    exclude = Path(git(root, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = root / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if not any(line.strip() == ".tangle/" for line in existing.splitlines()):
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}.tangle/\n")


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:40] or "task"


def task_id(value: str) -> str:
    result = value.upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,31}", result):
        raise TangleError(
            "task id must be 1-32 letters, digits, underscores, or hyphens"
        )
    return result


def normalize_ownership(pattern: str) -> str:
    value = pattern.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts or "\0" in value:
        raise TangleError(f"unsafe ownership glob: {pattern!r}")
    if candidate.parts[0] in {".git", ".tangle"}:
        raise TangleError("workers cannot own .git/** or .tangle/**")
    if "[" in value or "]" in value:
        raise TangleError(
            "ownership globs support literals, *, **, and ? (not character classes)"
        )
    return "/".join(candidate.parts).rstrip("/")


def static_glob_prefix(pattern: str) -> str:
    wildcard = min(
        (pattern.find(char) for char in "*?" if char in pattern), default=len(pattern)
    )
    return pattern[:wildcard].rstrip("/")


def ownership_overlaps(left: str, right: str) -> bool:
    if left == right or glob_matches(left, right) or glob_matches(right, left):
        return True
    left_prefix = static_glob_prefix(left)
    right_prefix = static_glob_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(right_prefix)
        or right_prefix.startswith(left_prefix)
    )


@lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern[str]:
    result = "^"
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    result += "(?:.*/)?"
                    index += 1
                else:
                    result += ".*"
                continue
            result += "[^/]*"
        elif char == "?":
            result += "[^/]"
        else:
            result += re.escape(char)
        index += 1
    return re.compile(result + "$")


def glob_matches(pattern: str, path: str) -> bool:
    return glob_regex(pattern).fullmatch(path) is not None


def path_owned(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if glob_matches(pattern, path):
            return True
        if pattern.endswith("/**") and path == pattern[:-3].rstrip("/"):
            return True
        if not any(char in pattern for char in "*?[") and (
            path == pattern or path.startswith(pattern.rstrip("/") + "/")
        ):
            return True
    return False


def worktree_root(root: Path, config: dict[str, Any]) -> Path:
    target = (
        root
        / safe_relative_path(
            config["workers"]["worktree_root"], "workers.worktree_root"
        )
    ).resolve()
    if target == root or root not in target.parents:
        raise TangleError(
            "workers.worktree_root must resolve to a directory inside the repository"
        )
    return target


def worktree_is_registered(root: Path, worktree: Path) -> bool:
    registered = [
        Path(line[9:]).resolve()
        for line in git(root, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    return worktree.resolve() in registered


def dirty_paths(root: Path) -> str:
    return git(root, "status", "--porcelain=v1", "--untracked-files=all")


def snapshot(root: Path, label: str, config: dict[str, Any]) -> tuple[str, str]:
    head = git(root, "rev-parse", "HEAD")
    dirty = dirty_paths(root)
    active = config["active_session"]
    if dirty and not active["snapshot_dirty_tree"]:
        raise TangleError(
            "working tree is dirty and active_session.snapshot_dirty_tree is false"
        )
    git_dir = Path(git(root, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    fd, index_name = tempfile.mkstemp(prefix="tangle-index-", dir=git_dir)
    os.close(fd)
    Path(index_name).unlink()
    env = {"GIT_INDEX_FILE": index_name}
    try:
        git(root, "read-tree", head, env=env)
        if active["include_untracked_nonignored"]:
            git(root, "add", "-A", "--", ".", env=env)
        else:
            git(root, "add", "-u", "--", ".", env=env)
            staged_new = git_bytes(
                root, "diff", "--cached", "--name-only", "--diff-filter=A", "-z"
            ).split(b"\0")
            for raw_path in staged_new:
                if not raw_path:
                    continue
                path = raw_path.decode("utf-8", "surrogateescape")
                if path == ".tangle" or path.startswith(".tangle/"):
                    continue
                git(root, "add", "-A", "--", path, env=env)
        # .tangle is reserved runtime state. Remove it from the temporary
        # index even if the host repository previously tracked that path.
        git(root, "rm", "-r", "--cached", "--ignore-unmatch", "--", ".tangle", env=env)
        tree = git(root, "write-tree", env=env)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        message = f"Tangle session snapshot: {label} ({stamp})"
        commit = git(
            root,
            "commit-tree",
            tree,
            "-p",
            head,
            "-m",
            message,
            env=env
            | {
                "GIT_AUTHOR_NAME": "Tangle Orchestrator",
                "GIT_AUTHOR_EMAIL": "tangle-orchestrator@local",
                "GIT_COMMITTER_NAME": "Tangle Orchestrator",
                "GIT_COMMITTER_EMAIL": "tangle-orchestrator@local",
            },
        )
        ref = f"refs/tangle/snapshots/{stamp}-{slug(label)}-{uuid.uuid4().hex[:8]}"
        git(root, "update-ref", ref, commit)
        return commit, ref
    finally:
        Path(index_name).unlink(missing_ok=True)


def changed_files(worktree: Path, base: str, commit: str) -> list[str]:
    raw = git_bytes(
        worktree,
        "diff",
        "--name-only",
        "--no-renames",
        "--diff-filter=ACDMRTUXB",
        "-z",
        base,
        commit,
    )
    return sorted(
        item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item
    )


def validate_task_result(
    root: Path, task: dict[str, Any], *, allow_noop: bool = False
) -> tuple[str, list[str]]:
    raw_worktree = task.get("worktree")
    if not raw_worktree:
        raise TangleError("task worktree has already been cleaned up")
    worktree = Path(raw_worktree)
    if not worktree.exists() or not worktree_is_registered(root, worktree):
        raise TangleError(f"task worktree is missing or unregistered: {worktree}")
    branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != task["branch"]:
        raise TangleError(f"worker is on {branch!r}; expected {task['branch']!r}")
    if dirty_paths(worktree):
        raise TangleError(
            "worker has uncommitted changes; commit or discard them before completion"
        )
    commit = git(worktree, "rev-parse", "HEAD")
    base = task["base_commit"]
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, commit],
        cwd=worktree,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ancestor.returncode != 0:
        raise TangleError("worker HEAD does not descend from its recorded base")
    files = changed_files(worktree, base, commit)
    if not files and not allow_noop:
        raise TangleError(
            "worker made no committed changes; use --allow-noop only when intentional"
        )
    outside = [path for path in files if not path_owned(path, task["owns"])]
    if outside:
        raise TangleError(
            "worker changed paths outside ownership: " + ", ".join(outside)
        )
    return commit, files


def write_task_result(root: Path, task_key: str, task: dict[str, Any]) -> None:
    atomic_json(
        state_paths(root)["results"] / f"{task_key}.json",
        {
            "task_id": task_key,
            "status": task["status"],
            "commit": task.get("commit"),
            "changed_files": task.get("changed_files", []),
            "tests": task.get("tests", "not reported"),
            "unresolved": task.get("unresolved", []),
            "last_error": task.get("last_error"),
        },
    )


def cmd_init(args: argparse.Namespace) -> None:
    root = repo_root()
    config, config_source = load_config(root, args.config)
    with state_lock(root):
        state_file = state_paths(root)["state"]
        if state_file.exists() and not args.force:
            existing = load_state(root)
            ensure_runtime_layout(root, existing["config"])
            print(f"already initialized: {state_file}")
            return
        if state_file.exists() and args.force:
            existing = load_state(root)
            live = [
                key
                for key, task in existing["tasks"].items()
                if task.get("worktree") and Path(task["worktree"]).exists()
            ]
            if live:
                raise TangleError(
                    "cannot force reinitialize while registered task worktrees exist: "
                    + ", ".join(live)
                )
        ensure_runtime_layout(root, config)
        invoking_branch = git(root, "branch", "--show-current") or None
        if not invoking_branch:
            raise TangleError(
                "initialize Tangle from a named branch, not detached HEAD"
            )
        state = {
            "version": STATE_VERSION,
            "repo": str(root),
            "invoking_branch": invoking_branch,
            "base_commit": git(root, "rev-parse", "HEAD"),
            "snapshot_commit": None,
            "snapshot_ref": None,
            "config_source": config_source,
            "config": config,
            "tasks": {},
            "created_at": now(),
        }
        save_state(root, state)
        print(state_file)


def cmd_validate_config(args: argparse.Namespace) -> None:
    root = repo_root()
    config, source = load_config(root, args.config)
    print(json.dumps({"valid": True, "source": source, "config": config}, indent=2))


def cmd_configure(args: argparse.Namespace) -> None:
    root = repo_root()
    config, source = load_config(root, args.config)
    with state_lock(root):
        state = load_state(root)
        old_root = worktree_root(root, state["config"])
        new_root = worktree_root(root, config)
        live = any(
            task.get("worktree") and Path(task["worktree"]).exists()
            for task in state["tasks"].values()
        )
        if live and old_root != new_root:
            raise TangleError(
                "cannot change workers.worktree_root while task worktrees exist"
            )
        state["config"] = config
        state["config_source"] = source
        ensure_runtime_layout(root, config)
        save_state(root, state)
        print(json.dumps({"configured": True, "source": source}, indent=2))


def cmd_snapshot(args: argparse.Namespace) -> None:
    root = repo_root()
    with state_lock(root):
        state = load_state(root)
        if any(
            task.get("status") in ACTIVE_STATUSES for task in state["tasks"].values()
        ):
            raise TangleError(
                "cannot replace the session snapshot while tasks are active"
            )
        commit, ref = snapshot(root, args.label, state["config"])
        state["snapshot_commit"] = commit
        state["snapshot_ref"] = ref
        save_state(root, state)
        print(commit)


def dependency_patch(worktree: Path, dependency: dict[str, Any]) -> bytes:
    return git_bytes(
        worktree,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        dependency["base_commit"],
        dependency["commit"],
    )


def compose_dependencies(
    worktree: Path, dependencies: list[tuple[str, dict[str, Any]]]
) -> None:
    env = {
        "GIT_AUTHOR_NAME": "Tangle Orchestrator",
        "GIT_AUTHOR_EMAIL": "tangle-orchestrator@local",
        "GIT_COMMITTER_NAME": "Tangle Orchestrator",
        "GIT_COMMITTER_EMAIL": "tangle-orchestrator@local",
    }
    for key, dependency in dependencies:
        patch = dependency_patch(worktree, dependency)
        if not patch:
            continue
        git_apply(worktree, patch, check=True, index=True)
        git_apply(worktree, patch, index=True)
        git(worktree, "commit", "-m", f"Tangle dependency base: {key}", env=env)


def cmd_create_worker(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    owns = [normalize_ownership(pattern) for pattern in args.owns]
    dependencies = [task_id(item) for item in args.depends_on]
    if key in dependencies:
        raise TangleError("a task cannot depend on itself")
    with state_lock(root):
        state = load_state(root)
        if key in state["tasks"]:
            raise TangleError(f"task already exists: {key}")
        max_workers = state["config"]["max_workers"]
        active = sum(
            task.get("status") in ACTIVE_STATUSES for task in state["tasks"].values()
        )
        if active >= max_workers:
            raise TangleError(f"max_workers limit reached ({max_workers})")
        dependency_tasks: list[tuple[str, dict[str, Any]]] = []
        for dependency_key in dependencies:
            dependency = state["tasks"].get(dependency_key)
            if dependency is None:
                raise TangleError(f"unknown dependency: {dependency_key}")
            if dependency.get("status") not in {"accepted", "integrated"}:
                raise TangleError(
                    f"dependency {dependency_key} must be accepted before {key} can start"
                )
            dependency_tasks.append((dependency_key, dependency))
        for other_key, other in state["tasks"].items():
            if other.get("status") not in ACTIVE_STATUSES:
                continue
            for left in owns:
                for right in other.get("owns", []):
                    if ownership_overlaps(left, right):
                        raise TangleError(
                            f"ownership {left!r} overlaps active task {other_key}: {right!r}"
                        )
        base = state.get("snapshot_commit") or state["base_commit"]
        target = worktree_root(root, state["config"]) / key
        if target.exists():
            raise TangleError(f"worktree path exists: {target}")
        branch = f"tangle/{key.lower()}-{slug(args.title)}"
        if (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=root,
                check=False,
            ).returncode
            == 0
        ):
            raise TangleError(f"worker branch already exists: {branch}")
        try:
            git(root, "worktree", "add", "-b", branch, str(target), base)
            compose_dependencies(target, dependency_tasks)
            worker_base = git(target, "rev-parse", "HEAD")
        except Exception:
            if target.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(target)],
                    cwd=root,
                    check=False,
                )
            subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False)
            raise
        state["tasks"][key] = {
            "title": args.title,
            "status": "ready",
            "branch": branch,
            "worktree": str(target),
            "owns": owns,
            "depends_on": dependencies,
            "acceptance": args.acceptance,
            "test_commands": args.test,
            "base_commit": worker_base,
            "session_base_commit": base,
            "attempts": 0,
            "created_at": now(),
        }
        save_state(root, state)
        print(json.dumps(state["tasks"][key], indent=2))


def finish_task(
    root: Path,
    state: dict[str, Any],
    key: str,
    *,
    tests: str,
    unresolved: list[str],
    allow_noop: bool,
) -> dict[str, Any]:
    task = state["tasks"].get(key)
    if not task:
        raise TangleError(f"unknown task: {key}")
    if task.get("status") in {"accepted", "integrated", "canceled"}:
        raise TangleError(
            f"task {key} cannot complete from status {task.get('status')}"
        )
    commit, files = validate_task_result(root, task, allow_noop=allow_noop)
    task.update(
        {
            "commit": commit,
            "changed_files": files,
            "status": "review",
            "tests": tests,
            "unresolved": unresolved,
            "completed_at": now(),
            "last_error": None,
        }
    )
    write_task_result(root, key, task)
    return task


def cmd_complete(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    with state_lock(root):
        state = load_state(root)
        task = finish_task(
            root,
            state,
            key,
            tests=args.tests,
            unresolved=args.unresolved,
            allow_noop=args.allow_noop or state["config"]["workers"]["allow_noop"],
        )
        save_state(root, state)
        print(json.dumps(task, indent=2))


def cmd_accept(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    with state_lock(root):
        state = load_state(root)
        task = state["tasks"].get(key)
        if not task:
            raise TangleError(f"unknown task: {key}")
        if task.get("status") != "review":
            raise TangleError(f"task {key} must be in review before acceptance")
        commit, files = validate_task_result(
            root, task, allow_noop=state["config"]["workers"]["allow_noop"]
        )
        if commit != task.get("commit") or files != task.get("changed_files"):
            raise TangleError(
                "worker changed after completion; complete and review it again"
            )
        if task.get("unresolved") and not args.allow_unresolved:
            raise TangleError(
                "task has unresolved issues; pass --allow-unresolved only if reviewed"
            )
        task["status"] = "accepted"
        task["accepted_at"] = now()
        task["review_note"] = args.review_note
        save_state(root, state)
        write_task_result(root, key, task)
        print(commit)


def cmd_integrate(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    with state_lock(root):
        state = load_state(root)
        task = state["tasks"].get(key)
        if not task:
            raise TangleError(f"unknown task: {key}")
        if task.get("status") == "integrated":
            print(f"already integrated: {key}")
            return
        if task.get("status") != "accepted":
            raise TangleError(f"task {key} must be accepted before integration")
        pending = [
            dep
            for dep in task.get("depends_on", [])
            if state["tasks"].get(dep, {}).get("status") != "integrated"
        ]
        if pending:
            raise TangleError("integrate dependencies first: " + ", ".join(pending))
        branch = git(root, "branch", "--show-current") or None
        if branch != state.get("invoking_branch"):
            raise TangleError(
                f"integration must run on invoking branch {state.get('invoking_branch')!r}; "
                f"current branch is {branch!r}"
            )
        commit, files = validate_task_result(
            root, task, allow_noop=state["config"]["workers"]["allow_noop"]
        )
        if commit != task.get("commit") or files != task.get("changed_files"):
            raise TangleError("worker changed after acceptance")
        worker_tree = Path(task["worktree"])
        patch = git_bytes(
            worker_tree,
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            task["base_commit"],
            task["commit"],
        )
        index_before = git(root, "write-tree")
        already_applied = False
        if patch:
            try:
                git_apply(root, patch, check=True)
            except TangleError as original_error:
                try:
                    git_apply(root, patch, check=True, reverse=True)
                except TangleError:
                    raise original_error
                already_applied = True
            if not already_applied:
                git_apply(root, patch)
        index_after = git(root, "write-tree")
        if index_before != index_after:
            raise TangleError("integration unexpectedly changed the active Git index")
        task["status"] = "integrated"
        task["integrated_at"] = now()
        task["integration_mode"] = "unstaged-worker-delta"
        task["integration_recovered"] = already_applied
        save_state(root, state)
        write_task_result(root, key, task)
        print(
            json.dumps(
                {"task": key, "files": files, "staging_area_preserved": True}, indent=2
            )
        )


def codex_binary(command: str) -> str:
    resolved = shutil.which(command)
    if not resolved and Path(command).is_file():
        resolved = str(Path(command).resolve())
    if not resolved and command == "codex":
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled.is_file() and os.access(bundled, os.X_OK):
            resolved = str(bundled)
    if not resolved:
        raise TangleError(f"Codex CLI not found: {command}")
    return resolved


def codex_command(
    workers: dict[str, Any],
    *,
    result: Path,
    mode: str,
    session_id: str | None,
) -> list[str]:
    command = [codex_binary(workers["command"]), "exec"]
    if mode == "resume":
        if not session_id:
            raise TangleError(
                "cannot resume because the worker session id is unavailable"
            )
        command.extend(["resume", "--json", "--skip-git-repo-check"])
    else:
        command.extend(
            [
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                workers["sandbox"],
                "--color",
                "never",
            ]
        )
    command.extend(
        [
            "-m",
            workers["model"],
            "-c",
            f"model_reasoning_effort={json.dumps(workers['reasoning_effort'])}",
            "-c",
            f"service_tier={json.dumps(workers['service_tier'])}",
        ]
    )
    if workers["approval_policy"] == "never":
        command.extend(["-c", 'approval_policy="never"'])
    elif workers["approval_policy"] == "auto-review" and mode != "resume":
        command.append("--approve-for-me")
    command.extend(["-o", str(result)])
    if mode == "resume":
        command.extend([session_id or "", "-"])
    else:
        command.append("-")
    return command


def worker_prompt(key: str, task: dict[str, Any], extra: str | None) -> str:
    acceptance = (
        "\n".join(f"- {item}" for item in task.get("acceptance", []))
        or "- Satisfy the task title and scope."
    )
    tests = (
        "\n".join(f"- {item}" for item in task.get("test_commands", []))
        or "- Run the relevant project checks you can identify."
    )
    owns = "\n".join(f"- {item}" for item in task["owns"])
    extra_block = (
        f"\nReviewer feedback for this attempt:\n{extra.strip()}\n" if extra else ""
    )
    return f"""You are Codex worker {key} in a Tangle-managed isolated Git worktree.

Task: {task["title"]}

You may change only these owned paths:
{owns}

Acceptance criteria:
{acceptance}

Required checks:
{tests}
{extra_block}
Implement the task, inspect your complete diff, run the applicable checks, and commit every change on the current worker branch. Do not modify .git or .tangle, do not push, and do not change files outside the owned paths. Finish with a compact report containing: status, commit, files changed, tests, and unresolved issues.
"""


def parse_thread_id(events: Path) -> str | None:
    if not events.exists():
        return None
    with events.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
    return None


def launch_attempt(
    root: Path,
    state: dict[str, Any],
    key: str,
    *,
    mode: str,
    feedback: str | None,
) -> dict[str, Any]:
    task = state["tasks"][key]
    attempts = int(task.get("attempts", 0)) + 1
    workers = state["config"]["workers"]
    if attempts > 1 + workers["max_retries"]:
        raise TangleError(f"retry limit reached for {key}")
    locations = state_paths(root)
    token = uuid.uuid4().hex
    prompt = locations["prompts"] / f"{key}-attempt-{attempts}.md"
    result = locations["results"] / f"{key}-attempt-{attempts}.txt"
    events = locations["logs"] / f"{key}-attempt-{attempts}.events.jsonl"
    stderr = locations["logs"] / f"{key}-attempt-{attempts}.stderr.log"
    outcome = locations["jobs"] / f"{key}-attempt-{attempts}.outcome.json"
    runtime = locations["jobs"] / f"{key}-attempt-{attempts}.runtime.json"
    job = locations["jobs"] / f"{key}-attempt-{attempts}.json"
    prompt.write_text(worker_prompt(key, task, feedback), encoding="utf-8")
    command = codex_command(
        workers, result=result, mode=mode, session_id=task.get("session_id")
    )
    atomic_json(
        job,
        {
            "token": token,
            "command": command,
            "cwd": task["worktree"],
            "prompt": str(prompt),
            "events": str(events),
            "stderr": str(stderr),
            "outcome": str(outcome),
            "runtime": str(runtime),
            "timeout_seconds": workers["timeout_seconds"],
        },
    )
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_run-job", str(job)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    task.update(
        {
            "status": "running",
            "attempts": attempts,
            "worker_pid": process.pid,
            "launch_token": token,
            "job": str(job),
            "outcome": str(outcome),
            "runtime": str(runtime),
            "events": str(events),
            "worker_result": str(result),
            "started_at": now(),
            "last_error": None,
        }
    )
    return task


def cmd_launch(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    feedback = None
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = root / prompt_path
        try:
            feedback = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TangleError(f"cannot read prompt file: {prompt_path}: {exc}") from exc
    if args.prompt:
        feedback = f"{feedback}\n{args.prompt}" if feedback else args.prompt
    with state_lock(root):
        state = load_state(root)
        task = state["tasks"].get(key)
        if not task:
            raise TangleError(f"unknown task: {key}")
        if task.get("status") != "ready":
            raise TangleError(f"task {key} must be ready before launch")
        task = launch_attempt(root, state, key, mode="new", feedback=feedback)
        save_state(root, state)
        print(
            json.dumps(
                {"task": key, "status": task["status"], "pid": task["worker_pid"]},
                indent=2,
            )
        )


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def poll_one(root: Path, state: dict[str, Any], key: str) -> bool:
    task = state["tasks"][key]
    if task.get("status") != "running":
        return False
    outcome_path = Path(task["outcome"])
    if not outcome_path.exists():
        if pid_alive(task.get("worker_pid")):
            return False
        task["status"] = "failed"
        task["last_error"] = "worker exited without writing an outcome"
        task["finished_at"] = now()
        write_task_result(root, key, task)
        return True
    outcome = read_json(outcome_path, label="worker outcome")
    if outcome.get("token") != task.get("launch_token"):
        task["status"] = "failed"
        task["last_error"] = "worker outcome token mismatch"
    else:
        task["session_id"] = outcome.get("session_id") or task.get("session_id")
        task["returncode"] = outcome.get("returncode")
        task["finished_at"] = outcome.get("finished_at", now())
        if outcome.get("timed_out"):
            task["status"] = "failed"
            task["last_error"] = "Codex worker timed out"
        elif outcome.get("returncode") != 0:
            task["status"] = "failed"
            task["last_error"] = (
                f"Codex worker exited with code {outcome.get('returncode')}"
            )
        else:
            result_text = "not reported"
            result_path = Path(task["worker_result"])
            if result_path.exists():
                result_text = result_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            try:
                finish_task(
                    root,
                    state,
                    key,
                    tests=result_text,
                    unresolved=[],
                    allow_noop=state["config"]["workers"]["allow_noop"],
                )
            except TangleError as exc:
                task["status"] = "failed"
                task["last_error"] = f"worker result failed validation: {exc}"
    write_task_result(root, key, task)
    return True


def cmd_poll(args: argparse.Namespace) -> None:
    root = repo_root()
    selected = task_id(args.task_id) if args.task_id else None
    with state_lock(root):
        state = load_state(root)
        if selected and selected not in state["tasks"]:
            raise TangleError(f"unknown task: {selected}")
        keys = [selected] if selected else sorted(state["tasks"])
        changed = False
        for key in keys:
            changed = poll_one(root, state, key) or changed
        if changed:
            save_state(root, state)
        print(
            json.dumps(
                {key: state["tasks"][key] for key in keys}, indent=2, sort_keys=True
            )
        )


def cmd_resume(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    with state_lock(root):
        state = load_state(root)
        task = state["tasks"].get(key)
        if not task:
            raise TangleError(f"unknown task: {key}")
        if task.get("status") not in {"failed", "review"}:
            raise TangleError(f"task {key} must be failed or in review before resume")
        mode = "resume" if task.get("session_id") else "new"
        task = launch_attempt(root, state, key, mode=mode, feedback=args.feedback)
        save_state(root, state)
        print(
            json.dumps(
                {"task": key, "status": task["status"], "pid": task["worker_pid"]},
                indent=2,
            )
        )


def kill_process_group(pid: int | None) -> None:
    if not pid_alive(pid):
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except ProcessLookupError:
        return


def cmd_cancel(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    with state_lock(root):
        state = load_state(root)
        task = state["tasks"].get(key)
        if not task:
            raise TangleError(f"unknown task: {key}")
        if task.get("status") not in {
            "ready",
            "running",
            "review",
            "accepted",
            "failed",
        }:
            raise TangleError(
                f"task {key} cannot be canceled from status {task.get('status')}"
            )
        if task.get("status") == "running":
            outcome_path = Path(task.get("outcome", ""))
            if outcome_path.is_file():
                poll_one(root, state, key)
                save_state(root, state)
                raise TangleError(
                    f"task {key} already finished with status {task.get('status')}; review it instead"
                )
            runtime_path = Path(task.get("runtime", ""))
            for _ in range(20):
                if runtime_path.is_file():
                    break
                time.sleep(0.05)
            if not runtime_path.is_file():
                raise TangleError(
                    "worker runtime could not be verified safely; run reconcile and retry"
                )
            runtime = read_json(runtime_path, label="worker runtime")
            if runtime.get("token") != task.get("launch_token") or runtime.get(
                "wrapper_pid"
            ) != task.get("worker_pid"):
                raise TangleError(
                    "worker runtime identity does not match; refusing to signal it"
                )
            kill_process_group(runtime.get("child_pid"))
            kill_process_group(runtime.get("wrapper_pid"))
        task["status"] = "canceled"
        task["canceled_at"] = now()
        task["last_error"] = args.reason
        save_state(root, state)
        write_task_result(root, key, task)
        print(key)


def cmd_cleanup(args: argparse.Namespace) -> None:
    root = repo_root()
    key = task_id(args.task_id)
    with state_lock(root):
        state = load_state(root)
        task = state["tasks"].get(key)
        if not task:
            raise TangleError(f"unknown task: {key}")
        if task.get("status") not in TERMINAL_STATUSES:
            raise TangleError(f"task {key} must be terminal before cleanup")
        raw_worktree = task.get("worktree")
        if raw_worktree:
            target = Path(raw_worktree)
            if target.exists() and dirty_paths(target):
                raise TangleError(
                    "refusing cleanup because the worker worktree is dirty"
                )
            if target.exists() or worktree_is_registered(root, target):
                git(root, "worktree", "remove", str(target))
            task["worktree"] = None
            task["cleaned_at"] = now()
        if args.delete_branch:
            if task.get("status") not in {"integrated", "canceled", "failed"}:
                raise TangleError(
                    "delete the branch only after integration, cancellation, or failure"
                )
            branch = task["branch"]
            if not branch.startswith("tangle/"):
                raise TangleError(
                    "refusing to delete a branch outside the tangle/ namespace"
                )
            if (
                subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                    cwd=root,
                    check=False,
                ).returncode
                == 0
            ):
                git(root, "branch", "-D", branch)
            task["branch_deleted_at"] = now()
        save_state(root, state)
        print(key)


def cmd_reconcile(_: argparse.Namespace) -> None:
    root = repo_root()
    with state_lock(root):
        state = load_state(root)
        changed = False
        for key in sorted(state["tasks"]):
            changed = poll_one(root, state, key) or changed
            task = state["tasks"][key]
            raw_worktree = task.get("worktree")
            if (
                raw_worktree
                and not Path(raw_worktree).exists()
                and task.get("status") not in TERMINAL_STATUSES
            ):
                task["status"] = "failed"
                task["last_error"] = "registered worktree is missing"
                changed = True
        git(root, "worktree", "prune")
        if changed:
            save_state(root, state)
        print(json.dumps(state["tasks"], indent=2, sort_keys=True))


def cmd_status(_: argparse.Namespace) -> None:
    root = repo_root()
    with state_lock(root, shared=True):
        state = load_state(root)
        print(
            json.dumps(
                {
                    "branch": state["invoking_branch"],
                    "base": state["base_commit"],
                    "snapshot": state.get("snapshot_commit"),
                    "tasks": state["tasks"],
                },
                indent=2,
                sort_keys=True,
            )
        )


def cmd_doctor(args: argparse.Namespace) -> None:
    root = repo_root()
    checks: dict[str, Any] = {
        "repository": str(root),
        "python": sys.version.split()[0],
        "git": git(root, "--version"),
    }
    state_file = state_paths(root)["state"]
    if state_file.exists():
        with state_lock(root, shared=True):
            config = load_state(root)["config"]
        checks["state"] = "valid"
    else:
        config, _ = load_config(root, args.config)
        checks["state"] = "not initialized"
    binary = codex_binary(config["workers"]["command"])
    proc = subprocess.run(
        [binary, "--version"], text=True, capture_output=True, check=False
    )
    if proc.returncode:
        raise TangleError(proc.stderr.strip() or "Codex CLI version check failed")
    checks["codex"] = proc.stdout.strip()
    checks["config"] = "valid"
    checks["ready"] = True
    print(json.dumps(checks, indent=2))


def internal_run_job(job_path: Path) -> int:
    job = read_json(job_path, label="worker job")
    token = str(job["token"])
    started = now()
    command = [str(item) for item in job["command"]]
    cwd = Path(job["cwd"])
    prompt = Path(job["prompt"])
    events = Path(job["events"])
    stderr = Path(job["stderr"])
    outcome = Path(job["outcome"])
    runtime = Path(job["runtime"])
    for path in (events, stderr, outcome, runtime):
        path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    returncode = 1
    atomic_json(
        runtime,
        {
            "token": token,
            "wrapper_pid": os.getpid(),
            "child_pid": None,
            "started_at": started,
        },
    )
    with (
        prompt.open("rb") as stdin,
        events.open("wb") as stdout,
        stderr.open("wb") as err,
    ):
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=err,
            start_new_session=True,
        )
        atomic_json(
            runtime,
            {
                "token": token,
                "wrapper_pid": os.getpid(),
                "child_pid": process.pid,
                "started_at": started,
            },
        )
        try:
            returncode = process.wait(
                timeout=int(job.get("timeout_seconds", 0)) or None
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            returncode = 124
    atomic_json(
        outcome,
        {
            "token": token,
            "returncode": returncode,
            "timed_out": timed_out,
            "session_id": parse_thread_id(events),
            "started_at": started,
            "finished_at": now(),
        },
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=f"Tangle {VERSION}")
    sub = result.add_subparsers(required=True)
    init = sub.add_parser("init", help="initialize local Tangle state safely")
    init.add_argument("--config", default="tangle.json")
    init.add_argument(
        "--force", action="store_true", help="reset only when no task worktrees exist"
    )
    init.set_defaults(func=cmd_init)
    validate = sub.add_parser(
        "validate-config", help="validate and print the effective config"
    )
    validate.add_argument("--config", default="tangle.json")
    validate.set_defaults(func=cmd_validate_config)
    configure = sub.add_parser(
        "configure", help="reload validated configuration without losing tasks"
    )
    configure.add_argument("--config", default="tangle.json")
    configure.set_defaults(func=cmd_configure)
    snap = sub.add_parser(
        "snapshot", help="capture the active session without mutating it"
    )
    snap.add_argument("--label", default="active-session")
    snap.set_defaults(func=cmd_snapshot)
    worker = sub.add_parser(
        "create-worker", help="create a dependency-ready isolated worktree"
    )
    worker.add_argument("task_id")
    worker.add_argument("--title", required=True)
    worker.add_argument("--owns", action="append", required=True)
    worker.add_argument("--depends-on", action="append", default=[])
    worker.add_argument("--acceptance", action="append", default=[])
    worker.add_argument("--test", action="append", default=[])
    worker.set_defaults(func=cmd_create_worker)
    launch = sub.add_parser("launch", help="launch a Codex worker asynchronously")
    launch.add_argument("task_id")
    launch.add_argument("--prompt")
    launch.add_argument("--prompt-file")
    launch.set_defaults(func=cmd_launch)
    poll = sub.add_parser("poll", help="collect completed worker outcomes")
    poll.add_argument("task_id", nargs="?")
    poll.set_defaults(func=cmd_poll)
    resume = sub.add_parser(
        "resume", help="resume a failed or review worker with feedback"
    )
    resume.add_argument("task_id")
    resume.add_argument("--feedback", required=True)
    resume.set_defaults(func=cmd_resume)
    cancel = sub.add_parser("cancel", help="stop or deliberately abandon a worker")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason", default="canceled by engineering lead")
    cancel.set_defaults(func=cmd_cancel)
    complete = sub.add_parser("complete", help="validate a manually completed worker")
    complete.add_argument("task_id")
    complete.add_argument("--tests", default="not reported")
    complete.add_argument("--unresolved", action="append", default=[])
    complete.add_argument("--allow-noop", action="store_true")
    complete.set_defaults(func=cmd_complete)
    accept = sub.add_parser("accept", help="record Claude's completed review")
    accept.add_argument("task_id")
    accept.add_argument("--review-note", default="reviewed and accepted by Claude")
    accept.add_argument("--allow-unresolved", action="store_true")
    accept.set_defaults(func=cmd_accept)
    integrate = sub.add_parser("integrate", help="apply only the accepted worker delta")
    integrate.add_argument("task_id")
    integrate.set_defaults(func=cmd_integrate)
    cleanup = sub.add_parser("cleanup", help="remove a terminal task worktree safely")
    cleanup.add_argument("task_id")
    cleanup.add_argument("--delete-branch", action="store_true")
    cleanup.set_defaults(func=cmd_cleanup)
    reconcile = sub.add_parser(
        "reconcile", help="repair stale worker status and Git metadata"
    )
    reconcile.set_defaults(func=cmd_reconcile)
    status = sub.add_parser("status", help="show orchestration state")
    status.set_defaults(func=cmd_status)
    doctor = sub.add_parser(
        "doctor", help="check local prerequisites and configuration"
    )
    doctor.add_argument("--config", default="tangle.json")
    doctor.set_defaults(func=cmd_doctor)
    return result


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "_run-job":
            return internal_run_job(Path(sys.argv[2]))
        arguments = parser().parse_args()
        return int(arguments.func(arguments) or 0)
    except (TangleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
