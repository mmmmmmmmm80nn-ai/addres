"""Binance integration core (no Telegram code in here, so it is easy to test).

What lives here
---------------
* Vault      : API key/secret are stored encrypted (Fernet), never in plain text.
* Client     : HMAC-signed Binance REST client with time-sync + host fallback.
* Storage    : SQLite tables (created lazily) for account, recipients, pending
               confirmations, ledger, seen-events, invoices and invoice runs.
* Parsing    : Arabic/English command parsing (Arabic digits supported).
* Safety     : limits, new-recipient cool-down, PIN, 2-step pending confirmations.
* Invoices   : scheduled / recurring invoices + automatic payment matching.
* Watcher    : poll deposits / Binance Pay / withdrawals and return new events.

Hard rules baked in
-------------------
* Nothing moves money without a saved recipient AND an explicit confirmation.
* Natural-language ("smart") parsing only ever produces a DRAFT.
* A withdrawal whose outcome is unknown (network error after sending) is never
  retried automatically.
"""

import calendar
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken

import binance_proxy as _bproxy
import config
import database as _db

TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Africa/Cairo"))

STABLES = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}
NEW_RECIPIENT_COOLDOWN = int(os.environ.get("BINANCE_NEW_RECIPIENT_COOLDOWN", "600"))
PENDING_TTL_DEFAULT = 120
PIN_MAX_FAILS = 3
PIN_LOCK_SECONDS = 600


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫،", "01234567890123456789.,")


def to_ascii_digits(s):
    return (s or "").translate(_AR_DIGITS)


def norm_ar(s):
    """Normalise Arabic for matching: digits, hamza forms, ya/alef-maqsura, ta-marbuta, tatweel."""
    s = to_ascii_digits(s or "")
    s = s.replace("ـ", "")
    s = re.sub("[\u064b-\u065f\u0670]", "", s)  # tashkeel
    s = s.translate(str.maketrans("أإآٱىة", "اااايه"))
    return re.sub(r"\s+", " ", s).strip().lower()


def dec(value, default=None):
    try:
        d = Decimal(str(value).replace(",", "").strip())
        if not d.is_finite():
            return default
        return d
    except (InvalidOperation, ValueError, TypeError):
        return default


def fmt_dec(d, places=8):
    d = Decimal(d).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def mask_addr(addr):
    addr = addr or ""
    return addr if len(addr) <= 12 else f"{addr[:6]}…{addr[-5:]}"


def now_ts():
    return int(time.time())


def fmt_ts(ts):
    return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M")


def fmt_ts_full(ts):
    """DD-MM-YYYY HH:MM:SS (the format used in deposit notifications)."""
    return datetime.fromtimestamp(int(ts), TZ).strftime("%d-%m-%Y %H:%M:%S")


# ---------------------------------------------------------------------------
# Vault (encrypts API credentials)
# ---------------------------------------------------------------------------

_fernet = None
_fernet_lock = threading.Lock()


def _load_key():
    env = os.environ.get("BINANCE_VAULT_KEY", "").strip()
    if env:
        return env.encode()
    path = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), ".binance_vault.key")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _vault():
    global _fernet
    with _fernet_lock:
        if _fernet is None:
            _fernet = Fernet(_load_key())
        return _fernet


def encrypt(text):
    return _vault().encrypt(text.encode()).decode()


def decrypt(token):
    try:
        return _vault().decrypt(token.encode()).decode()
    except InvalidToken:
        raise RuntimeError("مفتاح التشفير اتغيّر أو الملف .binance_vault.key ضاع - لازم تربط حسابك من جديد.")


# ---------------------------------------------------------------------------
# Binance REST client
# ---------------------------------------------------------------------------

class BinanceError(Exception):
    def __init__(self, code, msg, status=None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.status = status


class BinanceNetworkError(Exception):
    """Request may or may not have reached Binance."""


def sign(secret, query):
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceClient:
    BASES = [
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
        "https://api4.binance.com",
    ]

    def __init__(self, key, secret, timeout=15):
        _custom = os.environ.get("BINANCE_BASE_URL", "").strip().rstrip("/")
        if _custom:
            self.BASES = [_custom]
        self.key = key
        self.secret = secret
        self.timeout = timeout
        self.offset_ms = 0
        self.session = requests.Session()

    # -- low level ---------------------------------------------------------
    def _send(self, method, path, params, signed, _retry_time=True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"X-MBX-APIKEY": self.key}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.offset_ms
            params.setdefault("recvWindow", 10000)
            q = urlencode(params)
            q += "&signature=" + sign(self.secret, q)
        else:
            q = urlencode(params)
        px = _bproxy.current()
        geo_msg = ""
        for _attempt in range(4):
            proxies = {"http": px, "https": px} if px else None
            blocked = False
            last_net = None
            for base in self.BASES:
                url = base + path
                try:
                    if method == "GET":
                        r = self.session.get(url + ("?" + q if q else ""), headers=headers,
                                             timeout=self.timeout, proxies=proxies)
                    else:
                        h = dict(headers, **{"Content-Type": "application/x-www-form-urlencoded"})
                        r = self.session.request(method, url, data=q, headers=h,
                                                 timeout=self.timeout, proxies=proxies)
                except requests.RequestException as exc:
                    last_net = exc
                    if method != "GET":
                        # a POST may have been received: never replay it
                        raise BinanceNetworkError(str(exc))
                    continue
                try:
                    data = r.json()
                except ValueError:
                    data = None
                txt = str((data.get("msg") if isinstance(data, dict) else r.text[:300]) or "")
                if r.status_code == 451 or "restricted location" in txt.lower():
                    # blocked before reaching Binance (geo/IP) -> safe to retry via another proxy
                    blocked = True
                    geo_msg = txt[:200]
                    break
                if r.status_code >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0):
                    code = data.get("code") if isinstance(data, dict) else r.status_code
                    msg = data.get("msg") if isinstance(data, dict) else r.text[:200]
                    if code == -1021 and signed and _retry_time:
                        self.sync_time()
                        return self._send(method, path, {k: v for k, v in params.items() if k not in ("timestamp", "signature")}, signed, False)
                    raise BinanceError(code, msg, r.status_code)
                return data
            if blocked or (px and last_net is not None):
                if px:
                    _bproxy.report_bad(px)
                px = _bproxy.find()
                if px is None:
                    if blocked:
                        raise BinanceError(451, geo_msg, 451)
                    raise BinanceNetworkError(str(last_net))
                continue
            raise BinanceNetworkError(str(last_net))
        raise BinanceError(451, geo_msg or "no working proxy", 451)

    def sync_time(self):
        data = self._send("GET", "/api/v3/time", {}, False)
        self.offset_ms = int(data["serverTime"]) - int(time.time() * 1000)

    # -- endpoints ---------------------------------------------------------
    def api_restrictions(self):
        return self._send("GET", "/sapi/v1/account/apiRestrictions", {}, True)

    def spot_balances(self):
        data = self._send("GET", "/api/v3/account", {"omitZeroBalances": "true"}, True)
        return {b["asset"]: dec(b["free"], Decimal(0)) for b in data.get("balances", []) if dec(b["free"], Decimal(0)) > 0}

    def funding_balances(self):
        data = self._send("POST", "/sapi/v1/asset/get-funding-asset", {}, True)
        return {b["asset"]: dec(b["free"], Decimal(0)) for b in (data or []) if dec(b["free"], Decimal(0)) > 0}

    def deposits(self, start_ms, limit=200):
        return self._send("GET", "/sapi/v1/capital/deposit/hisrec", {"startTime": start_ms, "limit": limit}, True) or []

    def withdrawals(self, start_ms, limit=200):
        return self._send("GET", "/sapi/v1/capital/withdraw/history", {"startTime": start_ms, "limit": limit}, True) or []

    def pay_transactions(self, start_ms, limit=100):
        data = self._send("GET", "/sapi/v1/pay/transactions", {"startTime": start_ms, "limit": limit}, True)
        return (data or {}).get("data") or []

    def deposit_address(self, coin, network=None):
        return self._send("GET", "/sapi/v1/capital/deposit/address", {"coin": coin, "network": network}, True)

    def all_coins(self):
        return self._send("GET", "/sapi/v1/capital/config/getall", {}, True) or []

    def coin_info(self, coin):
        for c in self._send("GET", "/sapi/v1/capital/config/getall", {}, True) or []:
            if c.get("coin") == coin:
                return c
        return None

    def withdraw(self, coin, network, address, amount, tag=None, wallet_type=1, order_id=None):
        return self._send("POST", "/sapi/v1/capital/withdraw/apply", {
            "coin": coin, "network": network, "address": address, "addressTag": tag or None,
            "amount": fmt_dec(amount), "walletType": wallet_type, "withdrawOrderId": order_id,
        }, True)

    def price_usdt(self, coin):
        if coin in STABLES:
            return Decimal(1)
        data = self._send("GET", "/api/v3/ticker/price", {"symbol": f"{coin}USDT"}, False)
        return dec(data.get("price"))


def client_for(owner_id):
    acc = get_account(owner_id)
    if not acc:
        return None
    return BinanceClient(decrypt(acc["api_key_enc"]), decrypt(acc["api_secret_enc"]))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bn_accounts (
    owner_id INTEGER PRIMARY KEY,
    api_key_enc TEXT NOT NULL,
    api_secret_enc TEXT NOT NULL,
    can_read INTEGER DEFAULT 1,
    can_withdraw INTEGER DEFAULT 0,
    ip_restricted INTEGER DEFAULT 0,
    linked_at INTEGER,
    notify_on INTEGER DEFAULT 1,
    transfers_on INTEGER DEFAULT 0,
    wallet_type INTEGER DEFAULT 1,
    per_tx_limit TEXT DEFAULT '100',
    daily_limit TEXT DEFAULT '300',
    pin_hash TEXT,
    pin_salt TEXT,
    pin_fails INTEGER DEFAULT 0,
    pin_lock_until INTEGER DEFAULT 0,
    receive_text TEXT DEFAULT '',
    auto_remind INTEGER DEFAULT 1,
    auto_receipt INTEGER DEFAULT 1,
    smart_on INTEGER DEFAULT 1,
    baseline_done INTEGER DEFAULT 0,
    strict_verify INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bn_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    coin TEXT NOT NULL,
    network TEXT NOT NULL,
    address TEXT NOT NULL,
    tag TEXT,
    created_at INTEGER,
    UNIQUE (owner_id, name_norm)
);
CREATE TABLE IF NOT EXISTS bn_pending (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER,
    expires_at INTEGER,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS bn_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    ts INTEGER,
    kind TEXT,
    coin TEXT,
    amount TEXT,
    usd TEXT,
    recipient TEXT,
    status TEXT,
    ref TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_bn_ledger_owner ON bn_ledger (owner_id, ts);
CREATE TABLE IF NOT EXISTS bn_seen (
    owner_id INTEGER, source TEXT, ref TEXT,
    PRIMARY KEY (owner_id, source, ref)
);
CREATE TABLE IF NOT EXISTS bn_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    bc_id TEXT,
    chat_id INTEGER,
    client_name TEXT,
    title TEXT,
    amount TEXT,
    coin TEXT,
    repeat TEXT DEFAULT 'once',
    due_days INTEGER DEFAULT 3,
    next_run INTEGER,
    status TEXT DEFAULT 'active',
    created_at INTEGER,
    runs_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bn_invoice_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    issued_at INTEGER,
    due_at INTEGER,
    amount TEXT,
    coin TEXT,
    status TEXT DEFAULT 'sent',
    paid_ref TEXT,
    paid_at INTEGER,
    reminders INTEGER DEFAULT 0,
    last_reminder_at INTEGER DEFAULT 0
);
"""


def init_tables():
    with _db._lock:
        conn = _db._connect()
        conn.executescript(_SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bn_accounts)").fetchall()}
        if "strict_verify" not in cols:  # migration for databases created before this setting existed
            conn.execute("ALTER TABLE bn_accounts ADD COLUMN strict_verify INTEGER DEFAULT 1")
        conn.commit()
        conn.close()


def _q(sql, args=(), one=False, many=False, commit=False):
    with _db._lock:
        conn = _db._connect()
        try:
            cur = conn.execute(sql, args)
            if commit:
                conn.commit()
                return cur.rowcount if not one else None
            if one:
                row = cur.fetchone()
                return dict(row) if row else None
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def _insert(sql, args=()):
    with _db._lock:
        conn = _db._connect()
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


# -- account -----------------------------------------------------------------

def get_account(owner_id):
    return _q("SELECT * FROM bn_accounts WHERE owner_id=?", (owner_id,), one=True)


def save_account(owner_id, api_key, api_secret, can_withdraw, ip_restricted, can_read=True):
    _q("DELETE FROM bn_seen WHERE owner_id=?", (owner_id,), commit=True)
    _q(
        "INSERT INTO bn_accounts (owner_id, api_key_enc, api_secret_enc, can_read, can_withdraw, ip_restricted, linked_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
        "api_key_enc=excluded.api_key_enc, api_secret_enc=excluded.api_secret_enc, can_read=excluded.can_read, "
        "can_withdraw=excluded.can_withdraw, ip_restricted=excluded.ip_restricted, linked_at=excluded.linked_at, "
        "baseline_done=0, transfers_on=0",
        (owner_id, encrypt(api_key), encrypt(api_secret), int(can_read), int(can_withdraw), int(ip_restricted), now_ts()),
        commit=True,
    )


_ACCOUNT_FIELDS = {
    "notify_on", "transfers_on", "wallet_type", "per_tx_limit", "daily_limit", "receive_text",
    "auto_remind", "auto_receipt", "smart_on", "baseline_done", "pin_fails", "pin_lock_until",
    "can_withdraw", "ip_restricted", "strict_verify",
}


def update_account(owner_id, **fields):
    bad = set(fields) - _ACCOUNT_FIELDS
    if bad:
        raise ValueError(f"bad fields: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    _q(f"UPDATE bn_accounts SET {sets} WHERE owner_id=?", (*fields.values(), owner_id), commit=True)


def delete_account(owner_id):
    _q("DELETE FROM bn_accounts WHERE owner_id=?", (owner_id,), commit=True)
    _q("DELETE FROM bn_pending WHERE owner_id=?", (owner_id,), commit=True)
    _q("DELETE FROM bn_seen WHERE owner_id=?", (owner_id,), commit=True)
    _q("UPDATE bn_invoices SET status='paused' WHERE owner_id=? AND status='active'", (owner_id,), commit=True)


def linked_owner_ids():
    return [r["owner_id"] for r in _q("SELECT owner_id FROM bn_accounts")]


# -- PIN -----------------------------------------------------------------------

def _pin_hash(pin, salt):
    return hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def set_pin(owner_id, pin):
    salt = secrets.token_hex(16)
    _q("UPDATE bn_accounts SET pin_hash=?, pin_salt=?, pin_fails=0, pin_lock_until=0 WHERE owner_id=?",
       (_pin_hash(pin, salt), salt, owner_id), commit=True)


def clear_pin(owner_id):
    _q("UPDATE bn_accounts SET pin_hash=NULL, pin_salt=NULL, pin_fails=0 WHERE owner_id=?", (owner_id,), commit=True)


def check_pin(owner_id, pin):
    """Returns (ok, message). Locks after PIN_MAX_FAILS wrong tries."""
    acc = get_account(owner_id)
    if not acc or not acc["pin_hash"]:
        return True, ""
    if acc["pin_lock_until"] > now_ts():
        return False, f"الـ PIN مقفول لحد {fmt_ts(acc['pin_lock_until'])}"
    if hmac.compare_digest(_pin_hash(pin, acc["pin_salt"]), acc["pin_hash"]):
        update_account(owner_id, pin_fails=0)
        return True, ""
    fails = acc["pin_fails"] + 1
    if fails >= PIN_MAX_FAILS:
        update_account(owner_id, pin_fails=0, pin_lock_until=now_ts() + PIN_LOCK_SECONDS)
        return False, "PIN غلط 3 مرات - اتقفلت العمليات 10 دقايق."
    update_account(owner_id, pin_fails=fails)
    return False, f"PIN غلط ({PIN_MAX_FAILS - fails} محاولات باقية)."


# -- recipients ----------------------------------------------------------------

def add_recipient(owner_id, name, coin, network, address, tag=None):
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise ValueError("الاسم لازم يكون من 1 لـ 40 حرف")
    _q(
        "INSERT INTO bn_recipients (owner_id, name, name_norm, coin, network, address, tag, created_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(owner_id, name_norm) DO UPDATE SET "
        "coin=excluded.coin, network=excluded.network, address=excluded.address, tag=excluded.tag, created_at=excluded.created_at",
        (owner_id, name, norm_ar(name), coin.upper(), network.upper(), address.strip(), (tag or "").strip() or None, now_ts()),
        commit=True,
    )


def list_recipients(owner_id):
    return _q("SELECT * FROM bn_recipients WHERE owner_id=? ORDER BY name", (owner_id,))


def get_recipient(owner_id, rid):
    return _q("SELECT * FROM bn_recipients WHERE owner_id=? AND id=?", (owner_id, rid), one=True)


def delete_recipient(owner_id, rid):
    _q("DELETE FROM bn_recipients WHERE owner_id=? AND id=?", (owner_id, rid), commit=True)


def get_recipient_by_exact_name(owner_id, name):
    """Exact normalised-name match only (no prefix fuzziness) — used to detect a name collision."""
    n = norm_ar(name)
    return _q("SELECT * FROM bn_recipients WHERE owner_id=? AND name_norm=?", (owner_id, n), one=True)


def recipient_count(owner_id):
    row = _q("SELECT COUNT(*) AS n FROM bn_recipients WHERE owner_id=?", (owner_id,), one=True)
    return row["n"] if row else 0


MAX_RECIPIENTS = int(os.environ.get("BINANCE_MAX_RECIPIENTS", "40"))


def find_recipient(owner_id, name):
    """Exact (normalised) match first, then a unique prefix match. Handles Arabic 'ل' prefix."""
    cands = [norm_ar(name)]
    n = cands[0]
    if n.startswith("ل") and len(n) > 2:
        cands.append(n[1:].lstrip())
    rows = list_recipients(owner_id)
    for c in cands:
        for r in rows:
            if r["name_norm"] == c:
                return r
    for c in cands:
        hits = [r for r in rows if r["name_norm"].startswith(c)] if len(c) >= 2 else []
        if len(hits) == 1:
            return hits[0]
    return None


# -- ledger / seen -----------------------------------------------------------------

def ledger_add(owner_id, kind, coin, amount, usd, recipient, status, ref="", note=""):
    return _insert(
        "INSERT INTO bn_ledger (owner_id, ts, kind, coin, amount, usd, recipient, status, ref, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (owner_id, now_ts(), kind, coin, str(amount), str(usd) if usd is not None else None, recipient, status, ref, note),
    )


def ledger_recent(owner_id, limit=10):
    return _q("SELECT * FROM bn_ledger WHERE owner_id=? ORDER BY id DESC LIMIT ?", (owner_id, limit))


def sent_usd_last_24h(owner_id):
    rows = _q("SELECT usd FROM bn_ledger WHERE owner_id=? AND kind='transfer' AND status IN ('sent','unknown') AND ts>=?",
              (owner_id, now_ts() - 86400))
    return sum((dec(r["usd"], Decimal(0)) for r in rows), Decimal(0))


def recent_sent_usd(owner_id, n=10):
    rows = _q("SELECT usd FROM bn_ledger WHERE owner_id=? AND kind='transfer' AND status='sent' ORDER BY id DESC LIMIT ?",
              (owner_id, n))
    return [dec(r["usd"], Decimal(0)) for r in rows]


def mark_seen(owner_id, source, ref):
    """Returns True if this is the first time we see it."""
    with _db._lock:
        conn = _db._connect()
        try:
            cur = conn.execute("INSERT OR IGNORE INTO bn_seen (owner_id, source, ref) VALUES (?,?,?)", (owner_id, source, str(ref)))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)(?!\d)"
_TRANSFER_VERBS_BUSINESS = ("حول", "حوّل", "حوِّل")
_TRANSFER_VERBS_PRIVATE = _TRANSFER_VERBS_BUSINESS + ("ارسل", "أرسل", "ابعت", "ابعث")


def parse_transfer(text, private=False):
    """'حول 55 USDT لأحمد' -> {'amount': Decimal, 'coin': 'USDT'|None, 'name': 'أحمد'} ; None if not a transfer."""
    t = to_ascii_digits((text or "").strip())
    verbs = _TRANSFER_VERBS_PRIVATE if private else _TRANSFER_VERBS_BUSINESS
    m = re.match(r"^(?:%s)\s+%s\s*(?:([A-Za-z]{2,10})\s+)?(.+)$" % ("|".join(verbs), _NUM), t)
    if not m:
        return None
    amount = dec(m.group(1))
    if amount is None or amount <= 0:
        return None
    return {"amount": amount, "coin": (m.group(2) or "").upper() or None, "name": m.group(3).strip()}


_BN_WORDS = r"(?:بينانس|باينانس|بايننس|بينانص|binance)"


def parse_binance_transfer(text):
    """Explicit Binance command, so it never clashes with the currency converter ('حول 55'):

        'حول 55 بينانس'            -> {'amount', 'coin': None,   'name': None}   (bot asks who)
        'حول 55 usdt بينانس'       -> {'amount', 'coin': 'USDT', 'name': None}
        'حول 55 بينانس لأحمد'      -> {'amount', 'coin': None,   'name': 'لأحمد'}
        'حول 55 usdt بينانس لأحمد' -> {'amount', 'coin': 'USDT', 'name': 'لأحمد'}
    """
    t = norm_ar(text)
    verbs = "|".join(norm_ar(v) for v in _TRANSFER_VERBS_PRIVATE)
    m = re.match(r"^(?:%s)\s+%s\s*(?:([a-z]{2,10})\s+)?%s(?:\s+(.+))?$" % (verbs, _NUM, _BN_WORDS), t)
    if not m:
        return None
    amount = dec(m.group(1))
    if amount is None or amount <= 0:
        return None
    # keep the original spelling of the recipient name
    name = (m.group(3) or "").strip()
    if name:
        om = re.search(r"%s\s+(.+)$" % _BN_WORDS, to_ascii_digits(text.strip()), re.I)
        name = (om.group(1) if om else name).strip()
    return {"amount": amount, "coin": (m.group(2) or "").upper() or None, "name": name or None}


def parse_transfer_no_name(text):
    """'حول 55' alone (private chat) -> amount, so the bot can ask for the recipient."""
    t = to_ascii_digits((text or "").strip())
    m = re.match(r"^(?:%s)\s+%s\s*([A-Za-z]{2,10})?$" % ("|".join(_TRANSFER_VERBS_PRIVATE), _NUM), t)
    if not m:
        return None
    amount = dec(m.group(1))
    if amount is None or amount <= 0:
        return None
    return {"amount": amount, "coin": (m.group(2) or "").upper() or None}


_REPEAT_WORDS = {"شهريا": "monthly", "شهري": "monthly", "اسبوعيا": "weekly", "اسبوعي": "weekly", "مره واحده": "once", "مره": "once"}


def parse_invoice(text):
    """'فاتورة 100 USDT شهريا عن اشتراك' -> dict or None."""
    t = norm_ar(text)
    m = re.match(r"^فاتوره\s+%s\s*(?:([a-z]{2,10})\s+)?(?:(شهريا|شهري|اسبوعيا|اسبوعي|مره واحده|مره)\s*)?(?:(?:عن|بخصوص)\s+(.+))?$" % _NUM, t)
    if not m:
        return None
    amount = dec(m.group(1))
    if amount is None or amount <= 0:
        return None
    # keep the original casing/spelling of the title
    title = None
    if m.group(4):
        om = re.search(r"(?:عن|بخصوص)\s+(.+)$", to_ascii_digits(text.strip()))
        title = (om.group(1) if om else m.group(4)).strip()[:80]
    return {
        "amount": amount,
        "coin": (m.group(2) or "").upper() or None,
        "repeat": _REPEAT_WORDS.get(m.group(3) or "", "once"),
        "title": title,
    }


_SIMPLE_CMDS = {
    "balance": {"رصيد", "الرصيد", "رصيدي", "رصيد بينانس", "balance"},
    "ledger": {"سجل", "السجل", "العمليات", "سجل العمليات"},
    "invoices": {"فواتير", "الفواتير", "فواتيري"},
    "summary": {"ملخص", "الملخص", "ملخص اليوم", "تقرير"},
}


def parse_simple(text):
    n = norm_ar(text)
    for intent, words in _SIMPLE_CMDS.items():
        if n in {norm_ar(w) for w in words}:
            return intent
    return None


def looks_financial(text):
    n = norm_ar(text)
    return bool(re.search(r"\d", n)) and bool(re.search(r"(حول|ابعت|ارسل|ادفع|فاتور|سحب|ورد|ارسال|usdt|دولار)", n))


# ---------------------------------------------------------------------------
# Transfers: checks, pending confirmation, execution
# ---------------------------------------------------------------------------

def usd_value(client, coin, amount):
    price = client.price_usdt(coin)
    if price is None:
        return None
    return (amount * price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def balance_of(client, coin, wallet_type):
    bal = client.funding_balances() if wallet_type == 1 else client.spot_balances()
    return bal.get(coin, Decimal(0))


def prepare_transfer(owner_id, client, recipient, amount, coin=None):
    """Validate everything and return (draft, None) or (None, error_message)."""
    acc = get_account(owner_id)
    if not acc:
        return None, "حساب بينانس مش مربوط."
    if not acc["transfers_on"]:
        return None, "التحويلات من البوت مقفولة. فعّلها من إعدادات بينانس الأول."
    if not acc["can_withdraw"]:
        return None, "الـ API Key مفيهوش صلاحية السحب (Enable Withdrawals)."
    if coin and coin.upper() != recipient["coin"]:
        return None, f"المستلم «{recipient['name']}» متسجل بعملة {recipient['coin']} مش {coin}."
    coin = recipient["coin"]
    if acc["pin_lock_until"] > now_ts():
        return None, f"العمليات مقفولة لحد {fmt_ts(acc['pin_lock_until'])} بسبب PIN غلط."
    strict = bool(acc["strict_verify"]) if "strict_verify" in acc.keys() else True
    if strict and not acc["pin_hash"]:
        return None, "التحقق الصارم شغال: لازم تفعّل PIN الأول من إعدادات بينانس عشان تقدر تحوّل."
    wait = recipient["created_at"] + NEW_RECIPIENT_COOLDOWN - now_ts()
    if wait > 0:
        return None, f"المستلم متضاف حديثًا - استنى {max(1, wait // 60)} دقيقة قبل أول تحويل (حماية)."
    if amount <= 0:
        return None, "المبلغ لازم يكون أكبر من صفر."

    usd = usd_value(client, coin, amount)
    if usd is None:
        return None, f"مقدرتش أجيب سعر {coin} عشان أتحقق من الحدود."
    per_tx = dec(acc["per_tx_limit"], Decimal(0))
    daily = dec(acc["daily_limit"], Decimal(0))
    if usd > per_tx:
        return None, f"المبلغ (~{usd}$) أكبر من حد العملية الواحدة ({fmt_dec(per_tx)}$)."
    used = sent_usd_last_24h(owner_id)
    if used + usd > daily:
        return None, f"هتعدّي الحد اليومي: اتحوّل {fmt_dec(used)}$ في آخر 24 ساعة من أصل {fmt_dec(daily)}$."

    info = None
    try:
        info = client.coin_info(coin)
    except BinanceError:
        info = None
    fee = minimum = None
    if info:
        net = next((n for n in info.get("networkList", []) if n.get("network") == recipient["network"]), None)
        if net is None:
            return None, f"الشبكة {recipient['network']} مش متاحة لعملة {coin} على بينانس."
        if not net.get("withdrawEnable", True):
            return None, f"السحب على شبكة {recipient['network']} متوقف حاليًا من بينانس."
        fee = dec(net.get("withdrawFee"))
        minimum = dec(net.get("withdrawMin"))
        if minimum and amount < minimum:
            return None, f"أقل مبلغ سحب على {recipient['network']} هو {fmt_dec(minimum)} {coin}."

    wallet = acc["wallet_type"]
    free = balance_of(client, coin, wallet)
    if free < amount:
        where = "المحفظة الرئيسية (Funding)" if wallet == 1 else "محفظة السبوت"
        return None, f"الرصيد مش كفاية في {where}: متاح {fmt_dec(free)} {coin}."

    flags = []
    hist = recent_sent_usd(owner_id)
    if len(hist) >= 3:
        s = sorted(hist)
        median = s[len(s) // 2]
        if median > 0 and usd > median * 3:
            flags.append("أكبر من المعتاد (أكتر من 3 أضعاف متوسط تحويلاتك).")
    return {
        "coin": coin, "network": recipient["network"], "address": recipient["address"], "tag": recipient["tag"],
        "recipient_id": recipient["id"], "recipient_name": recipient["name"],
        "amount": fmt_dec(amount), "usd": str(usd), "fee": fmt_dec(fee) if fee is not None else None,
        "wallet_type": wallet, "flags": flags, "need_pin": bool(acc["pin_hash"]),
        "strict": strict, "verified": False,
    }, None


def address_matches(expected, typed):
    """Compare the address the owner typed with the saved one (EVM hex addresses are case-insensitive)."""
    a, b = (expected or "").strip(), (typed or "").strip()
    if not a or not b:
        return False
    if a.lower().startswith("0x") and b.lower().startswith("0x"):
        a, b = a.lower(), b.lower()
    return hmac.compare_digest(a.encode(), b.encode())


def mark_verified(owner_id, pid):
    """Record that this pending transfer passed address/PIN verification (execute_transfer enforces it)."""
    row = get_pending(owner_id, pid)
    if not row or row["status"] != "pending":
        return False
    payload = row["payload"]
    payload["verified"] = True
    _q("UPDATE bn_pending SET payload=? WHERE id=? AND owner_id=? AND status='pending'",
       (json.dumps(payload, ensure_ascii=False, default=str), pid, owner_id), commit=True)
    return True


def create_pending(owner_id, kind, payload, ttl=PENDING_TTL_DEFAULT):
    pid = secrets.token_hex(6)
    _q("INSERT INTO bn_pending (id, owner_id, kind, payload, created_at, expires_at, status) VALUES (?,?,?,?,?,?,'pending')",
       (pid, owner_id, kind, json.dumps(payload, ensure_ascii=False, default=str), now_ts(), now_ts() + ttl), commit=True)
    return pid


def get_pending(owner_id, pid):
    row = _q("SELECT * FROM bn_pending WHERE id=? AND owner_id=?", (pid, owner_id), one=True)
    if row:
        row["payload"] = json.loads(row["payload"])
    return row


def claim_pending(owner_id, pid):
    """Atomically move pending -> running. Only ONE caller can win (prevents double-sends)."""
    n = _q("UPDATE bn_pending SET status='running' WHERE id=? AND owner_id=? AND status='pending' AND expires_at>=?",
           (pid, owner_id, now_ts()), commit=True)
    return n == 1


def finish_pending(pid, status):
    _q("UPDATE bn_pending SET status=? WHERE id=?", (status, pid), commit=True)


def cancel_pending(owner_id, pid):
    _q("UPDATE bn_pending SET status='cancelled' WHERE id=? AND owner_id=? AND status='pending'", (pid, owner_id), commit=True)


def execute_transfer(owner_id, pid):
    """Run a confirmed transfer. Returns (status, text). status: sent | failed | unknown | expired."""
    row = get_pending(owner_id, pid)
    if not row or row["kind"] != "transfer":
        return "expired", "الطلب مش موجود."
    _p = row["payload"]
    if (_p.get("strict") or _p.get("need_pin")) and not _p.get("verified"):
        return "failed", "التحويل محتاج تحقق (العنوان/PIN) قبل التنفيذ."
    if not claim_pending(owner_id, pid):
        return "expired", "الطلب ده اتنفذ قبل كده أو انتهت صلاحيته (من غير ما يتكرر)."
    p = row["payload"]
    try:
        client = client_for(owner_id)
        acc = get_account(owner_id)
        # re-validate at execution time (balance/limits may have changed since the draft)
        rec = get_recipient(owner_id, p["recipient_id"])
        if not rec or rec["address"] != p["address"] or rec["network"] != p["network"]:
            finish_pending(pid, "failed")
            return "failed", "بيانات المستلم اتغيّرت بعد الطلب - اتلغت العملية."
        draft, err = prepare_transfer(owner_id, client, rec, dec(p["amount"]), p["coin"])
        if err:
            finish_pending(pid, "failed")
            ledger_add(owner_id, "transfer", p["coin"], p["amount"], p["usd"], p["recipient_name"], "failed", pid, err)
            return "failed", err
        res = client.withdraw(p["coin"], p["network"], p["address"], dec(p["amount"]), tag=p["tag"],
                              wallet_type=acc["wallet_type"], order_id=pid)
        finish_pending(pid, "done")
        ledger_add(owner_id, "transfer", p["coin"], p["amount"], p["usd"], p["recipient_name"], "sent", str((res or {}).get("id", pid)))
        return "sent", str((res or {}).get("id", pid))
    except BinanceError as exc:
        finish_pending(pid, "failed")
        ledger_add(owner_id, "transfer", p["coin"], p["amount"], p["usd"], p["recipient_name"], "failed", pid, str(exc))
        return "failed", explain_error(exc)
    except BinanceNetworkError as exc:
        finish_pending(pid, "unknown")
        ledger_add(owner_id, "transfer", p["coin"], p["amount"], p["usd"], p["recipient_name"], "unknown", pid, str(exc))
        return "unknown", "فيه خطأ اتصال أثناء الإرسال ومش مؤكد التحويل حصل ولا لأ. راجع سجل السحب في بينانس قبل أي محاولة تانية."
    except Exception as exc:  # never leave a claimed pending hanging
        finish_pending(pid, "failed")
        ledger_add(owner_id, "transfer", p["coin"], p["amount"], p["usd"], p["recipient_name"], "failed", pid, repr(exc))
        return "failed", f"خطأ غير متوقع: {exc}"


_ERRORS = {
    451: "بينانس بيحظر الطلبات من موقع السيرفر ده (مشكلة في دولة/IP الاستضافة مش في الكود). الحل الدائم: انقل البوت لسيرفر في دولة مدعومة من بينانس، أو شغّل البوت عن طريق بروكسي ثابت في دولة مدعومة بمتغير BINANCE_PROXY (ولو كان متظبط وبرضه بيظهر الخطأ يبقى IP البروكسي نفسه محظور).",
    -2015: "المفتاح أو الصلاحيات أو الـ IP غير مقبول. تأكد إن IP السيرفر متضاف (IP whitelist) وإن الصلاحيات مفعّلة.",
    -2014: "صيغة الـ API Key غير صحيحة.",
    -1022: "الـ Secret غلط (التوقيع مرفوض).",
    -4026: "الرصيد مش كفاية.",
    -4028: "الشبكة دي مش مدعومة للعملة دي.",
    -4106: "الشبكة دي مبتدعمش Memo/Tag.",
    -4001: "العنوان غير صحيح.",
}


def explain_error(exc):
    if isinstance(exc, BinanceError):
        return _ERRORS.get(exc.code, f"بينانس رفض الطلب: {exc.msg} ({exc.code})")
    return str(exc)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

def add_months(ts, months=1):
    d = datetime.fromtimestamp(ts, TZ)
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return int(d.replace(year=y, month=m, day=day).timestamp())


def next_run_after(ts, repeat):
    if repeat == "weekly":
        return ts + 7 * 86400
    if repeat == "monthly":
        return add_months(ts, 1)
    return None


def create_invoice(owner_id, bc_id, chat_id, client_name, amount, coin, repeat, title, due_days=3, first_run=None):
    return _insert(
        "INSERT INTO bn_invoices (owner_id, bc_id, chat_id, client_name, title, amount, coin, repeat, due_days, next_run, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'active', ?)",
        (owner_id, bc_id, chat_id, client_name, title or "", fmt_dec(amount), coin.upper(), repeat, due_days,
         first_run or now_ts(), now_ts()),
    )


def list_invoices(owner_id, include_done=False):
    sql = "SELECT * FROM bn_invoices WHERE owner_id=?" + ("" if include_done else " AND status!='done'") + " ORDER BY id DESC LIMIT 30"
    return _q(sql, (owner_id,))


def get_invoice(owner_id, iid):
    return _q("SELECT * FROM bn_invoices WHERE owner_id=? AND id=?", (owner_id, iid), one=True)


def set_invoice_status(owner_id, iid, status):
    _q("UPDATE bn_invoices SET status=? WHERE owner_id=? AND id=?", (status, owner_id, iid), commit=True)


def delete_invoice(owner_id, iid):
    _q("DELETE FROM bn_invoice_runs WHERE owner_id=? AND invoice_id=?", (owner_id, iid), commit=True)
    _q("DELETE FROM bn_invoices WHERE owner_id=? AND id=?", (owner_id, iid), commit=True)


def due_invoices(now=None):
    return _q("SELECT * FROM bn_invoices WHERE status='active' AND next_run IS NOT NULL AND next_run<=?", (now or now_ts(),))


def issue_run(inv):
    """Create a run for an invoice and schedule the next one. Returns the run dict."""
    issued = now_ts()
    run_id = _insert(
        "INSERT INTO bn_invoice_runs (invoice_id, owner_id, issued_at, due_at, amount, coin, status) VALUES (?,?,?,?,?,?, 'sent')",
        (inv["id"], inv["owner_id"], issued, issued + inv["due_days"] * 86400, inv["amount"], inv["coin"]),
    )
    nxt = next_run_after(max(inv["next_run"] or issued, issued - 1), inv["repeat"])
    if nxt is None:
        _q("UPDATE bn_invoices SET status='done', next_run=NULL, runs_count=runs_count+1 WHERE id=?", (inv["id"],), commit=True)
    else:
        _q("UPDATE bn_invoices SET next_run=?, runs_count=runs_count+1 WHERE id=?", (nxt, inv["id"]), commit=True)
    return _q("SELECT * FROM bn_invoice_runs WHERE id=?", (run_id,), one=True)


def open_runs(owner_id):
    return _q("SELECT r.*, i.client_name, i.title, i.chat_id, i.bc_id FROM bn_invoice_runs r "
              "JOIN bn_invoices i ON i.id=r.invoice_id WHERE r.owner_id=? AND r.status IN ('sent','overdue') ORDER BY r.id", (owner_id,))


def mark_run_paid(run_id, ref):
    _q("UPDATE bn_invoice_runs SET status='paid', paid_ref=?, paid_at=? WHERE id=? AND status IN ('sent','overdue')",
       (str(ref), now_ts(), run_id), commit=True)


def match_payment(owner_id, coin, amount):
    """Find the open invoice run a payment belongs to.

    Returns (run, None) when exactly one fits, (None, [runs]) when ambiguous, (None, []) when none.
    A payment fits when it has the same coin and covers the amount due. Exact-amount matches win.
    """
    amount = dec(amount, Decimal(0))
    cands = [r for r in open_runs(owner_id) if r["coin"] == coin and dec(r["amount"], Decimal(0)) <= amount]
    exact = [r for r in cands if dec(r["amount"], Decimal(0)) == amount]
    pool = exact or cands
    if len(pool) == 1:
        return pool[0], None
    return None, pool


def runs_needing_attention(now=None):
    """Open runs that are past due and due for a reminder (every 3 days, max 3)."""
    now = now or now_ts()
    return _q(
        "SELECT r.*, i.client_name, i.title, i.chat_id, i.bc_id FROM bn_invoice_runs r "
        "JOIN bn_invoices i ON i.id=r.invoice_id "
        "WHERE r.status IN ('sent','overdue') AND r.due_at<=? AND r.reminders<3 AND r.last_reminder_at<=?",
        (now, now - 3 * 86400),
    )


def bump_reminder(run_id):
    _q("UPDATE bn_invoice_runs SET status='overdue', reminders=reminders+1, last_reminder_at=? WHERE id=?", (now_ts(), run_id), commit=True)


# ---------------------------------------------------------------------------
# Watcher (poll Binance for new money events)
# ---------------------------------------------------------------------------

def poll_events(owner_id):
    """Return a list of NEW events for this owner. First run only builds a baseline (no spam)."""
    acc = get_account(owner_id)
    if not acc:
        return []
    client = client_for(owner_id)
    baseline = not acc["baseline_done"]
    start = int((time.time() - (30 * 86400 if baseline else 3 * 86400)) * 1000)
    events = []

    def _add(source, ref, event):
        if mark_seen(owner_id, source, ref) and not baseline:
            events.append(event)

    try:
        for d in client.deposits(start):
            st = d.get("status")
            if st not in (0, 1):
                continue
            _add("deposit", f"{d.get('txId') or d.get('id')}:{st}", {
                "kind": "deposit", "coin": d["coin"], "amount": d["amount"], "network": d.get("network"),
                "pending": st == 0, "ref": d.get("txId") or d.get("id"), "ts": int(d.get("insertTime", 0)) // 1000,
            })
    except BinanceError as exc:
        print(f"[binance] deposits poll failed owner={owner_id}: {exc}", flush=True)

    try:
        for p in client.pay_transactions(start):
            amt = dec(p.get("amount"), Decimal(0))
            who = (p.get("payerInfo") or {}).get("name") if amt > 0 else (p.get("receiverInfo") or {}).get("name")
            _add("pay", p.get("transactionId"), {
                "kind": "pay_in" if amt > 0 else "pay_out", "coin": p.get("currency"), "amount": fmt_dec(abs(amt)),
                "who": who, "ref": p.get("transactionId"), "ts": int(p.get("transactionTime", 0)) // 1000,
                "order_type": p.get("orderType"),
            })
    except BinanceError as exc:
        print(f"[binance] pay poll failed owner={owner_id}: {exc}", flush=True)

    try:
        for w in client.withdrawals(start):
            st = w.get("status")
            # 6 = completed, 1 = cancelled, 3 = rejected, 5 = failure
            if st not in (1, 3, 5, 6):
                continue
            _add("withdraw", f"{w.get('id')}:{st}", {
                "kind": "withdraw", "coin": w["coin"], "amount": w["amount"], "network": w.get("network"),
                "state": {6: "done", 1: "cancelled", 3: "rejected", 5: "failed"}[st], "ref": w.get("id"),
                "address": w.get("address"), "ts": 0,
            })
    except BinanceError as exc:
        print(f"[binance] withdraw poll failed owner={owner_id}: {exc}", flush=True)

    if baseline:
        update_account(owner_id, baseline_done=1)
    return events


# ---------------------------------------------------------------------------
# Link account
# ---------------------------------------------------------------------------

def link_account(owner_id, api_key, api_secret):
    """Validate credentials against Binance and store them encrypted. Returns permission summary."""
    api_key, api_secret = api_key.strip(), api_secret.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{32,128}", api_key) or not re.fullmatch(r"[A-Za-z0-9]{32,128}", api_secret):
        raise ValueError("شكل الـ Key أو الـ Secret غير صحيح.")
    c = BinanceClient(api_key, api_secret)
    c.sync_time()
    r = c.api_restrictions()
    if not r.get("enableReading", True):
        raise ValueError("الـ API Key مفيهوش صلاحية القراءة (Enable Reading).")
    perms = {
        "can_withdraw": bool(r.get("enableWithdrawals")),
        "ip_restricted": bool(r.get("ipRestrict")),
        "trading": bool(r.get("enableSpotAndMarginTrading") or r.get("enableFutures")),
        "internal_transfer": bool(r.get("enableInternalTransfer")),
    }
    save_account(owner_id, api_key, api_secret, perms["can_withdraw"], perms["ip_restricted"])
    return perms


# ---------------------------------------------------------------------------
# Misc helpers used by the Telegram layer
# ---------------------------------------------------------------------------

def list_clients(owner_id, limit=12):
    return _q("SELECT chat_id, business_connection_id AS bc_id, client_name FROM business_clients "
              "WHERE owner_user_id=? ORDER BY last_message_at DESC LIMIT ?", (owner_id, limit))


def summary(owner_id):
    """Rule-based 24h summary (no AI, no network)."""
    since = now_ts() - 86400
    rows = _q("SELECT kind, coin, amount, status FROM bn_ledger WHERE owner_id=? AND ts>=?", (owner_id, since))
    totals = {"in": {}, "transfer": {}}
    for r in rows:
        if r["kind"] in totals and r["status"] in ("ok", "sent"):
            totals[r["kind"]][r["coin"]] = totals[r["kind"]].get(r["coin"], Decimal(0)) + dec(r["amount"], Decimal(0))
    runs = open_runs(owner_id)
    overdue = [r for r in runs if r["status"] == "overdue" or r["due_at"] < now_ts()]
    due_total = {}
    for r in runs:
        due_total[r["coin"]] = due_total.get(r["coin"], Decimal(0)) + dec(r["amount"], Decimal(0))
    return {"in": totals["in"], "out": totals["transfer"], "open": len(runs), "overdue": len(overdue), "due_total": due_total}
