"""كشف محفظة TON: الرصيد + اليوزرات + الأرقام + هدايا تيليجرام (NFT Gifts) + باقي الـ NFT.

* المصدر الأساسي TonAPI، وبديل تلقائي toncenter لو الأول وقع أو اتحجب (429).
* كل طلب له timeout وإعادة محاولة محدودة، وفيه ميزانية وقت كلية، فالكشف مبيعلقش.
* الهدايا بتتعرف من صورتها (nft.fragment.com/gift) أو من سماتها (Model/Backdrop/Symbol)،
  مش من اسم الكوليكشن، عشان كل هدية ليها كوليكشن باسمها.
"""

import re
import threading
import time

import requests

TONAPI = "https://tonapi.io/v2"
TONCENTER = "https://toncenter.com/api/v3"

_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 12
_BUDGET_SECONDS = 40
_MAX_NFT_PAGES = 6          # 6 * 1000 = 6000 NFT كحد أقصى
_PAGE = 1000

_UA = {"User-Agent": "Mozilla/5.0 (compatible; WalletScan/2.0)", "Accept": "application/json"}

_rl_lock = threading.Lock()
_last_call = {"tonapi": 0.0, "toncenter": 0.0}


class ScanError(Exception):
    pass


def _throttle(name, gap):
    with _rl_lock:
        wait = _last_call[name] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[name] = time.time()


def _get_json(url, name, gap, headers=None, params=None, deadline=None):
    """GET مع إعادة محاولة على 429/5xx/انقطاع. يرجّع JSON أو يرفع ScanError."""
    last_exc = None
    for attempt in range(4):
        if deadline and time.time() > deadline:
            break
        _throttle(name, gap)
        try:
            r = requests.get(
                url, headers=headers or _UA, params=params,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(0.8 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as exc:
                last_exc = exc
                break
        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            try:
                pause = float(retry_after) if retry_after else 1.5 * (attempt + 1)
            except ValueError:
                pause = 1.5 * (attempt + 1)
            last_exc = ScanError(f"{name} HTTP {r.status_code}")
            time.sleep(min(pause, 4))
            continue
        # 4xx تانية (مثلاً 404 عنوان غلط) مفيش فايدة من التكرار
        raise ScanError(f"{name} HTTP {r.status_code}")
    raise ScanError(f"{name} failed: {last_exc!r}")


# ------------------------------------------------------------------ التصنيف

_NAME_NUM_RE = re.compile(r"^(.*?)\s*#\s*(\d+)\s*$")


def _attr_names(meta):
    names = set()
    for a in (meta.get("attributes") or []):
        if isinstance(a, dict):
            names.add(str(a.get("trait_type", "")).strip().lower())
    return names


def classify(name, collection, image, attrs):
    """يرجّع 'username' | 'number' | 'gift' | 'other'."""
    n = (name or "").strip()
    c = (collection or "").lower()
    img = (image or "").lower()
    if "telegram usernames" in c or n.startswith("@"):
        return "username"
    if "anonymous telegram numbers" in c or "anonymous number" in c or re.match(r"^\+?888[\d\s]*$", n):
        return "number"
    if "fragment.com/gift" in img or "nft.fragment.com" in img and "gift" in img:
        return "gift"
    if {"model", "backdrop", "symbol"} <= attrs:
        return "gift"
    if "telegram gift" in c:
        return "gift"
    return "other"


def gift_slug(name):
    m = _NAME_NUM_RE.match(name or "")
    if not m:
        return None
    base = re.sub(r"[^A-Za-z0-9]", "", m.group(1))
    if not base:
        return None
    return f"{base}-{m.group(2)}"


GIFT_EMOJI = {
    "spicedwine": "🍷", "timelessbook": "📕", "chillflame": "🔥", "lolpop": "🍭",
    "springbasket": "🧺", "jesterhat": "🃏", "bowtie": "🎀", "instantramen": "🍜",
    "plushpepe": "🐸", "heartlocket": "💖", "durovscap": "🧢", "preciouspeach": "🍑",
    "heroichelmet": "⛑️", "nailbracelet": "💅", "perfumebottle": "🧴", "irisring": "💍",
    "eternalrose": "🌹", "diamondring": "💎", "lootbag": "💰", "scaredcat": "🐱",
    "santahat": "🎅", "signetring": "💍", "toybear": "🧸", "lovecandle": "🕯️",
    "lunarsnake": "🐍", "homemadecake": "🎂", "jellybunny": "🐰", "partysparkler": "🎇",
    "snoopdogg": "🐶", "swisswatch": "⌚", "astralshard": "💠", "bondedring": "💍",
    "candycane": "🍬", "snowglobe": "🔮", "bigyear": "🎆", "lightsword": "⚔️",
    "bdaycandle": "🎂", "berrybox": "🍓", "bunnymuffin": "🧁", "coolcow": "🐮",
    "cookieheart": "🍪", "crystalball": "🔮", "easteregg": "🥚", "electricskull": "💀",
    "evileye": "🧿", "flyingbroom": "🧹", "ginger cookie": "🍪", "gingercookie": "🍪",
    "hangingstar": "⭐", "hexpot": "🍯", "holidaydrink": "🥂", "hypnolollipop": "🍭",
    "inputkey": "🔑", "jackinthebox": "🤡", "lushbouquet": "💐", "mightyarm": "💪",
    "minioscar": "🏆", "momentofglory": "🏅", "neckless": "📿", "pethtyp": "🐾",
    "poolfloat": "🛟", "restlessjar": "🫙", "sakuraflower": "🌸", "sharptongue": "👅",
    "skullflower": "💀", "sleighbell": "🔔", "snakebox": "🐍", "spyagaric": "🍄",
    "starnotepad": "📓", "tamagadget": "📟", "trappedheart": "❤️", "valentinebox": "💝",
    "vintagecigar": "🚬", "voodoodoll": "🪆", "westsidesign": "🤟", "whipcupcake": "🧁",
    "winterwreath": "🎄", "witchhat": "🧙", "xmasstocking": "🧦", "genielamp": "🪔",
    "kissedfrog": "🐸", "magicpotion": "🧪", "lowrider": "🚗", "iongem": "💎",
    "lamp": "🪔", "lunarsnake": "🐍", "lolpop": "🍭",
}


def gift_emoji(name):
    base = _NAME_NUM_RE.match(name or "")
    base = (base.group(1) if base else (name or ""))
    key = re.sub(r"[^a-z]", "", base.lower())
    return GIFT_EMOJI.get(key, "🎁")


def gift_title(name):
    m = _NAME_NUM_RE.match(name or "")
    return (m.group(1) if m else (name or "")).strip() or "Gift"


def _entry(kind, name, address, on_sale=False):
    name = (name or "").strip() or "بدون اسم"
    if kind == "username":
        e = {"kind": kind, "text": name if name.startswith("@") else "@" + name.split(".")[0], "url": None}
    elif kind == "number":
        e = {"kind": kind, "text": name, "url": None}
    elif kind == "gift":
        slug = gift_slug(name)
        e = {"kind": kind, "text": name, "url": f"https://t.me/nft/{slug}" if slug else None,
             "emoji": gift_emoji(name), "title": gift_title(name)}
    else:
        e = {"kind": kind, "text": name, "url": f"https://tonviewer.com/{address}" if address else None}
    e["on_sale"] = bool(on_sale)
    return e


# ------------------------------------------------------------------ TonAPI

def _tonapi_headers(api_key):
    h = dict(_UA)
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _via_tonapi(address, api_key, deadline):
    gap = 0.25 if api_key else 1.15
    headers = _tonapi_headers(api_key)
    acc = _get_json(f"{TONAPI}/accounts/{address}", "tonapi", gap, headers, deadline=deadline)
    if not isinstance(acc, dict) or "balance" not in acc:
        raise ScanError("tonapi: bad account response")
    balance = int(acc.get("balance") or 0) / 1e9

    items = []
    for page in range(_MAX_NFT_PAGES):
        data = _get_json(
            f"{TONAPI}/accounts/{address}/nfts", "tonapi", gap, headers,
            params={"limit": _PAGE, "offset": page * _PAGE, "indirect_ownership": "true"},
            deadline=deadline,
        )
        batch = (data or {}).get("nft_items") or []
        for it in batch:
            meta = it.get("metadata") or {}
            coll = (it.get("collection") or {}).get("name") or ""
            image = meta.get("image") or ""
            if not image:
                prev = it.get("previews") or []
                if prev:
                    image = (prev[-1] or {}).get("url") or ""
            kind = classify(meta.get("name") or "", coll, image, _attr_names(meta))
            items.append(_entry(kind, meta.get("name") or "", it.get("address"),
                                on_sale=bool(it.get("sale"))))
        if len(batch) < _PAGE:
            break
    return balance, items


# ------------------------------------------------------------------ toncenter (بديل)

def _via_toncenter(address, deadline):
    gap = 1.2
    info = _get_json(f"{TONCENTER}/addressInformation", "toncenter", gap,
                     params={"address": address, "use_v2": "false"}, deadline=deadline)
    balance = int((info or {}).get("balance") or 0) / 1e9
    items = []
    for page in range(_MAX_NFT_PAGES * 10):
        data = _get_json(f"{TONCENTER}/nft/items", "toncenter", gap,
                         params={"owner_address": address, "limit": 100, "offset": page * 100},
                         deadline=deadline)
        batch = (data or {}).get("nft_items") or []
        book = (data or {}).get("metadata") or {}

        def token_info(addr):
            entry = book.get(addr) or {}
            infos = entry.get("token_info") or []
            return infos[0] if infos else {}

        for it in batch:
            ti = token_info(it.get("address"))
            name = ti.get("name") or ""
            image = ti.get("image") or ""
            attrs = set()
            extra = ti.get("extra") or {}
            for a in (extra.get("attributes") or []):
                if isinstance(a, dict):
                    attrs.add(str(a.get("trait_type", "")).strip().lower())
            coll_name = (token_info(it.get("collection_address")) or {}).get("name") or ""
            kind = classify(name, coll_name, image, attrs)
            items.append(_entry(kind, name, it.get("address")))
        if len(batch) < 100:
            break
    return balance, items


# ------------------------------------------------------------------ سعر TON

def fetch_ton_usd(api_key=None):
    """سعر TON بالدولار: TonAPI أولًا ثم CoinGecko. يرجّع None لو الاتنين وقعوا."""
    try:
        import currency_converter as _cvt
        _p = _cvt.get_ton_usd()
        if _p:
            return _p
    except Exception:
        pass
    try:
        data = _get_json(f"{TONAPI}/rates", "tonapi", 0.25 if api_key else 1.15,
                         _tonapi_headers(api_key), params={"tokens": "ton", "currencies": "usd"},
                         deadline=time.time() + 8)
        return float(data["rates"]["TON"]["prices"]["USD"])
    except Exception:
        pass
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "the-open-network", "vs_currencies": "usd"}, timeout=(4, 8))
        return float(r.json()["the-open-network"]["usd"])
    except Exception:
        return None


# ------------------------------------------------------------------ الواجهة

def scan(address, api_key=None):
    """يرجّع dict(address, balance, usernames, numbers, gifts, others) أو يرفع ScanError."""
    deadline = time.time() + _BUDGET_SECONDS
    errors = []
    for fn in (lambda: _via_tonapi(address, api_key, deadline), lambda: _via_toncenter(address, deadline)):
        try:
            balance, items = fn()
            break
        except ScanError as exc:
            errors.append(str(exc))
            print(f"wallet_scan: {exc}", flush=True)
    else:
        raise ScanError("; ".join(errors))
    out = {"address": address, "balance": balance, "usd_price": None,
           "usernames": [], "numbers": [], "gifts": [], "others": [], "auctions": []}
    key = {"username": "usernames", "number": "numbers", "gift": "gifts", "other": "others"}
    for it in items:
        if it.get("on_sale"):
            out["auctions"].append(it)
        else:
            out[key[it["kind"]]].append(it)
    out["usd_price"] = fetch_ton_usd(api_key)
    return out
