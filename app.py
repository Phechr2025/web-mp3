
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
COOKIE_FILE = BASE_DIR / "cookies" / "cookies.txt"

DL_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

# In-memory job store
jobs = {}
jobs_lock = threading.Lock()

YOUTUBE_SINGLE_RE = re.compile(r"^https?://(www\.)?(youtube\.com|youtu\.be)/", re.I)
TIKTOK_RE = re.compile(r"^https?://([^/]+\.)?tiktok\.com/", re.I)
BILIBILI_RE = re.compile(r"^(?:https?://(?:[^/]+\.)?bilibili\.com/|https?://b23\.tv/)", re.I)

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


def run_download(job_id, url, fmt, title_override, quality=None, source="youtube"):
    outpath = DL_DIR / f"{job_id}.%(ext)s"
    opts = {
        "outtmpl": str(outpath),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [ydl_hook(job_id)],
    }
    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)

    if fmt == "mp3":
        # เสียงอย่างเดียว ใช้ bestaudio แล้วแปลงเป็น MP3
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
        ]
    else:  # mp4
        if source == "tiktok":
            # TikTok: เลือกฟอร์แมตจากรายการจริง แบ่งเป็น ต่ำสุด / กลาง / สูงสุด และพยายามเลี่ยงลายน้ำ
            probe_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
            }
            if COOKIE_FILE.exists():
                probe_opts["cookiefile"] = str(COOKIE_FILE)
            with yt_dlp.YoutubeDL(probe_opts) as info_ydl:
                info = info_ydl.extract_info(url, download=False)

            fmts = info.get("formats") or []
            video_fmts = [f for f in fmts if f.get("vcodec") not in (None, "none")]
            if not video_fmts:
                raise Exception("No video formats available for this TikTok video")

            # พยายามเลือกฟอร์แมตที่ไม่ติดลายน้ำก่อน (format_note ไม่ควรมีคำว่า watermark)
            def _no_watermark(f):
                note = str(f.get("format_note") or "").lower()
                return "watermark" not in note and "watermarked" not in note

            clean_fmts = [f for f in video_fmts if _no_watermark(f)]
            if clean_fmts:
                video_fmts = clean_fmts

            # เรียงตามความสูง / bitrate ให้ต่ำ → สูง
            video_fmts.sort(key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))

            if quality == "low":
                chosen = video_fmts[0]
            elif quality == "medium":
                chosen = video_fmts[len(video_fmts) // 2]
            else:
                chosen = video_fmts[-1]

            fmt_id = chosen.get("format_id")
            if not fmt_id:
                raise Exception("Cannot determine format id for chosen TikTok quality")

            opts["format"] = fmt_id
            opts["merge_output_format"] = "mp4"
        else:
            # แพลตฟอร์มทั่วไป (เช่น YouTube, Bilibili):
            # รองรับทั้งโหมด "โดยรวม" (low/medium/high) และ "เจาะจง" (เช่น 480p, 720p, 1080p)
            specified_height = None
            qstr = str(quality or "").lower()
            m = re.match(r"(\d+)", qstr)
            if m:
                try:
                    specified_height = int(m.group(1))
                except ValueError:
                    specified_height = None

            if specified_height:
                # โหมดเจาะจงความละเอียด (เช่น 480p/720p/1080p) เลือกความสูงที่ระบุแบบตรง ๆ
                h = specified_height
                opts["format"] = (
                    f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                    f"best[ext=mp4][height<={h}]/best[height<={h}]"
                )
            else:
                # โหมดโดยรวม (ต่ำ/กลาง/สูง) ให้เลือกจากความละเอียดที่มีจริงของวิดีโอ
                probe_opts2 = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                }
                if COOKIE_FILE.exists():
                    probe_opts2["cookiefile"] = str(COOKIE_FILE)
                with yt_dlp.YoutubeDL(probe_opts2) as info_ydl2:
                    info2 = info_ydl2.extract_info(url, download=False)

                fmts2 = info2.get("formats") or []
                video_fmts2 = [f for f in fmts2 if f.get("vcodec") not in (None, "none")]
                heights = sorted({f.get("height") for f in video_fmts2 if f.get("height")})
                if not heights:
                    # ถ้าไม่มีข้อมูลความสูง ใช้ best ตามปกติ
                    opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
                else:
                    min_h = min(heights)
                    max_h = max(heights)
                    # ค่ากลาง (median) สำหรับระดับกลาง
                    mid_h = heights[len(heights)//2]

                    if quality == "low":
                        # เลือกความสูงที่ใกล้กับ 240p ขึ้นไป ถ้าไม่มีให้ใช้ความสูงต่ำสุด
                        target = None
                        for h in heights:
                            if h >= 240:
                                target = h
                                break
                        if target is None:
                            target = min_h
                    elif quality == "medium":
                        target = mid_h
                    else:
                        # สูง = ใช้ความละเอียดสูงสุดของวิดีโอนั้น (อาจเป็น 1080p, 2K, 4K ฯลฯ)
                        target = max_h

                    h = int(target)
                    opts["format"] = (
                        f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                        f"best[ext=mp4][height<={h}]/best[height<={h}]"
                    )

        opts["merge_output_format"] = "mp4"
    if fmt == "mp4":
        opts["writethumbnail"] = True
        pp_list = opts.get("postprocessors") or []
        # แปลง thumbnail ให้เป็น JPG เพื่อให้ฝังได้ง่าย
        pp_list.append({
            "key": "FFmpegThumbnailsConvertor",
            "format": "jpg"
        })
        # ฝัง thumbnail เป็นปกไฟล์วิดีโอ
        pp_list.append({
            "key": "EmbedThumbnail"
        })
        opts["postprocessors"] = pp_list

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # ตำแหน่งไฟล์จริงที่ได้หลังโหลดเสร็จ
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
                "quality": quality if fmt == "mp4" else "",
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
    quality = (data.get("quality") or "").lower()
    source = (data.get("source") or "youtube").lower()

    # ถ้าเป็น MP4 แต่ไม่ได้ส่งคุณภาพมา ให้ตั้งเป็น "สูง" เป็นค่าเริ่มต้น
    if fmt == "mp4" and not quality:
        quality = "high"

    # ตรวจสอบ URL ตามแพลตฟอร์มที่เลือก
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "invalid_url"}), 400

    if source == "youtube":
        if not YOUTUBE_SINGLE_RE.search(url):
            return jsonify({"ok": False, "error": "invalid_url"}), 400
        if not is_single_video(url):
            return jsonify({"ok": False, "error": "not_single"}), 400
    elif source == "tiktok":
        if not TIKTOK_RE.search(url):
            return jsonify({"ok": False, "error": "invalid_url"}), 400
    elif source == "bilibili":
        if not BILIBILI_RE.search(url):
            return jsonify({"ok": False, "error": "invalid_url"}), 400
    else:
        return jsonify({"ok": False, "error": "bad_source"}), 400

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
    t = threading.Thread(target=run_download, args=(job_id, url, fmt, title, quality, source), daemon=True)
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