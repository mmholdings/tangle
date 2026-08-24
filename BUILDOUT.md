# Tangle buildout

## Product contract

Tangle turns Claude Desktop or Claude Code into the engineering lead for parallel Codex execution. Claude retains its selected model, native tools, direct-edit authority, and full project context. Delegation expands capacity; it never restricts Claude.

## Delivered in v0.2.0

- Complete Plan → Implement → Review → Test → Release skills under the Tangle namespace.
- Safe clean- or dirty-session attachment using a temporary Git index and private snapshot ref.
- Atomic, process-locked state with idempotent initialization and validated configuration reloads.
- Isolated, configurable worktree roots and unique `tangle/*` worker branches.
- Dependency validation and composition of accepted upstream changes into downstream worker bases.
- Conservative active ownership conflict detection and exact post-run changed-path enforcement.
- Asynchronous Codex CLI launch, polling, thread capture, bounded resume, portable timeout, and verified cancellation.
- Strict completion checks for worktree identity, branch identity, ancestry, clean status, non-empty commits, and ownership.
- Separate `review`, `accepted`, and `integrated` gates with unresolved-issue handling.
- Dirty-session-safe integration that applies only a worker's accepted delta and preserves Claude's real index.
- Safe cleanup and stale-state reconciliation.
- One-command project installer and a prerequisite doctor.
- Cross-platform standard-library tests and GitHub Actions on macOS/Linux with Python 3.10/3.12.

## Runtime boundary

The core is local. GitHub provides source control and distribution. State lives under `.tangle/` and is locally ignored. Tangle does not need or transmit source to a remote Tangle service.

## Acceptance status

| Criterion | Status |
| --- | --- |
| Dirty attachment leaves branch, HEAD, files, and real index unchanged | Tested |
| Concurrent commands cannot lose task records | Tested |
| Repeated initialization cannot erase tasks | Tested |
| Every worker has a unique branch, worktree, base, and ownership scope | Enforced |
| Dependencies must be accepted and are included in downstream bases | Enforced and tested |
| Uncommitted, no-op, divergent, or out-of-scope results are rejected | Enforced and tested |
| Claude review is required before integration | Enforced |
| Integration preserves active staged work and excludes snapshot history | Enforced and tested |
| Codex workers run asynchronously with bounded retry and timeout | Delivered and tested |
| Core requires no hosted infrastructure | Delivered |

## Optional future work

These are enhancements, not blockers for local use:

1. Add a Codex MCP adapter beside the current CLI adapter when a stable target interface is selected.
2. Package the installer and skills as a signed Claude Desktop Extension.
3. Add an optional local dashboard that reads sanitized task events without becoming part of execution.
4. Add Windows support using a non-`fcntl` locking backend and Windows process-group handling.

The repository intentionally does not claim a hosted service, automatic GitHub release workflow, or unattended merge authority.
