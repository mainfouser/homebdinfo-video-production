import os
import sys
import json
import requests

# Environment Variables
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
TOPIC_ID = os.environ.get("TOPIC_ID", "N/A")
MAX_VIDEOS = os.environ.get("MAX_VIDEOS", "1")
VOICE_NAME = os.environ.get("VOICE_NAME", "bn-BD-NabanitaNeural")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(message: str):
    """
    Telegram-এ নোটিফিকেশন পাঠানোর ফাংশন।
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print("Telegram message sent successfully.")
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

def main():
    print(f"Starting long video generation for Topic ID: {TOPIC_ID}")
    
    # -------------------------------------------------------------
    # আপনার ভিডিও তৈরির মূল লজিক/ফাংশনসমূহ এখানে থাকবে
    # -------------------------------------------------------------
    
    # উদাহরণস্বরূপ ভিডিও আউটপুট ফোল্ডার ও ফাইল চেকিং
    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)
    
    # ভিডিও তৈরির প্রক্রিয়া সম্পন্ন হলে টেলিগ্রাম মেসেজ পাঠানো (Multi-line String)
    telegram_text = (
        f"🎬 <b>লং ভিডিও প্রসেসিং সম্পন্ন!</b>\n\n"
        f"📌 <b>Topic ID:</b> {TOPIC_ID}\n"
        f"🎙️ <b>Voice:</b> {VOICE_NAME}\n"
        f"📊 <b>Max Videos:</b> {MAX_VIDEOS}\n"
        f"✅ <b>Status:</b> Success"
    )
    
    send_telegram_message(telegram_text)

if __name__ == "__main__":
    main()
