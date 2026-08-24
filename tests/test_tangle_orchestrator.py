from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = PROJECT_ROOT / "scripts" / "tangle_orchestrator.py"
TOKEN_COUNTER = PROJECT_ROOT / "skills" / "tangle-compact" / "count-tokens.sh"
INSTALLER = PROJECT_ROOT / "scripts" / "install.sh"
MCP_SERVER = PROJECT_ROOT / "scripts" / "tangle_mcp_server.py"
DASHBOARD = PROJECT_ROOT / "scripts" / "tangle_dashboard.py"
MCPB_BUILDER = PROJECT_ROOT / "scripts" / "build_mcpb.py"


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tangle-test-")
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Tangle Tests")
        self.git("config", "user.email", "tangle-tests@local")
        (self.root / ".gitignore").write_text(
            ".tangle/\nsecret.env\n", encoding="utf-8"
        )
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "base")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            check=check,
            text=True,
            capture_output=True,
        )

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), *args],
            cwd=self.root,
            check=False,
            text=True,
            capture_output=True,
        )
        if check and result.returncode:
            self.fail(
                f"Tangle command failed ({result.returncode}): {' '.join(args)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def config(self, **overrides: object) -> Path:
        config = json.loads(
            (PROJECT_ROOT / "tangle.example.json").read_text(encoding="utf-8")
        )
        # Behavioral tests should not depend on transient CI host pressure.
        config["storage"]["minimum_free_gb"] = 0
        config["resources"]["minimum_available_memory_percent"] = 0
        for key, value in overrides.items():
            if key.startswith("workers__"):
                config["workers"][key.split("__", 1)[1]] = value
            elif key.startswith("active_session__"):
                config["active_session"][key.split("__", 1)[1]] = value
            elif key.startswith("storage__"):
                config["storage"][key.split("__", 1)[1]] = value
            elif key.startswith("resources__"):
                config["resources"][key.split("__", 1)[1]] = value
            else:
                config[key] = value
        path = self.root / "tangle.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def state(self) -> dict:
        return json.loads(
            (self.root / ".tangle" / "orchestrator" / "state.json").read_text(
                encoding="utf-8"
            )
        )

    def commit_worker(self, key: str, relative: str, content: str = "worker\n") -> Path:
        worktree = Path(self.state()["tasks"][key]["worktree"])
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", relative, cwd=worktree)
        self.git("commit", "-m", f"complete {key}", cwd=worktree)
        return worktree


class SnapshotTests(RepoCase):
    def test_snapshot_preserves_head_index_and_dirty_tree(self) -> None:
        self.config()
        self.cli("init")
        (self.root / "base.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "base.txt")
        (self.root / "base.txt").write_text("unstaged\n", encoding="utf-8")
        (self.root / "untracked.txt").write_text("visible\n", encoding="utf-8")
        (self.root / "secret.env").write_text("do-not-copy\n", encoding="utf-8")
        head_before = self.git("rev-parse", "HEAD").stdout
        index_before = self.git("write-tree").stdout
        status_before = self.git(
            "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout

        snapshot = self.cli("snapshot", "--label", "dirty-test").stdout.strip()

        self.assertEqual(head_before, self.git("rev-parse", "HEAD").stdout)
        self.assertEqual(index_before, self.git("write-tree").stdout)
        self.assertEqual(
            status_before,
            self.git("status", "--porcelain=v1", "--untracked-files=all").stdout,
        )
        self.assertEqual("unstaged\n", self.git("show", f"{snapshot}:base.txt").stdout)
        self.assertEqual(
            "visible\n", self.git("show", f"{snapshot}:untracked.txt").stdout
        )
        self.assertNotEqual(
            0,
            self.git(
                "cat-file", "-e", f"{snapshot}:secret.env", check=False
            ).returncode,
        )

    def test_snapshot_can_exclude_only_unstaged_untracked_files(self) -> None:
        self.config(active_session__include_untracked_nonignored=False)
        self.cli("init")
        (self.root / "staged-new.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "staged-new.txt")
        (self.root / "loose.txt").write_text("loose\n", encoding="utf-8")
        snapshot = self.cli("snapshot").stdout.strip()
        self.assertEqual(
            "staged\n", self.git("show", f"{snapshot}:staged-new.txt").stdout
        )
        self.assertNotEqual(
            0,
            self.git("cat-file", "-e", f"{snapshot}:loose.txt", check=False).returncode,
        )

    def test_dirty_snapshot_policy_is_enforced(self) -> None:
        self.config(active_session__snapshot_dirty_tree=False)
        self.cli("init")
        (self.root / "base.txt").write_text("dirty\n", encoding="utf-8")
        result = self.cli("snapshot", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("snapshot_dirty_tree is false", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class StateAndValidationTests(RepoCase):
    def test_init_is_idempotent_and_concurrent_creates_do_not_lose_state(self) -> None:
        self.config(workers__worktree_root="custom-workers")
        self.cli("init")
        command = [sys.executable, str(ORCHESTRATOR), "create-worker"]
        first = subprocess.Popen(
            [*command, "T1", "--title", "first", "--owns", "src/one/**"],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second = subprocess.Popen(
            [*command, "T2", "--title", "second", "--owns", "src/two/**"],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)
        self.assertEqual(0, first.returncode, first_stdout + first_stderr)
        self.assertEqual(0, second.returncode, second_stdout + second_stderr)
        state = self.state()
        self.assertEqual({"T1", "T2"}, set(state["tasks"]))
        self.assertIn("custom-workers", state["tasks"]["T1"]["worktree"])
        self.cli("init")
        self.assertEqual({"T1", "T2"}, set(self.state()["tasks"]))
        forced = self.cli("init", "--force", check=False)
        self.assertEqual(2, forced.returncode)
        self.assertIn("task worktrees exist", forced.stderr)

    def test_invalid_config_has_a_clear_error(self) -> None:
        self.config(max_workers="many")
        result = self.cli("validate-config", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("max_workers must be an integer", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_pre_v04_configs_receive_safe_resource_defaults(self) -> None:
        (self.root / "tangle.json").write_text('{"version": 1}\n', encoding="utf-8")
        result = json.loads(self.cli("validate-config").stdout)
        self.assertEqual("project", result["config"]["storage"]["mode"])
        self.assertTrue(result["config"]["resources"]["adaptive_worker_limit"])
        self.assertEqual(2, result["config"]["max_workers"])

    def test_configure_reloads_settings_without_resetting_tasks(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "one", "--owns", "src/one/**")
        self.config(max_workers=2)
        self.cli("configure")
        state = self.state()
        self.assertEqual(2, state["config"]["max_workers"])
        self.assertIn("T1", state["tasks"])
        self.config(max_workers=2, workers__worktree_root="moved-workers")
        moved = self.cli("configure", check=False)
        self.assertEqual(2, moved.returncode)
        self.assertIn("while task worktrees exist", moved.stderr)

    def test_missing_worktree_cannot_be_erased_by_reconfigure_or_force_init(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "one", "--owns", "src/**")
        target = Path(self.state()["tasks"]["T1"]["worktree"])
        self.git("worktree", "remove", "--force", str(target))
        self.config(workers__worktree_root="moved-workers")
        configured = self.cli("configure", check=False)
        self.assertEqual(2, configured.returncode)
        self.assertIn("while task worktrees exist", configured.stderr)
        forced = self.cli("init", "--force", check=False)
        self.assertEqual(2, forced.returncode)
        self.assertIn("task worktrees exist", forced.stderr)

    def test_dependencies_and_active_ownership_are_enforced(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "one", "--owns", "src/**")
        overlap = self.cli(
            "create-worker", "T2", "--title", "two", "--owns", "src/api/**", check=False
        )
        self.assertEqual(2, overlap.returncode)
        self.assertIn("overlaps active task", overlap.stderr)
        missing = self.cli(
            "create-worker",
            "T3",
            "--title",
            "three",
            "--owns",
            "docs/**",
            "--depends-on",
            "MISSING",
            check=False,
        )
        self.assertEqual(2, missing.returncode)
        self.assertIn("unknown dependency", missing.stderr)

        wildcard = self.cli(
            "create-worker",
            "T4",
            "--title",
            "wildcard",
            "--owns",
            "src/a*",
            check=False,
        )
        self.assertEqual(2, wildcard.returncode)
        self.assertIn("overlaps active task", wildcard.stderr)

    def test_worktree_root_cannot_escape_through_a_symlink(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (self.root / "linked-workers").symlink_to(outside, target_is_directory=True)
        self.config(workers__worktree_root="linked-workers")
        result = self.cli("init", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("inside the repository", result.stderr)

    def test_external_storage_configuration_requires_an_explicit_volume(self) -> None:
        self.config(storage__mode="external", storage__external_mount="relative")
        result = self.cli("validate-config", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("absolute mounted-volume path", result.stderr)

    def test_external_worktree_root_is_mount_checked_and_project_specific(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "tangle_orchestrator_storage_test", ORCHESTRATOR
        )
        assert specification and specification.loader
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        mount = Path(self.temporary.name) / "STORAGE 1"
        mount.mkdir()
        config = module.validate_config(
            {
                "storage": {
                    "mode": "external",
                    "external_mount": str(mount),
                    "external_volume_name": "STORAGE 1",
                    "external_volume_id": "volume-1",
                }
            }
        )
        metadata = {"name": "STORAGE 1", "id": "volume-1", "filesystem": "hfs"}
        with mock.patch.object(module.os.path, "ismount", return_value=True), mock.patch.object(
            module, "volume_metadata", return_value=metadata
        ):
            selected = module.worktree_root(self.root, config)
        self.assertTrue(selected.is_relative_to(mount.resolve()))
        self.assertIn("Tangle", selected.parts)
        self.assertIn(self.root.name, selected.parent.parent.name)

        with mock.patch.object(module.os.path, "ismount", return_value=True), mock.patch.object(
            module,
            "volume_metadata",
            return_value={"name": "STORAGE 1", "id": "volume-1", "filesystem": "exfat"},
        ):
            with self.assertRaises(module.ExternalStorageUnavailable):
                module.worktree_root(self.root, config)

    def test_reconcile_preserves_tasks_while_storage_is_offline(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "one", "--owns", "src/**")
        specification = importlib.util.spec_from_file_location(
            "tangle_orchestrator_reconcile_test", ORCHESTRATOR
        )
        assert specification and specification.loader
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        unavailable = {
            "available": False,
            "mode": "external",
            "mount": "/Volumes/OFFLINE",
            "reason": "external storage is offline",
            "minimum_free_gb": 5,
        }
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.object(module, "storage_health", return_value=unavailable):
                with contextlib.redirect_stdout(io.StringIO()):
                    module.cmd_reconcile(object())
                with self.assertRaises(module.ExternalStorageUnavailable):
                    module.cmd_poll(mock.Mock(task_id=None))
        finally:
            os.chdir(previous)
        self.assertEqual("ready", self.state()["tasks"]["T1"]["status"])


class ReviewAndIntegrationTests(RepoCase):
    def test_dirty_and_out_of_scope_worker_results_are_rejected(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "owned", "--owns", "owned/**")
        worktree = Path(self.state()["tasks"]["T1"]["worktree"])
        (worktree / "owned").mkdir()
        (worktree / "owned" / "file.txt").write_text("dirty\n", encoding="utf-8")
        dirty = self.cli("complete", "T1", check=False)
        self.assertEqual(2, dirty.returncode)
        self.assertIn("uncommitted changes", dirty.stderr)
        (worktree / "outside.txt").write_text("outside\n", encoding="utf-8")
        self.git("add", ".", cwd=worktree)
        self.git("commit", "-m", "bad scope", cwd=worktree)
        outside = self.cli("complete", "T1", check=False)
        self.assertEqual(2, outside.returncode)
        self.assertIn("outside ownership", outside.stderr)

    def test_single_star_does_not_own_nested_directories(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "shallow", "--owns", "src/*.py")
        self.commit_worker("T1", "src/deep/module.py")
        result = self.cli("complete", "T1", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("outside ownership", result.stderr)

    def test_review_gated_integration_preserves_active_index(self) -> None:
        self.config()
        self.cli("init")
        (self.root / "base.txt").write_text("active staged\n", encoding="utf-8")
        self.git("add", "base.txt")
        (self.root / "base.txt").write_text("active unstaged\n", encoding="utf-8")
        self.cli("snapshot")
        self.cli("create-worker", "T1", "--title", "owned", "--owns", "owned/**")
        self.commit_worker("T1", "owned/result.txt")
        self.cli("complete", "T1", "--tests", "unit tests passed")
        self.cli("accept", "T1", "--review-note", "diff reviewed")
        index_before = self.git("write-tree").stdout
        head_before = self.git("rev-parse", "HEAD").stdout
        self.cli("integrate", "T1")
        self.assertEqual(index_before, self.git("write-tree").stdout)
        self.assertEqual(head_before, self.git("rev-parse", "HEAD").stdout)
        self.assertEqual(
            "worker\n", (self.root / "owned" / "result.txt").read_text(encoding="utf-8")
        )
        self.assertEqual("integrated", self.state()["tasks"]["T1"]["status"])

        state = self.state()
        state["tasks"]["T1"]["status"] = "accepted"
        (self.root / ".tangle" / "orchestrator" / "state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.cli("integrate", "T1")
        self.assertTrue(self.state()["tasks"]["T1"]["integration_recovered"])

    def test_accepted_task_cannot_be_cleaned_before_abandonment(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "owned", "--owns", "owned/**")
        self.commit_worker("T1", "owned/result.txt")
        self.cli("complete", "T1")
        self.cli("accept", "T1")
        blocked = self.cli("cleanup", "T1", check=False)
        self.assertEqual(2, blocked.returncode)
        self.assertIn("must be terminal", blocked.stderr)
        self.cli("cancel", "T1", "--reason", "deliberately abandoned")
        self.cli("cleanup", "T1", "--delete-branch")
        self.assertIsNone(self.state()["tasks"]["T1"]["worktree"])

    def test_worker_reports_are_bounded_and_cleaned_artifacts_can_be_pruned(self) -> None:
        self.config(resources__max_worker_report_kb=1)
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "owned", "--owns", "owned/**")
        self.commit_worker("T1", "owned/result.txt")
        self.cli("complete", "T1", "--tests", "x" * 5000)
        report = self.state()["tasks"]["T1"]["tests"]
        self.assertLessEqual(len(report.encode("utf-8")), 1024)
        self.assertIn("report truncated", report)
        self.cli("accept", "T1")
        self.cli("integrate", "T1")
        self.cli("cleanup", "T1", "--delete-branch")
        artifact = self.root / ".tangle" / "logs" / "T1-attempt-1.stderr.log"
        artifact.write_text("old diagnostic", encoding="utf-8")

        preview = json.loads(
            self.cli("prune-runtime", "--older-than-days", "0", "--dry-run").stdout
        )
        self.assertEqual(1, preview["files"])
        self.assertTrue(artifact.is_file())
        removed = json.loads(
            self.cli("prune-runtime", "--older-than-days", "0").stdout
        )
        self.assertEqual(1, removed["files"])
        self.assertFalse(artifact.exists())

    def test_dependencies_are_composed_and_integrated_in_order(self) -> None:
        self.config()
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "library", "--owns", "lib/**")
        self.commit_worker("T1", "lib/value.txt", "dependency\n")
        self.cli("complete", "T1")
        self.cli("accept", "T1")
        self.cli(
            "create-worker",
            "T2",
            "--title",
            "consumer",
            "--owns",
            "app/**",
            "--depends-on",
            "T1",
        )
        second_tree = Path(self.state()["tasks"]["T2"]["worktree"])
        self.assertEqual(
            "dependency\n",
            (second_tree / "lib" / "value.txt").read_text(encoding="utf-8"),
        )
        self.commit_worker("T2", "app/consumer.txt", "uses dependency\n")
        self.cli("complete", "T2")
        self.cli("accept", "T2")
        blocked = self.cli("integrate", "T2", check=False)
        self.assertEqual(2, blocked.returncode)
        self.assertIn("integrate dependencies first", blocked.stderr)
        self.cli("integrate", "T1")
        self.cli("integrate", "T2")
        self.assertTrue((self.root / "lib" / "value.txt").is_file())
        self.assertTrue((self.root / "app" / "consumer.txt").is_file())


class WorkerAdapterTests(RepoCase):
    def test_running_worker_can_be_verified_and_canceled(self) -> None:
        fake = self.root / "slow-codex.sh"
        fake.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        fake.chmod(0o755)
        self.config(workers__command=str(fake), workers__timeout_seconds=60)
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "slow", "--owns", "owned/**")
        self.cli("launch", "T1")
        self.cli("cancel", "T1", "--reason", "test cancellation")
        self.assertEqual("canceled", self.state()["tasks"]["T1"]["status"])
        self.cli("cleanup", "T1", "--delete-branch")

    def test_async_worker_launch_reaches_review(self) -> None:
        fake = self.root / "fake-codex.py"
        fake.write_text(
            """#!/usr/bin/env python3
import json, pathlib, subprocess, sys
args = sys.argv[1:]
output = pathlib.Path(args[args.index('-o') + 1])
sys.stdin.read()
path = pathlib.Path('owned/worker.txt')
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text('from fake codex\\n', encoding='utf-8')
subprocess.run(['git', 'add', 'owned/worker.txt'], check=True)
subprocess.run(['git', 'commit', '-m', 'fake worker'], check=True, stdout=subprocess.DEVNULL)
output.write_text('tests: fake suite passed\\n' + ('x' * 5000), encoding='utf-8')
print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-thread'}))
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        self.config(
            workers__command=str(fake),
            workers__timeout_seconds=30,
            resources__max_worker_report_kb=1,
        )
        self.cli("init")
        self.cli("create-worker", "T1", "--title", "fake", "--owns", "owned/**")
        self.cli("launch", "T1")
        deadline = time.time() + 20
        while time.time() < deadline:
            self.cli("poll", "T1")
            if self.state()["tasks"]["T1"]["status"] != "running":
                break
            time.sleep(0.1)
        task = self.state()["tasks"]["T1"]
        self.assertEqual("review", task["status"], task.get("last_error"))
        self.assertEqual("fake-thread", task["session_id"])
        self.assertEqual(["owned/worker.txt"], task["changed_files"])
        self.assertLessEqual(len(task["tests"].encode("utf-8")), 1024)
        self.assertIn("report truncated", task["tests"])


class UtilityTests(unittest.TestCase):
    def test_adaptive_worker_limit_is_conservative_on_small_computers(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "tangle_orchestrator_resources_test", ORCHESTRATOR
        )
        assert specification and specification.loader
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        self.assertEqual(1, module.memory_worker_limit(8 * 1024**3))
        self.assertEqual(2, module.memory_worker_limit(16 * 1024**3))
        self.assertEqual(4, module.memory_worker_limit(64 * 1024**3))

    def test_codex_target_state_is_locked_per_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tangle-lock-") as temporary:
            state = Path(temporary) / "state"
            common = (
                PROJECT_ROOT / "skills" / "codex-plan-review" / "scripts" / "_common.sh"
            )
            environment = os.environ.copy()
            environment["STATE_DIR"] = str(state)
            holder = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'source "$1"; acquire_target_lock same-target; sleep 3',
                    "lock-holder",
                    str(common),
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.time() + 2
            while time.time() < deadline and not list((state / ".locks").glob("*/pid")):
                time.sleep(0.05)
            contender = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; acquire_target_lock same-target',
                    "lock-contender",
                    str(common),
                ],
                env=environment,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                75, contender.returncode, contender.stdout + contender.stderr
            )
            self.assertIn("another Codex operation", contender.stderr)
            holder.terminate()
            holder.communicate(timeout=5)

    def test_codex_timeout_is_bounded_without_extra_packages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tangle-timeout-") as temporary:
            temporary_path = Path(temporary)
            fake = temporary_path / "codex"
            fake.write_text("#!/bin/sh\nsleep 10\n", encoding="utf-8")
            fake.chmod(0o755)
            common = (
                PROJECT_ROOT / "skills" / "codex-plan-review" / "scripts" / "_common.sh"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{temporary_path}:{environment['PATH']}",
                    "CODEX_TIMEOUT": "1",
                    "STATE_DIR": str(temporary_path / "state"),
                }
            )
            started = time.monotonic()
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; set +e; codex_exec; rc=$?; echo "$rc"; exit "$rc"',
                    "timeout-test",
                    str(common),
                ],
                env=environment,
                check=False,
                text=True,
                capture_output=True,
                timeout=8,
            )
            self.assertEqual(124, result.returncode, result.stdout + result.stderr)
            self.assertLess(time.monotonic() - started, 7)
            self.assertIn("timed out", result.stderr)

    def test_skill_frontmatter_names_match_directories(self) -> None:
        for skill in sorted((PROJECT_ROOT / "skills").iterdir()):
            if not skill.is_dir():
                continue
            content = (skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(content.startswith("---\n"), skill.name)
            frontmatter = content.split("---\n", 2)[1]
            fields = {}
            for line in frontmatter.splitlines():
                if ":" in line and not line.startswith(" "):
                    key, value = line.split(":", 1)
                    fields[key] = value.strip().strip('"')
            self.assertEqual(skill.name, fields.get("name"), skill.name)
            self.assertTrue(fields.get("description"), skill.name)

    def test_installer_delivers_the_skills_helper_and_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tangle-install-") as temporary:
            target = Path(temporary) / "project"
            target.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=target,
                check=True,
                capture_output=True,
            )
            first = subprocess.run(
                ["bash", str(INSTALLER), str(target)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("Tangle installed", first.stdout)
            helper = target / ".claude" / "tangle" / "tangle_orchestrator.py"
            self.assertTrue(helper.is_file())
            self.assertTrue(
                (target / ".claude" / "tangle" / "tangle_mcp_server.py").is_file()
            )
            self.assertTrue(
                (target / ".claude" / "tangle" / "tangle_dashboard.py").is_file()
            )
            self.assertTrue(
                (
                    target / ".claude" / "skills" / "tangle-orchestrate" / "SKILL.md"
                ).is_file()
            )
            config = target / "tangle.json"
            self.assertTrue(config.is_file())
            validated = subprocess.run(
                [sys.executable, str(helper), "validate-config"],
                cwd=target,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertTrue(json.loads(validated.stdout)["valid"])
            duplicate = subprocess.run(
                ["bash", str(INSTALLER), str(target)],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(0, duplicate.returncode)
            installed_skill = (
                target / ".claude" / "skills" / "tangle-orchestrate" / "SKILL.md"
            )
            installed_skill.write_text("customized\n", encoding="utf-8")
            config_data = json.loads(config.read_text(encoding="utf-8"))
            config_data["max_workers"] = 2
            config.write_text(json.dumps(config_data), encoding="utf-8")
            forced = subprocess.run(
                ["bash", str(INSTALLER), "--force", str(target)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("backed up", forced.stdout)
            backups = list(
                (target / ".claude" / "tangle" / "backups").glob(
                    "*/skills/tangle-orchestrate/SKILL.md"
                )
            )
            self.assertEqual(1, len(backups))
            self.assertEqual("customized\n", backups[0].read_text(encoding="utf-8"))
            self.assertEqual(
                2, json.loads(config.read_text(encoding="utf-8"))["max_workers"]
            )

    def test_token_counter_handles_unicode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tangle-token-") as temporary:
            path = Path(temporary) / "unicode.md"
            path.write_text("Hello café — 世界 👋\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(TOKEN_COUNTER), str(path)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("tokens", result.stdout)
            self.assertNotIn("multibyte conversion failure", result.stderr)


class ProductLayerTests(RepoCase):
    def mcp_call(
        self, process: subprocess.Popen[str], request_id: int, method: str, params: dict | None = None
    ) -> dict:
        assert process.stdin is not None
        assert process.stdout is not None
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        self.assertTrue(line, f"MCP server exited while handling {method}")
        return json.loads(line)

    def test_mcp_adapter_initializes_and_exposes_scoped_tools(self) -> None:
        self.config()
        dashboard_url = ""
        dashboard_token = ""
        process = subprocess.Popen(
            [
                sys.executable,
                str(MCP_SERVER),
                "--repo",
                str(self.root),
                "--orchestrator",
                str(ORCHESTRATOR),
                "--dashboard",
                str(DASHBOARD),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            initialized = self.mcp_call(
                process,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "tests", "version": "1"},
                },
            )
            self.assertEqual("2025-11-25", initialized["result"]["protocolVersion"])
            tools = self.mcp_call(process, 2, "tools/list")["result"]["tools"]
            names = {tool["name"] for tool in tools}
            by_name = {tool["name"]: tool for tool in tools}
            self.assertIn("tangle_open_dashboard", names)
            self.assertIn("tangle_accept_worker", names)
            self.assertIn("tangle_prune_runtime", names)
            self.assertNotIn("shell", " ".join(names))
            self.assertTrue(by_name["tangle_status"]["annotations"]["readOnlyHint"])
            self.assertTrue(by_name["tangle_launch_worker"]["annotations"]["openWorldHint"])
            self.assertTrue(by_name["tangle_integrate_worker"]["annotations"]["destructiveHint"])

            init = self.mcp_call(
                process,
                3,
                "tools/call",
                {"name": "tangle_initialize", "arguments": {}},
            )["result"]
            self.assertFalse(init["isError"], init)
            status = self.mcp_call(
                process,
                4,
                "tools/call",
                {"name": "tangle_status", "arguments": {}},
            )["result"]
            self.assertEqual("main", status["structuredContent"]["result"]["branch"])
            pruned = self.mcp_call(
                process,
                40,
                "tools/call",
                {
                    "name": "tangle_prune_runtime",
                    "arguments": {"older_than_days": 0, "dry_run": True},
                },
            )["result"]
            self.assertFalse(pruned["isError"], pruned)
            self.assertEqual(0, pruned["structuredContent"]["result"]["files"])
            unknown = self.mcp_call(
                process,
                5,
                "tools/call",
                {"name": "run_shell", "arguments": {"command": "touch escaped"}},
            )["result"]
            self.assertTrue(unknown["isError"])
            self.assertFalse((self.root / "escaped").exists())

            opened = self.mcp_call(
                process,
                6,
                "tools/call",
                {
                    "name": "tangle_open_dashboard",
                    "arguments": {"port": 0, "open_browser": False},
                },
            )["result"]
            self.assertFalse(opened["isError"], opened)
            dashboard_url = opened["structuredContent"]["result"]["url"]
            with urllib.request.urlopen(dashboard_url, timeout=2) as response:
                markup = response.read().decode("utf-8")
            token_match = re.search(
                r'<meta name="tangle-token" content="([^"]+)">', markup
            )
            self.assertIsNotNone(token_match)
            dashboard_token = token_match.group(1) if token_match else ""
            reused = self.mcp_call(
                process,
                7,
                "tools/call",
                {
                    "name": "tangle_open_dashboard",
                    "arguments": {"port": 0, "open_browser": False},
                },
            )["result"]
            self.assertTrue(reused["structuredContent"]["result"]["reused"])
        finally:
            if dashboard_url and dashboard_token:
                shutdown = urllib.request.Request(
                    dashboard_url + "api/shutdown",
                    data=b"",
                    headers={"X-Tangle-Token": dashboard_token},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(shutdown, timeout=2):
                        pass
                except urllib.error.URLError:
                    pass
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=5)
            stderr = process.stderr.read() if process.stderr else ""
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            if process.returncode:
                self.fail(f"MCP server exited {process.returncode}: {stderr}")

    def test_local_dashboard_is_loopback_token_protected_and_review_safe(self) -> None:
        self.config()
        self.cli("init")
        info = self.root / ".tangle" / "dashboard-test.json"
        process = subprocess.Popen(
            [
                sys.executable,
                str(DASHBOARD),
                "--repo",
                str(self.root),
                "--orchestrator",
                str(ORCHESTRATOR),
                "--port",
                "0",
                "--write-info",
                str(info),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not info.is_file():
                time.sleep(0.05)
            if not info.is_file():
                if process.poll() is None:
                    self.fail("Dashboard stayed alive but did not write readiness metadata")
                stderr = process.stderr.read() if process.stderr else ""
                self.fail(
                    f"Dashboard exited {process.returncode} before readiness:\n{stderr}"
                )
            details = json.loads(info.read_text(encoding="utf-8"))
            url = details["url"]
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
            with urllib.request.urlopen(url, timeout=2) as response:
                markup = response.read().decode("utf-8")
                self.assertEqual("DENY", response.headers["X-Frame-Options"])
                self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertIn("document.hidden", markup)
            self.assertIn("10000", markup)
            match = re.search(r'<meta name="tangle-token" content="([^"]+)">', markup)
            self.assertIsNotNone(match)
            token = match.group(1) if match else ""
            with urllib.request.urlopen(url + "api/status", timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))
            self.assertTrue(state["initialized"])

            unauthorized = urllib.request.Request(
                url + "api/action",
                data=json.dumps({"action": "poll"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(unauthorized, timeout=2)
            self.assertEqual(403, denied.exception.code)
            denied.exception.close()

            forbidden_gate = urllib.request.Request(
                url + "api/action",
                data=json.dumps({"action": "accept"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Tangle-Token": token},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(forbidden_gate, timeout=2)
            self.assertEqual(400, rejected.exception.code)
            rejected.exception.close()

            shutdown = urllib.request.Request(
                url + "api/shutdown",
                data=b"",
                headers={"X-Tangle-Token": token},
                method="POST",
            )
            with urllib.request.urlopen(shutdown, timeout=2) as response:
                self.assertTrue(json.loads(response.read().decode("utf-8"))["ok"])
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def test_dashboard_binding_does_not_depend_on_reverse_dns(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "tangle_dashboard_test_module", DASHBOARD
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader if specification else None)
        module = importlib.util.module_from_spec(specification)
        assert specification and specification.loader
        specification.loader.exec_module(module)
        with mock.patch(
            "socket.getfqdn", side_effect=AssertionError("reverse DNS is forbidden")
        ):
            server = module.TangleHttpServer(
                ("127.0.0.1", 0), self.root, ORCHESTRATOR
            )
        try:
            self.assertEqual("127.0.0.1", server.server_name)
            self.assertGreater(server.server_port, 0)
        finally:
            server.server_close()

    def test_mcp_bundle_is_complete_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tangle-mcpb-") as temporary:
            first = Path(temporary) / "first.mcpb"
            second = Path(temporary) / "second.mcpb"
            for target in (first, second):
                subprocess.run(
                    [sys.executable, str(MCPB_BUILDER), "--output", str(target)],
                    cwd=PROJECT_ROOT,
                    check=True,
                    text=True,
                    capture_output=True,
                )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as bundle:
                self.assertEqual(
                    {
                        "icon.png",
                        "manifest.json",
                        "server/tangle_dashboard.py",
                        "server/tangle_mcp_server.py",
                        "server/tangle_orchestrator.py",
                    },
                    set(bundle.namelist()),
                )
                manifest = json.loads(bundle.read("manifest.json"))
                self.assertEqual("0.3", manifest["manifest_version"])
                self.assertEqual("0.4.0", manifest["version"])
                self.assertEqual("tangle", manifest["name"])
                self.assertEqual("server/tangle_mcp_server.py", manifest["server"]["entry_point"])


if __name__ == "__main__":
    unittest.main()
