import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
import gspread
import requests
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_gspread_client() -> gspread.Client:
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON secret is missing.")
    info = json.loads(raw_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def find_bengali_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSerifBengali-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = re.split(r"\s+", text.strip())
    if not words or words == [""]:
        return []
    lines_out = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = candidate
        else:
            lines_out.append(current)
            current = word
    lines_out.append(current)
    return lines_out


def draw_broadcast_anchor_avatar(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    radius: int,
    slide_index: int,
) -> None:
    """Draws a crisp broadcast-style news anchor / correspondent emblem & portrait frame."""
    # Outer broadcast ring & subtle glow
    draw.ellipse(
        [cx - radius - 10, cy - radius - 10, cx + radius + 10, cy + radius + 10],
        fill=(220, 38, 38),
    )
    draw.ellipse(
        [cx - radius - 4, cy - radius - 4, cx + radius + 4, cy + radius + 4],
        fill=(248, 250, 252),
    )
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=(15, 23, 42),
    )

    # Stylized studio backdrop arcs inside avatar circle
    draw.arc(
        [cx - radius + 12, cy - radius + 12, cx + radius - 12, cy + radius - 12],
        start=180,
        end=360,
        fill=(37, 99, 235),
        width=8,
    )

    # Shoulders / broadcast blazer
    shoulder_top = cy + int(radius * 0.28)
    draw.pieslice(
        [cx - radius + 14, shoulder_top, cx + radius - 14, cy + radius + 42],
        start=180,
        end=360,
        fill=(30, 58, 138),
    )
    # Shirt collar & red/gold broadcast tie accent
    draw.polygon(
        [
            (cx - 18, shoulder_top + 6),
            (cx + 18, shoulder_top + 6),
            (cx, shoulder_top + 54),
        ],
        fill=(248, 250, 252),
    )
    draw.polygon(
        [
            (cx - 6, shoulder_top + 14),
            (cx + 6, shoulder_top + 14),
            (cx, shoulder_top + 52),
        ],
        fill=(220, 38, 38),
    )

    # Head & subtle expression shift per segment
    head_r = int(radius * 0.36)
    head_cy = cy - int(radius * 0.14)
    draw.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
        fill=(241, 194, 155),
        outline=(251, 191, 36),
        width=2,
    )
    # Hair cap
    draw.pieslice(
        [cx - head_r, head_cy - head_r - 4, cx + head_r, head_cy + int(head_r * 0.15)],
        start=180,
        end=360,
        fill=(17, 24, 39),
    )

    # Eyes & expression variation across segments
    eye_y = head_cy - 4
    eye_dx = int(head_r * 0.38)
    draw.ellipse([cx - eye_dx - 5, eye_y - 4, cx - eye_dx + 5, eye_y + 4], fill=(15, 23, 42))
    draw.ellipse([cx + eye_dx - 5, eye_y - 4, cx + eye_dx + 5, eye_y + 4], fill=(15, 23, 42))

    # Mouth expression changes slightly by segment index for visual variety
    mouth_y = head_cy + int(head_r * 0.42)
    if slide_index % 3 == 1:
        # Confident reporting smile/open speaking stance
        draw.arc([cx - 14, mouth_y - 8, cx + 14, mouth_y + 10], start=10, end=170, fill=(153, 27, 27), width=3)
    elif slide_index % 3 == 2:
        # Attentive analytical expression
        draw.line([(cx - 12, mouth_y), (cx + 12, mouth_y)], fill=(153, 27, 27), width=3)
    else:
        # Emphasized summary expression
        draw.ellipse([cx - 8, mouth_y - 5, cx + 8, mouth_y + 6], fill=(153, 27, 27))

    # Live / On-Air pill below avatar
    badge_w, badge_h = 128, 30
    bx1 = cx - badge_w // 2
    by1 = cy + radius - 14
    draw.rounded_rectangle([bx1, by1, bx1 + badge_w, by1 + badge_h], radius=15, fill=(220, 38, 38))
    draw.ellipse([bx1 + 12, by1 + 9, bx1 + 24, by1 + 21], fill=(254, 240, 138))
    font_tag = find_bengali_font(16)
    draw.text((bx1 + 30, by1 + 5), "LIVE DESK", font=font_tag, fill=(255, 255, 255))


def render_tv_report_slide(
    out_path: Path,
    title: str,
    chapter_label: str,
    body_text: str,
    slide_index: int,
    total_slides: int,
    channel_name: str = "HOMEBD NEWS DESK",
    avatar_path: str = "",
) -> None:
    """
    Renders a 1920x1080 broadcast television report frame with:
    - Top channel masthead, LIVE / SPECIAL REPORT badge, and timestamp bar
    - Main report headline & analytical brief card
    - Anchor/correspondent avatar panel on the right
    - Bottom ticker tape & lower-third chapter banner
    """
    width, height = 1920, 1080
    img = Image.new("RGB", (width, height), color=(10, 15, 29))
    draw = ImageDraw.Draw(img)

    # Subtle broadcast grid / studio background bands
    draw.rectangle([0, 0, width, 74], fill=(15, 23, 42))
    draw.rectangle([0, 74, width, 80], fill=(220, 38, 38))

    # Diagonal studio accent stripe on right side
    draw.polygon(
        [(1360, 80), (1920, 80), (1920, 920), (1180, 920)],
        fill=(17, 27, 52),
    )
    draw.line([(1360, 80), (1180, 920)], fill=(30, 64, 175), width=4)

    font_top_brand = find_bengali_font(26)
    font_live = find_bengali_font(22)
    font_headline = find_bengali_font(46)
    font_chapter = find_bengali_font(26)
    font_body = find_bengali_font(36)
    font_ticker = find_bengali_font(24)
    font_meta = find_bengali_font(20)

    # Top bar left: Channel brand + BREAKING / SPECIAL REPORT badge
    draw.rounded_rectangle([36, 16, 176, 58], radius=6, fill=(220, 38, 38))
    draw.text((52, 22), "বিশেষ প্রতিবেদন", font=font_live, fill=(255, 255, 255))

    clean_channel = (channel_name or "HOMEBD NEWS DESK").strip()[:36]
    draw.text((196, 20), clean_channel, font=font_top_brand, fill=(248, 250, 252))

    # Top bar right: Segment indicator & clock
    seg_meta = f"পর্ব {slide_index}/{total_slides}  |  {datetime.now().strftime('%d %b %Y • %H:%M')}"
    draw.text((width - 420, 24), seg_meta, font=font_meta, fill=(148, 163, 184))

    # Left Main Content Area (x: 48 to 1330)
    # Main Headline Banner
    headline_lines = wrap_text(draw, title, font_headline, 1240)[:2]
    hy = 104
    draw.rounded_rectangle([48, hy, 1330, hy + 136], radius=12, fill=(23, 37, 84), outline=(59, 130, 246), width=2)
    draw.rectangle([48, hy, 62, hy + 136], fill=(239, 68, 68))

    ty_head = hy + 16 if len(headline_lines) == 2 else hy + 40
    for hline in headline_lines:
        draw.text((84, ty_head), hline, font=font_headline, fill=(255, 255, 255))
        ty_head += 56

    # Analytical Report Body Card
    card_top = hy + 160
    card_bottom = 816
    draw.rounded_rectangle(
        [48, card_top, 1330, card_bottom],
        radius=14,
        fill=(15, 23, 42),
        outline=(51, 65, 85),
        width=2,
    )

    # Sub-header inside card
    draw.rounded_rectangle([72, card_top + 20, 1306, card_top + 68], radius=8, fill=(30, 41, 59))
    draw.text((92, card_top + 28), "প্রতিবেদনের মূল তথ্য ও বিশ্লেষণ", font=font_chapter, fill=(250, 204, 21))

    body_lines = wrap_text(draw, body_text, font_body, 1180)
    max_lines = max(1, (card_bottom - (card_top + 88) - 24) // 50)
    shown_lines = body_lines[:max_lines]
    if len(body_lines) > max_lines and shown_lines:
        shown_lines[-1] = shown_lines[-1][: max(0, len(shown_lines[-1]) - 3)] + "..."

    by = card_top + 90
    for bline in shown_lines:
        draw.text((92, by), bline, font=font_body, fill=(241, 245, 249))
        by += 50

    # Right Panel: Anchor / Correspondent Avatar & Briefing Box (x: 1365 to 1884)
    panel_x1, panel_y1, panel_x2, panel_y2 = 1365, 104, 1884, 816
    draw.rounded_rectangle(
        [panel_x1, panel_y1, panel_x2, panel_y2],
        radius=14,
        fill=(15, 23, 42),
        outline=(51, 65, 85),
        width=2,
    )

    avatar_cx = (panel_x1 + panel_x2) // 2
    avatar_cy = panel_y1 + 235
    avatar_r = 165

    custom_loaded = False
    if avatar_path and os.path.exists(avatar_path):
        try:
            av_img = Image.open(avatar_path).convert("RGBA")
            av_size = avatar_r * 2
            av_img = av_img.resize((av_size, av_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (av_size, av_size), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, av_size, av_size], fill=255)
            draw.ellipse(
                [avatar_cx - avatar_r - 8, avatar_cy - avatar_r - 8, avatar_cx + avatar_r + 8, avatar_cy + avatar_r + 8],
                fill=(220, 38, 38),
            )
            img.paste(av_img, (avatar_cx - avatar_r, avatar_cy - avatar_r), mask)
            custom_loaded = True
        except Exception as e:
            log(f"Custom avatar load fallback: {e}")

    if not custom_loaded:
        draw_broadcast_anchor_avatar(draw, avatar_cx, avatar_cy, avatar_r, slide_index)

    # Presenter / Desk Card under avatar
    desk_box_top = avatar_cy + avatar_r + 36
    draw.rounded_rectangle(
        [panel_x1 + 24, desk_box_top, panel_x2 - 24, panel_y2 - 24],
        radius=10,
        fill=(30, 41, 59),
        outline=(71, 85, 105),
        width=1,
    )
    draw.text((panel_x1 + 42, desk_box_top + 16), "নিউজ ডেস্ক প্রতিনিধি", font=font_chapter, fill=(96, 165, 250))

    clean_label = re.sub(r"[^\w\s\u0980-\u09FF:/.-]+", "", chapter_label or "").strip()
    chap_display = clean_label or f"অধ্যায় {slide_index}"
    chap_lines = wrap_text(draw, chap_display, font_ticker, (panel_x2 - panel_x1) - 84)[:3]
    cy_txt = desk_box_top + 58
    for cline in chap_lines:
        draw.text((panel_x1 + 42, cy_txt), cline, font=font_ticker, fill=(248, 250, 252))
        cy_txt += 36

    # Lower Third Banner (y: 834 to 996)
    lt_top = 834
    lt_bottom = 996
    draw.rectangle([0, lt_top, width, lt_bottom], fill=(15, 23, 42))
    draw.rectangle([0, lt_top, 360, lt_bottom], fill=(185, 28, 28))
    draw.polygon([(360, lt_top), (410, lt_top), (370, lt_bottom), (320, lt_bottom)], fill=(250, 204, 21))

    draw.text((28, lt_top + 24), "লোয়ার থার্ড", font=font_meta, fill=(254, 202, 202))
    lt_title_lines = wrap_text(draw, chap_display, font_headline, 1420)[:2]
    ly = lt_top + 54 if len(lt_title_lines) == 1 else lt_top + 52
    for lline in lt_title_lines:
        draw.text((425, ly), lline, font=font_headline, fill=(255, 255, 255))
        ly += 52

    # Bottom News Ticker Tape (y: 996 to 1080)
    ticker_top = 996
    draw.rectangle([0, ticker_top, width, height], fill=(2, 6, 23))
    draw.rectangle([0, ticker_top, 230, height], fill=(250, 204, 21))
    draw.text((24, ticker_top + 26), "ব্রেকিং / আপডেট", font=font_ticker, fill=(15, 23, 42))

    ticker_msg = f"{title}  •  {chap_display}  •  {clean_channel}"
    ticker_lines = wrap_text(draw, ticker_msg, font_ticker, width - 270)[:1]
    if ticker_lines:
        draw.text((250, ticker_top + 26), ticker_lines[0], font=font_ticker, fill=(226, 232, 240))

    img.save(out_path, format="PNG")


async def synthesize_tts(text: str, voice: str, mp3_path: Path) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice)
    await communicate.save(str(mp3_path))


def get_audio_duration(audio_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return max(1.0, float(res.stdout.strip()))


def split_into_segments(voiceover_script: str, scene_plan_text: str, num_segments: int = 6) -> list[tuple[str, str]]:
    scene_lines = [ln.strip() for ln in scene_plan_text.splitlines() if ln.strip()]
    sentences = [s.strip() for s in re.split(r"(?<=[।.!?])\s+", voiceover_script.strip()) if s.strip()]
    if not sentences:
        sentences = [voiceover_script.strip() or "বিস্তারিত তথ্যের জন্য আমাদের সাথে থাকুন।"]

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
        label = scene_lines[idx] if idx < len(scene_lines) else f"পর্ব {idx + 1}"
        segments.append((label, part))
    return segments


def render_segment_video_with_motion(
    seg_png: Path,
    seg_mp3: Path,
    seg_mp4: Path,
    duration_sec: float,
    slide_index: int,
) -> None:
    """
    Creates a broadcast-style 1920x1080 video clip with subtle Ken Burns zoom/pan motion
    and a top progress bar so each segment feels like a TV news report rather than a static image.
    """
    frames = max(25, int(math.ceil(duration_sec * 25)))
    # Alternate gentle zoom-in / zoom-out around center
    if slide_index % 2 == 1:
        zoom_expr = f"1+0.04*on/{frames}"
    else:
        zoom_expr = f"1.04-0.04*on/{frames}"

    vf_chain = (
        f"scale=2048:1152,"
        f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080:fps=25,"
        f"drawbox=x=0:y=74:w=iw*(t/{max(duration_sec, 0.1)}):h=6:color=0xFACC15@0.95:t=fill,"
        f"format=yuv420p"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(seg_png),
        "-i",
        str(seg_mp3),
        "-vf",
        vf_chain,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(seg_mp4),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_video_from_segments(
    work_dir: Path,
    title: str,
    voiceover_script: str,
    scene_plan_text: str,
    voice_name: str,
    out_mp4: Path,
    channel_name: str = "HOMEBD NEWS DESK",
    avatar_path: str = "",
) -> float:
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    segments = split_into_segments(voiceover_script, scene_plan_text, num_segments=6)
    clip_paths = []
    total_duration = 0.0

    for idx, (label, text) in enumerate(segments, start=1):
        seg_mp3 = work_dir / f"seg_{idx:02d}.mp3"
        seg_png = work_dir / f"seg_{idx:02d}.png"
        seg_mp4 = work_dir / f"seg_{idx:02d}.mp4"

        asyncio.run(synthesize_tts(text, voice_name, seg_mp3))
        dur = get_audio_duration(seg_mp3)
        total_duration += dur

        render_tv_report_slide(
            out_path=seg_png,
            title=title,
            chapter_label=label,
            body_text=text,
            slide_index=idx,
            total_slides=len(segments),
            channel_name=channel_name,
            avatar_path=avatar_path,
        )

        render_segment_video_with_motion(
            seg_png=seg_png,
            seg_mp3=seg_mp3,
            seg_mp4=seg_mp4,
            duration_sec=dur,
            slide_index=idx,
        )
        clip_paths.append(seg_mp4)

    concat_list = work_dir / "concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for cp in clip_paths:
            f.write(f"file '{cp.resolve()}'\n")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    concat_cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(out_mp4),
    ]
    subprocess.run(concat_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return round(total_duration, 1)


def delete_github_release_if_exists(release_id: str) -> None:
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not gh_token or not gh_repo or not release_id or not str(release_id).strip().isdigit():
        return
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        requests.delete(
            f"https://api.github.com/repos/{gh_repo}/releases/{str(release_id).strip()}",
            headers=headers,
            timeout=25,
        )
    except Exception as e:
        log(f"Old release cleanup warning: {e}")


def upload_to_github_release(topic_id: str, mp4_path: Path, old_release_id: str = "") -> tuple[str, str]:
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not gh_token or not gh_repo:
        return "", f"artifact://{mp4_path.name}"

    if old_release_id:
        delete_github_release_if_exists(old_release_id)

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", f"long-{topic_id}-{stamp}")
    use_prerelease = os.environ.get("RELEASE_PRERELEASE", "false").strip().lower() in ("1", "true", "yes")

    rel_res = requests.post(
        f"https://api.github.com/repos/{gh_repo}/releases",
        headers=headers,
        json={
            "tag_name": safe_tag,
            "name": f"TV Report Long Video: {topic_id}",
            "body": f"Automated 16:9 TV Report Long Video asset for {topic_id}",
            "draft": False,
            "prerelease": use_prerelease,
        },
        timeout=30,
    )
    rel_res.raise_for_status()
    rel_json = rel_res.json()
    release_id = str(rel_json.get("id", ""))
    upload_url_raw = str(rel_json.get("upload_url", ""))
    upload_base = upload_url_raw.split("{")[0]

    with open(mp4_path, "rb") as f:
        asset_res = requests.post(
            f"{upload_base}?name={mp4_path.name}",
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Content-Type": "video/mp4",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            data=f,
            timeout=240,
        )
    asset_res.raise_for_status()
    asset_json = asset_res.json()
    download_url = str(asset_json.get("browser_download_url") or f"artifact://{mp4_path.name}")
    return release_id, download_url


def notify_telegram_with_controls(topic_id: str, title: str, duration_label: str, video_url: str, release_id: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    text = (
        "📺 <b>TV Report Long Video (16:9) Ready for Review</b>\n"
        f"• <b>Topic ID:</b> <code>{topic_id}</code>\n"
        f"• <b>Title:</b> {title}\n"
        f"• <b>Duration:</b> {duration_label}\n"
        f"• <b>Release ID:</b> <code>{release_id or 'Artifact'}</code>\n"
        f"• <b>Video URL:</b> {video_url}"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"LONG_APPROVE_{topic_id}"},
                {"text": "⏰ Schedule", "callback_data": f"LONG_SCHEDULE_{topic_id}"},
            ],
            [
                {"text": "🔄 Regenerate", "callback_data": f"LONG_REGEN_{topic_id}"},
                {"text": "❌ Reject", "callback_data": f"LONG_REJECT_{topic_id}"},
            ],
        ]
    }
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": markup,
                "disable_web_page_preview": True,
            },
            timeout=25,
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
    channel_name = os.environ.get("CHANNEL_NAME", "HOMEBD NEWS DESK").strip() or "HOMEBD NEWS DESK"
    avatar_path = os.environ.get("AVATAR_PATH", "assets/anchor_avatar.png").strip()

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
    rejected_ids = set()
    regen_ids = set()
    prod_row_by_topic = {}
    old_release_by_topic = {}

    for idx, r in enumerate(prod_rows[1:], start=2):
        t_id = str(r[0] if len(r) > 0 else "").strip()
        prod_status = str(r[2] if len(r) > 2 else "").strip().upper()
        approval_status = str(r[6] if len(r) > 6 else "").strip().upper()
        rel_id = str(r[9] if len(r) > 9 else "").strip()
        if t_id:
            t_up = t_id.upper()
            prod_row_by_topic[t_up] = idx
            if rel_id:
                old_release_by_topic[t_up] = rel_id
            if prod_status in ("REJECTED", "REJECT") or approval_status in ("REJECTED", "REJECT"):
                rejected_ids.add(t_up)
            elif prod_status == "REGENERATE_REQUESTED":
                regen_ids.add(t_up)
            elif prod_status in ("READY", "COMPLETED", "DONE", "APPROVED", "SCHEDULED", "PUBLISHED"):
                completed_ids.add(t_up)

    candidates = []
    for r in script_rows[1:]:
        t_id = str(r[0] if len(r) > 0 else "").strip()
        v_title = str(r[1] if len(r) > 1 else "").strip()
        voiceover = str(r[3] if len(r) > 3 else "").strip()
        scene_plan = str(r[4] if len(r) > 4 else "").strip()
        if not t_id or not voiceover:
            continue
        t_up = t_id.upper()
        if t_up in rejected_ids:
            continue
        if requested_topic:
            if t_up == requested_topic.upper():
                candidates.append((t_id, v_title or t_id, voiceover, scene_plan))
                break
        else:
            if t_up in regen_ids or t_up not in completed_ids:
                candidates.append((t_id, v_title or t_id, voiceover, scene_plan))

    if not candidates:
        log("No pending or regenerate-requested Long Video scripts to render.")
        return

    processed = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for t_id, v_title, voiceover, scene_plan in candidates[:max_videos]:
        log(f"Rendering 16:9 TV Report Long Video for {t_id}: {v_title}")
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
            channel_name=channel_name,
            avatar_path=avatar_path,
        )

        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        dur_label = f"{mins}m {secs}s"

        old_rel = old_release_by_topic.get(t_id.upper(), "")
        release_id, video_url = upload_to_github_release(t_id, out_mp4, old_release_id=old_rel)

        row_values = [
            t_id,
            v_title,
            "Ready",
            video_url,
            dur_label,
            "Auto-Checked",
            "Pending Review",
            f"Generated 16:9 TV report video via GitHub Actions ({voice_name}) | Release ID: {release_id}",
            now_str,
            release_id,
            video_url,
            "ACTIVE",
        ]

        if t_id.upper() in prod_row_by_topic:
            r_num = prod_row_by_topic[t_id.upper()]
            prod_ws.update(f"A{r_num}:L{r_num}", [row_values])
        else:
            prod_ws.append_row(row_values, value_input_option="USER_ENTERED")

        notify_telegram_with_controls(t_id, v_title, dur_label, video_url, release_id)
        log(f"Completed {t_id} ({dur_label}) -> {video_url} [Release: {release_id}]")
        processed += 1

    log(f"Total long videos generated: {processed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"FATAL ERROR: {exc}")
        sys.exit(1)
