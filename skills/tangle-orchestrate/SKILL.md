---
name: tangle-orchestrate
description: Coordinate parallel Codex implementation workers from Claude using safe snapshots and Git worktrees
argument-hint: "feature, approved plan, or orchestration status request"
---

# Tangle Orchestrate

Act as engineering lead. Keep the user's selected Claude model and all Claude coding capabilities. This workflow is **Codex-first, not Codex-only**.

## Read first

Read every line of `docs/ARCHI.md` (project memory), the approved plan, `tangle.json` (or `tangle.example.json` for defaults), and the current Git status. Never discard or overwrite existing work.

## Decide whether to delegate

Delegate routine, separable, reviewable implementation to Codex. Claude may implement directly when the change is tiny, architecture-sensitive, high-risk, blocked by repeated Codex failure, involved in a merge/integration conflict, or explicitly assigned to Claude. State the reason in orchestration state. Do not switch Claude's model or forbid its native tools.

## Attach to the active session

If the tree is dirty, run `python3 scripts/tangle_orchestrator.py snapshot --label active-session`. This must include staged, unstaged, and nonignored untracked files while preserving the branch, HEAD, working files, and real index. Never use stash, reset, clean, checkout, or a temporary commit on the user's branch.

## Plan the worker pool

Break the work into tasks with IDs, dependencies, acceptance criteria, test commands, and non-overlapping ownership globs. Keep tightly coupled interfaces and implementations together. Do not delegate credentials, dashboard actions, subjective product decisions, destructive operations, or files Claude is actively editing.

Initialize state and create only dependency-ready workers:

```bash
python3 scripts/tangle_orchestrator.py init --config tangle.json
python3 scripts/tangle_orchestrator.py create-worker T1 --title "..." --owns 'src/example/**'
```

Never exceed `max_workers`. Give each Codex worker its worktree path, exact scope, plan, acceptance criteria, tests, prohibited paths, and this result contract: status, commit, files changed, tests, unresolved issues. Require it to commit; do not stream long reasoning back into Claude's context.

## While workers run

Claude may continue on unowned files. Check status without busy polling. If Claude must take a worker's files, stop that worker, record the handoff, release its ownership, then edit. On failure, make a bounded retry with precise feedback; after repeated failure, Claude may take the task.

## Review and integrate

For each completed worker: verify its commit, review the full diff against the snapshot/base, reject paths outside ownership, and run the specified tests. Integrate dependency-first into the invoking session branch. Resolve simple conflicts deliberately; for architectural conflicts, Claude takes ownership. Never blindly merge a worker branch.

After integration, run the project's full applicable gate and an independent Codex code review. Address findings until approved, then continue through `/tangle-3-release`.

## Safety

Do not push, merge, delete worktrees/branches/refs, or perform destructive cleanup without the workflow's explicit release/cleanup step. Treat `.tangle/` as local state. Warn that nonignored untracked files enter snapshots and secrets therefore belong in `.gitignore`.
