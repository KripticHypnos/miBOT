# STABLE VER: 1115AM 280526
import logging
import os
import re
import sys
from datetime import datetime
from flask import Flask
from threading import Thread
import requests
import threading
import string
import time
import asyncio
import multiprocessing
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from datetime import datetime, timezone, timedelta
import signal
import json
import traceback

app_flask = Flask(__name__)


@app_flask.route('/')
def home():
    return "Bot running"


def run_web():
    app_flask.run(
        host='0.0.0.0',
        port=10000,
        debug=False,
        use_reloader=False
    )


def heartbeat():
    while True:
        try:
            requests.get("http://localhost:10000/", timeout=10)
            print(f"heartbeat: {datetime.now(SGT)}")
        except Exception as e:
            print(f"Heartbeat failed: {e}")
        time.sleep(240)


SGT = timezone(timedelta(hours=8))


def handle_sigterm(signum, frame):
    print("SIGTERM received - ignoring to keep bot alive")


signal.signal(signal.SIGTERM, handle_sigterm)

# =========================================================
# BASE 36 ENCODING
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
    """
    Create compact unique ID using Base36 encoding.
    """
    raw = f"{user_id}{date}{vehicle}{int(time.time() * 1000)}"
    numeric_seed = abs(hash(raw))
    return to_base36(numeric_seed)


# =========================================================
# ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

# =========================================================
# GOOGLE SHEETS
# =========================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

google_creds = os.getenv("GOOGLE_CREDS")

if not google_creds:
    raise ValueError("GOOGLE_CREDS missing")

creds_json = json.loads(google_creds)

creds = Credentials.from_service_account_info(
    creds_json,
    scopes=SCOPES
)

authed_session = AuthorizedSession(creds)
authed_session.configure_mtls_channel = False

client = gspread.Client(auth=creds, session=authed_session)

sheet = client.open("MileageBotDB")

users_sheet = sheet.worksheet("users")
logs_sheet = sheet.worksheet("logs")

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# =========================================================
# STORAGE (IN-MEMORY)
# =========================================================

registered_users = {}  # telegram_id -> user_id
mileage_logs = []  # list of logs


# =========================================================
# DATABASE HELPERS
# =========================================================

def load_registered_users():
    try:
        data = users_sheet.get_all_records()
    except Exception as e:
        print(f"Failed to load users: {e}")
        return {}

    users = {}
    for row in data:
        if not str(row.get("telegram_id", "")).strip():
            continue
        try:
            users[int(row["telegram_id"])] = row["user_id"]
        except (ValueError, KeyError):
            continue

    return users


def save_user(telegram_id, user_id):
    try:
        users_sheet.append_row([
            telegram_id,
            user_id
        ])
    except Exception as e:
        print("SAVE FAILED")


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


def load_logs():
    try:
        rows = logs_sheet.get_all_records()
    except Exception as e:
        print("SAVE LOG ERROR:", e)
        return []

    logs = []
    for row in rows:

        raw_date = str(row.get("date", "")).strip()
        formatted_date = raw_date.zfill(6) if raw_date.isdigit() else raw_date

        logs.append({
            "log_id": row["log_id"],
            "telegram_id": int(row["telegram_id"]),
            "user_id": row["user_id"],
            "date": str(row["date"]).strip(),
            "vehicle_number": str(row["vehicle_number"]),
            "vehicle_class": row["vehicle_class"],
            "start": int(row["start"]),
            "end": int(row["end"]),
            "total": int(row["total"]),
            "reason": row["reason"],
            "timestamp": row["timestamp"],
        })

    return logs


# =========================================================
# STATES
# =========================================================
REGISTER = 0

DATE = 1
VEHICLE_NUMBER = 2
START_ODOMETER = 3
END_ODOMETER = 4
REASON = 5

EDIT_FIELD = 6
EDIT_VALUE = 7
DELETE_CONFIRM = 8

GET_EDIT_ID = 9
GET_DELETE_ID = 10
PASTE_TEXT = 11
GET_DATE_FILTER = 12


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
# CLASSIFICATION (EXCEL LOGIC)
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
# HELPERS
# =========================================================

def calculate_totals(telegram_id):
    c3 = 0
    c4 = 0
    for log in mileage_logs:
        if log["telegram_id"] == telegram_id:
            if log["vehicle_class"] == "Class 3":
                c3 += log["total"]
            elif log["vehicle_class"] == "Class 4":
                c4 += log["total"]
    return c3, c4, c3 + c4


def find_log_by_id(log_id: str, tid: int):
    return next(
        (log for log in mileage_logs
         if log["telegram_id"] == tid and log["log_id"] == log_id),
        None
    )


def update_log_in_sheet(log):
    records = logs_sheet.get_all_values()
    for idx, row in enumerate(records[1:], start=2):
        if str(row[0]).strip() == str(log["log_id"]).strip():
            try:
                logs_sheet.update(
                    range_name=f"A{idx}:K{idx}",
                    values=[[
                        str(log["log_id"]),
                        str(log["telegram_id"]),
                        str(log["user_id"]),
                        str(log["date"]),
                        str(log["vehicle_number"]),
                        str(log["vehicle_class"]),
                        str(log["start"]),
                        str(log["end"]),
                        str(log["total"]),
                        str(log["reason"]),
                        str(log["timestamp"])
                    ]],
                    value_input_option="RAW"
                )
            except Exception as e:
                print("UPDATE FAILED:", e)
            return True
    return False


def delete_log_from_sheet(log_id):
    try:
        records = logs_sheet.get_all_records()
        for idx, row in enumerate(records, start=2):
            if row["log_id"] == log_id:
                logs_sheet.delete_rows(idx)
                break
    except Exception as e:
        print("DELETE FAILED:", e)


# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update)


async def error_handler(update, context):
    tb = "".join(traceback.format_exception(
        type(context.error),
        context.error,
        context.error.__traceback__
    ))
    logger.error("Exception while handling update:\n%s", tb)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/register - register ID\n"
        "/start - Start main menu\n"
        "/help - Show this help page\n"
        "Log Mileage - Record mileage entry manually\n"
        "Paste PLN Log - Paste the message from PLN Bot\n"
        "View Logs - View past entries\n"
        "View Total - View total mileage\n"
        "Edit Log - Edit past entries\n"
        "Delete Log - Delete past entries\n"

    )


# =========================================================
# MAIN OPTIONS DISPLAY
# =========================================================

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    """Displays the persistent inline main menu with balanced 2-column grid layout."""
    keyboard = [
        [InlineKeyboardButton("📝 Log Mileage", callback_data="menu_log"),
         InlineKeyboardButton("📋 Paste PLN Log", callback_data="menu_logpaste")],
        [InlineKeyboardButton("📋 View Logs", callback_data="menu_view_opts"),
         InlineKeyboardButton("📊 View Totals", callback_data="menu_mytotal")],
        [InlineKeyboardButton("✏️ Edit Log", callback_data="menu_edit"),
         InlineKeyboardButton("❌ Delete Log", callback_data="menu_delete")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🤖 *miBOT Main Menu*\nSelect an action from the options below:"

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def view_logs_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📊 All Logs", callback_data="filter_all")],
        [InlineKeyboardButton("🚗 Class 3 Logs", callback_data="filter_c3"),
         InlineKeyboardButton("🚚 Class 4 Logs", callback_data="filter_c4")],
        [InlineKeyboardButton("📅 Filter by Specific Date", callback_data="filter_date_manual")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu_back")]
    ]
    await query.message.edit_text("Select a filter to view your logs:", reply_markup=InlineKeyboardMarkup(keyboard))


# =========================================================
# UNIFIED TERMINATION / CANCEL HANDLER
# =========================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal termination function that resets memory, drops the state machine, and shows the menu."""
    if context.user_data:
        context.user_data.clear()

    text = "❌ *Action Terminated.*\n\n🤖 *miBOT Main Menu*\nSelect an action from the options below:"
    keyboard = [
        [InlineKeyboardButton("📝 Log Mileage", callback_data="menu_log"),
         InlineKeyboardButton("📋 Paste PLN Log", callback_data="menu_logpaste")],
        [InlineKeyboardButton("📋 View Logs", callback_data="menu_view_opts"),
         InlineKeyboardButton("📊 View Totals", callback_data="menu_mytotal")],
        [InlineKeyboardButton("✏️ Edit Log", callback_data="menu_edit"),
         InlineKeyboardButton("❌ Delete Log", callback_data="menu_delete")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        try:
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return ConversationHandler.END


# =========================================================
# REGISTRATION GUARD
# =========================================================

async def check_registered(update: Update) -> bool:
    """Refreshes registered_users from sheet and returns True if user is registered."""
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)
    tid = update.effective_user.id
    if tid not in registered_users:
        text = "⚠️ You are not registered. Please use /register before performing any actions."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text)
        else:
            await update.message.reply_text(text)
        return False
    return True


# =========================================================
# REGISTER FLOW
# =========================================================

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global registered_users, mileage_logs
    registered_users = await asyncio.to_thread(load_registered_users)
    mileage_logs = await asyncio.to_thread(load_logs)
    tid = update.effective_user.id

    if tid in registered_users:
        await update.message.reply_text(f"Already registered: {registered_users[tid]}")
        await show_main_menu(update)
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await update.message.reply_text("Enter ID (123A format):", reply_markup=InlineKeyboardMarkup(keyboard))
    return REGISTER


async def register_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    user_id = update.message.text.strip().upper()

    if not validate_user_id(user_id):
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
        await update.message.reply_text("Invalid format. Use 123A:", reply_markup=InlineKeyboardMarkup(keyboard))
        return REGISTER

    if user_id in registered_users.values():
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
        await update.message.reply_text("ID already taken. Try again:", reply_markup=InlineKeyboardMarkup(keyboard))
        return REGISTER

    registered_users[tid] = user_id
    save_user(tid, user_id)

    await update.message.reply_text(f"Registered: {user_id}")
    await show_main_menu(update)
    return ConversationHandler.END


# =========================================================
# LOG MILEAGE FLOW
# =========================================================

async def start_log_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sync global cache on entry
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        msg_obj = query.message
    else:
        msg_obj = update.message

    if not await check_registered(update):
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("📆 Today", callback_data="date_today"),
         InlineKeyboardButton("⏳ Yesterday", callback_data="date_yesterday")],
        [InlineKeyboardButton("⌨️ Enter Manually", callback_data="date_manual")],
        [InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]
    ]

    if update.callback_query:
        await msg_obj.edit_text("Step 1: Select the log date:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg_obj.reply_text("Step 1: Select the log date:", reply_markup=InlineKeyboardMarkup(keyboard))
    return DATE


async def handle_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = query.data
    now_sgt = datetime.now(SGT)

    if action == "date_today":
        date_str = now_sgt.strftime("%d%m%y")
    elif action == "date_yesterday":
        date_str = (now_sgt - timedelta(days=1)).strftime("%d%m%y")
    elif action == "date_manual":
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
        await query.message.edit_text("Please type your date manually in **DDMMYY** format (e.g., 270526):",
                                      reply_markup=InlineKeyboardMarkup(keyboard))
        return DATE

    context.user_data['date'] = date_str
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.edit_text(f"✅ Date set to: *{date_str}*\n\nStep 2: Type the 5-digit **Vehicle Number**:",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return VEHICLE_NUMBER


async def log_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date = update.message.text.strip()

    if not validate_date(date):
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
        await update.message.reply_text("Invalid date format. Use DDMMYY:", reply_markup=InlineKeyboardMarkup(keyboard))
        return DATE

    context.user_data["date"] = date
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await update.message.reply_text("Enter vehicle number (5 digits only, e.g. 12345):",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return VEHICLE_NUMBER


async def log_vehicle_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vn = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not validate_vehicle_number(vn):
        await update.message.reply_text("Invalid vehicle number. Must be exactly 5 digits:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return VEHICLE_NUMBER

    vn_int = int(vn)
    vehicle_class = classify_vehicle(vn_int)

    if vehicle_class == "Unknown":
        await update.message.reply_text("Vehicle number does not match any class range. Try again:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return VEHICLE_NUMBER

    context.user_data["vehicle_number"] = vn
    context.user_data["vehicle_class"] = vehicle_class

    await update.message.reply_text(f"Detected Class: {vehicle_class}\n\nEnter starting odometer:",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return START_ODOMETER


async def log_start_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    try:
        start = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Numbers only. Enter starting odometer:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return START_ODOMETER

    context.user_data["start"] = start
    await update.message.reply_text("Enter ending odometer:", reply_markup=InlineKeyboardMarkup(keyboard))
    return END_ODOMETER


async def log_end_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    try:
        end = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Numbers only. Enter ending odometer:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return END_ODOMETER

    start = context.user_data["start"]
    if end < start:
        await update.message.reply_text("End cannot be smaller than start. Enter ending odometer:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return END_ODOMETER

    context.user_data["end"] = end
    await update.message.reply_text("Enter reason for driving:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REASON


async def log_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    reason = update.message.text.strip()

    start = context.user_data["start"]
    end = context.user_data["end"]
    total = end - start

    log_id = generate_base36_id(
        registered_users[tid],
        context.user_data["date"],
        context.user_data["vehicle_number"]
    )
    log_entry = {
        "log_id": log_id,
        "telegram_id": tid,
        "user_id": registered_users[tid],
        "date": context.user_data["date"],
        "vehicle_number": context.user_data["vehicle_number"],
        "vehicle_class": context.user_data["vehicle_class"],
        "start": start,
        "end": end,
        "total": total,
        "reason": reason,
        "timestamp": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S"),
    }

    mileage_logs.append(log_entry)
    await asyncio.to_thread(save_log, log_entry)

    await update.message.reply_text(
        "Logged Successfully\n\n"
        f"LOG ID: {log_id}\n\n"
        f"Date: {log_entry['date']}\n"
        f"Vehicle: {log_entry['vehicle_number']}\n"
        f"Class: {log_entry['vehicle_class']}\n"
        f"Distance: {total} km\n"
        f"Reason: {reason}"
    )
    await show_main_menu(update)
    return ConversationHandler.END


# =========================================================
# LOGPASTE INTERACTION FLOW
# =========================================================

async def start_logpaste_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sync global cache on entry
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        msg_obj = query.message
    else:
        msg_obj = update.message

    if not await check_registered(update):
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    text = "📋 Please paste your raw mileage log block text from the PLN bot below:"

    if update.callback_query:
        await msg_obj.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return PASTE_TEXT


async def process_logpaste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await execute_logpaste_logic(update, context, text)
    return ConversationHandler.END


async def execute_logpaste_logic(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    global registered_users, mileage_logs
    registered_users = await asyncio.to_thread(load_registered_users)
    mileage_logs = await asyncio.to_thread(load_logs)
    tid = update.effective_user.id

    try:
        vehicle_match = re.search(r"VEH NO:\s*(\d{5})", text, re.IGNORECASE)
        date_match = re.search(r"START DATE:\s*(\d{6})", text, re.IGNORECASE)
        start_match = re.search(r"START ODOMETER:\s*(\d+)", text, re.IGNORECASE)
        end_match = re.search(r"END ODOMETER:\s*(\d+)", text, re.IGNORECASE)
        reason_match = re.search(r"MOVEMENT PURPOSE.*?:\s*(.+)", text, re.IGNORECASE)

        if not all([vehicle_match, date_match, start_match, end_match, reason_match]):
            await update.message.reply_text(
                "Could not parse pasted format. Please try again from the menu layout option.")
            await show_main_menu(update)
            return

        vehicle_number = vehicle_match.group(1)
        date = date_match.group(1)
        start = int(start_match.group(1))
        end = int(end_match.group(1))
        reason = reason_match.group(1).strip()

        if not validate_date(date):
            await update.message.reply_text("Invalid date format inside block payload.")
            await show_main_menu(update)
            return

        if end < start:
            await update.message.reply_text("End odometer cannot be smaller than start odometer.")
            await show_main_menu(update)
            return

        vehicle_class = classify_vehicle(int(vehicle_number))
        if vehicle_class == "Unknown":
            await update.message.reply_text("Vehicle class could not be matched automatically.")
            await show_main_menu(update)
            return

        total = end - start
        log_id = generate_base36_id(registered_users[tid], date, vehicle_number)

        log_entry = {
            "log_id": log_id,
            "telegram_id": tid,
            "user_id": registered_users[tid],
            "date": date,
            "vehicle_number": vehicle_number,
            "vehicle_class": vehicle_class,
            "start": start,
            "end": end,
            "total": total,
            "reason": reason,
            "timestamp": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S"),
        }

        mileage_logs.append(log_entry)
        await asyncio.to_thread(save_log, log_entry)

        await update.message.reply_text(
            "✅ Log imported successfully\n\n"
            f"LOG ID: {log_id}\n"
            f"Date: {date}\n"
            f"Vehicle: {vehicle_number}\n"
            f"Class: {vehicle_class}\n"
            f"Distance: {total} km\n"
            f"Reason: {reason}"
        )
    except Exception as e:
        await update.message.reply_text(f"Import failed:\n{str(e)}")

    await show_main_menu(update)


# =========================================================
# EDIT FLOW
# =========================================================

async def start_edit_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sync global cache on entry
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    if not await check_registered(update):
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.edit_text("✏️ Please type the **LOG ID** of the record you wish to modify:",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return GET_EDIT_ID


async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sync global cache on entry via text command entry point
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /edit <log_id>\n\nOr type the target **LOG ID** below directly:",
                                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return GET_EDIT_ID

    log = find_log_by_id(context.args[0].lower(), tid)
    if not log:
        await update.message.reply_text("Log not found. Type a valid LOG ID:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return GET_EDIT_ID

    context.user_data["edit_log"] = log
    return await prompt_field_selection(update.message, context)


async def process_edit_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    log_id = update.message.text.strip().lower()
    log = find_log_by_id(log_id, tid)
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not log:
        await update.message.reply_text("❌ Log ID not found. Please enter a valid LOG ID:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return GET_EDIT_ID

    context.user_data["edit_log"] = log
    return await prompt_field_selection(update.message, context)


async def prompt_field_selection(msg_obj, context):
    keyboard = [
        [InlineKeyboardButton("Date", callback_data="edit_field_date"),
         InlineKeyboardButton("Vehicle No.", callback_data="edit_field_vn")],
        [InlineKeyboardButton("Start Odo", callback_data="edit_field_start"),
         InlineKeyboardButton("End Odo", callback_data="edit_field_end")],
        [InlineKeyboardButton("Reason", callback_data="edit_field_reason")],
        [InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]
    ]
    await msg_obj.reply_text("Select which field you would like to edit:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_FIELD


async def handle_field_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chosen_field = query.data.replace("edit_field_", "")
    if chosen_field == "vn": chosen_field = "vehicle"

    context.user_data['edit_field'] = chosen_field
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.edit_text(f"Please type the new value for *{chosen_field.title()}*:",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return EDIT_VALUE


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = update.message.text.strip().lower()
    allowed = ["date", "vehicle", "start", "end", "reason"]
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if field not in allowed:
        await update.message.reply_text("Invalid field. Choose: date / vehicle / start / end / reason",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return EDIT_FIELD

    context.user_data["edit_field"] = field
    await update.message.reply_text("Enter new value:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log = context.user_data["edit_log"]
    field = context.user_data["edit_field"]
    value = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if field == "date":
        if not validate_date(value):
            await update.message.reply_text("Invalid date format (ddmmyy):",
                                            reply_markup=InlineKeyboardMarkup(keyboard))
            return EDIT_VALUE
        log["date"] = value

    elif field == "vehicle":
        if not validate_vehicle_number(value):
            await update.message.reply_text("Invalid vehicle number (5-digits):",
                                            reply_markup=InlineKeyboardMarkup(keyboard))
            return EDIT_VALUE
        log["vehicle_number"] = value
        log["vehicle_class"] = classify_vehicle(int(value))

    elif field == "start":
        try:
            log["start"] = int(value)
        except ValueError:
            await update.message.reply_text("Numbers only. Enter start value:",
                                            reply_markup=InlineKeyboardMarkup(keyboard))
            return EDIT_VALUE

    elif field == "end":
        try:
            log["end"] = int(value)
        except ValueError:
            await update.message.reply_text("Numbers only. Enter end value:",
                                            reply_markup=InlineKeyboardMarkup(keyboard))
            return EDIT_VALUE

    elif field == "reason":
        log["reason"] = value

    if log["end"] < log["start"]:
        await update.message.reply_text("End odometer cannot be smaller than start odometer. Try again:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return EDIT_VALUE

    log["total"] = log["end"] - log["start"]
    await update.message.reply_text(
        "✅ Log updated successfully\n\n"
        f"ID: {log['log_id']}\n"
        f"New total: {log['total']} km"
    )

    await asyncio.to_thread(update_log_in_sheet, log)

    # Sync local cache instantly after saving changes
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    await show_main_menu(update)
    return ConversationHandler.END


# =========================================================
# DELETE FLOW
# =========================================================

async def start_delete_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sync global cache on entry
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    if not await check_registered(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.edit_text("❌ Please type the **LOG ID** of the record you wish to delete:",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return GET_DELETE_ID


async def delete_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sync global cache on entry via text command entry point
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /delete <log_id>\n\nOr type the target **LOG ID** below directly:",
                                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return GET_DELETE_ID

    log_id = context.args[0].lower()
    log = find_log_by_id(log_id, tid)
    if not log:
        await update.message.reply_text("Log ID not found. Type valid LOG ID:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return GET_DELETE_ID

    context.user_data["delete_log"] = log
    return await prompt_delete_confirmation(update.message, log)


async def process_delete_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    log_id = update.message.text.strip().lower()
    log = find_log_by_id(log_id, tid)
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not log:
        await update.message.reply_text("❌ Log ID not found. Please enter a valid LOG ID:",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return GET_DELETE_ID

    context.user_data["delete_log"] = log
    return await prompt_delete_confirmation(update.message, log)


async def prompt_delete_confirmation(msg_obj, log):
    keyboard = [
        [InlineKeyboardButton("🗑️ Yes, Delete Log", callback_data="confirm_delete_yes")],
        [InlineKeyboardButton("❌ No, Keep Log", callback_data="flow_cancel")]
    ]
    await msg_obj.reply_text(
        f"⚠️ *CRITICAL VERIFICATION*\nAre you sure you want to permanently delete log `{log['log_id']}`?\n\n"
        f"Date: {log['date']}\n"
        f"Vehicle: {log['vehicle_number']}\n"
        f"Distance: {log['total']} km",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return DELETE_CONFIRM


async def handle_delete_execution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    log = context.user_data.get("delete_log")
    if not log:
        await query.message.edit_text("❌ Error: No pending deletion session found.")
        return await cancel(update, context)

    if query.data == "confirm_delete_yes":
        await asyncio.to_thread(delete_log_from_sheet, log["log_id"])

        # Sync structural reality straight from Google Drive row blocks
        global mileage_logs
        mileage_logs = await asyncio.to_thread(load_logs)

        await query.message.edit_text(f"✅ Log entry `{log['log_id']}` successfully deleted.")

    context.user_data.clear()
    await show_main_menu(update, context)
    return ConversationHandler.END


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response = update.message.text.strip().lower()
    log = context.user_data.get("delete_log")

    if response == "yes" and log:
        await asyncio.to_thread(delete_log_from_sheet, log["log_id"])

        # Sync structural reality straight from Google Drive row blocks
        global mileage_logs
        mileage_logs = await asyncio.to_thread(load_logs)

        await update.message.reply_text(f"✅ Deleted log {log['log_id']} successfully.")
    else:
        await update.message.reply_text("❌ Deletion cancelled.")

    context.user_data.clear()
    await show_main_menu(update)
    return ConversationHandler.END


# =========================================================
# LOG FILTERS & SEARCH / TOTALS VIEWERS
# =========================================================

async def handle_log_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tid = update.effective_user.id
    action = query.data
    results = []
    title = ""

    # Sync cache to fetch live manual sheet removals before building view lists
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    if action == "filter_all":
        results = [log for log in mileage_logs if log["telegram_id"] == tid]
        title = "All Logs"
    elif action == "filter_c3":
        results = [log for log in mileage_logs if log["telegram_id"] == tid and log["vehicle_class"] == "Class 3"]
        title = "Class 3 Logs"
    elif action == "filter_c4":
        results = [log for log in mileage_logs if log["telegram_id"] == tid and log["vehicle_class"] == "Class 4"]
        title = "Class 4 Logs"
    elif action == "filter_date_manual":
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
        await query.message.edit_text(
            "📅 Enter the date to search *(DDMMYY)*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        context.user_data["awaiting_date_filter"] = True
        return GET_DATE_FILTER

    if not results:
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        await query.message.edit_text(f"ℹ️ No logs found under: *{title}*", reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode="Markdown")
        return

    results.sort(key=lambda log: datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S"), reverse=True)

    msg = f"📊 *{title}*\n\n"
    for i, log in enumerate(results, 1):
        msg += (
            f"{i}. LOG ID: `{log['log_id']}`\n"
            f"Date: {log['date']}\n"
            f"Vehicle: {log['vehicle_number']} ({log['vehicle_class']})\n"
            f"Odometer: {log['start']} → {log['end']}\n"
            f"Distance: {log['total']} km\n"
            f"Reason: {log['reason']}\n\n"
        )

    keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu_back")]]
    await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def process_date_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    value = update.message.text.strip().lower()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not validate_date(value):
        await update.message.reply_text(
            "❌ Invalid format. Please enter date as *DDMMYY*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return GET_DATE_FILTER

    # Sync cache to fetch live status before single-date matching checks
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    results = [log for log in mileage_logs if log["telegram_id"] == tid and log["date"] == value]

    if not results:
        back_keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu_back")]]
        await update.message.reply_text(
            f"ℹ️ No logs found for *{value}*.",
            reply_markup=InlineKeyboardMarkup(back_keyboard),
            parse_mode="Markdown"
        )
        context.user_data.clear()
        return ConversationHandler.END

    msg = f"📊 *Logs for {value}*\n\n"
    for i, log in enumerate(results, 1):
        msg += (
            f"{i}. LOG ID: `{log['log_id']}`\n"
            f"Vehicle: {log['vehicle_number']} ({log['vehicle_class']})\n"
            f"Odometer: {log['start']} → {log['end']}\n"
            f"Distance: {log['total']} km\n"
            f"Reason: {log['reason']}\n\n"
        )

    back_keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu_back")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(back_keyboard), parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def my_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_registered(update):
        msg = "⚠️ Please /register first."
        if update.message:
            await update.message.reply_text(msg)
        elif update.callback_query:
            await update.callback_query.message.reply_text(msg)
        return

    # Sync cache before balancing math computations
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id
    c3, c4, total = calculate_totals(tid)
    text = (
        "📊 *Mileage Totals*\n\n"
        f"Class 3: {c3} km\n"
        f"Class 4: {c4} km\n"
        f"Total: {total} km"
    )

    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, parse_mode="Markdown")

    await show_main_menu(update)


async def logs_by_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if len(context.args) == 0:
        await update.message.reply_text("/logs all | class 3 | class 4 | <ddmmyy>")
        return

    # Sync cache on text command parameters initialization
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    args_text = " ".join(context.args).strip().lower()
    if args_text == "all":
        results = [log for log in mileage_logs if log["telegram_id"] == tid]
        title = "All Logs"
    elif args_text == "class 3":
        results = [log for log in mileage_logs if log["telegram_id"] == tid and log["vehicle_class"] == "Class 3"]
        title = "Class 3 Logs"
    elif args_text == "class 4":
        results = [log for log in mileage_logs if log["telegram_id"] == tid and log["vehicle_class"] == "Class 4"]
        title = "Class 4 Logs"
    else:
        if not validate_date(args_text):
            await update.message.reply_text("Invalid format. Use DDMMYY.")
            return
        results = [log for log in mileage_logs if log["telegram_id"] == tid and log["date"] == args_text]
        title = f"Logs for {args_text}"

    if not results:
        await update.message.reply_text("No logs found.")
        return

    msg = f"{title}\n\n"
    for i, log in enumerate(results, 1):
        msg += f"{i}. ID: {log['log_id']} | {log['total']} km | Vehicle: {log['vehicle_number']}\n"

    await update.message.reply_text(msg)
    await show_main_menu(update)


async def today_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    today = datetime.now(SGT).strftime("%d%m%y")

    # Sync cache on daily checklist tracking commands
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    results = [log for log in mileage_logs if log["telegram_id"] == tid and log["date"] == today]

    if not results:
        await update.message.reply_text("No logs today.")
        return

    total = sum(log["total"] for log in results)
    msg = f"Today ({today})\n\n" + "".join(
        [f"{l['vehicle_number']}: {l['total']} km\n" for l in results]) + f"\nTOTAL: {total} km"
    await update.message.reply_text(msg)
    await show_main_menu(update)


async def search_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /search <log_id>")
        return

    # Sync cache to support targeting matching specific unique strings
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    log = find_log_by_id(context.args[0].lower(), tid)
    if not log:
        await update.message.reply_text("Log not found.")
        return

    await update.message.reply_text(f"Log Found: {log['log_id']} | {log['date']} | {log['total']} km | {log['reason']}")
    await show_main_menu(update)


async def unkown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /help.")
    await show_main_menu(update)


# =========================================================
# BOT ORCHESTRATION & HANDLER REGISTRATION
# =========================================================

def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", show_main_menu),
            CommandHandler("menu", show_main_menu),
            CommandHandler("register", register_start),
            CommandHandler("log", start_log_flow),
            CommandHandler("edit", edit),
            CommandHandler("delete", delete_log),
            CommandHandler("logpaste", start_logpaste_flow),
            CallbackQueryHandler(start_log_flow, pattern="^menu_log$"),
            CallbackQueryHandler(start_edit_flow, pattern="^menu_edit$"),
            CallbackQueryHandler(start_delete_flow, pattern="^menu_delete$"),
            CallbackQueryHandler(start_logpaste_flow, pattern="^menu_logpaste$"),
            CallbackQueryHandler(handle_log_filters, pattern="^filter_date_manual$")
        ],
        states={
            REGISTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_save)],
            DATE: [
                CallbackQueryHandler(handle_date_selection, pattern="^date_.*$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, log_date)
            ],
            VEHICLE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_vehicle_number)],
            START_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_start_odometer)],
            END_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_end_odometer)],
            REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_reason)],
            GET_EDIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_id)],
            EDIT_FIELD: [
                CallbackQueryHandler(handle_field_choice, pattern="^edit_field_.*$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field)
            ],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
            GET_DELETE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_delete_id)],
            DELETE_CONFIRM: [
                CallbackQueryHandler(handle_delete_execution, pattern="^confirm_delete_.*$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)
            ],
            PASTE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_logpaste)],
            GET_DATE_FILTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_date_filter)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^flow_cancel$"),
            CallbackQueryHandler(show_main_menu, pattern="^menu_back$"),
        ],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mytotal", my_total))
    application.add_handler(CommandHandler("logs", logs_by_date))
    application.add_handler(CommandHandler("today", today_logs))
    application.add_handler(CommandHandler("search", search_log))

    application.add_handler(CallbackQueryHandler(view_logs_options, pattern="^menu_view_opts$"))
    application.add_handler(CallbackQueryHandler(handle_log_filters, pattern="^filter_.*$"))
    application.add_handler(CallbackQueryHandler(my_total, pattern="^menu_mytotal$"))
    application.add_handler(CallbackQueryHandler(show_main_menu, pattern="^menu_back$"))

    application.add_error_handler(error_handler)
    application.add_handler(MessageHandler(filters.COMMAND, unkown_command))

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    print("Pre-fetching Google Sheets database records...")
    global registered_users, mileage_logs
    registered_users = loop.run_until_complete(asyncio.to_thread(load_registered_users))
    mileage_logs = loop.run_until_complete(asyncio.to_thread(load_logs))
    print(f"Cache synced. Users: {len(registered_users)} | Entries: {len(mileage_logs)}")

    print("miBOT Engine Ready.")
    application.run_polling(close_loop=False,
                            drop_pending_updates=True)


def main():
    Thread(target=run_web, daemon=True).start()
    Thread(target=heartbeat, daemon=True).start()
    run_bot()


if __name__ == '__main__':
    multiprocessing.set_start_method("spawn")
    main()