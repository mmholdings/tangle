# Claude Desktop and MCP setup

Tangle has two complementary Claude integrations:

- Project skills under `.claude/skills/` teach Claude the Plan → Implement → Review → Test → Release workflow.
- The Tangle MCP adapter gives Claude bounded tools for the same local orchestrator lifecycle.

Neither integration changes Claude's selected model or removes Claude's ability to inspect, edit, run, debug, and implement code directly.

## Mac prerequisites

- Claude Desktop with custom extension installation enabled.
- Git and Python 3.10 or newer.
- Codex CLI authenticated with the user's account. Tangle also detects the Codex executable bundled with ChatGPT for macOS.

Run the project installer first:

```bash
bash scripts/install.sh /absolute/path/to/project
cd /absolute/path/to/project
python3 .claude/tangle/tangle_orchestrator.py doctor --config tangle.json
```

This adds the Tangle skills and all three local runtime files to that project without overwriting its `tangle.json` on future forced updates.

On a low-memory Mac, leave `resources.adaptive_worker_limit` enabled. Tangle may reduce simultaneous Codex launches below `max_workers`; this does not change Claude's selected model or prevent Claude from coding directly.

To place worker checkouts on an external Mac volume, configure the project's `storage` block before initialization. Use a Mac-native filesystem such as APFS or HFS+, the exact `/Volumes/...` mount path and volume name, and optionally the UUID reported by `diskutil info`. Do not select an ExFAT drive for code worktrees. After editing `tangle.json`, run:

```bash
python3 .claude/tangle/tangle_orchestrator.py configure --config tangle.json
python3 .claude/tangle/tangle_orchestrator.py doctor --config tangle.json
```

If worktrees already exist, finish and clean them before changing storage. If a configured drive disconnects, reconnect the same drive and run `reconcile`; Tangle preserves its task records and does not fall back to the internal disk.

## Install in Claude Desktop

From the Tangle source repository:

```bash
python3 scripts/build_mcpb.py
```

The output is `dist/tangle.mcpb`.

1. Open Claude Desktop.
2. Open **Settings → Extensions → Advanced settings**.
3. Under Extension Developer, choose **Install Extension…**.
4. Select `dist/tangle.mcpb`.
5. For **Project folder**, select the exact root of the Git project installed above.
6. Enable the extension if Claude Desktop does not enable it automatically.

Start or resume a Claude coding session for that project and ask: “Check Tangle, initialize it if needed, and show the worker status.” Claude should see the `tangle_doctor`, `tangle_initialize`, and `tangle_status` tools.

The bundle is currently unsigned for private installation. Claude Desktop supports custom `.mcpb` installation through the developer controls. Production signing requires a code-signing certificate and is intentionally not faked with a self-signed release identity.

## Connect the adapter to Claude Code

If the project installer has already run, register the same local stdio server from the project root:

```bash
claude mcp add --scope project tangle -- \
  python3 "$PWD/.claude/tangle/tangle_mcp_server.py" \
  --repo "$PWD"
```

Verify it with:

```bash
claude mcp get tangle
```

Claude Code project MCP configuration and Claude Desktop extension configuration are independent. It is safe to use either or both; the orchestrator serializes state mutations with its local lock.

## Dashboard

Ask Claude to call `tangle_open_dashboard`, or run:

```bash
python3 .claude/tangle/tangle_dashboard.py --repo "$PWD" --open
```

The server chooses port 8765 for the direct command. Pass `--port 0` to choose an available port. It listens only on `127.0.0.1`, loads no remote assets, and can poll or reconcile state. Review acceptance and integration stay in Claude's MCP/CLI workflow.

Use the page's **Stop dashboard** button when finished.

## Update

Pull or download a newer Tangle release, then:

```bash
bash scripts/install.sh --force /absolute/path/to/project
python3 scripts/build_mcpb.py
```

Reinstall the new `dist/tangle.mcpb` in Claude Desktop. The forced project installer backs up existing Tangle skills and runtime files and preserves `tangle.json`.

## Troubleshooting

- **No Tangle tools:** confirm the extension is enabled and its Project folder is the Git root, then restart Claude Desktop.
- **Python not found:** install Python 3.10+ and confirm `python3 --version` works in Terminal.
- **Codex not found:** run `codex --version` or install/authenticate Codex CLI; on macOS Tangle also checks ChatGPT's bundled Codex executable.
- **Project rejected:** choose the Git top-level folder, not a subfolder. Tangle intentionally refuses broader or ambiguous access.
- **Dashboard did not open:** run the direct dashboard command and use the printed local URL. Logs from MCP-launched dashboards are under `.tangle/logs/dashboard.stderr.log`.
- **External storage offline:** reconnect the configured volume at the same mount path. Confirm its name and optional UUID in `tangle.json`, then run `doctor` and `reconcile`.
- **External filesystem rejected:** use APFS or HFS+ for Git worktrees. ExFAT remains suitable for large media archives, but not Tangle's checked-out code.
- **Low memory or disk warning:** close unused heavy applications, reduce `max_workers`, move worktrees to a supported external volume, or prune expired artifacts with `prune-runtime --dry-run` followed by `prune-runtime`.
