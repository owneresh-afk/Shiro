import asyncio
import random
import time as _time
import httpx
import re
import json
from datetime import datetime
from urllib.parse import urlparse, quote
import sys


try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# ── Selenium CAPTCHA solver (auto-bypass Shopify checkpoint) ──────────
_CAPTCHA_SOLVER_AVAILABLE = False
try:
    from captcha_solver import solve_shopify_captcha, is_solver_available, get_solver_status, get_cached_cookies
    _CAPTCHA_SOLVER_AVAILABLE = True
    print("[shopifyapi] \u2705 Selenium CAPTCHA solver loaded")
except ImportError:
    print("[shopifyapi] \u26a0\ufe0f captcha_solver not available \u2014 CAPTCHA bypass disabled")

# ── curl_cffi: Chrome TLS fingerprint impersonation ──────────────────────
# This is the #1 anti-CAPTCHA measure — Shopify/Cloudflare detect httpx's
# TLS fingerprint (JA3/JA4) instantly. curl_cffi sends a real Chrome TLS
# handshake so the server thinks it's a genuine browser.
_CURL_CFFI_AVAILABLE = False
try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    _CURL_CFFI_AVAILABLE = True
    print("[shopifyapi] ✅ curl_cffi loaded — Chrome TLS fingerprint active")
except ImportError:
    print("[shopifyapi] ⚠️ curl_cffi not installed — using httpx (CAPTCHA risk higher)")

# Chrome impersonation versions — MUST match _FP_CHROME_VERSIONS to avoid TLS/UA mismatch
# Only profiles supported by curl_cffi 0.14.0: chrome136, chrome133a, chrome131, chrome124, chrome123, chrome120
_CHROME_TO_IMPERSONATE = {"136": "chrome136", "133": "chrome133a", "131": "chrome131", "124": "chrome124", "123": "chrome123", "120": "chrome120"}
_CURL_IMPERSONATE = list(_CHROME_TO_IMPERSONATE.values())

# Check HTTP/2 support once at module load (only used as httpx fallback)
_H2_AVAILABLE = False
try:
    import h2  # noqa: F401
    _H2_AVAILABLE = True
except ImportError:
    pass


class _CurlSessionWrapper:
    """Wraps curl_cffi AsyncSession to match httpx.AsyncClient interface.
    curl_cffi methods return Response objects with .status_code, .text, .json(), .url etc.
    """
    def __init__(self, session):
        self._s = session

    @staticmethod
    def _clean_headers(headers):
        """Ensure all header values are ASCII-safe (curl_cffi crashes on non-ASCII bytes)."""
        if not headers:
            return headers
        cleaned = {}
        for k, v in headers.items():
            if isinstance(v, str):
                cleaned[k] = v.encode('ascii', errors='ignore').decode('ascii')
            else:
                cleaned[k] = v
        return cleaned

    async def get(self, url, **kwargs):
        if 'headers' in kwargs:
            kwargs['headers'] = self._clean_headers(kwargs['headers'])
        return await self._s.get(url, **kwargs)

    async def post(self, url, **kwargs):
        if 'headers' in kwargs:
            kwargs['headers'] = self._clean_headers(kwargs['headers'])
        return await self._s.post(url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._s.close()


def _create_async_client(proxy_url=None, timeout=30.0, chrome_version=None):
    """Create an async HTTP client — curl_cffi (Chrome TLS) preferred, httpx fallback.
    chrome_version: e.g. '136' — matched to curl_cffi impersonation for TLS/UA consistency."""
    if _CURL_CFFI_AVAILABLE:
        # Match TLS impersonation to fingerprint Chrome version (prevents TLS/UA mismatch detection)
        if chrome_version and chrome_version in _CHROME_TO_IMPERSONATE:
            impersonate = _CHROME_TO_IMPERSONATE[chrome_version]
        else:
            impersonate = random.choice(_CURL_IMPERSONATE)
        kw = {
            "impersonate": impersonate,
            "allow_redirects": True,
            "timeout": timeout,
            "verify": False,
        }
        if proxy_url:
            kw["proxy"] = proxy_url
        session = _CurlAsyncSession(**kw)
        return _CurlSessionWrapper(session)
    else:
        # Fallback to httpx
        client_kw = {
            "follow_redirects": True,
            "timeout": httpx.Timeout(timeout, connect=8.0, read=25.0, write=8.0, pool=5.0),
            "limits": httpx.Limits(max_connections=100, max_keepalive_connections=20),
            "http2": _H2_AVAILABLE,
        }
        if proxy_url:
            client_kw["proxy"] = proxy_url
        return httpx.AsyncClient(**client_kw)

# ── Network error tuple (works with both curl_cffi and httpx) ──────────
_NETWORK_ERRORS = (
    httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout,
    httpx.ProxyError, httpx.ConnectTimeout, httpx.WriteTimeout,
    httpx.TimeoutException,
    ConnectionResetError, ConnectionAbortedError, OSError,
)
# Add curl_cffi errors if available
if _CURL_CFFI_AVAILABLE:
    try:
        from curl_cffi.requests.errors import RequestsError as _CurlRequestsError
        _NETWORK_ERRORS = _NETWORK_ERRORS + (_CurlRequestsError,)
    except ImportError:
        pass

def format_proxy(proxy_string):
    """Convert proxy string to httpx-compatible URL (http:// or https://)."""
    if not proxy_string or not proxy_string.strip():
        return None
    s = proxy_string.strip()
    if s.startswith(("http://", "https://", "socks4://", "socks5://")):
        return s
    if "@" in s:
        auth, host_port = s.split("@", 1)
        return f"http://{auth}@{host_port}"
    if ":" in s:
        parts = s.split(":")
        if len(parts) >= 4:
            host, port, user, pwd = parts[0], parts[1], ":".join(parts[2:-1]), parts[-1]
            if port.isdigit():
                return f"http://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
        if len(parts) == 2 and parts[1].isdigit():
            return f"http://{parts[0]}:{parts[1]}"
    return None

def load_proxy_list(source):
    """Load proxies from 'file:path.txt' or comma-separated list. Returns list of formatted proxy URLs."""
    if not source or not source.strip():
        return []
    s = source.strip()
    if s.lower().startswith("file:"):
        path = s[5:].strip()
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [line.strip() for line in f if line.strip()]
            return [p for line in lines for p in [format_proxy(line)] if p]
        except Exception as e:
            print(f"   ⚠️ Could not load proxy file: {e}")
            return []
    return [p for part in s.split(",") for p in [format_proxy(part.strip())] if p]

def get_random_fingerprint():
    """Random browser fingerprint per check — large pool to reduce CAPTCHA."""
    return _build_fingerprint_from_pools()


# ── Pre-built static pools (module level, created once) ──
# ONLY modern Chrome versions (131+) — older ones get flagged by Shopify CAPTCHA
# Safari/Firefox/Edge/Brave removed — Shopify checkout detects non-Chrome inconsistencies
# ONLY versions with matching curl_cffi TLS impersonation — prevents TLS/UA mismatch
# Chrome versions with REAL build numbers — prevents UA vs sec-ch-ua mismatch detection
_FP_CHROME_BUILDS = {
    "136": "136.0.7103.93",
    "133": "133.0.6943.126",
    "131": "131.0.6778.85",
    "124": "124.0.6367.118",
    "123": "123.0.6312.86",
    "120": "120.0.6099.109",
}
_FP_CHROME_VERSIONS = tuple(_FP_CHROME_BUILDS.keys())
_FP_CHROME_WIN = [
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{build} Safari/537.36"
    for build in _FP_CHROME_BUILDS.values()
]
_FP_CHROME_MAC = [
    f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{build} Safari/537.36"
    for build in _FP_CHROME_BUILDS.values()
]
_FP_CHROME_ANDROID = [
    f"Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{build} Mobile Safari/537.36"
    for build in _FP_CHROME_BUILDS.values()
]
_FP_ALL_UAS = _FP_CHROME_WIN + _FP_CHROME_MAC + _FP_CHROME_ANDROID

_FP_ACCEPT_LANGS = [
    "en-US,en;q=0.9", "en-US,en;q=0.9,es;q=0.8", "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,de;q=0.8", "en-US,en;q=0.9,pt;q=0.7", "en-GB,en;q=0.9",
    "en-GB,en;q=0.9,fr;q=0.8", "en-CA,en;q=0.9,fr;q=0.8", "en-AU,en;q=0.9",
]
# Chrome brand strings keyed by Chrome major version — must match UA version exactly
# Format: (sec-ch-ua, sec-ch-ua-full-version-list)
_FP_CHROME_BRANDS_MAP = {
    "136": (
        '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        '"Chromium";v="136.0.7103.93", "Google Chrome";v="136.0.7103.93", "Not.A/Brand";v="99.0.0.0"',
    ),
    "133": (
        '"Not?A_Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        '"Not?A_Brand";v="99.0.0.0", "Google Chrome";v="133.0.6943.126", "Chromium";v="133.0.6943.126"',
    ),
    "131": (
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        '"Google Chrome";v="131.0.6778.85", "Chromium";v="131.0.6778.85", "Not_A Brand";v="24.0.0.0"',
    ),
    "124": (
        '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        '"Chromium";v="124.0.6367.118", "Google Chrome";v="124.0.6367.118", "Not-A.Brand";v="99.0.0.0"',
    ),
    "123": (
        '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        '"Google Chrome";v="123.0.6312.86", "Not:A-Brand";v="8.0.0.0", "Chromium";v="123.0.6312.86"',
    ),
    "120": (
        '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        '"Not_A Brand";v="8.0.0.0", "Chromium";v="120.0.6099.109", "Google Chrome";v="120.0.6099.109"',
    ),
}
_FP_PLATFORMS = [('"Windows"', "?0"), ('"macOS"', "?0"), ('"Android"', "?1")]  # include mobile

_FP_VIEWPORTS = [
    "1920x1080", "1366x768", "1536x864", "1440x900", "1280x720",
    "2560x1440", "1600x900", "1920x1200", "1680x1050", "3840x2160",
    "1280x800", "1280x1024", "1360x768", "2560x1600",
]
_FP_ACCEPTS = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
]

# ── Pre-compiled regex for Chrome version extraction ──
_RE_CHROME_VER = re.compile(r'Chrome/(\d+)')

# ── Module-level constant pools for get_random_info() (avoid re-allocating per call) ──
_POOL_ADDRESSES = (
    {"add1": "123 Main St", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04101"},
    {"add1": "456 Oak Ave", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04102"},
    {"add1": "789 Pine Rd", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04103"},
    {"add1": "321 Elm St", "city": "Bangor", "state": "Maine", "state_short": "ME", "zip": "04401"},
    {"add1": "654 Maple Dr", "city": "Lewiston", "state": "Maine", "state_short": "ME", "zip": "04240"},
    {"add1": "1200 Market St", "city": "Wilmington", "state": "Delaware", "state_short": "DE", "zip": "19801"},
    {"add1": "950 Penn Ave", "city": "Dover", "state": "Delaware", "state_short": "DE", "zip": "19901"},
    {"add1": "88 Broad St", "city": "Burlington", "state": "Vermont", "state_short": "VT", "zip": "05401"},
    {"add1": "222 State St", "city": "Montpelier", "state": "Vermont", "state_short": "VT", "zip": "05602"},
    {"add1": "415 Congress St", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04101"},
    {"add1": "77 Park Ave", "city": "Nashua", "state": "New Hampshire", "state_short": "NH", "zip": "03060"},
    {"add1": "300 Elm St", "city": "Manchester", "state": "New Hampshire", "state_short": "NH", "zip": "03101"},
    {"add1": "55 Hope St", "city": "Providence", "state": "Rhode Island", "state_short": "RI", "zip": "02906"},
    {"add1": "180 Angell St", "city": "Providence", "state": "Rhode Island", "state_short": "RI", "zip": "02906"},
    {"add1": "42 College St", "city": "New Haven", "state": "Connecticut", "state_short": "CT", "zip": "06510"},
    {"add1": "600 Trumbull St", "city": "Hartford", "state": "Connecticut", "state_short": "CT", "zip": "06103"},
    {"add1": "101 Federal St", "city": "Boston", "state": "Massachusetts", "state_short": "MA", "zip": "02110"},
    {"add1": "250 Northern Ave", "city": "Boston", "state": "Massachusetts", "state_short": "MA", "zip": "02210"},
    {"add1": "33 Warwick Ave", "city": "Cranston", "state": "Rhode Island", "state_short": "RI", "zip": "02910"},
    {"add1": "710 Main St", "city": "Stamford", "state": "Connecticut", "state_short": "CT", "zip": "06901"},
    {"add1": "1425 Broadway", "city": "New York", "state": "New York", "state_short": "NY", "zip": "10018"},
    {"add1": "350 5th Ave", "city": "New York", "state": "New York", "state_short": "NY", "zip": "10118"},
    {"add1": "200 Park Ave", "city": "New York", "state": "New York", "state_short": "NY", "zip": "10166"},
    {"add1": "1600 Vine St", "city": "Los Angeles", "state": "California", "state_short": "CA", "zip": "90028"},
    {"add1": "8500 Beverly Blvd", "city": "Los Angeles", "state": "California", "state_short": "CA", "zip": "90048"},
    {"add1": "233 S Wacker Dr", "city": "Chicago", "state": "Illinois", "state_short": "IL", "zip": "60606"},
    {"add1": "875 N Michigan Ave", "city": "Chicago", "state": "Illinois", "state_short": "IL", "zip": "60611"},
    {"add1": "1500 Market St", "city": "Philadelphia", "state": "Pennsylvania", "state_short": "PA", "zip": "19102"},
    {"add1": "401 N Broad St", "city": "Philadelphia", "state": "Pennsylvania", "state_short": "PA", "zip": "19108"},
    {"add1": "2000 McKinney Ave", "city": "Dallas", "state": "Texas", "state_short": "TX", "zip": "75201"},
    {"add1": "500 Main St", "city": "Houston", "state": "Texas", "state_short": "TX", "zip": "77002"},
    {"add1": "100 Peachtree St", "city": "Atlanta", "state": "Georgia", "state_short": "GA", "zip": "30303"},
    {"add1": "191 Peachtree St NE", "city": "Atlanta", "state": "Georgia", "state_short": "GA", "zip": "30303"},
    {"add1": "701 Brickell Ave", "city": "Miami", "state": "Florida", "state_short": "FL", "zip": "33131"},
    {"add1": "200 S Orange Ave", "city": "Orlando", "state": "Florida", "state_short": "FL", "zip": "32801"},
    {"add1": "1201 3rd Ave", "city": "Seattle", "state": "Washington", "state_short": "WA", "zip": "98101"},
    {"add1": "400 Pine St", "city": "Seattle", "state": "Washington", "state_short": "WA", "zip": "98101"},
    {"add1": "1000 SW Broadway", "city": "Portland", "state": "Oregon", "state_short": "OR", "zip": "97205"},
    {"add1": "750 E Pratt St", "city": "Baltimore", "state": "Maryland", "state_short": "MD", "zip": "21202"},
    {"add1": "1100 Wilson Blvd", "city": "Arlington", "state": "Virginia", "state_short": "VA", "zip": "22209"},
)
_POOL_FIRST_NAMES = (
    "John", "Emily", "Alex", "Sarah", "Michael", "Jessica", "David", "Lisa",
    "James", "Jennifer", "Robert", "Amanda", "Daniel", "Ashley", "Matthew",
    "Megan", "Andrew", "Lauren", "Ryan", "Rachel", "Joshua", "Stephanie",
    "Christopher", "Nicole", "Brandon", "Elizabeth", "Tyler", "Heather",
    "Kevin", "Samantha", "Brian", "Kimberly", "Nathan", "Melissa",
    "Jacob", "Hannah", "Ethan", "Olivia", "Noah", "Sophia", "Liam", "Emma",
    "Mason", "Ava", "Logan", "Isabella", "Lucas", "Mia", "Aiden", "Charlotte",
    "Caleb", "Amelia", "Jack", "Harper", "Owen", "Evelyn", "Luke", "Abigail",
    "Henry", "Ella", "Sebastian", "Scarlett", "Carter", "Grace", "Wyatt", "Chloe",
    "Dylan", "Victoria", "Gabriel", "Riley", "Julian", "Aria", "Levi", "Lily",
    "Isaac", "Aurora", "Lincoln", "Zoey", "Jaxon", "Nora", "Asher", "Camila",
    "Theodore", "Penelope", "Leo", "Layla", "Thomas", "Paisley", "Charles", "Savannah",
    "Marcus", "Allison", "Patrick", "Natalie", "Peter", "Hazel", "George", "Violet",
)
_POOL_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis",
    "Martinez", "Anderson", "Taylor", "Thomas", "Jackson", "White",
    "Harris", "Clark", "Lewis", "Robinson", "Walker", "Young",
    "Allen", "King", "Wright", "Scott", "Green", "Baker",
    "Adams", "Nelson", "Hill", "Campbell", "Mitchell", "Roberts",
    "Carter", "Phillips", "Evans", "Turner", "Torres", "Parker",
    "Collins", "Edwards", "Stewart", "Flores", "Morris", "Nguyen",
    "Murphy", "Rivera", "Cook", "Rogers", "Morgan", "Peterson",
    "Cooper", "Reed", "Bailey", "Bell", "Gomez", "Kelly",
    "Howard", "Ward", "Cox", "Diaz", "Richardson", "Wood",
    "Watson", "Brooks", "Bennett", "Gray", "James", "Reyes",
    "Cruz", "Hughes", "Price", "Myers", "Long", "Foster",
)
_POOL_EMAIL_DOMAINS = (
    "gmail.com", "yahoo.com", "outlook.com", "icloud.com",
    "hotmail.com", "aol.com", "protonmail.com", "mail.com",
    "live.com", "msn.com", "ymail.com", "me.com",
    "comcast.net", "att.net", "verizon.net", "cox.net",
)
_POOL_PHONES = (
    "2025550199", "3105551234", "4155559876", "6175550123",
    "9718081573", "2125559999", "7735551212", "4085556789",
    "5035559012", "6025553456", "7025557890", "8015551234",
    "2145555678", "3035559012", "4045553456", "5125557890",
    "6155551234", "7165555678", "8185559012", "9195553456",
    "2675557890", "3125551234", "4155555678", "5035559012",
)


def _build_fingerprint_from_pools():
    """Build a random fingerprint — Chrome only, version-matched sec-ch-ua."""
    ua = random.choice(_FP_ALL_UAS)

    # Extract Chrome major version from UA to match sec-ch-ua exactly
    _ver_m = _RE_CHROME_VER.search(ua)
    chrome_ver = _ver_m.group(1) if _ver_m else "136"
    brand_entry = _FP_CHROME_BRANDS_MAP.get(chrome_ver, _FP_CHROME_BRANDS_MAP["136"])
    ch_ua, ch_ua_full = brand_entry

    # Match platform to UA
    if "Android" in ua or "Mobile" in ua:
        platform, mobile = '"Android"', "?1"
    elif "Macintosh" in ua or "Mac OS" in ua:
        platform, mobile = '"macOS"', "?0"
    else:
        platform, mobile = '"Windows"', "?0"

    fp = {
        "_chrome_ver": chrome_ver,
        "User-Agent": ua,
        "Accept-Language": random.choice(_FP_ACCEPT_LANGS),
        "Accept": random.choice(_FP_ACCEPTS),
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Priority": "u=0, i",
        "viewport": random.choice(_FP_VIEWPORTS),
        "screen-depth": random.choice(["24", "32"]),
        # sec-ch-ua always present (Chrome only)
        "Sec-Ch-Ua": ch_ua,
        "Sec-Ch-Ua-Mobile": mobile,
        "Sec-Ch-Ua-Platform": platform,
    }
    # Randomly include full-version-list (~50% of real Chrome browsers send it)
    if random.random() < 0.5:
        fp["Sec-Ch-Ua-Full-Version-List"] = ch_ua_full
    # Randomly include DNT header (~20% of real browsers)
    if random.random() < 0.2:
        fp["DNT"] = "1"

    return fp

def find_between(s, start, end):
    """Extract text between *start* and *end* markers using str.find (O(n) vs split)."""
    try:
        i = s.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = s.find(end, i)
        if j == -1:
            return ""
        return s[i:j]
    except Exception:
        return ""


# ── Pre-compiled regex patterns (compiled once at module load) ──────────────
_RE_SESSION_TOKEN = re.compile(r'name="serialized-sessionToken"\s+content="&quot;([^"]+)&quot;"')
_RE_DELIVERY_LINE_1 = re.compile(r'"deliveryLineStableId"\s*:\s*"([^"]+)"')
_RE_DELIVERY_LINE_2 = re.compile(r'"deliveryGroupStableId"\s*:\s*"([^"]+)"')
_RE_DELIVERY_LINE_3 = re.compile(r"deliveryLineStableId['\"]\s*:\s*['\"]([^'\"]+)['\"]")
_RE_DELIVERY_LINE_4 = re.compile(r'deliveryLines&quot;:\[\{&q
_RE_DELIVERY_LINE_5 = re.compile(r'"deliveryLines"\s*:\s*\[\s*\{\s*"stableId"\s*:\s*"([^"]+)"')
