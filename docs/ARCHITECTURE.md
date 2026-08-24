# Tangle architecture

## Operating model

```text
User
  └─ Claude Desktop / Claude Code
       ├─ architecture, planning, direct coding, debugging
       └─ Tangle
            ├─ Codex worker A → isolated worktree A
            ├─ Codex worker B → isolated worktree B
            └─ Codex reviewer  → integrated feature diff
```

Claude is always the engineering lead. Tangle adds an execution pool without replacing Claude's model, context, tools, or authority.

## Local state

```text
.tangle/
  orchestrator/state.json
  worktrees/<task-id>/
  results/<task-id>.json
  logs/<task-id>.log
```

State is written atomically and records the invoking branch, base/snapshot commit, task graph, ownership globs, worker branches, worktree paths, statuses, and results.

## Safe mid-session attachment

The helper creates a temporary Git index, seeds it from `HEAD`, adds the working tree under normal ignore rules, writes a tree, and creates a commit using `git commit-tree`. Only a private `refs/tangle/snapshots/*` ref and ignored `.tangle` state are added. The active branch, HEAD, files, and actual index remain unchanged.

Nonignored untracked files enter the snapshot so workers see the same project Claude sees. Sensitive machine-local files must be ignored before orchestration.

## Isolation and ownership

Each ready task receives a `tangle/<task-id>-<slug>` branch and `.tangle/worktrees/<task-id>` checkout. Ownership globs reserve areas during concurrent work. Integration compares each committed diff with those declarations and rejects unexpected paths. Dependencies must reach an accepted state before downstream tasks start.

## Integration

Workers commit on isolated branches and return compact results. Claude verifies the commit, reviews the complete diff, checks scope and ownership, and runs relevant tests. Claude then integrates in dependency order, resumes a worker with precise feedback, or takes ownership directly. An independent Codex thread reviews the integrated feature before release.

## Deployment boundary

The core runs locally because it requires source files, Git, processes, credentials, and build tools. GitHub hosts releases and collaboration. A hosted dashboard is optional and may receive sanitized events only; it is outside the execution path.
