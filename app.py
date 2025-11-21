
import os
import re
import json
import uuid
import time
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

APP_VERSION = os.getenv("APP_VERSION", "v1.0")
PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASS = os.getenv("PANEL_PASS", "1234")
PORT = int(os.getenv("PORT", "8090"))
DOWNLOAD_ENABLED_FLAG = os.getenv("DOWNLOAD_ENABLED", "1")  # "1" enabled / "0" disabled

BASE_DIR = Path(__file__).parent.resolve()
DL_DIR = BASE_DIR / "downloads"
LOG_DIR = BASE_DIR / "logs"
HISTORY_FILE = LOG_DIR / "history.json"

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

# In-memory job store
jobs = {}
jobs_lock = threading.Lock()

YOUTUBE_SINGLE_RE = re.compile(r"^https?://(www\.)?(youtube\.com|youtu\.be)/", re.I)

def is_single_video(url: str) -> bool:
    return ("list=" not in url) and ("playlist" not in url)

def _append_history(data):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if HISTORY_FILE.exists():
            arr = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        else:
            arr = []
        arr.append(data)
        HISTORY_FILE.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("history write error:", e)

def ydl_hook(job_id):
    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            if d.get('status') == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes') or 0
                p = 0
                if total:
                    p = int(downloaded * 100 / total)
                job['progress'] = min(max(p, 0), 100)
                job['speed'] = d.get('speed')
                job['eta'] = d.get('eta')
                job['status'] = 'downloading'
            elif d.get('status') == 'finished':
                job['progress'] = 100
                job['status'] = 'processing'
    return hook

def run_download(job_id, url, fmt, title_override):
    outpath = DL_DIR / f"{job_id}.%(ext)s"
    opts = {
        "outtmpl": str(outpath),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [ydl_hook(job_id)]
    }
    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
        ]
    else:  # mp4
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # compute file path
            if fmt == "mp3":
                file_path = DL_DIR / f"{job_id}.mp3"
            else:
                file_path = DL_DIR / f"{job_id}.mp4"
            display_name = title_override.strip() if title_override else info.get("title", "download")
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["file"] = str(file_path)
                jobs[job_id]["title"] = display_name
            _append_history({
                "when": datetime.utcnow().isoformat() + "Z",
                "url": url,
                "format": fmt,
                "title": display_name,
                "file": str(file_path.name)
            })
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)

@app.context_processor
def inject_globals():
    return {"APP_VERSION": APP_VERSION}

@app.route("/")
def index():
    enabled = os.getenv("DOWNLOAD_ENABLED", DOWNLOAD_ENABLED_FLAG) == "1"
    return render_template("index.html", enabled=enabled)

@app.post("/api/create")
def api_create():
    enabled = os.getenv("DOWNLOAD_ENABLED", DOWNLOAD_ENABLED_FLAG) == "1"
    if not enabled:
        return jsonify({"ok": False, "error": "downloads_disabled"}), 403

    data = request.json or {}
    url = (data.get("url") or "").strip()
    fmt = (data.get("format") or "mp3").lower()
    title = (data.get("title") or "").strip()

    if not url.startswith(("http://", "https://")) or not YOUTUBE_SINGLE_RE.search(url):
        return jsonify({"ok": False, "error": "invalid_url"}), 400
    if not is_single_video(url):
        return jsonify({"ok": False, "error": "not_single"}), 400
    if fmt not in ("mp3", "mp4"):
        return jsonify({"ok": False, "error": "bad_format"}), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "created": time.time(),
            "status": "queued",
            "progress": 0,
            "file": None,
            "title": None
        }
    t = threading.Thread(target=run_download, args=(job_id, url, fmt, title), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})

@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True, "job": job})

@app.get("/download/<job_id>")
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "done" or not job.get("file"):
            return "Not ready", 404
        filename = job.get("title") or Path(job["file"]).name
        return send_file(job["file"], as_attachment=True, download_name=filename + Path(job["file"]).suffix)

# ---------------- Admin Panel ----------------
def _login_required():
    return ("admin_logged" in session) and session["admin_logged"] is True

@app.get("/admin/login")
def admin_login_page():
    if _login_required():
        return redirect(url_for("admin_home"))
    return render_template("admin_login.html")

@app.post("/admin/login")
def admin_login():
    user = request.form.get("user") or ""
    passwd = request.form.get("pass") or ""
    if user == PANEL_USER and passwd == PANEL_PASS:
        session["admin_logged"] = True
        flash("เข้าสู่ระบบสำเร็จ", "ok")
        return redirect(url_for("admin_home"))
    flash("ชื่อผู้ใช้/รหัสผ่านไม่ถูกต้อง", "err")
    return redirect(url_for("admin_login_page"))

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login_page"))

@app.get("/admin")
def admin_home():
    if not _login_required():
        return redirect(url_for("admin_login_page"))
    enabled = os.getenv("DOWNLOAD_ENABLED", DOWNLOAD_ENABLED_FLAG) == "1"
    hist = []
    try:
        if HISTORY_FILE.exists():
            hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return render_template("admin.html", enabled=enabled, history=hist[-100:][::-1])

@app.post("/admin/toggle")
def admin_toggle():
    if not _login_required():
        return "unauthorized", 401
    val = request.form.get("enable") == "1"
    os.environ["DOWNLOAD_ENABLED"] = "1" if val else "0"
    flash("เปิดดาวน์โหลด" if val else "ปิดดาวน์โหลด", "ok")
    return redirect(url_for("admin_home"))

@app.post("/admin/restart")
def admin_restart():
    if not _login_required():
        return "unauthorized", 401
    # Soft-restart by touching app file so systemd/gunicorn reload via next deploy;
    # here we just flash message.
    flash("ทำการรีสตาร์ท (จำลอง) แล้ว", "ok")
    return redirect(url_for("admin_home"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
