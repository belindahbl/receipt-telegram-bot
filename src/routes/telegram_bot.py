import os
import json
import base64
import requests
from flask import Blueprint, request, jsonify
import openai
from googleapiclient.discovery import build
from google.oauth2 import service_account
from datetime import datetime
import re
import threading
import html
import hashlib

telegram_bp = Blueprint("telegram", __name__)

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY

# Store receipt data temporarily (in production, use Redis or database)
temp_receipt_storage = {}

def get_google_sheets_service():
    """Initialize Google Sheets service with credentials."""
    try:
        # Debug: Check if credentials exist
        if not GOOGLE_SHEETS_CREDENTIALS_JSON:
            print("❌ GOOGLE_SHEETS_CREDENTIALS_JSON environment variable is not set")
            return None
        
        print(f"📋 Credentials JSON length: {len(GOOGLE_SHEETS_CREDENTIALS_JSON)}")
        print(f"📋 First 100 chars: {GOOGLE_SHEETS_CREDENTIALS_JSON[:100]}...")
        
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

def extract_receipt_data(image_base64):
    """Extract receipt data using OpenAI Vision API."""
    try:
        client = openai.OpenAI()
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Please extract the following information from this receipt image:
                            1. Date (convert to DD/MM/YY format)
                            2. Company/Store name
                            3. Total amount (including GST)
                            4. GST amount (if shown, otherwise calculate 9% of pre-GST amount)
                            
                            Return the data in this exact JSON format:
                            {
                                "date": "DD/MM/YY",
                                "company": "Company Name",
                                "total_incl_gst": "X.XX",
                                "gst_amount": "X.XX",
                                "total_excl_gst": "X.XX"
                            }
                            
                            For Singapore receipts, GST is typically 9%. Calculate GST excluded amount as: total_incl_gst / 1.09
                            If GST amount is not shown, calculate it as: total_excl_gst * 0.09
                            """
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=500
        )
        
        content = response.choices[0].message.content
        
        # Extract JSON from the response
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            receipt_data = json.loads(json_match.group())
            return receipt_data
        else:
            return None
            
    except Exception as e:
        print(f"Error extracting receipt data: {e}")
        return None

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
            range_ = "Sheet 1!A1:E1",
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

def download_telegram_photo(file_id):
    """Download photo from Telegram and return as base64."""
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
        file_response = requests.get(download_url, timeout=30)
        
        if file_response.status_code == 200:
            return base64.b64encode(file_response.content).decode("utf-8")
        else:
            return None
            
    except Exception as e:
        print(f"Error downloading Telegram photo: {e}")
        return None

def process_receipt_in_background(chat_id, file_id):
    """Process receipt in background thread."""
    try:
        print("Background processing started...")
        
        # Download image
        image_base64 = download_telegram_photo(file_id)
        if not image_base64:
            print("❌ Failed to download the image in background.")
            send_telegram_message(chat_id, "❌ Failed to download the image. Please try again.")
            return
        
        # Extract receipt data
        receipt_data = extract_receipt_data(image_base64)
        print(f"OpenAI result in background: {receipt_data}")
        if not receipt_data:
            print("❌ Failed to extract receipt data in background.")
            send_telegram_message(chat_id, "❌ Failed to extract receipt data. Please ensure the image is clear and contains a valid receipt.")
            return
        
        print("📊 Preparing confirmation message...")
        
        # Store receipt data with a hash key (instead of embedding in callback)
        receipt_hash = hashlib.md5(json.dumps(receipt_data, sort_keys=True).encode()).hexdigest()[:8]
        temp_receipt_storage[receipt_hash] = receipt_data
        
        # Clean up the text - escape HTML entities and fix formatting
        company_name = html.escape(str(receipt_data.get("company", "Unknown")))
        date_str = html.escape(str(receipt_data.get("date", "Unknown")))
        total_incl = html.escape(str(receipt_data.get("total_incl_gst", "0.00")))
        gst_amount = html.escape(str(receipt_data.get("gst_amount", "0.00")))
        total_excl = html.escape(str(receipt_data.get("total_excl_gst", "0.00")))
        
        confirmation_text = f"""📋 <b>Receipt Data Extracted:</b>

📅 <b>Date:</b> {date_str}
🏪 <b>Company:</b> {company_name}
💰 <b>Total (incl GST):</b> ${total_incl}
📊 <b>GST (9%):</b> ${gst_amount}
💵 <b>Amount (excl GST):</b> ${total_excl}

Please confirm if this data is correct:"""
        
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm & Save", "callback_data": f"confirm:{receipt_hash}"},
                    {"text": "❌ Cancel", "callback_data": "cancel"}
                ]
            ]
        }
        
        result = send_telegram_message(chat_id, confirmation_text, reply_markup)
        if result:
            print("✅ PROCESS COMPLETE in background - User notified successfully")
        else:
            print("❌ Failed to send confirmation message")
            
    except Exception as e:
        print(f"❌ ERROR in background processing: {str(e)}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        send_telegram_message(chat_id, "❌ An error occurred while processing your receipt. Please try again.")

@telegram_bp.route("/webhook", methods=["POST"])
def telegram_webhook():
    """Handle incoming Telegram messages."""
    print("=== WEBHOOK RECEIVED ===")
    try:
        update = request.get_json()
        print(f"Request data keys: {list(update.keys()) if update else 'No data'}")
        
        if "message" in update:
            message = update["message"]
            print(f"Message type: {list(message.keys())}")
            chat_id = message["chat"]["id"]
            
            # Handle photo messages
            if "photo" in message:
                print("📸 PHOTO DETECTED - Starting processing...")
                photo = max(message["photo"], key=lambda x: x["file_size"])
                file_id = photo["file_id"]
                
                print("Sending \"processing\" message to user...")
                result = send_telegram_message(chat_id, "📄 Processing your receipt... Please wait.")
                
                if not result:
                    print("❌ Failed to send processing message")
                    return jsonify({"status": "error"})
                
                # Start background processing
                thread = threading.Thread(target=process_receipt_in_background, args=(chat_id, file_id))
                thread.daemon = True
                thread.start()
                
                print("Webhook returning OK, background processing initiated.")
                return jsonify({"status": "ok"})
                
            # Handle text messages
            elif "text" in message:
                text = message["text"].lower()
                print(f"Text message received: {text}")
                
                if text == "/start":
                    welcome_text = """🤖 <b>Welcome to Receipt Scanner Bot!</b>

📸 Send me a photo of your receipt and I'll:
1. Extract the key information (date, company, amounts)
2. Show you the extracted data for confirmation
3. Save it to your Google Sheets automatically

Just send a photo to get started! 📄"""
                    send_telegram_message(chat_id, welcome_text)
                
                elif text == "/help":
                    help_text = """📋 <b>How to use Receipt Scanner Bot:</b>

1. 📸 Take a clear photo of your receipt
2. 📤 Send the photo to this bot
3. ⏳ Wait for data extraction (usually takes a few seconds)
4. ✅ Review and confirm the extracted data
5. 💾 Data will be saved to your Google Sheets

<b>Tips for best results:</b>
• Ensure good lighting
• Keep the receipt flat
• Make sure all text is visible and readable
• Avoid shadows and glare

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

def handle_callback_query(update):
    """Handle Telegram callback queries (button presses)."""
    try:
        callback_query = update["callback_query"]
        chat_id = callback_query["message"]["chat"]["id"]
        message_id = callback_query["message"]["message_id"]
        data = callback_query["data"]
        
        if data.startswith("confirm:"):
            # Extract receipt hash from callback data
            receipt_hash = data[8:]  # Remove "confirm:" prefix
            receipt_data = temp_receipt_storage.get(receipt_hash)
            
            if not receipt_data:
                response_text = "❌ <b>Error:</b> Receipt data expired. Please try again."
            else:
                # Save to Google Sheets
                success = save_to_google_sheets(receipt_data)
                
                if success:
                    response_text = "✅ <b>Receipt saved successfully!</b>\n\nYour data has been added to the Google Sheets."
                    # Clean up temporary storage
                    temp_receipt_storage.pop(receipt_hash, None)
                else:
                    response_text = "❌ <b>Failed to save receipt.</b>\n\nPlease check your Google Sheets configuration and try again."
            
        elif data == "cancel":
            response_text = "❌ <b>Receipt processing cancelled.</b>\n\nSend another photo to try again."
        else:
            response_text = "❌ Unknown action."
        
        # Edit the original message to remove buttons
        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        edit_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": response_text,
            "parse_mode": "HTML"
        }
        edit_response = requests.post(edit_url, data=edit_data, timeout=10)
        
        if edit_response.status_code != 200:
            print(f"Failed to edit message: {edit_response.text}")
        
        # Answer the callback query
        answer_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        answer_data = {"callback_query_id": callback_query["id"]}
        requests.post(answer_url, data=answer_data, timeout=10)
        
        return jsonify({"status": "ok"})
        
    except Exception as e:
        print(f"Error in handle_callback_query: {e}")
        return jsonify({"status": "error", "message": str(e)})

@telegram_bp.route("/callback", methods=["POST"])
def telegram_callback():
    """Handle Telegram callback queries (button presses) - legacy endpoint."""
    return handle_callback_query(request.get_json())

@telegram_bp.route("/set_webhook", methods=["POST"])
def set_webhook():
    """Set the webhook URL for the Telegram bot."""
    try:
        data = request.get_json()
        webhook_url = data.get("webhook_url")
        
        if not webhook_url:
            return jsonify({"error": "webhook_url is required"}), 400
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
        response = requests.post(url, data={"url": webhook_url}, timeout=10)
        
        return jsonify(response.json())
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@telegram_bp.route("/webhook_info", methods=["GET"])
def webhook_info():
    """Get current webhook information."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
        response = requests.get(url, timeout=10)
        return jsonify(response.json())
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500