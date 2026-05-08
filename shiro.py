# Shiro Telegram Bot - Shopify checker
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
import telebot
from telebot import types, apihelper

# ── Dynamic Imports ──
from shopifyapi import format_proxy, load_proxy_list, check_site_fast, run_shopify_check
from stripeapi import try_checkout_card, fetch_checkout_info

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
    def _st_check_sync(*a, **kw): return {"status": "Error", "message": "stripecharge not installed"}
    def _st_format_ui(*a, **kw): return "stripecharge not installed"

# ── Shared Loop ──
try:
    import uvloop
    _shared_loop = uvloop.new_event_loop()
except ImportError:
    _shared_loop = asyncio.new_event_loop()

def _start_shared_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=_start_shared_loop, args=(_shared_loop,), daemon=True).start()

# ── Configuration & Logger ──
DEBUG = os.environ.get("DEBUG", "true").lower() in ("1", "true", "yes")

class ShiroLogger:
    def success(self, cat, msg): print(f"[\033[92m✓\033[0m] {cat}: {msg}")
    def info(self, cat, msg): print(f"[\033[94mi\033[0m] {cat}: {msg}")
    def warning(self, cat, msg): print(f"[\033[93m!\033[0m] {cat}: {msg}")
    def error(self, cat, msg): print(f"[\033[91m✗\033[0m] {cat}: {msg}")

log = ShiroLogger()

# ── Database Setup ──
MONGO_URI = os.environ.get("SHIRO_MONGO_URI")
INITIAL_CREDITS = 100
_mongo_client = None

def _get_mongo():
    global _mongo_client
    if not MONGO_URI: return None, None
    try:
        from pymongo import MongoClient
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = _mongo_client["shiro"]
        return db, db["chats"]
    except: return None, None

def _users_coll():
    db, _ = _get_mongo()
    return db["users"] if db is not None else None

def _codes_coll():
    db, _ = _get_mongo()
    return db["credit_codes"] if db is not None else None

# ── Functional Credit Logic ──
def redeem_credit_code(user_id, code):
    codes_col = _codes_coll()
    users_col = _users_coll()
    if not codes_col or not users_col: return False, "DB Error", 0
    
    code_doc = codes_col.find_one_and_update(
        {"code": code.upper(), "used_by": {"$ne": user_id}, "$expr": {"$lt": ["$used_count", "$max_uses"]}},
        {"$inc": {"used_count": 1}, "$push": {"used_by": user_id}}
    )
    if not code_doc: return False, "Invalid/Used Code", 0
    
    credits = code_doc.get("credits", 0)
    users_col.update_one({"user_id": user_id}, {"$inc": {"credits": credits}}, upsert=True)
    return True, f"Added {credits} credits", credits

# ── Bot Initialization ──
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=16)

def _crash_safe(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try: return func(*args, **kwargs)
        except Exception as e:
            log.error('Crash', f'{func.__name__}: {e}')
            traceback.print_exc()
    return wrapper

# ── Handlers ──
@bot.message_handler(commands=['start'])
@_crash_safe
def cmd_start(message):
    bot.reply_to(message, "<b>Shiro Bot Online</b>\nUse /sh [card] to check.", parse_mode="HTML")

@bot.message_handler(commands=['redeem'])
@_crash_safe
def cmd_redeem(message):
    code = message.text.split()[1] if len(message.text.split()) > 1 else None
    if not code: return bot.reply_to(message, "Usage: /redeem [code]")
    success, msg, _ = redeem_credit_code(message.from_user.id, code)
    bot.reply_to(message, msg)

# ── Polling & Shutdown ──
_shutdown_event = threading.Event()

def _graceful_shutdown(signum, frame):
    _shutdown_event.set()
    bot.stop_polling()
    if _mongo_client: _mongo_client.close()
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)

if __name__ == "__main__":
    log.info('System', 'Starting Shiro Process...')
    
    while not _shutdown_event.is_set():
        try:
            bot.infinity_polling(timeout=30, skip_pending=True, none_stop=True)
        except Exception as e:
            log.error('Loop', f'Restarting due to: {e}')
            time.sleep(5)

    # FINAL CLEANUP (Fixes your SyntaxError)
    try:
        if '_aio_session' in globals() and _aio_session and not _aio_session.closed:
            asyncio.run_coroutine_threadsafe(_aio_session.close(), _shared_loop).result(timeout=3)
    except Exception:
        pass
    log.success('Shutdown', 'Bot stopped')
