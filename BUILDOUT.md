# Tangle buildout

## Product contract

Tangle turns Claude Desktop or Claude Code into the engineering lead for parallel Codex execution. Claude retains its selected model, native tools, direct-edit authority, and full project context. Delegation expands capacity; it never restricts Claude.

## Delivered through v0.4.0

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
- A fixed-project MCP adapter with bounded lifecycle tools, JSON-RPC negotiation, structured results, timeouts, and no arbitrary command surface.
- A Claude Desktop MCP Bundle with current manifest metadata, project-directory configuration, bundled runtime files, Tangle icon, and deterministic packaging.
- A localhost-only dashboard with live state, automatic refresh, token-protected poll/reconcile actions, secure headers, and graceful shutdown.
- Explicit project-local or removable-volume worktree storage with mount, volume-name, optional UUID, filesystem, and free-space validation.
- Offline-drive reconciliation that preserves tasks and Git metadata instead of treating a disconnected volume as a deleted worktree.
- RAM-aware launch throttling, low-memory and low-disk preflight checks, and a conservative two-worker configured default.
- Streamed dependency/integration patches, bounded worker reports and logs, compact status payloads, bounded MCP/dashboard output, and visibility-aware dashboard polling.
- Retention-based runtime pruning restricted to cleaned terminal workers, with CLI and MCP dry-run support.

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
| Claude Desktop bundle is complete and reproducible | Tested |
| MCP cannot change its selected root or execute arbitrary commands | Enforced and tested |
| Dashboard binds to loopback and cannot approve/integrate | Enforced and tested |
| An offline or mismatched external volume cannot fall back to internal storage | Enforced and tested |
| ExFAT/FAT/NTFS volumes are rejected for Git worktrees | Enforced and tested |
| Small-memory systems automatically cap concurrent Codex launches | Enforced and tested |
| Large reports, logs, patches, MCP output, and dashboard output are bounded or streamed | Enforced and tested |
| Retention cleanup cannot remove active-worker artifacts | Enforced and tested |

## Optional future work

These are enhancements, not blockers for local use:

1. Sign release MCP Bundles with a production code-signing certificate when distribution expands beyond private installs.
2. Add automatic update metadata and a release workflow after a distribution policy is chosen.
3. Add Windows support using a non-`fcntl` locking backend and Windows process-group handling.
4. Add optional sanitized event export only if a concrete external observability need arises.

The repository intentionally does not claim a hosted service, automatic GitHub release workflow, a production-signed bundle, or unattended merge authority.
