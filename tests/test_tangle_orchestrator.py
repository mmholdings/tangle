from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = PROJECT_ROOT / "scripts" / "tangle_orchestrator.py"
TOKEN_COUNTER = PROJECT_ROOT / "skills" / "tangle-compact" / "count-tokens.sh"
INSTALLER = PROJECT_ROOT / "scripts" / "install.sh"


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
        for key, value in overrides.items():
            if key.startswith("workers__"):
                config["workers"][key.split("__", 1)[1]] = value
            elif key.startswith("active_session__"):
                config["active_session"][key.split("__", 1)[1]] = value
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
output.write_text('tests: fake suite passed', encoding='utf-8')
print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-thread'}))
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        self.config(workers__command=str(fake), workers__timeout_seconds=30)
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


class UtilityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
