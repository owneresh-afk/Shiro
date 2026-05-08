#!/usr/bin/env python3
"""
Shiro Shopify API v5.1 - RENDER OPTIMIZED
Modified for 24/7 Hosting and UptimeRobot support
"""

import os
import time
import threading
import queue
import random
from flask import Flask, request, jsonify
from datetime import datetime
from shopifyapi import format_proxy, ShopifyAuto

# Check if solver is available
try:
    from captcha_solver import (
        _get_or_create_browser, _kill_browser, _full_browser_checkout,
        _do_full_browser_checkout_sync, logger
    )
    SOLVER_ENABLED = True
except ImportError:
    SOLVER_ENABLED = False

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG (Optimized for Render Free Tier)
# ══════════════════════════════════════════════════════════════════════════════
NUM_WORKERS = 2  # Reduced from 8 to save RAM on Render
TIMEOUT = 120  
MAX_QUEUE = 200

# ══════════════════════════════════════════════════════════════════════════════
# WORKER POOL
# ══════════════════════════════════════════════════════════════════════════════
_queue = queue.Queue()
_results = {}
_events = {}
_rlock = threading.Lock()
_running = False
_busy = [False] * NUM_WORKERS
_browser_lock = threading.Lock() 

def _do_check(site, cc, mon, yr, cvv, info):
    if not SOLVER_ENABLED:
        return {"status": "Error", "message": "Solver components missing"}
    
    with _browser_lock:
        try:
            browser = _get_or_create_browser()
            if not browser:
                return {"status": "Error", "message": "No browser available"}
            
            ok = _full_browser_checkout(browser, site)
            if not ok:
                return {"status": "Error", "message": "Checkout blocked", "error_code": "CAPTCHA_REQUIRED"}
            
            result = _do_full_browser_checkout_sync(cc, mon, yr, cvv, info)
            return result
            
        except Exception as e:
            err = str(e)[:80]
            if "session" in err.lower() or "disconnect" in err.lower():
                try: _kill_browser()
                except: pass
            return {"status": "Error", "message": err}

def _worker(wid):
    global _running
    while _running:
        try:
            req = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        
        rid, site, cc, mon, yr, cvv, info = req
        _busy[wid] = True
        
        try:
            res = _do_check(site, cc, mon, yr, cvv, info)
        except Exception as e:
            res = {"status": "Error", "message": str(e)[:60]}
        
        _busy[wid] = False
        
        with _rlock:
            _results[rid] = res
            if rid in _events:
                _events[rid].set()
        _queue.task_done()

def _start_workers():
    global _running
    if _running:
        return
    _running = True
    for i in range(NUM_WORKERS):
        threading.Thread(target=_worker, args=(i,), daemon=True).start()

def _check(site, card, proxy=None):
    _start_workers()
    
    p = card.strip().replace(" ", "").split("|")
    if len(p) != 4:
        return {"status": "Error", "message": "Bad format"}
    cc, mon, yr, cvv = p
    if len(yr) == 2: yr = "20" + yr
    
    rid = f"{time.time()}_{random.randint(1000,9999)}"
    ev = threading.Event()
    
    with _rlock:
        _events[rid] = ev
    
    info = ShopifyAuto().get_random_info()
    _queue.put((rid, site, cc, mon, yr, cvv, info))
    
    if ev.wait(timeout=TIMEOUT):
        with _rlock:
            _events.pop(rid, None)
            return _results.pop(rid, {"status": "Error", "message": "No result"})
    
    with _rlock:
        _events.pop(rid, None)
        _results.pop(rid, None)
    return {"status": "Error", "message": "Timeout"}

# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return jsonify({
        "name": "Shiro API",
        "status": "Online",
        "workers": NUM_WORKERS,
        "note": "24/7 Keep-alive active"
    })

@app.route('/health')
def health():
    """UptimeRobot should ping this endpoint"""
    return jsonify({
        "status": "ok", 
        "v": "5.1-render",
        "busy_workers": sum(_busy),
        "queue_size": _queue.qsize()
    }), 200

@app.route('/shopify', methods=['GET'])
def shopify():
    site = request.args.get('site', '').strip()
    cc = request.args.get('cc', '').strip()
    proxy = request.args.get('proxy', '').strip()
    
    if not site or not cc or cc.count('|') != 3:
        return jsonify({"status": "Error", "message": "Bad params"}), 400
    
    if _queue.qsize() > MAX_QUEUE:
        return jsonify({"status": "Error", "message": "Server Busy", "error_code": "SERVER_BUSY"})
    
    t0 = time.time()
    res = _check(site, cc, format_proxy(proxy) if proxy else None)
    elapsed = time.time() - t0
    
    return jsonify({
        "status": res.get("status", "Error"),
        "message": res.get("message", ""),
        "time": f"{elapsed:.1f}s",
        "site": site.replace("https://","").replace("http://","").split("/")[0]
    })

if __name__ == "__main__":
    _start_workers()
    # Get port from Render environment variable
    port = int(os.environ.get("PORT", 5000))
    print(f"[*] Shiro API starting on port {port}")
    app.run(host='0.0.0.0', port=port)
_running = False
_busy = [False] * NUM_WORKERS
_browser_lock = threading.Lock()  # Serialize browser access


def _do_check(site, cc, mon, yr, cvv, info):
    """Run browser checkout."""
    with _browser_lock:  # Only one browser operation at a time
        try:
            browser = _get_or_create_browser()
            if not browser:
                return {"status": "Error", "message": "No browser"}
            
            # Fast checkout: products.json -> cart -> checkout
            ok = _full_browser_checkout(browser, site)
            if not ok:
                return {"status": "Error", "message": "Checkout blocked", "error_code": "CAPTCHA_REQUIRED"}
            
            # Fill form and pay
            result = _do_full_browser_checkout_sync(cc, mon, yr, cvv, info)
            return result
            
        except Exception as e:
            err = str(e)[:80]
            logger.error(f"Check error: {err}")
            if "session" in err.lower() or "disconnect" in err.lower():
                try:
                    _kill_browser()
                except:
                    pass
            return {"status": "Error", "message": err}


def _worker(wid):
    global _running
    print(f"[W{wid}] Ready")
    
    while _running:
        try:
            req = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        
        rid, site, cc, mon, yr, cvv, info = req
        _busy[wid] = True
        
        try:
            res = _do_check(site, cc, mon, yr, cvv, info)
        except Exception as e:
            res = {"status": "Error", "message": str(e)[:60]}
        
        _busy[wid] = False
        
        with _rlock:
            _results[rid] = res
            if rid in _events:
                _events[rid].set()
        
        _queue.task_done()


def _start():
    global _running
    if _running:
        return
    _running = True
    for i in range(NUM_WORKERS):
        threading.Thread(target=_worker, args=(i,), daemon=True).start()
    print(f"[API] ✅ {NUM_WORKERS} workers started")


def _check(site, card, proxy=None):
    _start()
    
    p = card.strip().replace(" ", "").split("|")
    if len(p) != 4:
        return {"status": "Error", "message": "Bad format"}
    cc, mon, yr, cvv = p
    if len(yr) == 2:
        yr = "20" + yr
    
    rid = f"{time.time()}_{random.randint(1000,9999)}"
    ev = threading.Event()
    
    with _rlock:
        _events[rid] = ev
    
    info = ShopifyAuto().get_random_info()
    _queue.put((rid, site, cc, mon, yr, cvv, info))
    
    if ev.wait(timeout=TIMEOUT):
        with _rlock:
            _events.pop(rid, None)
            return _results.pop(rid, {"status": "Error", "message": "No result"})
    
    with _rlock:
        _events.pop(rid, None)
        _results.pop(rid, None)
    return {"status": "Error", "message": "Timeout"}


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/shopify', methods=['GET'])
def shopify():
    site = request.args.get('site', '').strip()
    cc = request.args.get('cc', '').strip()
    proxy = request.args.get('proxy', '').strip()
    
    if not site or not cc or cc.count('|') != 3:
        return jsonify({"status": "Error", "message": "Bad params"}), 400
    
    qs = _queue.qsize()
    if qs > MAX_QUEUE:
        return jsonify({"status": "Error", "message": f"Busy ({qs})", "error_code": "SERVER_BUSY"})
    
    t0 = time.time()
    res = _check(site, cc, format_proxy(proxy) if proxy else None)
    elapsed = time.time() - t0
    
    out = {
        "status": res.get("status", "Error"),
        "message": res.get("message", ""),
        "error_code": res.get("error_code", ""),
        "price": res.get("price", ""),
        "product": res.get("product", ""),
        "gateway": "Shopify Payments",
        "time": f"{elapsed:.1f}s",
        "site": site.replace("https://","").replace("http://","").split("/")[0]
    }
    
    mask = cc[:6] + "****" + cc.split("|")[0][-4:] if len(cc) > 10 else "****"
    busy = sum(_busy)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {out['status']} | {mask} | {elapsed:.1f}s | q:{qs} w:{busy}")
    
    return jsonify(out)


@app.route('/health')
def health():
    return jsonify({
        "status": "ok", "v": "5.1",
        "workers": NUM_WORKERS,
        "busy": sum(_busy),
        "queue": _queue.qsize()
    })


@app.route('/')
def index():
    return jsonify({"name": "Shiro API", "v": "5.1", "workers": NUM_WORKERS})


# Pre-warm browser
def _prewarm():
    try:
        b = _get_or_create_browser()
        if b:
            logger.info("🔥 Browser pre-warmed")
    except Exception as e:
        logger.error(f"Pre-warm failed: {e}")


if __name__ == '__main__':
    _prewarm()
    _start()
    print(f"[Shiro API] v5.1 | {NUM_WORKERS} workers | port 5000")
    app.run(host='0.0.0.0', port=5000, threaded=True)
          
