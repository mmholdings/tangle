# Tangle architecture

## Operating model

```text
User
  └─ Claude Desktop / Claude Code (selected Claude model stays active)
       ├─ architecture, direct coding, debugging, review, integration
       └─ Tangle local runtime
            ├─ Codex worker A → branch + worktree A
            ├─ Codex worker B → branch + worktree B
            └─ Codex reviewer  → independent final review
```

Claude is always the engineering lead. Tangle adds a Codex execution pool; it does not replace Claude's model, context, tools, or authority.

## Components

- `.claude/skills/tangle-*` — Claude-facing planning, implementation, review, test, release, and orchestration workflows.
- `.claude/skills/codex-*` — focused Codex implementation and second-opinion wrappers.
- `.claude/tangle/tangle_orchestrator.py` — installed local runtime; the repository source is `scripts/tangle_orchestrator.py`.
- `tangle.json` — validated project configuration.
- Git branches, private refs, and worktrees — isolation and durable worker artifacts.

The runtime uses only the Python standard library and Git. `fcntl` state locking and Unix process groups make the current release a macOS/Linux runtime.

## Local state

```text
.tangle/
  orchestrator/
    state.json
    state.lock
    jobs/
    prompts/
  worktrees/<task-id>/
  results/<task-id>.json
  logs/<task-id>-attempt-<n>.*
```

Every mutating command holds a stable file lock across state loading, related Git mutations, and atomic state replacement. This prevents concurrent `create-worker`, polling, and review operations from losing updates. `init` is idempotent; an explicit force reset is refused while registered worktrees exist.

Initialization adds `.tangle/` to `.git/info/exclude`, keeping runtime files out of the user's status without changing the committed `.gitignore`.

## Safe active-session snapshot

The snapshot flow records HEAD, creates a temporary Git index in the repository's Git directory, seeds it from HEAD, adds the current working-tree view under normal ignore rules, writes a tree, and creates a private commit with `git commit-tree`. The temporary index is deleted afterward.

The active branch, HEAD, files, and real index remain unchanged. Staged, unstaged, and nonignored untracked files are included by default. Staged new files remain included when the user disables loose untracked files. `.tangle/` is always removed from the temporary index.

Sensitive machine-local files must be ignored. Snapshotting can be disabled for dirty trees through configuration, in which case Tangle fails closed.

## Task lifecycle

```text
ready → running → review → accepted → integrated
          │          │
          └→ failed ←┘ → resumed (bounded)
          └→ canceled
```

- `ready`: branch and worktree exist; dependencies and ownership were validated.
- `running`: an asynchronous Codex process owns the worktree.
- `review`: Codex exited successfully and the committed result passed mechanical validation.
- `accepted`: Claude reviewed the diff, tests, and unresolved issues.
- `integrated`: only the accepted worker delta was applied to the active worktree.
- `failed`/`canceled`: terminal or retryable outcomes retained for diagnosis and safe cleanup.

## Isolation, ownership, and dependencies

Each task declares at least one repo-relative ownership glob. Tangle conservatively rejects overlaps with active tasks. On completion it computes a no-renames changed-file set from the task's recorded base to worker HEAD and rejects every path outside ownership.

A dependency must exist and be accepted or integrated before a downstream worktree is created. Tangle applies each dependency's accepted delta into the downstream worktree and creates a private synthetic base commit. The downstream worker diff therefore contains only its own work, while its code view includes prerequisite work. Integration later requires dependencies first.

## Codex adapter

The current adapter launches authenticated Codex CLI sessions asynchronously. Each attempt receives a generated contract containing task scope, owned paths, acceptance criteria, tests, and prohibited behavior. Events, stderr, the final report, process identity, timeout outcome, and Codex thread ID are persisted locally.

Polling never trusts a successful process exit by itself. It runs the same branch, ancestry, cleanliness, non-no-op, and ownership validation used for manual completion. Resume reuses the captured Codex thread when available and observes the configured retry bound. Timeout handling and cancellation terminate the Codex process group; cancellation validates a per-launch token and both recorded process identities before signaling.

## Review and integration

Completion produces a compact result record but stops at `review`. Claude inspects the exact base-to-commit diff and runs the applicable project checks before invoking `accept`.

Integration revalidates that the worker has not changed since acceptance. It then applies a binary-capable patch for only `task.base_commit..task.commit` to Claude's invoking branch. The patch is applied without `--index`; Tangle verifies the active index tree is identical before and after. The combined change stays available for Claude to inspect, adjust, stage, and commit normally.

This delta approach is essential for mid-session attachment: merging the worker branch would also merge the private snapshot commit and could commit Claude's previously dirty work.

## Configuration and failure behavior

The runtime merges `tangle.json` over explicit defaults and rejects unknown keys, invalid types, unsafe paths, unsupported adapters, excessive worker limits, and dangerous worker sandboxes. `configure` reloads settings without resetting tasks and refuses to move the worktree root while worktrees exist.

Expected failures return concise errors without tracebacks. Cleanup refuses active or dirty worktrees. Branch deletion is separate and allowed only for integrated, canceled, or failed tasks. Reconciliation collects finished jobs, marks missing active worktrees failed, and prunes stale Git worktree metadata.

## Deployment and trust boundary

The execution core remains local because it needs source files, Git, credentials, processes, and build tools. GitHub hosts the repository and collaboration history. An optional future dashboard may consume sanitized events, but it must not receive source by default or become required for execution.
