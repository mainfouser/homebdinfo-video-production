import os
import re
import gc
import json
import time
import asyncio
import tempfile
import subprocess
from urllib.parse import urljoin
from typing import List, Dict, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from playwright.sync_api import sync_playwright

VOICE_NAME = "bn-BD-PradeepNeural"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
SHEET_NAME = os.environ.get("LONG_SHEET_NAME", "Long_Video_Queue")
BGM_FILE = "bgm.mp3"
LOGO_FILE = "logo.png"

BGM_VOICE_VOLUME = 0.042
BGM_OUTRO_VOLUME = 0.075

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
MAX_SCROLL_PX = 6800
HOOK_DURATION = 4.2
COVER_DURATION = 0.35
OUTRO_DURATION = 4.8

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


def get_google_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not creds_json or not sheet_id:
        raise RuntimeError("GOOGLE_CREDENTIALS বা SHEET_ID সেট করা নেই।")

    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id).worksheet(SHEET_NAME)


def _spell_domain_for_tts(domain_str: str) -> str:
    parts = [p.strip() for p in domain_str.strip().split(".") if p.strip()]
    if not parts:
        return domain_str
    spoken_parts = []
    for part in parts:
        low = part.lower()
        if low == "www":
            spoken_parts.append("ডাব্লিউ ডাব্লিউ ডাব্লিউ")
        elif low == "gov":
            spoken_parts.append("গভ")
        elif low == "bd":
            spoken_parts.append("বিডি")
        elif low == "com":
            spoken_parts.append("কম")
        elif low == "org":
            spoken_parts.append("ওআরজি")
        elif low == "net":
            spoken_parts.append("নেট")
        elif low == "edu":
            spoken_parts.append("ইডিইউ")
        elif re.fullmatch(r"[a-z]{1,4}", low):
            spoken_parts.append(" ".join(list(low)))
        else:
            spoken_parts.append(part)
    return " ডট ".join(spoken_parts)


def clean_voiceover_script(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(
        r"হোম\s*বিডি\s*ইনফো\s*ডট\s*কম\s*\(?homebdinfo\.com\)?",
        "হোম বিডি ইনফো ডট কম",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"homebdinfo\.com\s*\(?হোম\s*বিডি\s*ইনফো\s*ডট\s*কম\)?",
        "হোম বিডি ইনফো ডট কম",
        text,
        flags=re.IGNORECASE,
    )
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
    text = re.sub(r"\s*:\s*", ": ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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
            if merged and (len(sp) < 15 or len(merged[-1]) < 18):
                merged[-1] = f"{merged[-1]} {sp}".strip()
            else:
                merged.append(sp)

        for c_idx, clause_txt in enumerate(merged):
            is_sentence_end = (c_idx == len(merged) - 1)
            clauses.append(
                {
                    "text": clause_txt,
                    "sent_idx": s_idx,
                    "total_sents": total_s,
                    "is_hook": (s_idx == 0 and c_idx == 0),
                    "is_sentence_end": is_sentence_end,
                }
            )
    return clauses


def get_tv_presenter_prosody(clause_info: Dict) -> Tuple[str, str, float]:
    txt = clause_info["text"]
    s_idx = clause_info["sent_idx"]
    total_s = clause_info["total_sents"]
    is_hook = clause_info["is_hook"]
    is_end = clause_info["is_sentence_end"]

    pause = 0.28 if is_end else 0.11

    if is_hook:
        return "+4%", "-1Hz", 0.26

    if "?" in txt or any(w in txt for w in ["কীভাবে", "কেন", "কী", "কত", "কখন", "আদৌ"]):
        return "+2%", "+1Hz", pause + 0.03

    if any(w in txt for w in ["সতর্ক", "জরুরি", "অবশ্যই", "মনে রাখবেন", "ভুল", "জরিমানা", "আইন", "নিষেধ", "বাতিল"]):
        return "-1%", "-2Hz", pause + 0.04

    if any(w in txt for w in ["প্রথম", "দ্বিতীয়", "তৃতীয়", "ধাপ", "ক্যাটাগরি", "পয়েন্ট", "নিয়ম"]):
        return "+1%", "-1Hz", pause + 0.02

    if s_idx >= max(1, total_s - 1) and is_end:
        return "-1%", "-2Hz", 0.32

    pattern_idx = (s_idx + (0 if is_end else 1)) % 3
    if pattern_idx == 0:
        return "+1%", "-1Hz", pause
    elif pattern_idx == 1:
        return "+0%", "-2Hz", pause
    else:
        return "+2%", "-1Hz", pause


async def _synthesize_phrase(text: str, out_path: str, rate: str, pitch: str, voice: str = VOICE_NAME):
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await communicate.save(out_path)


def split_kinetic_chunks(sentence: str, max_chars: int = 40) -> List[str]:
    words = sentence.split()
    chunks = []
    cur = []
    cur_len = 0
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
        wav_seg = os.path.join(work_dir, f"tts_{idx}.wav")

        asyncio.run(_synthesize_phrase(clause_text, raw_mp3, rate, pitch, voice=VOICE_NAME))

        subprocess.run(
            ["ffmpeg", "-y", "-threads", "2", "-i", raw_mp3, "-ar", "44100", "-ac", "2", wav_seg],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.exists(raw_mp3):
            os.remove(raw_mp3)

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", wav_seg,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        dur = float(probe.stdout.strip())
        segment_files.append(wav_seg)

        clause_start = current_time
        current_time += dur

        chunks = split_kinetic_chunks(clause_text, max_chars=40)
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
                    "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{end_pause:.2f}", sil_wav,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            segment_files.append(sil_wav)
            current_time += end_pause

    concat_list = os.path.join(work_dir, "audio_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    raw_concat_wav = os.path.join(work_dir, "voiceover_raw.wav")
    final_voice_wav = os.path.join(work_dir, "voiceover_full.wav")

    subprocess.run(
        ["ffmpeg", "-y", "-threads", "2", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", raw_concat_wav],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

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
            "-ar", "44100", "-ac", "2", final_voice_wav,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for sf in segment_files:
        if os.path.exists(sf):
            os.remove(sf)
    if os.path.exists(raw_concat_wav):
        os.remove(raw_concat_wav)

    return final_voice_wav, kinetic_segments


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


def create_minimal_watermark_png(out_path: str):
    w, h = 440, 82
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [4, 6, w - 4, h - 6],
        radius=36,
        fill=(9, 24, 38, 215),
        outline=(250, 204, 21, 195),
        width=2,
    )
    text_x = 24
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 54
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            draw.ellipse([12, 12, 70, 70], fill=(255, 255, 255, 245))
            img.paste(logo, (14, (h - lh) // 2), logo)
            text_x = 84
        except Exception:
            pass
    bn_font = find_font(BENGALI_FONT_PATHS, 28)
    en_font = find_font(LATIN_FONT_PATHS, 28)
    render_mixed_text_line(draw, text_x, 23, "homebdinfo.com", bn_font, en_font, fill=(255, 255, 255, 250))
    img.save(out_path, "PNG")


def create_clean_subtitle_png(text: str, is_hook: bool, out_path: str):
    canvas_w = 1040
    canvas_h = 340
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_size = 56 if is_hook else 46
    if len(text) > 38:
        font_size = 41

    bn_font = find_font(BENGALI_FONT_PATHS, font_size)
    en_font = find_font(LATIN_FONT_PATHS, font_size - 4)
    lines = wrap_text_by_pixel(draw, text, bn_font, en_font, 880)
    if len(lines) > 2:
        lines = lines[:2]
    line_h = font_size + 18
    total_h = len(lines) * line_h
    start_y = (canvas_h - total_h) // 2
    max_line_w = max((measure_mixed_text_width(draw, ln, bn_font, en_font) for ln in lines), default=400)
    pad_x, pad_y = 38, 18
    box_x0 = max(20, (canvas_w - max_line_w) // 2 - pad_x)
    box_x1 = min(canvas_w - 20, (canvas_w + max_line_w) // 2 + pad_x)
    box_y0 = max(10, start_y - pad_y)
    box_y1 = min(canvas_h - 10, start_y + total_h + pad_y - 8)
    border_col = (250, 204, 21, 225) if is_hook else (255, 255, 255, 115)
    draw.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1],
        radius=24,
        fill=(9, 20, 32, 220),
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
    screenshot_png: str,
    out_mp4: str,
    duration: float = HOOK_DURATION,
):
    sc = Image.open(screenshot_png).convert("RGB")
    bg_crop = sc.crop((0, 0, VIDEO_WIDTH, min(VIDEO_HEIGHT, sc.height)))
    if bg_crop.height < VIDEO_HEIGHT:
        padded = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (8, 22, 36))
        padded.paste(bg_crop, (0, 0))
        bg_crop = padded

    bg_blur = bg_crop.filter(ImageFilter.GaussianBlur(radius=16))
    dark_scrim = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 17, 29, 165))
    base = Image.alpha_composite(bg_blur.convert("RGBA"), dark_scrim)
    draw = ImageDraw.Draw(base)

    badge_w, badge_h = 460, 62
    bx0 = (VIDEO_WIDTH - badge_w) // 2
    by0 = 250
    draw.rounded_rectangle(
        [bx0, by0, bx0 + badge_w, by0 + badge_h],
        radius=31,
        fill=(250, 204, 21, 255),
    )
    bn_badge = find_font(BENGALI_FONT_PATHS, 29)
    en_badge = find_font(LATIN_FONT_PATHS, 27)
    b_txt = "বিস্তারিত গাইড • HOME BD INFO"
    bw = measure_mixed_text_width(draw, b_txt, bn_badge, en_badge)
    render_mixed_text_line(draw, (VIDEO_WIDTH - bw) // 2, by0 + 13, b_txt, bn_badge, en_badge, fill=(8, 22, 36, 255))

    clean_title = re.sub(r"#\w+", "", title).strip()
    bn_t = find_font(BENGALI_FONT_PATHS, 54)
    en_t = find_font(LATIN_FONT_PATHS, 50)
    t_lines = wrap_text_by_pixel(draw, clean_title, bn_t, en_t, VIDEO_WIDTH - 200)[:3]

    clean_hook = re.sub(r"[•✔*#]+", "", hook_line).strip()
    bn_h = find_font(BENGALI_FONT_PATHS, 39)
    en_h = find_font(LATIN_FONT_PATHS, 37)
    h_lines = wrap_text_by_pixel(draw, clean_hook, bn_h, en_h, VIDEO_WIDTH - 210)[:3]

    title_block_h = len(t_lines) * 74
    hook_block_h = len(h_lines) * 53
    card_h = 52 + title_block_h + 34 + hook_block_h + 48
    card_x0, card_y0 = 64, 345
    card_x1, card_y1 = VIDEO_WIDTH - 64, card_y0 + card_h

    draw.rounded_rectangle(
        [card_x0, card_y0, card_x1, card_y1],
        radius=34,
        fill=(9, 24, 38, 242),
        outline=(250, 204, 21, 220),
        width=3,
    )

    cur_y = card_y0 + 48
    for i, ln in enumerate(t_lines):
        lw = measure_mixed_text_width(draw, ln, bn_t, en_t)
        col = (250, 204, 21, 255) if i == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, (VIDEO_WIDTH - lw) // 2, cur_y, ln, bn_t, en_t, fill=col)
        cur_y += 74

    draw.line([(card_x0 + 60, cur_y + 12), (card_x1 - 60, cur_y + 12)], fill=(250, 204, 21, 110), width=2)
    cur_y += 40

    for hl in h_lines:
        hw = measure_mixed_text_width(draw, hl, bn_h, en_h)
        render_mixed_text_line(draw, (VIDEO_WIDTH - hw) // 2, cur_y, hl, bn_h, en_h, fill=(226, 232, 240, 255))
        cur_y += 53

    hook_png = os.path.join(os.path.dirname(out_mp4), "hook_card.png")
    base.convert("RGB").save(hook_png, "PNG")
    del sc, bg_crop, bg_blur, dark_scrim, base, draw
    gc.collect()

    n_frames = max(1, int(round(duration * FPS)))
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-loop", "1", "-i", hook_png,
            "-vf", (
                f"scale={VIDEO_WIDTH * 2}:{VIDEO_HEIGHT * 2},"
                f"zoompan=z='1+0.03*(on/{max(1, n_frames)})':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
                f":d={n_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS}"
            ),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
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
    screenshot_png: str,
    visual_clips: List[str],
    out_jpg: str,
    work_dir: str,
):
    bg_img = None
    first_clip = visual_clips[0] if visual_clips else None
    if first_clip and os.path.exists(first_clip):
        frame_png = os.path.join(work_dir, "thumb_stock_frame.png")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-threads", "2", "-i", first_clip, "-ss", "00:00:00.5", "-vframes", "1", frame_png],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.path.exists(frame_png):
                raw = Image.open(frame_png).convert("RGB")
                scale = max(VIDEO_WIDTH / raw.width, VIDEO_HEIGHT / raw.height)
                nw, nh = int(raw.width * scale), int(raw.height * scale)
                raw = raw.resize((nw, nh), Image.Resampling.LANCZOS)
                left = (nw - VIDEO_WIDTH) // 2
                top = (nh - VIDEO_HEIGHT) // 2
                bg_img = raw.crop((left, top, left + VIDEO_WIDTH, top + VIDEO_HEIGHT))
                os.remove(frame_png)
        except Exception:
            bg_img = None

    if bg_img is None:
        sc = Image.open(screenshot_png).convert("RGB")
        bg_img = sc.crop((0, 0, VIDEO_WIDTH, min(VIDEO_HEIGHT, sc.height)))
        if bg_img.height < VIDEO_HEIGHT:
            padded = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (8, 24, 38))
            padded.paste(bg_img, (0, 0))
            bg_img = padded

    canvas = bg_img.convert("RGBA")
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    for y in range(VIDEO_HEIGHT):
        ratio = y / VIDEO_HEIGHT
        alpha = (
            int(205 - (ratio / 0.25) * 50)
            if ratio < 0.25
            else (int(155 + ((ratio - 0.65) / 0.35) * 80) if ratio > 0.65 else 155)
        )
        ov_draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(7, 20, 34, alpha))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    top_y = 115
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 160
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            canvas.paste(logo, ((VIDEO_WIDTH - lw) // 2, top_y), logo)
            top_y += lh + 35
        except Exception:
            pass

    clean_title = re.sub(r"#\w+", "", title).strip()
    bn_title_font = find_font(BENGALI_FONT_PATHS, 68)
    en_title_font = find_font(LATIN_FONT_PATHS, 64)
    lines = wrap_text_by_pixel(draw, clean_title, bn_title_font, en_title_font, 900)
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1][:22] + "..."

    line_h = 94
    box_pad_y = 44
    box_h = len(lines) * line_h + box_pad_y * 2
    box_y0 = max(top_y + 60, (VIDEO_HEIGHT - box_h) // 2)
    box_y1 = box_y0 + box_h
    draw.rounded_rectangle(
        [48, box_y0, VIDEO_WIDTH - 48, box_y1],
        radius=32,
        fill=(9, 25, 40, 235),
        outline=(250, 204, 21, 235),
        width=4,
    )

    for idx, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_title_font, en_title_font)
        lx = (VIDEO_WIDTH - lw) // 2
        ly = box_y0 + box_pad_y + idx * line_h
        txt_color = (250, 204, 21, 255) if idx % 2 == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, lx, ly, ln, bn_title_font, en_title_font, fill=txt_color)

    canvas.convert("RGB").save(out_jpg, "JPEG", quality=92)
    del canvas, overlay, bg_img
    gc.collect()


def _smooth_ease_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1.0 - ((1.0 - x) ** 3)


def _draw_broadcast_outro_backdrop() -> Image.Image:
    top_rgb = (7, 19, 32)
    bot_rgb = (12, 36, 56)
    bg = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (*top_rgb, 255))
    draw = ImageDraw.Draw(bg)

    for y in range(0, VIDEO_HEIGHT, 4):
        p = y / VIDEO_HEIGHT
        r = int(top_rgb[0] + (bot_rgb[0] - top_rgb[0]) * p)
        g = int(top_rgb[1] + (bot_rgb[1] - top_rgb[1]) * p)
        b = int(top_rgb[2] + (bot_rgb[2] - top_rgb[2]) * p)
        draw.rectangle([0, y, VIDEO_WIDTH, y + 4], fill=(r, g, b, 255))

    draw.rectangle([0, 0, VIDEO_WIDTH, 10], fill=(250, 204, 21, 230))
    draw.rectangle([0, VIDEO_HEIGHT - 10, VIDEO_WIDTH, VIDEO_HEIGHT], fill=(250, 204, 21, 230))
    return bg


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

    bn_tag = find_font(BENGALI_FONT_PATHS, 28)
    en_tag = find_font(LATIN_FONT_PATHS, 26)
    bn_h1 = find_font(BENGALI_FONT_PATHS, 48)
    en_h1 = find_font(LATIN_FONT_PATHS, 44)
    bn_pill = find_font(BENGALI_FONT_PATHS, 34)
    en_pill = find_font(LATIN_FONT_PATHS, 32)
    bn_sub = find_font(BENGALI_FONT_PATHS, 33)
    en_sub = find_font(LATIN_FONT_PATHS, 33)
    bn_dom = find_font(BENGALI_FONT_PATHS, 44)
    en_dom = find_font(LATIN_FONT_PATHS, 46)

    backdrop_base = _draw_broadcast_outro_backdrop()

    for f_idx in range(n_frames):
        t = f_idx / FPS
        ease_h1 = _smooth_ease_out(min(1.0, t / 0.42))
        ease_cta = _smooth_ease_out(max(0.0, min(1.0, (t - 0.22) / 0.45)))
        ease_dom = _smooth_ease_out(max(0.0, min(1.0, (t - 0.45) / 0.45)))

        img = backdrop_base.copy()
        draw = ImageDraw.Draw(img)

        top_pill_w, top_pill_h = 430, 56
        tpx0 = (VIDEO_WIDTH - top_pill_w) // 2
        tpy0 = 185
        draw.rounded_rectangle(
            [tpx0, tpy0, tpx0 + top_pill_w, tpy0 + top_pill_h],
            radius=28,
            fill=(15, 42, 66, 235),
            outline=(250, 204, 21, 200),
            width=2,
        )
        top_label = "HOME BD INFO • অফিসিয়াল চ্যানেল"
        tlw = measure_mixed_text_width(draw, top_label, bn_tag, en_tag)
        render_mixed_text_line(draw, (VIDEO_WIDTH - tlw) // 2, tpy0 + 11, top_label, bn_tag, en_tag, fill=(250, 204, 21, 255))

        ring_cx, ring_cy = VIDEO_WIDTH // 2, 410
        outer_r = 136
        inner_r = 116
        draw.ellipse(
            [ring_cx - outer_r, ring_cy - outer_r, ring_cx + outer_r, ring_cy + outer_r],
            outline=(250, 204, 21, 225),
            width=4,
        )
        draw.ellipse(
            [ring_cx - inner_r, ring_cy - inner_r, ring_cx + inner_r, ring_cy + inner_r],
            fill=(255, 255, 255, 252),
        )
        if logo_src is not None:
            lw = 176
            lh = int(logo_src.height * (lw / logo_src.width))
            resized_logo = logo_src.resize((lw, lh), Image.Resampling.LANCZOS)
            img.paste(resized_logo, (ring_cx - lw // 2, ring_cy - lh // 2), resized_logo)

        card_x0, card_y0 = 64, 620
        card_x1, card_y1 = VIDEO_WIDTH - 64, 1510
        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=36,
            fill=(11, 28, 45, 240),
            outline=(250, 204, 21, 175),
            width=3,
        )

        draw.rectangle([VIDEO_WIDTH // 2 - 64, card_y0 + 38, VIDEO_WIDTH // 2 + 64, card_y0 + 46], fill=(250, 204, 21, 255))

        h1 = "উপকারী ভিডিওটি ভালো লাগলে"
        w1 = measure_mixed_text_width(draw, h1, bn_h1, en_h1)
        h1_y = int(card_y0 + 72 + (1.0 - ease_h1) * 16)
        render_mixed_text_line(draw, (VIDEO_WIDTH - w1) // 2, h1_y, h1, bn_h1, en_h1, fill=(255, 255, 255, 255))

        cta_y = int(card_y0 + 158 + (1.0 - ease_cta) * 16)
        pills = [
            ("👍 লাইক দিন", (23, 56, 86, 245), (250, 204, 21, 235), (250, 204, 21, 255)),
            ("💬 কমেন্ট করুন", (23, 56, 86, 245), (250, 204, 21, 235), (255, 255, 255, 255)),
            ("↗ শেয়ার করুন", (23, 56, 86, 245), (250, 204, 21, 235), (250, 204, 21, 255)),
        ]
        gap = 20
        total_pills_w = VIDEO_WIDTH - 180
        pill_w = (total_pills_w - gap * 2) // 3
        pill_h = 74
        p_start_x = (VIDEO_WIDTH - total_pills_w) // 2

        for p_i, (p_txt, p_fill, p_out, p_col) in enumerate(pills):
            px0 = p_start_x + p_i * (pill_w + gap)
            px1 = px0 + pill_w
            draw.rounded_rectangle(
                [px0, cta_y, px1, cta_y + pill_h],
                radius=20,
                fill=p_fill,
                outline=p_out,
                width=2,
            )
            pw = measure_mixed_text_width(draw, p_txt, bn_pill, en_pill)
            render_mixed_text_line(draw, px0 + (pill_w - pw) // 2, cta_y + 17, p_txt, bn_pill, en_pill, fill=p_col)

        sub_txt = "সাবস্ক্রাইব করে পাশে থাকুন • ভিজিট করুন"
        sw = measure_mixed_text_width(draw, sub_txt, bn_sub, en_sub)
        sub_y = int(cta_y + 124 + (1.0 - ease_cta) * 14)
        render_mixed_text_line(draw, (VIDEO_WIDTH - sw) // 2, sub_y, sub_txt, bn_sub, en_sub, fill=(226, 232, 240, 255))

        dw, dh = 720, 106
        dom_scale = 0.96 + 0.04 * ease_dom
        cur_dw = int(dw * dom_scale)
        cur_dh = int(dh * dom_scale)
        dx0 = (VIDEO_WIDTH - cur_dw) // 2
        dy0 = int(sub_y + 84 + (1.0 - ease_dom) * 16 + (dh - cur_dh) // 2)

        draw.rounded_rectangle(
            [dx0, dy0, dx0 + cur_dw, dy0 + cur_dh],
            radius=24,
            fill=(250, 204, 21, 255),
        )
        dom_txt = "homebdinfo.com"
        dom_w = measure_mixed_text_width(draw, dom_txt, bn_dom, en_dom)
        render_mixed_text_line(draw, (VIDEO_WIDTH - dom_w) // 2, dy0 + 25, dom_txt, bn_dom, en_dom, fill=(8, 22, 36, 255))

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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
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


def _encode_lightweight_motion_clip(image_path: str, out_mp4: str, duration: int = 10):
    n_frames = max(1, int(round(duration * FPS)))
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-loop", "1", "-i", image_path,
            "-vf", (
                f"scale={VIDEO_WIDTH * 2}:{VIDEO_HEIGHT * 2},"
                f"zoompan=z='1+0.06*(on/{max(1, n_frames)})':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
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
) -> Optional[str]:
    try:
        card_png = os.path.join(work_dir, f"motion_card_{clip_idx}.png")
        clip_mp4 = os.path.join(work_dir, f"motion_card_{clip_idx}.mp4")
        img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (7, 20, 34, 255))
        draw = ImageDraw.Draw(img)

        for gx in range(0, VIDEO_WIDTH, 96):
            draw.line([(gx, 0), (gx, VIDEO_HEIGHT)], fill=(255, 255, 255, 12), width=1)
        for gy in range(0, VIDEO_HEIGHT, 96):
            draw.line([(0, gy), (VIDEO_WIDTH, gy)], fill=(255, 255, 255, 12), width=1)

        badge_w, badge_h = 430, 64
        bx0 = (VIDEO_WIDTH - badge_w) // 2
        by0 = 230
        draw.rounded_rectangle([bx0, by0, bx0 + badge_w, by0 + badge_h], radius=32, fill=(250, 204, 21, 255))
        bn_badge = find_font(BENGALI_FONT_PATHS, 30)
        en_badge = find_font(LATIN_FONT_PATHS, 28)
        b_txt = "মূল তথ্য ও নির্দেশনা"
        bw = measure_mixed_text_width(draw, b_txt, bn_badge, en_badge)
        render_mixed_text_line(draw, (VIDEO_WIDTH - bw) // 2, by0 + 13, b_txt, bn_badge, en_badge, fill=(7, 20, 34, 255))

        card_x0, card_y0 = 60, 330
        card_x1, card_y1 = VIDEO_WIDTH - 60, 960
        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=32,
            fill=(11, 28, 45, 242),
            outline=(250, 204, 21, 220),
            width=3,
        )

        clean_head = re.sub(r"#\w+", "", headline).strip()
        bn_h = find_font(BENGALI_FONT_PATHS, 46)
        en_h = find_font(LATIN_FONT_PATHS, 42)
        h_lines = wrap_text_by_pixel(draw, clean_head, bn_h, en_h, VIDEO_WIDTH - 190)[:2]
        cur_y = card_y0 + 38
        for hl in h_lines:
            hw = measure_mixed_text_width(draw, hl, bn_h, en_h)
            render_mixed_text_line(draw, (VIDEO_WIDTH - hw) // 2, cur_y, hl, bn_h, en_h, fill=(250, 204, 21, 255))
            cur_y += 64

        draw.line([(card_x0 + 40, cur_y + 12), (card_x1 - 40, cur_y + 12)], fill=(255, 255, 255, 65), width=2)
        cur_y += 38

        bn_pt = find_font(BENGALI_FONT_PATHS, 36)
        en_pt = find_font(LATIN_FONT_PATHS, 34)
        valid_pts = [p.strip() for p in bullet_points if len(p.strip()) > 6][:4]
        for pt in valid_pts:
            if cur_y > card_y1 - 80:
                break
            pt_lines = wrap_text_by_pixel(draw, f"• {pt}", bn_pt, en_pt, VIDEO_WIDTH - 200)[:2]
            for pl in pt_lines:
                render_mixed_text_line(draw, card_x0 + 45, cur_y, pl, bn_pt, en_pt, fill=(255, 255, 255, 245))
                cur_y += 48
            cur_y += 14

        img.convert("RGB").save(card_png, "PNG")
        del img, draw
        gc.collect()
        _encode_lightweight_motion_clip(card_png, clip_mp4, duration=10)
        if os.path.exists(card_png):
            os.remove(card_png)
        gc.collect()
        return clip_mp4
    except Exception:
        return None


def capture_site_and_extract_images(post_url: str, out_png: str, work_dir: str) -> Tuple[int, List[str], List[str]]:
    target_url = post_url if post_url.startswith("http") else "https://homebdinfo.com"
    image_urls = []
    article_headings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2.0,
            is_mobile=True,
            has_touch=True,
            user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        )
        page = context.new_page()
        ad_domains = [
            "doubleclick.net", "googlesyndication.com", "googleadservices.com",
            "adservice.google", "adnxs.com", "popads", "monetag", "adsterra", "propellerads"
        ]
        page.route(
            "**/*",
            lambda route: route.abort()
            if any(d in route.request.url.lower() for d in ad_domains)
            else route.continue_(),
        )
        try:
            page.goto(target_url, wait_until="networkidle", timeout=45000)
        except Exception:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1600)

        page.evaluate(
            """() => {
                document.body.style.zoom = "1.08";
                const adSelectors = [
                    'ins.adsbygoogle', '.adsbygoogle', '[id*="google_ads"]', '[id*="ad-"]',
                    '[class*="ad-container"]', '[class*="adsense"]', '[class*="banner-ad"]',
                    '[id*="cookie"]', '[class*="cookie"]', '[class*="popup"]', '[class*="modal"]',
                    'iframe', '[data-ad-client]', '[data-ad-slot]'
                ];
                adSelectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
            }"""
        )
        page.wait_for_timeout(500)

        extracted = page.evaluate(
            """() => {
                const container = document.querySelector('article, .post-body, .entry-content, main') || document.body;
                const imgs = Array.from(container.querySelectorAll('img')).map(img => ({
                    src: img.currentSrc || img.src || img.getAttribute('data-src') || '',
                    w: img.naturalWidth || img.width || 0,
                    h: img.naturalHeight || img.height || 0
                }));
                const headings = Array.from(container.querySelectorAll('h2, h3'))
                    .map(h => (h.innerText || '').trim())
                    .filter(t => t.length > 8 && t.length < 90 && !t.includes('লেখায় যা আছে'));
                return { imgs, headings };
            }"""
        )
        article_headings = extracted.get("headings", [])[:8]
        seen_urls = set()
        for item in extracted.get("imgs", []):
            src = item.get("src", "").strip()
            if not src or src.startswith("data:"):
                continue
            full_src = urljoin(target_url, src)
            lower_src = full_src.lower()
            if any(skip in lower_src for skip in ["logo", "icon", "avatar", "emoji", "banner-ad", "doubleclick"]):
                continue
            if item.get("w", 0) >= 300 and item.get("h", 0) >= 200 and full_src not in seen_urls:
                seen_urls.add(full_src)
                image_urls.append(full_src)

        page.screenshot(path=out_png, full_page=True)
        browser.close()

    img = Image.open(out_png).convert("RGB")
    scale = VIDEO_WIDTH / img.width
    new_h = int(img.height * scale)
    img = img.resize((VIDEO_WIDTH, new_h), Image.Resampling.LANCZOS)
    max_allowed_h = VIDEO_HEIGHT + MAX_SCROLL_PX
    if img.height > max_allowed_h:
        img = img.crop((0, 0, VIDEO_WIDTH, max_allowed_h))
    elif img.height < VIDEO_HEIGHT + 800:
        padded = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT + 800), (245, 247, 250))
        padded.paste(img, (0, 0))
        img = padded

    tint = Image.new("RGBA", img.size, (7, 18, 28, 42))
    img = Image.alpha_composite(img.convert("RGBA"), tint).convert("RGB")
    img.save(out_png, "PNG")
    final_sc_height = img.height
    del img, tint
    gc.collect()

    article_clips = []
    for idx, img_u in enumerate(image_urls[:4]):
        try:
            resp = requests.get(img_u, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and len(resp.content) > 4096:
                raw_img_p = os.path.join(work_dir, f"art_raw_{idx}.png")
                framed_img_p = os.path.join(work_dir, f"art_framed_{idx}.png")
                clip_mp4_p = os.path.join(work_dir, f"art_clip_{idx}.mp4")
                with open(raw_img_p, "wb") as f:
                    f.write(resp.content)
                src_im = Image.open(raw_img_p).convert("RGB")
                if src_im.width < 240 or src_im.height < 130:
                    src_im.close()
                    os.remove(raw_img_p)
                    continue

                bg = src_im.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
                dark_bg = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (7, 20, 34, 165))
                canvas = Image.alpha_composite(bg.convert("RGBA"), dark_bg)
                target_w = VIDEO_WIDTH - 80
                target_h = int(src_im.height * (target_w / src_im.width))
                if target_h > 900:
                    target_h = 900
                    target_w = int(src_im.width * (target_h / src_im.height))
                fg = src_im.resize((target_w, target_h), Image.Resampling.LANCZOS)
                fx = (VIDEO_WIDTH - target_w) // 2
                fy = 350 if target_h <= 560 else 270
                draw_c = ImageDraw.Draw(canvas)
                draw_c.rounded_rectangle(
                    [fx - 8, fy - 8, fx + target_w + 8, fy + target_h + 8],
                    radius=26,
                    fill=(250, 204, 21, 235),
                )
                canvas.paste(fg, (fx, fy))
                canvas.convert("RGB").save(framed_img_p, "PNG")
                del src_im, bg, dark_bg, canvas, fg, draw_c
                gc.collect()

                _encode_lightweight_motion_clip(framed_img_p, clip_mp4_p, duration=10)
                article_clips.append(clip_mp4_p)
                for tmp_p in (raw_img_p, framed_img_p):
                    if os.path.exists(tmp_p):
                        os.remove(tmp_p)
                gc.collect()
        except Exception:
            continue

    return final_sc_height, article_clips, article_headings


def download_relevant_stock_clips(keywords: str, work_dir: str, max_clips: int = 3) -> List[str]:
    if max_clips <= 0:
        return []
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return []
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        return []

    banned_tags = {
        "animal", "monkey", "baboon", "ape", "fire", "flame", "wildfire",
        "dog", "cat", "bird", "insect", "spider", "snake", "skull", "blood"
    }
    downloaded = []
    seen_ids = set()

    for kw in kw_list[:5]:
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
                vid_info = videos.get("medium") or videos.get("small") or videos.get("tiny")
                if vid_info and vid_info.get("url"):
                    raw_p = os.path.join(work_dir, f"raw_stock_{len(downloaded)}.mp4")
                    norm_p = os.path.join(work_dir, f"norm_stock_{len(downloaded)}.mp4")
                    v_data = requests.get(vid_info["url"], timeout=45).content
                    with open(raw_p, "wb") as f:
                        f.write(v_data)
                    del v_data

                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-threads", "2", "-i", raw_p,
                            "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},eq=brightness=-0.06:contrast=1.06",
                            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", str(FPS),
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


def build_subtitle_overlay_track(kinetic_segments: List[Dict], voice_dur: float, work_dir: str) -> str:
    sub_dir = os.path.join(work_dir, "sub_frames")
    os.makedirs(sub_dir, exist_ok=True)
    blank_png = os.path.join(sub_dir, "blank.png")
    Image.new("RGBA", (1040, 340), (0, 0, 0, 0)).save(blank_png, "PNG")

    seg_pngs = []
    for idx, seg in enumerate(kinetic_segments):
        kp = os.path.join(sub_dir, f"sub_{idx}.png")
        create_clean_subtitle_png(seg["text"], seg["is_hook"], kp)
        seg_pngs.append((kp, seg["start"], min(seg["end"], voice_dur)))

    concat_file = os.path.join(sub_dir, "sub_concat.txt")
    cursor = 0.0
    entries = []
    for kp, st, en in seg_pngs:
        if st >= voice_dur or en <= st:
            continue
        if st > cursor + 0.02:
            entries.append((blank_png, st - cursor))
        entries.append((kp, en - st))
        cursor = en
    if cursor < voice_dur:
        entries.append((blank_png, voice_dur - cursor))
    if not entries:
        entries.append((blank_png, voice_dur))

    with open(concat_file, "w", encoding="utf-8") as f:
        for img_path, dur in entries:
            f.write(f"file '{img_path}'\n")
            f.write(f"duration {dur:.3f}\n")
        f.write(f"file '{entries[-1][0]}'\n")

    sub_mov = os.path.join(work_dir, "kinetic_track.mov")
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-f", "concat", "-safe", "0", "-i", concat_file,
            "-vf", "fps=15", "-c:v", "png", sub_mov,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gc.collect()
    return sub_mov


def build_final_video(
    yt_title: str,
    screenshot_png: str,
    screenshot_h: int,
    voice_wav: str,
    kinetic_segments: List[Dict],
    visual_clips: List[str],
    thumbnail_jpg: str,
    output_mp4: str,
    work_dir: str,
):
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", voice_wav,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    voice_dur = float(probe.stdout.strip())
    total_duration = voice_dur + OUTRO_DURATION

    hook_line = kinetic_segments[0]["text"] if kinetic_segments else yt_title
    hook_mp4 = os.path.join(work_dir, "hook_banner.mp4")
    create_hook_banner_clip(yt_title, hook_line, screenshot_png, hook_mp4, duration=min(HOOK_DURATION, voice_dur))

    watermark_png = os.path.join(work_dir, "watermark_pill.png")
    outro_mp4 = os.path.join(work_dir, "outro_animated.mp4")
    create_minimal_watermark_png(watermark_png)
    build_animated_outro_clip(outro_mp4, work_dir, duration=OUTRO_DURATION)
    sub_mov = build_subtitle_overlay_track(kinetic_segments, voice_dur, work_dir)

    base_mp4 = os.path.join(work_dir, "base_layer.mp4")
    base_inputs = ["-loop", "1", "-t", f"{voice_dur:.2f}", "-i", screenshot_png]
    next_i = 1
    hook_idx = None
    if os.path.exists(hook_mp4):
        base_inputs.extend(["-i", hook_mp4])
        hook_idx = next_i
        next_i += 1

    clip_indices = []
    for cp in visual_clips[:6]:
        if os.path.exists(cp):
            base_inputs.extend(["-stream_loop", "-1", "-t", f"{voice_dur:.2f}", "-i", cp])
            clip_indices.append(next_i)
            next_i += 1

    scroll_dist = max(0, screenshot_h - VIDEO_HEIGHT)
    base_filters = [
        f"[0:v]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'min({scroll_dist}, {scroll_dist}*(t/{max(1.0, voice_dur):.2f}))'[site_bg]"
    ]
    last_v = "site_bg"

    if hook_idx is not None:
        hook_end = min(HOOK_DURATION, voice_dur)
        base_filters.append(
            f"[{last_v}][{hook_idx}:v]overlay=0:0:enable='between(t,0,{hook_end:.2f})'[v_hook]"
        )
        last_v = "v_hook"

    if clip_indices and voice_dur > HOOK_DURATION + 4.0:
        cur_t = HOOK_DURATION + 4.5
        seg_len = 8.0
        gap_len = 14.0
        s_i = 0
        while cur_t + 2.0 < voice_dur:
            win_end = min(voice_dur, cur_t + seg_len)
            c_in = clip_indices[s_i % len(clip_indices)]
            out_lbl = f"v_scene_{s_i}"
            base_filters.append(
                f"[{last_v}][{c_in}:v]overlay=0:0:enable='between(t,{cur_t:.2f},{win_end:.2f})'[{out_lbl}]"
            )
            last_v = out_lbl
            s_i += 1
            cur_t += seg_len + gap_len

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
        f"[v_base][2:v]overlay=32:48:enable='between(t,0,{voice_dur:.2f})'[v_wm]",
        f"[v_wm][3:v]overlay=(W-w)/2:1340:enable='between(t,0,{voice_dur:.2f})'[v_sub]",
        f"[v_sub]drawbox=x=0:y=0:w='iw*(t/{total_duration:.2f})':h=8:color=#FACC15@0.90:t=fill[v_prog]",
        f"[v_prog][4:v]overlay=0:0:enable='between(t,{voice_dur:.2f},{total_duration:.2f})'[v_outro]",
        f"[5:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}[thumb_cover]",
        f"[v_outro][thumb_cover]overlay=0:0:enable='between(t,0,{COVER_DURATION:.2f})'[v_final]",
    ]
    if bgm_idx is not None:
        outro_fade_out_start = max(0.0, OUTRO_DURATION - 0.8)
        f_parts.extend([
            f"[{bgm_idx}:a]atrim=0:{voice_dur:.2f},highpass=f=120,lowpass=f=2600,volume={BGM_VOICE_VOLUME},afade=t=in:st=0:d=0.6[bgm_body]",
            f"[{bgm_idx}:a]atrim={voice_dur:.2f}:{total_duration:.2f},asetpts=PTS-STARTPTS,highpass=f=120,lowpass=f=2800,volume={BGM_OUTRO_VOLUME},afade=t=out:st={outro_fade_out_start:.2f}:d=0.8[bgm_outro]",
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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-r", str(FPS),
            "-t", f"{total_duration:.2f}", output_mp4,
        ],
        check=True,
    )
    gc.collect()


def delete_previous_github_release(old_release_id: str):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token or not old_release_id or not str(old_release_id).strip().isdigit():
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.delete(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/{str(old_release_id).strip()}",
            headers=headers,
            timeout=30,
        )
        if resp.status_code in (200, 204):
            print(f"Previous GitHub release {old_release_id} deleted.")
    except Exception as e:
        print(f"Warning: Could not delete previous release {old_release_id}: {e}")


def upload_to_github_release(video_path: str, thumb_path: str, row_num: int, title: str) -> Tuple[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN পাওয়া যায়নি।")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tag_name = f"long-row-{row_num}-{int(time.time())}"
    release_payload = {
        "tag_name": tag_name,
        "name": f"Long Video Row {row_num}: {title[:60]}",
        "body": "Automated Bangla Long Video and thumbnail release.",
        "draft": False,
        "prerelease": False,
    }
    rel_resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases",
        headers=headers,
        json=release_payload,
        timeout=30,
    )
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()
    release_id = str(rel_data["id"])
    upload_url = rel_data["upload_url"].split("{")[0]

    file_name = os.path.basename(video_path)
    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_url}?name={file_name}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
            data=f,
            timeout=180,
        )
    up_resp.raise_for_status()
    download_url = up_resp.json()["browser_download_url"]

    if thumb_path and os.path.exists(thumb_path):
        thumb_name = os.path.basename(thumb_path)
        with open(thumb_path, "rb") as tf:
            t_resp = requests.post(
                f"{upload_url}?name={thumb_name}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                data=tf,
                timeout=60,
            )
        t_resp.raise_for_status()

    return release_id, download_url


def send_telegram_for_approval(video_path: str, thumb_path: str, row_num: int, title: str, post_url: str) -> Tuple[str, str]:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN বা TELEGRAM_CHAT_ID পাওয়া যায়নি।")

    caption = (
        f"🎬 নতুন Bangla Long Video প্রস্তুত!\n\n"
        f"📌 শিরোনাম: {title}\n"
        f"🔗 পোস্ট: {post_url}\n"
        f"📊 Sheet Row: {row_num}\n\n"
        f"💡 Approve দিলে ইনস্ট্যান্ট পোস্ট হবে, Schedule-এ সময় দিন, অথবা প্রয়োজন অনুযায়ী Regenerate বা Reject চাপুন।"
    )
    reply_markup = json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve (Instant)", "callback_data": f"APPROVE_{row_num}"},
                    {"text": "⏰ Schedule", "callback_data": f"SCHEDULE_{row_num}"},
                ],
                [
                    {"text": "🔄 Regenerate", "callback_data": f"REGENERATE_{row_num}"},
                    {"text": "❌ Reject", "callback_data": f"REJECT_{row_num}"},
                ],
            ]
        }
    )

    files = {"video": open(video_path, "rb")}
    if thumb_path and os.path.exists(thumb_path):
        files["thumbnail"] = open(thumb_path, "rb")

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "reply_markup": reply_markup},
            files=files,
            timeout=180,
        )
    finally:
        for f_obj in files.values():
            f_obj.close()

    res_json = resp.json()
    if not res_json.get("ok"):
        raise RuntimeError(f"Telegram sendVideo Error: {res_json}")

    msg = res_json["result"]
    return str(msg.get("video", {}).get("file_id", "")), str(msg["message_id"])


def _pick_col(col_map: Dict[str, int], names: List[str]) -> Optional[int]:
    for n in names:
        if n in col_map:
            return col_map[n]
    return None


def main():
    sheet = get_google_sheet()
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {h: idx + 1 for idx, h in enumerate(headers)}
    all_rows = sheet.get_all_values()

    status_col = _pick_col(col_map, ["Status", "Production Status"])
    if not status_col:
        raise RuntimeError("Sheet-এ Status বা Production Status কলাম পাওয়া যায়নি।")

    target_row = None
    row_data = None
    for r_idx in range(2, len(all_rows) + 1):
        row = all_rows[r_idx - 1]
        status_val = row[status_col - 1].strip() if len(row) >= status_col else ""
        if status_val in ("SCRIPT_READY", "REGENERATE_REQUESTED"):
            target_row = r_idx
            row_data = row
            break

    if not target_row:
        print("SCRIPT_READY বা REGENERATE_REQUESTED স্ট্যাটাসযুক্ত কোনো রো পাওয়া যায়নি।")
        return

    print(f"Processing Long Video Row {target_row}...")
    rel_id_col = _pick_col(col_map, ["Drive File ID", "Release ID"])
    rel_url_col = _pick_col(col_map, ["Drive File URL", "Video URL", "Video Link"])
    tg_file_col = _pick_col(col_map, ["Telegram Video File ID"])
    tg_msg_col = _pick_col(col_map, ["Telegram Message ID"])
    vid_status_col = _pick_col(col_map, ["Video Status"])
    err_col = _pick_col(col_map, ["Last Error", "Notes"])
    title_col = _pick_col(col_map, ["Video Title", "Shorts Title (YT)", "Title"])
    script_col = _pick_col(col_map, ["Bangla Voiceover Script", "Voiceover Script"])
    kw_col = _pick_col(col_map, ["Image Search Keywords (English)", "Primary Keyword"])
    url_col = _pick_col(col_map, ["Post URL", "URL"])

    old_release_id = (
        row_data[rel_id_col - 1].strip()
        if rel_id_col and len(row_data) >= rel_id_col
        else ""
    )
    if old_release_id:
        delete_previous_github_release(old_release_id)

    sheet.update_cell(target_row, status_col, "PROCESSING")
    if vid_status_col:
        sheet.update_cell(target_row, vid_status_col, "GENERATING")

    try:
        yt_title = row_data[title_col - 1].strip() if title_col and len(row_data) >= title_col else "HomeBDInfo Special Guide"
        script = row_data[script_col - 1].strip() if script_col and len(row_data) >= script_col else ""
        keywords = row_data[kw_col - 1].strip() if kw_col and len(row_data) >= kw_col else "bangladesh, office, guide"
        post_url = (
            row_data[url_col - 1].strip()
            if url_col and len(row_data) >= url_col and row_data[url_col - 1].strip()
            else "https://homebdinfo.com"
        )
        if not script:
            raise ValueError("Voiceover Script খালি রয়েছে।")

        with tempfile.TemporaryDirectory() as work_dir:
            screenshot_png = os.path.join(work_dir, "site_mobile.png")
            thumb_jpg = os.path.join(work_dir, f"thumb_long_row_{target_row}.jpg")
            final_mp4 = os.path.join(work_dir, f"bangla_long_row_{target_row}.mp4")

            print("1. Capturing ad-free site screenshot & extracting article graphics...")
            sc_height, article_clips, article_headings = capture_site_and_extract_images(post_url, screenshot_png, work_dir)

            print("2. Generating natural voiceover...")
            voice_wav, kinetic_segments = generate_natural_voiceover(script, work_dir)

            print("3. Preparing visual clips...")
            visual_clips = list(article_clips)
            if len(visual_clips) < 2:
                bullets = article_headings if len(article_headings) >= 2 else split_clauses_for_tts(clean_voiceover_script(script))[1:5]
                bullet_texts = [b["text"] if isinstance(b, dict) else str(b) for b in bullets]
                info_card_clip = create_dynamic_topic_graphic_clip(yt_title, bullet_texts, work_dir, len(visual_clips))
                if info_card_clip:
                    visual_clips.append(info_card_clip)

            needed_stock = max(0, 4 - len(visual_clips))
            if needed_stock > 0:
                stock_clips = download_relevant_stock_clips(keywords, work_dir, max_clips=needed_stock)
                visual_clips.extend(stock_clips)

            print("4. Generating clean branded thumbnail...")
            create_thumbnail_jpg(yt_title, screenshot_png, visual_clips, thumb_jpg, work_dir)

            print("5. Rendering clean final long video...")
            build_final_video(
                yt_title=yt_title,
                screenshot_png=screenshot_png,
                screenshot_h=sc_height,
                voice_wav=voice_wav,
                kinetic_segments=kinetic_segments,
                visual_clips=visual_clips,
                thumbnail_jpg=thumb_jpg,
                output_mp4=final_mp4,
                work_dir=work_dir,
            )

            print("6. Uploading video and thumbnail to GitHub Releases...")
            rel_id, rel_url = upload_to_github_release(final_mp4, thumb_jpg, target_row, yt_title)

            print("7. Sending to Telegram for approval...")
            tg_file_id, tg_msg_id = send_telegram_for_approval(final_mp4, thumb_jpg, target_row, yt_title, post_url)

            if rel_id_col:
                sheet.update_cell(target_row, rel_id_col, rel_id)
            if rel_url_col:
                sheet.update_cell(target_row, rel_url_col, rel_url)
            if tg_file_col:
                sheet.update_cell(target_row, tg_file_col, tg_file_id)
            if tg_msg_col:
                sheet.update_cell(target_row, tg_msg_col, tg_msg_id)
            if vid_status_col:
                sheet.update_cell(target_row, vid_status_col, "COMPLETED")
            sheet.update_cell(target_row, status_col, "PENDING_APPROVAL")
            if err_col:
                sheet.update_cell(target_row, err_col, "")
            print(f"Long Video Row {target_row} completed successfully!")

    except Exception as e:
        err_msg = str(e)[:900]
        print(f"Error on Long Video Row {target_row}: {err_msg}")
        sheet.update_cell(target_row, status_col, "ERROR")
        if vid_status_col:
            sheet.update_cell(target_row, vid_status_col, "FAILED")
        if err_col:
            sheet.update_cell(target_row, err_col, err_msg)
        raise


if __name__ == "__main__":
    main()
