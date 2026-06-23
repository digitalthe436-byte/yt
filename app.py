"""
YT Auto Uploader — Legacy v1 (single-channel, inline HTML dashboard)
DEPRECATED: Use api.py instead, which supports multiple channels,
            a proper frontend, and all new features.
Run  : python api.py
"""

import logging
import threading
from collections import deque
from datetime import datetime

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from uploader import (
    get_google_clients,
    get_next_drive_video,
    load_config,
    upload_job,
)

# ---------------------------------------------------------------------------
# Shared in-memory state  (read/written from both scheduler + API threads)
# ---------------------------------------------------------------------------
_lock  = threading.Lock()
_state = {
    "status":            "idle",   # idle | uploading | error
    "last_upload_at":    None,
    "last_video_title":  None,
    "last_video_url":    None,
    "queue_count":       None,     # None = still counting
    "next_run_at":       None,
    "paused":            False,
}
_logs: deque = deque(maxlen=200)   # newest-first ring buffer


# ---------------------------------------------------------------------------
# Memory log handler — captures every log line into _logs
# ---------------------------------------------------------------------------
class _MemHandler(logging.Handler):
    def emit(self, record):
        _logs.appendleft({
            "time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level":   record.levelname,
            "message": record.getMessage(),
        })

_mem = _MemHandler()
_mem.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_mem)


# ---------------------------------------------------------------------------
# Upload wrapper — updates state, can be called from scheduler or API
# ---------------------------------------------------------------------------
def _run_upload():
    with _lock:
        if _state["paused"] or _state["status"] == "uploading":
            return
        _state["status"] = "uploading"
    try:
        result = upload_job()
        with _lock:
            _state["status"] = "idle"
            if result:
                _state["last_upload_at"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _state["last_video_title"] = result["title"]
                _state["last_video_url"]   = result["url"]
    except Exception:
        with _lock:
            _state["status"] = "error"
    finally:
        threading.Thread(target=_refresh_queue_count, daemon=True).start()


def _refresh_queue_count():
    try:
        config = load_config()
        folder = config.get("drive_queue_folder_id", "").strip()
        if not folder:
            return
        _, drive  = get_google_clients()
        count, token = 0, None
        while True:
            kw = dict(
                q=f"'{folder}' in parents and mimeType contains 'video/' and trashed=false",
                fields="nextPageToken,files(id)",
                pageSize=1000,
            )
            if token:
                kw["pageToken"] = token
            res   = drive.files().list(**kw).execute()
            count += len(res.get("files", []))
            token  = res.get("nextPageToken")
            if not token:
                break
        with _lock:
            _state["queue_count"] = count
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="YT Auto Uploader", version="1.0")

_scheduler: BackgroundScheduler | None = None


@app.on_event("startup")
def _startup():
    global _scheduler
    config       = load_config()
    upload_time  = config.get("upload_time", "09:00")
    hour, minute = map(int, upload_time.split(":"))

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_run_upload, "cron", hour=hour, minute=minute, id="daily")
    _scheduler.start()

    nxt = _scheduler.get_job("daily").next_run_time
    with _lock:
        _state["next_run_at"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else None

    threading.Thread(target=_refresh_queue_count, daemon=True).start()


@app.on_event("shutdown")
def _shutdown():
    if _scheduler:
        _scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# REST API — for external systems
# ---------------------------------------------------------------------------
@app.get("/api/status", summary="Current uploader state")
def api_status():
    with _lock:
        return dict(_state)


@app.post("/api/trigger", summary="Trigger an upload right now")
def api_trigger():
    with _lock:
        if _state["status"] == "uploading":
            return {"ok": False, "message": "Upload already in progress"}
    threading.Thread(target=_run_upload, daemon=True).start()
    return {"ok": True, "message": "Upload triggered"}


@app.post("/api/pause", summary="Toggle the daily schedule on / off")
def api_pause():
    with _lock:
        _state["paused"] = not _state["paused"]
        return {"ok": True, "paused": _state["paused"]}


@app.get("/api/logs", summary="Recent log lines")
def api_logs(limit: int = 100):
    return list(_logs)[:limit]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return _HTML


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Auto Uploader</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#e8e8e8;font-family:system-ui,-apple-system,sans-serif;min-height:100vh}
a{color:inherit;text-decoration:none}

/* header */
header{background:#111;border-bottom:1px solid #1e1e1e;padding:14px 24px;display:flex;align-items:center;gap:12px}
.logo{background:#ff0000;color:#fff;border-radius:6px;padding:3px 9px;font-size:.7rem;font-weight:800;letter-spacing:.06em}
header h1{font-size:1.1rem;font-weight:600}
#countdown{margin-left:auto;font-size:.72rem;color:#444}

/* layout */
.wrap{max-width:1000px;margin:0 auto;padding:24px 20px}

/* stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:20px}
.card{background:#161616;border:1px solid #242424;border-radius:10px;padding:18px 20px}
.card .lbl{font-size:.68rem;color:#666;text-transform:uppercase;letter-spacing:.09em;margin-bottom:8px}
.card .val{font-size:1.7rem;font-weight:700;line-height:1.2}
.card .sub{font-size:.75rem;color:#555;margin-top:5px}
.card .sub a{color:#ff5555}

/* status row */
.srow{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:20px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;font-size:.82rem;font-weight:600}
.badge.idle     {background:#0e1f0e;color:#4caf50;border:1px solid #1a3a1a}
.badge.uploading{background:#0e0e1f;color:#5c9dff;border:1px solid #1a1a3a}
.badge.error    {background:#1f0e0e;color:#f44336;border:1px solid #3a1a1a}
.badge.paused   {background:#1f1a0e;color:#ffb74d;border:1px solid #3a2a1a}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.dot.pulse{animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
btn,button{padding:7px 16px;border-radius:8px;border:none;cursor:pointer;font-size:.82rem;font-weight:600;transition:opacity .15s}
button:hover{opacity:.8} button:disabled{opacity:.35;cursor:not-allowed}
.btn-red  {background:#ff0000;color:#fff}
.btn-ghost{background:#1e1e1e;color:#ccc;border:1px solid #2e2e2e}
.btn-ghost.active{background:#2a2a1a;color:#ffb74d;border-color:#3a3a1a}

/* logs */
.sec-title{font-size:.7rem;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.09em;margin-bottom:10px}
.log-box{background:#0d0d0d;border:1px solid #1e1e1e;border-radius:10px;overflow:hidden;max-height:340px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-family:'SF Mono','Fira Code',ui-monospace,monospace;font-size:.75rem}
td{padding:6px 14px;border-bottom:1px solid #161616}
tr:last-child td{border-bottom:none}
.t-time{color:#444;white-space:nowrap;width:155px}
.t-lvl{width:58px;font-size:.65rem;font-weight:700;text-align:center;border-radius:4px;padding:1px 0}
.t-lvl.INFO   {color:#4caf50}
.t-lvl.WARNING{color:#ffb74d}
.t-lvl.ERROR  {color:#f44336}
.t-lvl.DEBUG  {color:#555}
.t-msg{color:#bbb}

/* API reference */
.api-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-top:8px}
.api-card{background:#161616;border:1px solid #242424;border-radius:8px;padding:12px 14px}
.method{display:inline-block;padding:1px 7px;border-radius:4px;font-size:.65rem;font-weight:800;margin-bottom:5px}
.GET {background:#0e1f0e;color:#4caf50}
.POST{background:#0e0e1f;color:#5c9dff}
.ep{font-family:monospace;font-size:.82rem;margin-bottom:3px}
.desc{font-size:.72rem;color:#555}

.foot{text-align:center;font-size:.7rem;color:#333;margin-top:20px}
</style>
</head>
<body>
<header>
  <div class="logo">YT</div>
  <h1>Auto Uploader</h1>
  <span id="countdown"></span>
</header>

<div class="wrap">

  <div class="cards">
    <div class="card">
      <div class="lbl">In Queue</div>
      <div class="val" id="c-queue">—</div>
      <div class="sub">videos waiting</div>
    </div>
    <div class="card">
      <div class="lbl">Next Upload</div>
      <div class="val" style="font-size:1rem;padding-top:4px" id="c-next">—</div>
      <div class="sub">scheduled daily run</div>
    </div>
    <div class="card">
      <div class="lbl">Last Upload</div>
      <div class="val" style="font-size:.95rem;padding-top:4px" id="c-last">Never</div>
      <div class="sub" id="c-last-link"></div>
    </div>
    <div class="card">
      <div class="lbl">Last Title</div>
      <div class="val" style="font-size:.82rem;padding-top:4px;font-weight:500;color:#aaa" id="c-title">—</div>
      <div class="sub">AI-generated</div>
    </div>
  </div>

  <div class="srow">
    <div class="badge idle" id="badge">
      <div class="dot" id="dot"></div>
      <span id="badge-txt">Loading…</span>
    </div>
    <button class="btn-red"   id="btn-up"    onclick="triggerUpload()">Upload Now</button>
    <button class="btn-ghost" id="btn-pause" onclick="togglePause()">Pause</button>
    <a href="/docs" target="_blank" style="margin-left:auto">
      <button class="btn-ghost">API Docs ↗</button>
    </a>
  </div>

  <div class="sec-title">Recent Activity</div>
  <div class="log-box">
    <table><tbody id="log-body">
      <tr><td class="t-time"></td><td class="t-lvl INFO">INFO</td><td class="t-msg">Loading…</td></tr>
    </tbody></table>
  </div>

  <div style="margin-top:24px">
    <div class="sec-title">REST API — connect your external systems</div>
    <div class="api-grid">
      <div class="api-card"><div class="method GET">GET</div><div class="ep">/api/status</div><div class="desc">Queue count, status, last &amp; next upload</div></div>
      <div class="api-card"><div class="method POST">POST</div><div class="ep">/api/trigger</div><div class="desc">Trigger an upload immediately</div></div>
      <div class="api-card"><div class="method POST">POST</div><div class="ep">/api/pause</div><div class="desc">Toggle the daily schedule on / off</div></div>
      <div class="api-card"><div class="method GET">GET</div><div class="ep">/api/logs?limit=N</div><div class="desc">Recent log lines as JSON</div></div>
    </div>
  </div>

  <div class="foot">Auto-refreshes every 15 s</div>
</div>

<script>
let ticker = 15;

async function fetchStatus() {
  const s = await fetch('/api/status').then(r => r.json());

  document.getElementById('c-queue').textContent = s.queue_count ?? '…';
  document.getElementById('c-next').textContent  = s.next_run_at ?? '—';
  document.getElementById('c-last').textContent  = s.last_upload_at ?? 'Never';
  document.getElementById('c-title').textContent = s.last_video_title ?? '—';

  const linkEl = document.getElementById('c-last-link');
  linkEl.innerHTML = s.last_video_url
    ? `<a href="${s.last_video_url}" target="_blank" style="color:#ff5555">watch on YouTube ↗</a>`
    : '';

  const badge = document.getElementById('badge');
  const txt   = document.getElementById('badge-txt');
  const dot   = document.getElementById('dot');
  const st    = s.paused ? 'paused' : s.status;

  badge.className = 'badge ' + st;
  txt.textContent = st.charAt(0).toUpperCase() + st.slice(1);
  dot.className   = 'dot' + (s.status === 'uploading' ? ' pulse' : '');

  document.getElementById('btn-up').disabled    = s.status === 'uploading';
  const bp = document.getElementById('btn-pause');
  bp.textContent = s.paused ? 'Resume' : 'Pause';
  bp.className   = 'btn-ghost' + (s.paused ? ' active' : '');
}

async function fetchLogs() {
  const logs = await fetch('/api/logs?limit=80').then(r => r.json());
  if (!logs.length) return;
  document.getElementById('log-body').innerHTML = logs.map(l =>
    `<tr>
       <td class="t-time">${l.time}</td>
       <td class="t-lvl ${l.level}">${l.level}</td>
       <td class="t-msg">${esc(l.message)}</td>
     </tr>`
  ).join('');
}

async function triggerUpload() {
  document.getElementById('btn-up').disabled = true;
  const r = await fetch('/api/trigger', {method:'POST'}).then(r=>r.json());
  if (!r.ok) alert(r.message);
  await refresh();
}

async function togglePause() {
  await fetch('/api/pause', {method:'POST'});
  await refresh();
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function refresh() {
  await Promise.all([fetchStatus(), fetchLogs()]);
}

const countEl = document.getElementById('countdown');
setInterval(() => {
  ticker--;
  countEl.textContent = `Refreshing in ${ticker}s`;
  if (ticker <= 0) { ticker = 15; refresh(); }
}, 1000);

refresh();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
