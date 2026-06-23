"""
YT Auto Uploader — Multi-Channel REST API
Run  : python api.py
UI   : http://localhost:8000
Docs : http://localhost:8000/docs
"""

import json
import logging
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from uploader import get_google_clients, load_config, upload_job

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR        = Path(__file__).parent
CHANNELS_FILE   = BASE_DIR / "channels.json"
HISTORY_FILE    = BASE_DIR / "data" / "uploads.json"
CREDENTIALS_DIR = BASE_DIR / "credentials"
FRONTEND_DIR    = BASE_DIR / "frontend"
CONFIG_FILE     = BASE_DIR / "config.json"
SECRETS_FILE    = BASE_DIR / "client_secrets.json"

for _d in (HISTORY_FILE.parent, CREDENTIALS_DIR, FRONTEND_DIR / "css", FRONTEND_DIR / "js"):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

_log_ring: deque = deque(maxlen=500)


class _MemLog(logging.Handler):
    def emit(self, record):
        _log_ring.appendleft({
            "time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level":      record.levelname,
            "message":    record.getMessage(),
            "channel_id": getattr(record, "channel_id", None),
        })


logging.getLogger().addHandler(_MemLog())

# ---------------------------------------------------------------------------
# Per-channel in-memory state
# ---------------------------------------------------------------------------
_lock     = threading.Lock()
_ch_state: dict[str, dict] = {}


def _blank_state() -> dict:
    return {
        "status":           "idle",
        "last_upload_at":   None,
        "last_video_title": None,
        "last_video_url":   None,
        "queue_count":      None,
        "next_run_at":      None,
        "error_msg":        None,
    }


# ---------------------------------------------------------------------------
# Channels file helpers
# ---------------------------------------------------------------------------
def _load_channels() -> list[dict]:
    if not CHANNELS_FILE.exists():
        return []
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        return json.load(f).get("channels", [])


def _save_channels(channels: list[dict]) -> None:
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump({"channels": channels}, f, indent=2)


# ---------------------------------------------------------------------------
# Upload history  (analytics source of truth)
# ---------------------------------------------------------------------------
_history_lock = threading.Lock()


def _load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, encoding="utf-8") as f:
        return json.load(f)


def _append_history(record: dict) -> None:
    with _history_lock:
        hist = _load_history()
        hist.append(record)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=2)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
_scheduler = BackgroundScheduler()


def _run_batch(channel_id: str) -> None:
    """Upload a full day's batch, each video scheduled with publishAt."""
    channels = _load_channels()
    ch = next((c for c in channels if c["id"] == channel_id), None)
    if not ch or not ch.get("enabled"):
        return

    with _lock:
        st = _ch_state.get(channel_id, {})
        if st.get("status") in ("uploading", "paused"):
            return
        _ch_state.setdefault(channel_id, _blank_state())["status"] = "uploading"
        _ch_state[channel_id]["error_msg"] = None

    uploads_per_day = max(1, int(ch.get("uploads_per_day", 1)))
    h, m            = map(int, ch.get("upload_time", "09:00").split(":"))
    interval_mins   = int(24 * 60 / uploads_per_day)

    # First slot = now + 15 min (YouTube minimum), rounded to clean minute
    now  = datetime.now()
    base = (now + timedelta(minutes=15)).replace(second=0, microsecond=0)

    global_cfg    = load_config()
    token_file    = str(BASE_DIR / ch.get("token_file", f"credentials/{channel_id}_token.json"))
    uploaded_list = str(BASE_DIR / "data" / f"uploaded_{channel_id}.txt")
    channel_cfg   = {**global_cfg, **ch, "token_file": token_file, "uploaded_list": uploaded_list}

    log.info(f"[{channel_id}] Batch start: {uploads_per_day} videos — first slot {base.strftime('%Y-%m-%d %H:%M')}")
    success = 0

    for i in range(uploads_per_day):
        slot       = base + timedelta(minutes=i * interval_mins)
        publish_at = slot.astimezone()
        log.info(f"[{channel_id}] Video {i+1}/{uploads_per_day} → scheduled {slot.strftime('%Y-%m-%d %H:%M')}")
        result = None
        for attempt in range(2):
            try:
                result = upload_job(channel_config=channel_cfg, publish_at=publish_at)
                break
            except Exception as e:
                if attempt == 0:
                    log.warning(f"[{channel_id}] Video {i+1} failed, retrying in 30s… ({e})")
                    time.sleep(30)
                else:
                    log.error(f"[{channel_id}] Video {i+1} failed after retry: {e}", exc_info=True)
                    with _lock:
                        _ch_state[channel_id]["error_msg"] = str(e)
        if result is None:
            continue

        if not result:
            log.info(f"[{channel_id}] Queue empty — stopped at {i}/{uploads_per_day}")
            break

        success += 1
        with _lock:
            _ch_state[channel_id]["last_upload_at"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _ch_state[channel_id]["last_video_title"] = result["title"]
            _ch_state[channel_id]["last_video_url"]   = result["url"]
        _append_history({
            "channel_id":   channel_id,
            "channel_name": ch.get("name", channel_id),
            "timestamp":    datetime.now().isoformat(),
            "video_id":     result["video_id"],
            "title":        result["title"],
            "url":          result["url"],
            "scheduled_at": slot.isoformat(),
        })

    with _lock:
        _ch_state[channel_id]["status"] = "idle"
    log.info(f"[{channel_id}] Batch done: {success}/{uploads_per_day} videos scheduled on YouTube ✓")
    threading.Thread(target=_refresh_queue, args=[channel_id], daemon=True).start()


def _refresh_queue(channel_id: str) -> None:
    try:
        channels = _load_channels()
        ch = next((c for c in channels if c["id"] == channel_id), None)
        if not ch:
            return
        folder = ch.get("drive_queue_folder_id", "").strip()
        if not folder:
            return
        token_file = BASE_DIR / ch.get("token_file", f"credentials/{channel_id}_token.json")
        _, drive   = get_google_clients(token_file=token_file)
        count, tok = 0, None
        while True:
            kw = dict(
                q=f"'{folder}' in parents and mimeType contains 'video/' and trashed=false",
                fields="nextPageToken,files(id)", pageSize=1000,
            )
            if tok:
                kw["pageToken"] = tok
            res    = drive.files().list(**kw).execute()
            count += len(res.get("files", []))
            tok    = res.get("nextPageToken")
            if not tok:
                break
        with _lock:
            if channel_id in _ch_state:
                _ch_state[channel_id]["queue_count"] = count
    except Exception as e:
        log.warning(f"[{channel_id}] Queue count failed: {e}")


def _reschedule():
    for job in _scheduler.get_jobs():
        if job.id.startswith("yt_"):
            _scheduler.remove_job(job.id)

    for ch in _load_channels():
        if not ch.get("enabled"):
            continue

        ch_id           = ch["id"]
        uploads_per_day = max(1, int(ch.get("uploads_per_day", 1)))
        h, m            = map(int, ch.get("upload_time", "09:00").split(":"))
        job_id          = f"yt_{ch_id}"

        # One daily batch job at the configured time
        _scheduler.add_job(
            _run_batch, "cron",
            hour=h, minute=m,
            id=job_id, args=[ch_id], replace_existing=True,
        )

        job = _scheduler.get_job(job_id)
        nxt = job.next_run_time if job else None
        with _lock:
            _ch_state.setdefault(ch_id, _blank_state())
            _ch_state[ch_id]["next_run_at"]     = nxt.strftime("%Y-%m-%d %H:%M") if nxt else None
            _ch_state[ch_id]["uploads_per_day"] = uploads_per_day


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI):
    _scheduler.start()
    _reschedule()
    for ch in _load_channels():
        threading.Thread(target=_refresh_queue, args=[ch["id"]], daemon=True).start()
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="YT Auto Uploader", version="2.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ── Frontend ──────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def root():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# ── Channels ──────────────────────────────────────────────────────────────────
@app.get("/api/channels", summary="List all channels with live state")
def api_get_channels():
    channels = _load_channels()
    result = []
    for ch in channels:
        with _lock:
            state = dict(_ch_state.get(ch["id"], _blank_state()))
        result.append({**ch, "state": state})
    return result


class ChannelBody(BaseModel):
    name:                  str
    platform:              str  = "youtube"
    color:                 str  = "#ff4444"
    drive_queue_folder_id: str  = ""
    drive_done_folder_id:  str  = ""
    upload_time:           str  = "09:00"
    uploads_per_day:       int  = 1
    privacy_status:        str  = "public"
    gemini_extra_prompt:   str  = ""
    enabled:               bool = True


@app.post("/api/channels", status_code=201, summary="Add a new channel")
def api_add_channel(body: ChannelBody):
    ch_id    = f"ch_{uuid.uuid4().hex[:8]}"
    channels = _load_channels()
    ch = {"id": ch_id, "token_file": f"credentials/{ch_id}_token.json", **body.model_dump()}
    channels.append(ch)
    _save_channels(channels)
    with _lock:
        _ch_state[ch_id] = _blank_state()
    _reschedule()
    return ch


@app.put("/api/channels/{channel_id}", summary="Update a channel")
def api_update_channel(channel_id: str, body: ChannelBody):
    channels = _load_channels()
    idx = next((i for i, c in enumerate(channels) if c["id"] == channel_id), None)
    if idx is None:
        raise HTTPException(404, "Channel not found")
    channels[idx] = {**channels[idx], **body.model_dump()}
    _save_channels(channels)
    _reschedule()
    return channels[idx]


@app.delete("/api/channels/{channel_id}", status_code=204, summary="Delete a channel")
def api_delete_channel(channel_id: str):
    channels = [c for c in _load_channels() if c["id"] != channel_id]
    _save_channels(channels)
    with _lock:
        _ch_state.pop(channel_id, None)
    try:
        _scheduler.remove_job(f"yt_{channel_id}")
    except Exception:
        pass


@app.post("/api/channels/{channel_id}/trigger", summary="Upload now")
def api_trigger(channel_id: str):
    with _lock:
        status = _ch_state.get(channel_id, {}).get("status")
        if status == "uploading":
            return {"ok": False, "message": "Already uploading"}
        if status == "paused":
            return {"ok": False, "message": "Channel is paused — resume it first"}
    threading.Thread(target=_run_batch, args=[channel_id], daemon=True).start()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/pause", summary="Toggle pause")
def api_pause(channel_id: str):
    with _lock:
        if channel_id not in _ch_state:
            raise HTTPException(404)
        cur = _ch_state[channel_id]["status"]
        new = "idle" if cur == "paused" else "paused"
        _ch_state[channel_id]["status"] = new
        return {"ok": True, "status": new}


@app.post("/api/channels/{channel_id}/authenticate", summary="Trigger OAuth for a channel (opens browser)")
def api_authenticate(channel_id: str):
    channels = _load_channels()
    ch = next((c for c in channels if c["id"] == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    token_path = BASE_DIR / ch.get("token_file", f"credentials/{channel_id}_token.json")
    try:
        get_google_clients(token_file=token_path, force_reauth=True)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Analytics ─────────────────────────────────────────────────────────────────
@app.get("/api/analytics", summary="Global stats + 7-day time series")
def api_analytics():
    history  = _load_history()
    channels = _load_channels()
    today    = datetime.now().date()
    days     = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

    per_day: dict[str, dict[str, int]] = {d: {} for d in days}
    by_channel: dict[str, int] = {}
    uploads_today = 0

    for rec in history:
        try:
            ts  = datetime.fromisoformat(rec["timestamp"])
            d   = ts.date().isoformat()
            cid = rec["channel_id"]
            by_channel[cid] = by_channel.get(cid, 0) + 1
            if d in per_day:
                per_day[d][cid] = per_day[d].get(cid, 0) + 1
            if ts.date() == today:
                uploads_today += 1
        except Exception:
            pass

    with _lock:
        total_queue = sum(s.get("queue_count") or 0 for s in _ch_state.values())

    return {
        "total_channels":  len(channels),
        "total_queue":     total_queue,
        "total_uploaded":  sum(by_channel.values()),
        "uploads_today":   uploads_today,
        "days":            days,
        "per_day":         per_day,
        "by_channel":      by_channel,
        "channel_names":   {c["id"]: c["name"]           for c in channels},
        "channel_colors":  {c["id"]: c.get("color", "#ff4444") for c in channels},
    }


# ── Logs ──────────────────────────────────────────────────────────────────────
@app.get("/api/logs", summary="Recent logs (optionally filtered by channel_id)")
def api_logs(limit: int = 100, channel_id: Optional[str] = None):
    logs = list(_log_ring)
    if channel_id:
        logs = [l for l in logs if l.get("channel_id") == channel_id]
    return logs[:limit]


# ── Recent uploads ─────────────────────────────────────────────────────────────
@app.get("/api/history", summary="Upload history")
def api_history(limit: int = 50):
    hist = _load_history()
    return list(reversed(hist))[:limit]


# ── Uploaded list ─────────────────────────────────────────────────────────────
@app.get("/api/channels/{channel_id}/uploaded", summary="View uploaded video list")
def api_get_uploaded(channel_id: str):
    path = BASE_DIR / "data" / f"uploaded_{channel_id}.txt"
    if not path.exists():
        return {"count": 0, "entries": []}
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            entries.append({
                "drive_file_id": parts[0] if len(parts) > 0 else "",
                "filename":      parts[1] if len(parts) > 1 else "",
                "youtube_url":   parts[2] if len(parts) > 2 else "",
                "uploaded_at":   parts[3] if len(parts) > 3 else "",
            })
    return {"count": len(entries), "entries": list(reversed(entries))}


@app.delete("/api/channels/{channel_id}/uploaded/{drive_file_id}",
            status_code=204, summary="Remove an entry (allows re-upload)")
def api_remove_uploaded(channel_id: str, drive_file_id: str):
    path = BASE_DIR / "data" / f"uploaded_{channel_id}.txt"
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept  = [l for l in lines if not l.strip().startswith(drive_file_id)]
    path.write_text("".join(kept), encoding="utf-8")


@app.delete("/api/channels/{channel_id}/uploaded", summary="Clear all uploaded entries (fresh start)")
def api_clear_channel_uploaded(channel_id: str):
    path = BASE_DIR / "data" / f"uploaded_{channel_id}.txt"
    if path.exists():
        lines  = path.read_text(encoding="utf-8").splitlines(keepends=True)
        header = [l for l in lines if l.startswith("#")]
        path.write_text("".join(header) + "\n", encoding="utf-8")
    return {"ok": True}


@app.delete("/api/history", summary="Clear all upload history from dashboard")
def api_clear_history():
    with _history_lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
    return {"ok": True}


# ── Settings ───────────────────────────────────────────────────────────────────
@app.get("/api/settings", summary="Read current global config")
def api_get_settings():
    cfg  = load_config()
    gkey = cfg.get("gemini_api_key", "")
    qkey = cfg.get("groq_api_key", "")
    return {
        "gemini_api_key":        ("*" * 8 + gkey[-4:]) if len(gkey) > 4 else ("set" if gkey else ""),
        "gemini_api_key_is_set": bool(gkey),
        "groq_api_key":          ("*" * 8 + qkey[-4:]) if len(qkey) > 4 else ("set" if qkey else ""),
        "groq_api_key_is_set":   bool(qkey),
        "secrets_file_exists":   SECRETS_FILE.exists(),
        "default_upload_time":   cfg.get("upload_time",      "09:00"),
        "default_privacy":       cfg.get("privacy_status",   "public"),
        "default_category":      cfg.get("default_category", "22"),
    }


class SettingsBody(BaseModel):
    gemini_api_key:      Optional[str] = None
    groq_api_key:        Optional[str] = None
    default_upload_time: Optional[str] = None
    default_privacy:     Optional[str] = None
    default_category:    Optional[str] = None


@app.post("/api/settings", summary="Update global config")
def api_save_settings(body: SettingsBody):
    cfg = load_config()
    if body.gemini_api_key is not None:
        cfg["gemini_api_key"] = body.gemini_api_key
    if body.groq_api_key is not None:
        cfg["groq_api_key"] = body.groq_api_key
    if body.default_upload_time is not None:
        cfg["upload_time"] = body.default_upload_time
    if body.default_privacy is not None:
        cfg["privacy_status"] = body.default_privacy
    if body.default_category is not None:
        cfg["default_category"] = body.default_category
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"ok": True}


class SecretsBody(BaseModel):
    json_content: str   # raw text of client_secrets.json


@app.post("/api/secrets", summary="Save client_secrets.json from pasted text")
def api_save_secrets(body: SecretsBody):
    try:
        parsed = json.loads(body.json_content)
        # Basic validation — must contain 'installed' or 'web' key
        if "installed" not in parsed and "web" not in parsed:
            raise ValueError("Not a valid OAuth client secrets file")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(parsed, f, indent=2)
    return {"ok": True}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
