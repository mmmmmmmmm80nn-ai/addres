"""أسعار نجوم تيليجرام واشتراك Premium من Fragment (بالتون) + تحويلها لدولار وجنيه.

مهم: Fragment مفيهوش API رسمي، فالموديول بيقرا صفحاته مباشرة. لو Fragment غيّر شكل
الصفحة ممكن الجلب يفشل، وساعتها البوت بيقول إنه مقدرش يجيب السعر (مبيألفش أرقام).
أي فشل بيتسجّل في اللوج بكلمة fragment_prices عشان نعدّل الـ regex بسهولة.
"""

import json
import re
import threading
import time
from html import escape as _esc

import requests

import currency_converter as cvt

_UA = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 12
_TTL = 120  # ثانية
_cache = {}
_lock = threading.Lock()

DEFAULT_STAR_PACKS = (50, 100, 250, 500, 1000, 2500, 5000)
PREMIUM_MONTHS = (3, 6, 12)

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬،", "0123456789.,,")


def _log(msg):
    print(f"fragment_prices: {msg}", flush=True)


def _cached(key, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    value = fn()
    if value is not None:
        with _lock:
            _cache[key] = (now, value)
    return value


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _get_page(path):
    r = requests.get("https://fragment.com" + path, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.text


def _api_hash(html):
    m = re.search(r'/api\?hash=([0-9a-fA-F]+)', html.replace("\\/", "/"))
    return m.group(1) if m else None


def _ton_values(html_fragment):
    """كل قيم التون المعروضة في جزء HTML (عناصر tm-value / icon-ton)."""
    vals = []
    for m in re.finditer(r'icon-ton[^>]*>\s*([\d][\d,]*(?:\.\d+)?)', html_fragment):
        v = _num(m.group(1))
        if v:
            vals.append(v)
    return vals


# ------------------------------------------------------------------ النجوم

def _fetch_star_price_ton(qty):
    page = _get_page("/stars/buy")
    h = _api_hash(page)
    if not h:
        _log("stars: api hash not found in page")
        return None
    headers = dict(_UA)
    headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://fragment.com",
        "Referer": "https://fragment.com/stars/buy",
    })
    r = requests.post(
        f"https://fragment.com/api?hash={h}",
        data={"quantity": str(int(qty)), "method": "updateStarsPrices"},
        headers=headers, timeout=_TIMEOUT,
    )
    r.raise_for_status()
    text = r.text
    try:
        data = json.loads(text)
    except ValueError:
        _log(f"stars: non-json response: {text[:200]!r}")
        return None
    # 1) حقول رقمية مباشرة
    for key in ("cur_price", "price", "amount", "ton_price"):
        if key in data:
            v = _num(data[key])
            if v:
                # بعض ردود Fragment بتبقى بالـ nanoTON
                return v / 1e9 if v > 1e6 else v
    # 2) HTML جوه الرد
    blob = json.dumps(data, ensure_ascii=False).replace("\\\"", '"').replace("\\/", "/")
    vals = _ton_values(blob)
    if vals:
        return vals[0]
    _log(f"stars: could not parse price from response: {text[:300]!r}")
    return None


def get_star_price_ton(qty):
    qty = int(qty)
    try:
        return _cached(("stars", qty), lambda: _fetch_star_price_ton(qty))
    except Exception as exc:  # noqa: BLE001
        _log(f"stars fetch failed: {exc!r}")
        return None


# ------------------------------------------------------------------ بريميوم

def _fetch_premium_prices_ton():
    page = _get_page("/premium/gift")
    out = {}
    flat = re.sub(r"\s+", " ", page)
    for months in PREMIUM_MONTHS:
        m = re.search(
            rf'\b{months}\s*months?\b.{{0,600}}?icon-ton[^>]*>\s*([\d][\d,]*(?:\.\d+)?)',
            flat, re.I,
        )
        if m:
            v = _num(m.group(1))
            if v:
                out[months] = v
    if not out:
        _log("premium: no prices parsed from /premium/gift")
        return None
    return out


def get_premium_prices_ton():
    try:
        return _cached(("premium",), _fetch_premium_prices_ton)
    except Exception as exc:  # noqa: BLE001
        _log(f"premium fetch failed: {exc!r}")
        return None


# ------------------------------------------------------------------ تحويل وعرض

from datetime import datetime

import config
from premium_emoji import e

_FAIL = "⚠️ <b>مقدرتش أجيب الأسعار دلوقتي.</b> جرب كمان شوية."


def _rates():
    return cvt.get_rates(["TON", "USD", "EGP"])


def _stamp():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Africa/Cairo"))
    except Exception:  # noqa: BLE001
        now = datetime.utcnow()
    return now.strftime("%p %I:%M:%S")


def _price_lines(usd, rates):
    """سطور السعر: دولار / جنيه / GRAM بإيموجي مميز."""
    out = [(e("usd"), f"${usd:,.2f}")]
    egp = cvt.convert(usd, "USD", "EGP", rates)
    ton = cvt.convert(usd, "USD", "TON", rates)
    if egp is not None:
        out.append((e("egp"), f"{egp:,.2f} جنيه"))
    if ton is not None:
        out.append((e("ton"), f"{ton:,.3f} GRAM"))
    return out


def _tree(lines):
    """يرسم السطور بشكل شجري ┣ ┗."""
    rows = []
    for i, (icon, text) in enumerate(lines):
        branch = "┗" if i == len(lines) - 1 else "┣"
        rows.append(f"{branch} {icon} <b>{text}</b>")
    return "\n".join(rows)


_BAR = "✦━━━━━━━━━━━━━━━━✦"
STAR_PACKS = (50, 100, 250, 500, 1000, 2500, 5000)


def stars_help_text():
    return (
        f"<blockquote>{e('star')} <b>أسعار نجوم تيليجرام</b></blockquote>\n"
        f"{_BAR}\n"
        f"{e('pencil')} <b>اكتب الكمية بالشكل ده:</b>\n"
        f"<code>100نجمة</code>\n\n"
        f"{e('cash')} هجيبلك السعر بـ {e('usd')} الدولار و{e('egp')} الجنيه و{e('ton')} التون\n"
        f"{e('point_up')} <b>أو اختار كمية جاهزة 👇</b>"
    )


def stars_text(qty=None, mode="buy"):
    if not qty:
        return stars_help_text()
    try:
        rates = _rates()
    except Exception as exc:  # noqa: BLE001
        _log(f"rates failed: {exc!r}")
        return _FAIL
    per = config.STARS_WITHDRAW_USD if mode == "withdraw" else config.STARS_BUY_USD
    label = "سعر السحب" if mode == "withdraw" else "سعر الشراء"
    q = int(qty)
    if mode == "both":
        buy_per = config.STARS_BUY_USD
        withdraw_per = config.STARS_WITHDRAW_USD
        parts = [
            f"<blockquote>{e('star')} <b>أسعار نجوم تيليجرام</b>\n"
            "<i>Telegram Stars · شراء وسحب</i></blockquote>",
            _BAR,
            f"{e('sparkles')} <b>الكمية:</b> <code>{q:,}</code> نجمة",
            f"<blockquote>{e('cash')} <b>سعر الشراء</b>\n"
            f"{_tree(_price_lines(buy_per * q, rates))}\n"
            f"{e('diamond')} النجمة الواحدة: <code>${buy_per:.3f}</code></blockquote>",
            f"<blockquote>{e('sparkles')} <b>سعر السحب</b>\n"
            f"{_tree(_price_lines(withdraw_per * q, rates))}\n"
            f"{e('diamond')} النجمة الواحدة: <code>${withdraw_per:.3f}</code></blockquote>",
            _BAR,
            f"{e('clock')} <b>آخر تحديث:</b> <code>{_stamp()}</code>",
        ]
        return "\n".join(parts)
    parts = [
        f"<blockquote>{e('star')} <b>أسعار نجوم تيليجرام</b>\n"
        f"<i>Telegram Stars · {label}</i></blockquote>",
        _BAR,
        f"{e('sparkles')} <b>الكمية :</b> <code>{q:,}</code> نجمة",
        f"<blockquote>{e('cash')} <b>{label}</b>\n{_tree(_price_lines(per * q, rates))}</blockquote>",
        _BAR,
        f"{e('diamond')} <b>سعر النجمة الواحدة:</b> <code>${per:.3f}</code>",
        f"{e('clock')} <b>آخر تحديث:</b> <code>{_stamp()}</code>",
    ]
    return "\n".join(parts)


def premium_text():
    try:
        rates = _rates()
    except Exception as exc:  # noqa: BLE001
        _log(f"rates failed: {exc!r}")
        return _FAIL
    live = get_premium_prices_ton() or {}
    ton_usd = rates.usd.get("TON")
    parts = [
        f"<blockquote>{e('star')} {e('diamond')} <b>اشتراك تليجرام بريميوم</b>\n"
        "<i>Telegram Premium · الأسعار حسب المدة</i></blockquote>",
        _BAR,
    ]
    for months in PREMIUM_MONTHS:
        if live.get(months) and ton_usd:
            usd = live[months] * ton_usd
        else:
            usd = config.PREMIUM_USD.get(months)
        if not usd:
            continue
        parts.append(
            f"<blockquote>{e('sparkles')} <b>{months} شهور بريميوم</b>\n"
            f"{_tree(_price_lines(usd, rates))}</blockquote>"
        )
    parts.append(_BAR)
    parts.append(f"{e('clock')} <b>آخر تحديث:</b> <code>{_stamp()}</code>")
    return "\n".join(parts)


# ------------------------------------------------------------------ تحليل الأوامر

def _norm(s):
    s = (s or "").strip().translate(_AR_DIGITS)
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", s).lower()


_STAR_WORDS = r"(?:ال)?(?:نجوم|نجمه|نجمات|ستارز|ستارس|stars?)"
_PRICE_WORDS = r"(?:سعر|اسعار|بكام|كام)"
_STARS_QTY_RE = re.compile(
    rf"^(?:{_PRICE_WORDS}\s+)?(\d[\d,]*)\s*{_STAR_WORDS}$"
)
_STARS_ALL_RE = re.compile(rf"^(?:{_PRICE_WORDS}\s+)?{_STAR_WORDS}$")
_PREMIUM_RE = re.compile(
    rf"^(?:{_PRICE_WORDS}\s+)?(?:ال)?(?:بريميوم|premium|تليجرام بريميوم|تيليجرام بريميوم|مميز|اشتراك بريميوم)$"
)


def parse_request(text):
    """يرجّع {'kind': 'stars', 'qty': int|None} أو {'kind': 'premium'} أو None."""
    t = _norm(text)
    if not t or len(t) > 40:
        return None
    m = _STARS_QTY_RE.match(t)
    if m:
        qty = int(m.group(1).replace(",", ""))
        if 1 <= qty <= 1_000_000:
            return {"kind": "stars", "qty": qty}
        return None
    if _STARS_ALL_RE.match(t):
        return {"kind": "stars", "qty": None}
    if _PREMIUM_RE.match(t):
        return {"kind": "premium"}
    return None


def build_text(request, mode="buy"):
    if request["kind"] == "premium":
        return premium_text()
    return stars_text(request.get("qty"), mode)


# ------------------------------------------------------------------ الشكل الجديد (جداول غنية + زراير Fragment)

FRAGMENT_PREMIUM_URL = "https://fragment.com/premium/gift"
FRAGMENT_STARS_URL = "https://fragment.com/stars/buy"
_MONTHS_AR = {3: "3 شهور", 6: "6 شهور", 12: "12 شهر"}


def _n(v, big=2, small=3):
    """رقم بخانتين لو >= 1، وبـ 3 خانات لو أصغر (عشان أسعار النجمة الواحدة متتقربش لصفر)."""
    return f"{v:,.{big}f}" if v >= 1 else f"{v:,.{small}f}"


def _rows(usd, rates, ton=None):
    """صفوف الجدول: (التسمية، القيمة) — دولار / تون / جنيه."""
    if ton is None:
        ton = cvt.convert(usd, "USD", "TON", rates)
    egp = cvt.convert(usd, "USD", "EGP", rates)
    rows = [("دولار - Usdt", _n(usd))]
    if ton is not None:
        rows.append(("جرام - تون", _n(ton)))
    if egp is not None:
        rows.append(("جنيه مصري", f"{egp:,.0f}" if egp >= 100 else f"{egp:,.2f}"))
    return rows


def _card(title_html, rows, note_html=""):
    """بيرجّع (rich_html, fallback_html): جدول بحدود + نسخة نصية لو الجداول مش متاحة."""
    table = "<table bordered>" + "".join(
        f"<tr><td><b>{label}</b></td><td><b>{value}</b></td></tr>" for label, value in rows
    ) + "</table>"
    rich = f"<p><b>{title_html}</b></p>{table}"
    plain = f"<blockquote><b>{title_html}</b>\n" + "\n".join(
        f"{label}: <b>{value}</b>" for label, value in rows
    ) + "</blockquote>"
    if note_html:
        rich += f"<p>{note_html}</p>"
        plain += f"\n{note_html}"
    return rich, plain


def premium_cards():
    """كارت لكل مدة (3/6/12 شهر): [(rich, fallback), ...] أو None لو الأسعار مش متاحة."""
    try:
        rates = _rates()
    except Exception as exc:  # noqa: BLE001
        _log(f"rates failed: {exc!r}")
        return None
    live = get_premium_prices_ton() or {}
    ton_usd = rates.usd.get("TON")
    cards = []
    for months in PREMIUM_MONTHS:
        ton = live.get(months) if ton_usd else None
        usd = ton * ton_usd if ton else config.PREMIUM_USD.get(months)
        if not usd:
            continue
        title = f"{e('gift')} بريميوم تيليجرام — {_MONTHS_AR[months]}"
        cards.append(_card(title, _rows(usd, rates, ton)))
    return cards or None


def stars_choice_text(qty):
    return (
        f"{e('star')} <b>النجوم : {int(qty):,} نجمة</b>\n"
        "اختر عملية من الاثنين ، سحب او شراء :"
    )


def stars_card(qty, mode="buy"):
    """كارت سعر الشراء أو السحب لكمية نجوم: (rich, fallback) أو None."""
    try:
        rates = _rates()
    except Exception as exc:  # noqa: BLE001
        _log(f"rates failed: {exc!r}")
        return None
    q = int(qty)
    withdraw = mode == "withdraw"
    per = config.STARS_WITHDRAW_USD if withdraw else config.STARS_BUY_USD
    title = f"{e('star')} سعر {'سحب' if withdraw else 'شراء'} النجوم (Fragment) — {q:,} نجمة"
    note = "✅ <b>ملاحظة:</b> السعر من منصة Fragment."
    return _card(title, _rows(per * q, rates), note)
