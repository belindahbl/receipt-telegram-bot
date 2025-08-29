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

telegram_bp = Blueprint("telegram", __name__)

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY

def get_google_sheets_service():
    """Initialize Google Sheets service with credentials."""
    try:
        credentials_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials)
        return service
    except Exception as e:
        print(f"Error initializing Google Sheets service: {e}")
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
        service = get_google_sheets_service()
        if not service:
            return False
            
        # Prepare the row data
        row_data = [
            receipt_data["date"],
            receipt_data["company"],
            float(receipt_data["total_excl_gst"]),
            float(receipt_data["gst_amount"]),
            float(receipt_data["total_incl_gst"])
        ]
        
        # Append to the sheet
        body = {
            "values": [row_data]
        }
        
        result = service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range="Sheet1!A:E",
            valueInputOption="RAW",
            body=body
        ).execute()
        
        return True
        
    except Exception as e:
        print(f"Error saving to Google Sheets: {e}")
        return False

def send_telegram_message(chat_id, text, reply_markup=None):
    """Send a message to Telegram chat."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
            
        response = requests.post(url, data=data)
        return response.json()
    except Exception as e:
        print(f"Error sending Telegram message: {e}")
        return None

def download_telegram_photo(file_id):
    """Download photo from Telegram and return as base64."""
    try:
        # Get file path
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
        response = requests.get(url, params={"file_id": file_id})
        file_info = response.json()
        print(f"Telegram getFile response: {file_info}") # Added logging
        
        if not file_info.get("ok"):
            return None
            
        file_path = file_info["result"]["file_path"]
        
        # Download the file
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        file_response = requests.get(download_url)
        
        if file_response.status_code == 200:
            return base64.b64encode(file_response.content).decode("utf-8")
        else:
            return None
            
    except Exception as e:
        print(f"Error downloading Telegram photo: {e}")
        return None

@telegram_bp.route("/webhook", methods=["POST"])
def telegram_webhook():
    """Handle incoming Telegram messages."""
    print("=== WEBHOOK RECEIVED ===")
    try:
        update = request.get_json()
        print(f"Request data keys: {list(update.keys()) if update else \"No data\"}")
        
        if "message" not in update:
            print("No message in update, returning ok.")
            return jsonify({"status": "ok"})
            
        message = update["message"]
        print(f"Message type: {list(message.keys())}")
        chat_id = message["chat"]["id"]
        
        # Handle photo messages
        if "photo" in message:
            print("📸 PHOTO DETECTED - Starting processing...")
            # Get the largest photo
            photo = max(message["photo"], key=lambda x: x["file_size"])
            file_id = photo["file_id"]
            
            # Send processing message
            print("Sending \"processing\" message to user...")
            send_telegram_message(chat_id, "📄 Processing your receipt... Please wait.")
            
            print("🤖 CALLING OPENAI...")
            # Download and process the photo
            image_base64 = download_telegram_photo(file_id)
            if not image_base64:
                print("❌ Failed to download the image.")
                send_telegram_message(chat_id, "❌ Failed to download the image. Please try again.")
                return jsonify({"status": "ok"})
            
            # Extract receipt data
            receipt_data = extract_receipt_data(image_base64)
            print(f"OpenAI result: {receipt_data}")
            if not receipt_data:
                print("❌ Failed to extract receipt data.")
                send_telegram_message(chat_id, "❌ Failed to extract receipt data. Please ensure the image is clear and contains a valid receipt.")
                return jsonify({"status": "ok"})
            
            print("📊 WRITING TO GOOGLE SHEETS...")
            # Format confirmation message
            confirmation_text = f"""📋 <b>Receipt Data Extracted:</b>\n\n📅 <b>Date:</b> {receipt_data["date"]}\n🏪 <b>Company:</b> {receipt_data["company"]}\n💰 <b>Total (incl GST):</b> ${receipt_data["total_incl_gst"]}\n📊 <b>GST (9%):</b> ${receipt_data["gst_amount"]}\n💵 <b>Amount (excl GST):</b> ${receipt_data["total_excl_gst"]}\n\nPlease confirm if this data is correct:"""
            
            # Create inline keyboard for confirmation
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Confirm & Save", "callback_data": f"confirm:{json.dumps(receipt_data)}"},
                        {"text": "❌ Cancel", "callback_data": "cancel"}
                    ]
                ]
            }
            
            send_telegram_message(chat_id, confirmation_text, reply_markup)
            print("✅ PROCESS COMPLETE - Notifying user...")
            
        # Handle text messages
        elif "text" in message:
            text = message["text"].lower()
            print(f"Text message received: {text}")
            
            if text == "/start":
                welcome_text = """🤖 <b>Welcome to Receipt Scanner Bot!</b>\n\n📸 Send me a photo of your receipt and I\"ll:\n1. Extract the key information (date, company, amounts)\n2. Show you the extracted data for confirmation\n3. Save it to your Google Sheets automatically\n\nJust send a photo to get started! 📄"""
                send_telegram_message(chat_id, welcome_text)
            
            elif text == "/help":
                help_text = """📋 <b>How to use Receipt Scanner Bot:</b>\n\n1. 📸 Take a clear photo of your receipt\n2. 📤 Send the photo to this bot\n3. ⏳ Wait for data extraction (usually takes a few seconds)\n4. ✅ Review and confirm the extracted data\n5. 💾 Data will be saved to your Google Sheets\n\n<b>Tips for best results:</b>\n• Ensure good lighting\n• Keep the receipt flat\n• Make sure all text is visible and readable\n• Avoid shadows and glare\n\n<b>Commands:</b>\n/start - Welcome message\n/help - This help message"""
                send_telegram_message(chat_id, help_text)
            
            else:
                send_telegram_message(chat_id, "📸 Please send me a photo of your receipt to process it.")
        
        print("=== WEBHOOK COMPLETE ===")
        return jsonify({"status": "ok"})
        
    except Exception as e:
        print(f"❌ ERROR in telegram_webhook: {str(e)}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)})

@telegram_bp.route("/callback", methods=["POST"])
def telegram_callback():
    """Handle Telegram callback queries (button presses)."""
    try:
        update = request.get_json()
        
        if "callback_query" not in update:
            return jsonify({"status": "ok"})
            
        callback_query = update["callback_query"]
        chat_id = callback_query["message"]["chat"]["id"]
        message_id = callback_query["message"]["message_id"]
        data = callback_query["data"]
        
        if data.startswith("confirm:"):
            # Extract receipt data from callback data
            receipt_json = data[8:]  # Remove "confirm:" prefix
            receipt_data = json.loads(receipt_json)
            
            # Save to Google Sheets
            success = save_to_google_sheets(receipt_data)
            
            if success:
                response_text = "✅ <b>Receipt saved successfully!</b>\n\nYour data has been added to the Google Sheets."
            else:
                response_text = "❌ <b>Failed to save receipt.</b>\n\nPlease check your Google Sheets configuration and try again."
            
            # Edit the original message to remove buttons
            edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
            edit_data = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": response_text,
                "parse_mode": "HTML"
            }
            requests.post(edit_url, data=edit_data)
            
        elif data == "cancel":
            response_text = "❌ <b>Receipt processing cancelled.</b>\n\nSend another photo to try again."
            
            # Edit the original message to remove buttons
            edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
            edit_data = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": response_text,
                "parse_mode": "HTML"
            }
            requests.post(edit_url, data=edit_data)
        
        # Answer the callback query
        answer_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        answer_data = {"callback_query_id": callback_query["id"]}
        requests.post(answer_url, data=answer_data)
        
        return jsonify({"status": "ok"})
        
    except Exception as e:
        print(f"Error in telegram_callback: {e}")
        return jsonify({"status": "error", "message": str(e)})

@telegram_bp.route("/set_webhook", methods=["POST"])
def set_webhook():
    """Set the webhook URL for the Telegram bot."""
    try:
        data = request.get_json()
        webhook_url = data.get("webhook_url")
        
        if not webhook_url:
            return jsonify({"error": "webhook_url is required"}), 400
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
        response = requests.post(url, data={"url": webhook_url})
        
        return jsonify(response.json())
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@telegram_bp.route("/webhook_info", methods=["GET"])
def webhook_info():
    """Get current webhook information."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
        response = requests.get(url)
        return jsonify(response.json())
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500