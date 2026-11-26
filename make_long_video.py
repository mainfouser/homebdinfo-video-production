import os
import json
import random
import time
import asyncio
import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------
# 1. ENVIRONMENT & CONFIGURATION
# ---------------------------------------------------------
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.environ.get("SHEET_ID") or os.environ.get("SPREADSHEET_ID")
TOPIC_ID = os.environ.get("TOPIC_ID", "").strip()
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "1"))
VOICE_NAME = os.environ.get("VOICE_NAME", "bn-BD-NabanitaNeural")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------
# 2. HELPER FUNCTIONS
# ---------------------------------------------------------
def send_telegram(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"Telegram Notification Error: {e}")

def get_google_sheet():
    if not GOOGLE_CREDENTIALS or not SHEET_ID:
        raise RuntimeError("GOOGLE_CREDENTIALS (বা GOOGLE_SERVICE_ACCOUNT_JSON) এবং SHEET_ID (বা SPREADSHEET_ID) সেট করা নেই।")
    
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    return spreadsheet

async def generate_audio(text, output_audio_path, voice=VOICE_NAME):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_audio_path)

def render_html_to_image(html_content, output_image_path, width=1920, height=1080):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_content(html_content)
        page.screenshot(path=output_image_path)
        browser.close()

# ---------------------------------------------------------
# 3. MAIN WORKFLOW
# ---------------------------------------------------------
def main():
    print("Starting Video Production Pipeline...")
    try:
        spreadsheet = get_google_sheet()
        print(f"Successfully connected to Google Sheet: {spreadsheet.title}")
    except Exception as e:
        error_msg = f"❌ <b>Google Sheet Error:</b> {str(e)}"
        print(error_msg)
        send_telegram(error_msg)
        raise e

    # Add your script processing and FFmpeg long video generation logic here

if __name__ == "__main__":
    main()
