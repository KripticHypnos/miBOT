# STABLE VER: 1115AM 280526 + FEEDBACK FEATURE + CACHE/MEMSTATS
import logging
import os
import re
import sys
import math
from flask import Flask
from threading import Thread
import requests
import string
import time
import asyncio
import multiprocessing
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

try:
    # Unix-only (Render runs Linux) - used by /memstats. Not available on
    # Windows, so memstats degrades gracefully there instead of crashing.
    import resource
except ImportError:
    resource = None

app_flask = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)


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
    raw = f"{user_id}{date}{vehicle}{int(time.time() * 1000)}"
    numeric_seed = abs(hash(raw))
    return to_base36(numeric_seed)


# =========================================================
# ENV
# =========================================================

# ENV_FILE lets you point at a different .env when running locally, e.g.:
#   ENV_FILE=.env.test python mileage_bot.py
# Falls back to ".env" (used by both plain local runs and most hosts).
# Note: load_dotenv() never overwrites variables the host has already
# injected (e.g. Render's dashboard env vars), so this is safe in prod too.
load_dotenv(os.getenv("ENV_FILE", ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

# RUN_ENV controls environment-specific behaviour (see main()).
# Set RUN_ENV=local in your test .env file so the bot skips the
# Flask keep-alive server/heartbeat that only matters for hosted deploys.
RUN_ENV = os.getenv("RUN_ENV", "production").strip().lower()

# SHEET_NAME lets test and prod point at completely separate Google Sheets
# (and therefore completely separate data) using only an env var.
SHEET_NAME = os.getenv("SHEET_NAME", "MileageBotDB")

# =========================================================
# GOOGLE SHEETS
# =========================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Two ways to supply credentials, checked in this order:
#   1. GOOGLE_CREDS  - a raw JSON string in an env var (how Render is set up)
#   2. GOOGLE_CREDS_FILE - path to a local JSON key file (defaults to
#      "credentials.json" in the project folder, for local/test runs)
google_creds = os.getenv("GOOGLE_CREDS")
google_creds_file = os.getenv("GOOGLE_CREDS_FILE", "credentials.json")

if google_creds:
    creds_json = json.loads(google_creds)
elif os.path.exists(google_creds_file):
    with open(google_creds_file, "r") as f:
        creds_json = json.load(f)
else:
    raise ValueError(
        "No Google credentials found. Set the GOOGLE_CREDS env var (raw JSON), "
        f"or place a service account key file at '{google_creds_file}' "
        "(or set GOOGLE_CREDS_FILE to point at it)."
    )

creds = Credentials.from_service_account_info(
    creds_json,
    scopes=SCOPES
)

authed_session = AuthorizedSession(creds)
authed_session.configure_mtls_channel = False

client = gspread.Client(auth=creds, session=authed_session)

sheet = client.open(SHEET_NAME)

users_sheet = sheet.worksheet("users")
logs_sheet = sheet.worksheet("logs")
log_helper_sheet = sheet.worksheet("log_helper")
master_sheet = sheet.worksheet("master_users")
# NOTE: Create a worksheet named "feedback" in the MileageBotDB spreadsheet
# with header row: feedback_id | telegram_id | user_id | category | message | status | timestamp
feedback_sheet = sheet.worksheet("feedback")

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
master_users = {}     # telegram_id -> {user_id, name}
mileage_logs = []
log_index = {}         # log_id -> log dict (same object references as in mileage_logs)
feedback_entries = []

# Drive API modifiedTime of the whole spreadsheet, as of the last time our
# cache was known to match it. Used by ensure_logs_fresh() to detect manual
# edits made directly on the sheet. See STALENESS CHECK section below.
last_known_modified_time = None


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


def load_master_users():
    try:
        data = master_sheet.get_all_records()
    except Exception as e:
        print(f"Failed to load master users: {e}")
        return {}

    masters = {}
    for row in data:
        if not str(row.get("telegram_id", "")).strip():
            continue
        try:
            masters[int(row["telegram_id"])] = {
                "user_id": row.get("user_id", ""),
                "name": row.get("name", ""),
            }
        except (ValueError, KeyError):
            continue
    return masters


def is_master(telegram_id: int) -> bool:
    return telegram_id in master_users


def find_log_by_id_admin(log_id: str):
    return log_index.get(log_id)


def save_user(telegram_id, user_id, name):
    try:
        users_sheet.append_row([
            telegram_id,
            user_id,
            name
        ])
        mark_cache_synced()
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
        mark_cache_synced()
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
            "date": formatted_date,
            "vehicle_number": str(row["vehicle_number"]),
            "vehicle_class": row["vehicle_class"],
            "start": int(row["start"]),
            "end": int(row["end"]),
            "total": int(row["total"]),
            "reason": row["reason"],
            "timestamp": row["timestamp"],
        })

    return logs


def save_feedback(entry):
    try:
        feedback_sheet.append_row([
            entry["feedback_id"],
            entry["telegram_id"],
            entry["user_id"],
            entry["category"],
            entry["message"],
            entry["status"],
            entry["timestamp"],
        ])
        mark_cache_synced()
    except Exception as e:
        print("SAVE FEEDBACK ERROR:", e)


def load_feedback():
    try:
        rows = feedback_sheet.get_all_records()
    except Exception as e:
        print("LOAD FEEDBACK ERROR:", e)
        return []

    entries = []
    for row in rows:
        if not str(row.get("feedback_id", "")).strip():
            continue
        try:
            entries.append({
                "feedback_id": row["feedback_id"],
                "telegram_id": int(row["telegram_id"]),
                "user_id": row.get("user_id", ""),
                "category": row.get("category", "General"),
                "message": row.get("message", ""),
                "status": (row.get("status") or "open").strip().lower(),
                "timestamp": row.get("timestamp", ""),
            })
        except (ValueError, KeyError):
            continue

    return entries


def find_feedback_by_id(feedback_id: str):
    return next(
        (f for f in feedback_entries if f["feedback_id"] == feedback_id),
        None
    )


def update_feedback_status_in_sheet(feedback_id: str, status: str) -> bool:
    try:
        records = feedback_sheet.get_all_values()
        for idx, row in enumerate(records[1:], start=2):
            if str(row[0]).strip().lower() == str(feedback_id).strip().lower():
                feedback_sheet.update(
                    range_name=f"F{idx}",
                    values=[[status]],
                    value_input_option="RAW"
                )
                mark_cache_synced()
                return True
    except Exception as e:
        print("UPDATE FEEDBACK STATUS FAILED:", e)
    return False


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
REGISTER_NAME = 13
ADMIN_ANNOUNCE = 14
ADMIN_ANNOUNCE_CONFIRM = 15
ADMIN_SCHEDULE_MSG = 16
ADMIN_SCHEDULE_TIME = 17
ADMIN_VIEW_USER = 18
ADMIN_EDIT_LOG = 19
ADMIN_DELETE_LOG = 20
ADMIN_FORCE_REG_TID = 21
ADMIN_FORCE_REG_UID = 22
ADMIN_FORCE_REG_NAME = 23
ADMIN_LOG_TARGET = 24
ADMIN_LOGPASTE_TARGET = 25
FEEDBACK_CATEGORY = 26
FEEDBACK_TEXT = 27


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


def parse_date_input(raw: str):
    """
    Accepts a wide range of manually-typed date formats and normalises
    them to DDMMYY (the format the rest of the app/sheet expects):
      - 140526              (DDMMYY, no separator)
      - 14/05/26, 14/05/2026
      - 14-05-26, 14-05-2026
      - 14.05.26, 14.05.2026
      - 14 05 26, 14 5 2026
      - 14 May 26, 14 May 2026, 14 MAY 2026
    """
    if not raw:
        return None

    raw = raw.strip()
    raw = re.sub(r"\s+", " ", raw)

    # DDMMYY with no separator
    m = re.match(r"^(\d{2})(\d{2})(\d{2})$", raw)
    if m:
        normalised = "".join(m.groups())
        if validate_date(normalised):
            return normalised

    # DD<sep>MM<sep>YY(YY) where sep is /, -, ., or space
    m = re.match(r"^(\d{1,2})[/\-.\s](\d{1,2})[/\-.\s](\d{2}|\d{4})$", raw)
    if m:
        dd, mm, yy = m.groups()
        dd, mm = dd.zfill(2), mm.zfill(2)
        if len(yy) == 4:
            yy = yy[2:]
        normalised = dd + mm + yy
        if validate_date(normalised):
            return normalised

    # DD <Month name/abbrev> YY(YY), also accepts - or . as separators
    m = re.match(r"^(\d{1,2})[\s\-.]+([A-Za-z]+)[\s\-.]+(\d{2}|\d{4})$", raw)
    if m:
        dd, mon, yy = m.groups()
        dd = dd.zfill(2)
        for fmt in ("%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y"):
            try:
                parsed = datetime.strptime(f"{dd} {mon} {yy}", fmt)
                return parsed.strftime("%d%m%y")
            except ValueError:
                continue

    # Last-ditch: strip out any non-alphanumeric separators and retry DDMMYY
    compact = re.sub(r"[^\dA-Za-z]", "", raw)
    if re.match(r"^\d{6}$", compact) and validate_date(compact):
        return compact

    return None


def validate_vehicle_number(vn: str) -> bool:
    return bool(re.match(r"^\d{5}$", vn))


def parse_vehicle_number(raw: str):
    """Pulls the numeric vehicle number out of a value that may carry an
    alpha prefix/suffix (e.g. 'MID 33928', 'SBA-33928', '33928')."""
    if not raw:
        return None
    m = re.search(r"(\d{4,6})", raw)
    return m.group(1) if m else None


def parse_odometer(raw: str):
    """Strips commas/spaces/units from an odometer reading and returns an int."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    return int(digits)


def extract_field(text: str, label_pattern: str):
    """Grabs everything after 'LABEL:' on its own line, tolerant of
    leading/trailing whitespace and gaps around the colon."""
    m = re.search(
        rf"^[ \t]*{label_pattern}[ \t]*:[ \t]*(.*)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not m:
        return None
    value = m.group(1).strip()
    return value if value else None


def parse_logpaste_block(text: str):
    """
    Parses a pasted PLN mileage log block, tolerant of:
      - variable whitespace around colons / between header and value
      - vehicle numbers with alpha prefixes (e.g. 'MID 33928')
      - odometer readings with commas/spaces
      - multiple date formats (see parse_date_input)
    Returns (data_dict, None) on success, or (None, error_message) on failure.
    NRIC/RANK/NAME are intentionally not parsed, matching current output
    field selection.
    """
    raw_vehicle = extract_field(text, r"VEH(?:ICLE)?\s*NO\.?")
    raw_start_date = extract_field(text, r"START\s*DATE")
    raw_start_odo = extract_field(text, r"START\s*ODO(?:METER)?")
    raw_end_odo = extract_field(text, r"END\s*ODO(?:METER)?")
    raw_reason = extract_field(text, r"MOVEMENT\s*PURPOSE[^:]*")

    missing = []
    if not raw_vehicle:
        missing.append("VEH NO")
    if not raw_start_date:
        missing.append("START DATE")
    if not raw_start_odo:
        missing.append("START ODOMETER")
    if not raw_end_odo:
        missing.append("END ODOMETER")
    if not raw_reason:
        missing.append("MOVEMENT PURPOSE")
    if missing:
        return None, f"Could not find field(s): {', '.join(missing)}. Please check the pasted text."

    vehicle_number = parse_vehicle_number(raw_vehicle)
    if not vehicle_number:
        return None, f"Could not read a valid vehicle number from '{raw_vehicle}'."

    date = parse_date_input(raw_start_date)
    if not date:
        return None, f"Could not read a valid start date from '{raw_start_date}'."

    start = parse_odometer(raw_start_odo)
    end = parse_odometer(raw_end_odo)
    if start is None:
        return None, f"Could not read a valid start odometer from '{raw_start_odo}'."
    if end is None:
        return None, f"Could not read a valid end odometer from '{raw_end_odo}'."

    return {
        "vehicle_number": vehicle_number,
        "date": date,
        "start": start,
        "end": end,
        "reason": raw_reason.strip(),
    }, None


# =========================================================
# CLASSIFICATION (EXCEL LOGIC)
# =========================================================

def classify_vehicle(c: int) -> str:
    if 11000 < c < 24999:
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


def load_training_totals(telegram_id):
    try:
        raw = log_helper_sheet.get("L1:P1000")
        if not raw or len(raw) < 2:
            return 0, 0
        headers = [str(h).strip() for h in raw[0]]
        rows = [dict(zip(headers, row)) for row in raw[1:] if row]
    except Exception as e:
        print(f"Failed to load course totals: {e}")
        return 0, 0

    for row in rows:
        try:
            if int(row["telegram_id"]) == telegram_id:
                return int(row.get("class_3DC") or 0), int(row.get("class_4DC") or 0)
        except (ValueError, KeyError):
            continue
    return 0, 0


def find_log_by_id(log_id: str, tid: int):
    log = log_index.get(log_id)
    if log and log["telegram_id"] == tid:
        return log
    return None


def register_log_in_cache(log_entry):
    """Add a newly-created log to the in-memory cache + index.
    Call this instead of mileage_logs.append(...) directly so the index
    stays in sync without needing a full sheet reload."""
    global mileage_logs
    mileage_logs.append(log_entry)
    log_index[log_entry["log_id"]] = log_entry


def remove_log_from_cache(log_id):
    """Remove a deleted log from the in-memory cache + index."""
    global mileage_logs
    log_index.pop(log_id, None)
    mileage_logs = [log for log in mileage_logs if log["log_id"] != log_id]


def rebuild_log_index():
    """Rebuilds log_index from the current mileage_logs list. Called after
    any full reload (startup, or a manual /refreshcache)."""
    global log_index
    log_index = {log["log_id"]: log for log in mileage_logs}


# =========================================================
# STALENESS CHECK (edit/delete-flow cache freshness)
# =========================================================
#
# The "logs" sheet is also a live, human-edited display, not just a
# backend DB - so the in-memory cache can go stale if someone edits it
# directly outside the bot. Drive/Sheets APIs don't expose a per-tab
# modified time, only a spreadsheet-wide one, so we use that as a signal
# and scope reloads to `mileage_logs` (the only tab meant to be a live
# human-edited display; the others still rely on /refreshcache).
#
# Design (edit-wins rule): ensure_logs_fresh() is only called once, at
# the *start* of an edit/delete flow - never re-checked mid-flow - and
# the flow's own write at the end is an unconditional overwrite. So a
# manual sheet edit made mid-flow gets overwritten by the bot's own
# edit, never silently merged or discarded.

def get_sheet_modified_time():
    """Cheap metadata-only Drive API call - returns the spreadsheet's
    modifiedTime, or None if the call fails for any reason. Callers must
    treat None as "unknown, fail open" and proceed on the existing cache
    rather than block the user."""
    try:
        resp = authed_session.get(
            f"https://www.googleapis.com/drive/v3/files/{sheet.id}",
            params={"fields": "modifiedTime"},
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json().get("modifiedTime")
    except Exception as e:
        print(f"STALENESS CHECK: failed to fetch modifiedTime: {e}")
        return None


def mark_cache_synced(timestamp=None):
    """Records the spreadsheet's current modifiedTime as the last-known-
    good value. Call this after any full reload AND after every bot-
    initiated write (any tab) - otherwise the bot's own writes would be
    mistaken for external drift on the next staleness check.

    Pass `timestamp` if you already have a fresh value (e.g. from
    ensure_logs_fresh's own fetch) to avoid a redundant API call;
    otherwise this fetches it itself."""
    global last_known_modified_time
    last_known_modified_time = timestamp if timestamp is not None else get_sheet_modified_time()


def ensure_logs_fresh():
    """Call once at the start of an edit/delete flow, before looking up
    the target log. If the sheet has been modified since our cache was
    last known to match it, reloads `mileage_logs` + rebuilds the index.

    Fails open: any Drive API error is swallowed and the existing cache
    is used as-is, so a transient API hiccup never blocks a user's flow.
    """
    global mileage_logs

    current = get_sheet_modified_time()
    if current is None:
        return  # fail open - proceed on whatever cache we have

    if current != last_known_modified_time:
        print("STALENESS CHECK: sheet changed externally - reloading logs cache")
        mileage_logs = load_logs()
        rebuild_log_index()

    mark_cache_synced(current)


LOG_PAGE_SIZE = 10  # entries per page - keeps each message safely under
                    # Telegram's 4096-char limit even with long reasons


def format_log_entry(i: int, log: dict) -> str:
    return (
        f"{i}. LOG ID: `{log['log_id']}`\n"
        f"Date: {log['date']}\n"
        f"Vehicle: {log['vehicle_number']} ({log['vehicle_class']})\n"
        f"Odometer: {log['start']} → {log['end']}\n"
        f"Distance: {log['total']} km\n"
        f"Reason: {log['reason']}\n\n"
    )


def render_log_page(context: ContextTypes.DEFAULT_TYPE):
    """Builds the text + keyboard for the current page of context.user_data's
    stored 'page_results'. Clamps the stored page index into range and
    always includes an Exit button, with Previous/Next shown only when
    there's somewhere to go."""
    results = context.user_data.get("page_results", [])
    title = context.user_data.get("page_title", "Logs")

    total_pages = max(1, math.ceil(len(results) / LOG_PAGE_SIZE))
    page = context.user_data.get("page_index", 0)
    page = max(0, min(page, total_pages - 1))
    context.user_data["page_index"] = page

    start = page * LOG_PAGE_SIZE
    page_results = results[start:start + LOG_PAGE_SIZE]

    msg = f"📊 *{title}* (Page {page + 1}/{total_pages})\n\n"
    for i, log in enumerate(page_results, start=start + 1):
        msg += format_log_entry(i, log)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Previous", callback_data="logpage_prev"))
    nav_row.append(InlineKeyboardButton("🚪 Exit", callback_data="logpage_exit"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data="logpage_next"))

    return msg, InlineKeyboardMarkup([nav_row])


async def handle_log_page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data  # "logpage_prev" | "logpage_next" | "logpage_exit"

    if action == "logpage_exit":
        context.user_data.pop("page_results", None)
        context.user_data.pop("page_title", None)
        context.user_data.pop("page_index", None)
        await query.message.delete()
        await show_main_menu(update)
        return

    page = context.user_data.get("page_index", 0)
    if action == "logpage_prev":
        context.user_data["page_index"] = page - 1
    elif action == "logpage_next":
        context.user_data["page_index"] = page + 1

    msg, markup = render_log_page(context)
    await query.message.edit_text(msg, reply_markup=markup, parse_mode="Markdown")


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
                mark_cache_synced()
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
                mark_cache_synced()
                break
    except Exception as e:
        print("DELETE FAILED:", e)


# =========================================================
# COMMANDS
# =========================================================

async def error_handler(update, context):
    tb = "".join(traceback.format_exception(
        type(context.error),
        context.error,
        context.error.__traceback__
    ))
    logger.error("Exception while handling update:\n%s", tb)

    # Previously this only logged to console, so failures (e.g. a message
    # exceeding Telegram's 4096-char limit) were completely silent to the
    # user - they'd tap a button and see nothing happen at all. Now we at
    # least tell them something went wrong, with a one-tap way to report it.
    if isinstance(update, Update) and update.effective_chat:
        keyboard = [[InlineKeyboardButton("📝 Report this issue", callback_data="report_error")]]
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Something went wrong processing that. Please try again — "
                     "if it keeps happening, you can report it below.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            pass  # avoid a failure loop if even this message can't be sent


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
        "/feedback - Report an issue or send feedback\n"

    )


# =========================================================
# MAIN OPTIONS DISPLAY
# =========================================================

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    global master_users
    master_users = await asyncio.to_thread(load_master_users)
    tid = update.effective_user.id
    if is_master(tid):
        await show_admin_menu(update)
        return

    keyboard = [
        [InlineKeyboardButton("📝 Log Mileage", callback_data="menu_log"),
         InlineKeyboardButton("📋 Paste PLN Log", callback_data="menu_logpaste")],
        [InlineKeyboardButton("📋 View Logs", callback_data="menu_view_opts"),
         InlineKeyboardButton("📊 View Totals", callback_data="menu_mytotal")],
        [InlineKeyboardButton("✏️ Edit Log", callback_data="menu_edit"),
         InlineKeyboardButton("❌ Delete Log", callback_data="menu_delete")],
        [InlineKeyboardButton("💬 Feedback / Report Issue", callback_data="menu_feedback")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🤖 *miBOT Main Menu*\nSelect an action from the options below:"

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


# =========================================================
# ADMIN MENU & FLOWS
# =========================================================

async def show_admin_menu(update: Update):
    keyboard = [
        [InlineKeyboardButton("📝 Log Mileage", callback_data="admin_log"),
         InlineKeyboardButton("📋 Paste PLN Log", callback_data="admin_logpaste")],
        [InlineKeyboardButton("📋 View Logs", callback_data="menu_view_opts"),
         InlineKeyboardButton("📊 My Total", callback_data="menu_mytotal")],
        [InlineKeyboardButton("✏️ Edit Log", callback_data="admin_edit_log"),
         InlineKeyboardButton("🗑 Delete Log", callback_data="admin_delete_log")],
        [InlineKeyboardButton("👁 View User", callback_data="admin_view_user"),
         InlineKeyboardButton("👤 Force Register", callback_data="admin_force_reg")],
        [InlineKeyboardButton("📢 Announce", callback_data="admin_announce"),
         InlineKeyboardButton("🕐 Schedule", callback_data="admin_schedule")],
        [InlineKeyboardButton("💬 Feedback", callback_data="menu_feedback"),
         InlineKeyboardButton("📬 View Feedback", callback_data="admin_view_feedback")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🔐 *Admin Menu*\nSelect an action:"

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def check_master(update: Update) -> bool:
    global master_users, registered_users
    master_users = await asyncio.to_thread(load_master_users)
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

    if not is_master(tid):
        text = "⛔ You do not have admin access."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text)
        else:
            await update.message.reply_text(text)
        return False

    return True


# --- Announce (immediate) ---

async def admin_start_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "📢 *Immediate Announcement*\n\nEnter the message to broadcast to all users:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_ANNOUNCE


async def admin_announce_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    context.user_data["announce_msg"] = msg
    user_count = len(registered_users)
    keyboard = [
        [InlineKeyboardButton(f"✅ Send to {user_count} users", callback_data="admin_confirm_announce")],
        [InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]
    ]
    await update.message.reply_text(
        f"📋 *Preview:*\n\n{msg}\n\n─────────────────\nSend to *{user_count}* registered users?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_ANNOUNCE_CONFIRM


async def admin_confirm_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg = context.user_data.get("announce_msg", "")
    if not msg:
        await query.message.reply_text("❌ No message found. Please try again.")
        return ConversationHandler.END

    sent = 0
    failed = 0
    for tid in list(registered_users.keys()):
        try:
            await context.bot.send_message(chat_id=tid, text=f"📢 *Announcement*\n\n{msg}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1

    await query.message.reply_text(f"✅ Sent to {sent} users." + (f" ({failed} failed)" if failed else ""))
    context.user_data.clear()
    await show_admin_menu(update)
    return ConversationHandler.END


# --- Schedule announcement ---

async def admin_start_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "🕐 *Schedule Announcement*\n\nEnter the message to schedule:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_SCHEDULE_MSG


async def admin_schedule_get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["scheduled_msg"] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await update.message.reply_text(
        "📅 Enter send date and time as *DDMMYY HHMM* (SGT):\ne.g. 270626 0800",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_SCHEDULE_TIME


async def admin_schedule_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    try:
        scheduled_dt = datetime.strptime(raw, "%d%m%y %H%M").replace(tzinfo=SGT)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid format. Use DDMMYY HHMM (e.g. 270626 0800):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_SCHEDULE_TIME

    if scheduled_dt <= datetime.now(SGT):
        await update.message.reply_text(
            "❌ Scheduled time must be in the future:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_SCHEDULE_TIME

    msg = context.user_data.get("scheduled_msg", "")
    tid = update.effective_user.id
    admin_uid = master_users.get(tid, {}).get("user_id", "unknown")

    try:
        scheduled_sheet = sheet.worksheet("scheduled_announcements")
        scheduled_sheet.append_row([
            generate_base36_id(admin_uid, scheduled_dt.strftime("%d%m%y"), "ANN"),
            msg,
            scheduled_dt.strftime("%d/%m/%y %H:%M"),
            "pending",
            admin_uid
        ])
        await update.message.reply_text(
            f"✅ Scheduled for *{scheduled_dt.strftime('%d %b %Y %H:%M')} SGT*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to save schedule: {e}")

    context.user_data.clear()
    await show_admin_menu(update)
    return ConversationHandler.END


# --- View any user's totals ---

async def admin_start_view_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "👁 *View User Totals*\n\nEnter the User ID to view (e.g. 123A):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_VIEW_USER


async def admin_view_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip().upper()
    target_tid = next((tid for tid, u in registered_users.items() if u == uid), None)

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]]
    if not target_tid:
        await update.message.reply_text(
            f"❌ User ID *{uid}* not found.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    c3, c4, _ = calculate_totals(target_tid)
    dc3, dc4 = await asyncio.to_thread(load_training_totals, target_tid)
    total = (c3 + dc3) + (c4 + dc4)

    text = (
        f"📊 *{uid} Totals*\n\n"
        f"Class 3: {c3 + dc3} km" + (f" _(+{dc3} Driving Course)_" if dc3 > 0 else "") + "\n"
        f"Class 4: {c4 + dc4} km" + (f" _(+{dc4} Driving Course)_" if dc4 > 0 else "") + "\n\n"
        f"*Total: {total} km*"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END


# --- Admin edit any log ---

async def admin_start_edit_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "✏️ *Admin Edit Log*\n\nEnter the LOG ID to edit:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_EDIT_LOG


async def admin_process_edit_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_id = update.message.text.strip().lower()
    log = find_log_by_id_admin(log_id)
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not log:
        await update.message.reply_text(
            "❌ Log ID not found. Enter a valid LOG ID:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_EDIT_LOG

    context.user_data["edit_log"] = log
    return await prompt_field_selection(update.message, context)


# --- Admin delete any log ---

async def admin_start_delete_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "🗑 *Admin Delete Log*\n\nEnter the LOG ID to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_DELETE_LOG


async def admin_process_delete_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_id = update.message.text.strip().lower()
    log = find_log_by_id_admin(log_id)
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not log:
        await update.message.reply_text(
            "❌ Log ID not found. Enter a valid LOG ID:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_DELETE_LOG

    context.user_data["delete_log"] = log
    return await prompt_delete_confirmation(update.message, log)


# --- Force register ---

async def admin_start_force_reg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "👤 *Force Register User*\n\nEnter the Telegram ID of the user:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_FORCE_REG_TID


async def admin_force_reg_get_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    try:
        target_tid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid Telegram ID. Must be numeric:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_FORCE_REG_TID

    if target_tid in registered_users:
        await update.message.reply_text(
            f"⚠️ Telegram ID {target_tid} is already registered as *{registered_users[target_tid]}*.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ADMIN_FORCE_REG_TID

    context.user_data["force_reg_tid"] = target_tid
    await update.message.reply_text(
        "Enter the user_id for this user (e.g. 123A):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_FORCE_REG_UID


async def admin_force_reg_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip().upper()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not validate_user_id(uid):
        await update.message.reply_text(
            "❌ Invalid format. Use 123A:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_FORCE_REG_UID

    if uid in registered_users.values():
        await update.message.reply_text(
            "❌ User ID already taken. Try another:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_FORCE_REG_UID

    context.user_data["force_reg_uid"] = uid
    await update.message.reply_text(
        "Enter the user's name:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_FORCE_REG_NAME


async def admin_force_reg_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not name:
        await update.message.reply_text(
            "❌ Name cannot be empty:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ADMIN_FORCE_REG_NAME

    target_tid = context.user_data.get("force_reg_tid")
    uid = context.user_data.get("force_reg_uid")

    registered_users[target_tid] = uid
    save_user(target_tid, uid, name)
    context.user_data.clear()

    await update.message.reply_text(f"✅ Registered *{uid}* — {name} (TID: `{target_tid}`)", parse_mode="Markdown")
    await show_admin_menu(update)
    return ConversationHandler.END


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
    if context.user_data:
        context.user_data.clear()

    global master_users
    master_users = await asyncio.to_thread(load_master_users)
    tid = update.effective_user.id

    if is_master(tid):
        text = "❌ *Action Terminated.*"
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.message.edit_text(text, parse_mode="Markdown")
            except Exception:
                await update.callback_query.message.reply_text(text, parse_mode="Markdown")
        await show_admin_menu(update)
        return ConversationHandler.END

    text = "❌ *Action Terminated.*\n\n🤖 *miBOT Main Menu*\nSelect an action from the options below:"
    keyboard = [
        [InlineKeyboardButton("📝 Log Mileage", callback_data="menu_log"),
         InlineKeyboardButton("📋 Paste PLN Log", callback_data="menu_logpaste")],
        [InlineKeyboardButton("📋 View Logs", callback_data="menu_view_opts"),
         InlineKeyboardButton("📊 View Totals", callback_data="menu_mytotal")],
        [InlineKeyboardButton("✏️ Edit Log", callback_data="menu_edit"),
         InlineKeyboardButton("❌ Delete Log", callback_data="menu_delete")],
        [InlineKeyboardButton("💬 Feedback / Report Issue", callback_data="menu_feedback")],
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
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)
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

    # Store user_id temporarily, proceed to name step
    context.user_data["pending_user_id"] = user_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await update.message.reply_text("Enter your name:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REGISTER_NAME


async def register_save_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    name = update.message.text.strip()
    user_id = context.user_data.get("pending_user_id")

    if not user_id:
        await update.message.reply_text("Something went wrong. Please /register again.")
        return ConversationHandler.END

    if not name:
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
        await update.message.reply_text("Name cannot be empty. Please enter your name:", reply_markup=InlineKeyboardMarkup(keyboard))
        return REGISTER_NAME

    registered_users[tid] = user_id
    save_user(tid, user_id, name)
    context.user_data.clear()

    await update.message.reply_text(f"✅ Registered: {user_id} — {name}")
    await show_main_menu(update)
    return ConversationHandler.END



# =========================================================
# ADMIN LOG & LOGPASTE FLOWS
# =========================================================

async def admin_start_log_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "📝 *Admin Log Mileage*\n\nEnter the User ID to log for (e.g. 123A):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_LOG_TARGET


async def admin_log_set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip().upper()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    target_tid = next((tid for tid, u in registered_users.items() if u == uid), None)
    if not target_tid:
        await update.message.reply_text(
            f"❌ User ID *{uid}* not found. Enter a valid User ID:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ADMIN_LOG_TARGET
    context.user_data["admin_target_tid"] = target_tid
    context.user_data["admin_target_uid"] = uid
    keyboard2 = [
        [InlineKeyboardButton("📆 Today", callback_data="date_today"),
         InlineKeyboardButton("⏳ Yesterday", callback_data="date_yesterday")],
        [InlineKeyboardButton("⌨️ Enter Manually", callback_data="date_manual")],
        [InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]
    ]
    await update.message.reply_text(
        f"Logging for *{uid}*\n\nStep 1: Select the log date:",
        reply_markup=InlineKeyboardMarkup(keyboard2),
        parse_mode="Markdown"
    )
    return DATE


async def admin_log_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    start = context.user_data["start"]
    end = context.user_data["end"]
    total = end - start

    target_tid = context.user_data["admin_target_tid"]
    target_uid = context.user_data["admin_target_uid"]

    log_id = generate_base36_id(
        target_uid,
        context.user_data["date"],
        context.user_data["vehicle_number"]
    )
    log_entry = {
        "log_id": log_id,
        "telegram_id": target_tid,
        "user_id": target_uid,
        "date": context.user_data["date"],
        "vehicle_number": context.user_data["vehicle_number"],
        "vehicle_class": context.user_data["vehicle_class"],
        "start": start,
        "end": end,
        "total": total,
        "reason": reason,
        "timestamp": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S"),
    }
    register_log_in_cache(log_entry)
    await asyncio.to_thread(save_log, log_entry)
    await update.message.reply_text(
        f"✅ Logged for *{target_uid}*\n\n"
        f"LOG ID: {log_id}\n"
        f"Date: {log_entry['date']}\n"
        f"Vehicle: {log_entry['vehicle_number']}\n"
        f"Class: {log_entry['vehicle_class']}\n"
        f"Distance: {total} km\n"
        f"Reason: {reason}",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    await show_admin_menu(update)
    return ConversationHandler.END


async def admin_start_logpaste_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_master(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "📋 *Admin Paste PLN Log*\n\nEnter the User ID to log for (e.g. 123A):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADMIN_LOGPASTE_TARGET


async def admin_logpaste_set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip().upper()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    target_tid = next((tid for tid, u in registered_users.items() if u == uid), None)
    if not target_tid:
        await update.message.reply_text(
            f"❌ User ID *{uid}* not found. Enter a valid User ID:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ADMIN_LOGPASTE_TARGET
    context.user_data["admin_target_tid"] = target_tid
    context.user_data["admin_target_uid"] = uid
    await update.message.reply_text(
        f"Logging for *{uid}*\n\n📋 Paste the PLN log block below:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return PASTE_TEXT


async def admin_process_logpaste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await admin_execute_logpaste_logic(update, context, text)
    return ConversationHandler.END


async def admin_execute_logpaste_logic(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    target_tid = context.user_data.get("admin_target_tid")
    target_uid = context.user_data.get("admin_target_uid")

    try:
        data, parse_error = parse_logpaste_block(text)
        if parse_error:
            await update.message.reply_text(f"Could not parse pasted format: {parse_error}")
            await show_admin_menu(update)
            return

        vehicle_number = data["vehicle_number"]
        date = data["date"]
        start = data["start"]
        end = data["end"]
        reason = data["reason"]

        if end < start:
            await update.message.reply_text("End odometer cannot be smaller than start odometer.")
            await show_admin_menu(update)
            return

        vehicle_class = classify_vehicle(int(vehicle_number))
        if vehicle_class == "Unknown":
            await update.message.reply_text("Vehicle class could not be matched automatically.")
            await show_admin_menu(update)
            return

        total = end - start
        log_id = generate_base36_id(target_uid, date, vehicle_number)

        log_entry = {
            "log_id": log_id,
            "telegram_id": target_tid,
            "user_id": target_uid,
            "date": date,
            "vehicle_number": vehicle_number,
            "vehicle_class": vehicle_class,
            "start": start,
            "end": end,
            "total": total,
            "reason": reason,
            "timestamp": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S"),
        }
        register_log_in_cache(log_entry)
        await asyncio.to_thread(save_log, log_entry)

        await update.message.reply_text(
            f"✅ Log imported for *{target_uid}*\n\n"
            f"LOG ID: {log_id}\n"
            f"Date: {date}\n"
            f"Vehicle: {vehicle_number}\n"
            f"Class: {vehicle_class}\n"
            f"Distance: {total} km\n"
            f"Reason: {reason}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Import failed:\n{str(e)}")

    context.user_data.clear()
    await show_admin_menu(update)


# =========================================================
# LOG MILEAGE FLOW
# =========================================================

async def start_log_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if context.user_data.get("admin_target_tid"):
        return await admin_log_reason(update, context)

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

    register_log_in_cache(log_entry)
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
    if context.user_data.get("admin_target_tid"):
        await admin_execute_logpaste_logic(update, context, text)
        return ConversationHandler.END
    await execute_logpaste_logic(update, context, text)
    return ConversationHandler.END


async def execute_logpaste_logic(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    global registered_users
    registered_users = await asyncio.to_thread(load_registered_users)
    tid = update.effective_user.id

    try:
        data, parse_error = parse_logpaste_block(text)
        if parse_error:
            await update.message.reply_text(
                f"Could not parse pasted format: {parse_error}\nPlease try again from the menu layout option.")
            await show_main_menu(update)
            return

        vehicle_number = data["vehicle_number"]
        date = data["date"]
        start = data["start"]
        end = data["end"]
        reason = data["reason"]

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

        register_log_in_cache(log_entry)
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
    if not await check_registered(update):
        return ConversationHandler.END

    await asyncio.to_thread(ensure_logs_fresh)

    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.edit_text("✏️ Please type the **LOG ID** of the record you wish to modify:",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return GET_EDIT_ID


async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    await asyncio.to_thread(ensure_logs_fresh)

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

    await show_main_menu(update)
    return ConversationHandler.END


# =========================================================
# DELETE FLOW
# =========================================================

async def start_delete_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_registered(update):
        return ConversationHandler.END

    await asyncio.to_thread(ensure_logs_fresh)

    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.edit_text("❌ Please type the **LOG ID** of the record you wish to delete:",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return GET_DELETE_ID


async def delete_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    await asyncio.to_thread(ensure_logs_fresh)

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
        remove_log_from_cache(log["log_id"])
        await query.message.edit_text(f"✅ Log entry `{log['log_id']}` successfully deleted.")
    await show_main_menu(update, context)
    return ConversationHandler.END


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response = update.message.text.strip().lower()
    log = context.user_data.get("delete_log")

    if response == "yes" and log:
        await asyncio.to_thread(delete_log_from_sheet, log["log_id"])
        remove_log_from_cache(log["log_id"])
        await update.message.reply_text(f"✅ Deleted log {log['log_id']} successfully.")
    else:
        await update.message.reply_text("❌ Deletion cancelled.")

    context.user_data.clear()
    await show_main_menu(update)
    return ConversationHandler.END


# =========================================================
# FEEDBACK FLOW
# =========================================================

async def start_feedback_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /feedback command and the 'Feedback' menu button."""
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        msg_obj = query.message
    else:
        msg_obj = update.message

    if not await check_registered(update):
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🐞 Bug Report", callback_data="fb_cat_Bug"),
         InlineKeyboardButton("💡 Feature Request", callback_data="fb_cat_Feature")],
        [InlineKeyboardButton("💬 General Feedback", callback_data="fb_cat_General")],
        [InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]
    ]
    text = "💬 *Feedback / Report an Issue*\n\nWhat would you like to share?"

    if update.callback_query:
        try:
            await msg_obj.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception:
            await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return FEEDBACK_CATEGORY


async def start_error_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the '📝 Report this issue' button attached to error
    messages. Skips category selection (pre-set to Bug) and goes straight
    to the text prompt, reusing the existing FEEDBACK_TEXT state handler."""
    query = update.callback_query
    await query.answer()

    if not await check_registered(update):
        return ConversationHandler.END

    context.user_data["feedback_category"] = "Bug"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.reply_text(
        "🐞 *Report Issue*\n\n"
        "Please describe what you were doing when this happened "
        "(e.g. which button/command, and what you expected instead):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return FEEDBACK_TEXT


async def feedback_set_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("fb_cat_", "")
    context.user_data["feedback_category"] = category

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]
    await query.message.edit_text(
        f"Category: *{category}*\n\n"
        "Please describe your issue or feedback in as much detail as you can "
        "(e.g. what you were doing, what you expected, what happened instead):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return FEEDBACK_TEXT


async def feedback_save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    message_text = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")]]

    if not message_text:
        await update.message.reply_text(
            "Feedback message cannot be empty. Please describe your issue or feedback:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return FEEDBACK_TEXT

    category = context.user_data.get("feedback_category", "General")
    user_id = registered_users.get(tid, "UNKNOWN")
    feedback_id = generate_base36_id(user_id, datetime.now(SGT).strftime("%d%m%y"), "FB")

    entry = {
        "feedback_id": feedback_id,
        "telegram_id": tid,
        "user_id": user_id,
        "category": category,
        "message": message_text,
        "status": "open",
        "timestamp": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S"),
    }

    global feedback_entries
    feedback_entries.append(entry)
    await asyncio.to_thread(save_feedback, entry)

    await update.message.reply_text(
        "✅ Thanks — your feedback has been submitted and the team has been notified.\n\n"
        f"Reference ID: `{feedback_id}`",
        parse_mode="Markdown"
    )

    # Notify all admins/masters immediately
    global master_users
    master_users = await asyncio.to_thread(load_master_users)
    notify_text = (
        f"📬 *New Feedback — {category}*\n\n"
        f"From: {user_id} (TID: `{tid}`)\n"
        f"Ref: `{feedback_id}`\n\n"
        f"{message_text}\n\n"
        f"_Reply with_ `/resolvefeedback {feedback_id}` _once addressed._"
    )
    for admin_tid in master_users.keys():
        try:
            await context.bot.send_message(chat_id=admin_tid, text=notify_text, parse_mode="Markdown")
        except Exception:
            pass

    context.user_data.clear()
    await show_main_menu(update)
    return ConversationHandler.END


async def admin_view_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin menu button — lists open feedback items."""
    if not await check_master(update):
        return

    global feedback_entries
    feedback_entries = await asyncio.to_thread(load_feedback)

    query = update.callback_query
    await query.answer()

    open_items = [f for f in feedback_entries if f["status"] == "open"]
    open_items.sort(key=lambda f: f["timestamp"], reverse=True)

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]]

    if not open_items:
        await query.message.reply_text(
            "✅ No open feedback items.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    msg = f"📬 *Open Feedback ({len(open_items)})*\n\n"
    for i, f in enumerate(open_items[:15], 1):
        msg += (
            f"{i}. `{f['feedback_id']}` [{f['category']}] — {f['user_id']}\n"
            f"{f['message'][:200]}\n"
            f"_{f['timestamp']}_\n\n"
        )
    if len(open_items) > 15:
        msg += f"...and {len(open_items) - 15} more. Use /feedbacklist for the full list."

    msg += "\nUse `/resolvefeedback <id>` to mark an item resolved."

    await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def feedback_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/feedbacklist — full list of open feedback (admin only), text command version."""
    if not await check_master(update):
        return

    global feedback_entries
    feedback_entries = await asyncio.to_thread(load_feedback)

    open_items = [f for f in feedback_entries if f["status"] == "open"]
    open_items.sort(key=lambda f: f["timestamp"], reverse=True)

    if not open_items:
        await update.message.reply_text("✅ No open feedback items.")
        return

    msg = f"📬 *Open Feedback ({len(open_items)})*\n\n"
    for i, f in enumerate(open_items, 1):
        msg += (
            f"{i}. `{f['feedback_id']}` [{f['category']}] — {f['user_id']}\n"
            f"{f['message'][:300]}\n"
            f"_{f['timestamp']}_\n\n"
        )
        # Telegram messages cap around 4096 chars; flush in chunks if needed
        if len(msg) > 3500:
            await update.message.reply_text(msg, parse_mode="Markdown")
            msg = ""

    if msg:
        await update.message.reply_text(msg, parse_mode="Markdown")


async def resolve_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resolvefeedback <feedback_id> — admin only."""
    if not await check_master(update):
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /resolvefeedback <feedback_id>")
        return

    feedback_id = context.args[0].strip().lower()
    success = await asyncio.to_thread(update_feedback_status_in_sheet, feedback_id, "resolved")

    global feedback_entries
    feedback_entries = await asyncio.to_thread(load_feedback)

    if success:
        await update.message.reply_text(f"✅ Feedback `{feedback_id}` marked as resolved.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Feedback ID not found.")


# =========================================================
# ADMIN OPS: CACHE REFRESH & MEMORY STATS
# =========================================================

async def refresh_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/refreshcache — admin only. Manual full resync from Google Sheets.

    Not needed in normal operation (the bot keeps mileage_logs/log_index
    in sync on every write), but useful as a recovery option if the
    'logs' sheet is ever edited directly outside the bot and the cache
    drifts out of sync.
    """
    if not await check_master(update):
        return

    global mileage_logs
    mileage_logs = await asyncio.to_thread(load_logs)
    rebuild_log_index()

    await update.message.reply_text(
        f"✅ Cache refreshed from sheet. {len(mileage_logs)} logs loaded, "
        f"{len(log_index)} indexed."
    )


async def memstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/memstats — admin only. Reports current process memory + cache sizes,
    so actual Render usage can be watched instead of estimated."""
    if not await check_master(update):
        return

    if resource is not None:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is KB on Linux (Render), bytes on macOS.
        rss_mb = raw / 1024 if sys.platform != "darwin" else raw / (1024 * 1024)
        mem_line = f"Peak RSS: *{rss_mb:.1f} MB*"
    else:
        mem_line = "Peak RSS: _unavailable on this platform (Windows)_"

    text = (
        "📊 *Memory Stats*\n\n"
        f"{mem_line}\n\n"
        f"Cached logs: {len(mileage_logs)}\n"
        f"Cached log index: {len(log_index)}\n"
        f"Cached registered users: {len(registered_users)}\n"
        f"Cached master users: {len(master_users)}\n"
        f"Cached feedback: {len(feedback_entries)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


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
        return GET_DATE_FILTER

    if not results:
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        await query.message.edit_text(f"ℹ️ No logs found under: *{title}*", reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode="Markdown")
        return

    results.sort(key=lambda log: datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S"), reverse=True)

    context.user_data["page_results"] = results
    context.user_data["page_title"] = title
    context.user_data["page_index"] = 0

    msg, markup = render_log_page(context)
    await query.message.edit_text(msg, reply_markup=markup, parse_mode="Markdown")


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

    context.user_data["page_results"] = results
    context.user_data["page_title"] = f"Logs for {value}"
    context.user_data["page_index"] = 0

    msg, markup = render_log_page(context)
    await update.message.reply_text(msg, reply_markup=markup, parse_mode="Markdown")
    return ConversationHandler.END


async def my_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_registered(update):
        return

    tid = update.effective_user.id
    c3, c4, _ = calculate_totals(tid)
    dc3, dc4 = await asyncio.to_thread(load_training_totals, tid)

    total_c3 = c3 + dc3
    total_c4 = c4 + dc4
    total = total_c3 + total_c4

    text = (
        "📊 *Mileage Totals*\n\n"
        f"Class 3: {total_c3} km"
        + (f" _(+{dc3} Driving Course)_" if dc3 > 0 else "") + "\n"
        f"Class 4: {total_c4} km"
        + (f" _(+{dc4} Driving Course)_" if dc4 > 0 else "") + "\n\n"
        f"*Total: {total} km*"
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

    context.user_data["page_results"] = results
    context.user_data["page_title"] = title
    context.user_data["page_index"] = 0

    msg, markup = render_log_page(context)
    await update.message.reply_text(msg, reply_markup=markup, parse_mode="Markdown")


async def today_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    today = datetime.now(SGT).strftime("%d%m%y")

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
            CommandHandler("feedback", start_feedback_flow),
            CallbackQueryHandler(start_log_flow, pattern="^menu_log$"),
            CallbackQueryHandler(start_edit_flow, pattern="^menu_edit$"),
            CallbackQueryHandler(start_delete_flow, pattern="^menu_delete$"),
            CallbackQueryHandler(start_logpaste_flow, pattern="^menu_logpaste$"),
            CallbackQueryHandler(start_feedback_flow, pattern="^menu_feedback$"),
            CallbackQueryHandler(start_error_report, pattern="^report_error$"),
            CallbackQueryHandler(handle_log_filters, pattern="^filter_date_manual$"),
            CallbackQueryHandler(admin_start_announce, pattern="^admin_announce$"),
            CallbackQueryHandler(admin_start_schedule, pattern="^admin_schedule$"),
            CallbackQueryHandler(admin_start_view_user, pattern="^admin_view_user$"),
            CallbackQueryHandler(admin_start_edit_log, pattern="^admin_edit_log$"),
            CallbackQueryHandler(admin_start_delete_log, pattern="^admin_delete_log$"),
            CallbackQueryHandler(admin_start_force_reg, pattern="^admin_force_reg$"),
            CallbackQueryHandler(admin_start_log_flow, pattern="^admin_log$"),
            CallbackQueryHandler(admin_start_logpaste_flow, pattern="^admin_logpaste$"),
        ],
        states={
            REGISTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_save)],
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_save_name)],
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
            ADMIN_ANNOUNCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_announce_preview)],
            ADMIN_ANNOUNCE_CONFIRM: [CallbackQueryHandler(admin_confirm_announce, pattern="^admin_confirm_announce$")],
            ADMIN_SCHEDULE_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_schedule_get_time)],
            ADMIN_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_schedule_confirm)],
            ADMIN_VIEW_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_view_user)],
            ADMIN_EDIT_LOG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_process_edit_log)],
            ADMIN_DELETE_LOG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_process_delete_log)],
            ADMIN_FORCE_REG_TID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_force_reg_get_uid)],
            ADMIN_FORCE_REG_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_force_reg_get_name)],
            ADMIN_FORCE_REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_force_reg_save)],
            ADMIN_LOG_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_log_set_target)],
            ADMIN_LOGPASTE_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_logpaste_set_target)],
            FEEDBACK_CATEGORY: [CallbackQueryHandler(feedback_set_category, pattern="^fb_cat_.*$")],
            FEEDBACK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_save_message)],
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
    application.add_handler(CommandHandler("resolvefeedback", resolve_feedback))
    application.add_handler(CommandHandler("feedbacklist", feedback_list_command))
    application.add_handler(CommandHandler("refreshcache", refresh_cache))
    application.add_handler(CommandHandler("memstats", memstats))

    application.add_handler(CallbackQueryHandler(view_logs_options, pattern="^menu_view_opts$"))
    application.add_handler(CallbackQueryHandler(handle_log_filters, pattern="^filter_.*$"))
    application.add_handler(CallbackQueryHandler(handle_log_page_nav, pattern="^logpage_(prev|next|exit)$"))
    application.add_handler(CallbackQueryHandler(my_total, pattern="^menu_mytotal$"))
    application.add_handler(CallbackQueryHandler(show_main_menu, pattern="^menu_back$"))
    application.add_handler(CallbackQueryHandler(admin_start_announce, pattern="^admin_announce$"))
    application.add_handler(CallbackQueryHandler(admin_start_schedule, pattern="^admin_schedule$"))
    application.add_handler(CallbackQueryHandler(admin_start_view_user, pattern="^admin_view_user$"))
    application.add_handler(CallbackQueryHandler(admin_start_edit_log, pattern="^admin_edit_log$"))
    application.add_handler(CallbackQueryHandler(admin_start_delete_log, pattern="^admin_delete_log$"))
    application.add_handler(CallbackQueryHandler(admin_start_force_reg, pattern="^admin_force_reg$"))
    application.add_handler(CallbackQueryHandler(admin_start_log_flow, pattern="^admin_log$"))
    application.add_handler(CallbackQueryHandler(admin_start_logpaste_flow, pattern="^admin_logpaste$"))
    application.add_handler(CallbackQueryHandler(admin_view_feedback, pattern="^admin_view_feedback$"))

    application.add_error_handler(error_handler)
    application.add_handler(MessageHandler(filters.COMMAND, unkown_command))

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    print("Pre-fetching Google Sheets database records...")
    global registered_users, master_users, mileage_logs, feedback_entries
    registered_users = loop.run_until_complete(asyncio.to_thread(load_registered_users))
    master_users = loop.run_until_complete(asyncio.to_thread(load_master_users))
    mileage_logs = loop.run_until_complete(asyncio.to_thread(load_logs))
    rebuild_log_index()
    feedback_entries = loop.run_until_complete(asyncio.to_thread(load_feedback))
    loop.run_until_complete(asyncio.to_thread(mark_cache_synced))
    print(f"Cache synced. Users: {len(registered_users)} | Masters: {len(master_users)} | "
          f"Entries: {len(mileage_logs)} | Feedback: {len(feedback_entries)}")

    print("miBOT Engine Ready.")
    application.run_polling(close_loop=False,
                            drop_pending_updates=True)


def main():
    print(f"RUN_ENV: {RUN_ENV} | Sheet: {SHEET_NAME}")

    if RUN_ENV == "local":
        # No hosting platform to keep awake when running on your own machine,
        # and binding port 10000 locally is unnecessary/can collide with
        # other local services - skip both.
        print("Local test run: skipping Flask keep-alive server + heartbeat.")
    else:
        Thread(target=run_web, daemon=True).start()

    run_bot()

if __name__ == '__main__':
    multiprocessing.set_start_method("spawn")
    main()