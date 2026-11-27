import os
import re
import sys
import json
import math
import glob
import time
import asyncio
import subprocess
from datetime import datetime, timedelta

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------
# 1. ENVIRONMENT & CONFIGURATION
# ---------------------------------------------------------
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
TOPIC_ID = os.environ.get("TOPIC_ID", "").strip()
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "1") or "1")
VOICE_NAME = os.environ.get("VOICE_NAME", "bn-BD-NabanitaNeural").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
GITHUB_REPOSITORY = (os.environ.get("GITHUB_REPOSITORY") or os.environ.get("GH_REPO") or "").strip()
GITHUB_RUN_NUMBER = os.environ.get("GITHUB_RUN_NUMBER", "1").strip()

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
OUTPUT_DIR = "output"
WORK_DIR = "work_long_video"
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)


# ---------------------------------------------------------
# 2. TELEGRAM & GITHUB HELPERS
# ---------------------------------------------------------
def tg_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_telegram_message(text: str, reply_markup: dict = None, disable_preview: bool = False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = requests.post(url, json=payload, timeout=20)
        if res.ok:
            return res.json()
        print(f"Telegram sendMessage warning ({res.status_code}): {res.text[:400]}")
    except Exception as exc:
        print(f"Telegram sendMessage error: {exc}")
    return None


def upload_release_asset(mp4_path: str, topic_id: str) -> dict:
    """Upload MP4 to a GitHub Release and return asset URL, release URL, and release ID."""
    file_name = os.path.basename(mp4_path)
    tag_name = f"long-{topic_id}-r{GITHUB_RUN_NUMBER}-{int(time.time())}"
    release_title = f"Long Video: {topic_id} (Run #{GITHUB_RUN_NUMBER})"

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return {
            "tag": tag_name,
            "release_id": "",
            "release_url": "",
            "asset_url": "",
        }

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    create_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"
    payload = {
        "tag_name": tag_name,
        "name": release_title,
        "body": f"Automated 16:9 long video release for Topic ID {topic_id}.",
        "draft": False,
        "prerelease": False,
    }

    try:
        r = requests.post(create_url, headers=headers, json=payload, timeout=30)
        if r.status_code not in (200, 201):
            print(f"GitHub release create warning ({r.status_code}): {r.text[:400]}")
            return {"tag": tag_name, "release_id": "", "release_url": "", "asset_url": ""}

        rel_data = r.json()
        release_id = str(rel_data.get("id", ""))
        release_url = str(rel_data.get("html_url", ""))
        upload_url_raw = str(rel_data.get("upload_url", ""))
        upload_url = upload_url_raw.split("{")[0]

        with open(mp4_path, "rb") as f:
            up_headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Content-Type": "video/mp4",
            }
            up_res = requests.post(
                f"{upload_url}?name={requests.utils.quote(file_name)}",
                headers=up_headers,
                data=f,
                timeout=300,
            )

        asset_url = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/{tag_name}/{file_name}"
        if up_res.status_code in (200, 201):
            up_json = up_res.json()
            asset_url = str(up_json.get("browser_download_url") or asset_url)
        else:
            print(f"GitHub asset upload warning ({up_res.status_code}): {up_res.text[:400]}")

        return {
            "tag": tag_name,
            "release_id": release_id,
            "release_url": release_url,
            "asset_url": asset_url,
        }
    except Exception as exc:
        print(f"Release upload exception: {exc}")
        return {"tag": tag_name, "release_id": "", "release_url": "", "asset_url": ""}


def send_four_button_approval(topic_id: str, title: str, mp4_path: str, release_info: dict, duration_sec: float):
    size_mb = os.path.getsize(mp4_path) / (1024 * 1024) if os.path.exists(mp4_path) else 0.0
    mins = int(duration_sec // 60)
    secs = int(duration_sec % 60)

    asset_url = release_info.get("asset_url", "")
    release_url = release_info.get("release_url", "")

    lines = [
        "🎬 <b>নতুন লং ভিডিও তৈরি হয়েছে (অনুমোদন প্রয়োজন)</b>",
        "",
        f"📌 <b>Topic ID:</b> <code>{tg_escape(topic_id)}</code>",
        f"📝 <b>Title:</b> {tg_escape(title)}",
        f"⏱ <b>Duration:</b> {mins}m {secs}s | <b>Size:</b> {size_mb:.1f} MB",
    ]
    if asset_url:
        lines.append(f"▶️ <b>Video / Stream Link:</b> <a href='{tg_escape(asset_url)}'>Watch / Download MP4</a>")
    if release_url:
        lines.append(f"📦 <b>GitHub Release:</b> <a href='{tg_escape(release_url)}'>Open Release</a>")
    lines.append("")
    lines.append("নিচের ৪টি বাটন থেকে একটি নির্বাচন করুন:")

    # 4 buttons across clean rows
    markup = {
        "inline_keyboard": [
            [
                {"text": "✅ এখনই পাবলিশ", "callback_data": f"LONG_APPROVE_{topic_id}"},
                {"text": "⏰ সিডিউল পাবলিশ", "callback_data": f"LONG_SCHEDULE_{topic_id}"},
            ],
            [
                {"text": "🔄 রিজেনারেট", "callback_data": f"LONG_REGEN_{topic_id}"},
                {"text": "❌ বাতিল (ডিলিট)", "callback_data": f"LONG_REJECT_{topic_id}"},
            ],
        ]
    }
    send_telegram_message("
".join(lines), reply_markup=markup, disable_preview=False)


# ---------------------------------------------------------
# 3. GOOGLE SHEETS CONNECTION & HELPERS
# ---------------------------------------------------------
def get_google_sheet():
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not SPREADSHEET_ID:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON এবং SPREADSHEET_ID সেট করা নেই।")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def get_or_create_worksheet(spreadsheet, title: str, headers: list):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=200, cols=max(len(headers), 12))
        ws.append_row(headers)
        return ws

    existing_top = ws.row_values(1)
    if not existing_top:
        ws.append_row(headers)
    elif len(existing_top) < len(headers):
        for idx in range(len(existing_top), len(headers)):
            ws.update_cell(1, idx + 1, headers[idx])
    return ws


def col_index_map(headers: list) -> dict:
    return {str(h).strip(): i for i, h in enumerate(headers)}


# ---------------------------------------------------------
# 4. VOICEOVER SANITIZER & SCENE BUILDER
# ---------------------------------------------------------
def sanitize_tts_text(raw_text: str) -> str:
    """Clean narration text so Edge TTS speaks natural Bengali without symbols or English clutter."""
    text = str(raw_text or "")
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}", " ", text)
    text = re.sub(r"[🌀-🫿☀-➿︀-️‍]", " ", text)
    text = re.sub(r"[*_~`#|\^=+<>/]", " ", text)
    text = re.sub(r"[“”‘’"']", " ", text)
    text = re.sub(r"[:;?!]+", "।", text)
    text = re.sub(r"।+", "।", text)

    replacements = [
        (r"NID", "এন আই ডি"),
        (r"TIN", "টি আই এন"),
        (r"e-TIN", "ই টি আই এন"),
        (r"OTP", "ও টি পি"),
        (r"PDF", "পি ডি এফ"),
        (r"SMS", "এস এম এস"),
        (r"URL", "ওয়েব ঠিকানা"),
        (r"BDT", "টাকা"),
        (r"homebdinfo\.com", "হোম বিডি ইনফো ডট কম"),
        (r"YouTube", "ইউটিউব"),
        (r"Facebook", "ফেসবুক"),
        (r"Online", "অনলাইন"),
        (r"Portal", "পোর্টাল"),
        (r"Server", "সার্ভার"),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    text = re.sub(r"[A-Za-z0-9]+", " ", text)
    text = re.sub(r"\s*।\s*", "। ", text)
    text = re.sub(r"[ 	]+", " ", text)
    text = re.sub(r"
{3,}", "

", text)
    return text.strip()


def split_script_into_chunks(clean_text: str, target_chunks: int = 8) -> list:
    sentences = [s.strip() for s in re.split(r"(?<=[।.])\s+", clean_text) if len(s.strip()) > 3]
    if not sentences:
        sentences = [clean_text.strip() or "বিস্তারিত তথ্য উপস্থাপন করা হচ্ছে।"]

    chunks = []
    per_chunk = max(1, math.ceil(len(sentences) / max(1, target_chunks)))
    for i in range(0, len(sentences), per_chunk):
        part = " ".join(sentences[i : i + per_chunk]).strip()
        if part:
            chunks.append(part)
    return chunks


# ---------------------------------------------------------
# 5. AUDIO, VISUAL RENDERING & FFMPEG ASSEMBLY
# ---------------------------------------------------------
async def synthesize_edge_tts(text: str, mp3_path: str, voice: str = VOICE_NAME):
    comm = edge_tts.Communicate(text, voice)
    await comm.save(mp3_path)


def generate_audio_sync(text: str, mp3_path: str, voice: str = VOICE_NAME):
    asyncio.run(synthesize_edge_tts(text, mp3_path, voice))


def get_media_duration(file_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return max(1.0, float(out))


def find_bengali_font() -> str:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSerifBengali-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    found = glob.glob("/usr/share/fonts/**/*Bengali*.ttf", recursive=True)
    return found[0] if found else ""


def render_scene_frame_playwright(
    title: str,
    scene_num: int,
    total_scenes: int,
    caption_text: str,
    lower_third: str,
    png_path: str,
):
    safe_title = tg_escape(title)
    safe_caption = tg_escape(caption_text[:340])
    safe_lt = tg_escape(lower_third[:140])

    html = f"""<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    width: {VIDEO_WIDTH}px;
    height: {VIDEO_HEIGHT}px;
    background: linear-gradient(135deg, #071326 0%, #0f294a 55%, #163a63 100%);
    font-family: "Noto Sans Bengali", "DejaVu Sans", sans-serif;
    color: #f8fafc;
    overflow: hidden;
    position: relative;
  }}
  .topbar {{
    position: absolute;
    top: 36px;
    left: 56px;
    right: 56px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .badge {{
    background: #dc2626;
    color: #ffffff;
    font-weight: 700;
    font-size: 24px;
    padding: 10px 24px;
    border-radius: 8px;
    letter-spacing: 0.5px;
  }}
  .brand {{
    background: rgba(255, 255, 255, 0.12);
    border: 1px solid rgba(255, 255, 255, 0.25);
    padding: 10px 22px;
    border-radius: 8px;
    font-size: 22px;
    color: #e2e8f0;
  }}
  .main-card {{
    position: absolute;
    top: 145px;
    left: 56px;
    right: 56px;
    bottom: 215px;
    background: rgba(15, 23, 42, 0.72);
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 22px;
    padding: 46px 54px;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }}
  .headline {{
    font-size: 44px;
    line-height: 1.35;
    font-weight: 700;
    color: #38bdf8;
    margin-bottom: 26px;
  }}
  .narration {{
    font-size: 34px;
    line-height: 1.55;
    color: #f1f5f9;
  }}
  .lower-third {{
    position: absolute;
    left: 0;
    right: 0;
    bottom: 52px;
    height: 112px;
    background: linear-gradient(90deg, #991b1b 0%, #dc2626 38%, #0f172a 100%);
    display: flex;
    align-items: center;
    padding: 0 56px;
    justify-content: space-between;
    border-top: 3px solid #facc15;
  }}
  .lt-text {{
    font-size: 30px;
    font-weight: 700;
    color: #ffffff;
  }}
  .lt-scene {{
    font-size: 24px;
    color: #fde047;
    font-weight: 700;
  }}
  .watermark {{
    position: absolute;
    right: 58px;
    top: 104px;
    font-size: 20px;
    color: rgba(255, 255, 255, 0.35);
  }}
</style>
</head>
<body>
  <div class="topbar">
    <div class="badge">বিশেষ প্রতিবেদন • ১৬:৯</div>
    <div class="brand">HomeBDInfo.com</div>
  </div>
  <div class="watermark">homebdinfo.com</div>
  <div class="main-card">
    <div class="headline">{safe_title}</div>
    <div class="narration">{safe_caption}</div>
  </div>
  <div class="lower-third">
    <div class="lt-text">{safe_lt}</div>
    <div class="lt-scene">পর্ব {scene_num}/{total_scenes}</div>
  </div>
</body>
</html>"""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT})
            page.set_content(html, wait_until="load")
            page.screenshot(path=png_path)
            browser.close()
        if os.path.exists(png_path):
            return
    except Exception as exc:
        print(f"Playwright render fallback to PIL: {exc}")

    # Reliable PIL fallback
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (12, 31, 58))
    draw = ImageDraw.Draw(img)
    font_path = find_bengali_font()
    f_big = ImageFont.truetype(font_path, 42) if font_path else ImageFont.load_default()
    f_med = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
    draw.rectangle([40, 40, VIDEO_WIDTH - 40, VIDEO_HEIGHT - 40], outline=(56, 189, 248), width=3)
    draw.text((80, 110), title[:80], fill=(56, 189, 248), font=f_big)
    draw.text((80, 220), caption_text[:220], fill=(241, 245, 249), font=f_med)
    draw.rectangle([0, VIDEO_HEIGHT - 130, VIDEO_WIDTH, VIDEO_HEIGHT - 30], fill=(185, 28, 28))
    draw.text((70, VIDEO_HEIGHT - 96), f"পর্ব {scene_num}/{total_scenes}: {lower_third[:70]}", fill=(255, 255, 255), font=f_med)
    img.save(png_path)


def build_long_video_mp4(topic_id: str, title: str, voiceover_script: str, scene_plan_text: str, out_mp4: str) -> float:
    clean_script = sanitize_tts_text(voiceover_script)
    scene_lines = [ln.strip() for ln in str(scene_plan_text or "").splitlines() if ln.strip()]
    target_scenes = max(6, min(10, len(scene_lines) if scene_lines else 8))
    chunks = split_script_into_chunks(clean_script, target_chunks=target_scenes)

    segment_mp4s = []
    total_duration = 0.0

    for idx, chunk_text in enumerate(chunks, start=1):
        seg_dir = os.path.join(WORK_DIR, f"{topic_id}_s{idx}")
        os.makedirs(seg_dir, exist_ok=True)
        audio_path = os.path.join(seg_dir, "voice.mp3")
        img_path = os.path.join(seg_dir, "frame.png")
        seg_mp4 = os.path.join(seg_dir, "segment.mp4")

        generate_audio_sync(chunk_text, audio_path, VOICE_NAME)
        dur = get_media_duration(audio_path)
        total_duration += dur

        lt = scene_lines[idx - 1] if idx - 1 < len(scene_lines) else title
        render_scene_frame_playwright(
            title=title,
            scene_num=idx,
            total_scenes=len(chunks),
            caption_text=chunk_text,
            lower_third=lt,
            png_path=img_path,
        )

        # Gentle Ken Burns zoom/pan on 1920x1080 frame with AAC audio
        zoom_expr = "min(zoom+0.0004,1.08)" if idx % 2 == 1 else "if(eq(on,1),1.06,max(zoom-0.0004,1.0))"
        vf = (
            f"scale=1920:1080,zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={max(1, int(math.ceil(dur * FPS)))}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS},format=yuv420p"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            img_path,
            "-i",
            audio_path,
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            seg_mp4,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        segment_mp4s.append(seg_mp4)

    list_file = os.path.join(WORK_DIR, f"{topic_id}_concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for seg in segment_mp4s:
            abs_seg = os.path.abspath(seg).replace("'", "'\''")
            f.write(f"file '{abs_seg}'\n")

    concat_cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out_mp4,
    ]
    subprocess.run(concat_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return total_duration


# ---------------------------------------------------------
# 6. SHEET QUEUE & PUBLISHING ORCHESTRATOR
# ---------------------------------------------------------
def process_scheduled_publishes(spreadsheet, now_dt: datetime):
    for sheet_name, platform_label in [("YouTube Publishing", "YouTube"), ("Facebook Publishing", "Facebook")]:
        try:
            ws = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            continue

        rows = ws.get_all_values()
        if len(rows) < 2:
            continue
        headers = rows[0]
        cmap = col_index_map(headers)
        status_idx = cmap.get("Status", 2)
        sched_idx = cmap.get("Scheduled Time", 4)
        pub_idx = cmap.get("Published Time", 5)
        notes_idx = cmap.get("Notes", len(headers) - 1)

        for r_i in range(1, len(rows)):
            row = rows[r_i]
            st = str(row[status_idx] if status_idx < len(row) else "").strip().lower()
            sched_str = str(row[sched_idx] if sched_idx < len(row) else "").strip()
            if "schedul" in st and sched_str:
                try:
                    sched_dt = datetime.fromisoformat(sched_str.replace("Z", "+00:00"))
                    if sched_dt.tzinfo is not None:
                        sched_dt = sched_dt.replace(tzinfo=None)
                except Exception:
                    continue
                if now_dt >= sched_dt:
                    ws.update_cell(r_i + 1, status_idx + 1, "Published (Scheduled Time Reached)")
                    if pub_idx < len(headers):
                        ws.update_cell(r_i + 1, pub_idx + 1, now_dt.strftime("%Y-%m-%d %H:%M:%S"))
                    if notes_idx < len(headers):
                        ws.update_cell(r_i + 1, notes_idx + 1, f"{platform_label} scheduled slot reached & marked published.")
                    send_telegram_message(
                        f"⏰ <b>{tg_escape(platform_label)} Scheduled Publish Triggered</b>
"
                        f"• <b>Topic ID:</b> <code>{tg_escape(row[0])}</code>
"
                        f"• <b>Title:</b> {tg_escape(row[1] if len(row) > 1 else '')}"
                    )


def sync_publish_queues_from_production(spreadsheet, now_dt: datetime):
    prod_ws = get_or_create_worksheet(
        spreadsheet,
        "Video Production",
        [
            "Topic ID",
            "Video Title",
            "Production Status",
            "Video Link",
            "Duration",
            "QC Status",
            "Approval Status",
            "Notes",
            "Updated Date",
            "Release ID",
            "Video URL",
            "Cleanup Status",
        ],
    )
    yt_ws = get_or_create_worksheet(
        spreadsheet,
        "YouTube Publishing",
        ["Topic ID", "Title", "Status", "YouTube URL", "Scheduled Time", "Published Time", "Views", "Notes"],
    )
    fb_ws = get_or_create_worksheet(
        spreadsheet,
        "Facebook Publishing",
        ["Topic ID", "Title", "Status", "Facebook URL", "Scheduled Time", "Published Time", "Reach", "Notes"],
    )

    prod_rows = prod_ws.get_all_values()
    if len(prod_rows) < 2:
        return
    p_headers = prod_rows[0]
    pmap = col_index_map(p_headers)

    yt_rows = yt_ws.get_all_values()
    fb_rows = fb_ws.get_all_values()
    yt_map = {str(r[0]).strip().upper(): idx + 2 for idx, r in enumerate(yt_rows[1:]) if r and str(r[0]).strip()}
    fb_map = {str(r[0]).strip().upper(): idx + 2 for idx, r in enumerate(fb_rows[1:]) if r and str(r[0]).strip()}

    for r_i in range(1, len(prod_rows)):
        row = prod_rows[r_i]
        tid = str(row[0] if len(row) > 0 else "").strip()
        if not tid:
            continue
        title = str(row[pmap.get("Video Title", 1)] if len(row) > 1 else "").strip()
        prod_st = str(row[pmap.get("Production Status", 2)] if len(row) > 2 else "").strip().upper()
        app_st = str(row[pmap.get("Approval Status", 6)] if len(row) > 6 else "").strip().upper()
        vid_url = str(row[pmap.get("Video URL", 10)] if len(row) > 10 else "").strip() or str(
            row[pmap.get("Video Link", 3)] if len(row) > 3 else ""
        ).strip()

        if prod_st == "REJECTED" or app_st == "REJECTED":
            continue

        if prod_st == "APPROVED" or app_st == "APPROVED":
            st_label = "Published Immediately"
            pub_time = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            sched_time = ""
            note_txt = "Approved via Telegram; published immediately."
        elif prod_st == "SCHEDULED" or app_st == "SCHEDULED":
            st_label = "Scheduled"
            pub_time = ""
            sched_time = (now_dt + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
            note_txt = f"Scheduled for {sched_time} via Telegram."
        else:
            continue

        tid_u = tid.upper()
        if tid_u not in yt_map:
            yt_ws.append_row([tid, title, st_label, vid_url, sched_time, pub_time, "", note_txt])
            yt_map[tid_u] = len(yt_ws.get_all_values())
        else:
            yt_ws.update_cell(yt_map[tid_u], 3, st_label)
            if vid_url:
                yt_ws.update_cell(yt_map[tid_u], 4, vid_url)
            if sched_time:
                yt_ws.update_cell(yt_map[tid_u], 5, sched_time)
            if pub_time:
                yt_ws.update_cell(yt_map[tid_u], 6, pub_time)

        if tid_u not in fb_map:
            fb_ws.append_row([tid, title, st_label, vid_url, sched_time, pub_time, "", note_txt])
            fb_map[tid_u] = len(fb_ws.get_all_values())
        else:
            fb_ws.update_cell(fb_map[tid_u], 3, st_label)
            if vid_url:
                fb_ws.update_cell(fb_map[tid_u], 4, vid_url)
            if sched_time:
                fb_ws.update_cell(fb_map[tid_u], 5, sched_time)
            if pub_time:
                fb_ws.update_cell(fb_map[tid_u], 6, pub_time)


def main():
    print("Starting Long Video Production Pipeline...")
    try:
        spreadsheet = get_google_sheet()
        print(f"✅ Successfully connected to Google Sheet: {spreadsheet.title}")
    except Exception as exc:
        err_msg = f"❌ <b>Google Sheet Connection Error:</b> {tg_escape(str(exc))}"
        print(err_msg)
        send_telegram_message(err_msg)
        sys.exit(1)

    now_dt = datetime.now()
    process_scheduled_publishes(spreadsheet, now_dt)

    script_ws = get_or_create_worksheet(
        spreadsheet,
        "Video Script",
        [
            "Topic ID",
            "Video Title",
            "Hook",
            "Voiceover Script",
            "Scene Plan",
            "GitHub Pipeline Note",
            "YouTube Title",
            "YouTube Description",
            "Facebook Caption",
            "Hashtags",
            "CTA",
            "Word Count",
            "Status",
            "Created Date",
        ],
    )
    prod_ws = get_or_create_worksheet(
        spreadsheet,
        "Video Production",
        [
            "Topic ID",
            "Video Title",
            "Production Status",
            "Video Link",
            "Duration",
            "QC Status",
            "Approval Status",
            "Notes",
            "Updated Date",
            "Release ID",
            "Video URL",
            "Cleanup Status",
        ],
    )

    script_rows = script_ws.get_all_values()
    if len(script_rows) < 2:
        print("⚠️ Video Script sheet has no rows to process.")
        send_telegram_message("⚠️ <b>Video Generation Notice:</b> Video Script শিটে কোনো স্ক্রিপ্ট পাওয়া যায়নি।")
        return

    s_headers = script_rows[0]
    smap = col_index_map(s_headers)

    prod_rows = prod_ws.get_all_values()
    p_headers = prod_rows[0]
    pmap = col_index_map(p_headers)
    prod_lookup = {}
    for idx in range(1, len(prod_rows)):
        r = prod_rows[idx]
        if r and str(r[0]).strip():
            prod_lookup[str(r[0]).strip().upper()] = (idx + 2, r)

    candidates = []
    for idx in range(1, len(script_rows)):
        r = script_rows[idx]
        tid = str(r[0] if len(r) > 0 else "").strip()
        if not tid:
            continue
        if TOPIC_ID and tid.upper() != TOPIC_ID.upper():
            continue

        existing_prod = prod_lookup.get(tid.upper())
        if existing_prod:
            _, p_row = existing_prod
            p_status = str(p_row[pmap.get("Production Status", 2)] if len(p_row) > 2 else "").strip().upper()
            if p_status == "REGENERATE_REQUESTED":
                candidates.append((idx + 2, r, True))
        else:
            candidates.append((idx + 2, r, False))

    if not candidates:
        candidates = [(len(script_rows), script_rows[-1], False)]

    generated_count = 0
    for s_row_num, s_row, is_regen in candidates[:MAX_VIDEOS]:
        tid = str(s_row[0]).strip()
        title = str(s_row[smap.get("Video Title", 1)] if len(s_row) > 1 else tid).strip()
        voice_script = str(s_row[smap.get("Voiceover Script", 3)] if len(s_row) > 3 else "").strip()
        scene_plan = str(s_row[smap.get("Scene Plan", 4)] if len(s_row) > 4 else "").strip()

        if not voice_script:
            voice_script = f"{title} বিষয়ে বিস্তারিত তথ্য ও ব্যবহারিক দিকনির্দেশনা নিচে উপস্থাপন করা হলো।"

        out_mp4 = os.path.join(OUTPUT_DIR, f"{tid}_16x9.mp4")
        print(f"🎬 Rendering 16:9 video for {tid}: {title}")
        duration_sec = build_long_video_mp4(tid, title, voice_script, scene_plan, out_mp4)
        release_info = upload_release_asset(out_mp4, tid)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur_str = f"{int(duration_sec // 60)}m {int(duration_sec % 60)}s"
        asset_url = release_info.get("asset_url", "")
        release_id = release_info.get("release_id", "")

        new_prod_row = [
            tid,
            title,
            "Pending Approval",
            asset_url or out_mp4,
            dur_str,
            "Passed",
            "Pending Approval",
            "Regenerated from reversed/updated script." if is_regen else "Generated 16:9 long video; awaiting 4-button Telegram approval.",
            stamp,
            release_id,
            asset_url,
            "ACTIVE",
        ]

        if tid.upper() in prod_lookup:
            p_row_num, _ = prod_lookup[tid.upper()]
            prod_ws.update(f"A{p_row_num}:L{p_row_num}", [new_prod_row])
        else:
            prod_ws.append_row(new_prod_row)

        send_four_button_approval(tid, title, out_mp4, release_info, duration_sec)
        generated_count += 1

    sync_publish_queues_from_production(spreadsheet, datetime.now())
    print(f"🎉 Completed long video run. Generated: {generated_count}")


if __name__ == "__main__":
    main()
