import os
import json
import base64
import requests
from flask import Blueprint, request, jsonify
import anthropic
from googleapiclient.discovery import build
from google.oauth2 import service_account
from datetime import date, datetime, timedelta, timezone
import re
import threading
import html
import hashlib
import hmac
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from PIL import Image, ImageOps

telegram_bp = Blueprint("telegram", __name__)

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
# Comma-separated Telegram chat IDs allowed to use the bot, e.g. "12345678,87654321"
ALLOWED_CHAT_IDS = {
    chat_id.strip() for chat_id in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if chat_id.strip()
}
# Base URL of this app, e.g. "https://my-bot.onrender.com". Render sets RENDER_EXTERNAL_URL itself.
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")

# Secret Telegram sends with every webhook request, derived from the bot token so
# it needs no extra configuration. Only Telegram (which we give it to) knows it.
WEBHOOK_SECRET = (
    hmac.new(TELEGRAM_BOT_TOKEN.encode(), b"telegram-webhook-secret", hashlib.sha256).hexdigest()
    if TELEGRAM_BOT_TOKEN else None
)

# Claude model used to read receipts. Sonnet or Opus read small or crowded text better.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
GST_RATE = Decimal("0.09")
SINGAPORE_TZ = timezone(timedelta(hours=8))  # Singapore has no daylight saving

# Claude image limits: formats it accepts, and images larger than this long edge
# are downscaled by Claude anyway (2576 px on newer models, 1568 px on Haiku 4.5)
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_LONG_EDGE = 2576
MAX_IMAGE_BYTES = 5 * 1024 * 1024
EXIF_ORIENTATION = 0x0112
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024

def get_google_sheets_service():
    """Initialize Google Sheets service with credentials."""
    try:
        # Debug: Check if credentials exist
        if not GOOGLE_SHEETS_CREDENTIALS_JSON:
            print("❌ GOOGLE_SHEETS_CREDENTIALS_JSON environment variable is not set")
            return None
        
        print(f"📋 Credentials JSON length: {len(GOOGLE_SHEETS_CREDENTIALS_JSON)}")
        
        # Try to parse the JSON
        try:
            credentials_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse credentials JSON: {e}")
            print("💡 Make sure your JSON is properly escaped and formatted")
            return None
        
        # Check if required keys exist
        required_keys = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
        missing_keys = [key for key in required_keys if key not in credentials_dict]
        if missing_keys:
            print(f"❌ Missing required keys in credentials: {missing_keys}")
            return None
        
        print("✅ Credentials JSON parsed successfully")
        print(f"📧 Client email: {credentials_dict.get('client_email', 'Not found')}")
        
        # Fix common private key formatting issues
        if 'private_key' in credentials_dict:
            private_key = credentials_dict['private_key']
            
            # Ensure private key has proper line breaks
            if '\\n' in private_key:
                private_key = private_key.replace('\\n', '\n')
                print("🔧 Fixed private key line breaks")
            
            # Fix missing spaces in BEGIN/END markers
            if '-----BEGINPRIVATEKEY-----' in private_key:
                private_key = private_key.replace('-----BEGINPRIVATEKEY-----', '-----BEGIN PRIVATE KEY-----')
                print("🔧 Fixed BEGIN PRIVATE KEY marker")
            
            if '-----ENDPRIVATEKEY-----' in private_key:
                private_key = private_key.replace('-----ENDPRIVATEKEY-----', '-----END PRIVATE KEY-----')
                print("🔧 Fixed END PRIVATE KEY marker")
            
            credentials_dict['private_key'] = private_key
        
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials)
        print("✅ Google Sheets service initialized successfully")
        return service
        
    except Exception as e:
        print(f"❌ Error initializing Google Sheets service: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return None

RECEIPTS_SCHEMA = {
    "type": "object",
    "properties": {
        "receipts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # Separate numbers, so the parts can't come back in the wrong order
                    "day": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "month": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "year": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "company": {"type": "string"},
                    "total_incl_gst": {"type": "number"},
                    "gst_amount": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                },
                "required": ["day", "month", "year", "company", "total_incl_gst", "gst_amount"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["receipts"],
    "additionalProperties": False,
}

EXTRACTION_PROMPT = """This image may contain one or more receipts. Return one entry per separate receipt, in reading order (top to bottom, left to right). If there are no receipts, return an empty list.

For each receipt:
- day, month, year: the transaction date as separate numbers, with a four-digit year. These are Singapore receipts: a date printed like 05/07/26 is day/month/year, and one printed like 2026-07-05 is year-month-day. Use null for all three if no date is visible.
- company: the store or company name as printed.
- total_incl_gst: the final amount charged, including GST and any service charge (not the cash tendered or change given).
- gst_amount: the GST amount if it is printed on the receipt; null if no GST amount is printed. Do not calculate it yourself."""

def to_money(value):
    """Parse a number or string like "$1,234.5" into a Decimal rounded to cents."""
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def build_receipt(date, company, total_incl_gst, gst_amount=None):
    """Build a receipt dict, calculating GST at 9% when it isn't given."""
    total = to_money(total_incl_gst)
    if gst_amount is None:
        total_excl = to_money(total / (1 + GST_RATE))
        gst = total - total_excl
    else:
        gst = to_money(gst_amount)
        total_excl = total - gst
    return {
        "date": normalize_date(date) or str(date),
        "company": str(company).strip() or "Unknown",
        "total_incl_gst": f"{total:.2f}",
        "gst_amount": f"{gst:.2f}",
        "total_excl_gst": f"{total_excl:.2f}",
    }

def receipt_date(day, month, today=None):
    """Build a DD/MM/YY date from the day and month Claude read.

    The year Claude reads is often wrong, so it isn't used: receipts are
    dated this year, or last year if that would put them in the future
    (e.g. a December receipt submitted in January).
    """
    if day is None or month is None:
        return ""
    if month > 12 and day <= 12:
        day, month = month, day
    today = today or datetime.now(SINGAPORE_TZ).date()
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= today:
            return candidate.strftime("%d/%m/%y")
    return ""

def normalize_date(value):
    """Return a date as DD/MM/YY, or None if it isn't a valid day/month/year date."""
    match = re.fullmatch(r"\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})\s*", str(value))
    if not match:
        return None
    day, month, year = match.groups()
    try:
        parsed = datetime.strptime(f"{day}/{month}/{year}", "%d/%m/%Y" if len(year) == 4 else "%d/%m/%y")
    except ValueError:
        return None
    return parsed.strftime("%d/%m/%y")

def prepare_media(file_bytes, media_type):
    """Return (content_block, error_message) for sending a downloaded file to Claude."""
    if media_type == "application/pdf":
        data = base64.b64encode(file_bytes).decode("utf-8")
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": data}}, None

    try:
        image = Image.open(BytesIO(file_bytes))
        # Phones store rotation in EXIF metadata, which Claude doesn't read
        needs_rotation = image.getexif().get(EXIF_ORIENTATION, 1) != 1
        too_large = max(image.size) > MAX_IMAGE_LONG_EDGE or len(file_bytes) > MAX_IMAGE_BYTES
        if needs_rotation or too_large or media_type not in SUPPORTED_IMAGE_TYPES:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((MAX_IMAGE_LONG_EDGE, MAX_IMAGE_LONG_EDGE))
            output = BytesIO()
            image.save(output, format="JPEG", quality=90)
            file_bytes, media_type = output.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"❌ Could not read image: {e}")
        return None, "❌ Could not open this image. Please send it as a JPEG or PNG, or as a normal photo."

    data = base64.b64encode(file_bytes).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}, None

def extract_receipts(media_block):
    """Extract every receipt in an image or PDF using Claude.

    Returns (receipts, error_message). On success error_message is None;
    on failure receipts is None and error_message is shown to the user.
    """
    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY environment variable is not set")
        return None, "❌ The bot's Claude API key is not configured. Please set ANTHROPIC_API_KEY."

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [media_block, {"type": "text", "text": EXTRACTION_PROMPT}]
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": RECEIPTS_SCHEMA}},
        )
        
        content = "".join(block.text for block in response.content if block.type == "text")
        print(f"Claude response (stop_reason={response.stop_reason}): {content}")
        
        if response.stop_reason != "end_turn":
            return None, "❌ Could not read receipt data from this image. Please try again."
        
        receipts = [
            build_receipt(receipt_date(r["day"], r["month"]), r["company"], r["total_incl_gst"], r["gst_amount"])
            for r in json.loads(content)["receipts"]
        ]
        if not receipts:
            return None, "❌ No receipts found in this image. Please ensure the image is clear and contains a receipt."
        return receipts, None
    
    except anthropic.AuthenticationError as e:
        print(f"❌ Claude API authentication error: {e}")
        return None, "❌ The bot's Claude API key is invalid or revoked. Please update ANTHROPIC_API_KEY."
    except anthropic.PermissionDeniedError as e:
        print(f"❌ Claude API permission error: {e}")
        return None, "❌ The bot's Claude API key is not allowed to make this request. Please check the Anthropic Console."
    except anthropic.RateLimitError as e:
        print(f"❌ Claude API rate limit error: {e}")
        return None, "❌ The Claude API is rate limiting the bot. Please try again in a minute."
    except anthropic.BadRequestError as e:
        print(f"❌ Claude API bad request: {e}")
        if "credit balance" in str(e).lower():
            return None, "❌ The bot's Claude API account is out of credits. Please top up in the Anthropic Console."
        return None, "❌ The Claude API rejected the request. Please check the server logs."
    except anthropic.APIStatusError as e:
        print(f"❌ Claude API error ({e.status_code}): {e}")
        return None, "❌ The Claude API is having problems right now. Please try again later."
    except anthropic.APIConnectionError as e:
        print(f"❌ Could not connect to the Claude API: {e}")
        return None, "❌ Could not reach the Claude API. Please try again later."
    except Exception as e:
        print(f"Error extracting receipt data: {e}")
        return None, "❌ Failed to extract receipt data. Please ensure the image is clear and contains a valid receipt."

def save_to_google_sheets(receipt_data):
    """Save receipt data to Google Sheets."""
    try:
        print("🔄 Attempting to initialize Google Sheets service...")
        service = get_google_sheets_service()
        if not service:
            print("❌ Failed to initialize Google Sheets service")
            return False
        
        print("✅ Google Sheets service ready, preparing data...")
        
        # Debug: Check sheet ID
        if not GOOGLE_SHEETS_ID:
            print("❌ GOOGLE_SHEETS_ID environment variable is not set")
            return False
        
        print(f"📊 Using Google Sheets ID: {GOOGLE_SHEETS_ID}")
        
        # Prepare the row data
        row_data = [
            receipt_data["date"],
            receipt_data["company"],
            float(receipt_data["total_excl_gst"]),
            float(receipt_data["gst_amount"]),
            float(receipt_data["total_incl_gst"])
        ]
        
        print(f"📝 Row data prepared: {row_data}")
        
        # Append to the sheet
        body = {
            "values": [row_data]
        }
        
        print("📤 Sending data to Google Sheets...")
        result = service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range= "Sheet 1!A1:E1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()

        
        print(f"✅ Successfully saved to Google Sheets. Result: {result}")
        return True
        
    except Exception as e:
        print(f"❌ Error saving to Google Sheets: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return False

def send_telegram_message(chat_id, text, reply_markup=None):
    """Send a message to Telegram chat with proper error handling."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        
        # Ensure text is not empty and not too long
        if not text or text.strip() == "":
            text = "Empty message"
        
        # Telegram message limit is 4096 characters
        if len(text) > 4000:
            text = text[:4000] + "... (truncated)"
        
        data = {
            "chat_id": str(chat_id),  # Ensure chat_id is string
            "text": text,
            "parse_mode": "HTML"
        }
        
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
            
        print(f"Sending message to chat_id {chat_id}: {text[:100]}...")
        response = requests.post(url, data=data, timeout=10)
        
        if response.status_code != 200:
            print(f"Telegram API error: {response.status_code} - {response.text}")
            return None
        
        result = response.json()
        if not result.get("ok"):
            print(f"Telegram API returned error: {result}")
            return None
            
        return result
        
    except Exception as e:
        print(f"Error sending Telegram message: {e}")
        return None

def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    """Replace the text (and optionally the buttons) of a message the bot sent."""
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText", data=data, timeout=10
    )
    if response.status_code != 200:
        print(f"Failed to edit message: {response.text}")

def download_telegram_file(file_id):
    """Download a file from Telegram and return its bytes."""
    try:
        # Get file path
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
        response = requests.get(url, params={"file_id": file_id}, timeout=10)
        file_info = response.json()
        print(f"Telegram getFile response: {file_info}")
        
        if not file_info.get("ok"):
            return None
            
        file_path = file_info["result"]["file_path"]
        
        # Download the file
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        file_response = requests.get(download_url, timeout=60)
        
        if file_response.status_code == 200:
            return file_response.content
        else:
            return None
            
    except Exception as e:
        # Request errors include the URL, which contains the bot token
        print(f"Error downloading Telegram file: {str(e).replace(TELEGRAM_BOT_TOKEN, '<bot-token>')}")
        return None

RECEIPT_BUTTONS = {
    "inline_keyboard": [
        [
            {"text": "✅ Confirm & Save", "callback_data": "confirm"},
            {"text": "❌ Cancel", "callback_data": "cancel"}
        ]
    ]
}

def format_receipt_message(receipt, index=1, count=1):
    """The confirmation message for one receipt. parse_receipt_message() reads it back."""
    title = f"Receipt {index} of {count}" if count > 1 else "Receipt"
    return f"""📋 <b>{title}</b>

📅 <b>Date:</b> {html.escape(receipt["date"] or "not found")}
🏪 <b>Company:</b> {html.escape(receipt["company"])}
💰 <b>Total (incl GST):</b> ${receipt["total_incl_gst"]}
📊 <b>GST:</b> ${receipt["gst_amount"]}
💵 <b>Amount (excl GST):</b> ${receipt["total_excl_gst"]}

Tap Confirm to save. To fix a value, reply to this message with e.g. <code>total 12.50</code>, <code>date 05/09/26</code>, <code>company ABC Pte Ltd</code> or <code>gst 0</code>."""

def parse_receipt_message(text):
    """Read a receipt back from a confirmation message's plain text, or return None.

    Storing the data in the message itself means pending receipts survive
    server restarts.
    """
    if not text or not text.startswith("📋"):
        return None
    patterns = {
        "date": r"^📅 Date: (.*)$",
        "company": r"^🏪 Company: (.*)$",
        "total_incl_gst": r"^💰 Total \(incl GST\): \$(.*)$",
        # Older messages labelled this "GST (9%)"
        "gst_amount": r"^📊 GST(?: \(9%\))?: \$(.*)$",
    }
    values = {}
    for field, pattern in patterns.items():
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            return None
        values[field] = match.group(1).strip()
    try:
        return build_receipt(**values)
    except (InvalidOperation, ValueError):
        return None

def summarize_receipt(receipt):
    return f'{html.escape(receipt["date"])} · {html.escape(receipt["company"])} · ${receipt["total_incl_gst"]}'

def process_receipt_in_background(chat_id, file_id, media_type):
    """Process a receipt photo or file in a background thread."""
    try:
        print("Background processing started...")
        
        file_bytes = download_telegram_file(file_id)
        if not file_bytes:
            print("❌ Failed to download the file in background.")
            send_telegram_message(chat_id, "❌ Failed to download the image. Please try again.")
            return
        
        media_block, error_message = prepare_media(file_bytes, media_type)
        if not media_block:
            send_telegram_message(chat_id, error_message)
            return
        
        receipts, error_message = extract_receipts(media_block)
        print(f"Claude result in background: {receipts}")
        if not receipts:
            print("❌ Failed to extract receipt data in background.")
            send_telegram_message(chat_id, error_message)
            return
        
        if len(receipts) > 1:
            send_telegram_message(chat_id, f"🧾 Found {len(receipts)} receipts. Please check each one:")
        for index, receipt in enumerate(receipts, start=1):
            send_telegram_message(chat_id, format_receipt_message(receipt, index, len(receipts)), RECEIPT_BUTTONS)
        print("✅ PROCESS COMPLETE in background - User notified successfully")
            
    except Exception as e:
        print(f"❌ ERROR in background processing: {str(e)}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        send_telegram_message(chat_id, "❌ An error occurred while processing your receipt. Please try again.")

def start_receipt_processing(chat_id, file_id, media_type):
    """Acknowledge the upload and process it in the background."""
    print("Sending \"processing\" message to user...")
    if not send_telegram_message(chat_id, "📄 Processing your receipt... Please wait."):
        print("❌ Failed to send processing message")
        return jsonify({"status": "error"})
    
    # Start background processing
    thread = threading.Thread(target=process_receipt_in_background, args=(chat_id, file_id, media_type))
    thread.daemon = True
    thread.start()
    
    print("Webhook returning OK, background processing initiated.")
    return jsonify({"status": "ok"})

CORRECTION_HELP = (
    "✏️ To fix a value, reply to the receipt with one of:\n"
    "<code>total 12.50</code>\n<code>gst 1.03</code> (or <code>gst 0</code> if no GST was charged)\n"
    "<code>date 05/09/26</code>\n<code>company ABC Pte Ltd</code>"
)

def apply_correction(chat_id, message):
    """Update a pending receipt from a reply like "total 12.50"."""
    original = message["reply_to_message"]
    receipt = parse_receipt_message(original.get("text", ""))
    if not receipt:
        send_telegram_message(chat_id, "❌ That message isn't a pending receipt. Reply to a receipt that still has Confirm and Cancel buttons.")
        return
    
    match = re.fullmatch(r"\s*(date|company|total|gst)\s*[:=]?\s*(.+?)\s*", message["text"], re.IGNORECASE | re.DOTALL)
    if not match:
        send_telegram_message(chat_id, CORRECTION_HELP)
        return
    field, value = match.group(1).lower(), match.group(2)
    
    try:
        if field == "date":
            date = normalize_date(value)
            if not date:
                send_telegram_message(chat_id, "❌ Please give the date as DD/MM/YY, e.g. <code>date 05/09/26</code>.")
                return
            receipt["date"] = date
        elif field == "company":
            receipt["company"] = value[:100]
        elif field == "total":
            # A new total means the old GST no longer applies; recalculate at 9%
            receipt = build_receipt(receipt["date"], receipt["company"], value)
        elif field == "gst":
            if to_money(value) > to_money(receipt["total_incl_gst"]):
                send_telegram_message(chat_id, "❌ GST can't be more than the total.")
                return
            receipt = build_receipt(receipt["date"], receipt["company"], receipt["total_incl_gst"], value)
    except (InvalidOperation, ValueError):
        send_telegram_message(chat_id, f"❌ <code>{html.escape(value)}</code> isn't a valid amount.")
        return
    
    title_match = re.match(r"📋 Receipt (\d+) of (\d+)", original["text"])
    index, count = (int(title_match.group(1)), int(title_match.group(2))) if title_match else (1, 1)
    edit_telegram_message(chat_id, original["message_id"], format_receipt_message(receipt, index, count), RECEIPT_BUTTONS)
    send_telegram_message(chat_id, f"✏️ Updated {field}. Check the receipt above and tap Confirm to save.")

@telegram_bp.route("/webhook", methods=["POST"])
def telegram_webhook():
    """Handle incoming Telegram messages."""
    print("=== WEBHOOK RECEIVED ===")
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(received_secret, WEBHOOK_SECRET):
        print("❌ Rejected webhook request without a valid Telegram secret token")
        return jsonify({"status": "forbidden"}), 403

    try:
        update = request.get_json()
        print(f"Request data keys: {list(update.keys()) if update else 'No data'}")
        
        if "message" in update:
            message = update["message"]
            print(f"Message type: {list(message.keys())}")
            chat_id = message["chat"]["id"]
            
            if not is_chat_allowed(chat_id):
                return jsonify({"status": "ok"})
            
            # Handle photo messages (Telegram sends these as compressed JPEGs)
            if "photo" in message:
                print("📸 PHOTO DETECTED - Starting processing...")
                photo = max(message["photo"], key=lambda x: x["file_size"])
                return start_receipt_processing(chat_id, photo["file_id"], "image/jpeg")
            
            # Handle images and PDFs sent as files
            elif "document" in message:
                document = message["document"]
                media_type = document.get("mime_type", "")
                print(f"📎 DOCUMENT DETECTED ({media_type})")
                if not (media_type.startswith("image/") or media_type == "application/pdf"):
                    send_telegram_message(chat_id, "❌ Please send a photo, an image file or a PDF.")
                elif document.get("file_size", 0) > MAX_TELEGRAM_DOWNLOAD_BYTES:
                    send_telegram_message(chat_id, "❌ That file is over 20 MB, which is too large for Telegram bots. Please send a smaller file.")
                else:
                    return start_receipt_processing(chat_id, document["file_id"], media_type)
                
            # Handle text messages
            elif "text" in message:
                text = message["text"].lower()
                print(f"Text message received: {text}")
                
                reply_to = message.get("reply_to_message")
                if reply_to and reply_to.get("from", {}).get("is_bot"):
                    apply_correction(chat_id, message)
                
                elif text == "/start":
                    welcome_text = """🤖 <b>Welcome to Receipt Scanner Bot!</b>

📸 Send me a photo of your receipt (or several receipts in one photo) and I'll:
1. Extract the key information (date, company, amounts)
2. Show you the extracted data for confirmation
3. Save it to your Google Sheets automatically

Just send a photo to get started! 📄"""
                    send_telegram_message(chat_id, welcome_text)
                
                elif text == "/help":
                    help_text = f"""📋 <b>How to use Receipt Scanner Bot:</b>

1. 📸 Take a clear photo of one or more receipts
2. 📤 Send the photo to this bot (images sent as files and PDFs work too)
3. ⏳ Wait for data extraction (usually takes a few seconds)
4. ✅ Review each receipt and tap Confirm
5. 💾 Data will be saved to your Google Sheets

{CORRECTION_HELP}

<b>Tips for best results:</b>
• Ensure good lighting
• Keep the receipt flat
• Make sure all text is visible and readable
• Avoid shadows and glare
• For more than 2-3 receipts, send separate photos, or send the photo as a file for full resolution

<b>Commands:</b>
/start - Welcome message
/help - This help message"""
                    send_telegram_message(chat_id, help_text)
                
                else:
                    send_telegram_message(chat_id, "📸 Please send me a photo of your receipt to process it.")
        
        elif "callback_query" in update:
            print("Callback query detected, processing...")
            return handle_callback_query(update)

        print("=== WEBHOOK COMPLETE ===")
        return jsonify({"status": "ok"})
        
    except Exception as e:
        print(f"❌ ERROR in telegram_webhook: {str(e)}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)})

# Receipts currently being saved, so a double tap on Confirm doesn't save twice
saving_message_ids = set()
saving_lock = threading.Lock()

def handle_callback_query(update):
    """Handle Telegram callback queries (button presses)."""
    try:
        callback_query = update["callback_query"]
        chat_id = callback_query["message"]["chat"]["id"]
        message_id = callback_query["message"]["message_id"]
        data = callback_query["data"]
        
        if not is_chat_allowed(chat_id, notify=False):
            return jsonify({"status": "ok"})
        
        # Answer the callback query (stops the button's loading spinner)
        answer_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        requests.post(answer_url, data={"callback_query_id": callback_query["id"]}, timeout=10)
        
        receipt = parse_receipt_message(callback_query["message"].get("text", ""))
        if not receipt:
            edit_telegram_message(chat_id, message_id, "❌ Could not read this receipt's details. Please send the photo again.")
            return jsonify({"status": "ok"})
        
        # "confirm:<id>" is the format used before receipts were stored in the message
        if data == "confirm" or data.startswith("confirm:"):
            if not normalize_date(receipt["date"]):
                send_telegram_message(chat_id, "📅 This receipt has no valid date. Reply to it with e.g. <code>date 05/09/26</code>, then tap Confirm.")
                return jsonify({"status": "ok"})
            with saving_lock:
                if message_id in saving_message_ids:
                    return jsonify({"status": "ok"})
                saving_message_ids.add(message_id)
            try:
                if save_to_google_sheets(receipt):
                    edit_telegram_message(chat_id, message_id, f"✅ <b>Saved:</b> {summarize_receipt(receipt)}")
                else:
                    send_telegram_message(chat_id, "❌ <b>Failed to save receipt.</b>\n\nPlease check your Google Sheets configuration and tap Confirm again.")
            finally:
                with saving_lock:
                    saving_message_ids.discard(message_id)
            
        elif data == "cancel":
            edit_telegram_message(chat_id, message_id, f"❌ <b>Cancelled:</b> {summarize_receipt(receipt)}")
        
        return jsonify({"status": "ok"})
        
    except Exception as e:
        print(f"Error in handle_callback_query: {e}")
        return jsonify({"status": "error", "message": str(e)})

def register_webhook():
    """Point Telegram at this app's webhook, with the secret token. Called on startup."""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set; cannot register webhook")
        return
    try:
        if WEBHOOK_BASE_URL:
            webhook_url = WEBHOOK_BASE_URL.rstrip("/") + "/telegram/webhook"
        else:
            # Fall back to whatever URL is already registered, just adding the secret
            info = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo", timeout=10
            ).json()
            webhook_url = info.get("result", {}).get("url")
            if not webhook_url:
                print("❌ No webhook URL known. Set WEBHOOK_BASE_URL to this app's URL.")
                return
        
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            data={"url": webhook_url, "secret_token": WEBHOOK_SECRET},
            timeout=10,
        ).json()
        if response.get("ok"):
            print(f"✅ Telegram webhook registered: {webhook_url}")
        else:
            print(f"❌ Failed to register Telegram webhook: {response}")
    except Exception as e:
        # Request errors include the URL, which contains the bot token
        print(f"❌ Error registering Telegram webhook: {str(e).replace(TELEGRAM_BOT_TOKEN, '<bot-token>')}")

def is_chat_allowed(chat_id, notify=True):
    """Only chats listed in ALLOWED_CHAT_IDS may use the bot."""
    if str(chat_id) in ALLOWED_CHAT_IDS:
        return True
    print(f"🚫 Ignoring chat {chat_id}: not in ALLOWED_CHAT_IDS")
    if notify:
        send_telegram_message(
            chat_id,
            f"🔒 This bot is private.\n\nYour chat ID is <code>{chat_id}</code>. "
            "If this is your bot, add it to ALLOWED_CHAT_IDS in your hosting settings."
        )
    return False

@telegram_bp.route("/webhook_info", methods=["GET"])
def webhook_info():
    """Get current webhook information."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
        response = requests.get(url, timeout=10)
        return jsonify(response.json())
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500