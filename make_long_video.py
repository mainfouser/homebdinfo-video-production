import asyncio
import json
import os
import re
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


def render_slide_image(
    out_path: Path,
    title: str,
    chapter_label: str,
    body_text: str,
    slide_index: int,
    total_slides: int,
) -> None:
    width, height = 1920, 1080
    img = Image.new("RGB", (width, height), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, 12], fill=(16, 185, 129))
    draw.rectangle([0, height - 68, width, height], fill=(30, 41, 59))

    font_badge = find_bengali_font(28)
    font_title = find_bengali_font(48)
    font_body = find_bengali_font(40)
    font_footer = find_bengali_font(26)

    clean_label = re.sub(r"[^\w\s\u0980-\u09FF:/.-]+", "", chapter_label or "").strip()
    badge_text = clean_label or f"অধ্যায় {slide_index}/{total_slides}"
    badge_text = badge_text[:34]

    badge_bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
    badge_w = max(240, (badge_bbox[2] - badge_bbox[0]) + 48)
    badge_right = min(width - 80, 80 + badge_w)

    draw.rounded_rectangle([80, 48, badge_right, 104], radius=12, fill=(16, 185, 129))
    draw.text((104, 58), badge_text, font=font_badge, fill=(15, 23, 42))

    title_lines = wrap_text(draw, title, font_title, width - 160)[:2]
    y = 130
    for line in title_lines:
        draw.text((80, y), line, font=font_title, fill=(248, 250, 252))
        y += 62

    draw.line([(80, y + 10), (width - 80, y + 10)], fill=(51, 65, 85), width=2)
    y += 42

    card_top = y
    card_bottom = height - 105
    draw.rounded_rectangle(
        [80, card_top, width - 80, card_bottom],
        radius=20,
        fill=(30, 41, 59),
        outline=(71, 85, 105),
        width=2,
    )

    body_lines = wrap_text(draw, body_text, font_body, width - 240)
    max_lines = max(1, (card_bottom - card_top - 80) // 56)
    display_lines = body_lines[:max_lines]
    if len(body_lines) > max_lines:
        display_lines[-1] = display_lines[-1][: max(0, len(display_lines[-1]) - 3)] + "..."

    ty = card_top + 40
    for line in display_lines:
        draw.text((120, ty), line, font=font_body, fill=(226, 232, 240))
        ty += 56

    draw.text(
        (80, height - 52),
        f"HomeBDInfo.com  |  স্লাইড {slide_index} / {total_slides}",
        font=font_footer,
        fill=(148, 163, 184),
    )

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


def build_video_from_segments(
    work_dir: Path,
    title: str,
    voiceover_script: str,
    scene_plan_text: str,
    voice_name: str,
    out_mp4: Path,
) -> float:
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

        render_slide_image(
            out_path=seg_png,
            title=title,
            chapter_label=label,
            body_text=text,
            slide_index=idx,
            total_slides=len(segments),
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
            "-c:v",
            "libx264",
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
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
            "name": f"Long Video: {topic_id}",
            "body": f"Automated 16:9 Long Video asset for {topic_id}",
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
        "🎬 <b>Long Video (16:9) Ready for Review</b>\n"
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
            f"Generated 16:9 long video via GitHub Actions ({voice_name}) | Release ID: {release_id}",
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
