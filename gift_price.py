import re
import threading

import requests

GIFT_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/nft/([A-Za-z][A-Za-z0-9]*)-(\d+)", re.IGNORECASE
)

MARKET_LINK_RE = re.compile(
    r"(?:https?://)?(?:"
    r"market\.tonnel\.network/gift/|"
    r"portals-market\.com/gift/|"
    r"t\.me/tonnel_network_bot\?startapp=gift_|"
    r"t\.me/portals/market\?startapp=gift_"
    r")([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GiftPriceBot/1.0; +https://t.me)"}
_TIMEOUT = 10


def parse_gift_link(text):
    text = (text or "").strip()
    if not text:
        return None
    m = GIFT_LINK_RE.search(text)
    if m:
        return {"slug": m.group(1), "number": int(m.group(2)), "source": "telegram"}
    m = MARKET_LINK_RE.search(text)
    if m:
        return {"slug": m.group(1), "number": None, "source": "market"}
    return None


def slug_to_words(slug):
    words = re.findall(r"[A-Z][a-z0-9]*|[A-Z0-9]+|[a-z0-9]+", slug or "")
    return " ".join(words) if words else (slug or "")


def fetch_telegram_gift_meta(slug, number):
    url = f"https://t.me/nft/{slug}-{number}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    html = resp.text
    title_m = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    desc_m = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    if not title_m and not desc_m:
        return None

    result = {
        "title": title_m.group(1) if title_m else None,
        "description": desc_m.group(1) if desc_m else None,
        "url": url,
        "model": None,
        "backdrop": None,
        "symbol": None,
        "issued": None,
        "supply": None,
    }

    desc_text = (result["description"] or "").replace("&#10;", "\n")
    for key, label in (("model", "Model"), ("backdrop", "Backdrop"), ("symbol", "Symbol")):
        m = re.search(rf"{label}:\s*([^\n<]+)", desc_text)
        if m:
            result[key] = m.group(1).strip()

    qty_m = re.search(r"([\d][\d\s]*)\s*/\s*([\d][\d\s]*)\s*issued", html)
    if qty_m:
        try:
            result["issued"] = int(qty_m.group(1).replace(" ", "").replace("\u00a0", ""))
            result["supply"] = int(qty_m.group(2).replace(" ", "").replace("\u00a0", ""))
        except ValueError:
            pass

    return result


def _extract_floor_from_html(html):
    for pattern in (
        r'"floorPrice"\s*:\s*"?([\d.]+)"?',
        r'"floor_price"\s*:\s*"?([\d.]+)"?',
        r'"minPrice"\s*:\s*"?([\d.]+)"?',
    ):
        m = re.search(pattern, html)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _try_tonnel_public_price(slug):
    url = f"https://market.tonnel.network/gift/{slug}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    floor = _extract_floor_from_html(resp.text)
    if floor is None:
        return None
    return {"source": "Tonnel", "floor_ton": floor, "url": url}


def _try_portals_public_price(slug):
    url = f"https://portals-market.com/gift/{slug}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    floor = _extract_floor_from_html(resp.text)
    if floor is None:
        return None
    return {"source": "Portals", "floor_ton": floor, "url": url}


_TG_GIFTS_CLIENT = None
_TG_GIFTS_LOCK = threading.Lock()

_MARKET_LABELS = {"tgmrkt": "TGMrkt", "getgems": "GetGems", "fragment": "Fragment"}


def _get_tg_gifts_client():
    global _TG_GIFTS_CLIENT
    with _TG_GIFTS_LOCK:
        if _TG_GIFTS_CLIENT is None:
            try:
                from TelegramGifts import TelegramGifts
            except Exception:
                return None
            try:
                _TG_GIFTS_CLIENT = TelegramGifts(cache_mode="http", asset_mode="lazy")
            except TypeError:
                try:
                    _TG_GIFTS_CLIENT = TelegramGifts()
                except Exception:
                    return None
            except Exception:
                return None
        return _TG_GIFTS_CLIENT


def _to_float(value):
    try:
        f = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _get_field(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _gift_id_candidates(slug):
    words = slug_to_words(slug)
    out = []
    for c in (words.lower().replace(" ", "_"), words, slug):
        if c and c not in out:
            out.append(c)
    return out


def _clean_model_name(model):
    return re.sub(r"\s*\([\d.,]+%\)\s*$", "", (model or "").strip())


def _try_telegramgifts_price(slug, model=None):
    client = _get_tg_gifts_client()
    if client is None:
        return None

    candidates = _gift_id_candidates(slug)
    model_name = _clean_model_name(model)

    if model_name:
        for gid in candidates:
            try:
                details = client.get_model_details(gid, model_name)
            except Exception:
                details = None
            price = _to_float(_get_field(details, "price_ton"))
            if price:
                return {
                    "source": f"TelegramGifts — أقل سعر لموديل {model_name}",
                    "floor_ton": price,
                    "url": None,
                    "scope": "model",
                }

    for name in candidates:
        try:
            info = client.get_gift(name)
        except Exception:
            info = None
        if not info:
            continue
        prices = _get_field(info, "prices")
        if not isinstance(prices, dict):
            continue
        best = None
        for key, value in prices.items():
            key = str(key)
            if not key.endswith("_price_ton"):
                continue
            p = _to_float(value)
            if p and (best is None or p < best[0]):
                best = (p, key[: -len("_price_ton")])
        if best:
            market = _MARKET_LABELS.get(best[1], best[1])
            return {
                "source": f"TelegramGifts ({market}) — أقل سعر للكولكشن كله مش للموديل",
                "floor_ton": best[0],
                "url": None,
                "scope": "collection",
            }
    return None


_PRICE_SOURCES = (_try_tonnel_public_price, _try_portals_public_price)


def fetch_gift_price(slug, model=None):
    try:
        result = _try_telegramgifts_price(slug, model)
    except Exception:
        result = None
    if result:
        return result
    for fn in _PRICE_SOURCES:
        try:
            result = fn(slug)
        except Exception:
            result = None
        if result:
            return result
    return None


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
