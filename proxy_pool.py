import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests


def _env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


_HERE = os.path.dirname(os.path.abspath(__file__))

ENABLED = _env_bool("PROXY_ENABLED", True)
PROXY_FILE = os.environ.get("PROXY_FILE", os.path.join(_HERE, "proxies.txt")).strip()
MAX_TRIES = max(1, _env_int("PROXY_MAX_TRIES", 4))
PHASE_BUDGET = max(10, _env_int("PROXY_PHASE_BUDGET", 45))
DIRECT_FALLBACK = _env_bool("PROXY_DIRECT_FALLBACK", True)
CHECK_TIMEOUT = max(2, _env_int("PROXY_CHECK_TIMEOUT", 4))
ATTEMPT_TIMEOUT_AUDIO = max(10, _env_int("PROXY_ATTEMPT_TIMEOUT", 35))
ATTEMPT_TIMEOUT_VIDEO = max(30, _env_int("PROXY_ATTEMPT_TIMEOUT_VIDEO", 150))
MIN_SPEED = max(0, _env_int("PROXY_MIN_SPEED_KB", 100)) * 1024
ASSUMED_SPEED = 300 * 1024
RECHECK_SECS = max(2, _env_int("PROXY_RECHECK_MIN", 10)) * 60
WAIT_SECS = max(0, _env_int("PROXY_WAIT_SECS", 25))
USE_COOKIES = _env_bool("PROXY_USE_COOKIES", False)

CHECK_URL = "https://www.youtube.com/generate_204"
CHECK_WORKERS = max(10, _env_int("PROXY_CHECK_WORKERS", 60))

PROXY_REMOTE_URL = os.environ.get(
    "PROXY_REMOTE_URL",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?"
    "request=display_proxies&proxy_format=protocolipport&format=text&protocol=https&timeout=5000",
).strip()
PROXY_REMOTE_ENABLED = _env_bool("PROXY_REMOTE_ENABLED", bool(PROXY_REMOTE_URL))
REMOTE_FETCH_TIMEOUT = max(3, _env_int("PROXY_REMOTE_FETCH_TIMEOUT", 10))

_LINE_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks4a?|socks5h?)://)?"
    r"(?:(?P<auth>[^@/\s]+)@)?"
    r"(?P<host>[A-Za-z0-9.\-]+):(?P<port>\d{2,5})$",
    re.I,
)

_lock = threading.RLock()
_state = {}
_file_mtime = None
_started = False
_ready = threading.Event()
_socks_warned = False
_sticky = None


def parse_line(line):
    s = (line or "").strip().strip(",;")
    if not s or s.startswith("#"):
        return []
    m = _LINE_RE.match(s)
    if not m:
        return []
    port = int(m.group("port"))
    if not (1 <= port <= 65535):
        return []
    scheme = (m.group("scheme") or "").lower()
    auth = (m.group("auth") + "@") if m.group("auth") else ""
    base = f"{auth}{m.group('host')}:{port}"
    if scheme:
        return [f"{scheme}://{base}"]
    return [f"socks5://{base}", f"http://{base}"]


def _fetch_remote_proxies():
    if not PROXY_REMOTE_ENABLED or not PROXY_REMOTE_URL:
        return []
    try:
        r = requests.get(PROXY_REMOTE_URL, timeout=REMOTE_FETCH_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        urls = []
        for line in r.text.splitlines():
            urls.extend(parse_line(line))
        print(f"proxy_pool: هات {len(urls)} بروكسي من المصدر الخارجي (ProxyScrape)", flush=True)
        return urls
    except Exception as exc:
        print(f"proxy_pool: فشل جلب البروكسيات من المصدر الخارجي: {exc}", flush=True)
        return []


def load_entries():
    urls = []
    seen = set()

    def _add(raw):
        for u in parse_line(raw):
            if u not in seen:
                seen.add(u)
                urls.append(u)

    try:
        if PROXY_FILE and os.path.exists(PROXY_FILE):
            with open(PROXY_FILE, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    _add(line)
    except OSError as exc:
        print(f"proxy_pool: تعذر قراءة {PROXY_FILE}: {exc}", flush=True)
    for raw in re.split(r"[,\s]+", os.environ.get("PROXY_LIST", "")):
        _add(raw)
    for raw in _fetch_remote_proxies():
        _add(raw)
    return urls


def _reload_if_changed(force=False):
    global _file_mtime
    try:
        _file_mtime = os.path.getmtime(PROXY_FILE) if os.path.exists(PROXY_FILE) else None
    except OSError:
        _file_mtime = None
    urls = load_entries()
    with _lock:
        for u in urls:
            _state.setdefault(u, {"ok": None, "fails": 0, "bad_until": 0.0,
                                  "last_ok": 0.0, "latency": None, "speed": None})
        for u in list(_state):
            if u not in urls:
                del _state[u]
    return True


def mask(url):
    return re.sub(r"//[^@/]+@", "//***@", url or "")


def requests_proxies(url):
    return {"http": url, "https": url} if url else None


def _check_one(url):
    global _socks_warned
    t0 = time.time()
    try:
        r = requests.get(CHECK_URL, proxies=requests_proxies(url),
                         timeout=(CHECK_TIMEOUT, CHECK_TIMEOUT), allow_redirects=False,
                         headers={"User-Agent": "Mozilla/5.0"})
        ok = r.status_code < 400
        return url, ok, (time.time() - t0) if ok else None
    except requests.exceptions.InvalidSchema:
        if not _socks_warned:
            _socks_warned = True
            print("proxy_pool: PySocks غير مثبت — بروكسيات SOCKS مش هتتفحص مسبقًا "
                  "(pip install 'requests[socks]')", flush=True)
        return url, None, None
    except Exception:
        return url, False, None


def check_all():
    with _lock:
        urls = list(_state)
    if not urls:
        return 0, 0
    alive = 0
    with ThreadPoolExecutor(max_workers=min(CHECK_WORKERS, len(urls))) as pool:
        for url, ok, latency in pool.map(_check_one, urls):
            with _lock:
                st = _state.get(url)
                if st is None:
                    continue
                st["ok"] = ok
                st["latency"] = latency
                if ok:
                    alive += 1
                    st["last_ok"] = time.time()
                    st["fails"] = 0
                    st["bad_until"] = 0.0
            if ok and not _ready.is_set():
                _ready.set()
    _ready.set()
    return alive, len(urls)


def _loop():
    first = True
    while True:
        try:
            _reload_if_changed(force=first)
            first = False
            alive, total = check_all()
            print(f"proxy_pool: {alive}/{total} بروكسي شغّال", flush=True)
            if total and not alive:
                print("proxy_pool: تحذير — مفيش بروكسي شغّال، التحميل هيكمل مباشر", flush=True)
        except Exception as exc:
            print(f"proxy_pool: خطأ في الفحص: {exc}", flush=True)
            _ready.set()
        time.sleep(RECHECK_SECS)


def start():
    global _started
    if not ENABLED:
        _ready.set()
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True, name="proxy-pool").start()


def acquire(exclude=(), wait=True):
    if not ENABLED:
        return None
    start()
    if wait and not _ready.is_set():
        _ready.wait(WAIT_SECS)
    now = time.time()
    with _lock:
        def _usable(u):
            s = _state.get(u)
            return bool(s) and s["ok"] is not False and s["bad_until"] <= now and u not in exclude

        if _sticky and _usable(_sticky):
            return _sticky
        alive = [u for u, s in _state.items() if s["ok"] is True and _usable(u)]
        if alive:
            alive.sort(key=lambda u: (-(_state[u]["speed"] or ASSUMED_SPEED), _state[u]["latency"] or 99))
            return random.choice(alive[:3])
        unverified = [u for u, s in _state.items() if s["ok"] is None and _usable(u)]
        return random.choice(unverified) if unverified else None


def mark_good(url):
    global _sticky
    if not url:
        return
    with _lock:
        st = _state.get(url)
        if st:
            st.update(ok=True, fails=0, bad_until=0.0, last_ok=time.time())
            if _sticky != url:
                _sticky = url
                print(f"proxy_pool: البروكسي الحالي → {mask(url)}", flush=True)


def record_speed(url, size_bytes, seconds):
    global _sticky
    if not url or size_bytes < 512 * 1024 or seconds <= 0:
        return
    bps = size_bytes / seconds
    with _lock:
        st = _state.get(url)
        if not st:
            return
        st["speed"] = bps if not st["speed"] else 0.5 * st["speed"] + 0.5 * bps
        if MIN_SPEED and size_bytes >= 1024 * 1024 and bps < MIN_SPEED:
            st["bad_until"] = time.time() + 600
            if _sticky == url:
                _sticky = None
            print(f"proxy_pool: {mask(url)} بطيء ({bps / 1024:.0f}KB/s) — هيتم التبديل", flush=True)
        else:
            print(f"proxy_pool: {mask(url)} سرعة {st['speed'] / 1024:.0f}KB/s", flush=True)


def mark_bad(url, kind="proxy"):
    global _sticky
    if not url or kind in ("other", "video"):
        return
    with _lock:
        st = _state.get(url)
        if not st:
            return
        if _sticky == url:
            _sticky = None
            print(f"proxy_pool: البروكسي {mask(url)} وقف ({kind}) — هيتم التبديل لأسرع بديل", flush=True)
        st["fails"] += 1
        base = 1800 if kind == "blocked" else 600
        st["bad_until"] = time.time() + base * min(st["fails"], 4)


_BLOCKED_PAT = ("not a bot", "sign in", "confirm you", "http error 429", "http error 403",
                "too many requests", "forbidden", "captcha")
_PROXY_PAT = ("proxy", "socks", "timed out", "timeout", "connection refused",
              "connection reset", "unable to connect", "tunnel", "remote end closed",
              "network is unreachable", "eof occurred", "ssl", "unable to download webpage",
              "max retries", "name or service not known", "broken pipe", "incompleteread",
              "eoferror", "bytes missing", "unexpected error", "connection aborted", "errno",
              "handshake", "http error 407", "http error 502", "http error 503", "http error 504")


_VIDEO_PAT = ("video unavailable", "private video", "has been removed", "has been terminated",
              "members-only", "join this channel", "live event will begin", "premieres in",
              "this video is no longer available")


def classify_error(err_text):
    e = (err_text or "").lower()
    if any(p in e for p in _VIDEO_PAT):
        return "video"
    if any(p in e for p in _BLOCKED_PAT):
        return "blocked"
    if any(p in e for p in _PROXY_PAT):
        return "proxy"
    return "other"


def candidates(max_tries=None):
    tried = set()
    for _ in range(MAX_TRIES if max_tries is None else max_tries):
        p = acquire(exclude=tried)
        if p is None:
            break
        tried.add(p)
        yield p


def plan(max_tries=None):
    got = False
    for p in candidates(max_tries):
        got = True
        yield p
    if DIRECT_FALLBACK or not got:
        yield None


def stats():
    with _lock:
        total = len(_state)
        alive = sum(1 for s in _state.values() if s["ok"] is True)
        cooling = sum(1 for s in _state.values() if s["bad_until"] > time.time())
    return {"total": total, "alive": alive, "cooling": cooling, "enabled": ENABLED}
