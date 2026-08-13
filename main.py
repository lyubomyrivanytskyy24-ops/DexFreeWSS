import asyncio
import os
import sys
import html
import time
import json
import hmac
import hashlib
import random
import string
import secrets
import re
import unicodedata
import urllib.request
from collections import defaultdict, deque
from typing import Set, Dict, Any, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
import uvicorn

app = FastAPI()

# ═════════════════════════════════════════════════════════════════════════════
# RANDOMNESS
# ═════════════════════════════════════════════════════════════════════════════

_RNG = random.SystemRandom()

_IDENTIFIER_CHARS = string.ascii_letters

_U8 = 0x100
_U16 = 0x10000

_PRNG_MULT = 25173
_PRNG_ADD = 13849

_MIX_A = 197
_MIX_B = 113
_MIX_C = 71
_MIX_D = 43


# ═════════════════════════════════════════════════════════════════════════════
# IDENTIFIERS
# ═════════════════════════════════════════════════════════════════════════════

def _rand_name(length=None):
    if length is None:
        length = _RNG.randint(9, 17)

    length = max(2, int(length))

    confusing = (
        string.ascii_letters
        + "IlIOoO"
        + "lI1"
        + "oO0"
    )

    result = _RNG.choice(string.ascii_letters)

    for _ in range(length - 1):
        result += _RNG.choice(confusing)

    return result


def _unique_name(used):
    while True:
        name = _rand_name()

        if name not in used:
            used.add(name)
            return name


# ═════════════════════════════════════════════════════════════════════════════
# INTEGER HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _u8(value):
    return int(value) & 0xFF


def _u16(value):
    return int(value) & 0xFFFF


def _prng_step(state, extra=0):
    return (
        int(state) * _PRNG_MULT
        + _PRNG_ADD
        + int(extra)
    ) % _U16


# ═════════════════════════════════════════════════════════════════════════════
# ROTATION
# ═════════════════════════════════════════════════════════════════════════════

def _rotl8(value, amount):
    value = _u8(value)
    amount = int(amount) & 7

    if amount == 0:
        return value

    return (
        ((value << amount) & 0xFF)
        | (value >> (8 - amount))
    )


def _rotr8(value, amount):
    value = _u8(value)
    amount = int(amount) & 7

    if amount == 0:
        return value

    return (
        (value >> amount)
        | ((value << (8 - amount)) & 0xFF)
    )


# ═════════════════════════════════════════════════════════════════════════════
# PERMUTATION
# ═════════════════════════════════════════════════════════════════════════════

def _make_permutation(length, seed):
    length = int(length)
    state = _u16(seed)

    permutation = list(range(length))

    for position in range(length - 1, 0, -1):
        state = _prng_step(
            state,
            position * 97
        )

        swap_index = state % (position + 1)

        permutation[position], permutation[swap_index] = (
            permutation[swap_index],
            permutation[position],
        )

    return permutation


# ═════════════════════════════════════════════════════════════════════════════
# ONE CIPHER ROUND
# ═════════════════════════════════════════════════════════════════════════════

def _encrypt_round(
    source,
    seed1,
    seed2,
    seed3,
    seed4,
    block_size
):
    encrypted = bytearray()

    for block_start in range(
        0,
        len(source),
        block_size
    ):
        block = source[
            block_start:
            block_start + block_size
        ]

        block_length = len(block)

        permutation_seed = (
            seed1
            + seed2
            + seed3 * (block_start + 1)
            + seed4 * block_length
            + block_start * _MIX_A
        ) % _U16

        permutation = _make_permutation(
            block_length,
            permutation_seed
        )

        state = (
            seed1
            + seed3
            + ((block_start + 1) * 17)
            + (block_length * _MIX_B)
        ) % _U16

        previous_cipher = (
            seed4
            + block_start
            + block_length
        ) & 0xFF

        transformed = [0] * block_length

        for destination in range(block_length):
            original_index = permutation[destination]

            absolute_index = (
                block_start
                + original_index
                + 1
            )

            state = _prng_step(
                state,
                original_index
                + destination
                + block_length
            )

            value = block[original_index]

            rotation = (
                seed2
                + original_index
                + destination
                + state
                + block_length
            ) & 7

            value = _rotl8(
                value,
                rotation
            )

            add_value = (
                (state >> 8)
                ^ (seed4 & 0xFF)
                ^ (absolute_index * 13)
                ^ (destination * _MIX_C)
            ) & 0xFF

            value = (
                value + add_value
            ) & 0xFF

            xor_value = (
                (seed3 & 0xFF)
                + destination * 29
                + (state & 0xFF)
                + absolute_index * 7
                + block_length * _MIX_D
            ) & 0xFF

            value ^= xor_value

            feedback = (
                previous_cipher
                ^ ((state >> 8) & 0xFF)
                ^ (seed1 & 0xFF)
                ^ ((absolute_index * 11) & 0xFF)
            ) & 0xFF

            value ^= feedback

            final_mix = (
                (seed4 >> 8)
                + destination * 17
                + original_index * 31
                + state
            ) & 0xFF

            value ^= final_mix
            value &= 0xFF

            previous_cipher = value
            transformed[destination] = value

        encrypted.extend(transformed)

    return bytes(encrypted)


def _decrypt_round(
    encrypted,
    seed1,
    seed2,
    seed3,
    seed4,
    block_size
):
    decrypted = bytearray()

    for block_start in range(
        0,
        len(encrypted),
        block_size
    ):
        block = encrypted[
            block_start:
            block_start + block_size
        ]

        block_length = len(block)

        permutation_seed = (
            seed1
            + seed2
            + seed3 * (block_start + 1)
            + seed4 * block_length
            + block_start * _MIX_A
        ) % _U16

        permutation = _make_permutation(
            block_length,
            permutation_seed
        )

        state = (
            seed1
            + seed3
            + ((block_start + 1) * 17)
            + (block_length * _MIX_B)
        ) % _U16

        previous_cipher = (
            seed4
            + block_start
            + block_length
        ) & 0xFF

        output = [0] * block_length

        for destination in range(block_length):
            original_index = permutation[destination]

            absolute_index = (
                block_start
                + original_index
                + 1
            )

            state = _prng_step(
                state,
                original_index
                + destination
                + block_length
            )

            value = block[destination]

            current_cipher = value

            final_mix = (
                (seed4 >> 8)
                + destination * 17
                + original_index * 31
                + state
            ) & 0xFF

            value ^= final_mix

            feedback = (
                previous_cipher
                ^ ((state >> 8) & 0xFF)
                ^ (seed1 & 0xFF)
                ^ ((absolute_index * 11) & 0xFF)
            ) & 0xFF

            value ^= feedback

            xor_value = (
                (seed3 & 0xFF)
                + destination * 29
                + (state & 0xFF)
                + absolute_index * 7
                + block_length * _MIX_D
            ) & 0xFF

            value ^= xor_value

            add_value = (
                (state >> 8)
                ^ (seed4 & 0xFF)
                ^ (absolute_index * 13)
                ^ (destination * _MIX_C)
            ) & 0xFF

            value = (
                value - add_value
            ) & 0xFF

            rotation = (
                seed2
                + original_index
                + destination
                + state
                + block_length
            ) & 7

            value = _rotr8(
                value,
                rotation
            )

            output[original_index] = value

            previous_cipher = current_cipher

        decrypted.extend(output)

    return bytes(decrypted)


# ═════════════════════════════════════════════════════════════════════════════
# INTEGRITY
# ═════════════════════════════════════════════════════════════════════════════

def _integrity_digest(data):
    h1 = 0x1357
    h2 = 0x2468
    h3 = 0x369C
    h4 = 0x4ACE

    for index, value in enumerate(data, 1):
        value = int(value)

        h1 = (
            h1 * 257
            + value
            + index
        ) % _U16

        h2 = (
            h2 * 263
            + value * 3
            + index * 7
        ) % _U16

        h3 = (
            h3 * 269
            + value * 5
            + index * 11
        ) % _U16

        h4 = (
            h4 * 271
            + value * 7
            + index * 17
        ) % _U16

    return h1, h2, h3, h4


def _cipher_digest(data):
    h1 = 0x5A31
    h2 = 0x71C9
    h3 = 0x42D7

    for index, value in enumerate(data, 1):
        value = int(value)

        h1 = (
            h1 * 251
            + value
            + index * 3
        ) % _U16

        h2 = (
            h2 * 277
            + value * 7
            + index * 13
        ) % _U16

        h3 = (
            h3 * 283
            + value * 11
            + index * 19
        ) % _U16

    return h1, h2, h3


# ═════════════════════════════════════════════════════════════════════════════
# OPAQUE NUMBERS
# ═════════════════════════════════════════════════════════════════════════════

def _num_expr(value):
    value = int(value)

    if value == 0:
        a = _RNG.randint(100, 9000)
        return f"({a}-{a})"

    if value < 0:
        return f"-({_num_expr(-value)})"

    style = _RNG.randint(0, 8)

    if style == 0:
        a = _RNG.randint(1, 5000)
        return f"({a}+({value-a}))"

    if style == 1:
        a = _RNG.randint(value + 1, value + 5000)
        return f"({a}-{a-value})"

    if style == 2:
        a = _RNG.randint(1, 1000)
        return f"(({value+a})-{a})"

    if style == 3:
        a = _RNG.randint(1, 200)
        b = _RNG.randint(1, 200)
        return f"(({value+a})+{b}-{a}-{b})"

    if style == 4:
        a = _RNG.randint(1, 100)
        return f"(({value}*1)+{a}-{a})"

    if style == 5:
        a = _RNG.randint(2, 31)
        return f"(({value}*{a})/{a})"

    if style == 6:
        a = _RNG.randint(1, 300)
        return f"(({value}~={value+a}) and {value} or {value})"

    if style == 7:
        a = _RNG.randint(1, 1000)
        return f"(({value}+{a})-{a})"

    a = _RNG.randint(2, 19)
    return f"(({value}*{a})/{a})"


# ═════════════════════════════════════════════════════════════════════════════
# LUA ESCAPING
# ═════════════════════════════════════════════════════════════════════════════

def _lua_escape_bytes(data):
    return "".join(
        f"\\{int(value):03d}"
        for value in data
    )


# ═════════════════════════════════════════════════════════════════════════════
# RANDOM TOKEN ALPHABET
# ═════════════════════════════════════════════════════════════════════════════

def _make_token_alphabet():
    candidates = list(
        "!#$%&()*+,-./:;<=>?@[]^_{|}~"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
    )

    token_zero = _RNG.choice(candidates)

    remaining = [
        character
        for character in candidates
        if character != token_zero
    ]

    token_one = _RNG.choice(remaining)

    return token_zero, token_one


def _encode_binary_tokens(
    data,
    token_zero,
    token_one
):
    output = []

    for value in data:
        value = _u8(value)

        for shift in range(7, -1, -1):
            if value & (1 << shift):
                output.append(token_one)
            else:
                output.append(token_zero)

    return "".join(output)


def _decode_binary_tokens(
    tokens,
    token_zero,
    token_one
):
    if not isinstance(tokens, str):
        raise TypeError(
            "Token payload must be a string."
        )

    if len(tokens) % 8 != 0:
        raise ValueError(
            "Token payload is not byte aligned."
        )

    output = bytearray()

    for cursor in range(
        0,
        len(tokens),
        8
    ):
        value = 0

        for character in tokens[
            cursor:
            cursor + 8
        ]:
            value <<= 1

            if character == token_one:
                value |= 1

            elif character != token_zero:
                raise ValueError(
                    "Invalid token character."
                )

        output.append(value)

    return bytes(output)


# ═════════════════════════════════════════════════════════════════════════════
# RANDOM FRAGMENTATION
# ═════════════════════════════════════════════════════════════════════════════

def _fragment_tokens(encoded, min_chunks=13, max_chunks=113):
    fragments = []
    cursor = 0

    while cursor < len(encoded):
        amount = (
            _RNG.randint(min_chunks, max_chunks)
            * 8
        )

        fragments.append(
            encoded[
                cursor:
                cursor + amount
            ]
        )

        cursor += amount

    indexed = list(
        enumerate(fragments)
    )

    _RNG.shuffle(indexed)

    return indexed


# ═════════════════════════════════════════════════════════════════════════════
# RUNTIME NOISE
# ═════════════════════════════════════════════════════════════════════════════

def _make_noise_expression():
    a = _RNG.randint(1000, 9000)
    b = _RNG.randint(1000, 9000)

    return (
        f"(({a}*{b})-"
        f"({a}*{b}))"
    )


# ═════════════════════════════════════════════════════════════════════════════
# RAW PUBLISHING IS OVERRIDDEN BELOW TO USE THIS SERVICE'S OWN /raw STORE.
# ═════════════════════════════════════════════════════════════════════════════



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


# Single shared key for the whole API. Defaults to the literal "DEXONTOP" so
# the app works out of the box - override with DEX_API_KEY in any deployment
# you want to actually secure (this default is visible to anyone who can
# read this source file).
API_KEY = (os.environ.get("DEX_API_KEY", "").strip() or "")
SECRET_KEY = _get_or_create_secret("DEX_SECRET_KEY", ".dex_secret_key")
BASE_URL = os.environ.get("DEX_BASE_URL", "https://dexapi1.up.railway.app").rstrip("/")


def constant_time_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def is_valid_key(k: str) -> bool:
    return bool(k) and constant_time_eq(k, API_KEY)


# -----------------------------
# GITHUB-MANAGED SCRIPT SOURCE
# -----------------------------
# See SECURITY NOTES item 5 above. These scripts can ONLY be changed by
# editing the file in the configured GitHub repo - there is no code path
# left (admin panel or API) that writes to them from within this app.

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

# name -> {"content": str, "fetched_at": float, "source": "github"|"local_fallback"|"default"}
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
    """Blocking fetch, run via asyncio.to_thread. Returns None on any failure."""
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
    """Return this script's current content: fresh cache -> GitHub fetch (also
    refreshes the on-disk fallback copy) -> in-memory cache (stale) ->
    on-disk fallback file -> hardcoded default. This is a read path only;
    nothing in this app writes new content for these scripts."""
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

    # GitHub fetch failed (or not configured) - fall back to whatever we have.
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
# LOCKOUT-STYLE RATE LIMITING (failed-attempt based, for auth/brute-force)
# -----------------------------
# This is the original mechanism: N failures within a window trips a
# lockout for a further window. Used for login/admin-key/paid-key guessing,
# where what matters is repeated *failures*, not raw request volume.

RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW = 15 * 60      # 15 minutes to accumulate failures
RATE_LIMIT_LOCKOUT = 15 * 60     # 15 minute lockout once tripped

_rate_state: Dict[str, Dict[str, Any]] = {}
_rate_lock = asyncio.Lock()


# Rough shape check for a single IPv4/IPv6 address/token, used to sanity-check
# whatever shows up in X-Forwarded-For before we trust it as "the" client IP.
_IP_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F:.]{2,45}$")


def _client_ip(request: Request) -> str:
    # If this app sits behind a reverse proxy (Railway, nginx, etc.), the
    # real client IP arrives via X-Forwarded-For - request.client.host would
    # otherwise just be the proxy's IP for every single visitor, which would
    # make all per-IP rate limiting useless. We take the left-most entry,
    # but only if it actually looks like an IP; a malformed/spoofed header
    # falls back to the direct connection IP rather than being trusted blindly.
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
# SLIDING-WINDOW RATE LIMITING (raw request volume, for every endpoint)
# -----------------------------
# Complements the lockout system above: this caps how many requests of any
# kind (successful or not) a single IP can send to a given bucket in a
# rolling window. In-memory and per-process - fine for a single Railway
# instance; move to a shared store (Redis) if you ever scale to multiple
# replicas, since each process would otherwise track its own counters.

_volume_buckets: Dict[str, deque] = defaultdict(deque)


def rate_limited(ip: str, bucket: str, max_requests: int, window_seconds: float) -> bool:
    """Returns True if this ip/bucket combo has exceeded the allowed rate."""
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
    """Periodically clears out stale sliding-window buckets so memory
    doesn't grow forever from one-off visitors."""
    while True:
        await asyncio.sleep(600)
        now = time.monotonic()
        stale_keys = [k for k, q in _volume_buckets.items() if not q or now - q[-1] > 3600]
        for k in stale_keys:
            _volume_buckets.pop(k, None)


def reject_if_oversized(request: Request, max_bytes: int) -> bool:
    """Cheap pre-check using the declared Content-Length so we can bail
    before buffering a huge/garbage body into memory. This is a courtesy
    check, not a hard guarantee - a client can lie about Content-Length or
    stream via chunked encoding, so the actual byte-length is still
    re-checked after the body is read (as this app already does). For real
    protection against giant bodies, also set a max request size at the
    reverse proxy / platform level in front of this service.
    """
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                return True
        except ValueError:
            return True
    return False


# -----------------------------
# CONTENT FILTERING (links, Discord invites, profanity/slurs - including
# spaced-out / leetspeak / accented evasion attempts)
# -----------------------------
# Applied to the open /logs endpoint and to /usernames, since both accept
# free text from untrusted, unauthenticated clients.

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

# Control characters (other than the ones we intentionally strip via
# .strip()) have no legitimate reason to be in a username or a log line -
# reject outright rather than silently stripping them. This permissive
# version still allows tab/newline/CR through, so it's only appropriate for
# genuinely multi-line fields (script code, announcements).
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Strict version for single-line fields (log lines, usernames, WSS URLs,
# blacklist entries) - blocks EVERY control character including tab,
# newline, and carriage return. A newline embedded in a "single value" is
# itself a format violation: it could forge extra /logs entries, corrupt
# the one-line-per-username storage files, or otherwise smuggle structure
# into a field that's supposed to be a single atomic value.
_STRICT_SINGLE_LINE_PATTERN = re.compile(r"[\x00-\x1f\x7f]")

# A field consisting entirely of NUL bytes has no legitimate use anywhere,
# including inside multi-line script code (Lua source is text, never
# embeds NULs) - checked separately from _CONTROL_CHAR_PATTERN since script
# code legitimately contains tabs/newlines that the permissive pattern
# already allows.
_NUL_BYTE_PATTERN = re.compile(r"\x00")

# Any whitespace at all (regular space, tabs, newlines, unicode spaces,
# etc). Usernames must be a single unbroken token - anything containing
# whitespace is silently ignored rather than stored.
_ANY_WHITESPACE_PATTERN = re.compile(r"\s")

# ws:// or wss:// URL, plain hostname (letters/digits/dots/hyphens), an
# optional port, and an optional path - anything outside this shape for
# the /secure endpoint is rejected rather than stored verbatim.
WSS_URL_PATTERN = re.compile(
    r"^wss?://[A-Za-z0-9.\-]{1,253}(:\d{1,5})?(/[A-Za-z0-9._~\-/%]*)?$"
)

# Shared shape check for usernames submitted to /usernames, /blacklisted,
# and /unblacklisted: must start and end with a letter/digit, 3-32 chars
# total, and allows single (non-repeated) underscores/periods in between -
# covers both classic Roblox usernames and newer period-containing display
# names, while still rejecting garbage.
USERNAME_FORMAT_PATTERN = re.compile(r"^(?!.*[_.]{2})[A-Za-z0-9][A-Za-z0-9_.]{1,30}[A-Za-z0-9]$")

# Paid keys are secrets.token_urlsafe() output (URL-safe base64 alphabet);
# HWIDs are opaque client-generated identifiers. Neither has any legitimate
# reason to be huge or to contain arbitrary characters - bound both before
# they're used in any comparison or dict lookup.
KEY_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
HWID_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def normalize_field(text: str) -> str:
    """Unicode-normalize a raw field (NFKC) so visually-similar characters
    (full-width forms, combining marks, etc.) collapse to their plain ASCII
    equivalent before we validate shape or scan for blocked content."""
    return unicodedata.normalize("NFKC", text)


def _normalize_for_matching(text: str) -> str:
    """Aggressively collapse a field down to bare lowercase letters/digits
    (stripping accents, translating common leetspeak, removing spaces and
    punctuation) purely for blocklist substring matching - NOT used for
    display or storage."""
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    stripped = stripped.lower().translate(_LEET_TRANSLATION)
    return re.sub(r"[^a-z0-9]+", "", stripped)


def contains_blocked_content(*fields: str) -> bool:
    """True if any field contains a link or a blocked word, including
    common spaced-out / leetspeak / punctuation-obfuscated variants."""
    for field in fields:
        if _URL_PATTERN.search(field.lower()):
            return True
        normalized = _normalize_for_matching(field)
        for bad in BLOCKED_SUBSTRINGS:
            if bad in normalized:
                return True
    return False


def has_excessive_repetition(text: str, max_repeat: int = 6) -> bool:
    """Flags obvious spam like 'aaaaaaaaaa' or '!!!!!!!!!!' - a single
    character repeated more than max_repeat times in a row."""
    return bool(re.search(r"(.)\1{" + str(max_repeat) + r",}", text))


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
MAX_LOG_LEN = 4096                   # a single /logs line - generous but bounded
MAX_USERNAME_LEN = 32
MAX_BANNER_LEN = 500                 # /banner text shown on the /scripts page


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
BANNER_FILE = "banner.txt"

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

# These local files are now ONLY a read fallback (last-known-good mirror of
# GitHub) - nothing in this app writes to them except get_github_script()
# syncing down a fresh GitHub copy.
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

# Central place mapping each fixed script name to its local fallback file + default.
FIXED_SCRIPTS: Dict[str, Dict[str, str]] = {
    "dexchilli": {"file": DEXCHILLI_FILE, "default": DEFAULT_DEXCHILLI, "label": "DexChilli"},
    "dexfree": {"file": DEXFREE_FILE, "default": DEFAULT_DEXFREE, "label": "DexFree"},
    "dexserverhop": {"file": DEXSERVERHOP_FILE, "default": DEFAULT_DEXSERVERHOP, "label": "DexServerHop"},
    "dexhub": {"file": DEXHUB_FILE, "default": DEFAULT_DEXHUB, "label": "DexHub"},
    "dexpaid": {"file": DEXPAID_FILE, "default": DEFAULT_DEXPAID, "label": "DexPaid"},
    "dexautoroll": {"file": DEXAUTOROLL_FILE, "default": DEFAULT_DEXAUTOROLL, "label": "DexAutoRoll"},
}

# Short taglines shown on the public /scripts page - purely cosmetic, no
# behavior depends on this.
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

# Hard cap on how many usernames can be stored at once. Once a POST would
# bring the list to (or past) this size, the app cleans out anything that
# shouldn't be there and, if still at capacity, restarts the process (see
# schedule_restart() / restart_process() below) so it comes back up clean.
MAX_STORED_USERNAMES = 5000


def load_usernames_from_file() -> set:
    if not os.path.exists(USERNAME_FILE):
        return set()
    with open(USERNAME_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f.readlines() if line.strip())


def save_usernames_to_file(names: set):
    _atomic_write(USERNAME_FILE, "\n".join(sorted(names)) + ("\n" if names else ""), mode=0o644)


stored_usernames: set = load_usernames_from_file()
# Lowercased mirror purely for case-insensitive dedup, so "Foo", "foo", and
# "FOO" don't each get stored as separate entries.
stored_usernames_lower: set = {u.lower() for u in stored_usernames}


def _purge_bad_usernames_locked() -> bool:
    """Defense-in-depth sweep of the in-memory username set: removes any
    entry that is empty, contains whitespace, fails the username shape
    check, or matches the blocklist (including obfuscated variants). Must
    be called while already holding `lock`. Returns True if anything was
    removed (i.e. the on-disk file needs rewriting)."""
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
    """Periodically sweeps the stored username list for anything that
    shouldn't be there (bad words, whitespace, bad shape) and rewrites the
    file if it had to remove something. Belt-and-suspenders on top of the
    checks already done at write time in /usernames."""
    while True:
        await asyncio.sleep(300)
        async with lock:
            changed = _purge_bad_usernames_locked()
            if changed:
                save_usernames_to_file(stored_usernames)


def restart_process():
    """Re-exec this process in place. Used when the username list hits its
    cap so the app comes back up with a clean, freshly-loaded state instead
    of just silently refusing new entries forever."""
    print("[USERNAMES] Reached MAX_STORED_USERNAMES - restarting process.")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def schedule_restart(delay: float = 1.0):
    """Give the current HTTP response a moment to actually get flushed to
    the client before we tear down and re-exec the process."""
    await asyncio.sleep(delay)
    restart_process()

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
# BANNER FILE HELPERS
# -----------------------------
# The banner is a short, admin-controlled line of text shown at the top of
# the public /scripts page. It's persisted to disk (unlike the ephemeral
# /announcements popup) so it survives restarts. Only POST /banner (with a
# valid X-Api-Key) can change it - there is no unauthenticated write path.

def load_banner_from_file() -> str:
    if not os.path.exists(BANNER_FILE):
        return ""
    with open(BANNER_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_banner_to_file(text: str):
    _atomic_write(BANNER_FILE, text, mode=0o644)


banner_text: str = load_banner_from_file()

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
    "dexautoroll", "admin/stats", "admin/update", "favicon.ico", "robots.txt",
    "scripts", "banner", "github/refresh", "dexpaid/keys",
}
RESERVED_PATHS_LOWER = {p.lower() for p in RESERVED_PATHS}


def ensure_builtin_scripts():
    """Registers the fixed scripts in the `scripts` dict for the admin
    overview listing only. Their actual served content always comes from
    get_github_script() at request time, never from this dict."""
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

# -----------------------------
# GLOBAL HTTP MIDDLEWARE — applies to every plain HTTP request (not the
# WebSocket upgrade, which is handled separately below): an overall per-IP
# request budget on top of each endpoint's own tighter limit, a standard
# set of defensive response headers, and a catch-all so nothing ever leaks
# a stack trace to a client.
# -----------------------------

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
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    )
    # Session-bearing / dynamic pages should never be cached by an
    # intermediary; static loader responses are short-lived anyway.
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"

    # Consistent DexNotifier visual skin for every HTML endpoint.
    media = response.headers.get("content-type", "")
    if "text/html" in media and getattr(response, "body", None):
        skin = b"""<style id="dex-global-skin">
:root{color-scheme:dark;--dex-bg:#060913;--dex-bg2:#0a1020;--dex-card:rgba(13,20,36,.88);--dex-card2:rgba(10,16,29,.92);--dex-line:#22304b;--dex-line2:#2c3d5e;--dex-text:#f4f7ff;--dex-muted:#91a0bb;--dex-accent:#7c5cff;--dex-cyan:#22d3ee;--dex-green:#35d07f;--dex-red:#ff6b81}
*{box-sizing:border-box}
html{background:var(--dex-bg)}
body{background:radial-gradient(900px 520px at 8% -10%,rgba(124,92,255,.20),transparent 62%),radial-gradient(760px 500px at 92% 0%,rgba(34,211,238,.12),transparent 58%),linear-gradient(145deg,#050812,#090f1d 55%,#060913)!important;color:var(--dex-text)!important;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif!important;min-height:100vh;letter-spacing:.005em}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);background-size:36px 36px;mask-image:linear-gradient(to bottom,black,transparent 78%);z-index:0}
body>*{position:relative;z-index:1}
a{color:#9db4ff!important;text-decoration:none;transition:.18s ease}a:hover{color:#d5ddff!important}
h1,h2,h3,h4{color:#f7f9ff!important;letter-spacing:-.025em}
p,small,.muted,.subtitle,.hint{color:var(--dex-muted)!important;line-height:1.65}
button,.btn,input[type=submit],input[type=button]{border-radius:12px!important;border:1px solid rgba(124,92,255,.35)!important;background:linear-gradient(135deg,#7657f4,#4d79ff)!important;color:#fff!important;font-weight:800!important;box-shadow:0 10px 28px rgba(70,80,220,.18);transition:transform .15s ease,box-shadow .15s ease,filter .15s ease;cursor:pointer}
button:hover,.btn:hover,input[type=submit]:hover,input[type=button]:hover{transform:translateY(-1px);filter:brightness(1.08);box-shadow:0 14px 34px rgba(70,80,220,.26)}
button:disabled,.btn:disabled{opacity:.55;transform:none;cursor:not-allowed}
input,textarea,select{border-radius:12px!important;border:1px solid var(--dex-line2)!important;background:rgba(5,10,19,.82)!important;color:#e8efff!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.025),0 8px 28px rgba(0,0,0,.12);outline:none}
input:focus,textarea:focus,select:focus{border-color:rgba(124,92,255,.8)!important;box-shadow:0 0 0 3px rgba(124,92,255,.12)!important}
.card,.panel,.container,.box,.resultbox,.result,.script-card,.admin-card,.section,.hero-card,.feature,.stat,.table-wrap,.auth-card{background:linear-gradient(145deg,var(--dex-card),var(--dex-card2))!important;border:1px solid rgba(86,108,150,.24)!important;border-radius:20px!important;box-shadow:0 24px 80px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.025)!important;backdrop-filter:blur(14px)}
.card:hover,.script-card:hover,.feature:hover{border-color:rgba(124,92,255,.38)!important}
pre,.code-box,.code,.output,.out{background:#050a13!important;border:1px solid var(--dex-line)!important;border-radius:14px!important;color:#cfe0ff!important}
table{border-collapse:separate!important;border-spacing:0!important;width:100%}th{background:#101a2d!important;color:#dce7ff!important}td{background:rgba(7,12,22,.65)!important;color:#b9c7df!important;border-color:#1d2a42!important}th:first-child{border-top-left-radius:10px}th:last-child{border-top-right-radius:10px}tr:last-child td:first-child{border-bottom-left-radius:10px}tr:last-child td:last-child{border-bottom-right-radius:10px}
hr{border:0!important;border-top:1px solid rgba(75,94,130,.25)!important}
.badge,.pill,.tag{border-radius:999px!important;background:rgba(124,92,255,.12)!important;border:1px solid rgba(124,92,255,.30)!important;color:#cfd5ff!important}
.status.ok,.success{color:var(--dex-green)!important}.status.error,.error{color:var(--dex-red)!important}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-track{background:#060a12}::-webkit-scrollbar-thumb{background:#243453;border-radius:999px;border:2px solid #060a12}::-webkit-scrollbar-thumb:hover{background:#354a73}
@media(max-width:700px){body{font-size:14px}.wrap,.container{width:min(100% - 24px,1180px)!important;margin-left:auto!important;margin-right:auto!important}.card,.panel,.container,.box,.resultbox,.result,.script-card,.admin-card,.section,.hero-card,.feature,.stat,.table-wrap,.auth-card{border-radius:16px!important}.grid{grid-template-columns:1fr!important}.copyrow{flex-direction:column!important}.actions{flex-direction:column!important;align-items:stretch!important}button,.btn,input[type=submit],input[type=button]{min-height:46px}}
</style>"""
        body = response.body
        if b"dex-global-skin" not in body:
            body = body.replace(b"</head>", skin + b"</head>", 1)
            response.body = body
            response.headers["content-length"] = str(len(body))
    return response


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # Never leak stack traces / internals to the client.
    print(f"❌ Unhandled exception on {request.url.path}: {exc}")
    return PlainTextResponse("INTERNAL_ERROR", status_code=500)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(rate_bucket_janitor())
    asyncio.create_task(username_cleanup_janitor())

# -----------------------------
# /secure ENDPOINT (now key-protected - previously open to anyone)
# -----------------------------
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


# -----------------------------
# WEBSOCKET ENDPOINT (VIEWERS + SENDER) - requires a key to connect, capped
# per IP, and now rate-limited on both connection attempts and message
# volume so a single client can't flood the broadcast or brute-force the
# in-band sender-auth message.
# -----------------------------

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

    # Require a valid key just to open the connection at all - previously
    # anyone could connect anonymously and read every broadcast log.
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
            else:
                continue

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

# -----------------------------
# LOGS ENDPOINT (HTTP -> WS) - stays key-less per requirements, but is now
# rate-limited AND content-filtered: control characters, links/Discord
# invites, and profanity/slurs (including obfuscated variants) cause the
# post to be rejected outright rather than stored or broadcast.
# -----------------------------

LOGS_POST_RATE_LIMIT = 20
LOGS_POST_RATE_WINDOW = 10.0
LOGS_GET_RATE_LIMIT = 30
LOGS_GET_RATE_WINDOW = 10.0


@app.post("/logs")
async def post_logs(request: Request):
    # Intentionally NO key check here - /logs is one of the POST endpoints
    # that stays open per updated requirements (like /usernames below).
    # Because it's open, it gets its own rate limit and strict content
    # filtering so it can't be used to spam the WS broadcast with links,
    # slurs, or garbage.
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

# -----------------------------
# USERNAME ENDPOINTS
#
# POST is now OPEN - no X-Api-Key required at all. Because anyone can hit
# it, it leans much harder on the layered defenses below instead of an
# auth check:
#   - tight sliding-window rate limit per IP (volume)
#   - a separate failed-attempt lockout per IP for rejected submissions
#     (so a script hammering it with garbage gets locked out, not just
#     slowed down)
#   - strict shape check (letters/digits/_/- only, 3-32 chars)
#   - ANY whitespace at all (space, tab, newline, unicode space) means the
#     submission is silently ignored - nothing is stored, no error is
#     raised, it just doesn't get added
#   - full bad-word / slur / link / Discord-invite filter (including
#     leetspeak + accent + spacing evasion) - any match is rejected and
#     never stored
#   - spam-repetition filter ("aaaaaaaaa", "!!!!!!!!!")
#   - reserved-name + case-insensitive dedup
#   - hard cap of MAX_STORED_USERNAMES (5000): once reached, the list is
#     swept for anything that shouldn't be there, and if it's still at
#     capacity after that, the process restarts itself so it comes back up
#     clean
#
# GET stays public and rate-limited, same as before.
# -----------------------------

USERNAME_POST_RATE_LIMIT = 8
USERNAME_POST_RATE_WINDOW = 10.0
USERNAME_GET_RATE_LIMIT = 30
USERNAME_GET_RATE_WINDOW = 10.0


@app.post("/usernames")
async def add_username(request: Request):
    ip = _client_ip(request)

    # Volume-based limit (applies to every request, valid or not).
    if rate_limited(ip, "username_post", max_requests=USERNAME_POST_RATE_LIMIT, window_seconds=USERNAME_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    # Failed-attempt lockout (applies specifically to rejected submissions -
    # this is what actually stops someone from grinding away at the filter
    # with garbage now that there's no key gating the endpoint).
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

    # Any whitespace anywhere (not just leading/trailing, which .strip()
    # already removed) means this isn't a single real username token -
    # ignore it outright, don't store it, don't even count it as a
    # "rejection" since it's not an attack signal on its own.
    if _ANY_WHITESPACE_PATTERN.search(raw_username):
        return PlainTextResponse("IGNORED_CONTAINS_WHITESPACE")

    if _CONTROL_CHAR_PATTERN.search(raw_username):
        await record_failed_attempt("username_reject", ip)
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)

    username = normalize_field(raw_username)

    # Re-check whitespace after normalization too (NFKC can turn some
    # unicode spacing characters into a plain space).
    if _ANY_WHITESPACE_PATTERN.search(username):
        return PlainTextResponse("IGNORED_CONTAINS_WHITESPACE")

    if not is_valid_username(username):
        # is_valid_username() already covers shape, reserved names, and the
        # blocked-content filter (bad words / slurs / links) in one place.
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

        # Defense-in-depth: sweep out anything bad that might already be in
        # the set (e.g. a legacy entry from before this filter existed).
        _purge_bad_usernames_locked()

        save_usernames_to_file(stored_usernames)

        if len(stored_usernames) >= MAX_STORED_USERNAMES:
            should_restart = True

    await clear_attempts("username_reject", ip)

    if should_restart:
        # Let this response go out first, then tear down and re-exec so the
        # app comes back up with a clean slate instead of just silently
        # refusing every new username forever.
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

# -----------------------------
# BLACKLIST ENDPOINTS - writes key-protected + rate-limited, reads public
# but rate-limited too.
# -----------------------------

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

# -----------------------------
# ANNOUNCEMENTS ENDPOINT - POST key-protected + rate-limited, GET public
# but rate-limited (generously, since clients poll it).
# -----------------------------

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
        else:
            return PlainTextResponse("")

# -----------------------------
# /banner ENDPOINT - the persistent banner shown at the top of /scripts.
# POST is key-protected + rate-limited (this is the endpoint the admin
# panel instructions point at); GET is public + rate-limited so the
# /scripts page (or anything else) can read the current value.
# -----------------------------

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

    # Key required - this is the only write path for the banner. No key,
    # no post: an invalid or missing X-Api-Key is rejected outright and
    # nothing is ever stored.
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

    # Banner is rendered on a public page - block control characters even
    # though the admin is trusted, as defense in depth against a leaked/
    # mistyped key being used to inject junk into stored HTML-adjacent text.
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

# -----------------------------
# DEXPAID GLOBAL KEY GENERATION - moved out of /admin (which is view-only now)
# -----------------------------

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
        last_generated_paid_loadstring = (
            f'loadstring(game:HttpGet("{BASE_URL}/dexpaid?key={new_key}"))()'
        )

    return JSONResponse({
        "key": new_key,
        "expires_at": expiry,
        "loadstring": last_generated_paid_loadstring,
    })

# -----------------------------
# GITHUB CACHE REFRESH - moved out of /admin (which is view-only now)
# -----------------------------

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

# -----------------------------
# HOME PAGE (ROOT)
# -----------------------------

INDEX_RATE_LIMIT = 30
INDEX_RATE_WINDOW = 10.0


@app.get("/")
async def index(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "index_get", max_requests=INDEX_RATE_LIMIT, window_seconds=INDEX_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

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
/dexfree, /dexchilli, /dexserverhop, /dexhub, /dexpaid, /dexautoroll, /&lt;your-endpoint&gt; -> "Private Script"<br><br>
Executor usage:<br>
loadstring(game:HttpGet("{BASE_URL}/dexfree"))()<br><br>
Paid usage:<br>
loadstring(game:HttpGet("{BASE_URL}/dexpaid?key=YOUR_KEY"))()<br><br>
Browse all free scripts (nice UI):<br>
{BASE_URL}/scripts
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html_page)

# -----------------------------
# /scripts PAGE - public, read-only showcase of every free/public loader
# script (everything except /dexpaid, which needs a key and lives on its
# own endpoint). Shows the admin-settable banner (see POST /banner above)
# plus a loadstring + copy button per script.
# -----------------------------

SCRIPTS_GET_RATE_LIMIT = 30
SCRIPTS_GET_RATE_WINDOW = 10.0

SCRIPTS_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>DEX SCRIPTS</title>
    <style>
        :root {{
            --bg: #050509; --accent1: #4fc3f7; --accent2: #7c4dff;
            --accent3: #ff5252; --accent4: #00e676; --border: #1c1c24;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            background:
                radial-gradient(circle at top left, #202040 0, #050509 40%, #000000 100%),
                linear-gradient(135deg, rgba(79,195,247,0.08), rgba(255,82,82,0.08));
            color: #e6e6e6;
        }}
        .wrap {{ max-width: 1180px; margin: 0 auto; padding: 48px 22px 60px; }}

        .hero {{ text-align: center; margin-bottom: 34px; }}
        .hero h1 {{
            margin: 0; font-size: 46px; font-weight: 800; letter-spacing: 0.08em;
            background: linear-gradient(135deg, var(--accent1), var(--accent2));
            -webkit-background-clip: text; background-clip: text; color: transparent;
        }}
        .hero p {{ margin: 10px 0 0; color: #9a9ab0; font-size: 15px; }}
        .hero .pills {{ margin-top: 16px; }}
        .pill {{
            display: inline-block; padding: 5px 14px; border-radius: 999px; font-size: 12px;
            background: rgba(79,195,247,0.14); border: 1px solid rgba(79,195,247,0.35);
            color: #e6f7ff; margin: 0 4px;
        }}
        .pill.green {{ background: rgba(0,230,118,0.14); border-color: rgba(0,230,118,0.35); color: #e6fff3; }}
        .pill.purple {{ background: rgba(124,77,255,0.14); border-color: rgba(124,77,255,0.35); color: #f0e6ff; }}

        .banner {{
            max-width: 900px; margin: 0 auto 34px; padding: 14px 20px; border-radius: 14px;
            background: linear-gradient(135deg, rgba(124,77,255,0.16), rgba(79,195,247,0.16));
            border: 1px solid rgba(124,77,255,0.35); color: #f2f2ff; font-size: 14px;
            text-align: center; box-shadow: 0 12px 30px rgba(0,0,0,0.35);
        }}

        .grid {{
            display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 22px;
        }}
        .script-card {{
            background: linear-gradient(150deg, rgba(16,16,24,0.96), rgba(9,9,15,0.96));
            border: 1px solid rgba(79,195,247,0.16); border-radius: 18px; padding: 22px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.55);
            position: relative; overflow: hidden; transition: transform 0.15s ease, border-color 0.15s ease;
        }}
        .script-card:hover {{ transform: translateY(-3px); border-color: rgba(79,195,247,0.4); }}
        .script-card::before {{
            content: ""; position: absolute; inset: -40% -40% auto auto; width: 160px; height: 160px;
            background: radial-gradient(circle, rgba(79,195,247,0.18), transparent 70%); pointer-events: none;
        }}
        .script-card-header {{
            display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;
        }}
        .script-card-header h2 {{ margin: 0; font-size: 19px; letter-spacing: 0.01em; }}
        .tagline {{ color: #9a9ab0; font-size: 13px; margin: 4px 0 14px; }}
        .endpoint-row {{ font-size: 12px; color: #8a8aa0; margin-bottom: 12px; }}
        .endpoint-row code {{
            background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 6px; color: #c9c9e0;
        }}

        .code-row {{
            display: flex; align-items: center; gap: 10px;
            background: rgba(8,8,13,0.95); border: 1px solid #262636; border-radius: 12px;
            padding: 12px 12px; overflow: hidden;
        }}
        .code-row code {{
            flex: 1; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 12.5px; color: #9eff9e; white-space: nowrap; overflow-x: auto;
        }}
        .copy-btn {{
            flex-shrink: 0; border: none; border-radius: 999px; padding: 8px 16px; font-weight: 600;
            font-size: 12px; cursor: pointer; color: #050509;
            background: linear-gradient(135deg, var(--accent1), var(--accent2));
            transition: opacity 0.15s ease;
        }}
        .copy-btn:hover {{ opacity: 0.85; }}
        .copy-btn.copied {{ background: linear-gradient(135deg, var(--accent4), #00c853); }}

        .paid-note {{
            max-width: 900px; margin: 34px auto 0; text-align: center; font-size: 13px; color: #8a8aa0;
        }}
        .paid-note a {{ color: var(--accent1); text-decoration: none; }}
        .paid-note a:hover {{ text-decoration: underline; }}

        footer {{ text-align: center; margin-top: 46px; font-size: 12px; color: #5c5c70; }}
    </style>
</head>
<body>
    <div class="wrap">
        <div class="hero">
            <h1>DEX SCRIPTS</h1>
            <p>Every free Dex loader, ready to copy into your executor.</p>
            <div class="pills">
                <span class="pill green">Free</span>
                <span class="pill">No key required</span>
                <span class="pill purple">Always up to date</span>
            </div>
        </div>
        {banner_html}
        <div class="grid">
            {cards}
        </div>
        <p class="paid-note">Looking for the paid script? Head to <a href="/dexpaid?key=YOUR_KEY">/dexpaid</a> with your key instead.</p>
        <footer>Dex API</footer>
    </div>
    <script>
        function copyScript(id, btn) {{
            const el = document.getElementById('code-' + id);
            if (!el) return;
            const text = el.textContent;
            const done = () => {{
                const original = btn.textContent;
                btn.textContent = 'Copied!';
                btn.classList.add('copied');
                setTimeout(() => {{
                    btn.textContent = original;
                    btn.classList.remove('copied');
                }}, 1500);
            }};
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
            }} else {{
                fallbackCopy(text, done);
            }}
        }}
        function fallbackCopy(text, done) {{
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try {{ document.execCommand('copy'); }} catch (e) {{}}
            document.body.removeChild(ta);
            done();
        }}
    </script>
</body>
</html>
"""


def build_scripts_page_html() -> str:
    banner_html = ""
    if banner_text:
        banner_html = f'<div class="banner">{html.escape(banner_text)}</div>'

    cards = ""
    for name, meta in FIXED_SCRIPTS.items():
        if name == "dexpaid":
            continue
        endpoint = f"{BASE_URL}/{name}"
        loadstring = f'loadstring(game:HttpGet("{html.escape(endpoint)}"))()'
        tagline = SCRIPT_TAGLINES.get(name, "")
        cards += f"""
        <div class="script-card">
            <div class="script-card-header">
                <h2>{html.escape(meta['label'])}</h2>
                <span class="pill green">Free</span>
            </div>
            <p class="tagline">{html.escape(tagline)}</p>
            <p class="endpoint-row">Endpoint: <code>/{html.escape(name)}</code></p>
            <div class="code-row">
                <code id="code-{html.escape(name)}">{loadstring}</code>
                <button class="copy-btn" onclick="copyScript('{html.escape(name)}', this)">Copy</button>
            </div>
        </div>
        """

    return SCRIPTS_PAGE_HTML.format(banner_html=banner_html, cards=cards)


@app.get("/scripts")
async def scripts_page(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "scripts_get", max_requests=SCRIPTS_GET_RATE_LIMIT, window_seconds=SCRIPTS_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    async with banner_lock:
        page = build_scripts_page_html()
    return HTMLResponse(page)

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


HOME_GET_RATE_LIMIT = 30
HOME_GET_RATE_WINDOW = 10.0
HOME_POST_RATE_LIMIT = 20
HOME_POST_RATE_WINDOW = 10.0


@app.get("/home")
async def home_get(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "home_get", max_requests=HOME_GET_RATE_LIMIT, window_seconds=HOME_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    username = get_logged_in_user(request)
    if not username:
        body = build_home_logged_out_body()
        return HTMLResponse(HOME_BASE_HTML.format(body=body))
    body = build_home_logged_in_body(username)
    return HTMLResponse(HOME_BASE_HTML.format(body=body))


@app.post("/home")
async def home_post(request: Request):
    ip = _client_ip(request)

    # Coarse per-IP volume cap on top of the existing failed-login lockout
    # below - stops mass script/account creation even when every individual
    # attempt "succeeds".
    if rate_limited(ip, "home_post", max_requests=HOME_POST_RATE_LIMIT, window_seconds=HOME_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    if reject_if_oversized(request, MAX_FORM_BODY):
        return PlainTextResponse("Payload too large.", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_FORM_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)

    data = parse_qs(raw.decode(errors="ignore"))
    action = data.get("action", [""])[0]
    username = data.get("username", [""])[0].strip()
    password = data.get("password", [""])[0]
    if len(password) > MAX_PASSWORD_LEN:
        password = password[:MAX_PASSWORD_LEN]

    if action == "register":
        if await is_rate_limited("home_register", ip):
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body("Too many attempts. Try again later.")), status_code=429)

        if not username or not password:
            await record_failed_attempt("home_register", ip)
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body("Username and password required.")))
        if not is_valid_username(username):
            await record_failed_attempt("home_register", ip)
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body(
                    "Username must be 3-32 chars: letters, numbers, _ or - only, and not a reserved/blocked name.")))
        if len(password) < 8:
            await record_failed_attempt("home_register", ip)
            return HTMLResponse(HOME_BASE_HTML.format(
                body=build_home_logged_out_body("Password must be at least 8 characters.")))
        async with users_lock:
            if username in users:
                await record_failed_attempt("home_register", ip)
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_out_body("Username already in use.")))
            users[username] = {
                "username": username,
                "password_hash": hash_password(password),
                "created_at": time.time(),
            }
            save_users_to_file()
        await clear_attempts("home_register", ip)
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

        async with scripts_lock:
            s = scripts.get(slug)
            if not s or s.get("owner") != current_user:
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(current_user, message="Script not found or not owned by you.")))
            if s.get("github_managed"):
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(
                        current_user,
                        message="This script is managed via the GitHub repo and can't be edited here.")))
            if len(code.encode("utf-8")) > MAX_SCRIPT_BODY:
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(current_user, message="Script code is too large.")))
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
            if s.get("github_managed"):
                return HTMLResponse(HOME_BASE_HTML.format(
                    body=build_home_logged_in_body(
                        current_user,
                        message="This script is managed via the GitHub repo and can't be deleted here.")))
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
        button.secondary {{
            background:linear-gradient(135deg,#3a3a4a,#25252f); color:#e6e6e6;
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
        .locked-note {{
            margin-top:8px; font-size:12px; color:#ffcf6b; background:rgba(255,193,7,0.1);
            border:1px solid rgba(255,193,7,0.3); border-radius:8px; padding:8px 10px;
        }}
        .logs-box {{
            background:rgba(8,8,13,0.95); border-radius:12px; border:1px solid #262636; padding:12px;
            font-family:monospace; font-size:12px; max-height:240px; overflow:auto; white-space:pre-wrap;
        }}
        a.repo-link {{ color:#4fc3f7; }}
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
                document.getElementById('banner-preview-box').textContent = data.banner || 'No banner set.';
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
        <p class="label">Enter the key to view loader scripts, blacklist, announcements, the site banner, paid keys, users, and system stats. This dashboard is view-only.</p>
        <form method="post">
            <label class="label">Admin Key</label><br>
            <input type="password" name="key" placeholder="Enter admin key">
            <button type="submit">Enter</button>
        </form>
        {err_html}
    </div>
    """


ADMIN_GET_RATE_LIMIT = 20
ADMIN_GET_RATE_WINDOW = 60.0


@app.get("/admin")
async def admin_get(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "admin_get", max_requests=ADMIN_GET_RATE_LIMIT, window_seconds=ADMIN_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    # if already have a valid admin session, skip straight to dashboard
    token = request.cookies.get("dex_admin_session")
    if verify_session_token(token, ADMIN_SESSION_MAX_AGE) == "admin":
        return HTMLResponse(ADMIN_BASE_HTML.format(body=await build_admin_dashboard_body()))
    return HTMLResponse(ADMIN_BASE_HTML.format(body=admin_login_form()))


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
            f'Source: <a class="repo-link" href="{html.escape(repo_link)}" target="_blank" rel="noopener">'
            f'{html.escape(GITHUB_OWNER)}/{html.escape(GITHUB_REPO)}@{html.escape(GITHUB_BRANCH)} :: {html.escape(repo_path)}</a>'
        )
    else:
        source_line = (
            "Source: GitHub repo not configured yet - set DEX_GITHUB_OWNER / DEX_GITHUB_REPO. "
            "Serving local fallback file / default text."
        )

    return f"""
    <div class="card">
        <h2>{html.escape(meta['label'])}</h2>
        <p class="label">
            <span class="pill purple">Read-only</span>
            <span class="pill">{html.escape(source or 'unknown')}</span>
            <span class="pill">fetched {html.escape(_format_age(fetched_at))}</span>
        </p>
        <p class="small-text">{source_line}</p>
        <div class="locked-note">
            This script is hard-locked to the GitHub repo. To change it, edit
            <code>{html.escape(repo_path)}</code> in the repo and push - then use
            "Refresh from GitHub" below (or wait up to {GITHUB_CACHE_TTL}s for the cache to expire).
        </div>
        <p class="small-text" style="margin-top:10px;">Current content (read-only):</p>
        <div class="logs-box">{html.escape(content)}</div>
    </div>
    """


async def build_admin_dashboard_body() -> str:
    fixed_cards_html = ""
    for name in ("dexchilli", "dexfree", "dexserverhop", "dexhub", "dexpaid", "dexautoroll"):
        fixed_cards_html += await build_fixed_script_card(name)

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
            f"{s['name']} ({s['slug']}) | owner: {s['owner']} | paid: {s['is_paid']} | "
            f"hwid_lock: {s['hwid_lock']}{' | GITHUB-LOCKED' if s.get('github_managed') else ''}"
        )
    scripts_preview = "\n".join(scripts_lines) if scripts_lines else "No scripts."

    github_status = (
        f"Connected to {html.escape(GITHUB_OWNER)}/{html.escape(GITHUB_REPO)}@{html.escape(GITHUB_BRANCH)}"
        if github_configured() else
        "Not configured - set DEX_GITHUB_OWNER and DEX_GITHUB_REPO"
    )

    body = f"""
    <div class="card">
        <h1>Dex Control Center</h1>
        <p class="label">
            <span class="pill red">View-Only</span>
            <span class="pill">/dexchilli</span>
            <span class="pill">/dexfree</span>
            <span class="pill">/dexserverhop</span>
            <span class="pill purple">/dexhub</span>
            <span class="pill green">/dexpaid</span>
            <span class="pill green">/dexautoroll</span>
            <span class="pill purple">/scripts</span>
        </p>
        <p class="label">This dashboard is read-only. Nothing here can change any data - there is
        no form on this page that submits anywhere. To change announcements, the site banner, the
        blacklist, paid keys, or the GitHub cache, call the API directly with header <code>X-Api-Key</code>:</p>
        <div class="logs-box">
POST /announcements     (raw text body = announcement)
POST /banner             (raw text body = banner shown on /scripts, up to {MAX_BANNER_LEN} chars)
POST /blacklisted        (raw text body = username to add)
POST /unblacklisted      (raw text body = username to remove)
POST /dexpaid/keys       (raw text body = duration in hours)
POST /github/refresh     (no body needed)
NOTE: POST /usernames and POST /logs are intentionally open (no key).
NOTE: POST /banner is key-protected - no valid X-Api-Key, no post; nothing is stored.
        </div>
        <p class="small-text" style="margin-top:10px;">GitHub source: {github_status}</p>
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
        {fixed_cards_html}
    </div>

    <div class="grid">
        <div class="card">
            <h2>Announcements</h2>
            <p class="small-text">Current announcement preview (read-only):</p>
            <div class="logs-box" id="announcement-preview-box">No active announcement.</div>
        </div>
        <div class="card">
            <h2>Site Banner (/scripts page)</h2>
            <p class="small-text">Read-only preview. Set via POST /banner with header X-Api-Key - no key, no post.</p>
            <div class="logs-box" id="banner-preview-box">No banner set.</div>
        </div>
        <div class="card">
            <h2>Blacklisted Users</h2>
            <p class="small-text">Read-only. Manage via POST /blacklisted or /unblacklisted with X-Api-Key.</p>
            <div class="logs-box" id="blacklist-preview-box"></div>
        </div>
        <div class="card">
            <h2>Recent Logs (Discord / Sender)</h2>
            <div class="logs-box" id="recent-logs-box">No logs.</div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>DexPaid Keys</h2>
            <p class="small-text">Read-only. Generate via POST /dexpaid/keys with X-Api-Key.</p>
            <p class="small-text" style="margin-top:10px;">Last generated key:</p>
            <div class="logs-box" id="dexpaid-last-key-box">No key generated yet.</div>
            <p class="small-text" style="margin-top:10px;">Last generated loadstring:</p>
            <div class="logs-box" id="dexpaid-last-loadstring-box">No loadstring generated yet.</div>
            <p class="small-text" style="margin-top:10px;">All active paid keys:</p>
            <div class="logs-box" id="dexpaid-keys-box">{keys_preview_text}</div>
        </div>
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

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("Payload too large.", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("Payload too large.", status_code=413)
    data = parse_qs(raw.decode(errors="ignore"))
    key = data.get("key", [""])[0]

    if not is_valid_key(key):
        await record_failed_attempt("admin_login", ip)
        return HTMLResponse(ADMIN_BASE_HTML.format(body=admin_login_form("Invalid key.")))

    await clear_attempts("admin_login", ip)
    resp = HTMLResponse(ADMIN_BASE_HTML.format(body=await build_admin_dashboard_body()))
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
    # /admin is permanently view-only now. This route intentionally performs
    # NO mutation of any kind, regardless of what's posted to it - it exists
    # only so old bookmarks/requests get a clear explanation instead of a
    # confusing 404. Use the dedicated API endpoints (with X-Api-Key) instead:
    #   POST /announcements, /banner, /blacklisted, /unblacklisted,
    #   /dexpaid/keys, /github/refresh
    if not require_admin_session(request):
        return PlainTextResponse("Unauthorized - please log in at /admin again.", status_code=401)

    return PlainTextResponse(
        "The admin panel is view-only. Nothing can be changed from /admin or /admin/update. "
        "Use the API directly with header X-Api-Key: POST /announcements, /banner, /blacklisted, "
        "/unblacklisted, /dexpaid/keys, or /github/refresh.",
        status_code=403,
    )

# -----------------------------
# ADMIN LIVE STATS API - requires an admin session, and is now rate-limited
# with headroom for the dashboard's own 2s polling.
# -----------------------------

ADMIN_STATS_RATE_LIMIT = 30
ADMIN_STATS_RATE_WINDOW = 10.0


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
    async with banner_lock:
        banner = banner_text
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
                f"{s['name']} ({s['slug']}) | owner: {s['owner']} | paid: {s['is_paid']} | "
                f"hwid_lock: {s['hwid_lock']}{' | GITHUB-LOCKED' if s.get('github_managed') else ''}"
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
            "banner": banner,
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
# FIXED LOADER ENDPOINTS (GITHUB-MANAGED) - rate-limited per IP so the
# loader can't be hammered into re-fetching from GitHub constantly (the
# cache absorbs most of this anyway, but the limit protects against abuse
# regardless of cache state).
# -----------------------------

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


# Paid-key guessing gets its own failed-attempt lockout (separate from the
# general loader rate limit above) since a wrong key here is a meaningful
# "attack signal", not just traffic volume.
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

# -----------------------------
# RAW LOADER BACKEND
# -----------------------------
# The obfuscator POSTs the final Lua text here. This backend stores the
# exact payload under a random loader id and returns a stable raw URL:
#   https://dexapi1.up.railway.app/raw/<LOADER_ID>
#
# Set DEX_API_KEY on this service. The obfuscator sends the same value in
# X-Api-Key (or its own DEX_RAW_BACKEND_API_KEY if you want a separate key).

RAW_LOADER_DIR = os.environ.get("DEX_RAW_LOADER_DIR", "raw_loaders").strip() or "raw_loaders"
RAW_LOADER_POST_RATE_LIMIT = 30
RAW_LOADER_POST_RATE_WINDOW = 60.0
RAW_LOADER_GET_RATE_LIMIT = 120
RAW_LOADER_GET_RATE_WINDOW = 10.0
RAW_LOADER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _publish_local_payload(payload: str):
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("Raw payload is empty.")
    loader_id = _create_raw_loader_id()
    path = _raw_loader_path(loader_id)
    _atomic_write(path, payload, mode=0o600)
    raw_url = f"https://dexnotifier.xyz/raw/{loader_id}"
    return raw_url, loader_id


def _raw_loader_path(loader_id: str) -> str:
    if not RAW_LOADER_ID_PATTERN.fullmatch(loader_id):
        raise ValueError("Invalid loader id.")
    return os.path.join(RAW_LOADER_DIR, loader_id + ".lua")


def _create_raw_loader_id() -> str:
    os.makedirs(RAW_LOADER_DIR, exist_ok=True)
    for _ in range(10):
        loader_id = secrets.token_urlsafe(18).rstrip("=")
        if RAW_LOADER_ID_PATTERN.fullmatch(loader_id):
            path = _raw_loader_path(loader_id)
            if not os.path.exists(path):
                return loader_id
    raise RuntimeError("Could not allocate a unique loader id.")


@app.post("/raw")
async def create_raw_loader(request: Request):
    ip = _client_ip(request)

    if rate_limited(
        ip,
        "raw_loader_post",
        max_requests=RAW_LOADER_POST_RATE_LIMIT,
        window_seconds=RAW_LOADER_POST_RATE_WINDOW,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("raw_loader_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if reject_if_oversized(request, MAX_SCRIPT_BODY):
        return JSONResponse({"error": "payload too large"}, status_code=413)

    try:
        body = await request.body()
    except Exception:
        return JSONResponse({"error": "invalid request body"}, status_code=400)

    if len(body) > MAX_SCRIPT_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    try:
        payload = body.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "payload must be UTF-8 text"}, status_code=400)

    if not payload.strip():
        return JSONResponse({"error": "payload is empty"}, status_code=400)

    if _NUL_BYTE_PATTERN.search(payload):
        return JSONResponse({"error": "payload contains NUL bytes"}, status_code=400)

    try:
        loader_id = _create_raw_loader_id()
        path = _raw_loader_path(loader_id)
        _atomic_write(path, payload, mode=0o600)
    except Exception as exc:
        print(f"[RAW_LOADER] Failed to store payload: {exc}")
        return JSONResponse({"error": "could not store payload"}, status_code=500)

    await clear_attempts("raw_loader_auth", ip)

    raw_url = f"https://dexnotifier.xyz/raw/{loader_id}"
    return JSONResponse(
        {
            "ok": True,
            "loader_id": loader_id,
            "raw_url": raw_url,
            "loadstring": f'loadstring(game:HttpGet("{raw_url}"))()',
        }
    )


@app.get("/raw/{loader_id}")
async def get_raw_loader(loader_id: str, request: Request):
    ip = _client_ip(request)

    if rate_limited(
        ip,
        "raw_loader_get",
        max_requests=RAW_LOADER_GET_RATE_LIMIT,
        window_seconds=RAW_LOADER_GET_RATE_WINDOW,
    ):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    if not RAW_LOADER_ID_PATTERN.fullmatch(loader_id):
        return PlainTextResponse("NOT_FOUND", status_code=404)

    path = _raw_loader_path(loader_id)

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = f.read()
    except FileNotFoundError:
        return PlainTextResponse("NOT_FOUND", status_code=404)
    except Exception as exc:
        print(f"[RAW_LOADER] Failed to read {loader_id}: {exc}")
        return PlainTextResponse("INTERNAL_ERROR", status_code=500)

    return PlainTextResponse(
        payload,
        media_type="text/plain",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


OBF_LEVELS = {
    "light": {"block1": (31, 53), "block2": (35, 59), "decoys": (16, 24), "fragment": (45, 85)},
    "medium": {"block1": (23, 49), "block2": (27, 55), "decoys": (64, 96), "fragment": (25, 65)},
    "hard": {"block1": (17, 53), "block2": (19, 59), "decoys": (128, 192), "fragment": (13, 113)},
}

def normalize_obf_level(level):
    level = str(level or "hard").strip().lower()
    if level in {"lightly", "light"}:
        return "light"
    if level in {"medium"}:
        return "medium"
    if level in {"hard"}:
        return "hard"
    raise ValueError("Invalid obfuscation level. Choose lightly, medium, or hard.")

# ═════════════════════════════════════════════════════════════════════════════
# OBFUSCATOR
# ═════════════════════════════════════════════════════════════════════════════

def _build_loadstring(raw_url):
    """Build the exact loader text shown to the user/copy controls."""
    if not isinstance(raw_url, str):
        raw_url = str(raw_url)

    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("Raw loader URL is empty.")

    # JSON string quoting is also valid Lua string quoting for URLs and avoids
    # accidental breakage if the URL ever contains a quote or backslash.
    return "loadstring(game:HttpGet(" + json.dumps(raw_url) + "))()"


def obfuscate_lua(source: str, publish=True, level="hard") -> str:

    if source is None:
        raise ValueError(
            "No Lua source was supplied."
        )

    if not isinstance(source, str):
        source = str(source)

    if not source.strip():
        raise ValueError(
            "Lua source is empty."
        )

    level = normalize_obf_level(level)
    profile = OBF_LEVELS[level]

    src = source.encode("utf-8")

    if not src:
        raise ValueError(
            "Lua source is empty."
        )

    used = set()

    def N():
        return _unique_name(used)

    # ═══════════════════════════════════════════════════════════════════════
    # RANDOM IDENTIFIERS
    # ═══════════════════════════════════════════════════════════════════════

    V_BYTE       = N()
    V_CHAR       = N()
    V_LEN        = N()
    V_CONCAT     = N()
    V_INSERT     = N()
    V_FLOOR      = N()
    V_LOAD       = N()

    V_XOR        = N()

    V_SEED1      = N()
    V_SEED2      = N()
    V_SEED3      = N()
    V_SEED4      = N()

    V_SEED5      = N()
    V_SEED6      = N()
    V_SEED7      = N()
    V_SEED8      = N()

    V_BLOCKSIZE  = N()
    V_BLOCKSIZE2 = N()

    V_PARTS      = N()
    V_PAYLOAD    = N()
    V_DECODED    = N()
    V_SOURCE     = N()
    V_RESULT     = N()

    V_BLOCKLEN   = N()
    V_STATE      = N()
    V_PERM       = N()

    V_I          = N()
    V_J          = N()
    V_K          = N()

    V_ORIGINAL   = N()
    V_ABSOLUTE   = N()

    V_VALUE      = N()
    V_ROTATION   = N()
    V_ADD        = N()
    V_X          = N()

    V_PREVIOUS   = N()
    V_CURRENT    = N()
    V_FEEDBACK   = N()
    V_FINALMIX   = N()

    V_H1         = N()
    V_H2         = N()
    V_H3         = N()
    V_H4         = N()

    V_EXPECT1    = N()
    V_EXPECT2    = N()
    V_EXPECT3    = N()
    V_EXPECT4    = N()

    V_CH1        = N()
    V_CH2        = N()
    V_CH3        = N()

    V_CEXPECT1   = N()
    V_CEXPECT2   = N()
    V_CEXPECT3   = N()

    V_LENGTH     = N()
    V_EXPECTLEN  = N()

    V_NOISE      = N()

    V_FN         = N()
    V_ERR        = N()

    V_BITCOUNT   = N()
    V_BYTEVALUE  = N()
    V_TOKEN     = N()

    V_ZEROCHAR   = N()
    V_ONECHAR    = N()

    V_GUARD      = N()
    V_GUARD2     = N()

    # ═══════════════════════════════════════════════════════════════════════
    # RANDOM BUILD STATE
    # ═══════════════════════════════════════════════════════════════════════

    seed1 = _RNG.randint(0, 0xFFFF)
    seed2 = _RNG.randint(0, 0xFFFF)
    seed3 = _RNG.randint(0, 0xFFFF)
    seed4 = _RNG.randint(0, 0xFFFF)

    seed5 = _RNG.randint(0, 0xFFFF)
    seed6 = _RNG.randint(0, 0xFFFF)
    seed7 = _RNG.randint(0, 0xFFFF)
    seed8 = _RNG.randint(0, 0xFFFF)

    block_size = _RNG.randint(*profile["block1"])
    block_size2 = _RNG.randint(*profile["block2"])

    token_zero, token_one = (
        _make_token_alphabet()
    )

    # ═══════════════════════════════════════════════════════════════════════
    # FIRST CIPHER LAYER
    # ═══════════════════════════════════════════════════════════════════════

    encrypted1 = _encrypt_round(
        src,
        seed1,
        seed2,
        seed3,
        seed4,
        block_size
    )

    # ═══════════════════════════════════════════════════════════════════════
    # SECOND CIPHER LAYER
    # ═══════════════════════════════════════════════════════════════════════

    encrypted2 = _encrypt_round(
        encrypted1,
        seed5,
        seed6,
        seed7,
        seed8,
        block_size2
    )

    # ═══════════════════════════════════════════════════════════════════════
    # INTERNAL ROUND-TRIP TEST
    # ═══════════════════════════════════════════════════════════════════════

    test1 = _decrypt_round(
        encrypted2,
        seed5,
        seed6,
        seed7,
        seed8,
        block_size2
    )

    test2 = _decrypt_round(
        test1,
        seed1,
        seed2,
        seed3,
        seed4,
        block_size
    )

    if test2 != src:
        raise RuntimeError(
            "Internal encryption/decryption error."
        )

    # ═══════════════════════════════════════════════════════════════════════
    # INTEGRITY VALUES
    # ═══════════════════════════════════════════════════════════════════════

    expected_h1, expected_h2, expected_h3, expected_h4 = (
        _integrity_digest(src)
    )

    expected_ch1, expected_ch2, expected_ch3 = (
        _cipher_digest(encrypted2)
    )

    # ═══════════════════════════════════════════════════════════════════════
    # TOKEN ENCODING
    # ═══════════════════════════════════════════════════════════════════════

    token_payload = _encode_binary_tokens(
        encrypted2,
        token_zero,
        token_one
    )

    if _decode_binary_tokens(
        token_payload,
        token_zero,
        token_one
    ) != encrypted2:
        raise RuntimeError(
            "Binary token encoder failure."
        )

    indexed_fragments = _fragment_tokens(
        token_payload, profile["fragment"][0], profile["fragment"][1]
    )

    # ═══════════════════════════════════════════════════════════════════════
    # OUTPUT
    # ═══════════════════════════════════════════════════════════════════════

    lines = []

    lines.append(
        "-- This file was protected using Dex Obfustucator v3.2 [.gg/dexfinder]"
    )

    lines.append("")
    lines.append("")

    lines.append(
        "return(function(...)"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # STANDARD FUNCTIONS
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local {V_BYTE}=string.byte"
    )

    lines.append(
        f"local {V_CHAR}=string.char"
    )

    lines.append(
        f"local {V_LEN}=string.len"
    )

    lines.append(
        f"local {V_CONCAT}=table.concat"
    )

    lines.append(
        f"local {V_INSERT}=table.insert"
    )

    lines.append(
        f"local {V_FLOOR}=math.floor"
    )

    lines.append(
        f"local {V_LOAD}=loadstring or load"
    )

    lines.append(
        f"if not {V_LOAD} then "
        f"error('Loadstring Is Not Supported On This Executer') "
        f"end"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PURE LUA XOR
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local function {V_XOR}({V_I},{V_J})"
    )

    lines.append(
        f"{V_I}={V_I}%256"
    )

    lines.append(
        f"{V_J}={V_J}%256"
    )

    lines.append(
        f"local {V_K}=0"
    )

    lines.append(
        f"local {V_X}=1"
    )

    lines.append(
        f"while {V_I}>0 or {V_J}>0 do"
    )

    lines.append(
        f"local {V_VALUE}={V_I}%2"
    )

    lines.append(
        f"local {V_ADD}={V_J}%2"
    )

    lines.append(
        f"if {V_VALUE}~={V_ADD} then "
        f"{V_K}={V_K}+{V_X} "
        f"end"
    )

    lines.append(
        f"{V_I}={V_FLOOR}({V_I}/2)"
    )

    lines.append(
        f"{V_J}={V_FLOOR}({V_J}/2)"
    )

    lines.append(
        f"{V_X}={V_X}*2"
    )

    lines.append("end")

    lines.append(
        f"return {V_K}%256"
    )

    lines.append("end")

    # ═══════════════════════════════════════════════════════════════════════
    # SEEDS
    # ═══════════════════════════════════════════════════════════════════════

    for variable, value in (
        (V_SEED1, seed1),
        (V_SEED2, seed2),
        (V_SEED3, seed3),
        (V_SEED4, seed4),
        (V_SEED5, seed5),
        (V_SEED6, seed6),
        (V_SEED7, seed7),
        (V_SEED8, seed8),
        (V_BLOCKSIZE, block_size),
        (V_BLOCKSIZE2, block_size2),
    ):
        lines.append(
            f"local {variable}={_num_expr(value)}"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # TOKEN ALPHABET
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local {V_ZEROCHAR}={_num_expr(ord(token_zero))}"
    )

    lines.append(
        f"local {V_ONECHAR}={_num_expr(ord(token_one))}"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # RUNTIME GUARDS
    # ═══════════════════════════════════════════════════════════════════════

    noise_a = _RNG.randint(1000, 9000)
    noise_b = _RNG.randint(1000, 9000)

    lines.append(
        f"local {V_NOISE}=("
        f"{_num_expr(noise_a)}*"
        f"{_num_expr(noise_b)}-"
        f"{_num_expr(noise_a)}*"
        f"{_num_expr(noise_b)}"
        f")"
    )

    lines.append(
        f"if {V_NOISE}~={_num_expr(0)} then "
        f"return nil "
        f"end"
    )

    guard_value = _RNG.randint(
        1000,
        50000
    )

    guard_a = _RNG.randint(
        100,
        10000
    )

    guard_b = guard_value - guard_a

    lines.append(
        f"local {V_GUARD}=("
        f"{_num_expr(guard_a)}+"
        f"{_num_expr(guard_b)}"
        f")"
    )

    lines.append(
        f"if {V_GUARD}~={_num_expr(guard_value)} then "
        f"error('Internal Error') "
        f"end"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # DECOY RUNTIME ENVIRONMENT
    # ═══════════════════════════════════════════════════════════════════════
    #
    # Non-semantic local environment noise. These fields are unrelated to the
    # payload/decryption state and are discarded before decoding.

    V_FENV      = N()
    V_FACC      = N()
    V_FTMP      = N()

    decoy_count = _RNG.randint(*profile["decoys"])
    decoys = []

    lines.append(
        f"local {V_FENV}={{}}"
    )

    lines.append(
        f"local {V_FACC}=0"
    )

    for _ in range(decoy_count):
        field = _rand_name(_RNG.randint(9, 17))
        value = _RNG.randint(0, 65535)
        add = _RNG.randint(1, 65535)

        decoys.append((field, value))

        lines.append(
            f"{V_FENV}.{field}={_num_expr(value)}"
        )

        lines.append(
            f"{V_FENV}.{field}={V_FENV}.{field}+"
            f"{_num_expr(add)}-{_num_expr(add)}"
        )

        lines.append(
            f"{V_FACC}={V_FACC}+({V_FENV}.{field}%257)"
        )

    expected_acc = sum(
        value % 257
        for _, value in decoys
    )

    lines.append(
        f"local {V_FTMP}=({V_FACC}%{_num_expr(1000003)})"
    )

    lines.append(
        f"if {V_FTMP}~={_num_expr(expected_acc % 1000003)} then "
        f"error('Internal Error') end"
    )

    lines.append(
        f"{V_FENV}=nil"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # TOKEN PAYLOAD
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local {V_PARTS}={{}}"
    )

    for index, fragment in indexed_fragments:

        escaped = _lua_escape_bytes(
            fragment.encode("ascii")
        )

        lines.append(
            f"{V_PARTS}[{index+1}]=\"{escaped}\""
        )

    lines.append(
        f"local {V_PAYLOAD}="
        f"{V_CONCAT}({V_PARTS})"
    )

    lines.append(
        f"local {V_EXPECTLEN}="
        f"{_num_expr(len(encrypted2))}"
    )

    lines.append(
        f"if {V_LEN}({V_PAYLOAD})~="
        f"({V_EXPECTLEN}*8) then "
        f"error('Protected payload length failure') "
        f"end"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # TOKEN DECODER
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local {V_DECODED}={{}}"
    )

    lines.append(
        f"local {V_I}=1"
    )

    lines.append(
        f"local {V_LENGTH}={V_LEN}({V_PAYLOAD})"
    )

    lines.append(
        f"while {V_I}<={V_LENGTH} do"
    )

    lines.append(
        f"local {V_BYTEVALUE}=0"
    )

    lines.append(
        f"for {V_BITCOUNT}=1,8 do"
    )

    lines.append(
        f"local {V_TOKEN}="
        f"{V_BYTE}("
        f"{V_PAYLOAD},"
        f"{V_I}+{V_BITCOUNT}-1"
        f")"
    )

    lines.append(
        f"if {V_TOKEN}=={V_ONECHAR} then "
        f"{V_BYTEVALUE}="
        f"{V_BYTEVALUE}*2+1 "
        f"elseif {V_TOKEN}=={V_ZEROCHAR} then "
        f"{V_BYTEVALUE}="
        f"{V_BYTEVALUE}*2 "
        f"else "
        f"error('Protected token validation failure') "
        f"end"
    )

    lines.append("end")

    lines.append(
        f"{V_INSERT}("
        f"{V_DECODED},"
        f"{V_CHAR}({V_BYTEVALUE})"
        f")"
    )

    lines.append(
        f"{V_I}={V_I}+8"
    )

    lines.append("end")

    lines.append(
        f"local {V_SOURCE}="
        f"{V_CONCAT}({V_DECODED})"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # CIPHER LENGTH
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"if {V_LEN}({V_SOURCE})~="
        f"{V_EXPECTLEN} then "
        f"error('Internal Error') "
        f"end"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # CIPHER INTEGRITY
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local {V_CH1}=0x5A31"
    )

    lines.append(
        f"local {V_CH2}=0x71C9"
    )

    lines.append(
        f"local {V_CH3}=0x42D7"
    )

    lines.append(
        f"for {V_I}=1,{V_LEN}({V_SOURCE}) do"
    )

    lines.append(
        f"local {V_VALUE}="
        f"{V_BYTE}({V_SOURCE},{V_I})"
    )

    lines.append(
        f"{V_CH1}=("
        f"{V_CH1}*251+"
        f"{V_VALUE}+"
        f"{V_I}*3"
        f")%65536"
    )

    lines.append(
        f"{V_CH2}=("
        f"{V_CH2}*277+"
        f"{V_VALUE}*7+"
        f"{V_I}*13"
        f")%65536"
    )

    lines.append(
        f"{V_CH3}=("
        f"{V_CH3}*283+"
        f"{V_VALUE}*11+"
        f"{V_I}*19"
        f")%65536"
    )

    lines.append("end")

    lines.append(
        f"local {V_CEXPECT1}="
        f"{_num_expr(expected_ch1)}"
    )

    lines.append(
        f"local {V_CEXPECT2}="
        f"{_num_expr(expected_ch2)}"
    )

    lines.append(
        f"local {V_CEXPECT3}="
        f"{_num_expr(expected_ch3)}"
    )

    lines.append(
        f"if {V_CH1}~={V_CEXPECT1} or "
        f"{V_CH2}~={V_CEXPECT2} or "
        f"{V_CH3}~={V_CEXPECT3} then "
        f"error('Internal Error') "
        f"end"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # TWO-LAYER DECRYPTION
    # ═══════════════════════════════════════════════════════════════════════

    def emit_decryption_layer(
        layer_seed1,
        layer_seed2,
        layer_seed3,
        layer_seed4,
        layer_block_size,
    ):
        lines.append(
            f"local {V_RESULT}={{}}"
        )

        lines.append(
            f"local {V_I}=1"
        )

        lines.append(
            f"while {V_I}<={V_LEN}({V_SOURCE}) do"
        )

        lines.append(
            f"local {V_BLOCKLEN}=math.min("
            f"{layer_block_size},"
            f"{V_LEN}({V_SOURCE})-{V_I}+1"
            f")"
        )

        lines.append(
            f"local {V_PERM}={{}}"
        )

        lines.append(
            f"for {V_J}=1,{V_BLOCKLEN} do "
            f"{V_PERM}[{V_J}]={V_J}-1 "
            f"end"
        )

        lines.append(
            f"{V_STATE}=("
            f"{layer_seed1}+"
            f"{layer_seed2}+"
            f"{layer_seed3}*{V_I}+"
            f"{layer_seed4}*{V_BLOCKLEN}+"
            f"({V_I}-1)*{_MIX_A}"
            f")%65536"
        )

        lines.append(
            f"for {V_J}={V_BLOCKLEN},2,-1 do"
        )

        lines.append(
            f"{V_STATE}=("
            f"{V_STATE}*25173+"
            f"13849+"
            f"({V_J}-1)*97"
            f")%65536"
        )

        lines.append(
            f"local {V_K}=({V_STATE}%{V_J})+1"
        )

        lines.append(
            f"local {V_X}={V_PERM}[{V_J}]"
        )

        lines.append(
            f"{V_PERM}[{V_J}]={V_PERM}[{V_K}]"
        )

        lines.append(
            f"{V_PERM}[{V_K}]={V_X}"
        )

        lines.append("end")

        lines.append(
            f"{V_STATE}=("
            f"{layer_seed1}+"
            f"{layer_seed3}+"
            f"{V_I}*17+"
            f"{V_BLOCKLEN}*{_MIX_B}"
            f")%65536"
        )

        lines.append(
            f"local {V_PREVIOUS}=("
            f"{layer_seed4}+"
            f"{V_I}-1+"
            f"{V_BLOCKLEN}"
            f")%256"
        )

        lines.append(
            f"local {V_DECODED}={{}}"
        )

        lines.append(
            f"for {V_J}=1,{V_BLOCKLEN} do"
        )

        lines.append(
            f"local {V_ORIGINAL}="
            f"{V_PERM}[{V_J}]"
        )

        lines.append(
            f"local {V_ABSOLUTE}="
            f"{V_I}+{V_ORIGINAL}"
        )

        lines.append(
            f"{V_STATE}=("
            f"{V_STATE}*25173+"
            f"13849+"
            f"{V_ORIGINAL}+"
            f"({V_J}-1)+"
            f"{V_BLOCKLEN}"
            f")%65536"
        )

        lines.append(
            f"local {V_VALUE}="
            f"{V_BYTE}("
            f"{V_SOURCE},"
            f"{V_I}+{V_J}-1"
            f")"
        )

        lines.append(
            f"{V_CURRENT}={V_VALUE}"
        )

        lines.append(
            f"local {V_FINALMIX}=("
            f"math.floor({layer_seed4}/256)+"
            f"({V_J}-1)*17+"
            f"{V_ORIGINAL}*31+"
            f"{V_STATE}"
            f")%256"
        )

        lines.append(
            f"{V_VALUE}="
            f"{V_XOR}("
            f"{V_VALUE},"
            f"{V_FINALMIX}"
            f")"
        )

        lines.append(
            f"{V_FEEDBACK}="
            f"{V_XOR}("
            f"{V_PREVIOUS},"
            f"math.floor({V_STATE}/256)"
            f")"
        )

        lines.append(
            f"{V_FEEDBACK}="
            f"{V_XOR}("
            f"{V_FEEDBACK},"
            f"{layer_seed1}%256"
            f")"
        )

        lines.append(
            f"{V_FEEDBACK}="
            f"{V_XOR}("
            f"{V_FEEDBACK},"
            f"{V_ABSOLUTE}*11"
            f")%256"
        )

        lines.append(
            f"{V_VALUE}="
            f"{V_XOR}("
            f"{V_VALUE},"
            f"{V_FEEDBACK}"
            f")"
        )

        lines.append(
            f"local {V_X}=("
            f"({layer_seed3}%256)+"
            f"({V_J}-1)*29+"
            f"({V_STATE}%256)+"
            f"{V_ABSOLUTE}*7+"
            f"{V_BLOCKLEN}*{_MIX_D}"
            f")%256"
        )

        lines.append(
            f"{V_VALUE}="
            f"{V_XOR}("
            f"{V_VALUE},"
            f"{V_X}"
            f")"
        )

        lines.append(
            f"local {V_ADD}="
            f"{V_XOR}("
            f"{V_XOR}("
            f"math.floor({V_STATE}/256)%256,"
            f"{layer_seed4}%256"
            f"),"
            f"{V_ABSOLUTE}*13"
            f")"
        )

        lines.append(
            f"{V_ADD}="
            f"{V_XOR}("
            f"{V_ADD},"
            f"({V_J}-1)*{_MIX_C}"
            f")%256"
        )

        lines.append(
            f"{V_VALUE}=("
            f"{V_VALUE}-{V_ADD}"
            f")%256"
        )

        lines.append(
            f"local {V_ROTATION}=("
            f"{layer_seed2}+"
            f"{V_ORIGINAL}+"
            f"({V_J}-1)+"
            f"{V_STATE}+"
            f"{V_BLOCKLEN}"
            f")%8"
        )

        lines.append(
            f"if {V_ROTATION}~=0 then"
        )

        lines.append(
            f"local {V_X}=2^{V_ROTATION}"
        )

        lines.append(
            f"local {V_ADD}=2^(8-{V_ROTATION})"
        )

        lines.append(
            f"{V_VALUE}=("
            f"{V_FLOOR}("
            f"{V_VALUE}/{V_X}"
            f")+"
            f"(({V_VALUE}%{V_X})*{V_ADD})"
            f")%256"
        )

        lines.append("end")

        lines.append(
            f"{V_DECODED}[{V_ORIGINAL}+1]="
            f"{V_VALUE}"
        )

        lines.append(
            f"{V_PREVIOUS}={V_CURRENT}"
        )

        lines.append("end")

        lines.append(
            f"for {V_J}=1,{V_BLOCKLEN} do"
        )

        lines.append(
            f"{V_INSERT}("
            f"{V_RESULT},"
            f"{V_CHAR}("
            f"{V_DECODED}[{V_J}]"
            f")"
            f")"
        )

        lines.append("end")

        lines.append(
            f"{V_I}={V_I}+{V_BLOCKLEN}"
        )

        lines.append("end")

        lines.append(
            f"{V_SOURCE}="
            f"{V_CONCAT}({V_RESULT})"
        )

    emit_decryption_layer(
        f"{V_SEED5}",
        f"{V_SEED6}",
        f"{V_SEED7}",
        f"{V_SEED8}",
        f"{V_BLOCKSIZE2}",
    )

    emit_decryption_layer(
        f"{V_SEED1}",
        f"{V_SEED2}",
        f"{V_SEED3}",
        f"{V_SEED4}",
        f"{V_BLOCKSIZE}",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PLAINTEXT INTEGRITY
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local {V_H1}=0x1357"
    )

    lines.append(
        f"local {V_H2}=0x2468"
    )

    lines.append(
        f"local {V_H3}=0x369C"
    )

    lines.append(
        f"local {V_H4}=0x4ACE"
    )

    lines.append(
        f"for {V_I}=1,{V_LEN}({V_SOURCE}) do"
    )

    lines.append(
        f"local {V_VALUE}="
        f"{V_BYTE}({V_SOURCE},{V_I})"
    )

    lines.append(
        f"{V_H1}=("
        f"{V_H1}*257+"
        f"{V_VALUE}+"
        f"{V_I}"
        f")%65536"
    )

    lines.append(
        f"{V_H2}=("
        f"{V_H2}*263+"
        f"{V_VALUE}*3+"
        f"{V_I}*7"
        f")%65536"
    )

    lines.append(
        f"{V_H3}=("
        f"{V_H3}*269+"
        f"{V_VALUE}*5+"
        f"{V_I}*11"
        f")%65536"
    )

    lines.append(
        f"{V_H4}=("
        f"{V_H4}*271+"
        f"{V_VALUE}*7+"
        f"{V_I}*17"
        f")%65536"
    )

    lines.append("end")

    for variable, value in (
        (V_EXPECT1, expected_h1),
        (V_EXPECT2, expected_h2),
        (V_EXPECT3, expected_h3),
        (V_EXPECT4, expected_h4),
    ):
        lines.append(
            f"local {variable}="
            f"{_num_expr(value)}"
        )

    lines.append(
        f"if {V_H1}~={V_EXPECT1} or "
        f"{V_H2}~={V_EXPECT2} or "
        f"{V_H3}~={V_EXPECT3} or "
        f"{V_H4}~={V_EXPECT4} then "
        f"error('Internal Error') "
        f"end"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # FINAL LENGTH
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"if {V_LEN}({V_SOURCE})~="
        f"{_num_expr(len(src))} then "
        f"error('Internal Error') "
        f"end"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # FINAL LOAD
    # ═══════════════════════════════════════════════════════════════════════

    lines.append(
        f"local {V_FN},{V_ERR}="
        f"{V_LOAD}({V_SOURCE})"
    )

    lines.append(
        f"if not {V_FN} then "
        f"error('Internal Error: '..tostring({V_ERR})) "
        f"end"
    )

    lines.append(
        f"return {V_FN}(...)"
    )

    lines.append(
        "end)(...)"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # COMPACT OUTPUT
    # ═══════════════════════════════════════════════════════════════════════

    header = lines[0]

    # TWO EMPTY LINES ARE INTENTIONAL.

    opener = lines[3]

    body = " ".join(
        line.strip()
        for line in lines[4:]
        if line.strip()
    )

    payload = (
        header
        + "\n\n"
        + opener
        + body
    )

    if publish:
        # The raw backend receives the complete executable payload. The Lua
        # file itself must contain that payload, never the user-facing loader.
        # The loader is exposed separately by obfuscate_lua_bundle().
        _raw_backend_publish(payload)

    return payload


# ═════════════════════════════════════════════════════════════════════════════
# SAFE PUBLIC API
# ═════════════════════════════════════════════════════════════════════════════

def obfuscate_lua_bundle(source, publish=True, level="hard"):
    """
    Return the three separate pieces the Discord/UI layer needs.

    lua_file
        The actual .lua file. It contains:
          line 1: header
          line 2: blank
          line 3: return:function(...) payload

    pc_copy / mobile_copy
        Plain text containing ONLY the loadstring. These are intentionally
        outside the Lua file so the UI can provide independent PC/mobile
        copy controls. Both values are identical by design.
    """
    if source is None:
        raise ValueError(
            "No Lua source was supplied."
        )

    if not isinstance(source, str):
        source = str(source)

    if not source.strip():
        raise ValueError(
            "Lua source is empty."
        )

    src = source.encode("utf-8")

    # Build the protected Lua payload without publishing it twice.
    level = normalize_obf_level(level)
    lua_file = obfuscate_lua(source, publish=False, level=level)

    if publish:
        raw_url, _loader_id = _publish_local_payload(lua_file)
        loader_text = _build_loadstring(raw_url)
    else:
        # A deterministic local placeholder is useful for tests. No fake
        # loader is emitted to the Lua file.
        loader_text = ""

    # User-facing output for the Discord/UI layer.
    # IMPORTANT: this does NOT change the actual Lua file.
    display_text = (
        "Loadstring Copy:\n"
        + loader_text
    )

    return {
        "lua_file": lua_file,
        "loadstring": loader_text,
        "pc_copy": loader_text,
        "mobile_copy": loader_text,
        "display_text": display_text,
    }


def obfuscate_lua_safe(source, publish=True):
    """Legacy-safe API: returns ONLY the Lua file text."""
    return obfuscate_lua(source, publish=publish)




# -----------------------------
# PUBLIC OBFUSCATOR WEBSITE + API
# -----------------------------

OBF_RATE_LIMIT = 8
OBF_RATE_WINDOW = 60.0
OBF_MAX_SOURCE = 2 * 1024 * 1024

OBF_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dex Obfuscator</title>
<style>
:root{color-scheme:dark;--bg:#070b14;--card:#0d1424;--line:#1d2940;--text:#edf3ff;--muted:#91a0bb;--accent:#7c5cff;--accent2:#22d3ee;--good:#35d07f}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0%,#18234a 0,#070b14 42%);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--text);min-height:100vh}.wrap{width:min(980px,92%);margin:0 auto;padding:42px 0 70px}.hero{text-align:center;margin-bottom:28px}.badge{display:inline-flex;padding:7px 12px;border:1px solid #293654;border-radius:999px;color:#b8c6e2;background:#0c1323;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}.hero h1{font-size:clamp(32px,7vw,56px);margin:14px 0 8px;letter-spacing:-.04em}.hero p{margin:0 auto;color:var(--muted);max-width:650px;line-height:1.6}.card{background:rgba(13,20,36,.9);border:1px solid var(--line);border-radius:22px;padding:22px;box-shadow:0 20px 70px rgba(0,0,0,.3);backdrop-filter:blur(12px)}label{display:block;font-weight:800;margin-bottom:10px}.editor{width:100%;min-height:330px;resize:vertical;border:1px solid #263552;border-radius:16px;background:#070c16;color:#dce7ff;padding:16px;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;outline:none}.editor:focus{border-color:var(--accent)}.levels{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:16px 0}.level{position:relative}.level input{position:absolute;opacity:0}.level label{margin:0;padding:14px;border:1px solid #263552;border-radius:14px;cursor:pointer;background:#0a1120}.level input:checked+label{border-color:var(--accent);background:#17142d;box-shadow:0 0 0 1px var(--accent) inset}.level small{display:block;color:var(--muted);font-weight:500;margin-top:4px}.actions{display:flex;gap:10px;align-items:center}.btn{border:0;border-radius:14px;padding:14px 20px;font-weight:900;cursor:pointer;background:linear-gradient(135deg,var(--accent),#5c8dff);color:white;flex:1}.btn:disabled{opacity:.55;cursor:not-allowed}.status{color:var(--muted);font-size:13px}.results{display:none;margin-top:18px;gap:14px}.resultbox{border:1px solid var(--line);border-radius:16px;background:#080e1a;padding:14px}.resultbox h3{margin:0 0 9px;font-size:14px}.copyrow{display:flex;gap:8px}.out{flex:1;min-width:0;border:1px solid #263552;border-radius:10px;padding:11px;background:#050a12;color:#cfe0ff;font:13px/1.45 ui-monospace,monospace;word-break:break-all}.copy{border:1px solid #344563;background:#111a2d;color:#fff;border-radius:10px;padding:0 14px;font-weight:800;cursor:pointer}.hint{color:var(--muted);font-size:12px;margin-top:12px}.error{color:#ff8d9b}.ok{color:var(--good)}@media(max-width:640px){.wrap{padding-top:24px}.card{padding:15px;border-radius:18px}.editor{min-height:270px}.levels{grid-template-columns:1fr}.actions{flex-direction:column;align-items:stretch}.btn{width:100%}.copyrow{flex-direction:column}.copy{padding:12px}.hero h1{font-size:38px}}
</style></head>
<body><main class="wrap"><section class="hero"><span class="badge">DexNotifier <span>Obfustucate</span><h1>Protect your Lua</h1><p>Paste your raw Lua, choose a protection level, and get a ready-to-copy raw loadstring plus the protected payload.</p></section>
<section class="card"><label for="source">Raw Lua source</label><textarea id="source" class="editor" spellcheck="false" placeholder="-- paste your Lua code here"></textarea>
<div class="levels">
<div class="level"><input id="light" name="level" type="radio" value="light"><label for="light">Lightly<small>Fastest • smaller output</small></label></div>
<div class="level"><input id="medium" name="level" type="radio" value="medium" checked><label for="medium">Medium<small>Balanced protection</small></label></div>
<div class="level"><input id="hard" name="level" type="radio" value="hard"><label for="hard">Hard<small>Maximum built-in noise</small></label></div>
</div><div class="actions"><button class="btn" id="go">Obfustucate Lua</button><span class="status" id="status">Ready</span></div>
<div class="results" id="results"><div class="resultbox"><h3>Raw loadstring</h3><div class="copyrow"><div class="out" id="loadstring"></div><button class="copy" data-copy="loadstring">Copy</button></div></div><div class="resultbox"><h3>Protected payload</h3><div class="copyrow"><div class="out" id="payload"></div><button class="copy" data-copy="payload">Copy</button></div><div class="hint">The downloadable payload contains the protected Lua itself. The loadstring only points at the raw endpoint.</div></div></div></section></main>
<script>
const $=id=>document.getElementById(id);const status=$('status');
$('go').onclick=async()=>{const source=$('source').value;const level=document.querySelector('input[name=level]:checked').value;if(!source.trim()){status.textContent='Paste Lua source first';status.className='status error';return} $('go').disabled=true;status.textContent='Obfustucating…';status.className='status';try{const r=await fetch('/obfustucate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source,level})});const d=await r.json();if(!r.ok)throw new Error(d.error||'Obfuscation failed');$('loadstring').textContent=d.loadstring;$('payload').textContent=d.payload;$('results').style.display='grid';status.textContent='Done';status.className='status ok'}catch(e){status.textContent=e.message;status.className='status error'}finally{$('go').disabled=false}};
document.querySelectorAll('.copy').forEach(b=>b.onclick=async()=>{const text=$(b.dataset.copy).textContent;await navigator.clipboard.writeText(text);const old=b.textContent;b.textContent='Copied';setTimeout(()=>b.textContent=old,900)});
</script></body></html>'''

@app.get("/obfustucate")
async def obfuscate_page():
    return HTMLResponse(OBF_PAGE)

@app.post("/obfustucate")
async def obfuscate_api(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "obfustucate", OBF_RATE_LIMIT, OBF_RATE_WINDOW):
        return JSONResponse({"error": "Too many obfuscation requests. Try again shortly."}, status_code=429)
    if reject_if_oversized(request, OBF_MAX_SOURCE + 32 * 1024):
        return JSONResponse({"error": "Source is too large."}, status_code=413)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Expected JSON with source and level."}, status_code=400)
    source = body.get("source", "") if isinstance(body, dict) else ""
    level = body.get("level", "medium") if isinstance(body, dict) else "medium"
    if not isinstance(source, str) or not source.strip():
        return JSONResponse({"error": "Lua source is empty."}, status_code=400)
    if len(source.encode("utf-8")) > OBF_MAX_SOURCE:
        return JSONResponse({"error": "Source is too large (2 MB maximum)."}, status_code=413)
    try:
        level = normalize_obf_level(level)
        bundle = obfuscate_lua_bundle(source, publish=True, level=level)
        return JSONResponse({"ok": True, "level": level, "loadstring": bundle["loadstring"], "payload": bundle["lua_file"]})
    except Exception as exc:
        print(f"[OBF] failed: {exc}")
        return JSONResponse({"error": "Obfuscation failed. Check that the source is valid Lua."}, status_code=400)


# -----------------------------
# PRIVATE SERVICE ENDPOINTS
# Not linked from public pages. Every route requires DEX_API_KEY.
# -----------------------------

PRIVATE_STATS_RATE_LIMIT = 20
PRIVATE_STATS_RATE_WINDOW = 30.0

def _private_key_ok(request: Request) -> bool:
    return is_valid_key(request.headers.get("X-Api-Key", ""))

@app.get("/info")
async def private_info(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "private_info", PRIVATE_STATS_RATE_LIMIT, PRIVATE_STATS_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    if not _private_key_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with scripts_lock:
        script_count = len(scripts)
    async with logs_lock:
        log_count = len(stored_logs)
    async with ws_count_lock:
        ws_connections = sum(ws_ip_connection_counts.values())
    return JSONResponse({"ok":True,"service":"DexNotifier","service_version":"4.0","timestamp":int(time.time()),"scripts":script_count,"stored_logs":log_count,"websocket_connections":ws_connections,"base_url":BASE_URL})

@app.get("/metrics")
async def private_metrics(request: Request):
    if not _private_key_ok(request):
        return PlainTextResponse("UNAUTHORIZED", status_code=401)
    async with logs_lock:
        log_count = len(stored_logs)
    async with viewers_lock:
        viewer_count = len(viewers)
    return JSONResponse({"ok":True,"viewers":viewer_count,"stored_logs":log_count,"uptime_seconds":int(time.time()-START_TIME) if 'START_TIME' in globals() else None})


# -----------------------------
# DYNAMIC LOADER ENDPOINTS - rate-limited, with a separate failed-attempt
# lockout on wrong paid keys / HWID mismatches per IP.
# -----------------------------

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

# -----------------------------
# RUN
# -----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
