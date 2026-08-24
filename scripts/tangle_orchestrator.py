#!/usr/bin/env python3
"""Local state and Git-worktree foundation for Tangle Orchestrator."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(["git", *args], cwd=root, env=merged, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise SystemExit(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def repo_root() -> Path:
    return Path(git(Path.cwd(), "rev-parse", "--show-toplevel")).resolve()


def paths(root: Path) -> tuple[Path, Path]:
    state_dir = root / ".tangle" / "orchestrator"
    return state_dir, state_dir / "state.json"


def load_state(root: Path) -> dict:
    _, state_file = paths(root)
    if not state_file.exists():
        raise SystemExit("not initialized; run the init command first")
    return json.loads(state_file.read_text())


def save_state(root: Path, state: dict) -> None:
    state_dir, state_file = paths(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, state_file)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:40] or "task"


def snapshot(root: Path, label: str) -> str:
    head = git(root, "rev-parse", "HEAD")
    git_dir = Path(git(root, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    fd, index_name = tempfile.mkstemp(prefix="tangle-index-", dir=git_dir)
    os.close(fd)
    os.unlink(index_name)  # read-tree expects an absent or valid index
    env = {"GIT_INDEX_FILE": index_name}
    try:
        git(root, "read-tree", head, env=env)
        # Runtime state must never leak into a worker snapshot, even if the
        # host project forgot to add .tangle/ to its own .gitignore.
        git(root, "add", "-A", "--", ".", ":(exclude).tangle", env=env)
        tree = git(root, "write-tree", env=env)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        message = f"Tangle session snapshot: {label} ({stamp})"
        commit = git(root, "commit-tree", tree, "-p", head, env=env | {
            "GIT_AUTHOR_NAME": "Tangle Orchestrator",
            "GIT_AUTHOR_EMAIL": "tangle-orchestrator@local",
            "GIT_COMMITTER_NAME": "Tangle Orchestrator",
            "GIT_COMMITTER_EMAIL": "tangle-orchestrator@local",
        }, *["-m", message])
        ref = f"refs/tangle/snapshots/{stamp}-{slug(label)}"
        git(root, "update-ref", ref, commit)
        return commit
    finally:
        Path(index_name).unlink(missing_ok=True)


def cmd_init(args: argparse.Namespace) -> None:
    root = repo_root()
    config_path = (root / args.config).resolve()
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    state = {
        "version": 1,
        "repo": str(root),
        "invoking_branch": git(root, "branch", "--show-current") or None,
        "base_commit": git(root, "rev-parse", "HEAD"),
        "snapshot_commit": None,
        "config": config,
        "tasks": {},
    }
    for directory in (root / ".tangle" / "worktrees", root / ".tangle" / "logs", root / ".tangle" / "results"):
        directory.mkdir(parents=True, exist_ok=True)
    save_state(root, state)
    print(paths(root)[1])


def cmd_snapshot(args: argparse.Namespace) -> None:
    root = repo_root()
    state = load_state(root)
    commit = snapshot(root, args.label)
    state["snapshot_commit"] = commit
    save_state(root, state)
    print(commit)


def cmd_create_worker(args: argparse.Namespace) -> None:
    root = repo_root()
    state = load_state(root)
    task_id = args.task_id.upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]*", task_id):
        raise SystemExit("task id must contain only letters, digits, underscore, or hyphen")
    if task_id in state["tasks"]:
        raise SystemExit(f"task already exists: {task_id}")
    max_workers = int(state.get("config", {}).get("max_workers", 4))
    active = sum(t.get("status") in {"ready", "running"} for t in state["tasks"].values())
    if active >= max_workers:
        raise SystemExit(f"max_workers limit reached ({max_workers})")
    base = state.get("snapshot_commit") or state["base_commit"]
    worktree = root / ".tangle" / "worktrees" / task_id
    if worktree.exists():
        raise SystemExit(f"worktree path exists: {worktree}")
    branch = f"tangle/{task_id.lower()}-{slug(args.title)}"
    git(root, "worktree", "add", "-b", branch, str(worktree), base)
    state["tasks"][task_id] = {
        "title": args.title,
        "status": "ready",
        "branch": branch,
        "worktree": str(worktree),
        "owns": args.owns,
        "depends_on": args.depends_on,
        "base_commit": base,
    }
    save_state(root, state)
    print(json.dumps(state["tasks"][task_id], indent=2))


def cmd_complete(args: argparse.Namespace) -> None:
    root = repo_root()
    state = load_state(root)
    task_id = args.task_id.upper()
    task = state["tasks"].get(task_id)
    if not task:
        raise SystemExit(f"unknown task: {task_id}")
    worktree = Path(task["worktree"])
    task["commit"] = git(worktree, "rev-parse", "HEAD")
    task["status"] = "complete"
    task["tests"] = args.tests
    task["unresolved"] = args.unresolved
    save_state(root, state)
    print(task["commit"])


def cmd_status(_: argparse.Namespace) -> None:
    root = repo_root()
    state = load_state(root)
    summary = {"branch": state["invoking_branch"], "base": state["base_commit"],
               "snapshot": state.get("snapshot_commit"), "tasks": state["tasks"]}
    print(json.dumps(summary, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(required=True)
    init = sub.add_parser("init")
    init.add_argument("--config", default="tangle.json")
    init.set_defaults(func=cmd_init)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--label", default="active-session")
    snap.set_defaults(func=cmd_snapshot)
    worker = sub.add_parser("create-worker")
    worker.add_argument("task_id")
    worker.add_argument("--title", required=True)
    worker.add_argument("--owns", action="append", default=[])
    worker.add_argument("--depends-on", action="append", default=[])
    worker.set_defaults(func=cmd_create_worker)
    complete = sub.add_parser("complete")
    complete.add_argument("task_id")
    complete.add_argument("--tests", default="not reported")
    complete.add_argument("--unresolved", action="append", default=[])
    complete.set_defaults(func=cmd_complete)
    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.func(arguments)
