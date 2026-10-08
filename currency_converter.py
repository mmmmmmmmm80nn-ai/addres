"""Live currency converter (fiat + crypto) for the Business bot.

Design notes
------------
* Every price is reduced to "USD per 1 unit", so any pair converts through USD.
* Three independent live components, each cached for a short TTL and each with
  an ordered list of fallback sources:
    - crypto : Binance (data-api mirror -> api) -> OKX -> CoinGecko   (TTL ~2s)
    - egp    : Binance P2P USDT/EGP market mid-price                   (TTL ~20s)
    - fiat   : open.er-api.com -> fawazahmed0 currency-api             (TTL 15min,
               these providers only publish new fiat rates hourly/daily)
* A failed refresh never raises: the last good value is reused (up to
  MAX_STALE seconds) and the result footer says so.
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape as _esc

import requests

# --------------------------------------------------------------------------
# Currency registry (order = button order)
# --------------------------------------------------------------------------

# code -> (arabic name, kind, flag/emoji)
CURRENCIES = {
    "USD": ("دولار", "fiat", "🇺🇸"),
    "EGP": ("جنيه مصري", "fiat", "🇪🇬"),
    "TON": ("تون", "crypto", "💎"),
}

FIAT_CODES = [c for c, v in CURRENCIES.items() if v[1] == "fiat"]
CRYPTO_CODES = [c for c, v in CURRENCIES.items() if v[1] == "crypto"]

# crypto code -> candidate Binance/OKX base symbols (first one that exists wins).
# MATIC was renamed POL on exchanges, so both are tried.
_CRYPTO_SYMBOLS = {"TON": ["GRAM"]}   # التوكن اتغير اسمه TON -> GRAM (15 يونيو 2026)

_COINGECKO_IDS = {"TON": ["the-open-network"]}

HTTP_TIMEOUT = 4
_UA = {"User-Agent": "Mozilla/5.0 (compatible; BusinessBot/1.0)"}

# --------------------------------------------------------------------------
# Live sources
# --------------------------------------------------------------------------

def _http(method, url, **kw):
    """requests مع fallback: لو المصدر (بينانس غالبًا) اتحظر/فشل من السيرفر، نعيد المحاولة عبر البروكسي الشغال."""
    kw.setdefault("timeout", HTTP_TIMEOUT)
    kw.setdefault("headers", _UA)
    last_exc = None
    try:
        r = requests.request(method, url, **kw)
        if r.status_code not in (403, 418, 429, 451):
            r.raise_for_status()
            return r
        last_exc = requests.HTTPError(f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        last_exc = exc
    if "binance" not in url:
        raise last_exc
    try:
        import binance_proxy
        px = binance_proxy.current() or binance_proxy.find()
    except Exception:  # noqa: BLE001
        px = None
    if not px:
        raise last_exc
    try:
        kw2 = dict(kw)
        kw2["proxies"] = {"http": px, "https": px}
        kw2["timeout"] = max(kw.get("timeout", HTTP_TIMEOUT), 8)
        r = requests.request(method, url, **kw2)
        r.raise_for_status()
        return r
    except Exception:
        try:
            binance_proxy.report_bad(px)
        except Exception:  # noqa: BLE001
            pass
        raise


def _positive(x):
    v = float(x)
    if not (v > 0) or v != v or v == float("inf"):
        raise ValueError("non-positive price")
    return v


def _pick_crypto(symbol_to_price):
    """symbol_to_price: {'BTC': 65000.0, ...}  ->  {our_code: usd_price}"""
    out = {}
    for code, candidates in _CRYPTO_SYMBOLS.items():
        for sym in candidates:
            if sym in symbol_to_price:
                out[code] = symbol_to_price[sym]
                break
    return out


def _crypto_binance(base_url):
    def fetch():
        # أول محاولة: سعر TON لوحده (خفيف وسريع)، وبعدين الجدول الكامل لو فشلت
        out = {}
        for sym in ("GRAMUSDT",):
            try:
                r = _http("GET", f"{base_url}/api/v3/ticker/price", params={"symbol": sym})
                out["TON"] = _positive(r.json()["price"])
            except Exception:  # noqa: BLE001
                out = {}
        if out:
            return out
        r = _http("GET", f"{base_url}/api/v3/ticker/price")
        table = {}
        for row in r.json():
            sym = row.get("symbol", "")
            if sym.endswith("USDT"):
                try:
                    table[sym[:-4]] = _positive(row["price"])
                except (ValueError, KeyError, TypeError):
                    continue
        out = _pick_crypto(table)
        if not out:
            raise ValueError("binance: empty")
        return out
    return fetch


def _crypto_okx():
    r = requests.get(
        "https://www.okx.com/api/v5/market/ticker",
        params={"instId": "GRAM-USDT"}, timeout=HTTP_TIMEOUT, headers=_UA,
    )
    r.raise_for_status()
    rows = r.json().get("data") or []
    if not rows:
        raise ValueError("okx: empty")
    return {"TON": _positive(rows[0]["last"])}


def _crypto_bybit():
    r = requests.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "spot", "symbol": "GRAMUSDT"}, timeout=HTTP_TIMEOUT, headers=_UA,
    )
    r.raise_for_status()
    rows = (r.json().get("result") or {}).get("list") or []
    if not rows:
        raise ValueError("bybit: empty")
    return {"TON": _positive(rows[0]["lastPrice"])}


def _crypto_coingecko():
    ids = sorted({i for lst in _COINGECKO_IDS.values() for i in lst})
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ",".join(ids), "vs_currencies": "usd"},
        timeout=HTTP_TIMEOUT, headers=_UA,
    )
    r.raise_for_status()
    data = r.json()
    out = {}
    for code, candidates in _COINGECKO_IDS.items():
        for cid in candidates:
            if cid in data and "usd" in data[cid]:
                out[code] = _positive(data[cid]["usd"])
                break
    if not out:
        raise ValueError("coingecko: empty")
    return out


def _crypto_kraken():
    r = requests.get(
        "https://api.kraken.com/0/public/Ticker", params={"pair": "GRAMUSD"},
        timeout=HTTP_TIMEOUT, headers=_UA,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError("kraken: " + ", ".join(data["error"]))
    row = next(iter((data.get("result") or {}).values()), None)
    if not row:
        raise ValueError("kraken: empty")
    try:  # منتصف أفضل سعر شراء/بيع أدق من آخر صفقة في زوج سيولته أقل
        return {"TON": (_positive(row["a"][0]) + _positive(row["b"][0])) / 2}
    except (KeyError, IndexError, ValueError, TypeError):
        return {"TON": _positive(row["c"][0])}


def _crypto_coinbase():
    r = requests.get(
        "https://api.coinbase.com/v2/prices/GRAM-USD/spot",
        timeout=HTTP_TIMEOUT, headers=_UA,
    )
    r.raise_for_status()
    return {"TON": _positive(r.json()["data"]["amount"])}


def _crypto_binance_spot():
    # data-api.binance.vision = نفس أسعار Binance العامة وبيشتغل حتى لو api.binance.com محجوب
    for base in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            r = _http("GET", f"{base}/api/v3/ticker/price", params={"symbol": "GRAMUSDT"})
            return {"TON": _positive(r.json()["price"])}
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("binance: unavailable")


def _crypto_gate():
    r = requests.get("https://api.gateio.ws/api/v4/spot/tickers",
                     params={"currency_pair": "GRAM_USDT"}, timeout=HTTP_TIMEOUT, headers=_UA)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError("gate: empty")
    return {"TON": _positive(rows[0]["last"])}


def _crypto_kucoin():
    r = requests.get("https://api.kucoin.com/api/v1/market/orderbook/level1",
                     params={"symbol": "GRAM-USDT"}, timeout=HTTP_TIMEOUT, headers=_UA)
    r.raise_for_status()
    data = r.json().get("data") or {}
    if not data.get("price"):
        raise ValueError("kucoin: empty")
    return {"TON": _positive(data["price"])}


def _crypto_mexc():
    r = requests.get("https://api.mexc.com/api/v3/ticker/price",
                     params={"symbol": "GRAMUSDT"}, timeout=HTTP_TIMEOUT, headers=_UA)
    r.raise_for_status()
    return {"TON": _positive(r.json()["price"])}


def _crypto_bitget():
    r = requests.get("https://api.bitget.com/api/v2/spot/market/tickers",
                     params={"symbol": "GRAMUSDT"}, timeout=HTTP_TIMEOUT, headers=_UA)
    r.raise_for_status()
    rows = r.json().get("data") or []
    if not rows:
        raise ValueError("bitget: empty")
    return {"TON": _positive(rows[0]["lastPr"])}


# أقصى فرق مسموح بين مصدر والوسيط (1% ≈ 2-3 سنت على TON) وأقل عدد مصادر متفقة
_TON_MAX_DEVIATION = 0.01
_TON_MIN_AGREE = 2


def _crypto_market_consensus():
    """وسيط (median) أسعار أكبر البورصات سيولة لحظيًا: Binance / OKX / Bybit / Kraken / Coinbase.

    أي مصدر بعيد أكتر من 1% عن الوسيط بيتستبعد، ولازم مصدرين على الأقل يتفقوا.
    CoinGecko بيتستخدم احتياطي بس لو البورصات كلها وقعت (لأنه متوسط بيتأخر).
    """
    exchanges = (
        ("Binance", _crypto_binance_spot),
        ("OKX", _crypto_okx),
        ("Kraken", _crypto_kraken),
        ("Coinbase", _crypto_coinbase),
        ("Gate", _crypto_gate),
        ("KuCoin", _crypto_kucoin),
        ("MEXC", _crypto_mexc),
        ("Bitget", _crypto_bitget),
    )
    quotes, errors = _collect_ton_quotes(exchanges)
    if len(quotes) < _TON_MIN_AGREE:
        more, more_err = _collect_ton_quotes((("CoinGecko", _crypto_coingecko),))
        quotes += more
        errors += more_err
    if not quotes:
        raise ValueError("no live TON quotes (" + ", ".join(errors) + ")")

    median = _median([price for _, price in quotes])
    if os.environ.get("TON_DEBUG"):
        print("[currency] TON quotes: " + ", ".join(f"{l}={p}" for l, p in quotes), flush=True)
    consistent = [(label, price) for label, price in quotes
                  if abs(price - median) / median <= _TON_MAX_DEVIATION]
    if len(consistent) < _TON_MIN_AGREE:
        raise ValueError("TON feeds disagree: " + ", ".join(f"{l}={p}" for l, p in quotes))
    return {"TON": _median([price for _, price in consistent])}


_PROVIDER_FAIL_AT = {}
_PROVIDER_COOLDOWN = 60.0   # مصدر فشل مايتجربش تاني قبل دقيقة (بيوفر الوقت والـ spam)
_LAST_ERR_LOG = {"t": 0.0}


def _err_text(exc):
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return f"{type(exc).__name__}{'(' + str(code) + ')' if code else ''}: {str(exc)[:60]}"


def _collect_ton_quotes(providers):
    now = time.time()
    active = [(l, f) for l, f in providers if now - _PROVIDER_FAIL_AT.get(l, 0) > _PROVIDER_COOLDOWN]
    if not active:           # كله في الكولداون؟ جرّب الكل
        active = list(providers)
    quotes, errors = [], []
    pool = ThreadPoolExecutor(max_workers=len(active))
    try:
        futures = {pool.submit(fn): label for label, fn in active}
        done, pending = wait(futures, timeout=HTTP_TIMEOUT + 1)
        for future in pending:
            future.cancel()
            _PROVIDER_FAIL_AT[futures[future]] = now
            errors.append(f"{futures[future]}: timeout")
        for future in done:
            label = futures[future]
            try:
                value = future.result().get("TON")
                if value:
                    quotes.append((label, _positive(value)))
                    _PROVIDER_FAIL_AT.pop(label, None)
            except Exception as exc:  # noqa: BLE001
                _PROVIDER_FAIL_AT[label] = now
                errors.append(f"{label}: {_err_text(exc)}")
    finally:
        pool.shutdown(wait=False)   # ماتستناش المصدر البطيء
    if os.environ.get("TON_DEBUG") and errors:
        print(f"[currency] TON sources failed: {' | '.join(errors)}", flush=True)
    return quotes, errors


def _fiat_open_er():
    r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=HTTP_TIMEOUT, headers=_UA)
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success":
        raise ValueError("open.er-api: not success")
    rates = data["rates"]
    out = {c: _positive(rates[c]) for c in FIAT_CODES if c in rates}
    if not out:
        raise ValueError("open.er-api: empty")
    return out


def _fiat_fawaz():
    r = requests.get(
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
        timeout=HTTP_TIMEOUT, headers=_UA,
    )
    r.raise_for_status()
    rates = r.json()["usd"]
    out = {c: _positive(rates[c.lower()]) for c in FIAT_CODES if c.lower() in rates}
    if not out:
        raise ValueError("fawaz: empty")
    return out


def _median(values):
    values = sorted(values)
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def _egp_binance_p2p():
    """Mid-market USDT/EGP from Binance P2P (median of top ads on each side)."""
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    sides = {}
    for trade_type in ("BUY", "SELL"):
        body = {
            "fiat": "EGP", "asset": "USDT", "tradeType": trade_type,
            "page": 1, "rows": 10, "payTypes": [], "countries": [],
            "publisherType": None, "proMerchantAds": False, "shieldMerchantAds": False,
            "transAmount": "",
        }
        r = _http("POST", url, json=body)
        prices = []
        for item in r.json().get("data") or []:
            try:
                prices.append(_positive(item["adv"]["price"]))
            except (KeyError, ValueError, TypeError):
                continue
        if len(prices) < 3:
            raise ValueError("p2p: too few ads")
        top = prices[:9]
        med = _median(top)
        # شيل الإعلانات الشاذة (سعر بعيد أكتر من 4% عن الوسيط)
        clean = [x for x in top if abs(x - med) / med <= 0.04] or top
        sides[trade_type] = _median(clean)
    ask = sides["BUY"]    # شراء USDT (إعلانات البائعين) = الأغلى
    bid = sides["SELL"]   # بيع USDT (إعلانات المشترين)  = الأرخص
    if bid > ask:
        bid, ask = ask, bid
    mid = (bid + ask) / 2
    if not (5 < mid < 1000):  # sanity band for EGP per USDT
        raise ValueError(f"p2p: implausible {mid}")
    # EGP per 1 USDT: mid للعرض، و bid/ask للتحويل حسب الاتجاه
    return {"mid": mid, "bid": bid, "ask": ask}


class _Cached:
    """One live value with TTL, ordered fallback sources and stale-on-error.

    السرعة: لو القيمة قديمة شوية بنرجّعها فورًا ونحدّثها في الخلفية (بدل ما المستخدم يستنى الشبكة)،
    ولو المصدر واقع بنفتكر الفشل 8 ثواني عشان كل ضغطة زرار ماتستناش timeout من جديد."""

    MIN_FORCE_GAP = 1.0  # a forced refresh never hits the network more than 1/s
    FAIL_MEMORY = 8.0

    def __init__(self, name, ttl, max_stale, sources):
        self.name = name
        self.ttl = ttl
        self.max_stale = max_stale
        self.swr = min(max_stale, max(ttl * 4, 30.0))  # أقصى عمر نرجّعه فورًا مع تحديث بالخلفية
        self.sources = sources  # [(label, callable)]
        self._lock = threading.Lock()
        self._bg_running = False
        self.value = None
        self.ts = 0.0
        self.source = None
        self.last_error = None
        self.last_used = 0.0
        self.fail_ts = 0.0

    def get(self, force=False):
        """Return (value, source_label, fetched_at, is_stale) - value may be None."""
        now = time.time()
        self.last_used = now
        value, source, ts = self.value, self.source, self.ts
        if value is not None and not force:
            age = now - ts
            if age < self.ttl:
                return value, source, ts, False
            if age < self.swr:
                self.refresh_async()
                return value, source, ts, False
        elif value is None and not force and now - self.fail_ts < self.FAIL_MEMORY:
            return None, None, 0.0, True
        return self._fetch_blocking(force)

    def refresh_async(self):
        if self._bg_running:
            return
        self._bg_running = True

        def _run():
            try:
                self._fetch_blocking(False)
            except Exception as exc:  # noqa: BLE001
                print(f"[currency] {self.name} background refresh failed: {exc}", flush=True)
            finally:
                self._bg_running = False

        threading.Thread(target=_run, daemon=True, name=f"cvt-refresh-{self.name}").start()

    def _fetch_blocking(self, force=False):
        with self._lock:
            now = time.time()
            age = now - self.ts
            fresh = self.value is not None and age < self.ttl
            if fresh and not (force and age >= self.MIN_FORCE_GAP):
                return self.value, self.source, self.ts, False
            for label, fn in self.sources:
                try:
                    value = fn()
                    if not value:
                        raise ValueError("empty")
                    self.value, self.source, self.ts = value, label, time.time()
                    self.last_error = None
                    self.fail_ts = 0.0
                    return self.value, self.source, self.ts, False
                except Exception as exc:  # noqa: BLE001 - try next source
                    self.last_error = f"{label}: {exc}"
                    print(f"[currency] {self.name} source failed -> {self.last_error}", flush=True)
            self.fail_ts = time.time()
            if self.value is not None and (time.time() - self.ts) < self.max_stale:
                return self.value, self.source, self.ts, True
            return None, None, 0.0, True


_crypto = _Cached("crypto", ttl=12.0, max_stale=120, sources=[
    ("وسيط Binance/OKX/Bybit/Kraken/Coinbase", _crypto_market_consensus),
])
_egp = _Cached("egp", ttl=20.0, max_stale=180, sources=[
    ("Binance P2P", _egp_binance_p2p),
])
_fiat = _Cached("fiat", ttl=900.0, max_stale=86400, sources=[
    ("open.er-api", _fiat_open_er),
    ("currency-api", _fiat_fawaz),
])


_warmer_started = False

def start_warmer():
    """بيسخّن الأسعار في الخلفية: أول ضغطة زرار بترجع فورًا من الكاش بدل ما تستنى الشبكة.
    بيحدّث بس المكونات اللي اتستخدمت آخر 10 دقايق، فمفيش طلبات على الفاضي."""
    global _warmer_started
    if _warmer_started:
        return
    _warmer_started = True

    def _loop():
        for comp in (_crypto, _fiat, _egp):  # تسخين أولي مرة واحدة عند التشغيل
            comp.last_used = time.time()
            comp.refresh_async()
        while True:
            time.sleep(3)
            now = time.time()
            for comp in (_crypto, _egp, _fiat):
                if now - comp.last_used < 600 and now - comp.ts >= comp.ttl * 0.8:
                    comp.refresh_async()

    threading.Thread(target=_loop, daemon=True, name="cvt-warmer").start()


class Rates:
    """Snapshot: usd[code] = USD value of 1 unit. Missing code => unavailable."""

    def __init__(self):
        self.usd = {"USD": 1.0, "USDT": 1.0}
        self.notes = []          # human-readable source lines
        self.stale = []          # codes served from old cache
        self.missing = []        # codes we could not price
        self.egp_official = False  # True = الجنيه بالسعر الرسمي مش سعر السوق
        self.egp_bid = None        # جنيه لكل 1 USDT لما بتبيع (تستلم جنيه)
        self.egp_ask = None        # جنيه لكل 1 USDT لما بتشتري (تدفع جنيه)
        self.fetched_at = time.time()


def _egp_settings():
    """(mode, override) من config.py — mode: official | p2p ؛ override: سعر ثابت أو 0."""
    try:
        import config
        mode = str(getattr(config, "EGP_RATE_MODE", "official")).strip().lower()
        override = float(getattr(config, "EGP_USD_OVERRIDE", 0) or 0)
    except Exception:  # noqa: BLE001
        mode, override = "official", 0.0
    return ("p2p" if mode == "p2p" else "official"), (override if override > 0 else 0.0)


def get_rates(codes=None, force=False):
    """Fetch (or reuse cached) live rates for `codes` (None = everything)."""
    wanted = set(codes) if codes else set(CURRENCIES)
    need_crypto = bool(wanted & set(CRYPTO_CODES))
    need_fiat = bool(wanted & (set(FIAT_CODES) - {"USD"}))
    need_egp = "EGP" in wanted
    egp_mode, egp_override = _egp_settings()
    need_p2p = need_egp and egp_mode == "p2p" and not egp_override

    results = {}

    def run(comp):
        results[comp.name] = comp.get(force=force)

    threads = []
    for need, comp in ((need_crypto, _crypto), (need_fiat or need_egp, _fiat), (need_p2p, _egp)):
        if need:
            t = threading.Thread(target=run, args=(comp,), daemon=True)
            t.start()
            threads.append(t)
    for t in threads:
        t.join(timeout=HTTP_TIMEOUT * 5)

    rates = Rates()

    if need_crypto:
        value, source, ts, stale = results.get("crypto", (None, None, 0, True))
        if value:
            for code, price in value.items():
                rates.usd[code] = price
            rates.notes.append(f"العملات الرقمية: {source} (لحظي)")
            if stale:
                rates.stale.append("crypto")
    if need_fiat or need_egp:
        value, source, ts, stale = results.get("fiat", (None, None, 0, True))
        if value:
            for code, per_usd in value.items():
                rates.usd[code] = 1.0 / per_usd
            rates.notes.append(f"العملات الورقية: {source} (بتتحدث كل ساعات مش بالثانية)")
            if stale:
                rates.stale.append("fiat")
    if need_egp and egp_override:
        rates.usd["EGP"] = 1.0 / egp_override
        rates.notes.append("الجنيه: سعر ثابت من الإعدادات")
    elif need_egp and not need_p2p:
        if "EGP" in rates.usd:
            rates.notes.append("الجنيه: السعر الرسمي")
    elif need_egp:
        value, source, ts, stale = results.get("egp", (None, None, 0, True))
        if value:  # EGP per USDT; treat USDT == USD
            rates.usd["EGP"] = 1.0 / value["mid"]
            rates.egp_bid = value["bid"]
            rates.egp_ask = value["ask"]
            rates.notes.append(f"الجنيه: {source} (سعر السوق لحظي)")
            if stale:
                rates.stale.append("egp")
        elif "EGP" in rates.usd:
            rates.egp_official = True
            rates.notes.append("الجنيه: السعر الرسمي (تعذر جلب سعر السوق)")

    rates.missing = [c for c in wanted if c not in rates.usd]
    rates.fetched_at = time.time()
    return rates


def get_ton_usd(force=False):
    """سعر TON بالدولار من نفس المصدر اللحظي بتاع المحوّل (وسيط Binance/OKX/Bybit/Kraken/Coinbase)."""
    try:
        value, _source, _ts, _stale = _crypto.get(force=force)
        if value and value.get("TON"):
            return float(value["TON"])
    except Exception as exc:  # noqa: BLE001
        print(f"[currency] get_ton_usd failed: {exc}", flush=True)
    return None


def convert(amount, src, dst, rates):
    """amount (Decimal/float) of src -> dst, or None if a rate is missing."""
    a = rates.usd.get(src)
    b = rates.usd.get(dst)
    if not a or not b:
        return None
    amt = float(amount)
    # اتجاه التحويل: لما بتستلم جنيه (بتبيع) بنستخدم سعر البيع الأرخص،
    # ولما بتدفع جنيه (بتشتري) بنستخدم سعر الشراء، بدل متوسط الاتنين.
    if dst == "EGP" and src != "EGP" and getattr(rates, "egp_bid", None):
        return amt * a * rates.egp_bid
    if src == "EGP" and dst != "EGP" and getattr(rates, "egp_ask", None):
        return amt / rates.egp_ask / b
    return amt * a / b


# --------------------------------------------------------------------------
# Number formatting
# --------------------------------------------------------------------------

def fmt_number(value, kind="fiat"):
    if value is None:
        return "—"
    ax = abs(value)
    if ax == 0:
        return "0"
    if ax >= 1000:
        return f"{value:,.2f}"
    if ax >= 1:
        if kind == "fiat":
            return f"{value:,.2f}"
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    if kind == "fiat":
        s = f"{value:.4f}"
        return s if float(s) != 0 else f"{value:.8f}".rstrip("0").rstrip(".")
    s = f"{value:.8f}".rstrip("0").rstrip(".")
    return s if float(s) != 0 else "<0.00000001"


def fmt_amount(amount):
    """Canonical text for the user's amount (Decimal)."""
    s = format(amount.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if abs(amount) >= 1000 and "." not in s:
        return f"{int(amount):,}"
    return s


# --------------------------------------------------------------------------
# Command parsing:  "حول 56"  /  "حول 56 دولار"  /  "تحويل ٥٦ usdt"
# --------------------------------------------------------------------------

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬،", "0123456789.,,")


def _norm(s):
    s = (s or "").strip().translate(_AR_DIGITS)
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s)          # tashkeel + tatweel
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", s).lower()


_TRIGGER_RE = re.compile(r"^(?:حول|تحويل)\s+(\d[\d,]*(?:\.\d+)?)\s*(.*)$")
# صيغة مختصرة بدون «حول»: «1تون» / «500 جنيه» / «1دولار» / «10usdt»
_SHORT_RE = re.compile(r"^(\d[\d,]*(?:\.\d+)?)\s*([^\d\s].*)$")

_ALIASES_RAW = {
    "USD": ["usd", "$", "دولار", "دولارات", "دولار امريكي"],
    "EGP": ["egp", "جنيه", "جنيهات", "جنيه مصري", "جنيه مصر"],
    "TON": ["ton", "gram", "تون", "جرام"],
}
ALIASES = {_norm(a): code for code, names in _ALIASES_RAW.items() for a in names}

MAX_AMOUNT = Decimal("1000000000000")  # 1e12


def parse_request(text):
    """Return {'amount': Decimal, 'currency': code|None} or None if not a convert command.

    "حول 56"        -> currency None (bot shows the currency picker)
    "حول 56 دولار"  -> currency 'USD'
    "حول 56 كيلو"   -> None  (unknown trailing word: normal chat, don't hijack it)
    """
    norm = _norm(text)
    m = _TRIGGER_RE.match(norm)
    short = False
    if not m:
        m = _SHORT_RE.match(norm)
        short = True
    if not m:
        return None
    number, rest = m.group(1), m.group(2).strip()
    if short and rest not in ALIASES:
        return None
    try:
        amount = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
    if len(format(amount.normalize(), "f")) > 18:
        return None
    if not rest:
        return {"amount": amount, "currency": None}
    code = ALIASES.get(rest)
    if code is None:
        return None
    return {"amount": amount, "currency": code}


def amount_token(amount):
    """Compact, round-trippable amount for callback_data (<= 18 chars)."""
    s = format(amount.normalize(), "f")
    return s


def amount_from_token(token):
    try:
        a = Decimal(token)
    except InvalidOperation:
        return None
    return a if 0 < a <= MAX_AMOUNT else None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _clock():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Africa/Cairo")).strftime("%I:%M:%S %p")
    except Exception:  # noqa: BLE001
        return datetime.utcnow().strftime("%H:%M:%S UTC")


_CUR_EMOJI = {"USD": ("usd", "🇺🇸"), "EGP": ("egp", "🇪🇬"), "TON": ("ton", "💎")}
_CUR_NAME = {"USD": "دولار", "EGP": "جنيه مصري", "TON": "TON"}
_BAR = "✦━━━━━━━━━━━━━━━━✦"


def _pe(name, fallback=""):
    """إيموجي مميز (premium) مع بديل عادي لو الموديول مش متاح."""
    try:
        from premium_emoji import e
        return e(name) or fallback
    except Exception:  # noqa: BLE001
        return fallback


def cur_icon(code):
    name, fallback = _CUR_EMOJI.get(code, ("sparkles", "✨"))
    return _pe(name, fallback)


def _fmt_value(value, code):
    if code == "TON":
        if abs(value) >= 1:
            return f"{value:,.4f}".rstrip("0").rstrip(".")
        s = f"{value:.6f}".rstrip("0").rstrip(".")
        return s if float(s) != 0 else "<0.000001"
    return fmt_number(value, "fiat")


def picker_text(amount):
    return (
        f"<blockquote>{_pe('sparkles', '✨')} <b>تحويل العملات</b></blockquote>\n"
        f"{_BAR}\n"
        f"{_pe('cash', '💵')} <b>المبلغ :</b> <code>{_esc(fmt_amount(amount))}</code>\n"
        f"{_pe('point_up', '👇')} <b>اختار العملة اللي المبلغ ده بها</b>"
    )


def result_text(amount, src, enabled, rates):
    lines = [
        f"<blockquote>{_pe('sparkles', '✨')} <b>تحويل العملات</b></blockquote>",
        f"{cur_icon(src)} <b>{_esc(fmt_amount(amount))}</b> {_CUR_NAME.get(src, src)}",
        _BAR,
    ]
    shown = 0
    for code in enabled:
        if code == src:
            continue
        value = convert(amount, src, code, rates)
        name = _CUR_NAME.get(code, code)
        if value is None:
            lines.append(f"<blockquote>{cur_icon(code)} <i>{name} غير متاح حالياً</i></blockquote>")
        else:
            lines.append(
                f"<blockquote>{cur_icon(code)} <b>{_esc(_fmt_value(value, code))}</b> {name}</blockquote>"
            )
        shown += 1
    if not shown:
        lines.append("<i>مفيش عملات تانية مفعّلة للتحويل.</i>")
    lines.append(_BAR)
    lines.append(f"{_pe('clock', '🕒')} <code>{_clock()}</code>")
    if getattr(rates, "egp_bid", None) and ("EGP" in enabled or src == "EGP"):
        lines.append(
            f"<i>الدولار P2P: بيع {rates.egp_bid:,.2f} | شراء {rates.egp_ask:,.2f} جنيه</i>"
        )
    if getattr(rates, "egp_official", False):
        lines.append("<i>⚠️ سعر الجنيه هنا رسمي (تعذر جلب سعر السوق لحظياً).</i>")
    if rates.stale:
        lines.append("<i>⚠️ بعض الأسعار من آخر تحديث ناجح (المصدر مؤقتاً مش راضي يرد).</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# شكل الخاص (زي TonKit): أزرار زرقا 2 في الصف -> سطر النتيجة + رجوع
# --------------------------------------------------------------------------

_SHORT_NAME = {"USD": "USDT", "EGP": "EGP", "TON": "Gram"}


def short_name(code):
    return _SHORT_NAME.get(code, code)


def _amt_plain(amount):
    s = fmt_amount(amount)
    return s + ".0" if "." not in s and "," not in s else s


def private_source_text(amount):
    """رقم من غير عملة: يختار العملة اللي المبلغ بها."""
    return f"<blockquote>{_pe('cash', '💵')} {_esc(_amt_plain(amount))}</blockquote>"


def private_choice_text(amount, src):
    """بعد تحديد العملة (مثلاً «1تون»): سطر المبلغ وتحته أزرار العملات."""
    return f"<blockquote>{cur_icon(src)} {_esc(_amt_plain(amount))} {short_name(src)}</blockquote>"


def private_result_text(amount, src, dst, rates):
    """«» 1.0 Gram = 80.41 EGP» أو None لو السعر مش متاح."""
    value = convert(amount, src, dst, rates)
    if value is None:
        return None
    return (
        f"<blockquote>» {_esc(_amt_plain(amount))} {short_name(src)} = "
        f"{_esc(_fmt_value(value, dst))} {short_name(dst)}</blockquote>"
    )
