<p align="center">
  <img src="assets/tangle-banner.svg" alt="Tangle — Claude-led, Codex-powered engineering" width="100%">
</p>

# Tangle

Tangle is a local-first engineering workflow that lets Claude Desktop or Claude Code coordinate a pool of isolated Codex workers without giving up Claude's own coding abilities.

**Claude leads. Codex scales. You stay in control.**

## Why Tangle

An ordinary coding agent works serially in one checkout. Tangle separates leadership from execution:

- Claude keeps the full project conversation, makes architectural decisions, and may code directly.
- Codex handles routine, separable implementation in parallel worktrees.
- Every worker owns an explicit scope, commits its result, and returns a compact report.
- Claude reviews and integrates each result before an independent final review.

The policy is **Codex-first, not Codex-only**. Claude can implement directly when work is small, architecture-sensitive, high-risk, conflict-heavy, repeatedly blocked, or explicitly assigned to Claude.

<p align="center">
  <img src="assets/tangle-system.svg" alt="Tangle execution model" width="860">
</p>

## Core workflow

| Command | Purpose |
| --- | --- |
| `/tangle-init` | Learn the project and establish architectural memory |
| `/tangle-1-plan` | Discover requirements and produce a reviewed plan |
| `/tangle-orchestrate` | Split work across safe parallel Codex worktrees |
| `/tangle-2-implement` | Implement, test, and review a focused feature |
| `/tangle-3-release` | Synchronize docs, version, merge, tag, and push |

Supporting skills provide research, hotfix, testing, review, compaction, upgrades, and direct Codex operations.

## Install

1. Copy the contents of `skills/` into your project's `.claude/skills/` directory.
2. Copy `tangle.example.json` to `tangle.json` and adjust worker limits if needed.
3. Ensure Git, Python 3, and Codex CLI are installed and authenticated.
4. Run `/tangle-init YourProject` once, then use `/tangle-1-plan <feature>` or `/tangle-orchestrate <approved plan>`.

## Attach during an active coding session

Tangle can start after Claude has already made staged, unstaged, and untracked changes:

```bash
python3 scripts/tangle_orchestrator.py init --config tangle.json
python3 scripts/tangle_orchestrator.py snapshot --label active-session
python3 scripts/tangle_orchestrator.py create-worker T1 \
  --title "Implement billing API" \
  --owns 'src/billing/**'
python3 scripts/tangle_orchestrator.py status
```

The snapshot uses a temporary Git index and a private commit ref. It does not switch branches, touch the real staging area, stash files, or alter the active worktree. Each worker receives an isolated worktree based on the exact snapshot.

> Nonignored untracked files are included. Keep secrets and machine-local files in `.gitignore` before starting Tangle.

## Where it runs

Tangle runs on your computer because it needs local access to Git, source files, tests, build tools, and Codex. GitHub stores and distributes the project; it is not Tangle's backend. No Railway service, database, Redis instance, or hosted queue is required.

## Project status

The repository currently contains the workflow and a tested local orchestration foundation. Asynchronous Codex adapters, automated ownership enforcement, integration queues, and Desktop Extension packaging are tracked in [BUILDOUT.md](BUILDOUT.md).

See [the architecture](docs/ARCHITECTURE.md) and [build prompt](BUILD_PROMPT.md) for the complete design contract.

## Brand

Tangle's visual system uses a midnight field, electric violet, signal aqua, and coral. Intersecting paths represent independent agents converging on one reviewed result.

## Provenance

Tangle is an independently branded evolution of ideas first explored in an earlier open-source workflow. Historical provenance is recorded in [NOTICE.md](NOTICE.md); that project is not part of Tangle's product identity.
