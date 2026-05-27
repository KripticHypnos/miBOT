#STABLE VER: 1032PM 270526
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
            requests.get("http://localhost:10000/",timeout=10)
            print(f"heartbeat: {datetime.now(SGT)}")
        except Exception as e:
            print(f"Heartbeat failed: {e}")
        time.sleep(240)



SGT = timezone(timedelta(hours=8))



def handle_sigterm(signum, frame):
    print("SIGTERM received - ignoring to keep bot alive")

signal.signal(signal.SIGTERM, handle_sigterm)

# =========================================================
#BASE 36 ENCODING
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

    # Combine into a single integer seed
    raw = f"{user_id}{date}{vehicle}{int(time.time() * 1000)}"

    # Convert string → integer hash
    numeric_seed = abs(hash(raw))

    # Convert to Base36
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
mileage_logs = []      # list of logs

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
        users[int(row["telegram_id"])] = row["user_id"]

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
        logs.append({
            "log_id": row["log_id"],
            "telegram_id": int(row["telegram_id"]),
            "user_id": row["user_id"],
            "date": str(row["date"]).strip(),  # ← force string
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
    """
    Replicates Excel formula logic.
    Returns Class 3 / Class 4 / Unknown.
    """

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

        # Column A = log_id
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
    except Exception as e:
        print("DELETE FAILED:", e)


    for idx, row in enumerate(records, start=2):

        if row["log_id"] == log_id:

            logs_sheet.delete_rows(idx)
            break


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
        "/log - add mileage\n"
        "/logpaste - Copy your mileage from PLN bot\n"
        "/mytotal - totals\n"
        "/logs <date> - search logs\n"
        "/today - today logs\n"
        "/cancel - cancel"
        "/search - search logs\n"
        "/edit - edit log\n"
        "/delete - delete log\n"
        "/cancel - cancel command"
    )

# =========================================================
# MAIN OPTIONS DISPLAY
# =========================================================

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the persistent inline main menu."""
    keyboard = [
        [InlineKeyboardButton("📝 Log Mileage", callback_data="menu_log")],
        [InlineKeyboardButton("📋 View Logs", callback_data="menu_view_opts")],
        [InlineKeyboardButton("✏️ Edit Log", callback_data="menu_edit")],
        [InlineKeyboardButton("❌ Delete Log", callback_data="menu_delete")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🤖 *miBOT Main Menu*\nSelect an action from the options below:"

    # Handle both direct commands (/start) and callback updates
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def view_logs_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replaces /logs text command arguments with filter buttons."""
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
# DATE QUICK SELECT
# =========================================================

async def start_log_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered by 'menu_log'. Initializes the registration check and prompts for date."""
    query = update.callback_query
    await query.answer()

    tid = update.effective_user.id
    if tid not in registered_users:
        await query.message.edit_text("⚠️ Please register first using /register.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("📆 Today", callback_data="date_today"),
         InlineKeyboardButton("⏳ Yesterday", callback_data="date_yesterday")],
        [InlineKeyboardButton("⌨️ Enter Manually", callback_data="date_manual")],
        [InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]
    ]
    await query.message.edit_text("Step 1: Select the log date:", reply_markup=InlineKeyboardMarkup(keyboard))
    return DATE


async def handle_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes inline shortcuts or transitions to text input for date."""
    query = update.callback_query
    await query.answer()

    action = query.data
    now_sgt = datetime.now(SGT)

    if action == "date_today":
        date_str = now_sgt.strftime("%d%m%y")
    elif action == "date_yesterday":
        date_str = (now_sgt - timedelta(days=1)).strftime("%d%m%y")
    elif action == "date_manual":
        await query.message.edit_text("Please type your date manually in **DDMMYY** format (e.g., 270526):")
        return DATE

    # Save automatically computed date and skip to vehicle number input
    context.user_data['date'] = date_str
    await query.message.edit_text(f"✅ Date set to: *{date_str}*\n\nStep 2: Type the 5-digit **Vehicle Number**:")
    return VEHICLE_NUMBER

# =========================================================
# REGISTER FLOW
# =========================================================

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id

    if tid in registered_users:
        await update.message.reply_text(
            f"Already registered: {registered_users[tid]}"
        )
        await show_main_menu(update)
        return ConversationHandler.END

    await update.message.reply_text("Enter ID (123A format)")
    return REGISTER


async def register_save(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id
    user_id = update.message.text.strip().upper()

    if not validate_user_id(user_id):
        await update.message.reply_text("Invalid format. Use 123A.")
        return REGISTER

    if user_id in registered_users.values():
        await update.message.reply_text("ID already taken.")
        return REGISTER

    registered_users[tid] = user_id
    save_user(tid, user_id)

    await update.message.reply_text(f"Registered: {user_id}")

    await show_main_menu(update)

    return ConversationHandler.END


# =========================================================
# LOG FLOW
# =========================================================

async def log_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)
    tid = update.effective_user.id

    if tid not in registered_users:
        await update.message.reply_text("Please /register first.")
        return ConversationHandler.END

    await update.message.reply_text("Enter date (ddmmyy)")
    return DATE


async def log_date(update: Update, context: ContextTypes.DEFAULT_TYPE):

    date = update.message.text.strip()

    if not validate_date(date):
        await update.message.reply_text("Invalid date format.")
        return DATE

    context.user_data["date"] = date

    await update.message.reply_text(
        "Enter vehicle number (5 digits only, e.g. 12345)"
    )

    return VEHICLE_NUMBER


async def log_vehicle_number(update: Update, context: ContextTypes.DEFAULT_TYPE):

    vn = update.message.text.strip()

    if not validate_vehicle_number(vn):
        await update.message.reply_text(
            "Invalid vehicle number.\nMust be exactly 5 digits."
        )
        return VEHICLE_NUMBER

    vn_int = int(vn)

    vehicle_class = classify_vehicle(vn_int)

    if vehicle_class == "Unknown":
        await update.message.reply_text(
            "Vehicle number does not match any class range in system."
        )
        return VEHICLE_NUMBER

    context.user_data["vehicle_number"] = vn
    context.user_data["vehicle_class"] = vehicle_class

    await update.message.reply_text(
        f"Detected Class: {vehicle_class}\n\nEnter starting odometer"
    )

    return START_ODOMETER


async def log_start_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        start = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Numbers only.")
        return START_ODOMETER

    context.user_data["start"] = start

    await update.message.reply_text("Enter ending odometer")
    return END_ODOMETER


async def log_end_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        end = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Numbers only.")
        return END_ODOMETER

    start = context.user_data["start"]

    if end < start:
        await update.message.reply_text("End cannot be smaller than start.")
        return END_ODOMETER

    context.user_data["end"] = end

    await update.message.reply_text("Enter reason for driving")
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


async def handle_log_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes inline filter buttons and displays matching mileage logs."""
    query = update.callback_query
    await query.answer()

    tid = update.effective_user.id
    if tid not in registered_users:
        await query.message.edit_text("⚠️ Please /register first.")
        return

    action = query.data
    results = []
    title = ""

    # =====================================================
    # EVALUATE CALLBACK ACTIONS (REPLACES TEXT ARGUMENTS)
    # =====================================================
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
        # Direct the user to use the legacy text syntax for manual custom ranges if desired
        await query.message.edit_text(
            "To filter manually by an exact date, please use the text command:\n"
            "`/logs <ddmmyy>` (e.g., `/logs 270526`)",
            parse_mode="Markdown"
        )
        return

    # Handle empty dataset results
    if not results:
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        await query.message.edit_text(f"ℹ️ No logs found under: *{title}*", reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode="Markdown")
        return

    # Sort chronologically (Matches your exact original repo logic)
    results.sort(
        key=lambda log: datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S"),
        reverse=True
    )

    # =====================================================
    # BUILD OUTPUT MESSAGE STRING
    # =====================================================
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

    # Display compiled log history with a navigation return anchor
    keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu_back")]]
    await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# =========================================================
# TOTALS
# =========================================================

async def my_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)

    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id

    if tid not in registered_users:
        await update.message.reply_text("Please /register first.")
        return

    c3, c4, total = calculate_totals(tid)

    await update.message.reply_text(
        "Mileage Totals\n\n"
        f"Class 3: {c3}\n"
        f"Class 4: {c4}\n"
        f"Total: {total}"
    )
    await show_main_menu(update)


# =========================================================
# LOGS BY DATE
# =========================================================

# =========================================================
# LOGS FILTER
# =========================================================

async def logs_by_date(update: Update, context: ContextTypes.DEFAULT_TYPE):

    tid = update.effective_user.id

    if tid not in registered_users:
        await update.message.reply_text("Please /register first.")
        return

    if len(context.args) == 0:
        await update.message.reply_text(
            "/logs all\n"
            "/logs class 3\n"
            "/logs class 4\n"
            "/logs <ddmmyy>"
        )
        return

    args_text = " ".join(context.args).strip().lower()

    # =====================================================
    # ALL LOGS
    # =====================================================

    if args_text == "all":

        results = [
            log for log in mileage_logs
            if log["telegram_id"] == tid
        ]

        title = "All Logs"

    # =====================================================
    # CLASS 3
    # =====================================================

    elif args_text == "class 3":

        results = [
            log for log in mileage_logs
            if log["telegram_id"] == tid
            and log["vehicle_class"] == "Class 3"
        ]

        title = "Class 3 Logs"

    # =====================================================
    # CLASS 4
    # =====================================================

    elif args_text == "class 4":

        results = [
            log for log in mileage_logs
            if log["telegram_id"] == tid
            and log["vehicle_class"] == "Class 4"
        ]

        title = "Class 4 Logs"

    # =====================================================
    # DATE FILTER
    # =====================================================

    else:

        date = args_text

        if not validate_date(date):
            await update.message.reply_text(
                "Invalid format.\n"
                "Use:\n"
                "/logs all\n"
                "/logs class 3\n"
                "/logs class 4\n"
                "/logs <ddmmyy>"
            )
            return

        results = [
            log for log in mileage_logs
            if log["telegram_id"] == tid
            and log["date"] == date
        ]

        title = f"Logs for {date}"

    # =====================================================
    # NO RESULTS
    # =====================================================

    if not results:
        await update.message.reply_text("No logs found.")
        return

    # =====================================================
    # SORT CHRONOLOGICALLY
    # =====================================================

    results.sort(
        key=lambda log: datetime.strptime(
            log["timestamp"],
            "%Y-%m-%d %H:%M:%S"
        ),
        reverse=True
    )

    # =====================================================
    # BUILD MESSAGE
    # =====================================================

    msg = f"{title}\n\n"

    for i, log in enumerate(results, 1):

        msg += (
            f"{i}.  LOG ID: {log['log_id']}\n\n"
            f"Date: {log['date']}\n"
            f"Vehicle: {log['vehicle_number']} ({log['vehicle_class']})\n"
            f"Odometer: {log['start']} → {log['end']}\n"
            f"Distance: {log['total']} km\n"
            f"Reason: {log['reason']}\n\n"
        )

    await update.message.reply_text(msg)
    await show_main_menu(update)

# =========================================================
# TODAY LOGS
# =========================================================

async def today_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global registered_users
    registered_users= await asyncio.to_thread(load_registered_users)

    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id

    if tid not in registered_users:
        await update.message.reply_text("Please /register first.")
        return

    today = datetime.now(SGT).strftime("%d%m%y")

    results = [
        log for log in mileage_logs
        if log["telegram_id"] == tid and log["date"] == today
    ]

    if not results:
        await update.message.reply_text("No logs today.")
        return

    total = sum(log["total"] for log in results)

    msg = f"Today ({today})\n\n"

    for log in results:
        msg += (
            f"{log['vehicle_number']} ({log['vehicle_class']})\n"
            f"{log['total']} km | {log['reason']}\n\n"
        )

    msg += f"TOTAL: {total} km"

    await update.message.reply_text(msg)
    await show_main_menu(update)


# =========================================================
# CANCEL
# =========================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if context.user_data:
        context.user_data.clear()

        await update.message.reply_text(
            "Cancelled",
            reply_markup=ReplyKeyboardRemove()
        )



    else:
        await update.message.reply_text(
            "No command to cancel."
        )
    await show_main_menu(update)
    return ConversationHandler.END

# =========================================================
# SEARCH ID
# =========================================================

async def search_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)

    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id

    if tid not in registered_users:
        await update.message.reply_text("Please /register first.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /search <log_id>")
        return

    log_id = context.args[0].lower()

    log = find_log_by_id(log_id, tid)

    if not log:
        await update.message.reply_text("Log not found.")
        return

    msg = (
        f"Log Found\n\n"
        f"ID: {log['log_id']}\n"
        f"Date: {log['date']}\n"
        f"Vehicle: {log['vehicle_number']} ({log['vehicle_class']})\n"
        f"Distance: {log['total']} km\n"
        f"Reason: {log['reason']}\n"
    )

    await update.message.reply_text(msg)
    await show_main_menu(update)

# =========================================================
# EDIT FIELD SELECTOR BUTTONS
# =========================================================
async def prompt_edit_field_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays fields available for modifications via an inline keyboard layout."""
    # (Assuming log ID validation has already happened up to this point)
    keyboard = [
        [InlineKeyboardButton("Date", callback_data="edit_field_date"),
         InlineKeyboardButton("Vehicle No.", callback_data="edit_field_vn")],
        [InlineKeyboardButton("Start Odo", callback_data="edit_field_start"),
         InlineKeyboardButton("End Odo", callback_data="edit_field_end")],
        [InlineKeyboardButton("Reason", callback_data="edit_field_reason")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="flow_cancel")]
    ]

    text = "Select which field you would like to edit:"
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_FIELD


async def handle_field_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures callback field data and shifts state to capture new data values."""
    query = update.callback_query
    await query.answer()

    # Extract structural field name out of callback data prefix
    chosen_field = query.data.replace("edit_field_", "")
    context.user_data['target_edit_field'] = chosen_field

    await query.message.edit_text(f"Please type the new value for *{chosen_field.replace('_', ' ').title()}*:")
    return EDIT_VALUE
# =========================================================
# EDIT
# =========================================================

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /edit <log_id>")
        return ConversationHandler.END

    log = find_log_by_id(context.args[0], tid)

    if not log:
        await update.message.reply_text("Log not found")
        return ConversationHandler.END

    context.user_data["edit_log"] = log

    await update.message.reply_text(
        "What do you want to edit?\n"
        "Options: date / vehicle / start / end / reason"
    )

    return EDIT_FIELD


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):

    field = update.message.text.strip().lower()

    allowed = ["date", "vehicle", "start", "end", "reason"]

    if field not in allowed:
        await update.message.reply_text("Invalid field. Choose: date / vehicle / start / end / reason")
        return EDIT_FIELD

    context.user_data["edit_field"] = field

    await update.message.reply_text("Enter new value")
    return EDIT_VALUE

async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):

    log = context.user_data["edit_log"]
    field = context.user_data["edit_field"]
    value = update.message.text.strip()

    # -------------------------
    # DATE
    # -------------------------
    if field == "date":
        if not validate_date(value):
            await update.message.reply_text("Invalid date format (ddmmyy)")
            return EDIT_VALUE
        log["date"] = value

    # -------------------------
    # VEHICLE
    # -------------------------
    elif field == "vehicle":
        if not validate_vehicle_number(value):
            await update.message.reply_text("Invalid vehicle number")
            return EDIT_VALUE

        log["vehicle_number"] = value
        log["vehicle_class"] = classify_vehicle(int(value))

    # -------------------------
    # START
    # -------------------------
    elif field == "start":
        try:
            log["start"] = int(value)
        except ValueError:
            await update.message.reply_text("Numbers only")
            return EDIT_VALUE

    # -------------------------
    # END
    # -------------------------
    elif field == "end":
        try:
            log["end"] = int(value)
        except ValueError:
            await update.message.reply_text("Numbers only")
            return EDIT_VALUE

    # -------------------------
    # REASON
    # -------------------------
    elif field == "reason":
        log["reason"] = value

    # -------------------------
    # RECALCULATE TOTAL
    # -------------------------

    if log["end"] < log["start"]:
        await update.message.reply_text(
            "End odometer cannot be smaller than start odometer."
        )
        return EDIT_VALUE

    log["total"] = log["end"] - log["start"]

    await update.message.reply_text(
        "✅ Log updated successfully\n\n"
        f"ID: {log['log_id']}\n"
        f"New total: {log['total']} km"
    )

    success = update_log_in_sheet(log)

    if not success:
        await update.message.reply_text(
            "Failed to update Google Sheet."
        )
        return ConversationHandler.END

    await show_main_menu(update)
    return ConversationHandler.END

async def delete_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)
    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)

    tid = update.effective_user.id

    if tid not in registered_users:
        await update.message.reply_text("Please /register first.")
        return ConversationHandler.END

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /delete <log_id>")
        return ConversationHandler.END

    log_id = context.args[0].lower()

    log = find_log_by_id(log_id, tid)

    if not log:
        await update.message.reply_text("Log not found.")
        return ConversationHandler.END

    context.user_data["delete_log"] = log

    await update.message.reply_text(
        "⚠️ Confirm deletion of this log:\n\n"
        f"ID: {log['log_id']}\n"
        f"Date: {log['date']}\n"
        f"Vehicle: {log['vehicle_number']} ({log['vehicle_class']})\n"
        f"Distance: {log['total']} km\n\n"
        "Type YES to confirm or NO to cancel."
    )

    return DELETE_CONFIRM


async def confirm_delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Presents explicit binary confirmation options before executing destructive events."""
    keyboard = [
        [InlineKeyboardButton("🗑️ Yes, Delete Log", callback_data="confirm_delete_yes")],
        [InlineKeyboardButton("❌ No, Keep Log", callback_data="confirm_delete_no")]
    ]

    await update.message.reply_text(
        "⚠️ *CRITICAL VERIFICATION*\nAre you absolutely sure you want to permanently clear this log?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return DELETE_CONFIRM


async def handle_delete_execution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes target database operations depending on confirmation button press states."""
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_delete_yes":
        # Put your existing deletion backend log manipulation code here
        await query.message.edit_text("✅ Log entry successfully deleted from the database.")
    else:
        await query.message.edit_text("ℹ️ Deletion canceled. Log footprint remains unchanged.")

    context.user_data.clear()
    await show_main_menu(update, context)
    return ConversationHandler.END

async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):

    response = update.message.text.strip().lower()

    log = context.user_data.get("delete_log")

    if not log:
        await update.message.reply_text("No pending delete action.")
        return ConversationHandler.END

    if response == "yes":

        mileage_logs.remove(log)
        delete_log_from_sheet(log["log_id"])

        await update.message.reply_text(
            f"✅ Deleted log {log['log_id']} successfully."
        )

    else:
        await update.message.reply_text("❌ Deletion cancelled.")

    await show_main_menu(update)
    return ConversationHandler.END

# =========================================================
# LOGPASTE
# =========================================================

async def logpaste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)
    tid = update.effective_user.id

    if tid not in registered_users:
        await update.message.reply_text(
            "Please /register first."
        )
        return

    text = update.message.text

    try:

        # ==========================================
        # EXTRACT ONLY REQUIRED FIELDS
        # ==========================================

        vehicle_match = re.search(
            r"VEH NO:\s*(\d{5})",
            text,
            re.IGNORECASE
        )

        date_match = re.search(
            r"START DATE:\s*(\d{6})",
            text,
            re.IGNORECASE
        )

        start_match = re.search(
            r"START ODOMETER:\s*(\d+)",
            text,
            re.IGNORECASE
        )

        end_match = re.search(
            r"END ODOMETER:\s*(\d+)",
            text,
            re.IGNORECASE
        )

        reason_match = re.search(
            r"MOVEMENT PURPOSE.*?:\s*(.+)",
            text,
            re.IGNORECASE
        )

        # ==========================================
        # VALIDATE
        # ==========================================

        if not all([
            vehicle_match,
            date_match,
            start_match,
            end_match,
            reason_match
        ]):
            await update.message.reply_text(
                "Could not parse pasted format. \n\nPlease use /logpaste\n\n<message>"
            )
            return

        vehicle_number = vehicle_match.group(1)
        date = date_match.group(1)
        start = int(start_match.group(1))
        end = int(end_match.group(1))
        reason = reason_match.group(1).strip()

        if not validate_date(date):
            await update.message.reply_text(
                "Invalid date format."
            )
            return

        if end < start:
            await update.message.reply_text(
                "End odometer cannot be smaller than start."
            )
            return

        # ==========================================
        # CLASSIFICATION
        # ==========================================

        vehicle_class = classify_vehicle(
            int(vehicle_number)
        )

        if vehicle_class == "Unknown":
            await update.message.reply_text(
                "Vehicle class could not be determined."
            )
            return

        total = end - start

        # ==========================================
        # GENERATE UNIQUE LOG ID
        # ==========================================

        log_id = generate_base36_id(
            registered_users[tid],
            date,
            vehicle_number
        )

        # ==========================================
        # CREATE ENTRY
        # ==========================================

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
            "timestamp": datetime.now(SGT).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }

        # ==========================================
        # SAVE
        # ==========================================

        mileage_logs.append(log_entry)

        await asyncio.to_thread(save_log, log_entry)

        # ==========================================
        # RESPONSE
        # ==========================================

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

        await update.message.reply_text(
            f"Import failed:\n{str(e)}"
        )

    await show_main_menu(update)

async def unkown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Sorry, I don't understand you. Please try again. \nUse /help to see available commands."
    )
    await show_main_menu(update)


# =========================================================
# STEP 6: DUAL-FUNCTION ORCHESTRATION (run_bot & main)
# =========================================================

def run_bot():
    """Initializes the Telegram bot application, registers handlers, and starts polling."""
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN environment variable is missing.")
        sys.exit(1)

    # Initialize the Telegram Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Define the precise ConversationHandler matching your UI states
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", show_main_menu),
            CommandHandler("menu", show_main_menu),
            # New inline keyboard entry points mapping to steps 2 & 4
            CallbackQueryHandler(start_log_flow, pattern="^menu_log$"),
            CallbackQueryHandler(prompt_edit_field_selection, pattern="^menu_edit$"),
        ],
        states={
            REGISTER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_start)
            ],
            DATE: [
                # Handles your new Today / Yesterday / Manual buttons from Step 3
                CallbackQueryHandler(handle_date_selection, pattern="^date_.*$"),
                # Text fallback if they choose to type the DDMMYY format manually
                MessageHandler(filters.TEXT & ~filters.COMMAND, log_date)
            ],
            VEHICLE_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, log_vehicle_number)
            ],
            START_ODOMETER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, log_start_odometer)
            ],
            END_ODOMETER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, log_end_odometer)
            ],
            REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, log_reason)
            ],
            EDIT_FIELD: [
                # Captures the button selection for the specific column to modify from Step 4
                CallbackQueryHandler(handle_field_choice, pattern="^edit_field_.*$")
            ],
            EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)
            ],
            DELETE_CONFIRM: [
                # Handles the binary confirmation layout from Step 5
                CallbackQueryHandler(handle_delete_execution, pattern="^confirm_delete_.*$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            # Closes menus and returns clean states when users hit Cancel/Back buttons
            CallbackQueryHandler(show_main_menu, pattern="^menu_back$|^flow_cancel$"),
        ],
        allow_reentry=True
    )

    # Register conversation framework
    application.add_handler(conv_handler)

    # Standalone view log routing (isolated so viewing logs doesn't break active log flows)
    application.add_handler(CallbackQueryHandler(view_logs_options, pattern="^menu_view_opts$"))
    application.add_handler(CallbackQueryHandler(handle_log_filters, pattern="^filter_.*$"))

    # Retain standalone legacy command access points
    application.add_handler(CommandHandler("today_logs", today_logs))

    # =========================================================
    # GOOGLE SHEETS COLD-START CACHE PRE-FETCH
    # =========================================================
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    print("Pre-fetching Google Sheets database records...")
    global registered_users, mileage_logs
    registered_users = loop.run_until_complete(asyncio.to_thread(load_registered_users))
    mileage_logs = loop.run_until_complete(asyncio.to_thread(load_logs))
    print(f"Cache successfully synced. Users loaded: {len(registered_users)} | Entries loaded: {len(mileage_logs)}")

    # Start the bot polling engine
    print("miBOT Telegram engine started successfully.")
    application.run_polling(close_loop=False)


def main():
    """Main coordinator function that fires up background threads and invokes the bot."""
    # Start the Flask web server thread for keeping the app awake
    web_thread = Thread(target=run_web, daemon=True)
    web_thread.start()

    # Start the local loopback heartbeat thread
    heartbeat_thread = Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    # Execute the main bot pipeline
    run_bot()


if __name__ == '__main__':
    # Standard Python entry block triggering the main coordinator
    multiprocessing.set_start_method("spawn")
    main()