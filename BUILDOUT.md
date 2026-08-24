# Tangle buildout

## Product contract

Tangle turns Claude Desktop or Claude Code into an engineering lead for parallel Codex execution. Claude retains its selected model, native tools, direct-edit authority, and full project context. Delegation expands capacity; it never restricts Claude.

## Delivered

- A complete Plan → Implement → Release skill system under the Tangle namespace.
- `/tangle-orchestrate` for decomposition, ownership, worker creation, review, and fallback policy.
- `scripts/tangle_orchestrator.py` for persistent state, safe dirty-tree snapshots, and isolated Git worktrees.
- `.tangle/` local runtime layout and `tangle/*` worker branches/private refs.
- Tangle configuration, architecture documentation, build contract, and visual identity.

## Runtime boundary

The core is local. GitHub provides source control and distribution. State lives under `.tangle/` and is excluded from commits. A future remote dashboard may consume sanitized task events, but it must not become required for execution or receive source code by default.

## Roadmap

1. Define a stable `CodexAdapter` interface with official CLI and MCP implementations.
2. Add asynchronous launch, status, resume, cancellation, structured results, and bounded retries.
3. Validate actual worker diffs against declared ownership before acceptance.
4. Add dependency-aware integration queues, conflict recovery, and project test gates.
5. Add independent final-review orchestration and auditable decision records.
6. Package the stabilized local service as a Claude Desktop Extension.

## Acceptance criteria

- Dirty-session attachment leaves the invoking branch, HEAD, files, and real index unchanged.
- Every worker has a unique branch, worktree, task contract, and declared ownership.
- Claude can continue coding on unowned files and can deliberately take back a worker task.
- Worker reports remain compact: status, commit, changed files, tests, and unresolved issues.
- No worker result is integrated without Claude review and applicable tests.
- The core requires no hosted infrastructure.
