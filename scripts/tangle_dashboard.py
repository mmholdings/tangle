#!/usr/bin/env python3
"""Serve Tangle's local-only project dashboard."""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any

VERSION = "0.4.0"
MAX_BODY_BYTES = 16_384
MAX_COMMAND_OUTPUT_BYTES = 1_000_000
ALLOWED_ACTIONS = {"poll", "reconcile"}


class DashboardError(RuntimeError):
    """Expected dashboard failure."""


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_repo(candidate: Path) -> Path:
    try:
        selected = candidate.expanduser().resolve(strict=True)
    except OSError as exc:
        raise DashboardError(f"Project directory is unavailable: {candidate}") from exc
    process = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=selected,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        raise DashboardError(process.stderr.strip() or "Project is not a Git repository")
    root = Path(process.stdout.strip()).resolve()
    if root != selected:
        raise DashboardError(f"Select the Git repository root: {root}")
    return root


def run_tangle(repo: Path, orchestrator: Path, arguments: list[str]) -> dict[str, Any]:
    with (
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout,
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr,
    ):
        try:
            process = subprocess.run(
                [sys.executable, str(orchestrator), *arguments],
                cwd=repo,
                text=True,
                stdout=stdout,
                stderr=stderr,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DashboardError("Tangle command exceeded 30 seconds") from exc
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read(MAX_COMMAND_OUTPUT_BYTES + 1)
        error_output = stderr.read(20_001)
    if len(output.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES:
        raise DashboardError("Tangle status is too large for the dashboard")
    if process.returncode:
        raise DashboardError(error_output.strip() or output.strip() or "Tangle command failed")
    output = output.strip()
    if not output:
        return {"ok": True}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"output": output}


def page(project_name: str, token: str) -> bytes:
    safe_name = html.escape(project_name)
    token_json = json.dumps(token)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="tangle-token" content="{html.escape(token, quote=True)}">
  <title>Tangle · {safe_name}</title>
  <style>
    :root {{ color-scheme: dark; --ink:#f8f7ff; --muted:#a7a3be; --panel:#151329; --line:#292541; --teal:#2dd4bf; --violet:#8b5cf6; --rose:#fb7185; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:radial-gradient(circle at 15% 0,#1d2440 0,transparent 35%),radial-gradient(circle at 90% 5%,#291742 0,transparent 32%),#090b16; color:var(--ink); font:15px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:42px 0 70px; }}
    header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:28px; }}
    .brand {{ display:flex; gap:16px; align-items:center; }}
    .mark {{ width:50px; height:50px; border-radius:15px; background:linear-gradient(135deg,var(--teal),var(--violet) 58%,var(--rose)); position:relative; box-shadow:0 0 38px #8b5cf655; }}
    .mark:after {{ content:""; position:absolute; inset:10px; border:4px solid #0a0b15; border-left-color:transparent; border-right-color:transparent; border-radius:50%; transform:rotate(35deg); }}
    h1 {{ margin:0; font-size:29px; letter-spacing:-.7px; }}
    .subtitle {{ color:var(--muted); margin-top:2px; }}
    .actions {{ display:flex; gap:9px; flex-wrap:wrap; justify-content:flex-end; }}
    button {{ border:1px solid var(--line); color:var(--ink); background:#1a1730; border-radius:10px; padding:9px 13px; cursor:pointer; font-weight:650; }}
    button:hover {{ border-color:#615b83; transform:translateY(-1px); }}
    button.danger {{ color:#ffc6cf; }}
    .summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:17px; }}
    .metric,.panel {{ background:linear-gradient(180deg,#17152bcc,#11101fcc); border:1px solid var(--line); box-shadow:0 20px 50px #0004; }}
    .metric {{ padding:18px; border-radius:14px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.11em; }}
    .metric strong {{ display:block; margin-top:4px; font-size:23px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .panel {{ border-radius:16px; overflow:hidden; }}
    .panel-head {{ padding:15px 18px; border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; }}
    .panel-head h2 {{ font-size:16px; margin:0; }}
    .stamp {{ color:var(--muted); font-size:12px; }}
    .empty {{ color:var(--muted); padding:42px 20px; text-align:center; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:13px 16px; text-align:left; border-bottom:1px solid #242039; vertical-align:top; }}
    th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.09em; }}
    tr:last-child td {{ border-bottom:0; }}
    .id {{ font-weight:780; }}
    .title {{ max-width:330px; }}
    .detail {{ color:var(--muted); font-size:12px; margin-top:3px; overflow-wrap:anywhere; }}
    .pill {{ display:inline-block; border:1px solid #3c3757; padding:3px 8px; border-radius:999px; font-size:12px; font-weight:700; }}
    .pill.running {{ color:#8eeede; border-color:#1e796d; }} .pill.review {{ color:#c7b8ff; border-color:#654bb5; }} .pill.failed,.pill.canceled {{ color:#ffadb9; border-color:#973c4a; }} .pill.integrated {{ color:#a9efbd; border-color:#367e49; }} .pill.accepted {{ color:#ffd89a; border-color:#86642d; }}
    #notice {{ min-height:23px; color:var(--muted); margin:0 0 9px; }}
    #notice.error {{ color:#ff9cab; }}
    @media(max-width:760px) {{ header {{ display:block; }} .actions {{ justify-content:flex-start; margin-top:18px; }} .summary {{ grid-template-columns:repeat(2,1fr); }} .panel {{ overflow-x:auto; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div class="brand"><div class="mark" aria-hidden="true"></div><div><h1>Tangle</h1><div class="subtitle">{safe_name} · local worker control room</div></div></div>
    <div class="actions"><button id="refresh">Refresh</button><button id="poll">Poll workers</button><button id="reconcile">Reconcile</button><button class="danger" id="stop">Stop dashboard</button></div>
  </header>
  <p id="notice" role="status"></p>
  <section class="summary" aria-label="Summary">
    <div class="metric"><span>Branch</span><strong id="branch">—</strong></div>
    <div class="metric"><span>Workers</span><strong id="workers">0</strong></div>
    <div class="metric"><span>Running</span><strong id="running">0</strong></div>
    <div class="metric"><span>Needs review</span><strong id="review">0</strong></div>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>Worker tasks</h2><span class="stamp" id="updated">Waiting for state…</span></div>
    <div id="empty" class="empty">Loading local Tangle state…</div>
    <table id="table" hidden><thead><tr><th>Task</th><th>Status</th><th>Scope</th><th>Attempts</th><th>Outcome</th></tr></thead><tbody id="rows"></tbody></table>
  </section>
</main>
<script nonce="{html.escape(token, quote=True)}">
const TOKEN = {token_json};
const q = (id) => document.getElementById(id);
const text = (value) => value === null || value === undefined || value === "" ? "—" : String(value);
function setNotice(message, isError=false) {{ q("notice").textContent = message || ""; q("notice").className = isError ? "error" : ""; }}
function node(tag, value, className="") {{ const el=document.createElement(tag); el.textContent=text(value); if(className) el.className=className; return el; }}
async function request(path, options={{}}) {{
  const response=await fetch(path, {{cache:"no-store", ...options}});
  const body=await response.json();
  if(!response.ok) throw new Error(body.error || `Request failed (${{response.status}})`);
  return body;
}}
function render(state) {{
  const tasks=state.tasks || {{}};
  const entries=Object.entries(tasks).sort(([a],[b]) => a.localeCompare(b));
  q("branch").textContent=text(state.branch);
  q("workers").textContent=entries.length;
  q("running").textContent=entries.filter(([,t]) => t.status === "running").length;
  q("review").textContent=entries.filter(([,t]) => t.status === "review").length;
  q("updated").textContent=`Updated ${{new Date().toLocaleTimeString()}}`;
  const warnings=(state.resources && state.resources.warnings) || [];
  if(state.storage && !state.storage.available) warnings.unshift(state.storage.reason || "Configured worktree storage is offline.");
  q("rows").replaceChildren();
  q("empty").hidden=entries.length > 0; q("table").hidden=entries.length === 0;
  if(!entries.length) q("empty").textContent="No workers yet. Ask Claude to create a scoped Tangle worker.";
  for(const [id,task] of entries) {{
    const tr=document.createElement("tr");
    const taskCell=document.createElement("td"); taskCell.append(node("div",id,"id"),node("div",task.title,"detail title"));
    const statusCell=document.createElement("td"); statusCell.append(node("span",task.status,`pill ${{task.status || ""}}`));
    const scopeCell=document.createElement("td"); scopeCell.append(node("div",(task.owns || []).join(", "),"detail"));
    const attemptCell=node("td",task.attempts || 0);
    const outcomeCell=document.createElement("td");
    const outcome=task.last_error || task.tests || ((task.changed_files || []).length ? `${{task.changed_files.length}} file(s)` : "—");
    outcomeCell.append(node("div",outcome,"detail"));
    tr.append(taskCell,statusCell,scopeCell,attemptCell,outcomeCell); q("rows").append(tr);
  }}
  return [...new Set(warnings)].join(" · ");
}}
async function refresh(clearNotice=false) {{
  try {{ const body=await request("/api/status"); if(!body.initialized) {{ q("empty").textContent=body.error; setNotice("Ask Claude to initialize Tangle in this project.",true); return; }} const warning=render(body.state); if(warning) setNotice(warning,true); else if(clearNotice) setNotice(""); }}
  catch(error) {{ setNotice(error.message,true); }}
}}
async function action(name) {{
  setNotice(`${{name[0].toUpperCase()+name.slice(1)}} in progress…`);
  try {{ await request("/api/action",{{method:"POST",headers:{{"Content-Type":"application/json","X-Tangle-Token":TOKEN}},body:JSON.stringify({{action:name}})}}); await refresh(false); setNotice(`${{name[0].toUpperCase()+name.slice(1)}} complete.`); }}
  catch(error) {{ setNotice(error.message,true); }}
}}
q("refresh").addEventListener("click",()=>refresh(true)); q("poll").addEventListener("click",()=>action("poll")); q("reconcile").addEventListener("click",()=>action("reconcile"));
q("stop").addEventListener("click",async()=>{{ if(!confirm("Stop this local Tangle dashboard?")) return; try {{ await request("/api/shutdown",{{method:"POST",headers:{{"X-Tangle-Token":TOKEN}}}}); document.body.replaceChildren(node("main","Tangle dashboard stopped.")); }} catch(error) {{ setNotice(error.message,true); }} }});
refresh(true); setInterval(()=>{{ if(!document.hidden) refresh(false); }},10000);
</script>
</body>
</html>""".encode("utf-8")


class TangleHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer's default binding performs a reverse-DNS lookup for its
        # display name. That can stall for several seconds on otherwise valid
        # loopback-only macOS hosts, delaying the readiness file. Tangle never
        # needs an external hostname, so retain the numeric address.
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])

    def __init__(self, address: tuple[str, int], repo: Path, orchestrator: Path) -> None:
        super().__init__(address, DashboardHandler)
        self.repo = repo
        self.orchestrator = orchestrator
        self.token = secrets.token_urlsafe(32)
        self.started_at = datetime.now(timezone.utc).isoformat()


class DashboardHandler(BaseHTTPRequestHandler):
    server: TangleHttpServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"Tangle dashboard: {self.address_string()} {format % args}", file=sys.stderr)

    def allowed_host(self) -> bool:
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0].strip("[]").lower()
        return hostname in {"127.0.0.1", "localhost", "::1"}

    def common_headers(self, *, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Security-Policy", f"default-src 'self'; style-src 'unsafe-inline'; script-src 'nonce-{self.server.token}'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def respond_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.common_headers(content_type=content_type, length=len(body))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.respond_bytes(status, body, "application/json; charset=utf-8")

    def reject_bad_host(self) -> bool:
        if self.allowed_host():
            return False
        self.respond_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Invalid local Host header"})
        return True

    def authenticated(self) -> bool:
        supplied = self.headers.get("X-Tangle-Token", "")
        return secrets.compare_digest(supplied, self.server.token)

    def do_GET(self) -> None:
        if self.reject_bad_host():
            return
        route = urllib.parse.urlsplit(self.path).path
        if route == "/":
            self.respond_bytes(HTTPStatus.OK, page(self.server.repo.name, self.server.token), "text/html; charset=utf-8")
            return
        if route == "/api/health":
            self.respond_json(
                HTTPStatus.OK,
                {"ok": True, "version": VERSION, "repo": str(self.server.repo), "pid": os.getpid()},
            )
            return
        if route == "/api/status":
            try:
                state = run_tangle(self.server.repo, self.server.orchestrator, ["status"])
                self.respond_json(HTTPStatus.OK, {"ok": True, "initialized": True, "state": state})
            except DashboardError as exc:
                message = str(exc)
                if "not initialized" in message:
                    self.respond_json(HTTPStatus.OK, {"ok": True, "initialized": False, "error": message})
                else:
                    self.respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": message})
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        if self.reject_bad_host():
            return
        if not self.authenticated():
            self.respond_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Invalid dashboard token"})
            return
        route = urllib.parse.urlsplit(self.path).path
        if route == "/api/shutdown":
            self.respond_json(HTTPStatus.OK, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if route != "/api/action":
            self.respond_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        try:
            raw_length = self.headers.get("Content-Length", "0")
            length = int(raw_length)
            if length < 0 or length > MAX_BODY_BYTES:
                raise DashboardError("Request body is too large")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or set(body) != {"action"}:
                raise DashboardError("Action request must contain only 'action'")
            action = body.get("action")
            if action not in ALLOWED_ACTIONS:
                raise DashboardError("Dashboard actions are limited to poll and reconcile")
            result = run_tangle(self.server.repo, self.server.orchestrator, [str(action)])
            self.respond_json(HTTPStatus.OK, {"ok": True, "result": result})
        except (DashboardError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.respond_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", type=Path, default=Path.cwd())
    result.add_argument(
        "--orchestrator",
        type=Path,
        default=Path(__file__).with_name("tangle_orchestrator.py"),
    )
    result.add_argument("--port", type=int, default=8765, help="0 chooses an available port")
    result.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    result.add_argument("--write-info", type=Path, help="write local server metadata after binding")
    result.add_argument("--version", action="version", version=f"Tangle Dashboard {VERSION}")
    return result


def main() -> int:
    info_path: Path | None = None
    try:
        args = parser().parse_args()
        if not 0 <= args.port <= 65535:
            raise DashboardError("--port must be between 0 and 65535")
        repo = resolve_repo(args.repo)
        orchestrator = args.orchestrator.expanduser().resolve(strict=True)
        server = TangleHttpServer(("127.0.0.1", args.port), repo, orchestrator)
        url = f"http://127.0.0.1:{server.server_port}/"
        info_path = args.write_info.expanduser().resolve() if args.write_info else None
        if info_path:
            atomic_json(
                info_path,
                {"url": url, "pid": os.getpid(), "repo": str(repo), "started_at": server.started_at, "version": VERSION},
            )
        print(url, flush=True)
        if args.open:
            threading.Timer(0.15, lambda: webbrowser.open(url)).start()
        try:
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
        finally:
            server.server_close()
        return 0
    except (DashboardError, OSError) as exc:
        print(f"Tangle dashboard: {exc}", file=sys.stderr)
        return 2
    finally:
        if info_path:
            info_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
