import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import edge_tts
import gspread
import requests
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

LOGO_FILE = "logo.png"
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
OUTRO_DURATION = 5.0

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


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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


def get_gspread_client() -> gspread.Client:
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON secret is missing.")
    info = json.loads(raw_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _spell_domain_for_tts(domain_str: str) -> str:
    parts = [p.strip() for p in domain_str.strip().split(".") if p.strip()]
    if not parts:
        return domain_str
    spoken_parts = []
    for part in parts:
        low = part.lower()
        mapping = {
            "www": "ডাব্লিউ ডাব্লিউ ডাব্লিউ",
            "gov": "গভ",
            "bd": "বিডি",
            "com": "কম",
            "org": "ওআরজি",
            "net": "নেট",
            "edu": "ইডিইউ",
        }
        if low in mapping:
            spoken_parts.append(mapping[low])
        elif re.fullmatch(r"[a-z]{1,4}", low):
            spoken_parts.append(" ".join(list(low)))
        else:
            spoken_parts.append(part)
    return " ডট ".join(spoken_parts)


def clean_voiceover_script(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"https?://(?:www\.)?homebdinfo\.com\S*", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:www\.)?homebdinfo\.com", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)

    def _replace_url(match):
        raw_u = match.group(0)
        cleaned_u = re.sub(r"^https?://", "", raw_u, flags=re.IGNORECASE)
        host_and_path = cleaned_u.split("/", 1)
        host_spoken = _spell_domain_for_tts(host_and_path[0])
        if len(host_and_path) > 1 and host_and_path[1].strip():
            return f"{host_spoken} পোর্টাল"
        return host_spoken

    text = re.sub(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]+", _replace_url, text)
    text = re.sub(
        r"\b(?:[a-z0-9-]+\.)+(?:gov\.bd|com\.bd|org\.bd|edu\.bd|bd|com|org|net|info)\b",
        lambda m: _spell_domain_for_tts(m.group(0)),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*[—–-]\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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
) -> None:
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|•]+|[^A-Za-z0-9\./:\-_@|•]+", text)
    cursor_x = x
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|•]+$", tok))
        fnt = en_font if is_latin else bn_font
        draw.text((cursor_x, y), tok, font=fnt, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        bbox = draw.textbbox((0, 0), tok, font=fnt)
        cursor_x += bbox[2] - bbox[0]


def wrap_text_by_pixel(
    draw: ImageDraw.ImageDraw,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
    max_width: int,
) -> List[str]:
    words = text.strip().split()
    if not words:
        return []
    lines = []
    cur = []
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


def load_logo_rgba(size_px: int) -> Image.Image | None:
    if not os.path.exists(LOGO_FILE):
        return None
    try:
        logo = Image.open(LOGO_FILE).convert("RGBA")
        w = max(20, size_px)
        h = max(20, int(logo.height * (w / max(1, logo.width))))
        return logo.resize((w, h), Image.Resampling.LANCZOS)
    except Exception:
        return None


def render_slide_image_16x9(
    out_path: Path,
    title: str,
    chapter_label: str,
    body_text: str,
    slide_index: int,
    total_slides: int,
) -> None:
    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (8, 22, 38, 255))
    draw = ImageDraw.Draw(img)

    # Top & bottom accent bars
    draw.rectangle([0, 0, VIDEO_WIDTH, 10], fill=(250, 204, 21, 255))
    draw.rectangle([0, VIDEO_HEIGHT - 64, VIDEO_WIDTH, VIDEO_HEIGHT], fill=(15, 35, 56, 255))

    bn_badge = find_font(BENGALI_FONT_PATHS, 28)
    en_badge = find_font(LATIN_FONT_PATHS, 26)
    bn_title = find_font(BENGALI_FONT_PATHS, 46)
    en_title = find_font(LATIN_FONT_PATHS, 42)
    bn_body = find_font(BENGALI_FONT_PATHS, 38)
    en_body = find_font(LATIN_FONT_PATHS, 36)
    bn_foot = find_font(BENGALI_FONT_PATHS, 24)
    en_foot = find_font(LATIN_FONT_PATHS, 22)

    # Top-right root logo badge
    logo_img = load_logo_rgba(68)
    if logo_img is not None:
        lx = VIDEO_WIDTH - logo_img.width - 70
        ly = 28
        draw.ellipse(
            [lx - 8, ly - 8, lx + logo_img.width + 8, ly + logo_img.height + 8],
            fill=(255, 255, 255, 240),
            outline=(250, 204, 21, 220),
            width=2,
        )
        img.paste(logo_img, (lx, ly), logo_img)

    badge_text = (chapter_label.strip() or f"অধ্যায় {slide_index} / {total_slides}")[:42]
    draw.rounded_rectangle([70, 36, 560, 92], radius=14, fill=(16, 185, 129, 255))
    render_mixed_text_line(draw, 92, 46, badge_text, bn_badge, en_badge, fill=(8, 22, 38, 255))

    title_lines = wrap_text_by_pixel(draw, title, bn_title, en_title, VIDEO_WIDTH - 240)[:2]
    y = 122
    for line in title_lines:
        render_mixed_text_line(draw, 70, y, line, bn_title, en_title, fill=(248, 250, 252, 255))
        y += 58

    draw.line([(70, y + 8), (VIDEO_WIDTH - 70, y + 8)], fill=(51, 65, 85, 255), width=2)
    y += 32

    card_top = y
    card_bottom = VIDEO_HEIGHT - 96
    draw.rounded_rectangle(
        [70, card_top, VIDEO_WIDTH - 70, card_bottom],
        radius=24,
        fill=(18, 38, 62, 245),
        outline=(250, 204, 21, 120),
        width=2,
    )

    body_lines = wrap_text_by_pixel(draw, body_text, bn_body, en_body, VIDEO_WIDTH - 220)
    max_lines = max(1, (card_bottom - card_top - 72) // 54)
    display_lines = body_lines[:max_lines]
    if len(body_lines) > max_lines and display_lines:
        display_lines[-1] = display_lines[-1][: max(0, len(display_lines[-1]) - 3)] + "..."

    ty = card_top + 36
    for line in display_lines:
        render_mixed_text_line(draw, 110, ty, line, bn_body, en_body, fill=(226, 232, 240, 255))
        ty += 54

    footer_txt = f"homebdinfo.com  •  পর্ব {slide_index} / {total_slides}"
    render_mixed_text_line(draw, 70, VIDEO_HEIGHT - 46, footer_txt, bn_foot, en_foot, fill=(148, 163, 184, 255))

    img.convert("RGB").save(out_path, format="PNG")


def build_outro_16x9(out_mp4: Path, work_dir: Path, duration: float = OUTRO_DURATION) -> None:
    frames_dir = work_dir / "outro_16x9_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(round(duration * FPS)))

    logo_src = load_logo_rgba(190)
    bn_h1 = find_font(BENGALI_FONT_PATHS, 52)
    en_h1 = find_font(LATIN_FONT_PATHS, 48)
    bn_sub = find_font(BENGALI_FONT_PATHS, 34)
    en_sub = find_font(LATIN_FONT_PATHS, 34)

    for f_idx in range(n_frames):
        t = f_idx / FPS
        pulse = 1.0 + 0.03 * math.sin(2.0 * math.pi * 1.2 * t)
        img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 34, 48, 255))
        draw = ImageDraw.Draw(img)

        draw.rounded_rectangle(
            [180, 120, VIDEO_WIDTH - 180, VIDEO_HEIGHT - 120],
            radius=36,
            fill=(12, 54, 74, 240),
            outline=(250, 204, 21, 180),
            width=3,
        )

        cx, cy = VIDEO_WIDTH // 2, 340
        r_ring = int(115 * pulse)
        draw.ellipse([cx - r_ring, cy - r_ring, cx + r_ring, cy + r_ring], fill=(255, 255, 255, 245), outline=(250, 204, 21, 255), width=4)
        if logo_src is not None:
            lw = max(30, int(logo_src.width * 0.78 * pulse))
            lh = max(30, int(logo_src.height * 0.78 * pulse))
            l_res = logo_src.resize((lw, lh), Image.Resampling.LANCZOS)
            img.paste(l_res, (cx - lw // 2, cy - lh // 2), l_res)

        h1 = "তথ্যটি উপকারী মনে হলে লাইক, কমেন্ট ও শেয়ার করুন"
        w1 = measure_mixed_text_width(draw, h1, bn_h1, en_h1)
        render_mixed_text_line(draw, (VIDEO_WIDTH - w1) // 2, 520, h1, bn_h1, en_h1, fill=(250, 204, 21, 255))

        sub = "বিস্তারিত গাইড ও আপডেটের জন্য ভিজিট করুন: homebdinfo.com"
        w2 = measure_mixed_text_width(draw, sub, bn_sub, en_sub)
        render_mixed_text_line(draw, (VIDEO_WIDTH - w2) // 2, 620, sub, bn_sub, en_sub, fill=(241, 245, 249, 255))

        img.convert("RGB").save(frames_dir / f"f_{f_idx:04d}.png", "PNG")

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2",
            "-framerate", str(FPS),
            "-i", str(frames_dir / "f_%04d.png"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-t", f"{duration:.2f}", str(out_mp4),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def synthesize_tts(text: str, voice: str, mp3_path: Path) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice)
    await communicate.save(str(mp3_path))


def get_audio_duration(audio_path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return max(1.0, float(res.stdout.strip()))


def split_into_segments(voiceover_script: str, scene_plan_text: str) -> List[Tuple[str, str]]:
    cleaned_script = clean_voiceover_script(voiceover_script)
    scene_lines = [ln.strip() for ln in scene_plan_text.splitlines() if ln.strip()]
    sentences = [s.strip() for s in re.split(r"(?<=[।.!?])\s+", cleaned_script) if s.strip()]
    if not sentences:
        sentences = [cleaned_script or "বিস্তারিত তথ্যের জন্য আমাদের সাথে থাকুন।"]

    # Adaptive segment count so full scripts stay richly paced
    num_segments = max(6, min(30, max(len(scene_lines), len(sentences) // 2 or 6)))
    chunks = []
    total_s = len(sentences)
    for i in range(num_segments):
        start_idx = int(i * total_s / num_segments)
        end_idx = int((i + 1) * total_s / num_segments)
        part = " ".join(sentences[start_idx:end_idx]).strip()
        if part:
            chunks.append(part)

    if not chunks:
        chunks = [" ".join(sentences)]

    segments = []
    for idx, part in enumerate(chunks):
        label = scene_lines[idx] if idx < len(scene_lines) else f"পর্ব {idx + 1} / {len(chunks)}"
        segments.append((label, part))
    return segments


def build_video_from_segments(
    work_dir: Path,
    title: str,
    voiceover_script: str,
    scene_plan_text: str,
    voice_name: str,
    out_mp4: Path,
) -> float:
    work_dir.mkdir(parents=True, exist_ok=True)
    segments = split_into_segments(voiceover_script, scene_plan_text)
    clip_paths = []
    total_duration = 0.0

    for idx, (label, text) in enumerate(segments, start=1):
        seg_mp3 = work_dir / f"seg_{idx:02d}.mp3"
        seg_png = work_dir / f"seg_{idx:02d}.png"
        seg_mp4 = work_dir / f"seg_{idx:02d}.mp4"

        asyncio.run(synthesize_tts(text, voice_name, seg_mp3))
        dur = get_audio_duration(seg_mp3)
        total_duration += dur

        render_slide_image_16x9(
            out_path=seg_png,
            title=title,
            chapter_label=label,
            body_text=text,
            slide_index=idx,
            total_slides=len(segments),
        )

        cmd = [
            "ffmpeg", "-y", "-threads", "2",
            "-loop", "1", "-i", str(seg_png),
            "-i", str(seg_mp3),
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", "-movflags", "+faststart", str(seg_mp4),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        clip_paths.append(seg_mp4)

    outro_mp4 = work_dir / "outro_16x9.mp4"
    outro_with_audio = work_dir / "outro_16x9_audio.mp4"
    build_outro_16x9(outro_mp4, work_dir, duration=OUTRO_DURATION)
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2",
            "-i", str(outro_mp4),
            "-f", "lavfi", "-t", f"{OUTRO_DURATION:.2f}", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(outro_with_audio),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    clip_paths.append(outro_with_audio)
    total_duration += OUTRO_DURATION

    concat_list = work_dir / "concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for cp in clip_paths:
            f.write(f"file '{cp.resolve()}'\n")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    concat_cmd = [
        "ffmpeg", "-y", "-threads", "2",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(out_mp4),
    ]
    subprocess.run(concat_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return round(total_duration, 1)


def upload_to_github_release(video_path: Path, row_num: int, topic_id: str, title: str) -> Tuple[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        return "", ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tag_name = f"long-row-{row_num}-{int(time.time())}"
    rel_resp = requests.post(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        json={
            "tag_name": tag_name,
            "name": f"Long Video ({topic_id}): {title[:50]}",
            "body": "Automated 16:9 Bangla Long Video release for approval.",
            "draft": False,
            "prerelease": False,
        },
        timeout=30,
    )
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()
    release_id = str(rel_data["id"])
    release_html_url = str(rel_data.get("html_url", ""))
    upload_url = rel_data["upload_url"].split("{")[0]

    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_url}?name={video_path.name}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
            data=f,
            timeout=180,
        )
    up_resp.raise_for_status()
    download_url = str(up_resp.json().get("browser_download_url", release_html_url))
    return release_id, (download_url or release_html_url)


def notify_telegram_approval(
    topic_id: str,
    row_num: int,
    title: str,
    duration_sec: float,
    release_url: str,
) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return

    mins = int(duration_sec // 60)
    secs = int(duration_sec % 60)
    link_line = f"<a href='{release_url}'>Open Release Link</a>" if release_url else "Artifact in GitHub Actions"
    text = (
        "🎬 <b>Long Video (16:9) Ready for Approval</b>\n"
        f"• Topic ID: <code>{topic_id}</code> (Row {row_num})\n"
        f"• Title: {title}\n"
        f"• Duration: <b>{mins}m {secs}s</b>\n"
        f"• Release Link: {link_line}\n\n"
        "চেক করে অনুমোদন দিলে তবেই YouTube ও Facebook ধাপে যাবে।"
    )
    reply_markup = json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve Long Video", "callback_data": f"APPROVE_LONG_{row_num}"},
                    {"text": "❌ Reject", "callback_data": f"REJECT_LONG_{row_num}"},
                ]
            ]
        }
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
            timeout=20,
        )
    except Exception as e:
        log(f"Telegram notify skipped/failed: {e}")


def main() -> None:
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID secret is missing.")

    requested_topic = os.environ.get("TOPIC_ID", "").strip()
    max_videos = max(1, int(os.environ.get("MAX_VIDEOS", "1") or "1"))
    voice_name = os.environ.get("VOICE_NAME", "bn-BD-NabanitaNeural").strip() or "bn-BD-NabanitaNeural"

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    script_ws = sh.worksheet("Video Script")
    script_rows = script_ws.get_all_values()
    if len(script_rows) < 2:
        log("No rows found in 'Video Script' sheet.")
        return

    prod_ws = sh.worksheet("Video Production")
    prod_rows = prod_ws.get_all_values()
    completed_ids = set()
    prod_row_by_topic = {}
    for idx, r in enumerate(prod_rows[1:], start=2):
        t_id = str(r[0] if len(r) > 0 else "").strip()
        status = str(r[2] if len(r) > 2 else "").strip().lower()
        if t_id:
            prod_row_by_topic[t_id.upper()] = idx
            if status in ("ready", "completed", "done", "approved"):
                completed_ids.add(t_id.upper())

    candidates = []
    for r in script_rows[1:]:
        t_id = str(r[0] if len(r) > 0 else "").strip()
        v_title = str(r[1] if len(r) > 1 else "").strip()
        voiceover = str(r[3] if len(r) > 3 else "").strip()
        scene_plan = str(r[4] if len(r) > 4 else "").strip()
        if not t_id or not voiceover:
            continue
        if requested_topic:
            if t_id.upper() == requested_topic.upper():
                candidates.append((t_id, v_title or t_id, voiceover, scene_plan))
                break
        else:
            if t_id.upper() not in completed_ids:
                candidates.append((t_id, v_title or t_id, voiceover, scene_plan))

    if not candidates:
        log("No pending Long Video scripts to render.")
        return

    processed = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for t_id, v_title, voiceover, scene_plan in candidates[:max_videos]:
        log(f"Rendering 16:9 Long Video for {t_id}: {v_title}")
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", t_id)
        out_mp4 = Path("output") / f"{safe_id}_long_16x9.mp4"
        work_dir = Path("work") / safe_id

        duration_sec = build_video_from_segments(
            work_dir=work_dir,
            title=v_title,
            voiceover_script=voiceover,
            scene_plan_text=scene_plan,
            voice_name=voice_name,
            out_mp4=out_mp4,
        )

        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        dur_label = f"{mins}m {secs}s"

        if t_id.upper() in prod_row_by_topic:
            target_row_num = prod_row_by_topic[t_id.upper()]
        else:
            target_row_num = len(prod_ws.get_all_values()) + 1

        rel_id, rel_url = upload_to_github_release(out_mp4, target_row_num, t_id, v_title)
        link_val = rel_url or f"artifact://{out_mp4.name}"

        row_values = [
            t_id,
            v_title,
            "Ready",
            link_val,
            dur_label,
            "Auto-Checked",
            "Pending Approval",
            f"Release ID: {rel_id or 'N/A'} | Voice: {voice_name}",
            now_str,
        ]

        if t_id.upper() in prod_row_by_topic:
            prod_ws.update(f"A{target_row_num}:I{target_row_num}", [row_values])
        else:
            prod_ws.append_row(row_values, value_input_option="USER_ENTERED")

        notify_telegram_approval(t_id, target_row_num, v_title, duration_sec, rel_url)
        log(f"Completed {t_id} ({dur_label}) -> {link_val}")
        processed += 1

    log(f"Total long videos generated: {processed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"FATAL ERROR: {exc}")
        sys.exit(1)
