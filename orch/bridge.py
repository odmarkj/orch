"""
orch mobile bridge.

A lightweight HTTP server that makes orch accessible from your phone or iPad
via Termius SSH or a browser through a Cloudflare tunnel.

Run with:  orch bridge
Or in TUI: press 'b' to start/stop

Endpoints:
  GET  /              Mobile dashboard HTML
  GET  /api/projects  All project snapshots as JSON
  GET  /api/plan      Latest day plan (cached, regenerated on request)
  POST /api/task      Add a todo to a project  { project, task }
  POST /api/stage     Advance project stage    { project, stage, note }
  GET  /api/status    Quick status for all projects (lightweight)

SSH access (preferred for Termius):
  The bridge runs on localhost only. Termius SSH key auth connects to your Mac
  and you run 'orch' directly in the terminal — full functionality, no bridge
  needed. The bridge is for browser-based access (iPad Safari, etc).

Cloudflare tunnel:
  cloudflared tunnel --url http://localhost:7777
  Add to ~/.orch/config.toml:
    [bridge]
    port = 7777
    tunnel_domain = "orch.yourdomain.com"
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from .models import Project

from .lifecycle import load as load_lifecycle, advance_stage, STAGE_EMOJI, STAGES
from .discovery import discover_projects


# ── Config ────────────────────────────────────────────────────────────────────

def _bridge_config() -> dict:
    defaults = {"port": 7777, "tunnel_domain": ""}
    config_file = Path.home() / ".orch" / "config.toml"
    if not config_file.exists():
        return defaults
    section = None
    for raw in config_file.read_text().splitlines():
        line = raw.strip()
        if line == "[bridge]":
            section = "bridge"
            continue
        if line.startswith("["):
            section = None
            continue
        if section == "bridge" and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key == "port":
                try:
                    defaults["port"] = int(val)
                except ValueError:
                    pass
            elif key == "tunnel_domain":
                defaults["tunnel_domain"] = val
    return defaults


# ── API helpers ───────────────────────────────────────────────────────────────

def _project_summary(project: "Project") -> dict:
    lc = load_lifecycle(project)
    return {
        "name":           project.name,
        "path":           str(project.path),
        "stage":          lc.stage,
        "stage_emoji":    lc.stage_emoji,
        "description":    lc.description,
        "days_in_stage":  lc.days_in_current_stage,
        "is_stalled":     lc.is_stalled,
        "stall_score":    round(lc.stall_score, 2),
        "launch_debt":    lc.launch_debt,
        "pending_todos":  project.pending_count,
        "claude_status":  project.current_status,
        "status_indicator": project.status_indicator,
        "ledger": [
            {"date": str(e.date), "stage": e.stage, "note": e.note}
            for e in lc.ledger
        ],
    }


def _todos_data(project: "Project") -> dict:
    text = project.todos_text
    pending, done, in_progress = [], [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- [ ]"):
            pending.append(s[5:].strip())
        elif s.startswith("- [x]") or s.startswith("- [X]"):
            done.append(s[5:].strip())
        elif s.startswith("- [~]"):
            in_progress.append(s[5:].strip())
    return {"pending": pending, "in_progress": in_progress, "done": done}


def _add_todo(project: "Project", task: str) -> None:
    todos_file = project.todos_file
    if todos_file.exists():
        text = todos_file.read_text()
        # Insert after ## Pending header if it exists, else prepend
        if "## Pending" in text:
            text = text.replace(
                "## Pending\n",
                f"## Pending\n- [ ] {task}\n",
                1,
            )
        else:
            text = f"## Pending\n- [ ] {task}\n\n" + text
    else:
        text = f"## Pending\n- [ ] {task}\n"
    todos_file.write_text(text)


# ── Mobile HTML UI ────────────────────────────────────────────────────────────

MOBILE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>orch</title>
<style>
  :root {
    --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
    --border: #30363d; --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --yellow: #d29922; --blue: #58a6ff;
    --teal: #39d353; --orange: #f78166; --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font: 15px/1.5 -apple-system, sans-serif;
         max-width: 680px; margin: 0 auto; padding: env(safe-area-inset-top) 0 80px; }
  header { background: var(--bg2); border-bottom: 1px solid var(--border);
           padding: 14px 16px; display: flex; align-items: center; justify-content: space-between;
           position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 18px; font-weight: 600; letter-spacing: -0.3px; }
  header time { font-size: 12px; color: var(--muted); }
  .tab-bar { display: flex; background: var(--bg2); border-bottom: 1px solid var(--border);
             position: sticky; top: 53px; z-index: 9; }
  .tab { flex: 1; padding: 10px; text-align: center; font-size: 13px; color: var(--muted);
         cursor: pointer; border-bottom: 2px solid transparent; transition: all .15s; }
  .tab.active { color: var(--blue); border-bottom-color: var(--blue); }
  .pane { display: none; padding: 12px; }
  .pane.active { display: block; }
  .card { background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
          padding: 14px; margin-bottom: 10px; }
  .card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .card-title { font-weight: 600; font-size: 15px; }
  .badge { font-size: 11px; padding: 2px 7px; border-radius: 20px; font-weight: 500; }
  .badge-building { background: #1c2d1e; color: var(--green); }
  .badge-mvp      { background: #1e1c2d; color: var(--purple); }
  .badge-staging  { background: #1c2330; color: var(--blue); }
  .badge-live     { background: #1e2d1c; color: var(--teal); }
  .badge-maintaining { background: #2d2418; color: var(--yellow); }
  .badge-idea     { background: #2d2418; color: var(--muted); }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .dot-active  { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot-waiting { background: var(--yellow); }
  .dot-idle    { background: var(--border); }
  .status-text { font-size: 13px; color: var(--muted); margin-top: 4px; }
  .meta { font-size: 12px; color: var(--muted); display: flex; gap: 12px; flex-wrap: wrap; margin-top: 6px; }
  .stalled { color: var(--orange); }
  .todo-list { list-style: none; margin-top: 10px; }
  .todo-list li { padding: 8px 0; border-top: 1px solid var(--border);
                  font-size: 13px; display: flex; align-items: flex-start; gap: 8px; }
  .todo-list li:first-child { border-top: none; }
  .checkbox { width: 16px; height: 16px; border: 1.5px solid var(--border); border-radius: 4px;
              flex-shrink: 0; margin-top: 1px; }
  .add-task { width: 100%; background: var(--bg3); border: 1px solid var(--border);
              border-radius: 8px; padding: 10px 12px; color: var(--text); font-size: 15px;
              margin-top: 10px; }
  .add-task:focus { outline: none; border-color: var(--blue); }
  .btn { background: var(--blue); color: #fff; border: none; border-radius: 8px;
         padding: 10px 16px; font-size: 14px; font-weight: 500; cursor: pointer;
         width: 100%; margin-top: 8px; -webkit-tap-highlight-color: transparent; }
  .btn:active { opacity: .8; }
  .btn-sm { background: var(--bg3); border: 1px solid var(--border); color: var(--text);
            border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
  .plan-item { border-left: 3px solid var(--blue); padding: 12px 12px 12px 14px;
               background: var(--bg2); border-radius: 0 8px 8px 0; margin-bottom: 10px; }
  .plan-priority { font-size: 11px; color: var(--muted); }
  .plan-headline { font-weight: 600; margin: 4px 0; }
  .plan-rationale { font-size: 13px; color: var(--muted); }
  .plan-tasks { margin-top: 8px; }
  .plan-tasks li { font-size: 13px; color: var(--text); padding: 3px 0; }
  .one-liner { background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
               padding: 14px; margin-bottom: 14px; font-size: 15px; line-height: 1.6;
               font-style: italic; color: var(--muted); }
  .warning-banner { background: #2d1f0e; border: 1px solid #6e3e0f; border-radius: 8px;
                    padding: 10px 14px; margin-bottom: 10px; font-size: 13px; color: var(--orange); }
  .stage-pills { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
  .stage-pill { padding: 5px 10px; border-radius: 20px; font-size: 12px; cursor: pointer;
                background: var(--bg3); border: 1px solid var(--border); color: var(--muted); }
  .stage-pill.current { border-color: var(--blue); color: var(--blue); }
  .loading { text-align: center; padding: 40px; color: var(--muted); }
  .empty { text-align: center; padding: 30px; color: var(--muted); font-size: 14px; }
  select.stage-select { background: var(--bg3); border: 1px solid var(--border); color: var(--text);
                        border-radius: 6px; padding: 8px 10px; font-size: 14px; width: 100%; margin-top: 8px; }
  textarea.note-input { width: 100%; background: var(--bg3); border: 1px solid var(--border);
                        color: var(--text); border-radius: 8px; padding: 10px; font-size: 14px;
                        min-height: 60px; resize: vertical; margin-top: 6px; }
  textarea.note-input:focus, select.stage-select:focus { outline: none; border-color: var(--blue); }
  .toast { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%);
           background: var(--green); color: #000; padding: 10px 20px; border-radius: 20px;
           font-size: 14px; font-weight: 500; opacity: 0; transition: opacity .2s;
           pointer-events: none; z-index: 100; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>⚡ orch</h1>
  <time id="clock"></time>
</header>
<div class="tab-bar">
  <div class="tab active" onclick="showTab('projects')">Projects</div>
  <div class="tab" onclick="showTab('plan')">Day Plan</div>
  <div class="tab" onclick="showTab('todos')">Todos</div>
</div>

<div id="projects" class="pane active"><div class="loading">Loading…</div></div>
<div id="plan" class="pane"><div class="loading">Loading plan…</div></div>
<div id="todos" class="pane"><div class="loading">Loading…</div></div>

<div class="toast" id="toast"></div>

<script>
let projects = [];
let selectedProject = null;
let planData = null;

function showTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const names = ['projects','plan','todos'];
    t.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.pane').forEach(p => {
    p.classList.toggle('active', p.id === name);
  });
  if (name === 'plan' && !planData) loadPlan();
}

function toast(msg, err=false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = err ? '#f78166' : '#3fb950';
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

function dotClass(indicator) {
  return { active: 'dot-active', waiting: 'dot-waiting', idle: 'dot-idle' }[indicator] || 'dot-idle';
}

function stageClass(stage) {
  return `badge-${stage}`;
}

async function loadProjects() {
  const res = await fetch('/api/projects');
  projects = await res.json();
  renderProjects();
}

function renderProjects() {
  const el = document.getElementById('projects');
  if (!projects.length) { el.innerHTML = '<div class="empty">No projects found in ~/Sites</div>'; return; }
  el.innerHTML = projects.map(p => `
    <div class="card" onclick="selectProject('${p.name}')">
      <div class="card-header">
        <div class="dot ${dotClass(p.status_indicator)}"></div>
        <div class="card-title">${p.name}</div>
        <span class="badge ${stageClass(p.stage)}">${p.stage_emoji} ${p.stage}</span>
      </div>
      ${p.description ? `<div class="status-text">${p.description}</div>` : ''}
      ${p.claude_status ? `<div class="status-text">${p.claude_status}</div>` : ''}
      <div class="meta">
        <span>${p.days_in_stage}d in stage</span>
        ${p.pending_todos ? `<span>${p.pending_todos} todos</span>` : ''}
        ${p.is_stalled ? '<span class="stalled">⏸ stalled</span>' : ''}
        ${p.launch_debt > 0 ? `<span class="stalled">⚠ ${p.launch_debt}d launch debt</span>` : ''}
      </div>
    </div>
  `).join('');
}

function selectProject(name) {
  selectedProject = projects.find(p => p.name === name);
  showTab('todos');
  renderTodos();
}

function renderTodos() {
  const el = document.getElementById('todos');
  if (!selectedProject) {
    el.innerHTML = '<div class="empty">Select a project from the Projects tab</div>';
    return;
  }
  const p = selectedProject;
  const lc = p;

  fetch(`/api/todos?project=${encodeURIComponent(p.name)}`)
    .then(r => r.json())
    .then(todos => {
      el.innerHTML = `
        <div class="card">
          <div class="card-header">
            <div class="card-title">${p.name}</div>
            <span class="badge ${stageClass(p.stage)}">${p.stage_emoji} ${p.stage}</span>
          </div>
          <div class="status-text">${p.claude_status || 'No active Claude session'}</div>
          <div class="meta">
            <span>${p.days_in_stage}d in current stage</span>
            ${p.is_stalled ? '<span class="stalled">⏸ stalled</span>' : ''}
          </div>
        </div>

        <div class="card">
          <div style="font-weight:600;margin-bottom:8px">Advance stage</div>
          <select class="stage-select" id="stage-select">
            ${['idea','building','mvp','staging','live','maintaining'].map(s =>
              `<option value="${s}" ${s===p.stage?'selected':''}>${s}</option>`
            ).join('')}
          </select>
          <textarea class="note-input" id="stage-note" placeholder="Note (optional)…"></textarea>
          <button class="btn" onclick="advanceStage('${p.name}')">Update stage</button>
        </div>

        <div class="card">
          <div style="font-weight:600;margin-bottom:4px">Pending (${todos.pending.length})</div>
          <ul class="todo-list">
            ${todos.pending.map(t => `<li><div class="checkbox"></div><span>${t}</span></li>`).join('') ||
              '<li style="color:var(--muted);font-size:13px">No pending todos</li>'}
          </ul>
          <input class="add-task" id="new-task" type="text" placeholder="Add a todo…"
                 onkeydown="if(event.key==='Enter') addTask('${p.name}')">
          <button class="btn" onclick="addTask('${p.name}')">Add todo</button>
        </div>

        ${todos.in_progress.length ? `
        <div class="card">
          <div style="font-weight:600;margin-bottom:4px">In progress</div>
          <ul class="todo-list">
            ${todos.in_progress.map(t => `<li><span style="color:var(--yellow)">~</span> ${t}</li>`).join('')}
          </ul>
        </div>` : ''}

        <div class="card">
          <div style="font-weight:600;margin-bottom:8px">Ledger</div>
          ${(p.ledger||[]).slice().reverse().map(e =>
            `<div style="font-size:13px;padding:4px 0;border-top:1px solid var(--border)">
               <span style="color:var(--muted)">${e.date}</span>
               &nbsp; ${e.stage} ${e.note ? '— '+e.note : ''}
             </div>`
          ).join('') || '<div style="color:var(--muted);font-size:13px">No ledger entries yet</div>'}
        </div>
      `;
    });
}

async function addTask(projectName) {
  const input = document.getElementById('new-task');
  const task = input.value.trim();
  if (!task) return;
  const res = await fetch('/api/task', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({project: projectName, task}),
  });
  if (res.ok) {
    input.value = '';
    toast('Todo added');
    await loadProjects();
    selectedProject = projects.find(p => p.name === projectName);
    renderTodos();
  } else {
    toast('Failed to add todo', true);
  }
}

async function advanceStage(projectName) {
  const stage = document.getElementById('stage-select').value;
  const note  = document.getElementById('stage-note').value.trim();
  const res = await fetch('/api/stage', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({project: projectName, stage, note}),
  });
  if (res.ok) {
    toast(`Moved to ${stage}`);
    await loadProjects();
    selectedProject = projects.find(p => p.name === projectName);
    renderTodos();
  } else {
    const err = await res.text();
    toast(err || 'Failed', true);
  }
}

async function loadPlan() {
  const el = document.getElementById('plan');
  el.innerHTML = '<div class="loading">Generating plan… (this takes ~10s)</div>';
  try {
    const res = await fetch('/api/plan');
    if (!res.ok) {
      const msg = await res.text();
      el.innerHTML = `<div class="empty">${msg}</div>`;
      return;
    }
    planData = await res.json();
    renderPlan();
  } catch(e) {
    el.innerHTML = `<div class="empty">Failed: ${e.message}</div>`;
  }
}

function renderPlan() {
  const el = document.getElementById('plan');
  if (!planData) { el.innerHTML = '<div class="empty">No plan yet</div>'; return; }
  const focusSet = new Set(planData.focus_projects || []);

  el.innerHTML = `
    <div class="one-liner">${planData.one_liner}</div>
    ${planData.launch_debt_warning?.length ? `
      <div class="warning-banner">⚠ Launch debt: ${planData.launch_debt_warning.join(', ')}</div>` : ''}
    ${(planData.plan || []).map(item => `
      <div class="plan-item" style="${focusSet.has(item.project) ? '' : 'border-left-color:var(--border);opacity:.7'}">
        <div class="plan-priority">#${item.priority} ${focusSet.has(item.project)?'· focus today':''} ${item.flag?'· '+item.flag:''}</div>
        <div class="plan-headline">${item.project} ${STAGE_EMOJI[item.stage]||''} — ${item.headline}</div>
        <div class="plan-rationale">${item.rationale}</div>
        ${item.suggested_tasks?.length ? `
          <ul class="plan-tasks">
            ${item.suggested_tasks.map(t=>`<li>• ${t}</li>`).join('')}
          </ul>` : ''}
      </div>
    `).join('')}
    <button class="btn" style="background:var(--bg3);color:var(--text);border:1px solid var(--border)"
            onclick="planData=null;loadPlan()">Regenerate plan</button>
  `;
}

const STAGE_EMOJI = {
  idea:'💡', building:'🔨', mvp:'🎯', staging:'🚀', live:'✅', maintaining:'🔧'
};

// Clock
function updateClock() {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
updateClock();
setInterval(updateClock, 30000);

// Auto-refresh projects every 30s
loadProjects();
setInterval(loadProjects, 30000);
</script>
</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class OrchHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default access log

    def _projects(self):
        return discover_projects()

    def _find_project(self, name: str) -> "Project | None":
        return next((p for p in self._projects() if p.name == name), None)

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/":
            self._send_html(MOBILE_HTML)

        elif path == "/api/projects":
            projects = self._projects()
            self._send_json([_project_summary(p) for p in projects])

        elif path == "/api/todos":
            name = qs.get("project", [None])[0]
            p = self._find_project(name) if name else None
            if not p:
                self._send_text("Project not found", 404)
                return
            self._send_json(_todos_data(p))

        elif path == "/api/status":
            projects = self._projects()
            self._send_json([
                {
                    "name": p.name,
                    "stage": load_lifecycle(p).stage,
                    "status_indicator": p.status_indicator,
                    "claude_status": p.current_status,
                    "pending_todos": p.pending_count,
                }
                for p in projects
            ])

        elif path == "/api/plan":
            from .planner import generate, format_plan
            try:
                projects = self._projects()
                plan = generate(projects)
                self._send_json({
                    "date":                plan.date,
                    "one_liner":           plan.one_liner,
                    "focus_projects":      plan.focus_projects,
                    "plan":                plan.plan,
                    "stalled":             plan.stalled,
                    "launch_debt_warning": plan.launch_debt_warning,
                    "skip_today":          plan.skip_today,
                })
            except RuntimeError as e:
                self._send_text(str(e), 500)

        else:
            self._send_text("Not found", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        path   = urlparse(self.path).path

        if path == "/api/task":
            name = body.get("project", "")
            task = body.get("task", "").strip()
            p = self._find_project(name)
            if not p or not task:
                self._send_text("Missing project or task", 400)
                return
            _add_todo(p, task)
            self._send_json({"ok": True})

        elif path == "/api/stage":
            name  = body.get("project", "")
            stage = body.get("stage", "")
            note  = body.get("note", "")
            p = self._find_project(name)
            if not p:
                self._send_text("Project not found", 404)
                return
            try:
                advance_stage(p, stage, note)
                self._send_json({"ok": True})
            except ValueError as e:
                self._send_text(str(e), 400)

        elif path == "/api/task/send":
            # Send a prompt to a waiting Claude session
            name = body.get("project", "")
            task = body.get("task", "").strip()
            p = self._find_project(name)
            if not p or not task:
                self._send_text("Missing project or task", 400)
                return
            pending = p.claude_dir / "pending_task"
            pending.write_text(task)
            self._send_json({"ok": True})

        else:
            self._send_text("Not found", 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── Server lifecycle ──────────────────────────────────────────────────────────

class BridgeServer:
    def __init__(self):
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def start(self) -> int:
        cfg  = _bridge_config()
        port = cfg["port"]
        self._server = HTTPServer(("127.0.0.1", port), OrchHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return port

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
            self._thread = None


# Singleton used by the TUI
_bridge = BridgeServer()


def start_bridge() -> int:
    return _bridge.start()


def stop_bridge():
    _bridge.stop()


def bridge_running() -> bool:
    return _bridge.is_running
