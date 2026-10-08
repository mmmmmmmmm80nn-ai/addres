"""Auto-picks a proxy that Binance actually accepts (HTTP 200 on /ping).

Candidates: BINANCE_PROXY (pinned, tried first) + every line of proxies.txt.
Set BINANCE_PROXY_AUTO=0 to disable the automatic search.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.environ.get("BINANCE_PROXY_FILE", os.path.join(_HERE, "proxies.txt")).strip()
AUTO = os.environ.get("BINANCE_PROXY_AUTO", "1").strip().lower() in ("1", "true", "yes", "on")
PING = "https://api.binance.com/api/v3/ping"
TEST_TIMEOUT = 8
COOLDOWN = 900

_lock = threading.Lock()
_find_lock = threading.Lock()
_sticky = None
_bad = {}


def _norm(line):
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line:
        line = "http://" + line
    return line.replace("socks5://", "socks5h://").replace("socks4://", "socks4a://")


def _all():
    out = []
    pinned = _norm(os.environ.get("BINANCE_PROXY", ""))
    if pinned:
        out.append(pinned)
    try:
        with open(FILE, encoding="utf-8") as f:
            out += [p for p in (_norm(l) for l in f) if p]
    except OSError:
        pass
    return list(dict.fromkeys(out))


def _test(px):
    try:
        r = requests.get(PING, proxies={"http": px, "https": px}, timeout=TEST_TIMEOUT)
        return px if r.status_code == 200 else None
    except Exception:
        return None


def current():
    with _lock:
        if _sticky:
            return _sticky
    return _norm(os.environ.get("BINANCE_PROXY", ""))


def report_bad(px):
    global _sticky
    with _lock:
        _bad[px] = time.time() + COOLDOWN
        if _sticky == px:
            _sticky = None


def find():
    """Test candidates in parallel, keep the first one Binance answers 200 through."""
    global _sticky
    if not AUTO:
        return None
    with _find_lock:
        with _lock:
            if _sticky:
                return _sticky
            now = time.time()
            cands = [p for p in _all() if _bad.get(p, 0) <= now]
        if not cands:
            return None
        print(f"binance_proxy: جاري اختبار {len(cands)} بروكسي...", flush=True)
        ex = ThreadPoolExecutor(max_workers=40)
        try:
            futs = [ex.submit(_test, p) for p in cands]
            for fut in as_completed(futs):
                px = fut.result()
                if px:
                    with _lock:
                        _sticky = px
                    print("binance_proxy: تم اختيار بروكسي شغال", flush=True)
                    return px
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        now = time.time()
        with _lock:
            for p in cands:
                _bad[p] = now + COOLDOWN
        print("binance_proxy: مفيش بروكسي شغال", flush=True)
        return None
