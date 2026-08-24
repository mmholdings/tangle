---
name: tangle-orchestrate
description: Coordinate parallel Codex implementation workers from Claude using safe snapshots, review gates, and Git worktrees
argument-hint: "approved plan, feature, or orchestration status request"
---

# Tangle Orchestrate

Act as engineering lead. Keep the user's selected Claude model, project context, native tools, and direct coding ability. Tangle is **Codex-first, not Codex-only**.

## Prepare

Read `docs/ARCHI.md` when present, the approved plan or feature request, `tangle.json`, and the current Git status. Never discard or overwrite existing work.

Use the installed helper, falling back to the source-tree path only when developing Tangle itself:

```bash
TANGLE=.claude/tangle/tangle_orchestrator.py
[ -f "$TANGLE" ] || TANGLE=scripts/tangle_orchestrator.py
python3 "$TANGLE" doctor --config tangle.json
python3 "$TANGLE" init --config tangle.json
```

Read the doctor's `resources` result before launching workers. Respect the effective worker limit even when the configured limit is higher. If configured external storage is offline, has the wrong volume name or identity, or uses an unsuitable filesystem, pause orchestration and reconnect the intended volume. Do not replace it with a symlink or an unmounted directory under `/Volumes` or `/mnt`.

When Tangle MCP tools are connected, prefer their equivalent fixed-project operations for status and lifecycle actions. The CLI remains the fallback and the enforcement behavior is identical. Never substitute an arbitrary shell or file tool for a Tangle review-gated operation merely because MCP is unavailable.

If the tree is dirty, run `snapshot --label active-session`. The helper must preserve the branch, HEAD, files, and real index. Never use stash, reset, clean, checkout, or a temporary commit on the user's branch.

## Decide and decompose

Delegate routine, separable, reviewable implementation to Codex. Claude may implement directly for a tiny change, architecture-sensitive or high-risk work, integration conflicts, repeated Codex failure, or explicit user direction. Do not switch Claude's model or forbid its native tools.

Give every worker a task ID, title, acceptance criteria, test commands, dependencies, and non-overlapping ownership globs. Keep tightly coupled interfaces and implementations together. Do not delegate credentials, dashboard actions, subjective product decisions, destructive operations, or files Claude is actively editing.

Create only dependency-ready workers:

```bash
python3 "$TANGLE" create-worker T1 \
  --title "..." \
  --owns 'src/example/**' \
  --acceptance "..." \
  --test "..."
python3 "$TANGLE" launch T1
```

Use `--depends-on T1` for downstream work. The helper enforces the worker limit, active ownership, accepted dependencies, and dependency composition.

## Run and review

Let workers run asynchronously. Check with `poll [TASK]` or `status` without busy polling. Claude may continue on unowned files.

When a task reaches `review`:

1. Inspect the exact diff from the task's `base_commit` to `commit`.
2. Confirm the implementation meets the plan and no owned interface is accidentally broken.
3. Run the declared tests and any integration checks made necessary by the diff.
4. Use `accept TASK --review-note "..."` only after the review passes.
5. Use `resume TASK --feedback "..."` for a bounded correction attempt.

If Claude must take a task back, use `cancel TASK --reason "..."` before editing its owned files. After repeated worker failure, record the reason and implement directly.

## Integrate

Integrate dependencies first, then run `integrate TASK`. The helper revalidates the accepted commit and applies only the worker delta as unstaged changes on the invoking branch while preserving Claude's real index. Inspect the combined result before staging or committing it.

Run the full applicable project gate and an independent Codex code review after all accepted deltas are integrated. Address findings before `/tangle-3-release`.

Use `cleanup TASK --delete-branch` only after the result is integrated or deliberately abandoned. Use `prune-runtime --dry-run` before removing expired attempt artifacts when disk space is constrained; pruning is limited to cleaned terminal tasks. Do not push, tag, publish, or delete unrelated refs without the release workflow's authorization.

## Safety

Treat `.tangle/` as local runtime state. Nonignored untracked files enter snapshots by default, so secrets and large generated assets belong in `.gitignore`. For removable storage, use Tangle's explicit `storage.mode: external` configuration; it binds worktrees to a mounted volume name and optional UUID while leaving essential state with the project. If that volume disconnects, do not prune or recreate worktrees—reconnect it and run `reconcile`. Do not use `danger-full-access` for workers, bypass the `review` → `accepted` gate, or merge worker branches that descend from a dirty-session snapshot.
