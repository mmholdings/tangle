# Tangle continuation prompt

Extend the existing Tangle v0.2.0 implementation; do not replace its safety model or rebuild it as a scaffold.

Tangle is local-first, Claude-led, and Codex-powered. Claude must retain the user's selected model, native tools, full coding authority, and project context. Keep the **Codex-first hybrid** policy: delegate large, routine, separable work to Codex while allowing Claude to implement directly when work is small, architecture-sensitive, high-risk, conflict-heavy, repeatedly blocked, or explicitly assigned to Claude.

Preserve these tested invariants:

1. Dirty-session snapshots use a temporary index and private ref. They never stash, checkout, stage, reset, clean, or commit on the user's branch.
2. State updates are atomic and locked across every read → Git mutation → write transaction.
3. Task ownership is non-overlapping while active and is enforced against the committed worker diff.
4. Dependencies must be accepted before scheduling and their accepted deltas must be composed into downstream private bases.
5. Workers run in isolated worktrees with bounded timeouts and retries. Cancellation must verify process identity before signaling.
6. A worker result remains in review until Claude validates and accepts it.
7. Integration applies only `task.base_commit..task.commit` to the active worktree, leaves it unstaged, preserves the real index, and never merges a dirty-session snapshot commit.
8. Runtime data stays under `.tangle/`; no hosted backend is required.
9. Public names, paths, commands, examples, and skill instructions remain under the Tangle brand.

Before accepting any extension, add behavior-focused tests for macOS and Linux, run the complete test suite, validate every shell script, scan for stale branding and unresolved template markers outside intentional initialization templates, and synchronize README.md, BUILDOUT.md, this prompt, docs/ARCHITECTURE.md, `tangle.example.json`, and `skills/tangle-orchestrate/SKILL.md`.

Suitable next projects are a stable `CodexAdapter` protocol with an MCP implementation, signed Claude Desktop Extension packaging, an optional read-only local dashboard, or a cross-platform locking/process backend for Windows. None may weaken Claude's authority or make a remote service mandatory.
