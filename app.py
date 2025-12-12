
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
import requests
import shutil

load_dotenv()

APP_VERSION = os.getenv("APP_VERSION", "v1.0")
PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASS = os.getenv("PANEL_PASS", "mp")
PORT = int(os.getenv("PORT", "8090"))
DOWNLOAD_ENABLED_FLAG = os.getenv("DOWNLOAD_ENABLED", "1")  # "1" enabled / "0" disabled

BASE_DIR = Path(__file__).parent.resolve()
DL_DIR = BASE_DIR / "downloads"
LOG_DIR = BASE_DIR / "logs"

HISTORY_FILE = LOG_DIR / "history.json"
COOKIE_FILE = BASE_DIR / "cookies" / "cookies.txt"
PLATFORM_FILE = BASE_DIR / "platforms.json"
SETTINGS_FILE = BASE_DIR / "settings.json"

# ค่าปริยายของแพลตฟอร์มที่เปิดให้ใช้งาน
DEFAULT_PLATFORMS = {
    "youtube": True,
    "tiktok": True,
    "bilibili": True,
    "instagram": True,
    "facebook": True,
}

# ค่าปริยายของการตั้งค่าทั่วไป
DEFAULT_SETTINGS = {
    "youtube_use_cookies": True,
    # เปิด/ปิดการลบไฟล์เก่าจากโฟลเดอร์ดาวน์โหลดอัตโนมัติ
    # 300 วินาที (5 นาที) และ 600 วินาที (10 นาที)
    "auto_sweep_300_enabled": True,
    "auto_sweep_600_enabled": False,
}

def _load_platform_flags():
    """โหลดสถานะแพลตฟอร์มที่อนุญาตจากไฟล์ (ถ้าไม่มีให้ใช้ค่าเริ่มต้น)"""
    data = {}
    try:
        if PLATFORM_FILE.exists():
            data = json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    flags = DEFAULT_PLATFORMS.copy()
    for k in list(flags.keys()):
        if isinstance(data.get(k), bool):
            flags[k] = data[k]
    return flags

def _load_settings():
    """โหลดการตั้งค่าทั่วไป เช่น โหมด cookies ของ YouTube"""
    data = {}
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    settings = DEFAULT_SETTINGS.copy()
    for k in list(settings.keys()):
        if isinstance(data.get(k), type(settings[k])):
            settings[k] = data[k]
    return settings

# เก็บไว้ในหน่วยความจำ
PLATFORM_FLAGS = _load_platform_flags()
SETTINGS = _load_settings()
YT_USE_COOKIES = SETTINGS.get("youtube_use_cookies", True)
AUTO_SWEEP_300_ENABLED = SETTINGS.get("auto_sweep_300_enabled", True)
AUTO_SWEEP_600_ENABLED = SETTINGS.get("auto_sweep_600_enabled", False)


DL_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

# In-memory job store
jobs = {}
jobs_lock = threading.Lock()

def schedule_delete_file(path: str, delay_seconds: int = 60) -> None:
    """ลบไฟล์ออกจากระบบหลังจากผ่านไป delay_seconds วินาที
    ใช้เพื่อลดการใช้พื้นที่บน Render หลังจากผู้ใช้ดาวน์โหลดไฟล์เสร็จแล้ว"""
    def _worker() -> None:
        try:
            # หน่วงเวลาให้ผู้ใช้ดาวน์โหลดไฟล์ให้เสร็จก่อน
            time.sleep(delay_seconds)
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:  # แค่ log ไว้ ไม่ให้เว็บล่ม
            app.logger.warning("schedule_delete_file error for %s: %s", path, e)

    threading.Thread(target=_worker, daemon=True).start()

SWEEP_INTERVAL_SECONDS = int(os.getenv("SWEEP_INTERVAL_SECONDS", "600"))
SWEEP_MAX_AGE_SECONDS = int(os.getenv("SWEEP_MAX_AGE_SECONDS", "300"))


def sweep_download_folder_once() -> None:
    """ลบไฟล์ในโฟลเดอร์ดาวน์โหลดที่เก่ากว่า SWEEP_MAX_AGE_SECONDS เพื่อคืนพื้นที่"""
    now = time.time()
    try:
        DL_DIR.mkdir(parents=True, exist_ok=True)
        for path in DL_DIR.iterdir():
            if path.is_file():
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                # ลบไฟล์ที่เก่ากว่าเวลาที่กำหนด
                if now - mtime > SWEEP_MAX_AGE_SECONDS:
                    try:
                        path.unlink()
                    except OSError:
                        continue
    except Exception as e:
        # ไม่ให้แครชทั้งแอป ถ้ามีปัญหาในการลบไฟล์
        app.logger.warning("sweep_download_folder_once error: %s", e)


def start_sweep_scheduler() -> None:
    """สร้างเธรดเบื้องหลังสำหรับสแกนและลบไฟล์เก่าเป็นระยะ ๆ

    เพิ่ม 2 โหมดให้เลือกจากหลังบ้าน:
    - ลบทุก ๆ 300 วินาที (5 นาที)
    - ลบทุก ๆ 600 วินาที (10 นาที)
    """

    def _loop_300() -> None:
        # วนลูปตลอดเวลา แต่จะทำงานจริงเฉพาะตอนที่โหมด 300s ถูกเปิดไว้
        while True:
            if AUTO_SWEEP_300_ENABLED:
                sweep_download_folder_once()
            time.sleep(300)

    def _loop_600() -> None:
        # วนลูปตลอดเวลา แต่จะทำงานจริงเฉพาะตอนที่โหมด 600s ถูกเปิดไว้
        while True:
            if AUTO_SWEEP_600_ENABLED:
                sweep_download_folder_once()
            time.sleep(600)

    t300 = threading.Thread(target=_loop_300, daemon=True)
    t600 = threading.Thread(target=_loop_600, daemon=True)
    t300.start()
    t600.start()


# สตาร์ทตัวลบไฟล์เก่าอัตโนมัติ (ทำงานควบคู่กับ schedule_delete_file)
start_sweep_scheduler()

# ฟังก์ชันช่วยคำนวณพื้นที่โฟลเดอร์ดาวน์โหลดและสภาพพื้นที่ดิสก์
def _get_storage_info() -> dict:
    DL_DIR.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    files_count = 0
    # รวมขนาดไฟล์ทั้งหมดในโฟลเดอร์ดาวน์โหลด (รวมซับโฟลเดอร์ ถ้ามี)
    for root, dirs, files in os.walk(DL_DIR):
        for name in files:
            try:
                fp = Path(root) / name
                total_bytes += fp.stat().st_size
                files_count += 1
            except OSError:
                continue

    info = {
        "downloads_bytes": total_bytes,
        "downloads_files": files_count,
    }
    try:
        usage = shutil.disk_usage(DL_DIR)
        info.update(
            {
                "disk_total_bytes": usage.total,
                "disk_used_bytes": usage.used,
                "disk_free_bytes": usage.free,
            }
        )
    except Exception:
        # ถ้าดึงข้อมูลดิสก์ไม่ได้ ให้ส่งเฉพาะข้อมูลโฟลเดอร์ดาวน์โหลด
        pass
    return info


def _delete_all_downloads() -> int:
    """ลบทุกอย่างที่อยู่ในโฟลเดอร์ดาวน์โหลด (แบบ manual / ตามปุ่มในหลังบ้าน)"""
    deleted = 0
    try:
        DL_DIR.mkdir(parents=True, exist_ok=True)
        for path in DL_DIR.iterdir():
            try:
                if path.is_file():
                    path.unlink()
                    deleted += 1
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    deleted += 1
            except OSError:
                continue
    except Exception as e:
        try:
            app.logger.warning("delete_all_downloads error: %s", e)
        except Exception:
            print("delete_all_downloads error:", e)
    return deleted


YOUTUBE_SINGLE_RE = re.compile(r"^https?://(www\.)?(youtube\.com|youtu\.be)/", re.I)
TIKTOK_RE = re.compile(r"^https?://([^/]+\.)?tiktok\.com/", re.I)
BILIBILI_RE = re.compile(r"^https?://(?:(?:[^/]+\.)?bilibili\.com|b23\.tv|bili\.im)/", re.I)
INSTAGRAM_RE = re.compile(r"^https?://(www\.)?(instagram\.com|instagr\.am)/", re.I)
FACEBOOK_RE = re.compile(r"^https?://(www\.|m\.)?(facebook\.com|fb\.watch)/", re.I)


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
    # ใช้ cookies.txt เฉพาะ YouTube และเฉพาะเมื่อถูกเปิดไว้ในแผงหลังบ้าน
    if source == "youtube" and YT_USE_COOKIES and COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)

    # ปรับการตั้งค่าเครือข่ายสำหรับ Bilibili (ช่วยให้คลิปยาวโหลดเสถียรขึ้น)
    if source == "bilibili":
        # ดาวน์โหลดเป็นชิ้น ๆ เพื่อลดโอกาสโหลดหลุดกลางทาง
        opts["http_chunk_size"] = 10 * 1024 * 1024  # 10MB ต่อชิ้น
        # ลดจำนวน fragment ที่โหลดพร้อมกัน (กันโดนจำกัดจากฝั่งเซิร์ฟเวอร์)
        opts["concurrent_fragment_downloads"] = 1
        # เพิ่มโอกาส retry เวลาเน็ตแกว่งหรือโดนตัดกลางทาง (คลิปยาวๆ)
        opts["retries"] = 50
        opts["fragment_retries"] = 50
        # ถ้ามี cookies.txt ให้ใช้ร่วมกับ Bilibili ด้วย (ช่วยเรื่องคลิปยาว / จำกัดสิทธิ์)
        if COOKIE_FILE.exists():
            opts["cookiefile"] = str(COOKIE_FILE)
        opts["socket_timeout"] = 120

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

        elif source == "instagram":
            # Instagram: เน้นความเสถียรของไฟล์ MP4 เป็นหลัก
            # ใช้ฟอร์แมต MP4 ที่แพร่หลาย แล้วให้ FFmpeg แปลง/จัดโครงสร้างใหม่ให้เล่นได้ลื่นในหลายอุปกรณ์
            q = (quality or "").lower()
            # กำหนดเพดานความสูงแบบง่าย ๆ ตามระดับคุณภาพ
            if q == "low":
                max_h = 480
            elif q == "medium":
                max_h = 720
            else:
                # high หรือไม่ได้ระบุ → ให้ใช้ถึง 1080p เป็นเพดาน
                max_h = 1080

            # พยายามเลือกเฉพาะ MP4 ถ้าไม่มีให้ fallback เป็น best ทั่วไป
            opts["format"] = (
                f"best[ext=mp4][height<={max_h}]/"
                f"bestvideo[ext=mp4][height<={max_h}]+bestaudio/best/"
                f"best"
            )
            opts["merge_output_format"] = "mp4"

            # ให้ FFmpeg ช่วยจัดโครงสร้างไฟล์ MP4 ใหม่ เพื่อลดอาการกระตุก/เวลาไม่ตรง
            pp_list = opts.get("postprocessors") or []
            pp_list.append({
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            })
            opts["postprocessors"] = pp_list
            # ใส่ movflags=faststart ให้เล่นได้ลื่นขึ้นในบาง player (โดยเฉพาะบนมือถือ/เบราว์เซอร์)
            pp_args = opts.get("postprocessor_args") or {}
            pp_args.setdefault("FFmpegVideoConvertor", ["-movflags", "faststart"])
            opts["postprocessor_args"] = pp_args

        else:
            # แพลตฟอร์มทั่วไป (เช่น YouTube, Bilibili, Instagram ถ้าไม่ได้เข้าเงื่อนไขด้านบน):
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
                # โหมดเจาะจง: ใช้ค่าความสูงที่ผู้ใช้เลือกและตรวจคุณภาพให้ตรงที่สุด
                h_req = int(specified_height)

                # ตรวจสอบความละเอียดวิดีโอที่มีอยู่จริง (ใช้เฉพาะ track วิดีโอ)
                probe_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                }
                if source == "youtube" and YT_USE_COOKIES and COOKIE_FILE.exists():
                    probe_opts["cookiefile"] = str(COOKIE_FILE)

                heights = []
                try:
                    with yt_dlp.YoutubeDL(probe_opts) as probe_ydl:
                        info2 = probe_ydl.extract_info(url, download=False)
                    fmts2 = info2.get("formats") or []
                    for f in fmts2:
                        vcodec = f.get("vcodec")
                        if vcodec and vcodec != "none":
                            h_av = f.get("height")
                            if isinstance(h_av, int):
                                heights.append(h_av)
                except Exception:
                    heights = []

                if heights:
                    max_h_avail = max(heights)
                    if h_req > max_h_avail:
                        # คุณภาพที่เลือกสูงกว่าต้นฉบับ → อัปเดตสถานะเป็น error แล้วจบงานเลย
                        with jobs_lock:
                            jobs[job_id]["status"] = "error"
                            jobs[job_id]["error"] = (
                                f"คุณภาพที่คุณเลือก ({h_req}p) สูงกว่าคลิปต้นฉบับ สูงสุดของต้นฉบับอยู่ที่ {max_h_avail}p"
                            )
                        return

                    # เลือกความละเอียดที่ใกล้เคียงกับที่ผู้ใช้เลือกมากที่สุด
                    target_h = min(heights, key=lambda hh: abs(hh - h_req))
                    h = int(target_h)
                else:
                    # ถ้าไม่สามารถ probe ความละเอียดได้ ให้ fallback ใช้ค่าที่ผู้ใช้เลือกเป็นเพดานเดิม
                    h = h_req

                # ตั้งค่า format string สำหรับดาวน์โหลด MP4
                if source == "bilibili":
                    # ไม่บังคับนามสกุลไฟล์ แต่จำกัดตามความสูง
                    opts["format"] = (
                        f"bestvideo[height<={h}]+bestaudio/"
                        f"best[height<={h}]/best"
                    )
                else:
                    # แพลตฟอร์มอื่น ๆ (เช่น YouTube) เน้นไฟล์ MP4 ให้มากที่สุด
                    opts["format"] = (
                        f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                        f"best[ext=mp4][height<={h}]/best[height<={h}]"
                    )
            else:
                # โหมดโดยรวม (low/medium/high) ให้เลือกความละเอียดแบบไดนามิกตามวิดีโอ
                probe_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                }
                if source == "youtube" and YT_USE_COOKIES and COOKIE_FILE.exists():
                    probe_opts["cookiefile"] = str(COOKIE_FILE)
                try:
                    with yt_dlp.YoutubeDL(probe_opts) as probe_ydl:
                        info2 = probe_ydl.extract_info(url, download=False)
                    fmts2 = info2.get("formats") or []
                    video_fmts2 = [f for f in fmts2 if f.get("vcodec") not in (None, "none")]
                    heights = sorted({f.get("height") for f in video_fmts2 if f.get("height")})
                except Exception:
                    heights = []
                if not heights:
                    # ถ้าอ่านความละเอียดไม่ได้ ให้ fallback ไปใช้ mapping เดิม
                    max_h = 1080
                    if quality == "medium":
                        max_h = 720
                    elif quality == "low":
                        max_h = 480
                    if source == "bilibili":
                        opts["format"] = (
                            f"bestvideo[height<={max_h}]+bestaudio/"
                            f"best[height<={max_h}]/best"
                        )
                    else:
                        opts["format"] = (
                            f"bestvideo[ext=mp4][height<={max_h}]+bestaudio[ext=m4a]/"
                            f"best[ext=mp4][height<={max_h}]/best[height<={max_h}]"
                        )
                else:
                    min_h = min(heights)
                    max_h = max(heights)
                    mid_h = heights[len(heights)//2]

                    if quality == "low":
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
                        target = max_h

                    h = int(target)
                    if source == "bilibili":
                        opts["format"] = (
                            f"bestvideo[height<={h}]+bestaudio/"
                            f"best[height<={h}]/best"
                        )
                    else:
                        opts["format"] = (
                            f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                            f"best[ext=mp4][height<={h}]/best[height<={h}]"
                        )
            opts["merge_output_format"] = "mp4"

    # ถ้าเป็นไฟล์วิดีโอ MP4 ให้ดึง thumbnail มาด้วย (ไม่ฝังลงไฟล์เพื่อลดบั๊ก)
    if fmt == "mp4":
        opts["writethumbnail"] = True

    try:
        # แก้กรณีลิงก์สั้น Bilibili เช่น https://bili.im/XXXX ให้ตาม redirect ก่อน
        if "bili.im/" in url:
            try:
                r = requests.get(url, allow_redirects=True, timeout=10)
                if r.url:
                    url = r.url
            except Exception:
                pass

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise Exception("Failed to fetch video info")

            # ตำแหน่งไฟล์จริงที่ได้หลังโหลดเสร็จ
            if fmt == "mp3":
                file_path = DL_DIR / f"{job_id}.mp3"
            else:
                file_path = DL_DIR / f"{job_id}.mp4"

            # กำหนดชื่อไฟล์ที่จะแสดงตอนดาวน์โหลด
            if title_override and title_override.strip():
                display_name = title_override.strip()
            else:
                # ถ้าไม่กำหนดชื่อเอง ให้ใช้ข้อมูลจากคลิป
                meta_title = (info.get("title") or "").strip()

                # สำหรับ Instagram / Facebook ให้ลองใช้ description (แคปชัน) เป็นชื่อก่อน
                if source in ("instagram", "facebook"):
                    desc = (info.get("description") or "").strip()
                    # ถ้า description มีข้อความจริง ให้ใช้เป็นชื่อหลัก
                    if desc:
                        meta_title = desc

                # กันชื่อไฟล์ยาวเกินหรือมีตัวอักษรแปลก ๆ
                if not meta_title:
                    meta_title = "download"

                # ลบขึ้นบรรทัดใหม่ และอักขระต้องห้ามในชื่อไฟล์ออก
                meta_title = meta_title.replace("\n", " ").replace("\r", " ")
                meta_title = re.sub(r'[\\/*?:"<>|]', "_", meta_title)
                # ตัดความยาวไม่ให้เกิน 120 ตัวอักษร (กันชื่อยาวเกิน)
                if len(meta_title) > 120:
                    meta_title = meta_title[:120].rstrip()

                display_name = meta_title

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
    return render_template("index.html", enabled=enabled, platforms=PLATFORM_FLAGS)

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


    # ตรวจว่าแพลตฟอร์มนี้ถูกเปิดใช้งานในแผงหลังบ้านหรือไม่
    if not PLATFORM_FLAGS.get(source, False):
        return jsonify({"ok": False, "error": "source_disabled"}), 403

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
    elif source == "instagram":
        if not INSTAGRAM_RE.search(url):
            return jsonify({"ok": False, "error": "invalid_url"}), 400
    elif source == "facebook":
        if not FACEBOOK_RE.search(url):
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
        file_path = job["file"]
        filename = job.get("title") or Path(file_path).name

    # หลังจากตอบไฟล์ให้ผู้ใช้แล้ว ให้ลบไฟล์ในระบบออกภายใน ~60 วินาที
    schedule_delete_file(file_path, delay_seconds=60)

    return send_file(file_path, as_attachment=True, download_name=filename + Path(file_path).suffix)

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
        hist = []
    # ส่งสถานะแพลตฟอร์มและการตั้งค่า YouTube ไปด้วย
    return render_template(
        "admin.html",
        enabled=enabled,
        history=hist[-100:][::-1],
        platforms=PLATFORM_FLAGS,
        yt_use_cookies=YT_USE_COOKIES,
        auto_sweep_300_enabled=AUTO_SWEEP_300_ENABLED,
        auto_sweep_600_enabled=AUTO_SWEEP_600_ENABLED,
    )

@app.get("/admin/storage-info")
def admin_storage_info():
    if not _login_required():
        return jsonify({"error": "unauthorized"}), 401
    info = _get_storage_info()
    # ส่งสถานะโหมดลบอัตโนมัติไปด้วย เผื่อใช้ในอนาคต
    info.update(
        {
            "auto_sweep_300_enabled": AUTO_SWEEP_300_ENABLED,
            "auto_sweep_600_enabled": AUTO_SWEEP_600_ENABLED,
        }
    )
    return jsonify(info)


@app.post("/admin/sweep-settings")
def admin_sweep_settings():
    if not _login_required():
        return "unauthorized", 401

    sweep_300 = request.form.get("sweep_300", "0") == "1"
    sweep_600 = request.form.get("sweep_600", "0") == "1"

    # บันทึกลง settings.json ให้คงอยู่หลังรีสตาร์ท
    try:
        data = {}
        if SETTINGS_FILE.exists():
            raw = SETTINGS_FILE.read_text(encoding="utf-8") or "{}"
            data = json.loads(raw)
    except Exception:
        data = {}
    data["auto_sweep_300_enabled"] = bool(sweep_300)
    data["auto_sweep_600_enabled"] = bool(sweep_600)
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("settings write error:", e)

    # โหลดกลับเข้า memory
    global SETTINGS, AUTO_SWEEP_300_ENABLED, AUTO_SWEEP_600_ENABLED
    SETTINGS = _load_settings()
    AUTO_SWEEP_300_ENABLED = SETTINGS.get("auto_sweep_300_enabled", True)
    AUTO_SWEEP_600_ENABLED = SETTINGS.get("auto_sweep_600_enabled", False)

    flash("อัปเดตการลบไฟล์อัตโนมัติแล้ว", "ok")
    return redirect(url_for("admin_home"))


@app.post("/admin/delete-downloads")
def admin_delete_downloads():
    if not _login_required():
        return "unauthorized", 401
    deleted = _delete_all_downloads()
    flash(f"ลบไฟล์ในโฟลเดอร์ดาวน์โหลดแล้ว {deleted} รายการ", "ok")
    return redirect(url_for("admin_home"))

@app.post("/admin/toggle")
def admin_toggle():
    if not _login_required():
        return "unauthorized", 401
    val = request.form.get("enable") == "1"
    os.environ["DOWNLOAD_ENABLED"] = "1" if val else "0"
    flash("เปิดดาวน์โหลด" if val else "ปิดดาวน์โหลด", "ok")
    return redirect(url_for("admin_home"))


@app.post("/admin/platforms")
def admin_platforms():
    if not _login_required():
        return "unauthorized", 401

    # รายการแพลตฟอร์มที่ติ๊กถูกจากฟอร์ม
    selected = request.form.getlist("platforms")

    flags = DEFAULT_PLATFORMS.copy()
    for key in flags.keys():
        flags[key] = (key in selected)

    # บันทึกลงไฟล์ให้คงอยู่หลังรีสตาร์ท
    try:
        PLATFORM_FILE.write_text(json.dumps(flags, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    # โหลดกลับเข้า memory
    global PLATFORM_FLAGS
    PLATFORM_FLAGS = _load_platform_flags()

    flash("อัปเดตแพลตฟอร์มที่อนุญาตแล้ว", "ok")
    return redirect(url_for("admin_home"))


@app.post("/admin/youtube-mode")
def admin_youtube_mode():
    if not _login_required():
        return "unauthorized", 401

    mode = request.form.get("yt_mode") or "cookies"
    use_cookies = (mode == "cookies")

    # บันทึกลงไฟล์ settings.json
    data = {}
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["youtube_use_cookies"] = use_cookies
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("settings write error:", e)

    # อัปเดตค่าในหน่วยความจำ
    global SETTINGS, YT_USE_COOKIES
    SETTINGS = _load_settings()
    YT_USE_COOKIES = SETTINGS.get("youtube_use_cookies", True)

    flash("อัปเดตโหมดการดาวน์โหลด YouTube แล้ว", "ok")
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
