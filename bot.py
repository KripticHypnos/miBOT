import logging
import os
import re
import threading
import string
import time
import json
from datetime import datetime

from flask import Flask
from threading import Thread

import requests
from dotenv import load_dotenv

from telegram import (
    ReplyKeyboardRemove,
    Update,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import gspread
from google.oauth2.service_account import Credentials

# =========================================================
# FLASK KEEPALIVE
# =========================================================

app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot running"

@app_flask.route("/health")
def health():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )

def keep_alive():
    thread = Thread(target=run_web, daemon=True)
    thread.start()

# =========================================================
# HEARTBEAT
# =========================================================

def heartbeat():

    while True:
        print(f"[HEARTBEAT] Bot alive at {datetime.now()}")
        time.sleep(300)

# =========================================================
# BASE36
# =========================================================

BASE36 = string.digits + string.ascii_lowercase

def to_base36(num: int) -> str:

    if num == 0:
        return "0"

    chars = []

    while num:
        num, i = divmod(num, 36)
        chars.append(BASE36[i])

    return ''.join(reversed(chars))

def generate_base36_id(user_id: str, date: str, vehicle: str) -> str:

    raw = f"{user_id}{date}{vehicle}{int(time.time() * 1000)}"

    numeric_seed = abs(hash(raw))

    return to_base36(numeric_seed)

# =========================================================
# ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing")

google_creds_raw = os.getenv("GOOGLE_CREDS")

if not google_creds_raw:
    raise Exception("GOOGLE_CREDS missing")

# =========================================================
# GOOGLE SHEETS
# =========================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds_json = json.loads(google_creds_raw)

creds = Credentials.from_service_account_info(
    creds_json,
    scopes=SCOPES
)

gc = gspread.authorize(creds)

sheet = gc.open("MileageBotDB")

users_sheet = sheet.worksheet("users")
logs_sheet = sheet.worksheet("logs")

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# =========================================================
# STORAGE
# =========================================================

registered_users = {}
mileage_logs = []

# =========================================================
# LOADERS
# =========================================================

def load_registered_users():

    users = {}

    try:

        data = users_sheet.get_all_records()

        for row in data:
            users[int(row["telegram_id"])] = row["user_id"]

    except Exception as e:
        print("LOAD USERS ERROR:", e)

    return users

def load_logs():

    logs = []

    try:

        rows = logs_sheet.get_all_records()

        for row in rows:

            logs.append({
                "log_id": row["log_id"],
                "telegram_id": int(row["telegram_id"]),
                "user_id": row["user_id"],
                "date": row["date"],
                "vehicle_number": str(row["vehicle_number"]),
                "vehicle_class": row["vehicle_class"],
                "start": int(row["start"]),
                "end": int(row["end"]),
                "total": int(row["total"]),
                "reason": row["reason"],
                "timestamp": row["timestamp"],
            })

    except Exception as e:
        print("LOAD LOGS ERROR:", e)

    return logs

# =========================================================
# SAVE HELPERS
# =========================================================

def save_user(telegram_id, user_id):

    try:

        users_sheet.append_row([
            telegram_id,
            user_id
        ])

    except Exception as e:
        print("SAVE USER ERROR:", e)

def save_log(log_entry):

    try:

        logs_sheet.append_row([
            log_entry["log_id"],
            log_entry["telegram_id"],
            log_entry["user_id"],
            log_entry["date"],
            log_entry["vehicle_number"],
            log_entry["vehicle_class"],
            log_entry["start"],
            log_entry["end"],
            log_entry["total"],
            log_entry["reason"],
            log_entry["timestamp"]
        ])

    except Exception as e:
        print("SAVE LOG ERROR:", e)

# =========================================================
# STATES
# =========================================================

REGISTER = 0

DATE = 1
VEHICLE_NUMBER = 2
START_ODOMETER = 3
END_ODOMETER = 4
REASON = 5

# =========================================================
# VALIDATION
# =========================================================

def validate_user_id(user_id: str) -> bool:
    return bool(re.match(r"^\d{3}[A-Za-z]$", user_id))

def validate_date(date_str: str) -> bool:

    if not re.match(r"^\d{6}$", date_str):
        return False

    try:
        datetime.strptime(date_str, "%d%m%y")
        return True

    except ValueError:
        return False

def validate_vehicle_number(vn: str) -> bool:
    return bool(re.match(r"^\d{5}$", vn))

# =========================================================
# VEHICLE CLASS
# =========================================================

def classify_vehicle(c: int) -> str:

    if 11000 < c < 21999:
        return "Class 4"

    if 32000 < c < 34645:
        return "Class 3"

    if 34645 < c < 35999:
        return "Class 4"

    if 36000 < c < 37999:
        return "Class 3"

    if 41000 < c < 41999:
        return "Class 3"

    if 46000 < c < 46999:
        return "Class 4"

    if 59000 < c < 99999:
        return "Class 3"

    return "Unknown"

# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Mileage Bot Running\n\n"
        "/register\n"
        "/log\n"
        "/help"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "/register\n"
        "/log\n"
        "/help"
    )

# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    print("ERROR:", context.error)

# =========================================================
# UNKNOWN COMMAND
# =========================================================

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Unknown command.\nUse /help"
    )

# =========================================================
# MAIN
# =========================================================

def main():

    global registered_users
    global mileage_logs

    print("Loading database...")

    registered_users = load_registered_users()
    mileage_logs = load_logs()

    print("Starting Flask keepalive...")
    keep_alive()

    print("Starting heartbeat thread...")
    threading.Thread(
        target=heartbeat,
        daemon=True
    ).start()

    print("Starting Telegram bot...")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command
        )
    )

    app.add_error_handler(error_handler)

    print("Bot polling started.")

    app.run_polling(
        poll_interval=2,
        timeout=30,
        drop_pending_updates=False
    )

if __name__ == "__main__":
    main()