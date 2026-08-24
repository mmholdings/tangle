# Tangle implementation prompt

Build Tangle as a local-first, Claude-led orchestration system for parallel Codex workers.

Claude remains the engineering lead and retains its selected model, native coding tools, subagents, full project context, and direct-edit authority. Apply a **Codex-first hybrid** policy: delegate large, routine, separable implementation to Codex; let Claude implement small changes, architecture-sensitive or high-risk work, integration conflicts, tasks after repeated worker failure, and anything explicitly assigned to Claude.

Required behavior:

1. Attach safely to an already-running dirty coding session. Capture staged, unstaged, and nonignored untracked files through a temporary Git index and private snapshot commit/ref. Never checkout, stash, stage, reset, clean, or commit on the user's branch.
2. Decompose an approved plan into dependency-aware tasks with explicit acceptance criteria, tests, and non-overlapping ownership globs.
3. Create one `tangle/*` Git branch and `.tangle/worktrees/*` worktree per worker from the active-session snapshot or clean HEAD.
4. Run Codex workers asynchronously through an adapter boundary supporting CLI and MCP implementations.
5. Require workers to commit and return only status, commit, files changed, tests, and unresolved issues.
6. Have Claude review diffs, enforce ownership, run tests, integrate dependency-first, and take ownership when direct implementation is more appropriate.
7. Run an independent Codex final review before release.
8. Store runtime data only under `.tangle/`. Do not require Railway, Redis, Postgres, or any remote control plane.
9. Add tests for snapshot non-mutation, secret/ignore behavior, worktree isolation, ownership enforcement, dependency scheduling, resumable state, and safe cleanup.

Keep Tangle's public names, commands, paths, documentation, and interface internally consistent. Before release, run all tests, inspect the final tree, and verify the GitHub default branch and displayed README.
