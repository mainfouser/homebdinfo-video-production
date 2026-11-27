import os
import re
import gc
import sys
import json
import math
import time
import asyncio
import tempfile
import subprocess
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------
# 1. ENVIRONMENT & LONG-VIDEO CONFIGURATION (16:9)
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

BGM_FILE = "bgm.mp3"
LOGO_FILE = "logo.png"

BGM_VOICE_VOLUME = 0.13
BGM_OUTRO_VOLUME = 0.19

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
HOOK_DURATION = 5.0
COVER_DURATION = 0.5
OUTRO_DURATION = 5.0

OUTPUT_DIR = "output"
WORK_DIR_ROOT = "work_long_video"
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(WORK_DIR_ROOT, exist_ok=True)

BENGALI_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-SemiBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
]
LATIN_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def find_font(paths: List[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    for root, _, files in os.walk("/usr/share/fonts"):
        for f in files:
            if "Bengali" in f and f.endswith((".ttf", ".otf")):
                try:
                    return ImageFont.truetype(os.path.join(root, f), size)
                except Exception:
                    pass
    return ImageFont.load_default()


# ---------------------------------------------------------
# 2. TELEGRAM STREAMING PLAYER & GITHUB RELEASE HELPERS
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
        res = requests.post(url, json=payload, timeout=25)
        if res.ok:
            return res.json()
        print(f"Telegram sendMessage warning ({res.status_code}): {res.text[:400]}")
    except Exception as exc:
        print(f"Telegram sendMessage error: {exc}")
    return None


def upload_release_asset(mp4_path: str, thumb_path: str, topic_id: str) -> dict:
    file_name = os.path.basename(mp4_path)
    tag_name = f"long-{topic_id}-r{GITHUB_RUN_NUMBER}-{int(time.time())}"
    release_title = f"HomeBDInfo Long Video: {topic_id} (Run #{GITHUB_RUN_NUMBER})"

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
        "body": f"Automated 16:9 long video release for Topic ID {topic_id} • HomeBDInfo.",
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
        upload_url = str(rel_data.get("upload_url", "")).split("{")[0]

        with open(mp4_path, "rb") as f:
            up_res = requests.post(
                f"{upload_url}?name={requests.utils.quote(file_name)}",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "video/mp4"},
                data=f,
                timeout=300,
            )

        asset_url = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/{tag_name}/{file_name}"
        if up_res.status_code in (200, 201):
            asset_url = str(up_res.json().get("browser_download_url") or asset_url)
        else:
            print(f"GitHub asset upload warning ({up_res.status_code}): {up_res.text[:400]}")

        if thumb_path and os.path.exists(thumb_path):
            thumb_name = os.path.basename(thumb_path)
            with open(thumb_path, "rb") as tf:
                requests.post(
                    f"{upload_url}?name={requests.utils.quote(thumb_name)}",
                    headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "image/jpeg"},
                    data=tf,
                    timeout=90,
                )

        return {
            "tag": tag_name,
            "release_id": release_id,
            "release_url": release_url,
            "asset_url": asset_url,
        }
    except Exception as exc:
        print(f"Release upload exception: {exc}")
        return {"tag": tag_name, "release_id": "", "release_url": "", "asset_url": ""}


def _compress_for_telegram_streaming(src_mp4: str, dst_mp4: str, max_mb: float = 48.0):
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-threads", "2", "-i", src_mp4,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-b:v", "1400k", "-maxrate", "1700k", "-bufsize", "2800k",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                dst_mp4,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.exists(dst_mp4) and (os.path.getsize(dst_mp4) / (1024 * 1024)) <= max_mb:
            return dst_mp4
    except Exception as exc:
        print(f"Telegram stream compression warning: {exc}")
    return src_mp4


def send_four_button_approval(
    topic_id: str,
    title: str,
    mp4_path: str,
    thumb_path: str,
    release_info: dict,
    duration_sec: float,
    work_dir: str = "",
):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    size_mb = os.path.getsize(mp4_path) / (1024 * 1024) if os.path.exists(mp4_path) else 0.0
    mins = int(duration_sec // 60)
    secs = int(duration_sec % 60)

    asset_url = release_info.get("asset_url", "")
    release_url = release_info.get("release_url", "")

    caption_lines = [
        "🎬 <b>নতুন ১৬:৯ লং ভিডিও তৈরি হয়েছে • HomeBDInfo</b>",
        "",
        f"📌 <b>Topic ID:</b> <code>{tg_escape(topic_id)}</code>",
        f"📝 <b>Title:</b> {tg_escape(title)}",
        f"⏱ <b>Duration:</b> {mins}m {secs}s | <b>Size:</b> {size_mb:.1f} MB",
    ]
    if asset_url:
        caption_lines.append(f"📦 <b>GitHub MP4:</b> <a href='{tg_escape(asset_url)}'>Download / Direct File</a>")
    if release_url:
        caption_lines.append(f"🏷 <b>Release:</b> <a href='{tg_escape(release_url)}'>Open GitHub Release</a>")
    caption_lines.append("")
    caption_lines.append("▶️ ডাউনলোড ছাড়াই টেলিগ্রাম প্লেয়ারে ভিডিওটি প্লে করে দেখুন এবং নিচের ৪টি বাটন থেকে একটি বেছে নিন:")
    caption = "\n".join(caption_lines)

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

    send_video_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    stream_candidate = mp4_path

    if os.path.exists(mp4_path) and size_mb > 49.0 and work_dir:
        compact_mp4 = os.path.join(work_dir, f"tg_stream_{topic_id}.mp4")
        stream_candidate = _compress_for_telegram_streaming(mp4_path, compact_mp4, max_mb=48.5)

    stream_size_mb = os.path.getsize(stream_candidate) / (1024 * 1024) if os.path.exists(stream_candidate) else 999.0

    if os.path.exists(stream_candidate) and stream_size_mb <= 49.5:
        files = {"video": open(stream_candidate, "rb")}
        if thumb_path and os.path.exists(thumb_path):
            files["thumbnail"] = open(thumb_path, "rb")
        try:
            res = requests.post(
                send_video_url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                    "width": str(VIDEO_WIDTH),
                    "height": str(VIDEO_HEIGHT),
                    "duration": str(int(round(duration_sec))),
                    "supports_streaming": "true",
                    "reply_markup": json.dumps(markup),
                },
                files=files,
                timeout=300,
            )
            if res.ok and res.json().get("ok"):
                print("✅ Sent streaming video player directly to Telegram.")
                return
            print(f"Telegram sendVideo file warning ({res.status_code}): {res.text[:400]}")
        except Exception as exc:
            print(f"Telegram sendVideo file upload error: {exc}")
        finally:
            for f_obj in files.values():
                try:
                    f_obj.close()
                except Exception:
                    pass

    if asset_url:
        try:
            res = requests.post(
                send_video_url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "video": asset_url,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                    "width": VIDEO_WIDTH,
                    "height": VIDEO_HEIGHT,
                    "duration": int(round(duration_sec)),
                    "supports_streaming": True,
                    "reply_markup": markup,
                },
                timeout=120,
            )
            if res.ok and res.json().get("ok"):
                print("✅ Sent streaming video via GitHub release URL to Telegram.")
                return
            print(f"Telegram sendVideo URL warning ({res.status_code}): {res.text[:400]}")
        except Exception as exc:
            print(f"Telegram sendVideo URL error: {exc}")

    if thumb_path and os.path.exists(thumb_path):
        try:
            with open(thumb_path, "rb") as ph:
                p_res = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps(markup),
                    },
                    files={"photo": ph},
                    timeout=90,
                )
                if p_res.ok and p_res.json().get("ok"):
                    print("✅ Sent preview photo + 4 approval buttons to Telegram.")
                    return
        except Exception as exc:
            print(f"Telegram sendPhoto fallback error: {exc}")

    send_telegram_message(caption, reply_markup=markup, disable_preview=False)


# ---------------------------------------------------------
# 3. GOOGLE SHEETS HELPERS
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
# 4. VOICEOVER & PROSODY ENGINE (DEMUXER CONCAT FIX)
# ---------------------------------------------------------
def _spell_domain_for_tts(domain_str: str) -> str:
    parts = [p.strip() for p in domain_str.strip().split(".") if p.strip()]
    if not parts:
        return "হোম বিডি ইনফো"
    spoken = []
    for part in parts:
        low = part.lower()
        if low in ("www", "http", "https"):
            continue
        elif low == "gov":
            spoken.append("গভ")
        elif low == "bd":
            spoken.append("বিডি")
        elif low == "com":
            spoken.append("কম")
        elif low == "org":
            spoken.append("ওআরজি")
        elif re.fullmatch(r"[a-z]{1,4}", low):
            spoken.append(" ".join(list(low)))
        else:
            spoken.append(part)
    return " ডট ".join(spoken) if spoken else "হোম বিডি ইনফো"


def strip_visual_stage_directions(text: str) -> str:
    """দৃশ্য নির্দেশনা বা ক্যামেরা/সিন নোট স্ক্রিন ও ভয়েস থেকে পরিষ্কারভাবে বাদ দেয়।"""
    t = str(text or "").strip()
    t = re.sub(r"\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}", " ", t)
    cleaned_lines = []
    for ln in t.splitlines():
        s = ln.strip()
        if not s:
            continue
        if re.match(
            r"^(?:দৃশ্য|সিন|ভিডিও\s*দৃশ্য|ক্যামেরা|শট|ভিজ্যুয়াল|গ্রাফিক|ব্যাকগ্রাউন্ড|scene|shot|visual|b-?roll|cut\s*to)\s*[:\-–—]?",
            s,
            flags=re.IGNORECASE,
        ):
            after = re.sub(
                r"^(?:দৃশ্য|সিন|ভিডিও\s*দৃশ্য|ক্যামেরা|শট|ভিজ্যুয়াল|গ্রাফিক|ব্যাকগ্রাউন্ড|scene|shot|visual|b-?roll|cut\s*to)\s*[:\-–—]?\s*\d*\s*[:\-–—]?\s*",
                "",
                s,
                flags=re.IGNORECASE,
            ).strip()
            if not after or len(after.split()) <= 5:
                continue
            s = after
        cleaned_lines.append(s)
    return " ".join(cleaned_lines).strip()


def clean_voiceover_script(text: str) -> str:
    t = strip_visual_stage_directions(text)
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(
        r"হোম\s*বিডি\s*ইনফো\s*(?:ডট\s*কম)?\s*\(?homebdinfo\.com\)?",
        "হোম বিডি ইনফো",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"https?://(?:www\.)?homebdinfo\.com\S*", "হোম বিডি ইনফো", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:www\.)?homebdinfo\.com", "হোম বিডি ইনফো", t, flags=re.IGNORECASE)

    t = re.sub(
        r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]+",
        lambda m: _spell_domain_for_tts(re.sub(r"^https?://", "", m.group(0)).split("/")[0]),
        t,
    )
    t = re.sub(
        r" (?:[a-z0-9-]+\.)+(?:gov\.bd|com\.bd|org\.bd|edu\.bd|bd|com|org|net|info) ",
        lambda m: _spell_domain_for_tts(m.group(0)),
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"[🌀-🫿☀-➿︀-️‍*_~`#|\^=+<>/]", " ", t)
    t = re.sub(r"\s*[—–-]\s*", ", ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or "হোম বিডি ইনফো এর বিশেষ তথ্যবহুল আলোচনায় আপনাকে স্বাগতম।"


def split_clauses_for_tts(script: str) -> List[Dict]:
    raw_sentences = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", script) if p.strip()]
    if not raw_sentences:
        raw_sentences = [script]

    clauses = []
    total_s = len(raw_sentences)
    for s_idx, sent in enumerate(raw_sentences):
        sub_parts = [sp.strip() for sp in re.split(r"(?<=[,;:])\s+", sent) if sp.strip()]
        if not sub_parts:
            sub_parts = [sent]

        merged = []
        for sp in sub_parts:
            if merged and (len(sp) < 16 or len(merged[-1]) < 20):
                merged[-1] = f"{merged[-1]} {sp}".strip()
            else:
                merged.append(sp)

        for c_idx, clause_txt in enumerate(merged):
            clauses.append(
                {
                    "text": clause_txt,
                    "sent_idx": s_idx,
                    "total_sents": total_s,
                    "is_hook": (s_idx == 0 and c_idx == 0),
                    "is_sentence_end": (c_idx == len(merged) - 1),
                }
            )
    return clauses


def get_tv_presenter_prosody(clause_info: Dict) -> Tuple[str, str, float]:
    txt = clause_info["text"]
    s_idx = clause_info["sent_idx"]
    total_s = clause_info["total_sents"]
    is_hook = clause_info["is_hook"]
    is_end = clause_info["is_sentence_end"]

    pause = 0.34 if is_end else 0.14
    if is_hook:
        return "+4%", "-1Hz", 0.30
    if "?" in txt or any(w in txt for w in ["কীভাবে", "কেন", "কী", "কত", "কখন", "আদৌ"]):
        return "+2%", "+1Hz", pause + 0.03
    if any(w in txt for w in ["সতর্ক", "জরুরি", "অবশ্যই", "মনে রাখবেন", "ভুল", "জরিমানা", "আইন", "নিষেধ", "বাতিল"]):
        return "-1%", "-2Hz", pause + 0.04
    if s_idx >= max(1, total_s - 1) and is_end:
        return "-1%", "-2Hz", 0.38

    pat = (s_idx + (0 if is_end else 1)) % 3
    if pat == 0:
        return "+1%", "-1Hz", pause
    elif pat == 1:
        return "+0%", "-2Hz", pause
    else:
        return "+2%", "-1Hz", pause


async def _synthesize_phrase(text: str, out_path: str, rate: str, pitch: str, voice: str = VOICE_NAME):
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await comm.save(out_path)


def split_kinetic_chunks(sentence: str, max_chars: int = 52) -> List[str]:
    words = sentence.split()
    chunks, cur, cur_len = [], [], 0
    for w in words:
        if cur and cur_len + len(w) + 1 > max_chars:
            chunks.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += len(w) + (1 if cur_len > 0 else 0)
    if cur:
        chunks.append(" ".join(cur))
    if len(chunks) >= 2 and len(chunks[-1]) <= 6:
        chunks[-2] = f"{chunks[-2]} {chunks[-1]}".strip()
        chunks.pop()
    return chunks


def _ensure_valid_pcm_wav(src_wav: str, dst_wav: str, min_dur: float = 0.15):
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-threads", "2", "-i", src_wav,
                "-af", f"apad=pad_dur={min_dur:.2f}",
                "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", dst_wav,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        subprocess.run(
            [
                "ffmpeg", "-y", "-threads", "2",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{min_dur:.2f}",
                "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", dst_wav,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _concat_wav_files_via_list(wav_files: List[str], out_wav: str, work_dir: str):
    valid_files = [wf for wf in wav_files if wf and os.path.exists(wf) and os.path.getsize(wf) > 44]
    if not valid_files:
        _ensure_valid_pcm_wav("", out_wav, min_dur=1.0)
        return

    normalized_files = []
    for i, vf in enumerate(valid_files):
        norm_f = os.path.join(work_dir, f"norm_seg_{i}_{int(time.time()*1000)%100000}.wav")
        _ensure_valid_pcm_wav(vf, norm_f, min_dur=0.15)
        normalized_files.append(norm_f)

    list_txt = os.path.join(work_dir, f"concat_list_{int(time.time()*1000)%100000}.txt")
    with open(list_txt, "w", encoding="utf-8") as lf:
        for nf in normalized_files:
            abs_nf = os.path.abspath(nf).replace("\\", "/")
            lf.write(f"file '{abs_nf}'\n")

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2",
            "-f", "concat", "-safe", "0", "-i", list_txt,
            "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", out_wav,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for tmp_f in normalized_files + [list_txt]:
        if tmp_f and os.path.exists(tmp_f):
            try:
                os.remove(tmp_f)
            except Exception:
                pass


def generate_natural_voiceover(script: str, work_dir: str) -> Tuple[str, List[Dict]]:
    cleaned = clean_voiceover_script(script)
    clauses = split_clauses_for_tts(cleaned)

    segment_files = []
    kinetic_segments = []
    current_time = 0.0

    for idx, c_info in enumerate(clauses):
        clause_text = c_info["text"]
        rate, pitch, end_pause = get_tv_presenter_prosody(c_info)

        raw_mp3 = os.path.join(work_dir, f"tts_{idx}.mp3")
        raw_wav = os.path.join(work_dir, f"tts_raw_{idx}.wav")
        wav_seg = os.path.join(work_dir, f"tts_{idx}.wav")

        tts_ok = False
        try:
            asyncio.run(_synthesize_phrase(clause_text, raw_mp3, rate, pitch, voice=VOICE_NAME))
            if os.path.exists(raw_mp3) and os.path.getsize(raw_mp3) > 100:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-threads", "2", "-i", raw_mp3,
                        "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", raw_wav,
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _ensure_valid_pcm_wav(raw_wav, wav_seg, min_dur=0.20)
                tts_ok = os.path.exists(wav_seg) and os.path.getsize(wav_seg) > 44
        except Exception as exc:
            print(f"Warning: TTS segment {idx} fallback to silence: {exc}")

        for tmp_f in (raw_mp3, raw_wav):
            if os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except Exception:
                    pass

        if not tts_ok:
            _ensure_valid_pcm_wav("", wav_seg, min_dur=0.80)

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", wav_seg,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        try:
            dur = max(0.20, float(probe.stdout.strip()))
        except Exception:
            dur = 0.50

        segment_files.append(wav_seg)

        clause_start = current_time
        current_time += dur

        chunks = split_kinetic_chunks(clause_text, max_chars=52)
        total_chars = sum(len(c) for c in chunks) or 1
        t_cursor = clause_start
        for c_i, ch in enumerate(chunks):
            ch_dur = dur * (len(ch) / total_chars)
            kinetic_segments.append(
                {
                    "text": ch,
                    "start": t_cursor,
                    "end": t_cursor + ch_dur + (end_pause if c_i == len(chunks) - 1 else 0.0),
                    "is_hook": (c_info["is_hook"] and c_i == 0),
                }
            )
            t_cursor += ch_dur

        if end_pause > 0:
            sil_wav = os.path.join(work_dir, f"sil_{idx}.wav")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-threads", "2", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{end_pause:.2f}",
                    "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", sil_wav,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.path.exists(sil_wav) and os.path.getsize(sil_wav) > 44:
                segment_files.append(sil_wav)
                current_time += end_pause

    if not segment_files:
        fallback_wav = os.path.join(work_dir, "fallback_voice.wav")
        _ensure_valid_pcm_wav("", fallback_wav, min_dur=3.0)
        segment_files.append(fallback_wav)

    raw_concat_wav = os.path.join(work_dir, "voiceover_raw.wav")
    final_voice_wav = os.path.join(work_dir, "voiceover_full.wav")

    _concat_wav_files_via_list(segment_files, raw_concat_wav, work_dir)

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-i", raw_concat_wav,
            "-af", (
                "highpass=f=85,"
                "equalizer=f=165:t=q:w=1.1:g=2.2,"
                "equalizer=f=3400:t=q:w=1.4:g=2.4,"
                "acompressor=threshold=-17dB:ratio=2.6:attack=6:release=110,"
                "loudnorm=I=-15.0:TP=-1.5:LRA=10"
            ),
            "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", final_voice_wav,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for cleanup_f in segment_files + [raw_concat_wav]:
        if cleanup_f and os.path.exists(cleanup_f):
            try:
                os.remove(cleanup_f)
            except Exception:
                pass

    return final_voice_wav, kinetic_segments


# ---------------------------------------------------------
# 5. TYPOGRAPHY, WATERMARK, HOOK, THUMBNAIL & OUTRO (1920x1080)
# ---------------------------------------------------------
def render_mixed_text_line(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
    fill=(255, 255, 255, 255),
    stroke_width: int = 0,
    stroke_fill=(0, 0, 0, 255),
):
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|•]+|[^A-Za-z0-9\./:\-_@|•]+", text)
    cursor_x = x
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|•]+$", tok))
        fnt = en_font if is_latin else bn_font
        draw.text((cursor_x, y), tok, font=fnt, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        bbox = draw.textbbox((0, 0), tok, font=fnt)
        cursor_x += bbox[2] - bbox[0]


def measure_mixed_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
) -> int:
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|•]+|[^A-Za-z0-9\./:\-_@|•]+", text)
    total_w = 0
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|•]+$", tok))
        fnt = en_font if is_latin else bn_font
        bbox = draw.textbbox((0, 0), tok, font=fnt)
        total_w += bbox[2] - bbox[0]
    return total_w


def wrap_text_by_pixel(
    draw: ImageDraw.ImageDraw,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
    max_width: int,
) -> List[str]:
    words = text.split()
    lines, cur = [], []
    for w in words:
        test = " ".join(cur + [w])
        if cur and measure_mixed_text_width(draw, test, bn_font, en_font) > max_width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def _build_cinematic_backdrop_16x9(seed_clip: Optional[str], work_dir: str) -> Image.Image:
    if seed_clip and os.path.exists(seed_clip):
        frame_png = os.path.join(work_dir, f"bg_seed_{int(time.time()*1000)}.png")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-threads", "2", "-i", seed_clip, "-ss", "00:00:01.0", "-vframes", "1", frame_png],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.path.exists(frame_png):
                raw = Image.open(frame_png).convert("RGB")
                bg = raw.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=14))
                os.remove(frame_png)
                return bg.convert("RGBA")
        except Exception:
            pass

    top_rgb = (6, 18, 34)
    bot_rgb = (14, 42, 68)
    bg = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (*top_rgb, 255))
    draw = ImageDraw.Draw(bg)
    for y in range(0, VIDEO_HEIGHT, 4):
        p = y / VIDEO_HEIGHT
        r = int(top_rgb[0] + (bot_rgb[0] - top_rgb[0]) * p)
        g = int(top_rgb[1] + (bot_rgb[1] - top_rgb[1]) * p)
        b = int(top_rgb[2] + (bot_rgb[2] - top_rgb[2]) * p)
        draw.rectangle([0, y, VIDEO_WIDTH, y + 4], fill=(r, g, b, 255))
    for gx in range(0, VIDEO_WIDTH, 120):
        draw.line([(gx, 0), (gx, VIDEO_HEIGHT)], fill=(255, 255, 255, 10), width=1)
    for gy in range(0, VIDEO_HEIGHT, 120):
        draw.line([(0, gy), (VIDEO_WIDTH, gy)], fill=(255, 255, 255, 10), width=1)
    return bg


def create_minimal_watermark_png(out_path: str):
    w, h = 290, 74
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [4, 5, w - 4, h - 5],
        radius=34,
        fill=(8, 22, 36, 220),
        outline=(250, 204, 21, 210),
        width=2,
    )
    text_x = 24
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 48
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            draw.ellipse([12, 11, 64, 63], fill=(255, 255, 255, 245))
            img.paste(logo, (14, (h - lh) // 2), logo)
            text_x = 76
        except Exception:
            pass
    bn_font = find_font(BENGALI_FONT_PATHS, 26)
    en_font = find_font(LATIN_FONT_PATHS, 26)
    render_mixed_text_line(draw, text_x, 20, "HomeBDInfo", bn_font, en_font, fill=(255, 255, 255, 252))
    img.save(out_path, "PNG")


def create_clean_subtitle_png(text: str, is_hook: bool, out_path: str):
    canvas_w = 1760
    canvas_h = 190
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    clean_sub = strip_visual_stage_directions(text)
    if not clean_sub:
        img.save(out_path, "PNG")
        return

    font_size = 42 if is_hook else 36
    if len(clean_sub) > 54:
        font_size = 32

    bn_font = find_font(BENGALI_FONT_PATHS, font_size)
    en_font = find_font(LATIN_FONT_PATHS, font_size - 2)
    lines = wrap_text_by_pixel(draw, clean_sub, bn_font, en_font, 1580)[:2]

    line_h = font_size + 14
    total_h = len(lines) * line_h
    start_y = (canvas_h - total_h) // 2
    max_line_w = max((measure_mixed_text_width(draw, ln, bn_font, en_font) for ln in lines), default=500)
    pad_x, pad_y = 40, 14

    box_x0 = max(24, (canvas_w - max_line_w) // 2 - pad_x)
    box_x1 = min(canvas_w - 24, (canvas_w + max_line_w) // 2 + pad_x)
    box_y0 = max(8, start_y - pad_y)
    box_y1 = min(canvas_h - 8, start_y + total_h + pad_y - 4)

    border_col = (250, 204, 21, 235) if is_hook else (255, 255, 255, 130)
    draw.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1],
        radius=22,
        fill=(7, 18, 30, 228),
        outline=border_col,
        width=3 if is_hook else 2,
    )

    for i, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_font, en_font)
        lx = (canvas_w - lw) // 2
        ly = start_y + i * line_h
        txt_fill = (250, 204, 21, 255) if (is_hook or i == 1) else (255, 255, 255, 255)
        render_mixed_text_line(
            draw, lx, ly, ln, bn_font, en_font,
            fill=txt_fill, stroke_width=3, stroke_fill=(0, 0, 0, 255)
        )

    img.save(out_path, "PNG")


def create_hook_banner_clip(
    title: str,
    hook_line: str,
    seed_clip: Optional[str],
    out_mp4: str,
    work_dir: str,
    duration: float = HOOK_DURATION,
):
    bg = _build_cinematic_backdrop_16x9(seed_clip, work_dir)
    scrim = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 17, 30, 175))
    base = Image.alpha_composite(bg, scrim)
    draw = ImageDraw.Draw(base)

    badge_w, badge_h = 460, 56
    bx0 = (VIDEO_WIDTH - badge_w) // 2
    by0 = 140
    draw.rounded_rectangle([bx0, by0, bx0 + badge_w, by0 + badge_h], radius=28, fill=(250, 204, 21, 255))
    bn_b = find_font(BENGALI_FONT_PATHS, 26)
    en_b = find_font(LATIN_FONT_PATHS, 25)
    b_txt = "স্পেশাল ফিচার • HOMEBDINFO"
    bw = measure_mixed_text_width(draw, b_txt, bn_b, en_b)
    render_mixed_text_line(draw, (VIDEO_WIDTH - bw) // 2, by0 + 12, b_txt, bn_b, en_b, fill=(7, 18, 30, 255))

    clean_title = re.sub(r"#\w+|https?://\S+", "", strip_visual_stage_directions(title)).strip()
    bn_t = find_font(BENGALI_FONT_PATHS, 56)
    en_t = find_font(LATIN_FONT_PATHS, 52)
    t_lines = wrap_text_by_pixel(draw, clean_title, bn_t, en_t, VIDEO_WIDTH - 320)[:2]

    clean_hook = re.sub(r"[•✔*#]+|https?://\S+", "", strip_visual_stage_directions(hook_line)).strip()
    bn_h = find_font(BENGALI_FONT_PATHS, 36)
    en_h = find_font(LATIN_FONT_PATHS, 34)
    h_lines = wrap_text_by_pixel(draw, clean_hook, bn_h, en_h, VIDEO_WIDTH - 340)[:2]

    card_w = 1520
    card_h = 60 + len(t_lines) * 76 + 34 + len(h_lines) * 50 + 54
    card_x0 = (VIDEO_WIDTH - card_w) // 2
    card_y0 = 230
    card_x1 = card_x0 + card_w
    card_y1 = card_y0 + card_h

    draw.rounded_rectangle(
        [card_x0, card_y0, card_x1, card_y1],
        radius=32,
        fill=(9, 24, 38, 244),
        outline=(250, 204, 21, 225),
        width=3,
    )

    cur_y = card_y0 + 46
    for i, ln in enumerate(t_lines):
        lw = measure_mixed_text_width(draw, ln, bn_t, en_t)
        col = (250, 204, 21, 255) if i == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, (VIDEO_WIDTH - lw) // 2, cur_y, ln, bn_t, en_t, fill=col)
        cur_y += 76

    draw.line([(card_x0 + 110, cur_y + 8), (card_x1 - 110, cur_y + 8)], fill=(250, 204, 21, 115), width=2)
    cur_y += 32

    for hl in h_lines:
        hw = measure_mixed_text_width(draw, hl, bn_h, en_h)
        render_mixed_text_line(draw, (VIDEO_WIDTH - hw) // 2, cur_y, hl, bn_h, en_h, fill=(226, 232, 240, 255))
        cur_y += 50

    hook_png = os.path.join(work_dir, "hook_card_16x9.png")
    base.convert("RGB").save(hook_png, "PNG")
    del bg, scrim, base, draw
    gc.collect()

    n_frames = max(1, int(round(duration * FPS)))
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-loop", "1", "-i", hook_png,
            "-vf", (
                f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
                f"zoompan=z='1+0.025*(on/{max(1, n_frames)})':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
                f":d={n_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS}"
            ),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-t", f"{duration:.2f}", out_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if os.path.exists(hook_png):
        os.remove(hook_png)
    gc.collect()


def create_thumbnail_jpg(
    title: str,
    visual_clips: List[str],
    out_jpg: str,
    work_dir: str,
):
    seed_clip = visual_clips[0] if visual_clips else None
    canvas = _build_cinematic_backdrop_16x9(seed_clip, work_dir)
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 18, 32, 165))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 130
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            draw.ellipse([76, 56, 76 + lw + 24, 56 + lh + 24], fill=(255, 255, 255, 248))
            canvas.paste(logo, (88, 68), logo)
        except Exception:
            pass

    pill_w, pill_h = 370, 52
    px0 = VIDEO_WIDTH - pill_w - 72
    py0 = 86
    draw.rounded_rectangle([px0, py0, px0 + pill_w, py0 + pill_h], radius=26, fill=(250, 204, 21, 255))
    bn_p = find_font(BENGALI_FONT_PATHS, 25)
    en_p = find_font(LATIN_FONT_PATHS, 24)
    p_txt = "HOMEBDINFO • গাইড"
    pw = measure_mixed_text_width(draw, p_txt, bn_p, en_p)
    render_mixed_text_line(draw, px0 + (pill_w - pw) // 2, py0 + 11, p_txt, bn_p, en_p, fill=(7, 18, 30, 255))

    clean_title = re.sub(r"#\w+|https?://\S+", "", strip_visual_stage_directions(title)).strip()
    bn_t = find_font(BENGALI_FONT_PATHS, 66)
    en_t = find_font(LATIN_FONT_PATHS, 60)
    lines = wrap_text_by_pixel(draw, clean_title, bn_t, en_t, VIDEO_WIDTH - 240)[:3]

    line_h = 92
    box_pad_y = 42
    box_h = len(lines) * line_h + box_pad_y * 2
    box_y0 = max(250, (VIDEO_HEIGHT - box_h) // 2)
    box_y1 = box_y0 + box_h

    draw.rounded_rectangle(
        [72, box_y0, VIDEO_WIDTH - 72, box_y1],
        radius=34,
        fill=(9, 25, 40, 238),
        outline=(250, 204, 21, 235),
        width=4,
    )

    for idx, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_t, en_t)
        lx = (VIDEO_WIDTH - lw) // 2
        ly = box_y0 + box_pad_y + idx * line_h
        col = (250, 204, 21, 255) if idx % 2 == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, lx, ly, ln, bn_t, en_t, fill=col, stroke_width=2, stroke_fill=(0, 0, 0, 255))

    canvas.convert("RGB").save(out_jpg, "JPEG", quality=93)
    del canvas, overlay
    gc.collect()


def _smooth_ease_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1.0 - ((1.0 - x) ** 3)


def build_animated_outro_clip(out_mp4: str, work_dir: str, duration: float = OUTRO_DURATION):
    frames_dir = os.path.join(work_dir, "outro_frames")
    os.makedirs(frames_dir, exist_ok=True)
    n_frames = max(1, int(round(duration * FPS)))

    logo_src = None
    if os.path.exists(LOGO_FILE):
        try:
            logo_src = Image.open(LOGO_FILE).convert("RGBA")
        except Exception:
            logo_src = None

    bn_tag = find_font(BENGALI_FONT_PATHS, 26)
    en_tag = find_font(LATIN_FONT_PATHS, 24)
    bn_h1 = find_font(BENGALI_FONT_PATHS, 52)
    en_h1 = find_font(LATIN_FONT_PATHS, 48)
    bn_sub = find_font(BENGALI_FONT_PATHS, 36)
    en_sub = find_font(LATIN_FONT_PATHS, 34)
    bn_brand = find_font(BENGALI_FONT_PATHS, 44)
    en_brand = find_font(LATIN_FONT_PATHS, 42)

    backdrop_base = _build_cinematic_backdrop_16x9(None, work_dir)
    bdraw = ImageDraw.Draw(backdrop_base)
    bdraw.rectangle([0, 0, VIDEO_WIDTH, 8], fill=(250, 204, 21, 235))
    bdraw.rectangle([0, VIDEO_HEIGHT - 8, VIDEO_WIDTH, VIDEO_HEIGHT], fill=(250, 204, 21, 235))

    for f_idx in range(n_frames):
        t = f_idx / FPS
        ease_h1 = _smooth_ease_out(min(1.0, t / 0.45))
        ease_sub = _smooth_ease_out(max(0.0, min(1.0, (t - 0.25) / 0.45)))
        ease_dom = _smooth_ease_out(max(0.0, min(1.0, (t - 0.45) / 0.45)))

        img = backdrop_base.copy()
        draw = ImageDraw.Draw(img)

        card_x0, card_y0 = 180, 170
        card_x1, card_y1 = VIDEO_WIDTH - 180, VIDEO_HEIGHT - 160
        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=36,
            fill=(10, 27, 44, 242),
            outline=(250, 204, 21, 180),
            width=3,
        )

        pill_w, pill_h = 420, 50
        px0 = (VIDEO_WIDTH - pill_w) // 2
        py0 = card_y0 + 38
        draw.rounded_rectangle([px0, py0, px0 + pill_w, py0 + pill_h], radius=25, fill=(17, 47, 74, 240), outline=(250, 204, 21, 200), width=2)
        tag_txt = "HOMEBDINFO • ডিজিটাল ডেস্ক"
        tw = measure_mixed_text_width(draw, tag_txt, bn_tag, en_tag)
        render_mixed_text_line(draw, (VIDEO_WIDTH - tw) // 2, py0 + 10, tag_txt, bn_tag, en_tag, fill=(250, 204, 21, 255))

        ring_cx, ring_cy = VIDEO_WIDTH // 2, card_y0 + 185
        draw.ellipse([ring_cx - 76, ring_cy - 76, ring_cx + 76, ring_cy + 76], outline=(250, 204, 21, 225), width=3)
        draw.ellipse([ring_cx - 64, ring_cy - 64, ring_cx + 64, ring_cy + 64], fill=(255, 255, 255, 252))
        if logo_src is not None:
            lw = 96
            lh = int(logo_src.height * (lw / logo_src.width))
            rlogo = logo_src.resize((lw, lh), Image.Resampling.LANCZOS)
            img.paste(rlogo, (ring_cx - lw // 2, ring_cy - lh // 2), rlogo)

        h1 = "নিয়মিত সঠিক ও নির্ভুল তথ্য পেতে"
        w1 = measure_mixed_text_width(draw, h1, bn_h1, en_h1)
        h1_y = int(ring_cy + 98 + (1.0 - ease_h1) * 16)
        render_mixed_text_line(draw, (VIDEO_WIDTH - w1) // 2, h1_y, h1, bn_h1, en_h1, fill=(255, 255, 255, 255))

        h2 = "লাইক, কমেন্ট, শেয়ার ও সাবস্ক্রাইব করুন"
        w2 = measure_mixed_text_width(draw, h2, bn_sub, en_sub)
        h2_y = int(h1_y + 74 + (1.0 - ease_sub) * 14)
        render_mixed_text_line(draw, (VIDEO_WIDTH - w2) // 2, h2_y, h2, bn_sub, en_sub, fill=(250, 204, 21, 255))

        bw, bh = 580, 82
        bscale = 0.96 + 0.04 * ease_dom
        cbw, cbh = int(bw * bscale), int(bh * bscale)
        bx0 = (VIDEO_WIDTH - cbw) // 2
        by0 = int(h2_y + 76 + (1.0 - ease_dom) * 14 + (bh - cbh) // 2)
        draw.rounded_rectangle([bx0, by0, bx0 + cbw, by0 + cbh], radius=22, fill=(250, 204, 21, 255))

        b_label = "HomeBDInfo"
        blw = measure_mixed_text_width(draw, b_label, bn_brand, en_brand)
        render_mixed_text_line(draw, (VIDEO_WIDTH - blw) // 2, by0 + 16, b_label, bn_brand, en_brand, fill=(7, 18, 30, 255))

        frame_path = os.path.join(frames_dir, f"outro_{f_idx:04d}.png")
        img.convert("RGB").save(frame_path, "PNG")
        del img, draw

    if logo_src is not None:
        logo_src.close()
    backdrop_base.close()

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2",
            "-framerate", str(FPS),
            "-i", os.path.join(frames_dir, "outro_%04d.png"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-t", f"{duration:.2f}", out_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for fn in os.listdir(frames_dir):
        fp = os.path.join(frames_dir, fn)
        if os.path.exists(fp):
            os.remove(fp)
    if os.path.exists(frames_dir):
        os.rmdir(frames_dir)
    gc.collect()


# ---------------------------------------------------------
# 6. CINEMATIC MOTION GRAPHICS & STOCK FOOTAGE (1920x1080)
# ---------------------------------------------------------
def _encode_lightweight_motion_clip_16x9(image_path: str, out_mp4: str, duration: int = 10):
    n_frames = max(1, int(round(duration * FPS)))
    zoom_expr = f"1+0.05*(on/{max(1, n_frames)})"
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-loop", "1", "-i", image_path,
            "-vf", (
                f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
                f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
                f":d={n_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS}"
            ),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-t", str(duration), out_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def create_dynamic_topic_graphic_clip(
    headline: str,
    bullet_points: List[str],
    work_dir: str,
    clip_idx: int,
    duration: int = 10,
) -> Optional[str]:
    try:
        card_png = os.path.join(work_dir, f"motion_card_16x9_{clip_idx}.png")
        clip_mp4 = os.path.join(work_dir, f"motion_card_16x9_{clip_idx}.mp4")

        img = _build_cinematic_backdrop_16x9(None, work_dir)
        draw = ImageDraw.Draw(img)

        badge_w, badge_h = 440, 54
        bx0 = (VIDEO_WIDTH - badge_w) // 2
        by0 = 120
        draw.rounded_rectangle([bx0, by0, bx0 + badge_w, by0 + badge_h], radius=27, fill=(250, 204, 21, 255))
        bn_b = find_font(BENGALI_FONT_PATHS, 26)
        en_b = find_font(LATIN_FONT_PATHS, 24)
        b_txt = f"গুরুত্বপূর্ণ পয়েন্ট • পর্ব ০{clip_idx + 1}"
        bw = measure_mixed_text_width(draw, b_txt, bn_b, en_b)
        render_mixed_text_line(draw, (VIDEO_WIDTH - bw) // 2, by0 + 11, b_txt, bn_b, en_b, fill=(7, 18, 30, 255))

        card_x0, card_y0 = 150, 205
        card_x1, card_y1 = VIDEO_WIDTH - 150, VIDEO_HEIGHT - 195
        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=34,
            fill=(10, 27, 44, 242),
            outline=(250, 204, 21, 210),
            width=3,
        )

        clean_head = re.sub(r"#\w+|https?://\S+", "", strip_visual_stage_directions(headline)).strip()
        bn_h = find_font(BENGALI_FONT_PATHS, 46)
        en_h = find_font(LATIN_FONT_PATHS, 42)
        h_lines = wrap_text_by_pixel(draw, clean_head, bn_h, en_h, card_x1 - card_x0 - 120)[:2]

        cur_y = card_y0 + 38
        for hl in h_lines:
            hw = measure_mixed_text_width(draw, hl, bn_h, en_h)
            render_mixed_text_line(draw, (VIDEO_WIDTH - hw) // 2, cur_y, hl, bn_h, en_h, fill=(250, 204, 21, 255))
            cur_y += 62

        draw.line([(card_x0 + 80, cur_y + 10), (card_x1 - 80, cur_y + 10)], fill=(255, 255, 255, 65), width=2)
        cur_y += 34

        bn_pt = find_font(BENGALI_FONT_PATHS, 34)
        en_pt = find_font(LATIN_FONT_PATHS, 32)
        valid_pts = [
            re.sub(r"https?://\S+", "", strip_visual_stage_directions(p)).strip()
            for p in bullet_points
            if len(strip_visual_stage_directions(p).strip()) > 6
        ][:3]
        for pt in valid_pts:
            if cur_y > card_y1 - 85:
                break
            pt_lines = wrap_text_by_pixel(draw, f"▸ {pt}", bn_pt, en_pt, card_x1 - card_x0 - 130)[:2]
            for pl in pt_lines:
                render_mixed_text_line(draw, card_x0 + 65, cur_y, pl, bn_pt, en_pt, fill=(241, 245, 249, 255))
                cur_y += 46
            cur_y += 12

        img.convert("RGB").save(card_png, "PNG")
        del img, draw
        gc.collect()

        _encode_lightweight_motion_clip_16x9(card_png, clip_mp4, duration=duration)
        if os.path.exists(card_png):
            os.remove(card_png)
        gc.collect()
        return clip_mp4
    except Exception:
        return None


def download_relevant_stock_clips(keywords: str, work_dir: str, max_clips: int = 5) -> List[str]:
    if max_clips <= 0:
        return []
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return []
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        kw_list = ["office work", "digital technology", "document review", "modern city"]

    banned_tags = {
        "animal", "monkey", "baboon", "ape", "fire", "flame", "wildfire",
        "dog", "cat", "bird", "insect", "spider", "snake", "skull", "blood"
    }
    downloaded = []
    seen_ids = set()

    for kw in kw_list[:6]:
        if len(downloaded) >= max_clips:
            break
        try:
            url = (
                f"https://pixabay.com/api/videos/?key={api_key}"
                f"&q={requests.utils.quote(kw)}&order=relevant&per_page=6&safesearch=true"
            )
            resp = requests.get(url, timeout=20).json()
            hits = resp.get("hits", [])
            for hit in hits:
                if len(downloaded) >= max_clips:
                    break
                vid_id = hit.get("id")
                if vid_id in seen_ids:
                    continue
                tags_str = str(hit.get("tags", "")).lower()
                if any(b in tags_str for b in banned_tags):
                    continue
                videos = hit.get("videos", {})
                vid_info = videos.get("large") or videos.get("medium") or videos.get("small")
                if vid_info and vid_info.get("url"):
                    raw_p = os.path.join(work_dir, f"raw_stock_16x9_{len(downloaded)}.mp4")
                    norm_p = os.path.join(work_dir, f"norm_stock_16x9_{len(downloaded)}.mp4")
                    v_data = requests.get(vid_info["url"], timeout=45).content
                    with open(raw_p, "wb") as f:
                        f.write(v_data)
                    del v_data

                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-threads", "2", "-i", raw_p,
                            "-vf", (
                                f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
                                f"eq=brightness=-0.08:contrast=1.08:saturation=1.05"
                            ),
                            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS),
                            "-t", "10", norm_p,
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if os.path.exists(raw_p):
                        os.remove(raw_p)
                    downloaded.append(norm_p)
                    seen_ids.add(vid_id)
                    gc.collect()
        except Exception:
            continue
    return downloaded


# ---------------------------------------------------------
# 7. SUBTITLE OVERLAY & FINAL 16:9 LONG VIDEO ASSEMBLY
# ---------------------------------------------------------
def _escape_concat_path(path_str: str) -> str:
    abs_p = os.path.abspath(path_str).replace("\\", "/")
    return abs_p.replace("'", "'\\''")


def _create_blank_subtitle_mov(out_mov: str, duration: float):
    safe_dur = max(0.5, float(duration))
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2",
            "-f", "lavfi",
            "-i", f"color=c=black@0.0:s=1760x190:r=15:d={safe_dur:.3f}",
            "-c:v", "png", "-pix_fmt", "rgba", out_mov,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build_subtitle_overlay_track(kinetic_segments: List[Dict], voice_dur: float, work_dir: str) -> str:
    sub_dir = os.path.join(work_dir, "sub_frames_16x9")
    os.makedirs(sub_dir, exist_ok=True)
    safe_voice_dur = max(0.5, float(voice_dur))

    blank_png = os.path.join(sub_dir, "blank.png")
    Image.new("RGBA", (1760, 190), (0, 0, 0, 0)).save(blank_png, "PNG")

    seg_pngs = []
    for idx, seg in enumerate(kinetic_segments):
        st = max(0.0, float(seg.get("start", 0.0)))
        en = min(safe_voice_dur, float(seg.get("end", st)))
        if st >= safe_voice_dur or en <= st + 0.02:
            continue
        txt = strip_visual_stage_directions(str(seg.get("text", "")))
        if not txt:
            continue
        kp = os.path.join(sub_dir, f"sub_{idx}.png")
        try:
            create_clean_subtitle_png(txt, bool(seg.get("is_hook", False)), kp)
            if os.path.exists(kp) and os.path.getsize(kp) > 0:
                seg_pngs.append((kp, st, en))
        except Exception as exc:
            print(f"Warning: subtitle frame {idx} skipped: {exc}")

    entries = []
    cursor = 0.0
    for kp, st, en in sorted(seg_pngs, key=lambda x: x[1]):
        if st < cursor:
            st = cursor
        if en <= st + 0.02:
            continue
        if st - cursor >= 0.02:
            entries.append((blank_png, round(st - cursor, 3)))
        entries.append((kp, round(en - st, 3)))
        cursor = en

    if safe_voice_dur - cursor >= 0.02:
        entries.append((blank_png, round(safe_voice_dur - cursor, 3)))

    valid_entries = [(p, d) for p, d in entries if p and os.path.exists(p) and d >= 0.02]
    if not valid_entries:
        valid_entries = [(blank_png, round(safe_voice_dur, 3))]

    concat_file = os.path.join(sub_dir, "sub_concat.txt")
    with open(concat_file, "w", encoding="utf-8") as f:
        for img_path, dur in valid_entries:
            f.write(f"file '{_escape_concat_path(img_path)}'\n")
            f.write(f"duration {dur:.3f}\n")
        f.write(f"file '{_escape_concat_path(valid_entries[-1][0])}'\n")

    sub_mov = os.path.join(work_dir, "kinetic_track_16x9.mov")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-threads", "2",
                "-f", "concat", "-safe", "0", "-i", concat_file,
                "-vf", "fps=15,format=rgba",
                "-c:v", "png", "-pix_fmt", "rgba",
                "-t", f"{safe_voice_dur:.3f}", sub_mov,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Warning: concat subtitle track fallback ({exc.returncode}), generating clean overlay...")
        _create_blank_subtitle_mov(sub_mov, safe_voice_dur)

    gc.collect()
    return sub_mov


def build_final_long_video(
    title: str,
    voice_wav: str,
    kinetic_segments: List[Dict],
    visual_clips: List[str],
    thumbnail_jpg: str,
    output_mp4: str,
    work_dir: str,
) -> float:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", voice_wav,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    voice_dur = max(2.0, float(probe.stdout.strip()))
    total_duration = voice_dur + OUTRO_DURATION

    seed_clip = visual_clips[0] if visual_clips else None
    hook_line = kinetic_segments[0]["text"] if kinetic_segments else title
    hook_mp4 = os.path.join(work_dir, "hook_banner_16x9.mp4")
    create_hook_banner_clip(title, hook_line, seed_clip, hook_mp4, work_dir, duration=min(HOOK_DURATION, voice_dur))

    watermark_png = os.path.join(work_dir, "watermark_pill_16x9.png")
    outro_mp4 = os.path.join(work_dir, "outro_animated_16x9.mp4")
    create_minimal_watermark_png(watermark_png)
    build_animated_outro_clip(outro_mp4, work_dir, duration=OUTRO_DURATION)
    sub_mov = build_subtitle_overlay_track(kinetic_segments, voice_dur, work_dir)

    base_bg_png = os.path.join(work_dir, "base_backdrop_16x9.png")
    _build_cinematic_backdrop_16x9(seed_clip, work_dir).convert("RGB").save(base_bg_png, "PNG")

    base_mp4 = os.path.join(work_dir, "base_layer_16x9.mp4")
    base_inputs = ["-loop", "1", "-t", f"{voice_dur:.2f}", "-i", base_bg_png]
    next_i = 1

    hook_idx = None
    if os.path.exists(hook_mp4):
        base_inputs.extend(["-i", hook_mp4])
        hook_idx = next_i
        next_i += 1

    clip_indices = []
    for cp in visual_clips:
        if os.path.exists(cp):
            base_inputs.extend(["-stream_loop", "-1", "-t", f"{voice_dur:.2f}", "-i", cp])
            clip_indices.append(next_i)
            next_i += 1

    base_filters = [f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},format=yuv420p[bg0]"]
    last_v = "bg0"

    if clip_indices:
        slot_len = 9.0
        t_cur = min(HOOK_DURATION, voice_dur) if hook_idx is not None else 0.0
        slot_idx = 0
        while t_cur < voice_dur - 0.3:
            t_end = min(voice_dur, t_cur + slot_len)
            c_in = clip_indices[slot_idx % len(clip_indices)]
            out_lbl = f"v_slot_{slot_idx}"
            base_filters.append(
                f"[{last_v}][{c_in}:v]overlay=0:0:enable='between(t,{t_cur:.2f},{t_end:.2f})'[{out_lbl}]"
            )
            last_v = out_lbl
            t_cur = t_end
            slot_idx += 1

    if hook_idx is not None:
        hook_end = min(HOOK_DURATION, voice_dur)
        base_filters.append(
            f"[{last_v}][{hook_idx}:v]overlay=0:0:enable='between(t,0,{hook_end:.2f})'[v_hook]"
        )
        last_v = "v_hook"

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", *base_inputs,
            "-filter_complex", ";".join(base_filters),
            "-map", f"[{last_v}]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-t", f"{voice_dur:.2f}", base_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gc.collect()

    final_inputs = [
        "-i", base_mp4,
        "-i", voice_wav,
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", watermark_png,
        "-i", sub_mov,
        "-i", outro_mp4,
        "-loop", "1", "-t", f"{COVER_DURATION:.2f}", "-i", thumbnail_jpg,
    ]
    bgm_idx = None
    if os.path.exists(BGM_FILE):
        final_inputs.extend(["-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", BGM_FILE])
        bgm_idx = 6

    f_parts = [
        f"[0:v]tpad=stop_mode=clone:stop_duration={OUTRO_DURATION:.2f}[v_base]",
        f"[v_base][2:v]overlay=36:36:enable='between(t,0,{voice_dur:.2f})'[v_wm]",
        f"[v_wm][3:v]overlay=(W-w)/2:850:enable='between(t,0,{voice_dur:.2f})'[v_sub]",
        f"[v_sub]drawbox=x=0:y=0:w='iw*(t/{total_duration:.2f})':h=7:color=#FACC15@0.95:t=fill[v_prog]",
        f"[v_prog][4:v]overlay=0:0:enable='between(t,{voice_dur:.2f},{total_duration:.2f})'[v_outro]",
        f"[5:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}[thumb_cover]",
        f"[v_outro][thumb_cover]overlay=0:0:enable='between(t,0,{COVER_DURATION:.2f})'[v_final]",
    ]

    if bgm_idx is not None:
        outro_fade_out_start = max(0.0, OUTRO_DURATION - 0.9)
        f_parts.extend([
            f"[{bgm_idx}:a]atrim=0:{voice_dur:.2f},highpass=f=120,lowpass=f=2600,volume={BGM_VOICE_VOLUME},afade=t=in:st=0:d=0.6[bgm_body]",
            f"[{bgm_idx}:a]atrim={voice_dur:.2f}:{total_duration:.2f},asetpts=PTS-STARTPTS,highpass=f=120,lowpass=f=2800,volume={BGM_OUTRO_VOLUME},afade=t=out:st={outro_fade_out_start:.2f}:d=0.9[bgm_outro]",
            f"[bgm_body][bgm_outro]concat=n=2:v=0:a=1[bgm_clean]",
            f"[1:a]apad=pad_dur={OUTRO_DURATION:.2f}[v_pad]",
            f"[v_pad][bgm_clean]amix=inputs=2:duration=first:weights='1.0 1.0':dropout_transition=2,volume=1.05[a_final]",
        ])
    else:
        f_parts.append(f"[1:a]apad=pad_dur={OUTRO_DURATION:.2f}[a_final]")

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", *final_inputs,
            "-filter_complex", ";".join(f_parts),
            "-map", "[v_final]", "-map", "[a_final]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-b:v", "1800k", "-maxrate", "2200k", "-bufsize", "3600k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", "-r", str(FPS),
            "-t", f"{total_duration:.2f}", output_mp4,
        ],
        check=True,
    )
    gc.collect()
    return total_duration


# ---------------------------------------------------------
# 8. SHEET QUEUE & PUBLISHING ORCHESTRATOR
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
                        f"⏰ <b>{tg_escape(platform_label)} Scheduled Publish Triggered</b>\n"
                        f"• <b>Topic ID:</b> <code>{tg_escape(row[0])}</code>\n"
                        f"• <b>Title:</b> {tg_escape(row[1] if len(row) > 1 else '')}"
                    )


def sync_publish_queues_from_production(spreadsheet, now_dt: datetime):
    prod_ws = get_or_create_worksheet(
        spreadsheet,
        "Video Production",
        [
            "Topic ID", "Video Title", "Production Status", "Video Link",
            "Duration", "QC Status", "Approval Status", "Notes",
            "Updated Date", "Release ID", "Video URL", "Cleanup Status",
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
    print("Starting 16:9 HomeBDInfo Long Video Production Pipeline...")
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
            "Topic ID", "Video Title", "Hook", "Voiceover Script", "Scene Plan",
            "GitHub Pipeline Note", "YouTube Title", "YouTube Description",
            "Facebook Caption", "Hashtags", "CTA", "Word Count", "Status", "Created Date",
        ],
    )
    prod_ws = get_or_create_worksheet(
        spreadsheet,
        "Video Production",
        [
            "Topic ID", "Video Title", "Production Status", "Video Link",
            "Duration", "QC Status", "Approval Status", "Notes",
            "Updated Date", "Release ID", "Video URL", "Cleanup Status",
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
    for _, s_row, is_regen in candidates[:MAX_VIDEOS]:
        tid = str(s_row[0]).strip()
        title = str(s_row[smap.get("Video Title", 1)] if len(s_row) > 1 else tid).strip()
        hook_txt = str(s_row[smap.get("Hook", 2)] if len(s_row) > 2 else "").strip()
        voice_script = str(s_row[smap.get("Voiceover Script", 3)] if len(s_row) > 3 else "").strip()
        keywords = str(s_row[smap.get("Keywords", 9)] if len(s_row) > 9 else "").strip()

        clean_title_for_vo = strip_visual_stage_directions(title) or title
        if not voice_script:
            voice_script = f"{clean_title_for_vo} বিষয়ে বিস্তারিত তথ্য ও ব্যবহারিক দিকনির্দেশনা নিচে উপস্থাপন করা হলো।"

        with tempfile.TemporaryDirectory(dir=WORK_DIR_ROOT) as work_dir:
            thumb_jpg = os.path.join(work_dir, f"thumb_{tid}_16x9.jpg")
            out_mp4 = os.path.join(OUTPUT_DIR, f"{tid}_16x9.mp4")

            print(f"1. Generating natural voiceover & prosody for {tid}...")
            voice_wav, kinetic_segments = generate_natural_voiceover(voice_script, work_dir)
            clean_hook_vo = strip_visual_stage_directions(hook_txt)
            if clean_hook_vo:
                kinetic_segments.insert(0, {"text": clean_hook_vo, "start": 0.0, "end": min(HOOK_DURATION, 4.5), "is_hook": True})

            print("2. Building cinematic 16:9 topic cards & relevant stock footage...")
            chunks_for_cards = [c["text"] for c in split_clauses_for_tts(clean_voiceover_script(voice_script))]

            visual_clips = []
            for c_idx in range(3):
                pts = chunks_for_cards[c_idx * 2 : c_idx * 2 + 3] or [clean_voiceover_script(voice_script)[:120]]
                head_txt = clean_title_for_vo
                m_clip = create_dynamic_topic_graphic_clip(head_txt, pts, work_dir, c_idx, duration=10)
                if m_clip:
                    visual_clips.append(m_clip)

            stock_clips = download_relevant_stock_clips(keywords, work_dir, max_clips=5)
            combined_clips = []
            max_len = max(len(stock_clips), len(visual_clips))
            for i in range(max_len):
                if i < len(stock_clips):
                    combined_clips.append(stock_clips[i])
                if i < len(visual_clips):
                    combined_clips.append(visual_clips[i])

            print("3. Generating 16:9 HomeBDInfo thumbnail...")
            create_thumbnail_jpg(title, combined_clips, thumb_jpg, work_dir)

            print("4. Rendering full 16:9 long video...")
            duration_sec = build_final_long_video(
                title=title,
                voice_wav=voice_wav,
                kinetic_segments=kinetic_segments,
                visual_clips=combined_clips,
                thumbnail_jpg=thumb_jpg,
                output_mp4=out_mp4,
                work_dir=work_dir,
            )

            print("5. Uploading release & sending Telegram streaming player + 4 buttons...")
            release_info = upload_release_asset(out_mp4, thumb_jpg, tid)
            send_four_button_approval(tid, title, out_mp4, thumb_jpg, release_info, duration_sec, work_dir=work_dir)

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
            "Regenerated 16:9 HomeBDInfo long video." if is_regen else "Generated 16:9 HomeBDInfo long video; awaiting approval.",
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

        generated_count += 1

    sync_publish_queues_from_production(spreadsheet, datetime.now())
    print(f"🎉 Completed 16:9 long video run. Generated: {generated_count}")


if __name__ == "__main__":
    main()
