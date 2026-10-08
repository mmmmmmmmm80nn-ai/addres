import re
import time

import requests

FRAGMENT_USERNAME_URL = "https://fragment.com/username/{u}"
TONAPI_BASE = "https://tonapi.io/v2"

USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")
_ADDRESS_RE = re.compile(r"\b(?:EQ|UQ)[A-Za-z0-9_\-]{46}\b")

_USERNAME_HINTS = ("telegram usernames", "usernames")
_NUMBER_HINTS = ("anonymous number", "anonymous numbers", "numbers")
_GIFT_HINTS = ("gift",)


def is_probable_username(text):
    text = (text or "").strip()
    return bool(USERNAME_RE.match(text))


def is_wallet_address(text):
    text = (text or "").strip()
    return bool(_ADDRESS_RE.fullmatch(text))


def resolve_fragment_wallet(username, timeout=10):
    handle = username.lstrip("@").strip()
    if not handle:
        return None
    try:
        resp = requests.get(
            FRAGMENT_USERNAME_URL.format(u=handle),
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; WalletLookupBot/1.0)"},
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    m = _ADDRESS_RE.search(resp.text)
    return m.group(0) if m else None


def _headers(api_key=None):
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def fetch_ton_usd_price(timeout=8):
    try:
        import currency_converter as _cvt
        _p = _cvt.get_ton_usd()
        if _p:
            return _p
    except Exception:
        pass
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "the-open-network", "vs_currencies": "usd"},
            timeout=timeout,
        )
        return float(r.json()["the-open-network"]["usd"])
    except Exception:
        return None


def fetch_wallet_report(address, api_key=None, timeout=12):
    try:
        acc = requests.get(
            f"{TONAPI_BASE}/accounts/{address}", headers=_headers(api_key), timeout=timeout
        ).json()
    except Exception:
        return None

    try:
        nfts_resp = requests.get(
            f"{TONAPI_BASE}/accounts/{address}/nfts",
            headers=_headers(api_key),
            params={"limit": 1000, "indirect_ownership": "false"},
            timeout=timeout,
        ).json()
    except Exception:
        nfts_resp = {}

    balance_nano = int(acc.get("balance", 0) or 0)
    balance_ton = balance_nano / 1_000_000_000

    usernames, numbers, gifts, others = [], [], [], []
    for item in nfts_resp.get("nft_items", []) or []:
        collection = (item.get("collection") or {}).get("name", "") or ""
        coll_l = collection.lower()
        meta = item.get("metadata") or {}
        name = meta.get("name") or item.get("index") or "بدون اسم"
        if any(h in coll_l for h in _USERNAME_HINTS):
            usernames.append(name)
        elif any(h in coll_l for h in _NUMBER_HINTS):
            numbers.append(name)
        elif any(h in coll_l for h in _GIFT_HINTS):
            gifts.append(name)
        else:
            others.append(name)

    return {
        "address": address,
        "balance_ton": balance_ton,
        "usernames": usernames,
        "numbers": numbers,
        "gifts": gifts,
        "others": others,
        "fetched_at": int(time.time()),
    }
