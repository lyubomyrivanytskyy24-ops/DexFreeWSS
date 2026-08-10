import asyncio
import os
import sys
import html
import time
import json
import hmac
import hashlib
import secrets
import re
import unicodedata
import urllib.request
import urllib.parse
from collections import defaultdict, deque
from typing import Set, Dict, Any, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, RedirectResponse
import uvicorn

app = FastAPI()

# =====================================================================
# SECTION 1 — CORE CONFIG / SECRETS
# =====================================================================


def _get_or_create_secret(env_name: str, file_name: str) -> str:
    """Load a secret from env, else from a local file, else generate+persist one."""
    val = os.environ.get(env_name)
    if val:
        return val.strip()

    if os.path.exists(file_name):
        with open(file_name, "r", encoding="utf-8") as f:
            existing = f.read().strip()
            if existing:
                return existing

    generated = secrets.token_urlsafe(32)
    try:
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(generated)
        os.chmod(file_name, 0o600)
    except Exception as e:
        print(f"WARNING: could not persist generated secret for {env_name}: {e}")

    print(f"[SECURITY] {env_name} was not set. Generated a new one and saved it to "
          f"{file_name} (mode 600). Set the {env_name} env var to control this explicitly.")
    return generated


# Single shared key for the whole API (used as the admin key too, same as
# the original system). Override with DEX_API_KEY in any real deployment.
API_KEY = (os.environ.get("DEX_API_KEY", "").strip() or "")
SECRET_KEY = _get_or_create_secret("DEX_SECRET_KEY", ".dex_secret_key")
BASE_URL = os.environ.get("DEX_BASE_URL", "https://dexapi1.up.railway.app").rstrip("/")

# Branding
SITE_NAME = os.environ.get("DEX_SITE_NAME", "Dex").strip() or "Dex"
LOGO_URL = os.environ.get("DEX_LOGO_URL", "").strip()  # set this once you have the image link

# Discord OAuth (replaces username/password login)
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", f"{BASE_URL}/auth/discord/callback").strip()
DISCORD_API_BASE = "https://discord.com/api"


def discord_configured() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)


def constant_time_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def is_valid_key(k: str) -> bool:
    return bool(k) and constant_time_eq(k, API_KEY)


# =====================================================================
# SECTION 2 — GITHUB-MANAGED SCRIPT SOURCE (unchanged from original:
# these scripts can ONLY be changed by editing the file in the configured
# GitHub repo — there is no code path that writes to them from this app)
# =====================================================================

GITHUB_OWNER = os.environ.get("DEX_GITHUB_OWNER", "").strip()
GITHUB_REPO = os.environ.get("DEX_GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.environ.get("DEX_GITHUB_BRANCH", "main").strip() or "main"
GITHUB_TOKEN = os.environ.get("DEX_GITHUB_TOKEN", "").strip()
GITHUB_CACHE_TTL = int(os.environ.get("DEX_GITHUB_CACHE_TTL", "60"))

GITHUB_SCRIPT_PATHS: Dict[str, str] = {
    "dexchilli": os.environ.get("DEX_GITHUB_PATH_DEXCHILLI", "scripts/dexchilli.lua").strip(),
    "dexfree": os.environ.get("DEX_GITHUB_PATH_DEXFREE", "scripts/dexfree.lua").strip(),
    "dexserverhop": os.environ.get("DEX_GITHUB_PATH_DEXSERVERHOP", "scripts/dexserverhop.lua").strip(),
    "dexhub": os.environ.get("DEX_GITHUB_PATH_DEXHUB", "scripts/dexhub.lua").strip(),
    "dexpaid": os.environ.get("DEX_GITHUB_PATH_DEXPAID", "scripts/dexpaid.lua").strip(),
    "dexautoroll": os.environ.get("DEX_GITHUB_PATH_DEXAUTOROLL", "scripts/dexautoroll.lua").strip(),
}

_github_cache: Dict[str, Dict[str, Any]] = {}
_github_cache_lock = asyncio.Lock()


def github_configured() -> bool:
    return bool(GITHUB_OWNER and GITHUB_REPO)


def github_repo_url() -> str:
    if not github_configured():
        return ""
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/blob/{GITHUB_BRANCH}"


def _github_raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{path}"


def _fetch_github_raw_sync(path: str) -> Optional[str]:
    url = _github_raw_url(path)
    req = urllib.request.Request(url)
    req.add_header("Cache-Control", "no-cache")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
            return data.decode("utf-8")
    except Exception as e:
        print(f"[GITHUB] Failed to fetch {path}: {e}")
        return None


async def get_github_script(name: str, local_fallback_file: str, default: str) -> str:
    now = time.time()

    async with _github_cache_lock:
        cached = _github_cache.get(name)
        if cached and (now - cached["fetched_at"]) < GITHUB_CACHE_TTL:
            return cached["content"]

    path = GITHUB_SCRIPT_PATHS.get(name)
    content = None
    if path and github_configured():
        content = await asyncio.to_thread(_fetch_github_raw_sync, path)

    if content is not None:
        async with _github_cache_lock:
            _github_cache[name] = {"content": content, "fetched_at": now, "source": "github"}
        try:
            save_file(local_fallback_file, content)
        except Exception:
            pass
        return content

    async with _github_cache_lock:
        cached = _github_cache.get(name)
        if cached:
            return cached["content"]

    fallback = load_file(local_fallback_file, default)
    async with _github_cache_lock:
        _github_cache[name] = {"content": fallback, "fetched_at": now, "source": "local_fallback"}
    return fallback


async def force_refresh_github_cache():
    async with _github_cache_lock:
        _github_cache.clear()


def get_cache_meta(name: str) -> Optional[Dict[str, Any]]:
    return _github_cache.get(name)


# =====================================================================
# SECTION 3 — SIGNED SESSION TOKENS (stdlib only)
# =====================================================================

SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days
ADMIN_SESSION_MAX_AGE = 2 * 3600  # 2 hours
OAUTH_STATE_MAX_AGE = 10 * 60     # 10 minutes to complete the Discord round-trip


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(subject: str) -> str:
    ts = str(int(time.time()))
    payload = f"{subject}|{ts}"
    sig = _sign(payload)
    raw = f"{payload}|{sig}"
    return raw.replace("|", ".")


def verify_session_token(token: Optional[str], max_age: int) -> Optional[str]:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    subject, ts, sig = parts
    payload = f"{subject}|{ts}"
    expected_sig = _sign(payload)
    if not constant_time_eq(sig, expected_sig):
        return None
    try:
        ts_int = int(ts)
    except ValueError:
        return None
    if time.time() - ts_int > max_age:
        return None
    return subject


# =====================================================================
# SECTION 4 — RATE LIMITING (lockout-style for auth, sliding-window for
# raw volume) — unchanged from the original
# =====================================================================

RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW = 15 * 60
RATE_LIMIT_LOCKOUT = 15 * 60

_rate_state: Dict[str, Dict[str, Any]] = {}
_rate_lock = asyncio.Lock()

_IP_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F:.]{2,45}$")


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        candidate = xff.split(",")[0].strip()
        if _IP_TOKEN_PATTERN.match(candidate):
            return candidate
    if request.client:
        return request.client.host
    return "unknown"


def _ws_client_ip(websocket: WebSocket) -> str:
    xff = websocket.headers.get("x-forwarded-for")
    if xff:
        candidate = xff.split(",")[0].strip()
        if _IP_TOKEN_PATTERN.match(candidate):
            return candidate
    if websocket.client:
        return websocket.client.host
    return "unknown"


async def is_rate_limited(bucket: str, ip: str) -> bool:
    key = f"{bucket}:{ip}"
    async with _rate_lock:
        state = _rate_state.get(key)
        if not state:
            return False
        now = time.time()
        if state.get("locked_until", 0) > now:
            return True
        if now - state.get("window_start", 0) > RATE_LIMIT_WINDOW:
            _rate_state.pop(key, None)
            return False
        return False


async def record_failed_attempt(bucket: str, ip: str):
    key = f"{bucket}:{ip}"
    async with _rate_lock:
        now = time.time()
        state = _rate_state.get(key)
        if not state or now - state.get("window_start", 0) > RATE_LIMIT_WINDOW:
            state = {"count": 0, "window_start": now, "locked_until": 0}
        state["count"] += 1
        if state["count"] >= RATE_LIMIT_MAX_ATTEMPTS:
            state["locked_until"] = now + RATE_LIMIT_LOCKOUT
        _rate_state[key] = state


async def clear_attempts(bucket: str, ip: str):
    key = f"{bucket}:{ip}"
    async with _rate_lock:
        _rate_state.pop(key, None)


_volume_buckets: Dict[str, deque] = defaultdict(deque)


def rate_limited(ip: str, bucket: str, max_requests: int, window_seconds: float) -> bool:
    key = f"{bucket}:{ip}"
    now = time.monotonic()
    q = _volume_buckets[key]
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= max_requests:
        return True
    q.append(now)
    return False


async def rate_bucket_janitor():
    while True:
        await asyncio.sleep(600)
        now = time.monotonic()
        stale_keys = [k for k, q in _volume_buckets.items() if not q or now - q[-1] > 3600]
        for k in stale_keys:
            _volume_buckets.pop(k, None)


def reject_if_oversized(request: Request, max_bytes: int) -> bool:
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                return True
        except ValueError:
            return True
    return False


# =====================================================================
# SECTION 5 — CONTENT FILTERING (unchanged from original)
# =====================================================================

BLOCKED_SUBSTRINGS = {
    "discord", "discordgg", "discordcom", "discordappcom", "dscgg",
    "fuck", "shit", "bitch", "asshole", "cunt", "dick", "cock", "pussy",
    "nigger", "nigga", "faggot", "fag", "retard", "whore", "slut",
    "porn", "rape", "nazi", "kike", "chink", "spic",
}

_LEET_TRANSLATION = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s",
})

_URL_PATTERN = re.compile(r"(https?://|www\.)", re.IGNORECASE)
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STRICT_SINGLE_LINE_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_NUL_BYTE_PATTERN = re.compile(r"\x00")
_ANY_WHITESPACE_PATTERN = re.compile(r"\s")

WSS_URL_PATTERN = re.compile(
    r"^wss?://[A-Za-z0-9.\-]{1,253}(:\d{1,5})?(/[A-Za-z0-9._~\-/%]*)?$"
)

USERNAME_FORMAT_PATTERN = re.compile(r"^(?!.*[_.]{2})[A-Za-z0-9][A-Za-z0-9_.]{1,30}[A-Za-z0-9]$")
KEY_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
HWID_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def normalize_field(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _normalize_for_matching(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    stripped = stripped.lower().translate(_LEET_TRANSLATION)
    return re.sub(r"[^a-z0-9]+", "", stripped)


def contains_blocked_content(*fields: str) -> bool:
    for field in fields:
        if _URL_PATTERN.search(field.lower()):
            return True
        normalized = _normalize_for_matching(field)
        for bad in BLOCKED_SUBSTRINGS:
            if bad in normalized:
                return True
    return False


def has_excessive_repetition(text: str, max_repeat: int = 6) -> bool:
    return bool(re.search(r"(.)\1{" + str(max_repeat) + r",}", text))


# =====================================================================
# SECTION 6 — INPUT VALIDATION / PASSWORD HASHING (kept for backward
# compatibility with any pre-existing username/password accounts)
# =====================================================================

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
SLUG_SOURCE_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,48}$")
RESERVED_USERNAMES = {"system", "admin", "administrator", "root", "sender", "owner", "sys"}

MAX_GENERIC_BODY = 8 * 1024
MAX_SCRIPT_BODY = 2 * 1024 * 1024
MAX_FORM_BODY = 2 * 1024 * 1024 + (100 * 1024)
MAX_PASSWORD_LEN = 128
MAX_LOG_LEN = 4096
MAX_USERNAME_LEN = 32
MAX_BANNER_LEN = 500

PBKDF2_ITERATIONS = 200_000


def is_valid_username(username: str) -> bool:
    if not USERNAME_RE.match(username):
        return False
    if username.lower() in RESERVED_USERNAMES:
        return False
    if contains_blocked_content(username):
        return False
    return True


def make_slug(name: str) -> Optional[str]:
    if not SLUG_SOURCE_RE.match(name):
        return None
    slug = name.strip().replace(" ", "-")
    if not slug:
        return None
    return slug


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk, expected)


# =====================================================================
# SECTION 7 — FILES / STORAGE
# =====================================================================

USERNAME_FILE = "usernames.txt"
BLACKLIST_FILE = "blacklisted.txt"
LOGS_FILE = "logs.txt"
DEXPAID_KEYS_FILE = "dexpaid_keys.json"
BANNER_FILE = "banner.txt"
ACCESS_KEYS_FILE = "access_keys.json"

USERS_FILE = "users.json"
SCRIPTS_FILE = "scripts.json"

lock = asyncio.Lock()
blacklist_lock = asyncio.Lock()
announcement_lock = asyncio.Lock()
logs_lock = asyncio.Lock()
dexpaid_keys_lock = asyncio.Lock()
users_lock = asyncio.Lock()
scripts_lock = asyncio.Lock()
ws_count_lock = asyncio.Lock()
banner_lock = asyncio.Lock()
access_keys_lock = asyncio.Lock()

DEXCHILLI_FILE = "dexchilli.lua"
DEXFREE_FILE = "dexfree.lua"
DEXSERVERHOP_FILE = "dexserverhop.lua"
DEXHUB_FILE = "dexhub.lua"
DEXPAID_FILE = "dexpaid.lua"
DEXAUTOROLL_FILE = "dexautoroll.lua"

DEFAULT_DEXCHILLI = "-- DexChilli loader script not set yet. Add scripts/dexchilli.lua to the GitHub repo."
DEFAULT_DEXFREE = "-- DexFree loader script not set yet. Add scripts/dexfree.lua to the GitHub repo."
DEFAULT_DEXSERVERHOP = "-- DexServerHop loader script not set yet. Add scripts/dexserverhop.lua to the GitHub repo."
DEFAULT_DEXHUB = "-- DexHub loader script not set yet. Add scripts/dexhub.lua to the GitHub repo."
DEFAULT_DEXPAID = "-- DexPaid loader script not set yet. Add scripts/dexpaid.lua to the GitHub repo."
DEFAULT_DEXAUTOROLL = "-- DexAutoRoll loader script not set yet. Add scripts/dexautoroll.lua to the GitHub repo."

FIXED_SCRIPTS: Dict[str, Dict[str, str]] = {
    "dexchilli": {"file": DEXCHILLI_FILE, "default": DEFAULT_DEXCHILLI, "label": "DexChilli"},
    "dexfree": {"file": DEXFREE_FILE, "default": DEFAULT_DEXFREE, "label": "DexFree"},
    "dexserverhop": {"file": DEXSERVERHOP_FILE, "default": DEFAULT_DEXSERVERHOP, "label": "DexServerHop"},
    "dexhub": {"file": DEXHUB_FILE, "default": DEFAULT_DEXHUB, "label": "DexHub"},
    "dexpaid": {"file": DEXPAID_FILE, "default": DEFAULT_DEXPAID, "label": "DexPaid"},
    "dexautoroll": {"file": DEXAUTOROLL_FILE, "default": DEFAULT_DEXAUTOROLL, "label": "DexAutoRoll"},
}

SCRIPT_TAGLINES: Dict[str, str] = {
    "dexchilli": "Smooth, reliable, and free to run.",
    "dexfree": "The classic free loader - no key required.",
    "dexserverhop": "Automatic server hopping on demand.",
    "dexhub": "The full hub experience, free tier.",
    "dexautoroll": "Set it and forget it automation.",
}

viewers: Set[WebSocket] = set()
sender_ws: Optional[WebSocket] = None
ws_ip_connection_counts: Dict[str, int] = defaultdict(int)

announcement_text: str = ""
announcement_timestamp: float = 0.0

dexpaid_keys: Dict[str, float] = {}
last_generated_paid_key: str = ""
last_generated_paid_loadstring: str = ""

users: Dict[str, Dict[str, Any]] = {}
scripts: Dict[str, Dict[str, Any]] = {}

# Global "access keys" — generated from the admin panel. Distinct from the
# per-script DexPaid keys: these are generic keys an admin can hand out
# (e.g. for the API itself, or for a general access tier) with a note and
# expiry, fully visible/manageable from /admin.
access_keys: Dict[str, Dict[str, Any]] = {}


def _atomic_write(path: str, content: str, mode: int = 0o600):
    tmp_path = f"{path}.tmp.{secrets.token_hex(4)}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, path)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


MAX_STORED_USERNAMES = 5000


def load_usernames_from_file() -> set:
    if not os.path.exists(USERNAME_FILE):
        return set()
    with open(USERNAME_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f.readlines() if line.strip())


def save_usernames_to_file(names: set):
    _atomic_write(USERNAME_FILE, "\n".join(sorted(names)) + ("\n" if names else ""), mode=0o644)


stored_usernames: set = load_usernames_from_file()
stored_usernames_lower: set = {u.lower() for u in stored_usernames}


def _purge_bad_usernames_locked() -> bool:
    bad = set()
    for name in stored_usernames:
        if not name:
            bad.add(name)
            continue
        if _ANY_WHITESPACE_PATTERN.search(name):
            bad.add(name)
            continue
        if not is_valid_username(name):
            bad.add(name)
            continue
    if bad:
        for name in bad:
            stored_usernames.discard(name)
            stored_usernames_lower.discard(name.lower())
        return True
    return False


async def username_cleanup_janitor():
    while True:
        await asyncio.sleep(300)
        async with lock:
            changed = _purge_bad_usernames_locked()
            if changed:
                save_usernames_to_file(stored_usernames)


def restart_process():
    print("[USERNAMES] Reached MAX_STORED_USERNAMES - restarting process.")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def schedule_restart(delay: float = 1.0):
    await asyncio.sleep(delay)
    restart_process()


def load_blacklist_from_file() -> set:
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f.readlines() if line.strip())


def save_blacklist_to_file(names: set):
    _atomic_write(BLACKLIST_FILE, "\n".join(sorted(names)) + ("\n" if names else ""), mode=0o644)


blacklisted_usernames: set = load_blacklist_from_file()


def load_banner_from_file() -> str:
    if not os.path.exists(BANNER_FILE):
        return ""
    with open(BANNER_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_banner_to_file(text: str):
    _atomic_write(BANNER_FILE, text, mode=0o644)


banner_text: str = load_banner_from_file()


def load_file(path: str, default: str) -> str:
    if not os.path.exists(path):
        _atomic_write(path, default, mode=0o644)
        return default
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_file(path: str, content: str):
    _atomic_write(path, content, mode=0o644)


MAX_STORED_LOGS = 5000


def load_logs_from_file() -> list:
    if not os.path.exists(LOGS_FILE):
        return []
    with open(LOGS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f.readlines() if line.strip()][-MAX_STORED_LOGS:]


def append_log_to_file(entry: str):
    with open(LOGS_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


stored_logs: list = load_logs_from_file()


def load_dexpaid_keys_from_file() -> Dict[str, float]:
    if not os.path.exists(DEXPAID_KEYS_FILE):
        return {}
    try:
        with open(DEXPAID_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {k: float(v) for k, v in data.items()}
    except Exception:
        return {}


def save_dexpaid_keys_to_file(keys: Dict[str, float]):
    _atomic_write(DEXPAID_KEYS_FILE, json.dumps(keys), mode=0o600)


dexpaid_keys = load_dexpaid_keys_from_file()


def load_access_keys_from_file() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(ACCESS_KEYS_FILE):
        return {}
    try:
        with open(ACCESS_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_access_keys_to_file():
    _atomic_write(ACCESS_KEYS_FILE, json.dumps(access_keys), mode=0o600)


access_keys = load_access_keys_from_file()


def generate_paid_key(length: int = 24) -> str:
    return secrets.token_urlsafe(length)[:length]


def cleanup_expired_paid_keys():
    now = time.time()
    expired = [k for k, exp in dexpaid_keys.items() if exp <= now]
    for k in expired:
        dexpaid_keys.pop(k, None)


def cleanup_expired_access_keys():
    now = time.time()
    expired = [k for k, meta in access_keys.items() if meta.get("expiry") and meta["expiry"] <= now]
    for k in expired:
        access_keys.pop(k, None)


MAX_KEY_DURATION_HOURS = 24 * 365


def parse_duration_hours(raw: str) -> Optional[float]:
    try:
        hours = float(raw)
    except Exception:
        return None
    if hours <= 0 or hours > MAX_KEY_DURATION_HOURS:
        return None
    return hours


def load_users_from_file() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_users_to_file():
    _atomic_write(USERS_FILE, json.dumps(users), mode=0o600)


def load_scripts_from_file() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(SCRIPTS_FILE):
        return {}
    try:
        with open(SCRIPTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_scripts_to_file():
    _atomic_write(SCRIPTS_FILE, json.dumps(scripts), mode=0o600)


users = load_users_from_file()
scripts = load_scripts_from_file()

RESERVED_PATHS = {
    "", "home", "dashboard", "login", "logout", "admin", "logs", "usernames", "blacklisted",
    "announcements", "ws", "secure", "dexfree", "dexchilli", "dexserverhop", "dexhub", "dexpaid",
    "dexautoroll", "admin/stats", "admin/update", "admin/keys", "favicon.ico", "robots.txt",
    "scripts", "banner", "github/refresh", "dexpaid/keys", "info", "rules", "auth",
    "auth/discord/callback",
}
RESERVED_PATHS_LOWER = {p.lower() for p in RESERVED_PATHS}


def ensure_builtin_scripts():
    builtin = [
        ("DexFree", "dexfree", DEXFREE_FILE, DEFAULT_DEXFREE),
        ("DexChilli", "dexchilli", DEXCHILLI_FILE, DEFAULT_DEXCHILLI),
        ("DexServerHop", "dexserverhop", DEXSERVERHOP_FILE, DEFAULT_DEXSERVERHOP),
        ("DexHub", "dexhub", DEXHUB_FILE, DEFAULT_DEXHUB),
        ("DexAutoRoll", "dexautoroll", DEXAUTOROLL_FILE, DEFAULT_DEXAUTOROLL),
    ]
    for name, slug, path, default in builtin:
        if slug not in scripts:
            scripts[slug] = {
                "name": name,
                "slug": slug,
                "code": load_file(path, default),
                "is_paid": False,
                "hwid_lock": False,
                "owner": "SYSTEM",
                "created_at": time.time(),
                "updated_at": time.time(),
                "keys": {},
                "last_key": "",
                "last_loadstring": "",
                "github_managed": True,
            }
        else:
            scripts[slug]["github_managed"] = True
    save_scripts_to_file()


ensure_builtin_scripts()

APP_STARTED_AT = time.time()

# =====================================================================
# SECTION 8 — SHARED PAGE SHELL (this is the actual "redesign" — every
# HTML page in the app renders through this so the banner, nav, favicon,
# tab title format ("Dex | X"), and responsive layout are consistent
# everywhere instead of six copy-pasted <html> blocks)
# =====================================================================

SHELL_CSS = """
:root {
    --bg: #050509; --card-bg: #0f0f16; --accent1: #4fc3f7; --accent2: #7c4dff;
    --accent3: #ff5252; --accent4: #00e676; --border: #1c1c24; --nav-h: 64px;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
    min-height: 100vh;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background:
        radial-gradient(circle at top left, #202040 0, #050509 40%, #000000 100%),
        linear-gradient(135deg, rgba(79,195,247,0.08), rgba(255,82,82,0.08));
    color: #e6e6e6;
    -webkit-font-smoothing: antialiased;
}
a { color: var(--accent1); }
.nav {
    position: sticky; top: 0; z-index: 20; height: var(--nav-h);
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 20px; backdrop-filter: blur(10px);
    background: rgba(6,6,10,0.85); border-bottom: 1px solid rgba(79,195,247,0.15);
}
.nav-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.nav-left img { height: 30px; width: 30px; border-radius: 8px; object-fit: cover; flex-shrink: 0; }
.nav-left .brand {
    font-weight: 800; letter-spacing: 0.06em; font-size: 17px; white-space: nowrap;
    background: linear-gradient(135deg, var(--accent1), var(--accent2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
}
.nav-links { display: flex; align-items: center; gap: 4px; }
.nav-links a, .nav-links button {
    color: #cfcfe0; text-decoration: none; font-size: 13.5px; font-weight: 600;
    padding: 8px 12px; border-radius: 10px; border: none; background: transparent;
    cursor: pointer; font-family: inherit;
}
.nav-links a:hover, .nav-links button:hover { background: rgba(79,195,247,0.12); color: #fff; }
.nav-links a.active { background: rgba(79,195,247,0.18); color: #fff; }
.nav-toggle {
    display: none; background: transparent; border: 1px solid rgba(255,255,255,0.15);
    color: #e6e6e6; border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 16px;
}
.site-banner {
    text-align: center; font-size: 13.5px; padding: 10px 16px; color: #fff5da;
    background: linear-gradient(90deg, rgba(255,193,7,0.22), rgba(255,82,82,0.18));
    border-bottom: 1px solid rgba(255,193,7,0.3);
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 40px 22px 60px; }
.card {
    background: linear-gradient(135deg, rgba(15,15,22,0.95), rgba(10,10,18,0.95));
    border-radius: 18px; padding: 22px; margin-bottom: 22px; border: 1px solid rgba(79,195,247,0.16);
    box-shadow: 0 20px 50px rgba(0,0,0,0.55); position: relative; overflow: hidden;
}
h1 { margin: 0 0 8px; font-size: clamp(24px, 5vw, 32px); letter-spacing: 0.02em; }
h2 { margin: 0 0 8px; font-size: 19px; }
p { line-height: 1.5; }
.label { font-size: 13px; color: #b0b0c0; margin-bottom: 6px; }
.small-text { font-size: 12px; color: #8a8aa0; }
.pill {
    display: inline-block; padding: 4px 11px; border-radius: 999px; font-size: 11px;
    background: rgba(79,195,247,0.16); border: 1px solid rgba(79,195,247,0.38);
    color: #e6f7ff; margin: 2px 4px 2px 0;
}
.pill.red { background: rgba(255,82,82,0.16); border-color: rgba(255,82,82,0.4); color: #ffe6e6; }
.pill.green { background: rgba(0,230,118,0.16); border-color: rgba(0,230,118,0.4); color: #e6fff3; }
.pill.purple { background: rgba(124,77,255,0.16); border-color: rgba(124,77,255,0.4); color: #f0e6ff; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; }
input[type=password], input[type=text] {
    width: 100%; padding: 12px; border-radius: 12px; border: 1px solid #262636;
    background: rgba(8,8,13,0.95); color: #e6e6e6; outline: none; font-size: 14px;
}
textarea {
    width: 100%; min-height: 180px; background: rgba(8,8,13,0.95); color: #9eff9e;
    border-radius: 12px; border: 1px solid #262636; padding: 12px; font-family: ui-monospace, monospace;
    font-size: 13.5px; outline: none;
}
button, .btn {
    padding: 10px 20px; border-radius: 999px; border: none; cursor: pointer; font-weight: 700;
    background: linear-gradient(135deg, var(--accent1), var(--accent2)); color: #050509;
    margin-top: 10px; font-size: 13.5px; display: inline-block; text-decoration: none;
}
button.secondary, .btn.secondary { background: linear-gradient(135deg, #3a3a4a, #25252f); color: #e6e6e6; }
button.danger, .btn.danger { background: linear-gradient(135deg, #ff5252, #ff1744); color: #fff; }
button.discord, .btn.discord { background: linear-gradient(135deg, #5865F2, #4752c4); color: #fff; }
.logs-box {
    background: rgba(8,8,13,0.95); border-radius: 12px; border: 1px solid #262636; padding: 12px;
    font-family: ui-monospace, monospace; font-size: 12px; max-height: 260px; overflow: auto;
    white-space: pre-wrap; word-break: break-word;
}
.code-row {
    display: flex; align-items: center; gap: 10px; background: rgba(8,8,13,0.95);
    border: 1px solid #262636; border-radius: 12px; padding: 10px 12px; overflow: hidden;
}
.code-row code {
    flex: 1; font-family: ui-monospace, monospace; font-size: 12.5px; color: #9eff9e;
    white-space: nowrap; overflow-x: auto;
}
.copy-btn {
    flex-shrink: 0; border: none; border-radius: 999px; padding: 8px 14px; font-weight: 700;
    font-size: 12px; cursor: pointer; color: #050509;
    background: linear-gradient(135deg, var(--accent1), var(--accent2));
}
.copy-btn.copied { background: linear-gradient(135deg, var(--accent4), #00c853); }
.error { margin-top: 10px; color: #ff5252; font-size: 13px; }
.success { margin-top: 10px; color: #00e676; font-size: 13px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 12px; }
.stat-box { background: rgba(8,8,13,0.95); border-radius: 12px; border: 1px solid #262636; padding: 10px 12px; }
.stat-label { color: #b0b0c0; font-size: 12px; }
.stat-value { font-size: 18px; font-weight: 700; margin-top: 4px; }
footer { text-align: center; margin-top: 40px; font-size: 12px; color: #5c5c70; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1c1c24; }
th { color: #9a9ab0; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }

@media (max-width: 720px) {
    .nav-links { position: fixed; top: var(--nav-h); left: 0; right: 0; flex-direction: column;
        align-items: stretch; background: rgba(6,6,10,0.97); padding: 8px; gap: 2px;
        border-bottom: 1px solid rgba(79,195,247,0.15); display: none; }
    .nav-links.open { display: flex; }
    .nav-links a, .nav-links button { width: 100%; text-align: left; padding: 12px 14px; }
    .nav-toggle { display: inline-block; }
    .wrap { padding: 24px 14px 40px; }
    .card { padding: 16px; border-radius: 14px; }
}
"""

SHELL_JS = """
function dexToggleNav() {
    var el = document.getElementById('dex-nav-links');
    if (el) el.classList.toggle('open');
}
function copyScript(id, btn) {
    var el = document.getElementById('code-' + id);
    if (!el) return;
    var text = el.textContent;
    var done = function () {
        var original = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function () { btn.textContent = original; btn.classList.remove('copied'); }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(function () { dexFallbackCopy(text, done); });
    } else {
        dexFallbackCopy(text, done);
    }
}
function dexFallbackCopy(text, done) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    done();
}
"""

NAV_ITEMS = [
    ("Home", "/"),
    ("Scripts", "/scripts"),
    ("Info", "/info"),
    ("Rules", "/rules"),
]


def render_logo_html() -> str:
    if LOGO_URL:
        return f'<img src="{html.escape(LOGO_URL)}" alt="{html.escape(SITE_NAME)} logo">'
    return ""


def render_nav(active_path: str, logged_in_user: Optional[str] = None) -> str:
    links = ""
    for label, href in NAV_ITEMS:
        cls = "active" if href == active_path else ""
        links += f'<a class="{cls}" href="{href}">{html.escape(label)}</a>'

    if logged_in_user:
        links += f'<a class="{"active" if active_path == "/dashboard" else ""}" href="/dashboard">Dashboard</a>'
        links += '<form method="post" action="/logout" style="display:inline;margin:0;"><button type="submit">Logout</button></form>'
    else:
        links += f'<a class="{"active" if active_path == "/login" else ""}" href="/login">Login</a>'

    links += f'<a class="{"active" if active_path == "/admin" else ""}" href="/admin">Admin</a>'

    return f"""
    <div class="nav">
        <div class="nav-left">
            {render_logo_html()}
            <span class="brand">{html.escape(SITE_NAME.upper())}</span>
        </div>
        <button class="nav-toggle" onclick="dexToggleNav()">&#9776;</button>
        <div class="nav-links" id="dex-nav-links">{links}</div>
    </div>
    """


def render_page(tab: str, active_path: str, body_html: str, request: Optional[Request] = None,
                 logged_in_user: Optional[str] = None, extra_head: str = "") -> str:
    """Shared shell for every HTML page: sets the browser tab to
    "SITE_NAME | tab", injects the current site-wide banner (if any) right
    under the nav so it's visible on every page, and renders the
    responsive nav bar."""
    banner_html = ""
    if banner_text:
        banner_html = f'<div class="site-banner">{html.escape(banner_text)}</div>'

    favicon_html = f'<link rel="icon" href="{html.escape(LOGO_URL)}">' if LOGO_URL else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(SITE_NAME)} | {html.escape(tab)}</title>
    {favicon_html}
    <style>{SHELL_CSS}</style>
    {extra_head}
</head>
<body>
    {render_nav(active_path, logged_in_user)}
    {banner_html}
    <div class="wrap">
        {body_html}
    </div>
    <script>{SHELL_JS}</script>
</body>
</html>"""


# =====================================================================
# SECTION 9 — GLOBAL HTTP MIDDLEWARE (unchanged behavior from original)
# =====================================================================

GLOBAL_HTTP_RATE_LIMIT = 200
GLOBAL_HTTP_RATE_WINDOW = 10.0


@app.middleware("http")
async def global_rate_limit_and_security_headers(request: Request, call_next):
    ip = _client_ip(request)

    if rate_limited(ip, "global_http", max_requests=GLOBAL_HTTP_RATE_LIMIT, window_seconds=GLOBAL_HTTP_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: data:; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    )
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    print(f"[ERROR] Unhandled exception on {request.url.path}: {exc}")
    return PlainTextResponse("INTERNAL_ERROR", status_code=500)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(rate_bucket_janitor())
    asyncio.create_task(username_cleanup_janitor())


# =====================================================================
# SECTION 10 — /secure, /ws, /logs, /usernames, /blacklisted,
# /announcements, /banner, /dexpaid/keys, /github/refresh
# (all unchanged from the original — same auth model, same rate limits)
# =====================================================================

current_wss: Optional[str] = None
SECURE_RATE_LIMIT = 20
SECURE_RATE_WINDOW = 10.0


@app.post("/secure")
async def set_wss(request: Request):
    global current_wss
    ip = _client_ip(request)
    if rate_limited(ip, "secure_post", max_requests=SECURE_RATE_LIMIT, window_seconds=SECURE_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("secure_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return JSONResponse({"error": "payload too large"}, status_code=413)
    raw_body = await request.body()
    if len(raw_body) > MAX_GENERIC_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    candidate = normalize_field(raw_body.decode("utf-8", errors="ignore").strip())
    if not candidate:
        return JSONResponse({"error": "empty"}, status_code=400)
    if _STRICT_SINGLE_LINE_PATTERN.search(candidate):
        return JSONResponse({"error": "invalid characters"}, status_code=400)
    if not WSS_URL_PATTERN.match(candidate):
        return JSONResponse({"error": "invalid format - expected a ws:// or wss:// URL"}, status_code=400)
    if contains_blocked_content(candidate):
        return JSONResponse({"error": "blocked content"}, status_code=400)
    current_wss = candidate
    return {"wss": current_wss}


@app.get("/secure")
async def get_wss(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "secure_get", max_requests=SECURE_RATE_LIMIT, window_seconds=SECURE_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("secure_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"wss": current_wss}


MAX_WS_CONNECTIONS_PER_IP = 5
WS_CONNECT_RATE_LIMIT = 20
WS_CONNECT_RATE_WINDOW = 60.0
WS_SEND_RATE_LIMIT = 30
WS_SEND_RATE_WINDOW = 10.0


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global sender_ws
    ip = _ws_client_ip(websocket)

    if await is_rate_limited("ws_sender_auth", ip):
        await websocket.close(code=4429)
        return
    if rate_limited(ip, "ws_connect", max_requests=WS_CONNECT_RATE_LIMIT, window_seconds=WS_CONNECT_RATE_WINDOW):
        await websocket.close(code=4429)
        return

    key_param = websocket.query_params.get("key", "")
    if not is_valid_key(key_param):
        await websocket.close(code=4401)
        return

    async with ws_count_lock:
        if ws_ip_connection_counts[ip] >= MAX_WS_CONNECTIONS_PER_IP:
            await websocket.close(code=4009)
            return
        ws_ip_connection_counts[ip] += 1

    await websocket.accept()
    role = "viewer"
    viewers.add(websocket)

    async with logs_lock:
        if stored_logs:
            try:
                await websocket.send_text(stored_logs[-1])
            except Exception:
                pass

    try:
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break

            if len(msg) > MAX_GENERIC_BODY:
                continue
            text = msg.strip()
            if not text:
                continue

            if role == "viewer":
                if rate_limited(ip, "ws_sender_auth_attempt", max_requests=10, window_seconds=60.0):
                    continue
                if constant_time_eq(text, API_KEY):
                    role = "sender"
                    sender_ws = websocket
                    viewers.discard(websocket)
                    await clear_attempts("ws_sender_auth", ip)
                else:
                    await record_failed_attempt("ws_sender_auth", ip)
                continue

            if role == "sender":
                if rate_limited(ip, "ws_send", max_requests=WS_SEND_RATE_LIMIT, window_seconds=WS_SEND_RATE_WINDOW):
                    continue
                log_entry = text
                async with logs_lock:
                    stored_logs.append(log_entry)
                    if len(stored_logs) > MAX_STORED_LOGS:
                        del stored_logs[0: len(stored_logs) - MAX_STORED_LOGS]
                    append_log_to_file(log_entry)
                    dead = []
                    for v in list(viewers):
                        try:
                            await v.send_text(log_entry)
                        except Exception:
                            dead.append(v)
                    for d in dead:
                        viewers.discard(d)
    except WebSocketDisconnect:
        pass
    finally:
        if role == "viewer":
            viewers.discard(websocket)
        elif role == "sender" and sender_ws is websocket:
            sender_ws = None
        async with ws_count_lock:
            ws_ip_connection_counts[ip] -= 1
            if ws_ip_connection_counts[ip] <= 0:
                ws_ip_connection_counts.pop(ip, None)


LOGS_POST_RATE_LIMIT = 20
LOGS_POST_RATE_WINDOW = 10.0
LOGS_GET_RATE_LIMIT = 30
LOGS_GET_RATE_WINDOW = 10.0


@app.post("/logs")
async def post_logs(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "logs_post", max_requests=LOGS_POST_RATE_LIMIT, window_seconds=LOGS_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    if reject_if_oversized(request, MAX_LOG_LEN):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_LOG_LEN:
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    msg = raw.decode(errors="ignore").strip()
    if not msg:
        return PlainTextResponse("EMPTY")
    if _CONTROL_CHAR_PATTERN.search(msg):
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)
    msg = normalize_field(msg)
    if contains_blocked_content(msg):
        return PlainTextResponse("REJECTED_BLOCKED_CONTENT", status_code=400)
    async with logs_lock:
        stored_logs.append(msg)
        if len(stored_logs) > MAX_STORED_LOGS:
            del stored_logs[0: len(stored_logs) - MAX_STORED_LOGS]
        append_log_to_file(msg)
        dead = []
        for v in list(viewers):
            try:
                await v.send_text(msg)
            except Exception:
                dead.append(v)
        for d in dead:
            viewers.discard(d)
    return PlainTextResponse("OK")


@app.get("/logs")
async def get_logs(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "logs_get", max_requests=LOGS_GET_RATE_LIMIT, window_seconds=LOGS_GET_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with logs_lock:
        return PlainTextResponse("\n".join(stored_logs))


USERNAME_POST_RATE_LIMIT = 8
USERNAME_POST_RATE_WINDOW = 10.0
USERNAME_GET_RATE_LIMIT = 30
USERNAME_GET_RATE_WINDOW = 10.0


@app.post("/usernames")
async def add_username(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "username_post", max_requests=USERNAME_POST_RATE_LIMIT, window_seconds=USERNAME_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    if await is_rate_limited("username_reject", ip):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    if reject_if_oversized(request, MAX_USERNAME_LEN + 16):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_USERNAME_LEN + 16:
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    raw_username = raw.decode(errors="ignore").strip()
    if not raw_username or len(raw_username) > MAX_USERNAME_LEN:
        return PlainTextResponse("EMPTY")
    if _ANY_WHITESPACE_PATTERN.search(raw_username):
        return PlainTextResponse("IGNORED_CONTAINS_WHITESPACE")
    if _CONTROL_CHAR_PATTERN.search(raw_username):
        await record_failed_attempt("username_reject", ip)
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)
    username = normalize_field(raw_username)
    if _ANY_WHITESPACE_PATTERN.search(username):
        return PlainTextResponse("IGNORED_CONTAINS_WHITESPACE")
    if not is_valid_username(username):
        await record_failed_attempt("username_reject", ip)
        return PlainTextResponse("REJECTED_INVALID_OR_BLOCKED", status_code=400)
    if has_excessive_repetition(username):
        await record_failed_attempt("username_reject", ip)
        return PlainTextResponse("REJECTED_SPAM_PATTERN", status_code=400)

    should_restart = False
    async with lock:
        if username.lower() not in stored_usernames_lower:
            stored_usernames.add(username)
            stored_usernames_lower.add(username.lower())
        _purge_bad_usernames_locked()
        save_usernames_to_file(stored_usernames)
        if len(stored_usernames) >= MAX_STORED_USERNAMES:
            should_restart = True

    await clear_attempts("username_reject", ip)
    if should_restart:
        asyncio.create_task(schedule_restart())
        return PlainTextResponse("OK_LIMIT_REACHED_RESTARTING")
    return PlainTextResponse("OK")


@app.get("/usernames")
async def get_usernames(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "username_get", max_requests=USERNAME_GET_RATE_LIMIT, window_seconds=USERNAME_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    async with lock:
        return PlainTextResponse("\n".join(sorted(stored_usernames)))


BLACKLIST_POST_RATE_LIMIT = 15
BLACKLIST_POST_RATE_WINDOW = 10.0
BLACKLIST_GET_RATE_LIMIT = 30
BLACKLIST_GET_RATE_WINDOW = 10.0


@app.post("/blacklisted")
async def add_blacklisted(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "blacklist_post", max_requests=BLACKLIST_POST_RATE_LIMIT, window_seconds=BLACKLIST_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("blacklist_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    username = raw.decode(errors="ignore").strip()
    if not username:
        return PlainTextResponse("EMPTY")
    async with blacklist_lock:
        if username not in blacklisted_usernames:
            blacklisted_usernames.add(username)
            save_blacklist_to_file(blacklisted_usernames)
    return PlainTextResponse("OK")


@app.post("/unblacklisted")
async def remove_blacklisted(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "blacklist_post", max_requests=BLACKLIST_POST_RATE_LIMIT, window_seconds=BLACKLIST_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("blacklist_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    username = raw.decode(errors="ignore").strip()
    if not username:
        return PlainTextResponse("EMPTY")
    async with blacklist_lock:
        if username in blacklisted_usernames:
            blacklisted_usernames.discard(username)
            save_blacklist_to_file(blacklisted_usernames)
    return PlainTextResponse("OK")


@app.get("/blacklisted")
async def get_blacklisted(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "blacklist_get", max_requests=BLACKLIST_GET_RATE_LIMIT, window_seconds=BLACKLIST_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    async with blacklist_lock:
        return PlainTextResponse("\n".join(sorted(blacklisted_usernames)))


ANNOUNCEMENT_POST_RATE_LIMIT = 10
ANNOUNCEMENT_POST_RATE_WINDOW = 10.0
ANNOUNCEMENT_GET_RATE_LIMIT = 60
ANNOUNCEMENT_GET_RATE_WINDOW = 10.0


@app.post("/announcements")
async def post_announcement(request: Request):
    global announcement_text, announcement_timestamp
    ip = _client_ip(request)
    if rate_limited(ip, "announcement_post", max_requests=ANNOUNCEMENT_POST_RATE_LIMIT, window_seconds=ANNOUNCEMENT_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("announcement_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    msg = raw.decode(errors="ignore").strip()
    async with announcement_lock:
        announcement_text = msg
        announcement_timestamp = time.time()
    return PlainTextResponse("OK")


@app.get("/announcements")
async def get_announcement(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "announcement_get", max_requests=ANNOUNCEMENT_GET_RATE_LIMIT, window_seconds=ANNOUNCEMENT_GET_RATE_WINDOW):
        return PlainTextResponse("", status_code=429)
    async with announcement_lock:
        now = time.time()
        if announcement_text and (now - announcement_timestamp) <= 1.0:
            return PlainTextResponse(announcement_text)
        return PlainTextResponse("")


BANNER_POST_RATE_LIMIT = 10
BANNER_POST_RATE_WINDOW = 10.0
BANNER_GET_RATE_LIMIT = 60
BANNER_GET_RATE_WINDOW = 10.0


@app.post("/banner")
async def post_banner(request: Request):
    global banner_text
    ip = _client_ip(request)
    if rate_limited(ip, "banner_post", max_requests=BANNER_POST_RATE_LIMIT, window_seconds=BANNER_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("banner_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)
    msg = normalize_field(raw.decode("utf-8", errors="ignore").strip())
    if len(msg) > MAX_BANNER_LEN:
        return PlainTextResponse("TOO_LONG", status_code=400)
    if _CONTROL_CHAR_PATTERN.search(msg):
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)
    async with banner_lock:
        banner_text = msg
        save_banner_to_file(banner_text)
    await clear_attempts("banner_auth", ip)
    return PlainTextResponse("OK")


@app.get("/banner")
async def get_banner(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "banner_get", max_requests=BANNER_GET_RATE_LIMIT, window_seconds=BANNER_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    async with banner_lock:
        return PlainTextResponse(banner_text)


DEXPAID_KEYS_RATE_LIMIT = 10
DEXPAID_KEYS_RATE_WINDOW = 60.0


@app.post("/dexpaid/keys")
async def create_dexpaid_key(request: Request):
    global last_generated_paid_key, last_generated_paid_loadstring
    ip = _client_ip(request)
    if rate_limited(ip, "dexpaid_keys_post", max_requests=DEXPAID_KEYS_RATE_LIMIT, window_seconds=DEXPAID_KEYS_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("dexpaid_keys_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return JSONResponse({"error": "payload too large"}, status_code=413)
    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    hours = parse_duration_hours(raw.decode(errors="ignore").strip())
    if hours is None:
        return JSONResponse({"error": f"invalid duration (0 < hours <= {MAX_KEY_DURATION_HOURS})"}, status_code=400)
    async with dexpaid_keys_lock:
        cleanup_expired_paid_keys()
        new_key = generate_paid_key(20)
        expiry = time.time() + hours * 3600.0
        dexpaid_keys[new_key] = expiry
        save_dexpaid_keys_to_file(dexpaid_keys)
        last_generated_paid_key = new_key
        last_generated_paid_loadstring = f'loadstring(game:HttpGet("{BASE_URL}/dexpaid?key={new_key}"))()'
    return JSONResponse({"key": new_key, "expires_at": expiry, "loadstring": last_generated_paid_loadstring})


GITHUB_REFRESH_RATE_LIMIT = 5
GITHUB_REFRESH_RATE_WINDOW = 60.0


@app.post("/github/refresh")
async def refresh_github(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "github_refresh_post", max_requests=GITHUB_REFRESH_RATE_LIMIT, window_seconds=GITHUB_REFRESH_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("github_refresh_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await force_refresh_github_cache()
    return JSONResponse({"ok": True, "message": "GitHub script cache cleared - next request re-fetches."})


# =====================================================================
# SECTION 11 — NEW: /info (Dex | Info) and /rules (Dex | Rules)
# =====================================================================

INFO_RATE_LIMIT = 30
INFO_RATE_WINDOW = 10.0


@app.get("/info")
async def info_page(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "info_get", max_requests=INFO_RATE_LIMIT, window_seconds=INFO_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    uptime_seconds = int(time.time() - APP_STARTED_AT)
    uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m {uptime_seconds % 60}s"

    endpoint_rows = "".join(
        f"<tr><td><code>{html.escape(m)}</code></td><td><code>{html.escape(p)}</code></td><td>{html.escape(d)}</td></tr>"
        for m, p, d in PUBLIC_ENDPOINT_TABLE
    )

    body = f"""
    <div class="card">
        <h1>System Info</h1>
        <p class="label">
            <span class="pill green">Online</span>
            <span class="pill">Uptime: {uptime_str}</span>
            <span class="pill purple">{"GitHub scripts connected" if github_configured() else "GitHub scripts not configured"}</span>
        </p>
        <p>This page lists every public endpoint this backend exposes and what it does. Endpoints
        marked <span class="pill red">Key</span> require an <code>X-Api-Key</code> header. Endpoints
        marked <span class="pill purple">Session</span> require being logged in via <a href="/login">/login</a>
        or the admin panel.</p>
    </div>
    <div class="card">
        <h2>Endpoints</h2>
        <table>
            <thead><tr><th>Method</th><th>Path</th><th>Description</th></tr></thead>
            <tbody>{endpoint_rows}</tbody>
        </table>
    </div>
    <div class="card">
        <h2>Executor usage</h2>
        <div class="code-row">
            <code id="code-info-example">loadstring(game:HttpGet("{BASE_URL}/dexfree"))()</code>
            <button class="copy-btn" onclick="copyScript('info-example', this)">Copy</button>
        </div>
        <p class="small-text" style="margin-top:10px;">Full script list with copy buttons: <a href="/scripts">/scripts</a></p>
    </div>
    """
    return HTMLResponse(render_page("Info", "/info", body, request))


PUBLIC_ENDPOINT_TABLE = [
    ("GET", "/", "Landing page"),
    ("GET", "/info", "This page — endpoint list and system status"),
    ("GET", "/rules", "Usage rules / terms"),
    ("GET", "/scripts", "Browse free loader scripts with copy-to-clipboard loadstrings"),
    ("GET", "/login", "Start Discord OAuth login"),
    ("GET", "/auth/discord/callback", "Discord OAuth redirect target"),
    ("POST", "/logout", "Clear your session"),
    ("GET/POST", "/dashboard", "Manage your own scripts (requires login)"),
    ("GET/POST", "/admin", "Admin dashboard (requires admin key)"),
    ("POST", "/admin/keys", "Generate a global access key (admin session)"),
    ("GET", "/admin/stats", "Live JSON stats for the admin dashboard (admin session)"),
    ("GET/POST", "/secure", "Get/set the current WSS relay URL (X-Api-Key)"),
    ("WS", "/ws", "Live log relay (requires ?key=)"),
    ("GET/POST", "/logs", "Public log line submission + key-gated read"),
    ("GET/POST", "/usernames", "Public username registry"),
    ("GET/POST", "/blacklisted", "Blacklist read (public) / write (X-Api-Key)"),
    ("GET/POST", "/banner", "Site-wide banner read (public) / write (X-Api-Key)"),
    ("GET/POST", "/announcements", "Short-lived popup announcement"),
    ("POST", "/dexpaid/keys", "Generate a DexPaid key (X-Api-Key)"),
    ("POST", "/github/refresh", "Force-refresh the GitHub script cache (X-Api-Key)"),
    ("GET", "/dexfree, /dexchilli, /dexserverhop, /dexhub, /dexautoroll", "Free loader scripts"),
    ("GET", "/dexpaid?key=...", "Paid loader script"),
    ("GET", "/{slug}", "Any user-created script endpoint"),
]

RULES_RATE_LIMIT = 30
RULES_RATE_WINDOW = 10.0


@app.get("/rules")
async def rules_page(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "rules_get", max_requests=RULES_RATE_LIMIT, window_seconds=RULES_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    body = f"""
    <div class="card">
        <h1>Rules</h1>
        <p class="label"><span class="pill purple">Please read before using {html.escape(SITE_NAME)}</span></p>
        <ul>
            <li>Don't submit links, invites, or slurs to any open text field (usernames, logs) — they're filtered and rejected automatically.</li>
            <li>Don't attempt to brute-force paid keys, HWID locks, or the admin login — repeated failures trigger a temporary lockout.</li>
            <li>Don't spam-create script endpoints or usernames — volume limits apply per IP.</li>
            <li>Paid keys are personal and tied to a script + HWID once first used — sharing them may get them revoked.</li>
            <li>Scripts you host under your own account are your responsibility.</li>
        </ul>
        <p class="small-text">These rules are enforced automatically wherever technically possible (rate limits, content filters,
        lockouts); the rest is on the honor system. Questions go through your server's usual support channel.</p>
    </div>
    """
    return HTMLResponse(render_page("Rules", "/rules", body, request))


# =====================================================================
# SECTION 12 — HOME PAGE (redesigned, same shell as everything else)
# =====================================================================

INDEX_RATE_LIMIT = 30
INDEX_RATE_WINDOW = 10.0


@app.get("/")
async def index(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "index_get", max_requests=INDEX_RATE_LIMIT, window_seconds=INDEX_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    body = f"""
    <div class="card">
        <h1>{html.escape(SITE_NAME)} API Backend</h1>
        <p class="label">
            <span class="pill green">Online</span>
            <span class="pill">Private Scripts</span>
            <span class="pill purple">Discord Login</span>
        </p>
        <p>This backend serves loader scripts to Roblox executors. Browsers get a generic
        "Private Script" response — real content is only returned to executor clients.</p>
        <div class="code-row">
            <code id="code-home-example">loadstring(game:HttpGet("{BASE_URL}/dexfree"))()</code>
            <button class="copy-btn" onclick="copyScript('home-example', this)">Copy</button>
        </div>
    </div>
    <div class="grid">
        <div class="card">
            <h2>Browse scripts</h2>
            <p class="small-text">Every free loader with a ready-to-copy loadstring.</p>
            <a class="btn" href="/scripts">Open /scripts</a>
        </div>
        <div class="card">
            <h2>Manage your own</h2>
            <p class="small-text">Login with Discord to create and manage your own script endpoints.</p>
            <a class="btn discord" href="/login">Login with Discord</a>
        </div>
        <div class="card">
            <h2>API reference</h2>
            <p class="small-text">Full endpoint list and system status.</p>
            <a class="btn secondary" href="/info">Open /info</a>
        </div>
    </div>
    """
    return HTMLResponse(render_page("Home", "/", body, request, logged_in_user=get_logged_in_user(request)))


# =====================================================================
# SECTION 13 — /scripts (redesigned via shared shell)
# =====================================================================

SCRIPTS_GET_RATE_LIMIT = 30
SCRIPTS_GET_RATE_WINDOW = 10.0


def build_scripts_body_html() -> str:
    cards = ""
    for name, meta in FIXED_SCRIPTS.items():
        if name == "dexpaid":
            continue
        endpoint = f"{BASE_URL}/{name}"
        loadstring = f'loadstring(game:HttpGet("{html.escape(endpoint)}"))()'
        tagline = SCRIPT_TAGLINES.get(name, "")
        cards += f"""
        <div class="card">
            <h2>{html.escape(meta['label'])} <span class="pill green">Free</span></h2>
            <p class="small-text">{html.escape(tagline)}</p>
            <p class="small-text">Endpoint: <code>/{html.escape(name)}</code></p>
            <div class="code-row">
                <code id="code-{html.escape(name)}">{loadstring}</code>
                <button class="copy-btn" onclick="copyScript('{html.escape(name)}', this)">Copy</button>
            </div>
        </div>
        """
    return f"""
    <div class="card">
        <h1>Scripts</h1>
        <p class="label">
            <span class="pill green">Free</span>
            <span class="pill">No key required</span>
            <span class="pill purple">Always up to date</span>
        </p>
        <p class="small-text">Looking for the paid script? Use <code>/dexpaid?key=YOUR_KEY</code> instead.</p>
    </div>
    <div class="grid">{cards}</div>
    """


@app.get("/scripts")
async def scripts_page(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "scripts_get", max_requests=SCRIPTS_GET_RATE_LIMIT, window_seconds=SCRIPTS_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    body = build_scripts_body_html()
    return HTMLResponse(render_page("Scripts", "/scripts", body, request, logged_in_user=get_logged_in_user(request)))


# =====================================================================
# SECTION 14 — DISCORD LOGIN (replaces username/password /home login).
# Old username/password accounts still work for script *ownership*
# lookups (unchanged users.json format, now keyed by Discord user id
# instead of a chosen username), but there is no more password form.
# =====================================================================

def get_logged_in_user(request: Request) -> Optional[str]:
    token = request.cookies.get("dex_session")
    username = verify_session_token(token, SESSION_MAX_AGE)
    if username and username in users:
        return username
    return None


def set_session_cookie(resp, subject: str):
    token = create_session_token(subject)
    resp.set_cookie("dex_session", token, httponly=True, secure=True, samesite="lax", max_age=SESSION_MAX_AGE)


LOGIN_RATE_LIMIT = 15
LOGIN_RATE_WINDOW = 60.0


@app.get("/login")
async def login_page(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "login_get", max_requests=LOGIN_RATE_LIMIT, window_seconds=LOGIN_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    if get_logged_in_user(request):
        return RedirectResponse("/dashboard", status_code=302)

    if not discord_configured():
        body = """
        <div class="card">
            <h1>Login</h1>
            <div class="error">Discord login isn't configured yet on this server. Set
            DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET (see /info for the full env var list).</div>
        </div>
        """
        return HTMLResponse(render_page("Login", "/login", body, request))

    state = secrets.token_urlsafe(24)
    state_token = create_session_token(f"oauth:{state}")

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "consent",
    }
    auth_url = f"{DISCORD_API_BASE}/oauth2/authorize?{urllib.parse.urlencode(params)}"

    body = f"""
    <div class="card">
        <h1>Login</h1>
        <p class="label"><span class="pill purple">Discord required</span></p>
        <p>Script management now goes through Discord instead of a username and password.
        You'll be redirected to Discord to approve access, then brought back here.</p>
        <a class="btn discord" href="{html.escape(auth_url)}">Continue with Discord</a>
    </div>
    """
    resp = HTMLResponse(render_page("Login", "/login", body, request))
    resp.set_cookie("dex_oauth_state", state_token, httponly=True, secure=True, samesite="lax", max_age=OAUTH_STATE_MAX_AGE)
    return resp


def _discord_token_exchange(code: str) -> Optional[dict]:
    data = urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{DISCORD_API_BASE}/oauth2/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[DISCORD] token exchange failed: {e}")
        return None


def _discord_fetch_user(access_token: str) -> Optional[dict]:
    req = urllib.request.Request(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[DISCORD] user fetch failed: {e}")
        return None


@app.get("/auth/discord/callback")
async def discord_callback(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "discord_callback_get", max_requests=LOGIN_RATE_LIMIT, window_seconds=LOGIN_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    if not discord_configured():
        return PlainTextResponse("Discord login not configured.", status_code=500)

    error = request.query_params.get("error")
    if error:
        return RedirectResponse("/login", status_code=302)

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state:
        return RedirectResponse("/login", status_code=302)

    state_cookie = request.cookies.get("dex_oauth_state")
    verified = verify_session_token(state_cookie, OAUTH_STATE_MAX_AGE)
    if not verified or verified != f"oauth:{state}":
        return RedirectResponse("/login", status_code=302)

    token_data = await asyncio.to_thread(_discord_token_exchange, code)
    if not token_data or "access_token" not in token_data:
        return RedirectResponse("/login", status_code=302)

    user_data = await asyncio.to_thread(_discord_fetch_user, token_data["access_token"])
    if not user_data or "id" not in user_data:
        return RedirectResponse("/login", status_code=302)

    discord_id = str(user_data["id"])
    discord_username = str(user_data.get("username", "unknown"))[:64]
    avatar_hash = user_data.get("avatar")
    subject = f"discord:{discord_id}"

    async with users_lock:
        existing = users.get(subject)
        if existing:
            existing["discord_username"] = discord_username
            existing["avatar"] = avatar_hash
            existing["last_login"] = time.time()
        else:
            users[subject] = {
                "username": subject,
                "discord_id": discord_id,
                "discord_username": discord_username,
                "avatar": avatar_hash,
                "created_at": time.time(),
                "last_login": time.time(),
            }
        save_users_to_file()

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.delete_cookie("dex_oauth_state")
    set_session_cookie(resp, subject)
    return resp


@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("dex_session")
    return resp


# Backward-compat: old bookmarks/links to /home now land on /login or
# /dashboard depending on session state, instead of a dead link.
@app.get("/home")
async def home_redirect(request: Request):
    if get_logged_in_user(request):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


# =====================================================================
# SECTION 15 — /dashboard (was /home's logged-in view): create/edit/
# delete your own script endpoints, generate paid keys for them.
# =====================================================================

def build_dashboard_body(subject: str, message: str = "", success: str = "") -> str:
    msg_html = ""
    if message:
        msg_html += f'<div class="error">{html.escape(message)}</div>'
    if success:
        msg_html += f'<div class="success">{html.escape(success)}</div>'

    user = users.get(subject, {})
    display_name = user.get("discord_username", subject)

    user_scripts = [s for s in scripts.values() if s.get("owner") == subject]
    cards_html = ""
    for s in user_scripts:
        slug = s["slug"]
        name = s["name"]
        code = html.escape(s.get("code", ""))
        is_paid = s.get("is_paid", False)
        hwid_lock = s.get("hwid_lock", False)
        last_key = s.get("last_key", "")
        last_loadstring = s.get("last_loadstring", "")
        endpoint = f"{BASE_URL}/{slug}"
        cards_html += f"""
        <div class="card">
            <h2>{html.escape(name)} <span class="small-text">({html.escape(slug)})</span></h2>
            <p class="label">
                <span class="pill {'red' if is_paid else 'green'}">{'Paid' if is_paid else 'Free'}</span>
                <span class="pill {'purple' if hwid_lock else ''}">{'HWID Locked' if hwid_lock else 'HWID Unlocked'}</span>
            </p>
            <p class="small-text">Endpoint: <code>{html.escape(endpoint)}</code></p>
            <div class="code-row">
                <code id="code-{html.escape(slug)}">loadstring(game:HttpGet("{html.escape(endpoint)}"))()</code>
                <button class="copy-btn" onclick="copyScript('{html.escape(slug)}', this)">Copy</button>
            </div>
            <p class="small-text" style="margin-top:10px;">Last generated key:</p>
            <div class="logs-box">{html.escape(last_key or 'No key yet.')}</div>
            <p class="small-text" style="margin-top:10px;">Last generated loadstring (paid):</p>
            <div class="logs-box">{html.escape(last_loadstring or 'No paid loadstring yet.')}</div>
            <p class="small-text" style="margin-top:10px;">Edit script:</p>
            <form method="post" action="/dashboard">
                <input type="hidden" name="action" value="update_script">
                <input type="hidden" name="slug" value="{html.escape(slug)}">
                <label class="label">Script Name</label>
                <input type="text" name="name" value="{html.escape(name)}" maxlength="48">
                <label class="label">Script Code (Lua)</label>
                <textarea name="code">{code}</textarea>
                <label class="label">Paid? (yes/no)</label>
                <input type="text" name="is_paid" value="{'yes' if is_paid else 'no'}">
                <label class="label">HWID Lock? (yes/no)</label>
                <input type="text" name="hwid_lock" value="{'yes' if hwid_lock else 'no'}">
                <button type="submit">Save Changes</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Generate paid key for this script:</p>
            <form method="post" action="/dashboard">
                <input type="hidden" name="action" value="generate_key">
                <input type="hidden" name="slug" value="{html.escape(slug)}">
                <label class="label">Duration (hours, max {MAX_KEY_DURATION_HOURS})</label>
                <input type="text" name="hours" placeholder="e.g. 1, 5, 10">
                <button type="submit">Generate Key</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Delete this script:</p>
            <form method="post" action="/dashboard">
                <input type="hidden" name="action" value="delete_script">
                <input type="hidden" name="slug" value="{html.escape(slug)}">
                <button type="submit" class="danger">Delete Script</button>
            </form>
        </div>
        """

    if not cards_html:
        cards_html = '<div class="card"><h2>No scripts yet</h2><p class="small-text">Create your first script below.</p></div>'

    body = f"""
    <div class="card">
        <h1>Dashboard</h1>
        <p class="label"><span class="pill">Logged in as {html.escape(display_name)}</span></p>
        {msg_html}
    </div>
    <div class="card">
        <h2>Create New Script</h2>
        <p class="small-text">Name determines endpoint. Spaces become dashes. Example: "Dex 2" -> /Dex-2</p>
        <form method="post" action="/dashboard">
            <input type="hidden" name="action" value="create_script">
            <label class="label">Script Name</label>
            <input type="text" name="name" placeholder="e.g. Dexnew, Dex 2" maxlength="48">
            <label class="label">Script Code (Lua)</label>
            <textarea name="code" placeholder="Paste your Lua script here"></textarea>
            <label class="label">Paid? (yes/no)</label>
            <input type="text" name="is_paid" placeholder="yes or no">
            <label class="label">HWID Lock? (yes/no)</label>
            <input type="text" name="hwid_lock" placeholder="yes or no">
            <button type="submit">Add Script</button>
        </form>
    </div>
    <div class="grid">{cards_html}</div>
    """
    return body


HOME_GET_RATE_LIMIT = 30
HOME_GET_RATE_WINDOW = 10.0
HOME_POST_RATE_LIMIT = 20
HOME_POST_RATE_WINDOW = 10.0


@app.get("/dashboard")
async def dashboard_get(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "dashboard_get", max_requests=HOME_GET_RATE_LIMIT, window_seconds=HOME_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    subject = get_logged_in_user(request)
    if not subject:
        return RedirectResponse("/login", status_code=302)
    body = build_dashboard_body(subject)
    return HTMLResponse(render_page("Dashboard", "/dashboard", body, request, logged_in_user=subject))


@app.post("/dashboard")
async def dashboard_post(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "dashboard_post", max_requests=HOME_POST_RATE_LIMIT, window_seconds=HOME_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    subject = get_logged_in_user(request)
    if not subject:
        return RedirectResponse("/login", status_code=302)

    if reject_if_oversized(request, MAX_FORM_BODY):
        return PlainTextResponse("Payload too large.", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_FORM_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)

    data = parse_qs(raw.decode(errors="ignore"))
    action = data.get("action", [""])[0]

    def render(msg="", ok=""):
        return HTMLResponse(render_page(
            "Dashboard", "/dashboard",
            build_dashboard_body(subject, message=msg, success=ok),
            request, logged_in_user=subject,
        ))

    if action == "create_script":
        name = data.get("name", [""])[0].strip()
        code = data.get("code", [""])[0]
        is_paid_str = data.get("is_paid", ["no"])[0].strip().lower()
        hwid_lock_str = data.get("hwid_lock", ["no"])[0].strip().lower()

        if not name or not code:
            return render(msg="Name and code required.")
        if len(code.encode("utf-8")) > MAX_SCRIPT_BODY:
            return render(msg="Script code is too large.")

        slug = make_slug(name)
        if not slug or slug.lower() in RESERVED_PATHS_LOWER:
            return render(msg="Invalid or reserved script name.")

        async with scripts_lock:
            if any(k.lower() == slug.lower() for k in scripts.keys()):
                return render(msg="Endpoint already exists.")
            scripts[slug] = {
                "name": name, "slug": slug, "code": code,
                "is_paid": is_paid_str == "yes", "hwid_lock": hwid_lock_str == "yes",
                "owner": subject, "created_at": time.time(), "updated_at": time.time(),
                "keys": {}, "last_key": "", "last_loadstring": "",
            }
            save_scripts_to_file()
        return render(ok="Script created.")

    if action == "update_script":
        slug = data.get("slug", [""])[0].strip()
        name = data.get("name", [""])[0].strip()
        code = data.get("code", [""])[0]
        is_paid_str = data.get("is_paid", ["no"])[0].strip().lower()
        hwid_lock_str = data.get("hwid_lock", ["no"])[0].strip().lower()

        async with scripts_lock:
            s = scripts.get(slug)
            if not s or s.get("owner") != subject:
                return render(msg="Script not found or not owned by you.")
            if s.get("github_managed"):
                return render(msg="This script is managed via the GitHub repo and can't be edited here.")
            if len(code.encode("utf-8")) > MAX_SCRIPT_BODY:
                return render(msg="Script code is too large.")
            s["name"] = name or s["name"]
            s["code"] = code
            s["is_paid"] = is_paid_str == "yes"
            s["hwid_lock"] = hwid_lock_str == "yes"
            s["updated_at"] = time.time()
            save_scripts_to_file()
        return render(ok="Script updated.")

    if action == "delete_script":
        slug = data.get("slug", [""])[0].strip()
        async with scripts_lock:
            s = scripts.get(slug)
            if not s or s.get("owner") != subject:
                return render(msg="Script not found or not owned by you.")
            if s.get("github_managed"):
                return render(msg="This script is managed via the GitHub repo and can't be deleted here.")
            scripts.pop(slug, None)
            save_scripts_to_file()
        return render(ok="Script deleted.")

    if action == "generate_key":
        slug = data.get("slug", [""])[0].strip()
        hours = parse_duration_hours(data.get("hours", [""])[0].strip())
        if hours is None:
            return render(msg="Invalid duration.")
        async with scripts_lock:
            s = scripts.get(slug)
            if not s or s.get("owner") != subject:
                return render(msg="Script not found or not owned by you.")
            new_key = generate_paid_key(20)
            expiry = time.time() + hours * 3600.0
            keys = s.get("keys", {})
            keys[new_key] = {"expiry": expiry, "hwid": None}
            s["keys"] = keys
            s["last_key"] = new_key
            s["last_loadstring"] = f'loadstring(game:HttpGet("{BASE_URL}/{slug}?key={new_key}&hwid=YOUR_HWID"))()'
            save_scripts_to_file()
        return render(ok="Key generated.")

    return render(msg="Unknown action.")


# =====================================================================
# SECTION 16 — ADMIN PANEL (still view-only for existing data, PLUS the
# new requested feature: generate global access keys from here)
# =====================================================================

ADMIN_GET_RATE_LIMIT = 20
ADMIN_GET_RATE_WINDOW = 60.0


def admin_login_form_body(error: str = "") -> str:
    err_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return f"""
    <div class="card">
        <h1>Admin Login</h1>
        <p class="label">
            <span class="pill">Private System</span>
            <span class="pill red">Key Protected</span>
        </p>
        <p class="small-text">Enter the admin key to view scripts, blacklist, announcements, the banner,
        paid keys, users, and generate global access keys.</p>
        <form method="post" action="/admin">
            <label class="label">Admin Key</label>
            <input type="password" name="key" placeholder="Enter admin key">
            <button type="submit">Enter</button>
        </form>
        {err_html}
    </div>
    """


@app.get("/admin")
async def admin_get(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "admin_get", max_requests=ADMIN_GET_RATE_LIMIT, window_seconds=ADMIN_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    token = request.cookies.get("dex_admin_session")
    if verify_session_token(token, ADMIN_SESSION_MAX_AGE) == "admin":
        body = await build_admin_dashboard_body()
        return HTMLResponse(render_page("Admin", "/admin", body, request, logged_in_user=get_logged_in_user(request)))
    body = admin_login_form_body()
    return HTMLResponse(render_page("Admin", "/admin", body, request, logged_in_user=get_logged_in_user(request)))


def _format_age(fetched_at: Optional[float]) -> str:
    if not fetched_at:
        return "never"
    delta = max(0, int(time.time() - fetched_at))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


async def build_fixed_script_card(name: str) -> str:
    meta = FIXED_SCRIPTS[name]
    content = await get_github_script(name, meta["file"], meta["default"])
    cache_meta = get_cache_meta(name)
    source = cache_meta.get("source") if cache_meta else "unknown"
    fetched_at = cache_meta.get("fetched_at") if cache_meta else None
    repo_path = GITHUB_SCRIPT_PATHS.get(name, "")

    if github_configured():
        repo_link = f"{github_repo_url()}/{repo_path}"
        source_line = (
            f'Source: <a href="{html.escape(repo_link)}" target="_blank" rel="noopener">'
            f'{html.escape(GITHUB_OWNER)}/{html.escape(GITHUB_REPO)}@{html.escape(GITHUB_BRANCH)} :: {html.escape(repo_path)}</a>'
        )
    else:
        source_line = "Source: GitHub repo not configured — set DEX_GITHUB_OWNER / DEX_GITHUB_REPO."

    return f"""
    <div class="card">
        <h2>{html.escape(meta['label'])}</h2>
        <p class="label">
            <span class="pill purple">Read-only</span>
            <span class="pill">{html.escape(source or 'unknown')}</span>
            <span class="pill">fetched {html.escape(_format_age(fetched_at))}</span>
        </p>
        <p class="small-text">{source_line}</p>
        <p class="small-text" style="margin-top:10px;">Current content (read-only):</p>
        <div class="logs-box">{html.escape(content)}</div>
    </div>
    """


async def build_admin_dashboard_body() -> str:
    fixed_cards_html = ""
    for name in ("dexchilli", "dexfree", "dexserverhop", "dexhub", "dexpaid", "dexautoroll"):
        fixed_cards_html += await build_fixed_script_card(name)

    now = time.time()
    keys_preview_lines = [f"{k}  |  expires in {max(0, int(exp - now))}s" for k, exp in dexpaid_keys.items()]
    keys_preview_text = "\n".join(keys_preview_lines) if keys_preview_lines else "No paid keys."

    users_lines = [f"{u.get('discord_username', u.get('username'))} | {u['username']} | created: {int(u.get('created_at', 0))}" for u in users.values()]
    users_preview = "\n".join(users_lines) if users_lines else "No users."

    scripts_lines = [
        f"{s['name']} ({s['slug']}) | owner: {s['owner']} | paid: {s['is_paid']} | "
        f"hwid_lock: {s['hwid_lock']}{' | GITHUB-LOCKED' if s.get('github_managed') else ''}"
        for s in scripts.values()
    ]
    scripts_preview = "\n".join(scripts_lines) if scripts_lines else "No scripts."

    access_keys_lines = [
        f"{k} | note: {meta.get('note', '')} | expires: {'never' if not meta.get('expiry') else int(meta['expiry'] - now)}s"
        for k, meta in access_keys.items()
    ]
    access_keys_preview = "\n".join(access_keys_lines) if access_keys_lines else "No access keys generated yet."

    github_status = (
        f"Connected to {html.escape(GITHUB_OWNER)}/{html.escape(GITHUB_REPO)}@{html.escape(GITHUB_BRANCH)}"
        if github_configured() else "Not configured"
    )
    discord_status = "Configured" if discord_configured() else "Not configured — set DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET"

    body = f"""
    <div class="card">
        <h1>Admin Control Center</h1>
        <p class="label">
            <span class="pill">GitHub: {html.escape(github_status)}</span>
            <span class="pill">Discord login: {html.escape(discord_status)}</span>
        </p>
        <div class="stats-grid">
            <div class="stat-box"><div class="stat-label">Registered Usernames</div><div class="stat-value" id="stat-usernames">0</div></div>
            <div class="stat-box"><div class="stat-label">Blacklisted Users</div><div class="stat-value" id="stat-blacklisted">0</div></div>
            <div class="stat-box"><div class="stat-label">Total Logs</div><div class="stat-value" id="stat-logs">0</div></div>
            <div class="stat-box"><div class="stat-label">Viewers Connected</div><div class="stat-value" id="stat-viewers">0</div></div>
            <div class="stat-box"><div class="stat-label">Sender Connected</div><div class="stat-value" id="stat-sender">No</div></div>
        </div>
        <p class="small-text" style="margin-top:10px;">Last log entry:</p>
        <div class="logs-box" id="last-log-box">No logs yet.</div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Site Banner</h2>
            <p class="small-text">Shown on every page. Setting it here posts to /banner using your admin session.</p>
            <form method="post" action="/admin/banner">
                <input type="text" name="text" placeholder="Banner text" maxlength="{MAX_BANNER_LEN}" value="{html.escape(banner_text)}">
                <button type="submit">Set Banner</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Current:</p>
            <div class="logs-box" id="banner-preview-box">{html.escape(banner_text) or 'No banner set.'}</div>
        </div>

        <div class="card">
            <h2>Generate Access Key</h2>
            <p class="small-text">Global keys, separate from per-script DexPaid keys. Useful for general
            API access grants. Optional note is for your own reference only.</p>
            <form method="post" action="/admin/keys">
                <label class="label">Duration hours (blank = never expires)</label>
                <input type="text" name="hours" placeholder="e.g. 24, 168, blank for permanent">
                <label class="label">Note (optional)</label>
                <input type="text" name="note" placeholder="e.g. for @someuser" maxlength="120">
                <button type="submit">Generate Key</button>
            </form>
            <p class="small-text" style="margin-top:10px;">All access keys:</p>
            <div class="logs-box" id="access-keys-box">{access_keys_preview}</div>
        </div>

        <div class="card">
            <h2>Announcements</h2>
            <p class="small-text">Short-lived popup. Set via POST /announcements with X-Api-Key.</p>
            <div class="logs-box" id="announcement-preview-box">No active announcement.</div>
        </div>

        <div class="card">
            <h2>Blacklisted Users</h2>
            <p class="small-text">Manage via POST /blacklisted or /unblacklisted with X-Api-Key.</p>
            <div class="logs-box" id="blacklist-preview-box"></div>
        </div>

        <div class="card">
            <h2>Recent Logs</h2>
            <div class="logs-box" id="recent-logs-box">No logs.</div>
        </div>

        <div class="card">
            <h2>DexPaid Keys</h2>
            <p class="small-text" style="margin-top:10px;">Last generated key:</p>
            <div class="logs-box" id="dexpaid-last-key-box">No key generated yet.</div>
            <p class="small-text" style="margin-top:10px;">All active paid keys:</p>
            <div class="logs-box" id="dexpaid-keys-box">{keys_preview_text}</div>
        </div>

        <div class="card">
            <h2>Users</h2>
            <div class="logs-box" id="admin-users-box">{users_preview}</div>
        </div>

        <div class="card">
            <h2>Scripts Overview</h2>
            <div class="logs-box" id="admin-scripts-box">{scripts_preview}</div>
        </div>
    </div>

    <div class="grid">{fixed_cards_html}</div>

    <script>
        async function refreshStats() {{
            try {{
                const res = await fetch('/admin/stats', {{ cache: 'no-store', credentials: 'same-origin' }});
                if (!res.ok) return;
                const data = await res.json();
                document.getElementById('stat-usernames').textContent = data.usernames_count;
                document.getElementById('stat-blacklisted').textContent = data.blacklisted_count;
                document.getElementById('stat-logs').textContent = data.logs_count;
                document.getElementById('stat-viewers').textContent = data.viewers_count;
                document.getElementById('stat-sender').textContent = data.sender_connected ? 'Yes' : 'No';
                document.getElementById('last-log-box').textContent = data.last_log || 'No logs yet.';
                document.getElementById('recent-logs-box').textContent = data.recent_logs || 'No logs.';
                document.getElementById('announcement-preview-box').textContent = data.announcement || 'No active announcement.';
                document.getElementById('blacklist-preview-box').textContent = data.blacklisted_list || '';
                document.getElementById('dexpaid-keys-box').textContent = data.dexpaid_keys_preview || 'No paid keys.';
                document.getElementById('dexpaid-last-key-box').textContent = data.dexpaid_last_key || 'No key generated yet.';
                document.getElementById('access-keys-box').textContent = data.access_keys_preview || 'No access keys generated yet.';
            }} catch (e) {{ console.error(e); }}
        }}
        document.addEventListener('DOMContentLoaded', () => {{ refreshStats(); setInterval(refreshStats, 3000); }});
    </script>
    """
    return body


@app.post("/admin")
async def admin_post(request: Request):
    ip = _client_ip(request)
    if await is_rate_limited("admin_login", ip):
        body = admin_login_form_body("Too many failed attempts. Try again later.")
        return HTMLResponse(render_page("Admin", "/admin", body, request), status_code=429)

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("Payload too large.", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)
    data = parse_qs(raw.decode(errors="ignore"))
    key = data.get("key", [""])[0]

    if not is_valid_key(key):
        await record_failed_attempt("admin_login", ip)
        body = admin_login_form_body("Invalid key.")
        return HTMLResponse(render_page("Admin", "/admin", body, request))

    await clear_attempts("admin_login", ip)
    body = await build_admin_dashboard_body()
    resp = HTMLResponse(render_page("Admin", "/admin", body, request))
    admin_token = create_session_token("admin")
    resp.set_cookie("dex_admin_session", admin_token, httponly=True, secure=True, samesite="strict", max_age=ADMIN_SESSION_MAX_AGE)
    return resp


def require_admin_session(request: Request) -> bool:
    token = request.cookies.get("dex_admin_session")
    return verify_session_token(token, ADMIN_SESSION_MAX_AGE) == "admin"


ADMIN_ACTION_RATE_LIMIT = 20
ADMIN_ACTION_RATE_WINDOW = 60.0


@app.post("/admin/banner")
async def admin_set_banner(request: Request):
    """Lets the admin set the site banner from inside the dashboard using
    their existing admin session cookie, instead of needing to hand-craft
    a curl request with X-Api-Key. POST /banner (with X-Api-Key) still
    works exactly as before for scripted/API use."""
    global banner_text
    ip = _client_ip(request)
    if rate_limited(ip, "admin_action", max_requests=ADMIN_ACTION_RATE_LIMIT, window_seconds=ADMIN_ACTION_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    if not require_admin_session(request):
        return PlainTextResponse("Unauthorized - please log in at /admin again.", status_code=401)

    raw = await request.body()
    if len(raw) > MAX_FORM_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)
    data = parse_qs(raw.decode(errors="ignore"))
    msg = normalize_field(data.get("text", [""])[0].strip())

    if len(msg) > MAX_BANNER_LEN:
        body = admin_login_form_body() if not require_admin_session(request) else await build_admin_dashboard_body()
        return HTMLResponse(render_page("Admin", "/admin", body, request), status_code=400)
    if _CONTROL_CHAR_PATTERN.search(msg):
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)

    async with banner_lock:
        banner_text = msg
        save_banner_to_file(banner_text)

    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/keys")
async def admin_generate_access_key(request: Request):
    """The requested 'generate keys from /admin' feature: a global access
    key, independent of the per-script DexPaid keys, manageable entirely
    from the dashboard using the admin session cookie."""
    ip = _client_ip(request)
    if rate_limited(ip, "admin_action", max_requests=ADMIN_ACTION_RATE_LIMIT, window_seconds=ADMIN_ACTION_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    if not require_admin_session(request):
        return PlainTextResponse("Unauthorized - please log in at /admin again.", status_code=401)

    raw = await request.body()
    if len(raw) > MAX_FORM_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)
    data = parse_qs(raw.decode(errors="ignore"))
    hours_str = data.get("hours", [""])[0].strip()
    note = normalize_field(data.get("note", [""])[0].strip())[:120]

    expiry = None
    if hours_str:
        hours = parse_duration_hours(hours_str)
        if hours is None:
            return PlainTextResponse("Invalid duration.", status_code=400)
        expiry = time.time() + hours * 3600.0

    async with access_keys_lock:
        cleanup_expired_access_keys()
        new_key = generate_paid_key(28)
        access_keys[new_key] = {"created_at": time.time(), "expiry": expiry, "note": note}
        save_access_keys_to_file()

    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/update")
async def admin_update(request: Request):
    if not require_admin_session(request):
        return PlainTextResponse("Unauthorized - please log in at /admin again.", status_code=401)
    return PlainTextResponse(
        "Use /admin/banner or /admin/keys from the dashboard, or the API directly with "
        "X-Api-Key: POST /announcements, /banner, /blacklisted, /unblacklisted, /dexpaid/keys, /github/refresh.",
        status_code=403,
    )


ADMIN_STATS_RATE_LIMIT = 30
ADMIN_STATS_RATE_WINDOW = 10.0


@app.get("/admin/stats")
async def admin_stats(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "admin_stats_get", max_requests=ADMIN_STATS_RATE_LIMIT, window_seconds=ADMIN_STATS_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    if not require_admin_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    global last_generated_paid_key, last_generated_paid_loadstring

    async with logs_lock:
        local_logs_count = len(stored_logs)
        last_log = stored_logs[-1] if stored_logs else ""
        recent_logs = "\n".join(stored_logs[-20:]) if stored_logs else ""
    async with lock:
        local_usernames_count = len(stored_usernames)
    async with blacklist_lock:
        blacklisted_count = len(blacklisted_usernames)
        blacklisted_list = "\n".join(sorted(blacklisted_usernames))
    async with announcement_lock:
        announcement = announcement_text
    async with dexpaid_keys_lock:
        cleanup_expired_paid_keys()
        now = time.time()
        preview_lines = [f"{k}  |  expires in {max(0, int(exp - now))}s" for k, exp in dexpaid_keys.items()]
        dexpaid_keys_preview = "\n".join(preview_lines) if preview_lines else "No paid keys."
    async with access_keys_lock:
        cleanup_expired_access_keys()
        now2 = time.time()
        ak_lines = [
            f"{k} | note: {meta.get('note', '')} | expires: {'never' if not meta.get('expiry') else int(meta['expiry'] - now2)}s"
            for k, meta in access_keys.items()
        ]
        access_keys_preview = "\n".join(ak_lines) if ak_lines else "No access keys generated yet."

    sender_connected = sender_ws is not None
    viewers_count = len(viewers)

    return JSONResponse(
        {
            "usernames_count": local_usernames_count,
            "blacklisted_count": blacklisted_count,
            "logs_count": local_logs_count,
            "viewers_count": viewers_count,
            "sender_connected": sender_connected,
            "last_log": last_log,
            "recent_logs": recent_logs,
            "announcement": announcement,
            "blacklisted_list": blacklisted_list,
            "dexpaid_keys_preview": dexpaid_keys_preview,
            "dexpaid_last_key": last_generated_paid_key,
            "access_keys_preview": access_keys_preview,
        },
        headers={"Cache-Control": "no-store"},
    )


# =====================================================================
# SECTION 17 — LOADER ENDPOINTS (unchanged logic from original)
# =====================================================================

def is_executor(request: Request) -> bool:
    ua = request.headers.get("User-Agent", "")
    ua_lower = ua.lower()
    return ("roblox" in ua_lower) or ("wininet" in ua_lower)


LOADER_RATE_LIMIT = 30
LOADER_RATE_WINDOW = 10.0


@app.get("/dexfree")
async def dexfree(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "loader_get", max_requests=LOADER_RATE_LIMIT, window_seconds=LOADER_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(await get_github_script("dexfree", DEXFREE_FILE, DEFAULT_DEXFREE))


@app.get("/dexchilli")
async def dexchilli(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "loader_get", max_requests=LOADER_RATE_LIMIT, window_seconds=LOADER_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(await get_github_script("dexchilli", DEXCHILLI_FILE, DEFAULT_DEXCHILLI))


@app.get("/dexserverhop")
async def dexserverhop(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "loader_get", max_requests=LOADER_RATE_LIMIT, window_seconds=LOADER_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(await get_github_script("dexserverhop", DEXSERVERHOP_FILE, DEFAULT_DEXSERVERHOP))


@app.get("/dexhub")
async def dexhub(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "loader_get", max_requests=LOADER_RATE_LIMIT, window_seconds=LOADER_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(await get_github_script("dexhub", DEXHUB_FILE, DEFAULT_DEXHUB))


@app.get("/dexautoroll")
async def dexautoroll(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "loader_get", max_requests=LOADER_RATE_LIMIT, window_seconds=LOADER_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(await get_github_script("dexautoroll", DEXAUTOROLL_FILE, DEFAULT_DEXAUTOROLL))


DEXPAID_KEY_CHECK_RATE_LIMIT = 20
DEXPAID_KEY_CHECK_RATE_WINDOW = 60.0


@app.get("/dexpaid")
async def dexpaid(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "loader_get", max_requests=LOADER_RATE_LIMIT, window_seconds=LOADER_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    if await is_rate_limited("dexpaid_key_guess", ip):
        return PlainTextResponse("-- Too many invalid key attempts. Try again later.", status_code=429)
    if rate_limited(ip, "dexpaid_key_check", max_requests=DEXPAID_KEY_CHECK_RATE_LIMIT, window_seconds=DEXPAID_KEY_CHECK_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)

    key = request.query_params.get("key", "").strip()
    if not key:
        return PlainTextResponse("-- Missing paid key.")

    async with dexpaid_keys_lock:
        cleanup_expired_paid_keys()
        expiry = None
        for stored_key, exp in dexpaid_keys.items():
            if constant_time_eq(stored_key, key):
                expiry = exp
                break
        if not expiry:
            await record_failed_attempt("dexpaid_key_guess", ip)
            return PlainTextResponse("-- Invalid paid key.")
        if time.time() > expiry:
            dexpaid_keys.pop(key, None)
            save_dexpaid_keys_to_file(dexpaid_keys)
            return PlainTextResponse("-- Paid key expired.")

    await clear_attempts("dexpaid_key_guess", ip)
    return PlainTextResponse(await get_github_script("dexpaid", DEXPAID_FILE, DEFAULT_DEXPAID))


SLUG_LOADER_RATE_LIMIT = 30
SLUG_LOADER_RATE_WINDOW = 10.0


@app.get("/{slug}")
async def dynamic_loader(slug: str, request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "slug_loader_get", max_requests=SLUG_LOADER_RATE_LIMIT, window_seconds=SLUG_LOADER_RATE_WINDOW):
        return PlainTextResponse("-- Rate limited, try again shortly.", status_code=429)
    if slug.lower() in RESERVED_PATHS_LOWER:
        return PlainTextResponse("Private Script")
    if not is_executor(request):
        return PlainTextResponse("Private Script")

    async with scripts_lock:
        s = scripts.get(slug)
        if not s:
            for k, v in scripts.items():
                if k.lower() == slug.lower():
                    s = v
                    break
        if not s:
            return PlainTextResponse("Private Script")
        code = s.get("code", "")
        is_paid = s.get("is_paid", False)
        hwid_lock = s.get("hwid_lock", False)
        actual_slug = s.get("slug", slug)

    if not is_paid:
        return PlainTextResponse(code)

    if await is_rate_limited("slug_key_guess", ip):
        return PlainTextResponse("-- Too many invalid key attempts. Try again later.", status_code=429)

    key = request.query_params.get("key", "").strip()
    hwid = request.query_params.get("hwid", "").strip()
    if not key:
        return PlainTextResponse("-- Missing paid key.")
    if hwid_lock and not hwid:
        return PlainTextResponse("-- Missing HWID for locked script.")

    async with scripts_lock:
        s = scripts.get(actual_slug)
        if not s:
            return PlainTextResponse("-- Script not found.")
        keys = s.get("keys", {})
        matched_key = None
        for stored_key in keys.keys():
            if constant_time_eq(stored_key, key):
                matched_key = stored_key
                break
        if matched_key is None:
            await record_failed_attempt("slug_key_guess", ip)
            return PlainTextResponse("-- Invalid paid key.")

        info = keys[matched_key]
        expiry = info.get("expiry", 0)
        bound_hwid = info.get("hwid")
        now = time.time()
        if now > expiry:
            keys.pop(matched_key, None)
            s["keys"] = keys
            save_scripts_to_file()
            return PlainTextResponse("-- Paid key expired.")
        if hwid_lock:
            if bound_hwid is None:
                info["hwid"] = hwid
                keys[matched_key] = info
                s["keys"] = keys
                save_scripts_to_file()
            else:
                if not constant_time_eq(bound_hwid, hwid):
                    await record_failed_attempt("slug_key_guess", ip)
                    return PlainTextResponse("-- HWID mismatch for this key.")

    await clear_attempts("slug_key_guess", ip)
    return PlainTextResponse(code)


# =====================================================================
# RUN
# =====================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
