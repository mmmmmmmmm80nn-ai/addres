from html import escape as html_escape

from telebot import types

def tg_emoji(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

EMOJI = {
    "folder": ("6039630677182254664", "\U0001F4C1"),
    "document": ("6034969813032374911", "\U0001F4C4"),
    "music": ("6037364759811068375", "\U0001F3B5"),
    "check": ("6041919344995209164", "\u2705"),
    "cross": ("5774077015388852135", "\u274C"),
    "new": ("5895669571058142797", "\U0001F195"),
    "diamond": ("5769406891289481208", "\U0001F48E"),
    "sparkles": ("5940660740758184142", "\u2728"),
    "star": ("6028338546736107668", "\u2B50"),
    "arrow_right": ("5895383238473421210", "\u27A1\uFE0F"),
    "arrow_left": ("6039519841256214245", "\u2B05\uFE0F"),
    "link": ("6028171274939797252", "\U0001F517"),
    "robot": ("6030400221232501136", "\U0001F916"),
    "image": ("6030466823290360017", "\U0001F5BC\uFE0F"),
    "lock": ("6037249452824072506", "\U0001F512"),
    "unlock": ("6037496202990194718", "\U0001F513"),
    "trash": ("6039522349517115015", "\U0001F5D1\uFE0F"),
    "eye": ("6037397706505195857", "\U0001F441\uFE0F"),
    "megaphone": ("6039450962865688331", "\U0001F4E3"),
    "bullhorn": ("6021418126061605425", "\U0001F4E2"),
    "bell": ("6039677157318332604", "\U0001F514"),
    "mute": ("6039569594157371705", "\U0001F515"),
    "book": ("6037286673010660132", "\U0001F4D6"),
    "chat": ("6021618194228187816", "\U0001F4AC"),
    "phone": ("6037418554276452311", "\U0001F4DE"),
    "people": ("5879905000972358125", "\U0001F465"),
    "person": ("5778145208411624388", "\U0001F464"),
    "gift": ("6037175527846975726", "\U0001F381"),
    "wave": ("6041921818896372382", "\U0001F44B"),
    "party": ("6041731551845159060", "\U0001F389"),
    "clap": ("5994417835630137549", "\U0001F44F"),
    "shield": ("6030537007350944596", "\U0001F6E1\uFE0F"),
    "upload": ("6043874504302661409", "\u2B06\uFE0F"),
    "download": ("5805382340519664323", "\u2B07\uFE0F"),
    "deleted_media": ("5873044297523140571", "\U0001F5D1\uFE0F"),
    "clock": ("5850317551090800862", "\u23F0"),
    "camera": ("5944969250086063864", "\U0001F4F7"),
    "point_up": ("5886583490434044162", "\u261D\uFE0F"),

    "bell_2": ("6039486778597970865", "\U0001F514"),
    "bell_3": ("6039636621416993073", "\U0001F514"),
    "camera_2": ("6030506650522096180", "\U0001F4F7"),
    "camera_3": ("6030506384234123289", "\U0001F4F7"),
    "camera_4": ("5944753741512052670", "\U0001F4F7"),
    "camera_5": ("5778549588172477436", "\U0001F4F7"),
    "camera_6": ("6048896442058150625", "\U0001F4F7"),
    "camera_7": ("6048808236314791508", "\U0001F4F7"),
    "camera_8": ("6048390817033228573", "\U0001F4F7"),
    "heart": ("6035127296598217030", "\u2764\uFE0F"),
    "heart_2": ("6037533152593842454", "\u2764\uFE0F"),
    "film": ("5945123916153360580", "\U0001F39E\uFE0F"),
    "film_2": ("5944777041709633960", "\U0001F39E\uFE0F"),
    "mic": ("6030722571412967168", "\U0001F3A4"),
    "mic_2": ("6030467682283818947", "\U0001F399\uFE0F"),
    "phone_2": ("6039605143601680423", "\U0001F4DE"),
    "phone_3": ("6039398100408209720", "\u260E\uFE0F"),
    "chat_2": ("6030512294109122096", "\U0001F4AC"),
    "chat_3": ("6030622631818956594", "\U0001F4AC"),
    "chat_4": ("6030784887093464891", "\U0001F4AC"),
    "robot_2": ("5983580310292402968", "\U0001F916"),
    "qb_robot": ("5291735300236664322", "\U0001F916"),
    "person_2": ("5891207662678317861", "\U0001F464"),
    "person_3": ("6035191085452497972", "\U0001F464"),
    "person_4": ("5893192487324880883", "\U0001F464"),
    "person_5": ("5904630315946611415", "\U0001F464"),
    "plus": ("6033108709213736873", "\u2795"),
    "speaking": ("6032653721853234759", "\U0001F5E3\uFE0F"),
    "people_2": ("6032609071373226027", "\U0001F465"),
    "people_3": ("6032594876506312598", "\U0001F465"),
    "people_4": ("6033125983572201397", "\U0001F465"),
    "thumbs_up": ("6041720006973067267", "\U0001F44D"),
    "wave_2": ("5985478698722136468", "\U0001F44B"),
    "zap": ("5884428842780594914", "\u26A1"),
    "pencil": ("5771847914477326786", "\u270F\uFE0F"),
    "letter_a": ("6030764546128352351", "\U0001F170\uFE0F"),
    "globe": ("5776233299424843260", "\U0001F310"),
    "diamond_2": ("6037083366438737901", "\U0001F48E"),

    "admin_panel": ("5818705028424141605", "\u2699\uFE0F"),

    "id_badge": ("5873044297523140571", "\U0001F194"),

    "ton_wallet": ("5258204546391351475", "\U0001F45B"),
    "ton_money": ("5258368777350816286", "\U0001F4B0"),
    "ton_users": ("5260399854500191689", "\U0001F464"),
    "ton_fire": ("5199457120428249992", "\U0001F525"),
    "ton_numbers": ("5258337316715373336", "\U0001F4DE"),
    "ton_gift": ("5301296193790308732", "\U0001F381"),
    "ton_other": ("5253959125838090076", "\U0001F47E"),

    "dash": ("5382360493161725288", "\u2796"),
    "wallet": ("5397829221605191505", "\U0001F45B"),
    "bang": ("5382224102180273389", "\u2755"),
    "star_2": ("5852582673893038533", "\u2B50\uFE0F"),
    "cash": ("5814331802529045304", "\U0001F4B5"),
    "cash_2": ("5201692367437974073", "\U0001F4B5"),
    "mobile": ("5404864841892508230", "\U0001F4F1"),

    "yt": ("5334681713316479679", "\u25B6\uFE0F"),
    "ig": ("5319160079465857105", "\U0001F4F8"),
    "fb": ("5323261730283863478", "\U0001F4D8"),
    "sc": ("5345844509412444249", "\U0001F7E0"),
    "pin": ("5346103513120258857", "\U0001F4CC"),
    "snap": ("5330248916224983855", "\U0001F47B"),

    "features_mgmt": ("5837100188669319064", "\u2699\uFE0F"),
    "welcome_hi": ("5388962171428297409", "\U0001F44B"),
    "welcome_person": ("5373012449597335010", "\U0001F464"),
    "welcome_tools": ("5233579750831713162", "\U0001F6E0\uFE0F"),

    # أيقونات صلاحيات الميزات (للجميع / للعميل / مغلق)
    "aud_all": ("5393579201141946670", "\U0001F465"),
    "aud_owner": ("5203965341440427191", "\U0001F451"),
    "aud_client": ("5296439530671330889", "\U0001F9D1\u200D\U0001F4BB"),
    "aud_closed": ("5375131156964468313", "\u274C"),

    # أعلام وعملات التحويل
    "egp": ("5913694831539916769", "\U0001F1EA\U0001F1EC"),
    "usd": ("5913463998522592692", "\U0001F1FA\U0001F1F8"),
    "ton": ("5264844355377604489", "\U0001F48E"),
}

def e(name: str) -> str:
    item = EMOJI.get(name)
    if not item:
        return ""
    emoji_id, fallback = item
    return tg_emoji(emoji_id, fallback)

def strip_premium(text: str) -> str:
    import re
    return re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", text)

# ---------------------------------------------------------------------------
# تلوين كل الأزرار + إيموجي مميز تلقائي (مفيش زرار شفاف تاني)
# ---------------------------------------------------------------------------
_VALID_STYLES = ("primary", "success", "danger")

_DANGER_WORDS = ("حذف", "مسح", "إغلاق", "اغلاق", "إلغاء", "الغاء", "حظر", "تعطيل", "إيقاف", "ايقاف",
                 "إعادة ضبط", "اعادة ضبط", "delete", "remove", "close", "cancel", "ban", "stop", "reset")
_SUCCESS_WORDS = ("حفظ", "تأكيد", "تاكيد", "تحقق", "تفعيل", "ابدأ", "ابدا", "بدء", "تم", "فتح", "إضافة",
                  "اضافة", "إرسال", "ارسال", "نعم", "موافق", "save", "confirm", "check", "enable",
                  "start", "open", "add", "send", "yes", "ok")

_ICON_RULES = (
    (("حذف", "مسح", "delete", "remove"), "trash"),
    (("إغلاق", "اغلاق", "إلغاء", "الغاء", "close", "cancel", "stop", "إيقاف", "ايقاف"), "cross"),
    (("حظر", "ban", "block"), "shield"),
    (("رجوع", "back", "عودة"), "arrow_right"),
    (("حفظ", "تأكيد", "تاكيد", "تحقق", "تم", "نعم", "موافق", "save", "confirm", "check", "ok"), "check"),
    (("إعدادات", "اعدادات", "ضبط", "settings"), "admin_panel"),
    (("رابط", "link", "url", "tonviewer", "فتح"), "link"),
    (("نسخ", "copy"), "document"),
    (("رسالة", "رسائل", "chat", "message"), "chat"),
    (("صوت", "voice", "صوتي", "mic"), "mic"),
    (("فيديو", "video"), "film"),
    (("صورة", "صور", "image", "photo"), "image"),
    (("محفظة", "wallet"), "wallet"),
    (("دولار", "جنيه", "عملة", "سعر", "usd", "egp", "price"), "cash"),
    (("هدية", "هدايا", "gift"), "gift"),
    (("نجوم", "نجمة", "star"), "star"),
    (("بريميوم", "premium", "ton"), "diamond"),
    (("اشتراك", "قناة", "قنوات", "channel"), "bullhorn"),
    (("إشعار", "اشعار", "تنبيه", "notify", "bell"), "bell"),
    (("عميل", "مستخدم", "user", "client"), "people"),
    (("ذكاء", "ai", "بوت", "bot"), "robot"),
    (("تحميل", "download"), "download"),
    (("رفع", "upload"), "upload"),
    (("إضافة", "اضافة", "add", "جديد", "new"), "plus"),
    (("لعبة", "لعب", "game"), "zap"),
    (("اتصال", "مكالمة", "call", "phone"), "phone"),
    (("وقت", "ساعة", "clock", "time"), "clock"),
    (("أمان", "امان", "حماية", "security"), "lock"),
)
_ICON_CYCLE = ("sparkles", "zap", "diamond", "star", "robot", "bell", "gift", "globe")

_LETTER_RE = None

def _has_letters(text):
    global _LETTER_RE
    if _LETTER_RE is None:
        import re
        _LETTER_RE = re.compile(r"[A-Za-z\u0600-\u06FF]")
    return bool(_LETTER_RE.search(text or ""))

def _auto_style(text, data):
    low = ((text or "") + " " + (data or "")).lower()
    for w in _DANGER_WORDS:
        if w in low:
            return "danger"
    for w in _SUCCESS_WORDS:
        if w in low:
            return "success"
    return "primary"

def _auto_icon_id(text, data):
    low = ((text or "") + " " + (data or "")).lower()
    for words, key in _ICON_RULES:
        for w in words:
            if w in low:
                return EMOJI[key][0]
    idx = sum(ord(c) for c in (text or "x")) % len(_ICON_CYCLE)
    return EMOJI[_ICON_CYCLE[idx]][0]

def _finalize_button_dict(btn_obj, d, data_key):
    """يضمن إن كل زرار ليه لون + أيقونة مميزة (إلا لو اتشال الأيقونات عمدًا للـ fallback)."""
    if not isinstance(d, dict):
        return d
    text = d.get("text") or ""
    data = str(d.get(data_key) or d.get("url") or "")
    stripped = getattr(btn_obj, "_pe_stripped", False)

    # الأيقونة
    icon_id = getattr(btn_obj, "icon_custom_emoji_id", None)
    if icon_id:
        if not stripped:
            d["icon_custom_emoji_id"] = str(icon_id)
    elif not stripped and _has_letters(text):
        d["icon_custom_emoji_id"] = _auto_icon_id(text, data)

    # اللون
    style = getattr(btn_obj, "style", None)
    if style not in _VALID_STYLES:
        style = _auto_style(text, data)
    d["style"] = style
    return d

def _patch_button_icon_support():
    if getattr(types.InlineKeyboardButton, "_pe_icon_patched", False):
        return
    orig_to_dict = types.InlineKeyboardButton.to_dict

    def patched_to_dict(self):
        return _finalize_button_dict(self, orig_to_dict(self), "callback_data")

    types.InlineKeyboardButton.to_dict = patched_to_dict
    types.InlineKeyboardButton._pe_icon_patched = True

def _patch_reply_button_icon_support():
    if getattr(types.KeyboardButton, "_pe_icon_patched", False):
        return
    orig_to_dict = types.KeyboardButton.to_dict

    def patched_to_dict(self):
        return _finalize_button_dict(self, orig_to_dict(self), "request_contact")

    types.KeyboardButton.to_dict = patched_to_dict
    types.KeyboardButton._pe_icon_patched = True

_patch_button_icon_support()
_patch_reply_button_icon_support()


# ---------------------------------------------------------------------------
# فلتر الإيموجي: أي إيموجي عادي في أي رسالة بيتحول لإيموجي مميز (أو بيتشال لو مالوش مقابل)
# ---------------------------------------------------------------------------
import json as _json
import re as _re

def _norm_emoji(ch):
    return ch.replace("\ufe0f", "")

_EXTRA_MAP = {
    "✅": "check", "✔": "check", "☑": "check", "👍": "thumbs_up", "👌": "thumbs_up", "🤝": "thumbs_up",
    "💪": "thumbs_up", "❌": "cross", "✖": "cross", "🚫": "cross", "⛔": "cross", "❎": "cross",
    "⚠": "bang", "❗": "bang", "❕": "bang", "❓": "bang", "❔": "bang", "‼": "bang",
    "💎": "diamond", "💠": "diamond_2", "⭐": "star", "🌟": "star_2", "🏆": "star_2", "🏅": "star_2",
    "✨": "sparkles", "🎇": "sparkles", "🎆": "sparkles", "⚡": "zap", "🔥": "ton_fire",
    "👇": "point_up", "☝": "point_up", "👆": "point_up", "👉": "arrow_right", "👈": "arrow_left",
    "💰": "ton_money", "💸": "cash", "💵": "cash", "💴": "cash_2", "💳": "wallet", "👛": "wallet",
    "🎁": "gift", "🎉": "party", "🎊": "party", "👋": "wave", "🙏": "clap", "👏": "clap",
    "🔔": "bell", "🔕": "mute", "🔇": "mute", "📢": "bullhorn", "📣": "megaphone",
    "🔒": "lock", "🔐": "lock", "🔑": "lock", "🔓": "unlock", "🛡": "shield", "🛟": "shield",
    "🗑": "trash", "🚮": "trash", "👁": "eye", "👀": "eye",
    "👤": "person", "👥": "people", "🧑‍💻": "person_2", "👑": "aud_owner",
    "⏰": "clock", "🕒": "clock", "⏳": "clock", "⌚": "clock", "⏱": "clock", "🕐": "clock",
    "⬅": "arrow_left", "➡": "arrow_right", "⬆": "upload", "⬇": "download", "🔼": "upload", "🔽": "download",
    "⚙": "admin_panel", "🛠": "welcome_tools", "🔧": "admin_panel",
    "🧾": "document", "📄": "document", "📃": "document", "📝": "pencil", "✏": "pencil",
    "📕": "book", "📓": "book", "📖": "book", "📚": "book", "📁": "folder", "📂": "folder",
    "❤": "heart", "💖": "heart_2", "💝": "heart_2", "💗": "heart_2", "💕": "heart_2", "🤍": "heart_2",
    "🤖": "robot", "🌐": "globe", "🔗": "link", "💬": "chat", "🗣": "speaking",
    "📞": "phone", "☎": "phone_3", "📱": "mobile", "📲": "mobile",
    "🎤": "mic", "🎙": "mic_2", "🎵": "music", "🎶": "music", "🎧": "music",
    "📷": "camera", "📸": "camera", "🖼": "image", "🎞": "film", "🎬": "film", "▶": "yt",
    "➕": "plus", "➖": "dash", "🆕": "new", "🆔": "id_badge", "🔁": "zap", "🔄": "zap",
    "🇺🇸": "usd", "🇪🇬": "egp", "💱": "cash", "🏢": "people_2", "🧿": "shield",
}
_UNI2KEY = {}
for _k, (_id, _fb) in EMOJI.items():
    if _fb:
        _UNI2KEY.setdefault(_norm_emoji(_fb), _k)
for _u, _k in _EXTRA_MAP.items():
    if _k in EMOJI:
        _UNI2KEY[_norm_emoji(_u)] = _k

# رموز ديكور/نص عادية (مش إيموجي) بنسيبها زي ما هي: ✦ ◦ ✓ ← → ⌫ ↔
_EMOJI_ONLY = set("✅❌⭕✨⭐⚡⌚⏰⏳⏸⏱▶❓❔❗❕➕➖♾⚠⚙⬅➡⬆⬇❤✏☎☝✔✖⚔⛑⛔❎‼☑⭐")

_EMOJI_RE = _re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2})"
    "|(?:[\U0001F300-\U0001FAFF][\ufe0f\U0001F3FB-\U0001F3FF]?"
    "(?:\u200d[\U0001F300-\U0001FAFF\u2640\u2642\u2695\u2696][\ufe0f]?)*)"
    "|(?:[\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF]\ufe0f?)"
)
_PROTECT_RE = _re.compile(
    r"(<tg-emoji\b[^>]*>.*?</tg-emoji>|<pre\b.*?</pre>|<code\b.*?</code>|<[^>]+>)", _re.S | _re.I
)

def _classify(token):
    """('map', key) | ('drop', None) | ('keep', None)"""
    key = _norm_emoji(token)
    if key in _UNI2KEY:
        return "map", _UNI2KEY[key]
    first = key[:1]
    if not first:
        return "keep", None
    cp = ord(first)
    if cp >= 0x1F300 or key in _EMOJI_ONLY or token.endswith("\ufe0f"):
        return "drop", None
    return "keep", None

def _repl_convert(m):
    token = m.group(0)
    kind, key = _classify(token)
    if kind == "map":
        return tg_emoji(EMOJI[key][0], token)
    if kind == "drop":
        return ""
    return token

def _repl_strip(m):
    kind, _ = _classify(m.group(0))
    return m.group(0) if kind == "keep" else ""

def convert_plain_emojis(html_text):
    """HTML text -> أي إيموجي عادي يتحول لـ <tg-emoji> مميز، وغير المتاح مقابل له يتشال."""
    if not html_text or not isinstance(html_text, str):
        return html_text
    parts = _PROTECT_RE.split(html_text)
    for i in range(0, len(parts), 2):
        if parts[i]:
            parts[i] = _EMOJI_RE.sub(_repl_convert, parts[i])
    return "".join(parts)

def strip_plain_emojis(text):
    if not text or not isinstance(text, str):
        return text
    return _EMOJI_RE.sub(_repl_strip, text)

def _clean_inline_markup(raw):
    try:
        data = _json.loads(raw)
    except Exception:
        return raw
    rows = data.get("inline_keyboard") if isinstance(data, dict) else None
    if not rows:
        return raw
    for row in rows:
        for b in row:
            t = b.get("text")
            if isinstance(t, str):
                cleaned = strip_plain_emojis(t).strip()
                if cleaned:      # لو الزرار إيموجي بس (زي لعبة XO) بنسيبه
                    b["text"] = cleaned
    return _json.dumps(data, ensure_ascii=False)

_TEXT_METHODS = {
    "sendMessage", "editMessageText", "editMessageCaption", "sendPhoto", "sendVideo",
    "sendDocument", "sendAudio", "sendVoice", "sendAnimation", "sendVideoNote",
}

def install_emoji_filter():
    """بيلف طلبات تليجرام عشان كل الرسائل (والأزرار) تطلع بإيموجي مميز بس."""
    from telebot import apihelper
    if getattr(apihelper, "_pe_emoji_filter", False):
        return
    orig = apihelper._make_request

    def wrapped(token, method_name, *args, **kwargs):
        try:
            params = kwargs.get("params")
            if params is None and len(args) >= 2:
                params = args[1]
            if isinstance(params, dict):
                if method_name in _TEXT_METHODS and params.get("parse_mode") == "HTML":
                    for key in ("text", "caption"):
                        if isinstance(params.get(key), str):
                            params[key] = convert_plain_emojis(params[key])
                elif method_name == "answerCallbackQuery" and isinstance(params.get("text"), str):
                    params["text"] = strip_plain_emojis(params["text"])
                rm = params.get("reply_markup")
                if isinstance(rm, str) and '"inline_keyboard"' in rm:
                    params["reply_markup"] = _clean_inline_markup(rm)
        except Exception:
            pass
        return orig(token, method_name, *args, **kwargs)

    apihelper._make_request = wrapped
    apihelper._pe_emoji_filter = True


def reply_btn(text, icon=None, style=None, **kwargs):
    """ReplyKeyboard button with a premium custom emoji icon and color."""
    button = types.KeyboardButton(text, **kwargs)
    if icon:
        emoji_id = EMOJI.get(icon, (icon, None))[0]
        button.icon_custom_emoji_id = str(emoji_id)
    if style:
        button.style = style
    return button

import os as _os
_BUTTON_ICONS = _os.environ.get("BUTTON_ICONS", "1").strip().lower() not in ("0", "false", "no", "off")

def btn(text, callback_data=None, url=None, icon=None, style=None, **kwargs):
    button = types.InlineKeyboardButton(text, callback_data=callback_data, url=url, **kwargs)
    if icon and _BUTTON_ICONS:
        emoji_id = EMOJI.get(icon, (icon, None))[0]
        button.icon_custom_emoji_id = str(emoji_id)
    # أي زرار لازم يبقى متلون: لو مفيش style (أو "default") بيتحدد تلقائي حسب نص الزرار
    button.style = style if style in _VALID_STYLES else None
    return button

def strip_button_icons(markup):
    """بيشيل الأيقونات المميزة (للـ fallback لو تليجرام رفضها) من غير ما يشيل الألوان."""
    rows = getattr(markup, "keyboard", None)
    if not rows:
        return markup
    for row in rows:
        for button in row:
            if hasattr(button, "icon_custom_emoji_id") or hasattr(button, "text"):
                button.icon_custom_emoji_id = None
                button._pe_stripped = True
    return markup

__all__ = ["convert_plain_emojis", "strip_plain_emojis", "install_emoji_filter", "e", "tg_emoji", "EMOJI", "strip_premium", "html_escape", "btn", "reply_btn", "strip_button_icons"]
