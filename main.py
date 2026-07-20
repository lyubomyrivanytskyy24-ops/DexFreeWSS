import asyncio
import os
import html
import time
import json
import hmac
import hashlib
import secrets
import re
import urllib.request
from typing import Set, Dict, Any, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
import uvicorn

app = FastAPI()

# =========================================================================
# SECURITY NOTES (read me)
# =========================================================================
# 1. Set these environment variables before running in production:
#      DEX_ADMIN_KEY   - admin panel key
#      DEX_SENDER_KEY  - key used by your log-sender (bot) and WS viewers
#      DEX_SECRET_KEY  - used to sign session cookies (keep this secret!)
#      DEX_BASE_URL    - public base URL, e.g. https://dexapi1.up.railway.app
#    If DEX_ADMIN_KEY / DEX_SENDER_KEY / DEX_SECRET_KEY are not set, secure
#    random values are generated at startup and printed ONCE to the console.
#    Save them somewhere safe - they will change on every restart if you
#    don't set the env vars.
#
# 2. Any client that previously called /logs, /blacklisted (POST) or
#    /admin/stats must now send header:  X-Api-Key: <your key>
#    (either DEX_ADMIN_KEY or DEX_SENDER_KEY both work for the sender-style
#    endpoints; only DEX_ADMIN_KEY works for /admin/*).
#
#    NOTE: POST /usernames is intentionally left WITHOUT a key requirement.
#    The in-game "finder" client reports its own username automatically and
#    has no way to hold a secret key, so this endpoint stays public (same
#    as its GET counterpart) and only ever writes a username string.
#
# 3. WebSocket viewers must connect to  /ws?key=<DEX_SENDER_KEY or DEX_ADMIN_KEY>
#    Anonymous viewer connections are no longer accepted.
#
# 4. Passwords are now hashed (PBKDF2-HMAC-SHA256, per-user salt). Existing
#    plaintext users.json from the old version will NOT be compatible -
#    users will need to re-register.
# =========================================================================

# -----------------------------
# SECRET / KEY MANAGEMENT
# -----------------------------

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


ADMIN_KEY = _get_or_create_secret("DEX_ADMIN_KEY", ".dex_admin_key")
SENDER_KEY = _get_or_create_secret("DEX_SENDER_KEY", ".dex_sender_key")
SECRET_KEY = _get_or_create_secret("DEX_SECRET_KEY", ".dex_secret_key")
BASE_URL = os.environ.get("DEX_BASE_URL", "https://dexapi1.up.railway.app").rstrip("/")


def constant_time_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def is_valid_admin_key(k: str) -> bool:
    return bool(k) and constant_time_eq(k, ADMIN_KEY)


def is_valid_sender_or_admin_key(k: str) -> bool:
    return bool(k) and (constant_time_eq(k, SENDER_KEY) or constant_time_eq(k, ADMIN_KEY))


# -----------------------------
# SIGNED SESSION TOKENS (stdlib only, no extra dependency)
# -----------------------------

SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days
ADMIN_SESSION_MAX_AGE = 2 * 3600  # 2 hours - shorter privilege window


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


# -----------------------------
# RATE LIMITING (simple in-memory, per-IP)
# -----------------------------

RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW = 15 * 60      # 15 minutes to accumulate failures
RATE_LIMIT_LOCKOUT = 15 * 60     # 15 minute lockout once tripped

_rate_state: Dict[str, Dict[str, Any]] = {}
_rate_lock = asyncio.Lock()


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
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
        # expire old failure windows
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


# -----------------------------
# INPUT VALIDATION HELPERS
# -----------------------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
SLUG_SOURCE_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,48}$")
RESERVED_USERNAMES = {"system", "admin", "administrator", "root", "sender", "owner", "sys"}

# --- Body size limits (raised to accommodate larger scripts, ~2MB) ---
MAX_GENERIC_BODY = 8 * 1024          # small text endpoints (logs/usernames/blacklist entries)
MAX_SCRIPT_BODY = 2 * 1024 * 1024    # script code, raised to 2MB
MAX_FORM_BODY = 2 * 1024 * 1024 + (100 * 1024)  # form posts (script code + other fields), 2MB + 100KB overhead buffer
MAX_PASSWORD_LEN = 128


# -----------------------------
# PASSWORD HASHING (PBKDF2-HMAC-SHA256, stdlib only)
# -----------------------------

PBKDF2_ITERATIONS = 200_000


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


# -----------------------------
# CORE CONFIG / FILES
# -----------------------------

USERNAME_FILE = "usernames.txt"
BLACKLIST_FILE = "blacklisted.txt"
LOGS_FILE = "logs.txt"
DEXPAID_KEYS_FILE = "dexpaid_keys.json"

USERS_FILE = "users.json"
SCRIPTS_FILE = "scripts.json"

lock = asyncio.Lock()
blacklist_lock = asyncio.Lock()
announcement_lock = asyncio.Lock()
logs_lock = asyncio.Lock()
dexpaid_keys_lock = asyncio.Lock()
users_lock = asyncio.Lock()
scripts_lock = asyncio.Lock()

DEXCHILLI_FILE = "dexchilli.lua"
DEXFREE_FILE = "dexfree.lua"
DEXSERVERHOP_FILE = "dexserverhop.lua"
DEXHUB_FILE = "dexhub.lua"
DEXPAID_FILE = "dexpaid.lua"

DEFAULT_DEXCHILLI = "-- DexChilli loader script not set yet."
DEFAULT_DEXFREE = "-- DexFree loader script not set yet."
DEFAULT_DEXSERVERHOP = "-- DexServerHop loader script not set yet."
DEFAULT_DEXHUB = "-- DexHub loader script not set yet."
DEFAULT_DEXPAID = "-- DexPaid loader script not set yet."

viewers: Set[WebSocket] = set()
sender_ws: Optional[WebSocket] = None

announcement_text: str = ""
announcement_timestamp: float = 0.0

dexpaid_keys: Dict[str, float] = {}
last_generated_paid_key: str = ""
last_generated_paid_loadstring: str = ""

users: Dict[str, Dict[str, Any]] = {}
scripts: Dict[str, Dict[str, Any]] = {}


# -----------------------------
# ATOMIC FILE HELPERS
# -----------------------------

def _atomic_write(path: str, content: str, mode: int = 0o600):
    tmp_path = f"{path}.tmp.{secrets.token_hex(4)}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, path)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


# -----------------------------
# USERNAME FILE HELPERS
# -----------------------------

def load_usernames_from_file() -> set:
    if not os.path.exists(USERNAME_FILE):
        return set()
    with open(USERNAME_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f.readlines() if line.strip())


def save_usernames_to_file(names: set):
    _atomic_write(USERNAME_FILE, "\n".join(sorted(names)) + ("\n" if names else ""), mode=0o644)


stored_usernames: set = load_usernames_from_file()

# -----------------------------
# BLACKLIST FILE HELPERS
# -----------------------------

def load_blacklist_from_file() -> set:
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f.readlines() if line.strip())


def save_blacklist_to_file(names: set):
    _atomic_write(BLACKLIST_FILE, "\n".join(sorted(names)) + ("\n" if names else ""), mode=0o644)


blacklisted_usernames: set = load_blacklist_from_file()

# -----------------------------
# GENERIC FILE HELPERS
# -----------------------------

def load_file(path: str, default: str) -> str:
    if not os.path.exists(path):
        _atomic_write(path, default, mode=0o644)
        return default
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_file(path: str, content: str):
    _atomic_write(path, content, mode=0o644)

# -----------------------------
# LOGS FILE HELPERS
# -----------------------------

MAX_STORED_LOGS = 5000  # cap memory/disk growth


def load_logs_from_file() -> list:
    if not os.path.exists(LOGS_FILE):
        return []
    with open(LOGS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f.readlines() if line.strip()][-MAX_STORED_LOGS:]


def append_log_to_file(entry: str):
    with open(LOGS_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


stored_logs: list = load_logs_from_file()

# -----------------------------
# DEXPAID KEYS HELPERS (GLOBAL)
# -----------------------------

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


def generate_paid_key(length: int = 24) -> str:
    # cryptographically secure token, not the `random` module
    return secrets.token_urlsafe(length)[:length]


def cleanup_expired_paid_keys():
    now = time.time()
    expired = [k for k, exp in dexpaid_keys.items() if exp <= now]
    for k in expired:
        dexpaid_keys.pop(k, None)


MAX_KEY_DURATION_HOURS = 24 * 365  # 1 year cap, prevents absurd/overflow values


def parse_duration_hours(raw: str) -> Optional[float]:
    try:
        hours = float(raw)
    except Exception:
        return None
    if hours <= 0 or hours > MAX_KEY_DURATION_HOURS:
        return None
    return hours


# -----------------------------
# USERS / SCRIPTS HELPERS
# -----------------------------

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
    "", "home", "admin", "logs", "usernames", "blacklisted", "announcements",
    "ws", "secure", "dexfree", "dexchilli", "dexserverhop", "dexhub", "dexpaid",
    "admin/stats", "admin/update", "favicon.ico", "robots.txt",
}
RESERVED_PATHS_LOWER = {p.lower() for p in RESERVED_PATHS}


def ensure_builtin_scripts():
    builtin = [
        ("DexFree", "dexfree", DEXFREE_FILE, DEFAULT_DEXFREE),
        ("DexChilli", "dexchilli", DEXCHILLI_FILE, DEFAULT_DEXCHILLI),
        ("DexServerHop", "dexserverhop", DEXSERVERHOP_FILE, DEFAULT_DEXSERVERHOP),
        ("DexHub", "dexhub", DEXHUB_FILE, DEFAULT_DEXHUB),
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
            }
    save_scripts_to_file()


ensure_builtin_scripts()

# -----------------------------
# /secure ENDPOINT (now key-protected - previously open to anyone)
# -----------------------------
current_wss: Optional[str] = None


@app.post("/secure")
async def set_wss(request: Request):
    global current_wss

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_admin_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw_body = await request.body()
    if len(raw_body) > MAX_GENERIC_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    current_wss = raw_body.decode("utf-8").strip()
    return {"wss": current_wss}


@app.get("/secure")
async def get_wss(request: Request):
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_admin_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"wss": current_wss}


# -----------------------------
# WEBSOCKET ENDPOINT (VIEWERS + SENDER) - now requires a key to connect
# -----------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global sender_ws

    # Require a valid key just to open the connection at all - previously
    # anyone could connect anonymously and read every broadcast log.
    key_param = websocket.query_params.get("key", "")
    if not is_valid_sender_or_admin_key(key_param):
        await websocket.close(code=4401)
        return

    await websocket.accept()

    role = "viewer"
    viewers.add(websocket)

    async with logs_lock:
        if stored_logs:
            last_entry = stored_logs[-1]
            try:
                await websocket.send_text(last_entry)
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

            # basic message-size guard
            if len(msg) > MAX_GENERIC_BODY:
                continue

            text = msg.strip()
            if not text:
                continue

            if role == "viewer" and constant_time_eq(text, SENDER_KEY):
                role = "sender"
                sender_ws = websocket
                viewers.discard(websocket)
                continue

            if role == "sender":
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
            else:
                continue

    except WebSocketDisconnect:
        pass
    finally:
        if role == "viewer":
            viewers.discard(websocket)
        elif role == "sender" and sender_ws is websocket:
            sender_ws = None

# -----------------------------
# LOGS ENDPOINT (HTTP -> WS) - now key-protected
# -----------------------------

@app.post("/logs")
async def post_logs(request: Request):
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_sender_or_admin_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    msg = raw.decode().strip()

    if not msg:
        return PlainTextResponse("EMPTY")

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
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_sender_or_admin_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with logs_lock:
        return PlainTextResponse("\n".join(stored_logs))

# -----------------------------
# USERNAME ENDPOINTS
# POST is intentionally public (no X-Api-Key) because the in-game finder
# script has no key to send - it just reports its own username. GET stays
# public too, since it always has.
# -----------------------------

@app.post("/usernames")
async def add_username(request: Request):
    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    username = raw.decode().strip()

    # Basic sanity check - Roblox usernames are short, so reject obvious junk
    # without being so strict that we reject legitimate names (e.g. those
    # with a period from newer Roblox display-name formats).
    if not username or len(username) > 32 or "\n" in username or "\r" in username:
        return PlainTextResponse("EMPTY")

    async with lock:
        if username not in stored_usernames:
            stored_usernames.add(username)
            save_usernames_to_file(stored_usernames)

    return PlainTextResponse("OK")


@app.get("/usernames")
async def get_usernames():
    async with lock:
        return PlainTextResponse("\n".join(sorted(stored_usernames)))

# -----------------------------
# BLACKLIST ENDPOINTS - writes now key-protected, reads remain public
# -----------------------------

@app.post("/blacklisted")
async def add_blacklisted(request: Request):
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_sender_or_admin_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    username = raw.decode().strip()

    if not username:
        return PlainTextResponse("EMPTY")

    async with blacklist_lock:
        if username not in blacklisted_usernames:
            blacklisted_usernames.add(username)
            save_blacklist_to_file(blacklisted_usernames)

    return PlainTextResponse("OK")


@app.post("/unblacklisted")
async def remove_blacklisted(request: Request):
    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_sender_or_admin_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    username = raw.decode().strip()

    if not username:
        return PlainTextResponse("EMPTY")

    async with blacklist_lock:
        if username in blacklisted_usernames:
            blacklisted_usernames.discard(username)
            save_blacklist_to_file(blacklisted_usernames)

    return PlainTextResponse("OK")


@app.get("/blacklisted")
async def get_blacklisted():
    async with blacklist_lock:
        return PlainTextResponse("\n".join(sorted(blacklisted_usernames)))

# -----------------------------
# ANNOUNCEMENTS ENDPOINT - POST now key-protected, GET stays public (polled by clients)
# -----------------------------

@app.post("/announcements")
async def post_announcement(request: Request):
    global announcement_text, announcement_timestamp

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_sender_or_admin_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    msg = raw.decode().strip()

    async with announcement_lock:
        announcement_text = msg
        announcement_timestamp = time.time()

    return PlainTextResponse("OK")


@app.get("/announcements")
async def get_announcement():
    async with announcement_lock:
        now = time.time()
        if announcement_text and (now - announcement_timestamp) <= 1.0:
            return PlainTextResponse(announcement_text)
        else:
            return PlainTextResponse("")

# -----------------------------
# HOME PAGE (ROOT)
# -----------------------------

@app.get("/")
async def index():
    html_page = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Dex API Backend</title>
        <style>
            :root {{ --bg: #050509; --accent1: #4fc3f7; --accent2: #7c4dff; --accent3: #ff5252; --accent4: #00e676; }}
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0;
                font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
                background:
                    radial-gradient(circle at top left, #202040 0, #050509 40%, #000000 100%),
                    linear-gradient(135deg, rgba(79,195,247,0.08), rgba(255,82,82,0.08));
                color: #e6e6e6;
            }}
            .wrap {{ max-width: 900px; margin: 60px auto; padding: 0 20px 40px; }}
            .card {{
                background: linear-gradient(135deg, rgba(15,15,22,0.95), rgba(10,10,18,0.95));
                border-radius: 20px; padding: 22px; border: 1px solid rgba(79,195,247,0.18);
                box-shadow: 0 24px 60px rgba(0,0,0,0.75); position: relative; overflow: hidden;
            }}
            h1 {{ margin: 0 0 10px 0; font-size: 26px; letter-spacing: 0.04em; }}
            p {{ margin: 6px 0; font-size: 14px; color: #b0b0c0; }}
            .pill {{
                display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 11px;
                background: rgba(79,195,247,0.18); border: 1px solid rgba(79,195,247,0.4);
                color: #e6f7ff; margin-right: 6px;
            }}
            .code-box {{
                margin-top: 14px; background: rgba(8,8,13,0.95); border-radius: 12px;
                border: 1px solid #262636; padding: 12px; font-family: monospace;
                font-size: 13px; color: #9eff9e; white-space: pre-wrap;
            }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="card">
                <h1>Dex API Backend Running</h1>
                <p><span class="pill">Private Scripts</span></p>
                <p>This backend powers dynamic loader endpoints.</p>
                <div class="code-box">
Public endpoints (browser view):<br>
/dexfree, /dexchilli, /dexserverhop, /dexhub, /dexpaid, /&lt;your-endpoint&gt; -> "Private Script"<br><br>
Executor usage:<br>
loadstring(game:HttpGet("{BASE_URL}/dexfree"))()<br><br>
Paid usage:<br>
loadstring(game:HttpGet("{BASE_URL}/dexpaid?key=YOUR_KEY"))()
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html_page)

# -----------------------------
# /HOME USER SCRIPT PANEL
# -----------------------------

HOME_BASE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Dex Home</title>
    <style>
        :root {{ --bg: #050509; --card-bg: #0f0f16; --accent1: #4fc3f7; --accent2: #7c4dff; --accent3: #ff5252; --accent4: #00e676; --border: #1c1c24; }}
        * {{ box-sizing: border-box; }}
        body {{
            background: radial-gradient(circle at top left, #202040 0, #050509 40%, #000000 100%),
                linear-gradient(135deg, rgba(79,195,247,0.08), rgba(255,82,82,0.08));
            color:#e6e6e6; font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif; margin:0;
        }}
        .wrap {{ max-width:1200px; margin:40px auto; padding:0 20px 40px; }}
        .card {{
            background: linear-gradient(135deg, rgba(15,15,22,0.95), rgba(10,10,18,0.95));
            border-radius:20px; padding:22px; margin-bottom:24px; border:1px solid rgba(79,195,247,0.18);
            box-shadow:0 24px 60px rgba(0,0,0,0.75); position:relative; overflow:hidden;
        }}
        h1,h2 {{ margin-top:0; }}
        h1 {{ font-size:26px; letter-spacing:0.04em; }}
        h2 {{ font-size:20px; }}
        input[type=password], input[type=text] {{
            width:100%; padding:12px; border-radius:12px; border:1px solid #262636;
            background:rgba(8,8,13,0.95); color:#e6e6e6; outline:none;
        }}
        textarea {{
            width:100%; min-height:180px; background:rgba(8,8,13,0.95); color:#9eff9e;
            border-radius:12px; border:1px solid #262636; padding:12px; font-family:monospace;
            font-size:14px; outline:none;
        }}
        button {{
            padding:10px 22px; border-radius:999px; border:none; cursor:pointer; font-weight:600;
            background:linear-gradient(135deg,var(--accent1),var(--accent2)); color:#050509; margin-top:10px;
        }}
        .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:20px; }}
        .label {{ font-size:13px; color:#b0b0c0; margin-bottom:6px; }}
        .pill {{
            display:inline-block; padding:4px 10px; border-radius:999px; font-size:11px;
            background:rgba(79,195,247,0.18); border:1px solid rgba(79,195,247,0.4); color:#e6f7ff; margin-right:6px;
        }}
        .pill.red {{ background:rgba(255,82,82,0.18); border-color:rgba(255,82,82,0.4); color:#ffe6e6; }}
        .pill.green {{ background:rgba(0,230,118,0.18); border-color:rgba(0,230,118,0.4); color:#e6fff3; }}
        .pill.purple {{ background:rgba(124,77,255,0.18); border-color:rgba(124,77,255,0.4); color:#f0e6ff; }}
        .small-text {{ font-size:12px; color:#8a8aa0; }}
        .logs-box {{
            background:rgba(8,8,13,0.95); border-radius:12px; border:1px solid #262636; padding:12px;
            font-family:monospace; font-size:12px; max-height:240px; overflow:auto; white-space:pre-wrap;
        }}
        .error {{ margin-top:10px; color:#ff5252; font-size:13px; }}
        .success {{ margin-top:10px; color:#00e676; font-size:13px; }}
    </style>
</head>
<body>
    <div class="wrap">
        {body}
    </div>
</body>
</html>
"""


def build_home_logged_out_body(message: str = "") -> str:
    msg_html = ""
    if message:
        msg_html = f'<div class="error">{html.escape(message)}</div>'
    body = f"""
    <div class="card">
        <h1>Dex Home - Login / Register</h1>
        <p class="label">
            <span class="pill">Public Script Loader</span>
            <span class="pill green">Create Your Own Endpoint</span>
            <span class="pill purple">Paid / Free & HWID Lock</span>
        </p>
        <p class="label">Login or create an account to manage your scripts and get loadstrings.</p>
        {msg_html}
        <div class="grid">
            <div>
                <h2>Login</h2>
                <form method="post" action="/home">
                    <input type="hidden" name="action" value="login">
                    <label class="label">Username</label>
                    <input type="text" name="username" placeholder="Your username" maxlength="32">
                    <label class="label">Password</label>
                    <input type="password" name="password" placeholder="Your password" maxlength="128">
                    <button type="submit">Login</button>
                </form>
            </div>
            <div>
                <h2>Register</h2>
                <form method="post" action="/home">
                    <input type="hidden" name="action" value="register">
                    <label class="label">Username (3-32 chars: letters, numbers, _ -)</label>
                    <input type="text" name="username" placeholder="Choose a username" maxlength="32">
                    <label class="label">Password (min 8 chars)</label>
                    <input type="password" name="password" placeholder="Choose a strong password" maxlength="128">
                    <button type="submit">Create Account</button>
                </form>
            </div>
        </div>
    </div>
    """
    return body


def build_home_logged_in_body(username: str, message: str = "", success: str = "") -> str:
    msg_html = ""
    if message:
        msg_html += f'<div class="error">{html.escape(message)}</div>'
    if success:
        msg_html += f'<div class="success">{html.escape(success)}</div>'

    user_scripts = [s for s in scripts.values() if s.get("owner") == username]
    cards_html = ""
    for s in user_scripts:
        slug = s["slug"]
        name = s["name"]
        code = html.escape(s.get("code", ""))
        is_paid = s.get("is_paid", False)
        hwid_lock = s.get("hwid_lock", False)
        last_key = s.get("last_key", "")
        last_loadstring = s.get("last_loadstring", "")
        paid_text = "Paid" if is_paid else "Free"
        hwid_text = "HWID Locked" if hwid_lock else "HWID Unlocked"
        endpoint = f"{BASE_URL}/{slug}"
        cards_html += f"""
        <div class="card">
            <h2>{html.escape(name)} ({html.escape(slug)})</h2>
            <p class="label">
                <span class="pill {'red' if is_paid else 'green'}">{paid_text}</span>
                <span class="pill {'purple' if hwid_lock else ''}">{hwid_text}</span>
            </p>
            <p class="small-text">Endpoint: <code>{html.escape(endpoint)}</code></p>
            <p class="small-text">Executor loadstring:</p>
            <div class="logs-box">
loadstring(game:HttpGet("{html.escape(endpoint)}"))()
            </div>
            <p class="small-text" style="margin-top:10px;">Last generated key:</p>
            <div class="logs-box">{html.escape(last_key or 'No key yet.')}</div>
            <p class="small-text" style="margin-top:10px;">Last generated loadstring (paid):</p>
            <div class="logs-box">{html.escape(last_loadstring or 'No paid loadstring yet.')}</div>
            <p class="small-text" style="margin-top:10px;">Edit script:</p>
            <form method="post" action="/home">
                <input type="hidden" name="action" value="update_script">
                <input type="hidden" name="slug" value="{html.escape(slug)}">
                <label class="label">Script Name</label>
                <input type="text" name="name" value="{html.escape(name)}" maxlength="48">
                <label class="label">Script Code (Lua)</label>
                <textarea name="code">{code}</textarea>
                <label class="label">Paid?</label>
                <input type="text" name="is_paid" placeholder="yes/no" value="{ 'yes' if is_paid else 'no' }">
                <label class="label">HWID Lock?</label>
                <input type="text" name="hwid_lock" placeholder="yes/no" value="{ 'yes' if hwid_lock else 'no' }">
                <button type="submit">Save Changes</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Generate paid key for this script:</p>
            <form method="post" action="/home">
                <input type="hidden" name="action" value="generate_key">
                <input type="hidden" name="slug" value="{html.escape(slug)}">
                <label class="label">Duration (hours, max {MAX_KEY_DURATION_HOURS})</label>
                <input type="text" name="hours" placeholder="e.g. 1, 5, 10">
                <button type="submit">Generate Key</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Delete this script:</p>
            <form method="post" action="/home">
                <input type="hidden" name="action" value="delete_script">
                <input type="hidden" name="slug" value="{html.escape(slug)}">
                <button type="submit" style="background:linear-gradient(135deg,#ff5252,#ff1744);">Delete Script</button>
            </form>
        </div>
        """

    if not cards_html:
        cards_html = """
        <div class="card">
            <h2>No scripts yet</h2>
            <p class="label">Create your first script below.</p>
        </div>
        """

    body = f"""
    <div class="card">
        <h1>Dex Home - Script Manager</h1>
        <p class="label">
            <span class="pill">Logged in as {html.escape(username)}</span>
            <span class="pill green">Create / Edit Scripts</span>
            <span class="pill purple">Paid / Free & HWID Lock</span>
        </p>
        <form method="post" action="/home" style="margin-top:10px;">
            <input type="hidden" name="action" value="logout">
            <button type="submit" style="background:linear-gradient(135deg,#ff5252,#ff1744);">Logout</button>
        </form>
        {msg_html}
    </div>

    <div class="card">
        <h2>Create New Script</h2>
        <p class="label">Name determines endpoint. Spaces become dashes. Example: "Dex 2" -> /Dex-2</p>
        <form method="post" action="/home">
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
        <p class="small-text" style="margin-top:10px;">After creation, you will see your script below with loadstring and controls.</p>
    </div>

    <div class="grid">
        {cards_html}
    </div>
    """
    return body


def get_logged_in_user(request: Request) -> Optional[str]:
    token = request.cookies.get("dex_session")
    username = verify_session_token(token, SESSION_MAX_AGE)
    if username and username in users:
        return username
    return None


def set_session_cookie(resp, username: str):
    token = create_session_token(username)
    resp.set_cookie(
        "dex_session", token,
        httponly=True, secure=True, samesite="strict",
        max_age=SESSION_MAX_AGE,
    )


@app.get("/home")
async def home_get(request: Request):
    username = get_logged_in_user(request)
    if not username:
        body = build_home_logged_out_body()
        return HTMLResponse(HOME_BASE_HTML.format(body=body))
    body = build_home_logged_in_body(username)
    return HTMLResponse(HOME_BASE_HTML.format(body=body))


@app.post("/home")
async def home_post(request: Request):
    raw = await request.body()
    if len(raw) > MAX_FORM_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)

    ip = _client_ip(request)
    data = parse_qs(raw.decode())
    action = data.get("action", [""])[0]
    username = data.get("username", [""])[0].strip()
    password = data.get("password", [""])[0]
    if len(password) > MAX_PASSWORD_LEN:
        password = password[:MAX_PASSWORD_LEN]

    if action == "register":
        if not username or not password:
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body("Username and password required.")))
        if not is_valid_username(username):
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body(
                    "Username must be 3-32 chars: letters, numbers, _ or - only, and not a reserved name.")))
        if len(password) < 8:
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body("Password must be at least 8 characters.")))
        async with users_lock:
            if username in users:
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_out_body("Username already in use.")))
            users[username] = {
                "username": username,
                "password_hash": hash_password(password),
                "created_at": time.time(),
            }
            save_users_to_file()
        resp = HTMLResponse(HOME_BASE_HTML.format(
            body=build_home_logged_in_body(username, success="Account created and logged in.")))
        set_session_cookie(resp, username)
        return resp

    if action == "login":
        if await is_rate_limited("home_login", ip):
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body("Too many failed attempts. Try again later.")), status_code=429)

        async with users_lock:
            user = users.get(username)
        if not user or "password_hash" not in user or not verify_password(password, user["password_hash"]):
            await record_failed_attempt("home_login", ip)
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body("Invalid username or password.")))

        await clear_attempts("home_login", ip)
        resp = HTMLResponse(HOME_BASE_HTML.format(
            body=build_home_logged_in_body(username, success="Logged in.")))
        set_session_cookie(resp, username)
        return resp

    current_user = get_logged_in_user(request)
    if not current_user:
        return HTMLResponse(HOME_BASE_HTML.format(
            body=build_home_logged_out_body("You must be logged in.")))

    if action == "logout":
        resp = HTMLResponse(HOME_BASE_HTML.format(body=build_home_logged_out_body("Logged out.")))
        resp.delete_cookie("dex_session")
        return resp

    if action == "create_script":
        name = data.get("name", [""])[0].strip()
        code = data.get("code", [""])[0]
        is_paid_str = data.get("is_paid", ["no"])[0].strip().lower()
        hwid_lock_str = data.get("hwid_lock", ["no"])[0].strip().lower()

        if not name or not code:
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_in_body(current_user, message="Name and code required.")))
        if len(code.encode("utf-8")) > MAX_SCRIPT_BODY:
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_in_body(current_user, message="Script code is too large.")))

        slug = make_slug(name)
        if not slug or slug.lower() in RESERVED_PATHS_LOWER:
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_in_body(current_user, message="Invalid or reserved script name.")))

        async with scripts_lock:
            if any(k.lower() == slug.lower() for k in scripts.keys()):
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(current_user, message="Endpoint already exists.")))
            scripts[slug] = {
                "name": name,
                "slug": slug,
                "code": code,
                "is_paid": is_paid_str == "yes",
                "hwid_lock": hwid_lock_str == "yes",
                "owner": current_user,
                "created_at": time.time(),
                "updated_at": time.time(),
                "keys": {},
                "last_key": "",
                "last_loadstring": "",
            }
            save_scripts_to_file()
        return HTMLResponse(HOME_BASE_HTML.format(
            body=build_home_logged_in_body(current_user, success="Script created.")))

    if action == "update_script":
        slug = data.get("slug", [""])[0].strip()
        name = data.get("name", [""])[0].strip()
        code = data.get("code", [""])[0]
        is_paid_str = data.get("is_paid", ["no"])[0].strip().lower()
        hwid_lock_str = data.get("hwid_lock", ["no"])[0].strip().lower()

        if len(code.encode("utf-8")) > MAX_SCRIPT_BODY:
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_in_body(current_user, message="Script code is too large.")))

        async with scripts_lock:
            s = scripts.get(slug)
            if not s or s.get("owner") != current_user:
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(current_user, message="Script not found or not owned by you.")))
            s["name"] = name or s["name"]
            s["code"] = code
            s["is_paid"] = is_paid_str == "yes"
            s["hwid_lock"] = hwid_lock_str == "yes"
            s["updated_at"] = time.time()
            save_scripts_to_file()
        return HTMLResponse(HOME_BASE_HTML.format(
            body=build_home_logged_in_body(current_user, success="Script updated.")))

    if action == "delete_script":
        slug = data.get("slug", [""])[0].strip()
        async with scripts_lock:
            s = scripts.get(slug)
            if not s or s.get("owner") != current_user:
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(current_user, message="Script not found or not owned by you.")))
            scripts.pop(slug, None)
            save_scripts_to_file()
        return HTMLResponse(HOME_BASE_HTML.format(
            body=build_home_logged_in_body(current_user, success="Script deleted.")))

    if action == "generate_key":
        slug = data.get("slug", [""])[0].strip()
        hours_str = data.get("hours", [""])[0].strip()
        hours = parse_duration_hours(hours_str)
        if hours is None:
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_in_body(current_user, message="Invalid duration.")))
        async with scripts_lock:
            s = scripts.get(slug)
            if not s or s.get("owner") != current_user:
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(current_user, message="Script not found or not owned by you.")))
            new_key = generate_paid_key(20)
            expiry = time.time() + hours * 3600.0
            keys = s.get("keys", {})
            keys[new_key] = {"expiry": expiry, "hwid": None}
            s["keys"] = keys
            s["last_key"] = new_key
            s["last_loadstring"] = f'loadstring(game:HttpGet("{BASE_URL}/{slug}?key={new_key}&hwid=YOUR_HWID"))()'
            save_scripts_to_file()
        return HTMLResponse(HOME_BASE_HTML.format(
            body=build_home_logged_in_body(current_user, success="Key generated.")))

    return HTMLResponse(HOME_BASE_HTML.format(
        body=build_home_logged_in_body(current_user, message="Unknown action.")))

# -----------------------------
# ADMIN PANEL
# -----------------------------

ADMIN_BASE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Dex Admin</title>
    <style>
        :root {{ --bg: #050509; --card-bg: #0f0f16; --accent1: #4fc3f7; --accent2: #7c4dff; --accent3: #ff5252; --accent4: #00e676; --border: #1c1c24; }}
        * {{ box-sizing: border-box; }}
        body {{
            background: radial-gradient(circle at top left, #202040 0, #050509 40%, #000000 100%),
                linear-gradient(135deg, rgba(79,195,247,0.08), rgba(255,82,82,0.08));
            color:#e6e6e6; font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif; margin:0;
        }}
        .wrap {{ max-width:1200px; margin:40px auto; padding:0 20px 40px; }}
        .card {{
            background: linear-gradient(135deg, rgba(15,15,22,0.95), rgba(10,10,18,0.95));
            border-radius:20px; padding:22px; margin-bottom:24px; border:1px solid rgba(79,195,247,0.18);
            box-shadow:0 24px 60px rgba(0,0,0,0.75); position:relative; overflow:hidden;
        }}
        h1,h2 {{ margin-top:0; }}
        h1 {{ font-size:26px; letter-spacing:0.04em; }}
        h2 {{ font-size:20px; }}
        input[type=password], input[type=text] {{
            width:100%; padding:12px; border-radius:12px; border:1px solid #262636;
            background:rgba(8,8,13,0.95); color:#e6e6e6; outline:none;
        }}
        textarea {{
            width:100%; min-height:180px; background:rgba(8,8,13,0.95); color:#9eff9e;
            border-radius:12px; border:1px solid #262636; padding:12px; font-family:monospace;
            font-size:14px; outline:none;
        }}
        button {{
            padding:10px 22px; border-radius:999px; border:none; cursor:pointer; font-weight:600;
            background:linear-gradient(135deg,var(--accent1),var(--accent2)); color:#050509; margin-top:10px;
        }}
        .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:20px; }}
        .label {{ font-size:13px; color:#b0b0c0; margin-bottom:6px; }}
        .pill {{
            display:inline-block; padding:4px 10px; border-radius:999px; font-size:11px;
            background:rgba(79,195,247,0.18); border:1px solid rgba(79,195,247,0.4); color:#e6f7ff; margin-right:6px;
        }}
        .pill.red {{ background:rgba(255,82,82,0.18); border-color:rgba(255,82,82,0.4); color:#ffe6e6; }}
        .pill.green {{ background:rgba(0,230,118,0.18); border-color:rgba(0,230,118,0.4); color:#e6fff3; }}
        .pill.purple {{ background:rgba(124,77,255,0.18); border-color:rgba(124,77,255,0.4); color:#f0e6ff; }}
        .stats-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-top:12px; }}
        .stat-box {{ background:rgba(8,8,13,0.95); border-radius:12px; border:1px solid #262636; padding:10px 12px; font-size:13px; }}
        .stat-label {{ color:#b0b0c0; font-size:12px; }}
        .stat-value {{ font-size:18px; font-weight:600; margin-top:4px; }}
        .small-text {{ font-size:12px; color:#8a8aa0; }}
        .error {{ margin-top:10px; color:#ff5252; font-size:13px; }}
        .logs-box {{
            background:rgba(8,8,13,0.95); border-radius:12px; border:1px solid #262636; padding:12px;
            font-family:monospace; font-size:12px; max-height:240px; overflow:auto; white-space:pre-wrap;
        }}
    </style>
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
                document.getElementById('dexpaid-last-loadstring-box').textContent = data.dexpaid_last_loadstring || 'No loadstring generated yet.';
                document.getElementById('admin-users-box').textContent = data.users_preview || 'No users.';
                document.getElementById('admin-scripts-box').textContent = data.scripts_preview || 'No scripts.';
            }} catch (e) {{
                console.error(e);
            }}
        }}
        document.addEventListener('DOMContentLoaded', () => {{
            refreshStats();
            setInterval(refreshStats, 2000);
        }});
    </script>
</head>
<body>
    <div class="wrap">
        {body}
    </div>
</body>
</html>
"""


def admin_login_form(error: str = "") -> str:
    err_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return f"""
    <div class="card">
        <h1>Dex Admin Login</h1>
        <p class="label">
            <span class="pill">Private System</span>
            <span class="pill red">Key Protected</span>
            <span class="pill green">Loader Scripts</span>
            <span class="pill purple">Control Center</span>
        </p>
        <p class="label">Enter the admin key to manage loader scripts, blacklist, announcements, paid keys, users, and system stats.</p>
        <form method="post">
            <label class="label">Admin Key</label><br>
            <input type="password" name="key" placeholder="Enter admin key">
            <button type="submit">Enter</button>
        </form>
        {err_html}
    </div>
    """


@app.get("/admin")
async def admin_get(request: Request):
    # if already have a valid admin session, skip straight to dashboard
    token = request.cookies.get("dex_admin_session")
    if verify_session_token(token, ADMIN_SESSION_MAX_AGE) == "admin":
        return HTMLResponse(ADMIN_BASE_HTML.format(body=build_admin_dashboard_body()))
    return HTMLResponse(ADMIN_BASE_HTML.format(body=admin_login_form()))


def build_admin_dashboard_body() -> str:
    dexchilli_code = html.escape(load_file(DEXCHILLI_FILE, DEFAULT_DEXCHILLI))
    dexfree_code = html.escape(load_file(DEXFREE_FILE, DEFAULT_DEXFREE))
    dexserverhop_code = html.escape(load_file(DEXSERVERHOP_FILE, DEFAULT_DEXSERVERHOP))
    dexhub_code = html.escape(load_file(DEXHUB_FILE, DEFAULT_DEXHUB))
    dexpaid_code = html.escape(load_file(DEXPAID_FILE, DEFAULT_DEXPAID))

    keys_preview_lines = []
    now = time.time()
    for k, exp in dexpaid_keys.items():
        remaining = max(0, int(exp - now))
        keys_preview_lines.append(f"{k}  |  expires in {remaining} seconds")
    keys_preview_text = "\n".join(keys_preview_lines) if keys_preview_lines else "No paid keys."

    # SECURITY FIX: no longer displays plaintext passwords (they no longer exist).
    users_lines = []
    for u in users.values():
        users_lines.append(f"{u['username']} | created: {int(u.get('created_at', 0))}")
    users_preview = "\n".join(users_lines) if users_lines else "No users."

    scripts_lines = []
    for s in scripts.values():
        scripts_lines.append(
            f"{s['name']} ({s['slug']}) | owner: {s['owner']} | paid: {s['is_paid']} | hwid_lock: {s['hwid_lock']}"
        )
    scripts_preview = "\n".join(scripts_lines) if scripts_lines else "No scripts."

    body = f"""
    <div class="card">
        <h1>Dex Control Center</h1>
        <p class="label">
            <span class="pill">/dexchilli</span>
            <span class="pill">/dexfree</span>
            <span class="pill">/dexserverhop</span>
            <span class="pill purple">/dexhub</span>
            <span class="pill green">/dexpaid</span>
        </p>
        <p class="label">Manage loader scripts, blacklist, announcements, paid keys, users, and live system data.</p>
        <div class="stats-grid">
            <div class="stat-box"><div class="stat-label">Registered Usernames</div><div class="stat-value" id="stat-usernames">0</div></div>
            <div class="stat-box"><div class="stat-label">Blacklisted Users</div><div class="stat-value" id="stat-blacklisted">0</div></div>
            <div class="stat-box"><div class="stat-label">Total Logs (Discord / Sender)</div><div class="stat-value" id="stat-logs">0</div></div>
            <div class="stat-box"><div class="stat-label">Viewers Connected (WS)</div><div class="stat-value" id="stat-viewers">0</div></div>
            <div class="stat-box"><div class="stat-label">Sender Connected</div><div class="stat-value" id="stat-sender">No</div></div>
        </div>
        <p class="small-text" style="margin-top:10px;">Last log entry:</p>
        <div class="logs-box" id="last-log-box">No logs yet.</div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>DexChilli Loader Script</h2>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="dexchilli">
                <label class="label">Script (plain Lua)</label>
                <textarea name="code">{dexchilli_code}</textarea>
                <button type="submit">Save DexChilli Script</button>
            </form>
        </div>
        <div class="card">
            <h2>DexFree Loader Script</h2>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="dexfree">
                <label class="label">Script (plain Lua)</label>
                <textarea name="code">{dexfree_code}</textarea>
                <button type="submit">Save DexFree Script</button>
            </form>
        </div>
        <div class="card">
            <h2>DexServerHop Loader Script</h2>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="dexserverhop">
                <label class="label">Script (plain Lua)</label>
                <textarea name="code">{dexserverhop_code}</textarea>
                <button type="submit">Save DexServerHop Script</button>
            </form>
        </div>
        <div class="card">
            <h2>DexHub Loader Script</h2>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="dexhub">
                <label class="label">Script (plain Lua)</label>
                <textarea name="code">{dexhub_code}</textarea>
                <button type="submit">Save DexHub Script</button>
            </form>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>DexPaid Loader Script</h2>
            <p class="label">Paid script, loaded only with valid key.</p>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="dexpaid_script">
                <label class="label">Script (plain Lua, obfuscated allowed)</label>
                <textarea name="code">{dexpaid_code}</textarea>
                <button type="submit">Save DexPaid Script</button>
            </form>
        </div>
        <div class="card">
            <h2>DexPaid Key Generator</h2>
            <p class="label">Generate a key with a time limit (in hours, max {MAX_KEY_DURATION_HOURS}).</p>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="dexpaid_key">
                <label class="label">Duration (hours)</label>
                <input type="text" name="code" placeholder="e.g. 1, 5, 10">
                <button type="submit">Generate Paid Key</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Last generated key:</p>
            <div class="logs-box" id="dexpaid-last-key-box">No key generated yet.</div>
            <p class="small-text" style="margin-top:10px;">Last generated loadstring:</p>
            <div class="logs-box" id="dexpaid-last-loadstring-box">No loadstring generated yet.</div>
            <p class="small-text" style="margin-top:10px;">All active paid keys:</p>
            <div class="logs-box" id="dexpaid-keys-box">{keys_preview_text}</div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Announcements</h2>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="announcement">
                <label class="label">Announcement Text</label>
                <textarea name="code"></textarea>
                <button type="submit">Send Announcement</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Current announcement preview:</p>
            <div class="logs-box" id="announcement-preview-box">No active announcement.</div>
        </div>
        <div class="card">
            <h2>Blacklist Management</h2>
            <p class="label">Add or remove usernames from blacklist. One username per line.</p>
            <form method="post" action="/admin/update">
                <input type="hidden" name="target" value="blacklist_add">
                <label class="label">Add to Blacklist</label>
                <textarea name="code"></textarea>
                <button type="submit">Blacklist Users</button>
            </form>
            <form method="post" action="/admin/update" style="margin-top:16px;">
                <input type="hidden" name="target" value="blacklist_remove">
                <label class="label">Remove from Blacklist</label>
                <textarea name="code"></textarea>
                <button type="submit">Unblacklist Users</button>
            </form>
            <p class="small-text" style="margin-top:10px;">Current blacklisted users:</p>
            <div class="logs-box" id="blacklist-preview-box"></div>
        </div>
        <div class="card">
            <h2>Recent Logs (Discord / Sender)</h2>
            <div class="logs-box" id="recent-logs-box">No logs.</div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Users Overview</h2>
            <p class="label">Registered usernames (password hashes are never displayed).</p>
            <div class="logs-box" id="admin-users-box">{users_preview}</div>
        </div>
        <div class="card">
            <h2>Scripts Overview</h2>
            <div class="logs-box" id="admin-scripts-box">{scripts_preview}</div>
        </div>
    </div>
    """
    return body


@app.post("/admin")
async def admin_post(request: Request):
    ip = _client_ip(request)
    if await is_rate_limited("admin_login", ip):
        return HTMLResponse(ADMIN_BASE_HTML.format(
            body=admin_login_form("Too many failed attempts. Try again later.")), status_code=429)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)
    data = parse_qs(raw.decode())
    key = data.get("key", [""])[0]

    if not is_valid_admin_key(key):
        await record_failed_attempt("admin_login", ip)
        return HTMLResponse(ADMIN_BASE_HTML.format(body=admin_login_form("Invalid key.")))

    await clear_attempts("admin_login", ip)
    resp = HTMLResponse(ADMIN_BASE_HTML.format(body=build_admin_dashboard_body()))
    admin_token = create_session_token("admin")
    resp.set_cookie(
        "dex_admin_session", admin_token,
        httponly=True, secure=True, samesite="strict",
        max_age=ADMIN_SESSION_MAX_AGE,
    )
    return resp


def require_admin_session(request: Request) -> bool:
    token = request.cookies.get("dex_admin_session")
    return verify_session_token(token, ADMIN_SESSION_MAX_AGE) == "admin"


@app.post("/admin/update")
async def admin_update(request: Request):
    global announcement_text, announcement_timestamp
    global last_generated_paid_key, last_generated_paid_loadstring

    if not require_admin_session(request):
        return PlainTextResponse("Unauthorized - please log in at /admin again.", status_code=401)

    raw = await request.body()
    if len(raw) > MAX_FORM_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)
    data = parse_qs(raw.decode())

    target = data.get("target", [""])[0]
    code = data.get("code", [""])[0]

    if target in ("dexchilli", "dexfree", "dexserverhop", "dexhub", "dexpaid_script"):
        if len(code.encode("utf-8")) > MAX_SCRIPT_BODY:
            return PlainTextResponse("Script too large.", status_code=413)

    if target == "dexchilli":
        save_file(DEXCHILLI_FILE, code)
    elif target == "dexfree":
        save_file(DEXFREE_FILE, code)
    elif target == "dexserverhop":
        save_file(DEXSERVERHOP_FILE, code)
    elif target == "dexhub":
        save_file(DEXHUB_FILE, code)
    elif target == "dexpaid_script":
        save_file(DEXPAID_FILE, code)
    elif target == "dexpaid_key":
        hours = parse_duration_hours(code.strip())
        if hours is not None:
            async with dexpaid_keys_lock:
                cleanup_expired_paid_keys()
                new_key = generate_paid_key(20)
                expiry = time.time() + hours * 3600.0
                dexpaid_keys[new_key] = expiry
                save_dexpaid_keys_to_file(dexpaid_keys)
                last_generated_paid_key = new_key
                last_generated_paid_loadstring = (
                    f'loadstring(game:HttpGet("{BASE_URL}/dexpaid?key={new_key}"))()'
                )
    elif target == "announcement":
        msg = code.strip()
        async with announcement_lock:
            announcement_text = msg
            announcement_timestamp = time.time()
    elif target == "blacklist_add":
        lines = [u.strip() for u in code.splitlines() if u.strip()][:200]
        async with blacklist_lock:
            for username in lines:
                if username not in blacklisted_usernames:
                    blacklisted_usernames.add(username)
            save_blacklist_to_file(blacklisted_usernames)
    elif target == "blacklist_remove":
        lines = [u.strip() for u in code.splitlines() if u.strip()][:200]
        async with blacklist_lock:
            for username in lines:
                blacklisted_usernames.discard(username)
            save_blacklist_to_file(blacklisted_usernames)
    else:
        return PlainTextResponse("Invalid target", status_code=400)

    return HTMLResponse(ADMIN_BASE_HTML.format(body=build_admin_dashboard_body()))

# -----------------------------
# ADMIN LIVE STATS API - now requires an admin session
# -----------------------------

async def fetch_remote_info() -> dict:
    url = f"{BASE_URL}/info"

    def _do_request():
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    return await asyncio.to_thread(_do_request)


@app.get("/admin/stats")
async def admin_stats(request: Request):
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
        preview_lines = []
        for k, exp in dexpaid_keys.items():
            remaining = max(0, int(exp - now))
            preview_lines.append(f"{k}  |  expires in {remaining} seconds")
        dexpaid_keys_preview = "\n".join(preview_lines) if preview_lines else "No paid keys."

    async with users_lock:
        users_lines = []
        for u in users.values():
            users_lines.append(f"{u['username']} | created: {int(u.get('created_at', 0))}")
        users_preview = "\n".join(users_lines) if users_lines else "No users."

    async with scripts_lock:
        scripts_lines = []
        for s in scripts.values():
            scripts_lines.append(
                f"{s['name']} ({s['slug']}) | owner: {s['owner']} | paid: {s['is_paid']} | hwid_lock: {s['hwid_lock']}"
            )
        scripts_preview = "\n".join(scripts_lines) if scripts_lines else "No scripts."

    remote = await fetch_remote_info()

    sender_connected = remote.get("sender_connected", sender_ws is not None)
    viewers_count = remote.get("viewer_count", len(viewers))
    logs_count = remote.get("stored_logs_count", local_logs_count)
    messages_count = remote.get("total_messages_broadcasted", logs_count)
    usernames_count = remote.get("stored_usernames_count", local_usernames_count)

    return JSONResponse(
        {
            "usernames_count": usernames_count,
            "blacklisted_count": blacklisted_count,
            "logs_count": logs_count,
            "messages_count": messages_count,
            "viewers_count": viewers_count,
            "sender_connected": sender_connected,
            "last_log": last_log,
            "recent_logs": recent_logs,
            "announcement": announcement,
            "blacklisted_list": blacklisted_list,
            "dexpaid_keys_preview": dexpaid_keys_preview,
            "dexpaid_last_key": last_generated_paid_key,
            "dexpaid_last_loadstring": last_generated_paid_loadstring,
            "users_preview": users_preview,
            "scripts_preview": scripts_preview,
        },
        headers={"Cache-Control": "no-store"},
    )

# -----------------------------
# SIMPLE EXECUTOR CHECK
# -----------------------------

def is_executor(request: Request) -> bool:
    ua = request.headers.get("User-Agent", "")
    ua_lower = ua.lower()
    return ("roblox" in ua_lower) or ("wininet" in ua_lower)

# -----------------------------
# FIXED LOADER ENDPOINTS (GLOBAL FILES)
# -----------------------------

@app.get("/dexfree")
async def dexfree(request: Request):
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(load_file(DEXFREE_FILE, DEFAULT_DEXFREE))


@app.get("/dexchilli")
async def dexchilli(request: Request):
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(load_file(DEXCHILLI_FILE, DEFAULT_DEXCHILLI))


@app.get("/dexserverhop")
async def dexserverhop(request: Request):
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(load_file(DEXSERVERHOP_FILE, DEFAULT_DEXSERVERHOP))


@app.get("/dexhub")
async def dexhub(request: Request):
    if not is_executor(request):
        return PlainTextResponse("Private Script")
    return PlainTextResponse(load_file(DEXHUB_FILE, DEFAULT_DEXHUB))


@app.get("/dexpaid")
async def dexpaid(request: Request):
    if not is_executor(request):
        return PlainTextResponse("Private Script")

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
            return PlainTextResponse("-- Invalid paid key.")
        if time.time() > expiry:
            dexpaid_keys.pop(key, None)
            save_dexpaid_keys_to_file(dexpaid_keys)
            return PlainTextResponse("-- Paid key expired.")

    return PlainTextResponse(load_file(DEXPAID_FILE, DEFAULT_DEXPAID))

# -----------------------------
# DYNAMIC LOADER ENDPOINTS
# -----------------------------

@app.get("/{slug}")
async def dynamic_loader(slug: str, request: Request):
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
                    return PlainTextResponse("-- HWID mismatch for this key.")

    return PlainTextResponse(code)

# -----------------------------
# RUN
# -----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
