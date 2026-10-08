import asyncio
import ast
import base64
import hashlib
import io
import json
import contextvars
import functools
import operator
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import builtins
import requests
import telebot
from telebot import types, apihelper
import yt_dlp
import universal_downloader as ud

import config
import database as db
import premium_emoji as pe
import proxy_pool
import wallet_lookup
import wallet_xray
import wallet_scan
import gift_price
import currency_converter as cvt
import fragment_prices as frag
import help_pages
import scam_channel_checker as scam_checker
import binance_hub
from premium_emoji import e, html_escape

_print_lock = threading.Lock()
_orig_print = builtins.print

_VERBOSE_LOGS = os.environ.get("BOT_VERBOSE", "0") == "1"
_ERROR_MARKERS = (
    "error", "failed", "exception", "traceback", "crash", "unhandled", "timed out", "refused",
    "فشل", "خطأ", "عطل", "اخطاء", "أخطاء",
)
_QUIET_MARKERS = ("network hiccup", "telegram notice", "not modified", "retrying", "retry")

def _locked_print(*args, **kwargs):
    # اللوج على السيرفر: مفيش غير الأخطاء الحقيقية (مفيش شات ولا أوقات ولا بيانات ناس)
    if not _VERBOSE_LOGS and kwargs.get("file") is None:
        try:
            text = " ".join(str(a) for a in args).lower()
        except Exception:
            text = ""
        if any(m in text for m in _QUIET_MARKERS) or not any(m in text for m in _ERROR_MARKERS):
            return
    with _print_lock:
        _orig_print(*args, **kwargs)

def _thread_excepthook(args):
    import traceback
    print(
        f"THREAD ERROR in {getattr(args.thread, 'name', '?')}: "
        + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        flush=True,
    )

threading.excepthook = _thread_excepthook

builtins.print = _locked_print

# num_threads كبير: قبل كده كان 2 بس فكانت الرسائل بتستنى في الطابور (ده سبب البطء)
bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="HTML", num_threads=int(os.environ.get("BOT_THREADS", "16")))
pe.install_emoji_filter()
db.init_db()

# ====================== كل زرار بيعدّل نفس الرسالة (من غير رسائل جديدة) ======================
# أي زرار بيتداس: أول رسالة البوت كان هيبعتها لنفس الشات بتتحول لتعديل على الرسالة اللي اتداس عليها.
_INPLACE = contextvars.ContextVar("inplace_ctx", default=None)
_INPLACE_SKIP = {"cb_backup_export", "cb_adm_backup_export", "cb_features_send",
                 "cb_copy_download", "cb_check_business_link"}
_INPLACE_KW = {"reply_markup", "parse_mode", "link_preview_options", "disable_web_page_preview", "entities"}
_INPLACE_CACHE = {}  # (chat_id, message_id) -> {"text", "markup"}  (عشان زرار رجوع)


def _inplace_enter(call, handler_name):
    msg = getattr(call, "message", None)
    if msg is None or handler_name in _INPLACE_SKIP:
        return None
    if getattr(getattr(msg, "chat", None), "type", "") != "private":
        return None
    if getattr(msg, "business_connection_id", None) or getattr(call, "inline_message_id", None):
        return None
    if getattr(msg, "content_type", "") != "text":
        return None
    return _INPLACE.set({"chat_id": msg.chat.id, "message_id": msg.message_id, "message": msg, "used": False})


def _inplace_remember(st):
    msg = st["message"]
    raw = getattr(msg, "json", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    markup = raw.get("reply_markup") if isinstance(raw, dict) else None
    text = getattr(msg, "html_text", None) or getattr(msg, "text", None)
    if len(_INPLACE_CACHE) > 500:
        _INPLACE_CACHE.pop(next(iter(_INPLACE_CACHE)))
    _INPLACE_CACHE[(st["chat_id"], st["message_id"])] = {"text": text, "markup": markup}


def _inplace_try_send(chat_id, text, kwargs):
    """لو إحنا جوه ضغطة زرار وده أول إرسال لنفس الشات: عدّل الرسالة بدل ما تبعت جديدة."""
    st = _INPLACE.get()
    if not st or st["used"] or chat_id != st["chat_id"]:
        return None
    if not isinstance(text, str) or not text.strip() or set(kwargs) - _INPLACE_KW:
        return None
    markup = kwargs.get("reply_markup")
    if markup is not None and not isinstance(markup, types.InlineKeyboardMarkup):
        return None
    st["used"] = True
    kw = dict(kwargs)
    if markup is None:
        # رسالة من غير أزرار (طلب إدخال/نتيجة): نحط زرار رجوع يرجّع الصفحة اللي كانت قدامه
        _inplace_remember(st)
        back = types.InlineKeyboardMarkup()
        back.add(pe.btn(" رجوع", callback_data="ip_back", icon="arrow_left", style="danger"))
        kw["reply_markup"] = back
    try:
        res = safe_edit_message_text(text, chat_id, st["message_id"], **kw)
    except Exception as exc:
        print(f"in-place edit failed, sending new message: {str(exc)[:160]}", flush=True)
        return None
    return res if res is not None else st["message"]


_orig_callback_query_handler = bot.callback_query_handler


def _inplace_callback_query_handler(func=None, **kwargs):
    register = _orig_callback_query_handler(func, **kwargs)

    def decorator(handler):
        @functools.wraps(handler)
        def inner(call, *a, **k):
            token = _inplace_enter(call, getattr(handler, "__name__", ""))
            try:
                return handler(call, *a, **k)
            finally:
                if token is not None:
                    _INPLACE.reset(token)
        register(inner)
        return handler
    return decorator


bot.callback_query_handler = _inplace_callback_query_handler


def _expired_business_callback(call):
    message = getattr(call, "message", None)
    bc_id = getattr(message, "business_connection_id", None) if message else None
    if not bc_id:
        return False
    connection = db.get_business_connection(bc_id) or {}
    owner_id = connection.get("owner_user_id")
    return bool(owner_id and not is_admin(owner_id) and trial_expired(owner_id))


@bot.callback_query_handler(func=_expired_business_callback)
def cb_expired_business_callback(call):
    connection = db.get_business_connection(call.message.business_connection_id) or {}
    owner_id = int(connection.get("owner_user_id") or call.from_user.id)
    bot.answer_callback_query(call.id, access_expired_alert(owner_id), show_alert=True)


_BENIGN_TG_ERRORS = (
    "query is too old",
    "query id is invalid",
    "message is not modified",
    "message to edit not found",
    "message can't be edited",
    "message to delete not found",
    "message_id_invalid",
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "have no rights to send a message",
    "business_peer_invalid",
    "business_connection_invalid",
)

def _is_benign_tg_error(exc):
    msg = str(exc).lower()
    return any(k in msg for k in _BENIGN_TG_ERRORS)

_orig_answer_callback_query = bot.answer_callback_query

def _safe_answer_callback_query(*args, **kwargs):
    try:
        return _orig_answer_callback_query(*args, **kwargs)
    except Exception as exc:
        if _is_benign_tg_error(exc):
            return False
        print(f"answer_callback_query failed: {exc}", flush=True)
        return False

bot.answer_callback_query = _safe_answer_callback_query

def _strip_kwargs_icons(kwargs):
    kwargs = dict(kwargs)
    markup = kwargs.get("reply_markup")
    if markup is not None:
        kwargs["reply_markup"] = pe.strip_button_icons(markup)
    return kwargs

def safe_send_message(chat_id, text, **kwargs):
    inplace = _inplace_try_send(chat_id, text, kwargs)
    if inplace is not None:
        return inplace
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        return bot.send_message(chat_id, pe.strip_premium(text), **_strip_kwargs_icons(kwargs))

def safe_send_photo(chat_id, photo, caption="", **kwargs):
    try:
        return bot.send_photo(chat_id, photo, caption=caption, **kwargs)
    except Exception:
        return bot.send_photo(
            chat_id, photo, caption=pe.strip_premium(caption), **_strip_kwargs_icons(kwargs)
        )

def safe_send_video(chat_id, video, caption="", **kwargs):
    try:
        return bot.send_video(chat_id, video, caption=caption, **kwargs)
    except Exception:
        return bot.send_video(
            chat_id, video, caption=pe.strip_premium(caption), **_strip_kwargs_icons(kwargs)
        )

def safe_send_business_message(chat_id, business_connection_id, text, **kwargs):
    kwargs = dict(kwargs)
    kwargs["business_connection_id"] = business_connection_id
    _remember_bot_business_text(business_connection_id, chat_id, text)
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as exc:
        if _is_benign_tg_error(exc):
            # BUSINESS_PEER_INVALID: العميل مش متاح للرد من الاتصال ده (حظر/صلاحيات) - إعادة المحاولة مش هتفيد
            print(f"business send skipped chat={chat_id}: {exc}", flush=True)
            return None
        print(f"Business subscription message failed: {exc}", flush=True)
        fallback = _strip_kwargs_icons(kwargs)
        fallback["business_connection_id"] = business_connection_id
        try:
            return bot.send_message(chat_id, pe.strip_premium(text), **fallback)
        except Exception as fallback_exc:
            print(f"Business subscription fallback failed: {fallback_exc}", flush=True)
            return None

def safe_edit_message_text(text, chat_id, message_id, **kwargs):
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as exc:
        if _is_benign_tg_error(exc):
            return None
        try:
            return bot.edit_message_text(
                pe.strip_premium(text), chat_id, message_id, **_strip_kwargs_icons(kwargs)
            )
        except Exception as exc2:
            if _is_benign_tg_error(exc2):
                return None
            raise

def _edit_prompt_or_send(chat_id, message_id, text, **kwargs):
    """Update the existing bot prompt when possible; send only if there is no anchor."""
    if message_id:
        try:
            return safe_edit_message_text(text, chat_id, message_id, **kwargs)
        except Exception as exc:
            print(f"prompt edit failed, sending fallback: {type(exc).__name__}", flush=True)
    return safe_send_message(chat_id, text, **kwargs)

def delete_business_message(business_connection_id, message_id):
    try:
        return apihelper.delete_business_messages(
            config.BOT_TOKEN, business_connection_id, [int(message_id)]
        )
    except Exception as exc:
        print(f"Business delete failed: {exc}")
        return False

MUTE_PERM_NOTICE_INTERVAL = 3600
_MUTE_PERM_NOTICE_AT = {}
_NON_PERMISSION_DELETE_ERRORS = (
    "message to delete not found",
    "message_id_invalid",
    "message can't be deleted",
)

def delete_business_message_checked(business_connection_id, message_id):
    try:
        apihelper.delete_business_messages(
            config.BOT_TOKEN, business_connection_id, [int(message_id)]
        )
        return True, ""
    except Exception as exc:
        print(f"Business delete failed: {exc}")
        return False, str(exc)

def delete_error_is_permission(error_text):
    low = (error_text or "").lower()
    return not any(marker in low for marker in _NON_PERMISSION_DELETE_ERRORS)

def business_delete_rights_missing(business_connection_id):
    try:
        connection = bot.get_business_connection(business_connection_id)
        rights = getattr(connection, "rights", None)
        if rights is None:
            return False
        return getattr(rights, "can_delete_all_messages", None) is False
    except Exception:
        return False

def notify_mute_permission_missing(owner_chat_id, owner_id, client_name=None, detail=None, force=False):
    now = time.time()
    if not force and now - _MUTE_PERM_NOTICE_AT.get(owner_id, 0) < MUTE_PERM_NOTICE_INTERVAL:
        return False
    _MUTE_PERM_NOTICE_AT[owner_id] = now
    lines = [
        f"{e('cross')} <b>الكتم مش شغال دلوقتي.</b>",
        "",
        f"{e('lock')} <b>لازم تدّي البوت صلاحية «حذف الرسائل» عشان يقدر يمسح رسائل العميل المكتوم.</b>",
        "",
        f"{e('point_up')} <b>افتح: Settings ← Telegram Business ← Chatbots ← صلاحيات البوت، وفعّل «حذف الرسائل».</b>",
    ]
    if client_name:
        lines += ["", f"{e('person')} <b>العميل المكتوم:</b> <b>{html_escape(client_name)}</b>"]
    if detail:
        lines += ["", f"<i>تفاصيل الخطأ: {html_escape(str(detail)[:200])}</i>"]
    try:
        safe_send_message(owner_chat_id, "\n".join(lines))
        return True
    except Exception as exc:
        print(f"mute permission notice failed: {exc}", flush=True)
        return False

def delete_business_messages_bulk(business_connection_id, message_ids):
    ids = [int(i) for i in message_ids]
    deleted_batches = 0
    had_error = False
    last_error = ""
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            apihelper.delete_business_messages(config.BOT_TOKEN, business_connection_id, chunk)
            deleted_batches += len(chunk)
        except Exception as exc:
            had_error = True
            last_error = str(exc)
            print(f"Bulk business delete failed for chunk: {exc}")
    return deleted_batches, had_error, last_error

def pin_business_message(chat_id, business_connection_id, message_id, disable_notification=True):
    try:
        return bot.pin_chat_message(
            chat_id, message_id,
            business_connection_id=business_connection_id,
            disable_notification=disable_notification,
        )
    except Exception as exc:
        print(f"Business pin failed: {exc}")
        return False

def unpin_business_message(chat_id, business_connection_id, message_id=None):
    try:
        return bot.unpin_chat_message(
            chat_id, message_id=message_id,
            business_connection_id=business_connection_id,
        )
    except Exception as exc:
        print(f"Business unpin failed: {exc}")
        return False

pending_replies = {}

# ===================== إلغاء لكل خطوة بتستنى ردك =====================
# أي مكان البوت بيستنى فيه رسالتك (اكتب اسم/رقم/رابط...) بقى فيه:
#  1) زرار «إلغاء» تحت الرسالة تلقائي
#  2) كلمة «الغاء / cancel / /cancel» بتلغي
#  3) أي أمر بيبدأ بـ / (زي /admin) أو كلمة «ادمن» بتقطع الانتظار وتتنفذ عادي
#  4) الانتظار بينتهي لوحده بعد 10 دقايق
_STEP_TTL = 600
_STEP_CANCEL_WORDS = {"الغاء", "إلغاء", "cancel", "/cancel", "الغاء الامر", "الغاء الأمر", "خروج"}
_STEP_ADMIN_WORDS = {"ادمن", "أدمن", "admin", "لوحة الادمن", "لوحة الأدمن", "لوحه الادمن"}
_STEP_CANCEL_DATA = "cancel_step"

def _step_norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())

def _clear_pending_state(user_id):
    for _d in (pending_replies, ):
        try:
            _d.pop(user_id, None)
        except Exception:
            pass
    for _name in ("_ADM_PENDING_BC",):
        _d = globals().get(_name)
        if isinstance(_d, dict):
            _d.pop(user_id, None)

def _attach_cancel_button(msg):
    try:
        if msg is None or getattr(getattr(msg, "from_user", None), "is_bot", True) is False:
            return   # مش رسالة البوت نفسه: مينفعش نعدلها
        rows = []
        existing = getattr(msg, "reply_markup", None)
        for row in (getattr(existing, "keyboard", None) or []):
            for b in row:
                label = _step_norm(getattr(b, "text", "")) if not isinstance(b, dict) else _step_norm(b.get("text"))
                data = (getattr(b, "callback_data", None) if not isinstance(b, dict) else b.get("callback_data")) or ""
                if "الغاء" in label or "إلغاء" in label or "cancel" in label or "cancel" in str(data).lower():
                    return   # فيه زرار إلغاء أصلًا
            rows.append(row)
        kb = types.InlineKeyboardMarkup()
        for row in rows:
            kb.row(*row)
        kb.add(pe.btn("إلغاء", callback_data=_STEP_CANCEL_DATA, icon="cross", style="danger"))
        kw = {}
        bc = getattr(msg, "business_connection_id", None)
        if bc:
            kw["business_connection_id"] = bc
        bot.edit_message_reply_markup(msg.chat.id, msg.message_id, reply_markup=kb, **kw)
    except Exception as exc:
        if not _is_benign_tg_error(exc):
            print(f"cancel button attach skipped: {str(exc)[:120]}", flush=True)

_orig_register_next_step_handler = bot.register_next_step_handler

def _guarded_register_next_step_handler(message, callback, *args, **kwargs):
    if message is None:
        return None
    registered_at = time.time()

    def guarded(m, *a, **k):
        text = _step_norm(getattr(m, "text", "") if getattr(m, "content_type", "") == "text" else "")
        uid = getattr(getattr(m, "from_user", None), "id", None)
        # 1) أمر (/admin /start ...) أو كلمة ادمن للمشرفين: اقطع الانتظار ونفّذه عادي
        if text.startswith("/") and text.split()[0] not in ("/cancel",) or (
            text in _STEP_ADMIN_WORDS and uid is not None and is_staff(uid)
        ):
            _clear_pending_state(uid)
            try:
                bot.process_new_messages([m])
            except Exception as exc:
                print(f"step interrupt failed: {exc}", flush=True)
            return None
        # 2) إلغاء
        if text in _STEP_CANCEL_WORDS:
            _clear_pending_state(uid)
            try:
                bot.send_message(m.chat.id, "تم الإلغاء.")
            except Exception:
                pass
            return None
        # 3) انتهت مهلة الانتظار: عامل الرسالة كرسالة عادية
        if time.time() - registered_at > _STEP_TTL:
            _clear_pending_state(uid)
            try:
                bot.process_new_messages([m])
            except Exception as exc:
                print(f"expired step reprocess failed: {exc}", flush=True)
            return None
        return callback(m, *a, **k)

    result = _orig_register_next_step_handler(message, guarded, *args, **kwargs)
    threading.Thread(target=_attach_cancel_button, args=(message,), daemon=True).start()
    return result

bot.register_next_step_handler = _guarded_register_next_step_handler

@bot.callback_query_handler(func=lambda c: c.data == _STEP_CANCEL_DATA)
def cb_cancel_step(call):
    try:
        _clear_pending_state(call.from_user.id)
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except Exception:
        pass
    try:
        bot.answer_callback_query(call.id, "تم الإلغاء.")
    except Exception:
        pass
    try:
        bc = getattr(call.message, "business_connection_id", None)
        if bc:
            delete_business_message(bc, call.message.message_id)
        else:
            bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=types.InlineKeyboardMarkup())
        except Exception:
            pass

@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    _clear_pending_state(message.from_user.id)
    try:
        bot.clear_step_handler_by_chat_id(message.chat.id)
    except Exception:
        pass
    safe_send_message(message.chat.id, f"{e('check')} <b>تم الإلغاء.</b>")

DELETED_MEDIA_CACHE = {}
PIN_UNDO_CACHE = {}
BOT_SENT_BUSINESS_TEXT = {}
BOT_SENT_BUSINESS_TEXT_GUARD = threading.Lock()

def _remember_bot_business_text(bc_id, chat_id, text):
    if not text:
        return
    now = time.time()
    key = (bc_id, int(chat_id), str(text).strip())
    with BOT_SENT_BUSINESS_TEXT_GUARD:
        for old_key, old_time in list(BOT_SENT_BUSINESS_TEXT.items()):
            if now - old_time > 180:
                BOT_SENT_BUSINESS_TEXT.pop(old_key, None)
        BOT_SENT_BUSINESS_TEXT[key] = now

def _is_recent_bot_business_text(bc_id, chat_id, text):
    if not text:
        return False
    key = (bc_id, int(chat_id), str(text).strip())
    with BOT_SENT_BUSINESS_TEXT_GUARD:
        sent_at = BOT_SENT_BUSINESS_TEXT.get(key)
        if sent_at and time.time() - sent_at <= 180:
            BOT_SENT_BUSINESS_TEXT.pop(key, None)
            return True
    return False

BUTTON_ICONS = {
    "green": "check",
    "red": "cross",
    "blue": "diamond",
}

def is_admin(user_id):
    return int(user_id) == int(config.ADMIN_ID)

def is_supervisor(user_id):
    try:
        return int(user_id) in _SUPERVISOR_IDS
    except Exception:
        return False

def is_staff(user_id):
    return is_admin(user_id) or is_supervisor(user_id)

def notify_admin_download_error(source, exc, extra_lines=None):
    lines = [f"{e('cross')} <b>فشل تحميل ({html_escape(source)})</b>"]
    for line in (extra_lines or []):
        lines.append(html_escape(str(line)))
    lines.append(f"<i>{html_escape(str(exc)[:500])}</i>")
    try:
        safe_send_message(config.ADMIN_ID, "\n".join(lines))
    except Exception as notify_exc:
        print(f"notify_admin_download_error failed: {notify_exc}", flush=True)
    return f"{e('cross')} <b>حصل عارض تقني أثناء التحميل، جاري المتابعة.</b>"

def bot_enabled():
    return db.get_setting("bot_enabled", "1") == "1"

FEATURES = {
    "delete_detect": {"label": "كشف الرسائل المحذوفة", "icon": "eye"},
    "edit_detect":   {"label": "كشف الرسائل المعدّلة", "icon": "bell"},
    "force_sub":     {"label": "الاشتراك الإجباري", "icon": "bullhorn"},
    "banned_words":  {"label": "الكلمات الممنوعة", "icon": "cross"},
    "scam_check":    {"label": "فحص قنوات البلاغات", "icon": "shield"},
    "profanity_filter": {"label": "منع السب والشتائم", "icon": "mute"},
    "muted":         {"label": "إدارة المكتومين", "icon": "mute"},
    "pin_clear":     {"label": "التثبيت والمسح الجماعي", "icon": "star"},
    "auto_replies":  {"label": "الردود التلقائية", "icon": "robot"},
    "social_media":  {"label": "السوشيال ميديا (يوت/تيك توك)", "icon": "download"},
    "song_download": {"label": "تحميل الأغاني (أمر تحميل)", "icon": "music"},
    "calculator":    {"label": "الآلة الحاسبة", "icon": "sparkles"},
    "broadcast":     {"label": "إذاعة العملاء", "icon": "megaphone"},
    "media":         {"label": "إدارة الميديا", "icon": "camera"},
    "anti_repeat":   {"label": "منع تكرار الرسائل", "icon": "clock"},
    "id_command":    {"label": "أمر الآيدي (ID)", "icon": "id_badge"},
    "xo_game":       {"label": "لعبة إكس أو (XO)", "icon": "party"},
    "wallet_lookup": {"label": "فحص يوزرات/محافظ TON", "icon": "ton_wallet"},
    "wallet_data":   {"label": "بيانات المحفظة (X-Ray)", "icon": "ton_wallet"},
    "gift_price":    {"label": "سعر هدايا تيليجرام (بيع/شراء)", "icon": "ton_gift"},
    "copy_message":  {"label": "نسخ الرسائل (نسخة احتياطية)", "icon": "download"},
    "currency_convert": {"label": "تحويل العملات (حول)", "icon": "diamond"},
    "binance_hub":   {"label": "بينانس (إشعارات/تحويل/فواتير)", "icon": "diamond"},
}

def feature_enabled(key):
    return db.get_setting(f"feature_{key}_enabled", "1") == "1"

def set_feature_enabled(key, value):
    db.set_setting(f"feature_{key}_enabled", "1" if value else "0")

ACCESS_STATES = ["disabled", "owner"]
ACCESS_INFO = {
    "disabled": {"label": "معطّلة", "icon": "cross", "style": "danger"},
    "owner":    {"label": "مفعّلة", "icon": "check", "style": "success"},
    "public":   {"label": "عام", "icon": "unlock", "style": "success"},
}

SHORT_STATE_LABEL = {
    "disabled": {"label": "معطّلة", "icon": "cross", "style": "danger"},
    "owner":    {"label": "لحسابي فقط", "icon": "lock", "style": "primary"},
    "public":   {"label": "عام", "icon": "unlock", "style": "success"},
}

def _owner_access_key(feature_key, owner_id):
    return f"owner_access_{feature_key}_{int(owner_id)}"

SCAM_CHECK_ACCESS_STATES = ["disabled", "owner", "public"]
SECURITY_ACCESS_INFO = {
    "disabled": {"label": "معطّلة", "icon": "cross", "style": "danger"},
    "owner":    {"label": "خاص", "icon": "lock", "style": "primary"},
    "public":   {"label": "في الشات", "icon": "unlock", "style": "success"},
}

CURRENCY_ACCESS_STATES = ["disabled", "owner", "public"]
CURRENCY_ACCESS_INFO = {
    "disabled": {"label": "مقفولة", "icon": "cross", "style": "danger"},
    "owner":    {"label": "لحسابي فقط", "icon": "lock", "style": "primary"},
    "public":   {"label": "في الشات", "icon": "unlock", "style": "success"},
}

SONG_ACCESS_STATES = ["disabled", "private", "chat", "public"]
SONG_ACCESS_INFO = {
    "disabled": {"label": "مقفول", "icon": "cross", "style": "danger"},
    "private":  {"label": "خاص", "icon": "lock", "style": "primary"},
    "chat":     {"label": "في الشات", "icon": "chat", "style": "success"},
    "public":   {"label": "عام", "icon": "unlock", "style": "success"},
}

def song_access(owner_id):
    state = db.get_setting(f"song_access_{int(owner_id)}", "chat")
    return state if state in SONG_ACCESS_STATES else "chat"

def set_song_access(owner_id, state):
    if state == "owner":
        state = "chat"
    if state in SONG_ACCESS_STATES:
        db.set_setting(f"song_access_{int(owner_id)}", state)

def owner_feature_access(feature_key, owner_id, default="owner"):
    if feature_key == "song_download":
        return song_access(owner_id)
    if feature_key == "scam_check":
        state = db.get_setting(f"scam_check_access_{int(owner_id)}", "owner")
        return state if state in SCAM_CHECK_ACCESS_STATES else "owner"
    if feature_key == "currency_convert":
        state = db.get_setting(f"currency_access_{int(owner_id)}", "owner")
        return state if state in CURRENCY_ACCESS_STATES else "owner"
    if feature_key == "id_command":
        state = db.get_setting(f"id_access_{int(owner_id)}", "public")
        return state if state in ("disabled", "owner", "public") else "public"
    state = db.get_setting(_owner_access_key(feature_key, owner_id), default)
    return state if state in ACCESS_STATES else default

def set_owner_feature_access(feature_key, owner_id, state):
    if feature_key == "song_download":
        set_song_access(owner_id, state)
        return
    if feature_key == "scam_check":
        if state in SCAM_CHECK_ACCESS_STATES:
            db.set_setting(f"scam_check_access_{int(owner_id)}", state)
        return
    if feature_key == "currency_convert":
        if state in CURRENCY_ACCESS_STATES:
            db.set_setting(f"currency_access_{int(owner_id)}", state)
        return
    if feature_key == "id_command":
        if state in ("disabled", "owner", "public"):
            db.set_setting(f"id_access_{int(owner_id)}", state)
        return
    db.set_setting(_owner_access_key(feature_key, owner_id), state)

def _next_access(current, states=ACCESS_STATES):
    idx = states.index(current) if current in states else 0
    return states[(idx + 1) % len(states)]

def owner_feature_on(feature_key, owner_id):
    return feature_enabled(feature_key) and owner_feature_access(feature_key, owner_id) != "disabled"

# ================= صلاحيات الميزات: للجميع / للمدير / للعميل / مغلق =================
# للجميع = المدير والعميل الاتنين | للمدير = صاحب الحساب بس | للعميل = العميل بس | مغلق = موقوفة
AUDIENCE_INFO = {
    "all":    {"label": "للعميل", "icon": "aud_all",    "style": "success", "prefix": ""},
    "owner":  {"label": "للمدير", "icon": "aud_owner", "style": "primary", "prefix": ""},
    "client": {"label": "للعميل", "icon": "aud_client", "style": "default", "prefix": ""},
    "closed": {"label": "مغلق",   "icon": "aud_closed", "style": "danger",  "prefix": ""},
}
_AUD_FULL = ["all", "owner", "closed"]
_AUD_CHAT = ["all", "owner", "closed"]
_AUD_BASIC = ["owner", "closed"]
FEATURE_AUDIENCE_OPTIONS = {
    "currency_convert": _AUD_FULL, "calculator": _AUD_FULL, "wallet_lookup": _AUD_FULL,
    "wallet_data": _AUD_FULL, "gift_price": _AUD_FULL, "song_download": _AUD_FULL,
    "social_media": _AUD_CHAT, "scam_check": _AUD_BASIC,
    "id_command": _AUD_CHAT,
}

def audience_options(key):
    return FEATURE_AUDIENCE_OPTIONS.get(key, _AUD_BASIC)

def _aud_client_flag_key(key, owner_id):
    return f"aud_client_{key}_{int(owner_id)}"

def feature_audience(key, owner_id):
    """الصلاحية الحالية للميزة (بتتحسب من الحالة القديمة + علامة «للعميل» عشان الأقسام القديمة تفضل شغالة)."""
    legacy = _feature_state(key, owner_id)
    if key == "scam_check":
        return "closed" if legacy == "disabled" else "owner"
    has_client = "client" in audience_options(key)
    if legacy == "disabled":
        return "closed"
    if legacy == "public":
        if has_client and db.get_setting(_aud_client_flag_key(key, owner_id), "0") == "1":
            return "client"
        return "all"
    if has_client and db.get_setting(_aud_client_flag_key(key, owner_id), "0") == "1":
        db.set_setting(_aud_client_flag_key(key, owner_id), "0")
    return "owner"

def audience_allows(key, owner_id, sender_id):
    aud = feature_audience(key, owner_id)
    if aud == "closed":
        return False
    if aud == "all":
        return True
    if aud == "owner":
        return sender_id == owner_id
    return True  # «للعميل» (all/client): المدير والعميل الاتنين

def set_feature_audience(key, owner_id, aud):
    options = audience_options(key)
    if aud == "client":
        aud = "all"  # «للعميل» = إنت والعميل الاتنين
    if aud not in options:
        aud = "closed" if aud == "closed" else "owner"
    if aud == "closed":
        legacy = "disabled"
    elif aud == "owner":
        legacy = "private" if (key == "song_download" and song_access(owner_id) == "private") else "owner"
    else:
        legacy = "public"
    _write_legacy_state(key, owner_id, legacy)
    if "client" in options:
        db.set_setting(_aud_client_flag_key(key, owner_id), "1" if aud == "client" else "0")
    return aud

MEDIA_BLOCKS = {
    "text": "رسائل",
    "sticker": "ملصقات",
    "emoji": "ملصقات مميزة",
    "video": "فيديوهات",
    "photo": "صور",
    "document": "ملفات",
    "audio": "صوتيات",
    "animation": "gif",
    "link": "لينكات",
}

MEDIA_ICONS = {
    "text": "chat_2",
    "sticker": "heart",
    "emoji": "heart_2",
    "video": "film",
    "photo": "camera_2",
    "document": "document",
    "audio": "mic_2",
    "animation": "film_2",
    "link": "link",
}

def _media_block_key(key, owner_id=None):
    return f"media_block_{key}_{int(owner_id)}" if owner_id is not None else f"media_block_{key}"

def media_block_enabled(key, owner_id=None):
    return db.get_setting(_media_block_key(key, owner_id), "0") == "1"

def set_media_block_enabled(key, value, owner_id=None):
    db.set_setting(_media_block_key(key, owner_id), "1" if value else "0")

def admin_media_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    keys = list(MEDIA_BLOCKS.keys())
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i:i + 2]:
            state = "لصاحب الحساب" if media_block_enabled(key) else "معطل"
            row.append(pe.btn(
                f"{MEDIA_BLOCKS[key]} : {state}",
                callback_data=f"admin_media_toggle_{key}",
                icon=MEDIA_ICONS.get(key),
                style="success" if media_block_enabled(key) else "danger",
            ))
        kb.row(*row)
    kb.add(pe.btn("رجوع", callback_data="admin_media_back", icon="arrow_left"))
    return kb

def owner_media_keyboard(owner_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    keys = list(MEDIA_BLOCKS.keys())
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i:i + 2]:
            state = "منع" if media_block_enabled(key, owner_id) else "سماح"
            row.append(pe.btn(
                f"{MEDIA_BLOCKS[key]} : {state}",
                callback_data=f"owner_media_toggle_{key}",
                icon=MEDIA_ICONS.get(key),
                style="danger" if media_block_enabled(key, owner_id) else "success",
            ))
        kb.row(*row)
    kb.row(
        pe.btn(" إعادة ضبط", callback_data="owner_media_reset", icon="admin_panel", style="danger"),
    )
    kb.row(_help_btn("media"))
    kb.row(
        types.InlineKeyboardButton("رجوع", callback_data="owner_media_back"),
    )
    return kb

def owner_media_text():
    return (
        f"{e('camera')} <b>إدارة الميديا</b>\n\n"
        "اضغط على أي زر لتغيير الحالة. عند التفعيل تُحذف رسائل العملاء من هذا النوع تلقائيًا."
    )

@bot.callback_query_handler(func=lambda c: c.data == "owner_media_menu")
def cb_owner_media_menu(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    safe_edit_message_text(
        owner_media_text(),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=owner_media_keyboard(owner_id),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("owner_media_toggle_"))
def cb_owner_media_toggle(call):
    owner_id = call.from_user.id
    key = call.data[len("owner_media_toggle_"):]
    if key not in MEDIA_BLOCKS:
        bot.answer_callback_query(call.id, "إعداد غير معروف.", show_alert=True)
        return
    new_state = not media_block_enabled(key, owner_id)
    set_media_block_enabled(key, new_state, owner_id)
    safe_edit_message_text(
        owner_media_text(),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=owner_media_keyboard(owner_id),
    )
    bot.answer_callback_query(call.id, "تم تحديث الحالة.")

@bot.callback_query_handler(func=lambda c: c.data == "owner_media_reset")
def cb_owner_media_reset(call):
    owner_id = call.from_user.id
    for key in MEDIA_BLOCKS:
        set_media_block_enabled(key, False, owner_id)
    safe_edit_message_text(
        owner_media_text(),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=owner_media_keyboard(owner_id),
    )
    bot.answer_callback_query(call.id, "تمت إعادة الضبط إلى السماح.")

@bot.callback_query_handler(func=lambda c: c.data == "owner_media_back")
def cb_owner_media_back(call):
    safe_edit_message_text(
        main_menu_text(call.from_user.id),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )
    bot.answer_callback_query(call.id)

def _message_has_link(message):
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "")
    if re.search(r"(?:https?://|www\\.|t\\.me/|telegram\\.me/)", text, re.IGNORECASE):
        return True
    for entity in (getattr(message, "entities", None) or []) + (getattr(message, "caption_entities", None) or []):
        if getattr(entity, "type", "") in ("url", "text_link"):
            return True
    return False

def _blocked_media_key(message, owner_id=None):
    ctype = getattr(message, "content_type", "")
    if ctype == "text":
        if _message_has_link(message) and media_block_enabled("link", owner_id):
            return "link"
        return "text" if media_block_enabled("text", owner_id) else None
    if ctype == "sticker":
        sticker = getattr(message, "sticker", None)
        if getattr(sticker, "is_custom_emoji", False) or getattr(sticker, "custom_emoji_id", None):
            return "emoji" if media_block_enabled("emoji", owner_id) else ("sticker" if media_block_enabled("sticker", owner_id) else None)
        return "sticker" if media_block_enabled("sticker", owner_id) else None
    if ctype == "voice":
        return "audio" if media_block_enabled("audio", owner_id) else None
    return ctype if ctype in MEDIA_BLOCKS and media_block_enabled(ctype, owner_id) else None

PROFANITY_WORDS = [
    "كسمك",
    "كسمين امك",
    "مصمص",
    "زبي",
    "احبه",
    "امك متناكه",
    "بن شرموطه",
]

PROFANITY_REPLY_TEXT = "الله يسامحك 🤍"

def _find_profanity_word(text):
    if not text:
        return None
    text_l = text.strip().lower()
    for w in PROFANITY_WORDS:
        if w.strip().lower() in text_l:
            return w
    return None

REPEAT_LIMIT = 2

def _repeat_track_key(bc_id, chat_id, sender_id):
    return f"repeat_track_{bc_id}_{chat_id}_{sender_id}"

def _repeat_text_key(message):
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "")
    text = text.strip().lower()
    return text or None

def track_repeat_count(bc_id, chat_id, sender_id, content_key):
    key = _repeat_track_key(bc_id, chat_id, sender_id)
    raw = db.get_setting(key, "") or ""
    last_hash, _, last_count = raw.partition("|")
    try:
        last_count = int(last_count)
    except ValueError:
        last_count = 0
    current_hash = hashlib.md5(content_key.encode("utf-8")).hexdigest()
    new_count = last_count + 1 if current_hash == last_hash else 0
    db.set_setting(key, f"{current_hash}|{new_count}")
    return new_count

def _force_sub_key(owner_id, name):
    return f"force_sub_{name}_{int(owner_id)}" if owner_id is not None else f"force_sub_{name}"

def force_sub_channels(owner_id=None):
    return [row["channel"] for row in db.list_force_sub_channels(owner_id)]

DEFAULT_FORCE_SUB_MESSAGE = (
    f"{e('bell')} <u><b>يجب عليك الانضمام إلى قناة البوت أولًا</b></u>\n"
    "للمتابعة واستخدام كل الخدمات المتاحة."
)

def force_sub_message(owner_id=None):
    return db.get_setting(_force_sub_key(owner_id, "message")) or DEFAULT_FORCE_SUB_MESSAGE

def force_sub_button_style(owner_id, button_name):
    return db.get_setting(_force_sub_key(owner_id, f"button_{button_name}"), "success")

def force_sub_button_text(owner_id):
    return db.get_setting(_force_sub_key(owner_id, "button_text"), "انضم للقناة")

def _social_dest_key(owner_id):
    return f"social_dest_{int(owner_id)}"

def social_dest(owner_id):
    return db.get_setting(_social_dest_key(owner_id), "شات")

def set_social_dest(owner_id, dest):
    db.set_setting(_social_dest_key(owner_id), dest)

SOCIAL_ACCESS_STATES = ["disabled", "owner", "public"]
SOCIAL_ACCESS_INFO = {
    "disabled": {"label": "مقفولة", "icon": "cross", "style": "danger"},
    "owner": {"label": "لحسابي فقط", "icon": "lock", "style": "primary"},
    "public": {"label": "عام", "icon": "unlock", "style": "success"},
}

def _social_access_key(owner_id):
    return f"social_access_{int(owner_id)}"

def social_access(owner_id):
    state = db.get_setting(_social_access_key(owner_id), "owner")
    return state if state in SOCIAL_ACCESS_STATES else "owner"

def set_social_access(owner_id, state):
    db.set_setting(_social_access_key(owner_id), state)

# ---- إعدادات كل منصة سوشيال ميديا على حدة ----
SOCIAL_TYPES = {"av": "صوت + فيديو", "a": "صوت فقط", "v": "فيديو فقط"}
SOCIAL_PLATFORMS = {
    "youtube":    {"label": "يوتيوب", "icon": "yt", "ud": ("YouTube",), "types": ("av", "a", "v")},
    "instagram":  {"label": "إنستجرام", "icon": "ig", "ud": ("Instagram",), "types": ("av", "a", "v")},
    "facebook":   {"label": "فيسبوك", "icon": "fb", "ud": ("Facebook",), "types": ("av", "a", "v")},
    "tiktok":     {"label": "تيك توك", "icon": "film", "ud": ("TikTok",), "types": ("av", "a", "v")},
    "soundcloud": {"label": "ساوندكلاود", "icon": "sc", "ud": ("SoundCloud",), "types": ("a",)},
    "pinterest":  {"label": "بينترست", "icon": "pin", "ud": ("Pinterest",), "types": ("av", "a", "v")},
    "snapchat":   {"label": "سناب شات", "icon": "snap", "ud": ("Snapchat",), "types": ("av", "a", "v")},
    "other":      {"label": "مواقع أخرى", "icon": "globe", "ud": (), "types": ("av", "a", "v")},
}
_UD_TO_SOCIAL = {name: key for key, pf in SOCIAL_PLATFORMS.items() for name in pf["ud"]}

def social_platform_key(url):
    name = ud.platform_of(url) if url else None
    return _UD_TO_SOCIAL.get(name, "other")

def social_p_state(owner_id, key):
    stored = db.get_setting(f"social_p_{key}_{int(owner_id)}", "")
    if stored in SONG_ACCESS_STATES:
        return stored
    # الافتراضي: كل المنصات شغّالة لحسابك فقط، ومحدش غيرك يقدر يحمّل لحد ما تفتحها (عام) بنفسك
    return "private" if social_dest(owner_id) == "خاص" else "chat"

def set_social_p_state(owner_id, key, state):
    if state in SONG_ACCESS_STATES:
        db.set_setting(f"social_p_{key}_{int(owner_id)}", state)

def social_p_dest(owner_id, key):
    stored = db.get_setting(f"social_pd_{key}_{int(owner_id)}", "")
    if stored in ("chat", "private"):
        return stored
    return "private" if social_p_state(owner_id, key) == "private" else "chat"

def set_social_p_dest(owner_id, key, dest):
    if dest in ("chat", "private"):
        db.set_setting(f"social_pd_{key}_{int(owner_id)}", dest)

def _social_owner_state(owner_id, key):
    return "private" if social_p_dest(owner_id, key) == "private" else "chat"

def social_p_types(owner_id, key):
    opts = SOCIAL_PLATFORMS[key]["types"]
    value = db.get_setting(f"social_t_{key}_{int(owner_id)}", opts[0])
    return value if value in opts else opts[0]

def set_social_p_types(owner_id, key, value):
    if value in SOCIAL_PLATFORMS[key]["types"]:
        db.set_setting(f"social_t_{key}_{int(owner_id)}", value)

def social_reset_platform(owner_id, key):
    """يرجّع إعدادات منصة واحدة للافتراضي: لحسابي فقط، في الشات، بدون انتظار ولا إشعارات."""
    oid = int(owner_id)
    db.set_setting(f"social_p_{key}_{oid}", "chat")
    db.set_setting(f"social_pd_{key}_{oid}", "chat")
    db.set_setting(f"social_t_{key}_{oid}", SOCIAL_PLATFORMS[key]["types"][0])
    db.set_setting(_social_opt_key("q", key, oid), "auto")
    db.set_setting(_social_opt_key("c", key, oid), "1")
    db.set_setting(_social_opt_key("cd", key, oid), "0")
    db.set_setting(_social_opt_key("n", key, oid), "0")

def social_reset_all(owner_id):
    set_social_dest(owner_id, "شات")
    set_social_access(owner_id, "owner")
    set_song_access(owner_id, "chat")
    for k in SOCIAL_PLATFORMS:
        social_reset_platform(owner_id, k)

def social_apply_types(types_value, mode):
    """يطبّق إعداد «النوع» على وضع التحميل المطلوب (audio/video)."""
    if types_value == "a":
        return "audio"
    if types_value == "v":
        return "video"
    return mode

SOCIAL_QUALITY = {"auto": "تلقائي", "720": "720p", "480": "480p", "360": "360p"}
SOCIAL_COOLDOWN = {"0": "بدون", "30": "30 ثانية", "60": "دقيقة", "300": "5 دقايق"}

def _social_opt_key(prefix, key, owner_id):
    return f"social_{prefix}_{key}_{int(owner_id)}"

def social_p_quality(owner_id, key):
    v = db.get_setting(_social_opt_key("q", key, owner_id), "auto")
    return v if v in SOCIAL_QUALITY else "auto"

def social_p_quality_int(owner_id, key):
    v = social_p_quality(owner_id, key)
    return int(v) if v.isdigit() else None

def social_p_caption(owner_id, key):
    return db.get_setting(_social_opt_key("c", key, owner_id), "1") == "1"

def social_p_cooldown(owner_id, key):
    v = db.get_setting(_social_opt_key("cd", key, owner_id), "0")
    return v if v in SOCIAL_COOLDOWN else "0"

def social_p_notify(owner_id, key):
    return db.get_setting(_social_opt_key("n", key, owner_id), "0") == "1"

def social_p_has_quality(key):
    return key not in ("soundcloud", "tiktok")

def social_p_has_caption(key):
    return key != "youtube"

def _cycle_key(options, current):
    keys = list(options)
    return keys[(keys.index(current) + 1) % len(keys)] if current in keys else keys[0]

_SOCIAL_LAST = {}

def social_p_client_gate(owner_id, key, sender, is_owner_sender, owner_chat_id, chat_id, bc_id):
    """حد الانتظار بين طلبات العميل + إشعار المالك. بيرجع False لو العميل لازم يستنى."""
    if is_owner_sender:
        return True
    secs = int(social_p_cooldown(owner_id, key))
    now = time.time()
    k = (int(owner_id), key, int(sender.id))
    if secs:
        wait = secs - (now - _SOCIAL_LAST.get(k, 0))
        if wait > 0:
            try:
                _social_send(chat_id, bc_id, f"{e('clock')} <b>استنى {int(wait) + 1} ثانية قبل الطلب الجاي.</b>")
            except Exception:
                pass
            return False
    _SOCIAL_LAST[k] = now
    if len(_SOCIAL_LAST) > 2000:
        for old in sorted(_SOCIAL_LAST, key=_SOCIAL_LAST.get)[:500]:
            _SOCIAL_LAST.pop(old, None)
    if social_p_notify(owner_id, key):
        name = " ".join(x for x in (getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or "") if x)
        if getattr(sender, "username", None):
            name = f"{name} (@{sender.username})".strip()
        try:
            safe_send_message(
                owner_chat_id,
                f"{e('bell')} <b>طلب تحميل من عميل</b>\n"
                f"المنصة: {html_escape(SOCIAL_PLATFORMS[key]['label'])}\n"
                f"العميل: {html_escape(name or str(sender.id))}",
            )
        except Exception:
            pass
    return True

def social_p_route(owner_id, key, is_owner_sender, owner_chat_id, chat_id, bc_id):
    """(مسموح؟، شات الإرسال، bc_id) حسب حالة المنصة: خاص / في الشات / عام / مقفول."""
    state = social_p_state(owner_id, key)
    if state == "disabled" or (not is_owner_sender and state != "public"):
        return False, None, None
    if state == "private":
        return True, owner_chat_id, None
    if state == "public" and is_owner_sender and social_p_dest(owner_id, key) == "private":
        return True, owner_chat_id, None
    return True, chat_id, bc_id

_SOCIAL_STOP_NOTICE = {}

def social_notify_stopped(owner_id, owner_chat_id, key, sender, is_owner_sender, globally=False):
    """إشعار خاص للمالك إن منصة واقفة وجاله طلب تحميل عليها (بحد أقصى مرة كل 30 ثانية لكل منصة)."""
    now = time.time()
    k = (int(owner_id), key)
    if now - _SOCIAL_STOP_NOTICE.get(k, 0) < 30:
        return
    _SOCIAL_STOP_NOTICE[k] = now
    label = SOCIAL_PLATFORMS[key]["label"]
    if is_owner_sender:
        who = "إنت"
    else:
        who = " ".join(x for x in (getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or "") if x)
        if getattr(sender, "username", None):
            who = f"{who} (@{sender.username})".strip()
        who = who or str(getattr(sender, "id", ""))
    reason = "السوشيال ميديا كلها واقفة" if globally else f"منصة {label} واقفة"
    try:
        safe_send_message(
            owner_chat_id,
            f"{e('cross')} <b>{html_escape(reason)}</b>\n"
            f"جاله طلب تحميل على {html_escape(label)} من: {html_escape(who)}\n"
            f"ما اتنفذش. شغّلها من إعدادات السوشيال ميديا.",
        )
    except Exception:
        pass

def social_p_route_notify(owner_id, key, social_on, sender, is_owner_sender, owner_chat_id, chat_id, bc_id):
    """زي social_p_route بس بيبعت إشعار خاص للمالك لو المنصة (أو السوشيال كله) واقف."""
    if not social_on:
        social_notify_stopped(owner_id, owner_chat_id, key, sender, is_owner_sender, globally=True)
        return False, None, None
    if social_p_state(owner_id, key) == "disabled":
        social_notify_stopped(owner_id, owner_chat_id, key, sender, is_owner_sender)
        return False, None, None
    return social_p_route(owner_id, key, is_owner_sender, owner_chat_id, chat_id, bc_id)

def _next_social_access(current):
    return "owner" if current == "disabled" else "disabled"

CALC_ACCESS_STATES = ["disabled", "owner", "public"]
CALC_ACCESS_INFO = {
    "disabled": {"label": "مقفولة", "icon": "cross", "style": "danger"},
    "owner": {"label": "لحسابي فقط", "icon": "lock", "style": "primary"},
    "public": {"label": "عام", "icon": "unlock", "style": "success"},
}

def _calc_access_key(owner_id):
    return f"calc_access_{int(owner_id)}"

def calc_access(owner_id):
    state = db.get_setting(_calc_access_key(owner_id), "owner")
    return state if state in CALC_ACCESS_STATES else "owner"

def set_calc_access(owner_id, state):
    db.set_setting(_calc_access_key(owner_id), state)

def _next_calc_access(current):
    idx = CALC_ACCESS_STATES.index(current) if current in CALC_ACCESS_STATES else 0
    return CALC_ACCESS_STATES[(idx + 1) % len(CALC_ACCESS_STATES)]

WALLET_ACCESS_STATES = ["disabled", "owner", "public"]
WALLET_ACCESS_INFO = {
    "disabled": {"label": "مقفولة", "icon": "cross", "style": "danger"},
    "owner": {"label": "لحسابي فقط", "icon": "lock", "style": "primary"},
    "public": {"label": "في الشات", "icon": "unlock", "style": "success"},
}

def _wallet_access_key(owner_id):
    return f"wallet_access_{int(owner_id)}"

def wallet_access(owner_id):
    state = db.get_setting(_wallet_access_key(owner_id), "owner")
    return state if state in WALLET_ACCESS_STATES else "owner"

def set_wallet_access(owner_id, state):
    db.set_setting(_wallet_access_key(owner_id), state)

def _next_wallet_access(current):
    idx = WALLET_ACCESS_STATES.index(current) if current in WALLET_ACCESS_STATES else 0
    return WALLET_ACCESS_STATES[(idx + 1) % len(WALLET_ACCESS_STATES)]

def wallet_data_access(owner_id):
    state = db.get_setting(f"wallet_data_access_{int(owner_id)}", "owner")
    return state if state in WALLET_ACCESS_STATES else "owner"

def set_wallet_data_access(owner_id, state):
    db.set_setting(f"wallet_data_access_{int(owner_id)}", state)

GIFT_PRICE_ACCESS_STATES = ["disabled", "owner", "public"]
GIFT_PRICE_ACCESS_INFO = {
    "disabled": {"label": "مقفولة", "icon": "cross", "style": "danger"},
    "owner": {"label": "لحسابي فقط", "icon": "lock", "style": "primary"},
    "public": {"label": "في الشات", "icon": "unlock", "style": "success"},
}

def _gift_price_access_key(owner_id):
    return f"gift_price_access_{int(owner_id)}"

def gift_price_access(owner_id):
    state = db.get_setting(_gift_price_access_key(owner_id), "owner")
    return state if state in GIFT_PRICE_ACCESS_STATES else "owner"

def set_gift_price_access(owner_id, state):
    db.set_setting(_gift_price_access_key(owner_id), state)

def _next_gift_price_access(current):
    idx = GIFT_PRICE_ACCESS_STATES.index(current) if current in GIFT_PRICE_ACCESS_STATES else 0
    return GIFT_PRICE_ACCESS_STATES[(idx + 1) % len(GIFT_PRICE_ACCESS_STATES)]

_CALC_TRIGGER_RAW = ["الحاسبة", "حاسبة", "الآلة الحاسبة", "الة حاسبة", "الالة حاسبة", "الالة الحاسبة"]

def _normalize_ar_text(s):
    s = (s or "").strip()
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ة", "ه").replace("ى", "ي")
    s = re.sub(r"\s+", " ", s)
    return s

CALC_TRIGGER_SET = {_normalize_ar_text(w) for w in _CALC_TRIGGER_RAW}

_AR_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
CALC_EXPR_RE = re.compile(r"^\s*\d+(?:\.\d+)?(?:\s*[+\-*/×÷%]\s*\d+(?:\.\d+)?)+\s*$")

_CALC_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Mod: operator.mod,
}
_CALC_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}

def _calc_eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BIN_OPS:
        return _CALC_BIN_OPS[type(node.op)](_calc_eval_node(node.left), _calc_eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARY_OPS:
        return _CALC_UNARY_OPS[type(node.op)](_calc_eval_node(node.operand))
    raise ValueError("invalid calculator expression")

def safe_eval_calc(expr):
    cleaned = (expr or "").translate(_AR_DIGITS_MAP).replace("×", "*").replace("÷", "/")
    node = ast.parse(cleaned, mode="eval").body
    return _calc_eval_node(node)

def _calc_format_number(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, float):
        value = round(value, 8)
        s = f"{value:.8f}".rstrip("0").rstrip(".")
        return s
    return str(value)

def _calc_is_expression(text):
    return bool(CALC_EXPR_RE.match((text or "").translate(_AR_DIGITS_MAP)))

def _calc_is_keyword(text):
    return _normalize_ar_text(text) in CALC_TRIGGER_SET

CALC_SESSIONS = {}

def _calc_render_text(expr):
    display = expr if expr else "0"
    return (
        f"{e('sparkles')} <b>الآلة الحاسبة</b>\n\n"
        f"<code>{html_escape(display)}</code>"
    )

def calc_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=4)
    markup.add(
        pe.btn("C", callback_data="calcbtn_C", icon="cross", style="danger"),
        pe.btn("⌫", callback_data="calcbtn_DEL", style="primary"),
        pe.btn("%", callback_data="calcbtn_%", style="primary"),
        pe.btn("÷", callback_data="calcbtn_/", style="primary"),
    )
    markup.add(
        pe.btn("7", callback_data="calcbtn_7"),
        pe.btn("8", callback_data="calcbtn_8"),
        pe.btn("9", callback_data="calcbtn_9"),
        pe.btn("×", callback_data="calcbtn_*", style="primary"),
    )
    markup.add(
        pe.btn("4", callback_data="calcbtn_4"),
        pe.btn("5", callback_data="calcbtn_5"),
        pe.btn("6", callback_data="calcbtn_6"),
        pe.btn("−", callback_data="calcbtn_-", style="primary"),
    )
    markup.add(
        pe.btn("1", callback_data="calcbtn_1"),
        pe.btn("2", callback_data="calcbtn_2"),
        pe.btn("3", callback_data="calcbtn_3"),
        pe.btn("+", callback_data="calcbtn_+", style="primary"),
    )
    markup.add(
        pe.btn("0", callback_data="calcbtn_0"),
        pe.btn(".", callback_data="calcbtn_."),
        pe.btn(" =", callback_data="calcbtn_=", icon="check", style="success"),
    )
    markup.add(pe.btn(" إغلاق", callback_data="calcbtn_CLOSE", icon="trash", style="danger"))
    return markup

def _wallet_list_text(items, joiner=" "):
    if not items:
        return "~ لا يوجد 🚫"
    shown = items[:40]
    text = joiner.join(f"@{html_escape(str(x))}" if joiner == " " else html_escape(str(x)) for x in shown)
    if len(items) > 40:
        text += f" …(+{len(items) - 40})"
    return text

def _format_wallet_report(username_or_address, data, usd_price):
    usd_txt = ""
    if usd_price:
        usd_txt = f" ≈ {data['balance_ton'] * usd_price:.2f}$"
    parts = [
        f"{e('ton_wallet')} <b>Wallet:</b> <code>{html_escape(data['address'])}</code>",
        "—————«•»—————",
        f"{e('ton_money')} <b>Balance:</b> {data['balance_ton']:.3f} TON{usd_txt}",
        "—————«•»—————",
        f"{e('ton_users')} <b>Usernames ({len(data['usernames'])}):</b>",
        _wallet_list_text(data["usernames"], joiner=" "),
        "—————«•»—————",
        f"{e('ton_numbers')} <b>Numbers ({len(data['numbers'])}):</b>",
        _wallet_list_text(data["numbers"], joiner=", "),
        "—————«•»—————",
        f"{e('ton_gift')} <b>Upgraded gifts ({len(data['gifts'])}):</b>",
        _wallet_list_text(data["gifts"], joiner="\n"),
        "—————«•»—————",
        f"{e('ton_other')} <b>Other NFT ({len(data['others'])}):</b>",
        _wallet_list_text(data["others"], joiner="\n"),
    ]
    return "\n".join(parts)

def _wallet_item_html(it):
    txt = html_escape(it["text"])
    return f'<a href="{html_escape(it["url"])}">{txt}</a>' if it.get("url") else txt

def _wallet_inline_section(icon, title, items, empty, per_line=2, sep=" - ", limit=30):
    """سكشن زي كشف المحفظة في الصورة: العنوان + العدد، والعناصر جنب بعض."""
    head = f"{e(icon)} <b>{title} ({len(items)}) :</b>"
    if not items:
        return f"{head} ~ {empty} 🚫."
    shown = items[:limit]
    chunks = [shown[i:i + per_line] for i in range(0, len(shown), per_line)]
    lines = []
    for chunk in chunks:
        parts = []
        for it in chunk:
            if it.get("kind") == "gift" and it.get("title"):
                label = f'{it.get("emoji", "🎁")} {html_escape(it["title"])}'
                parts.append(f'<a href="{html_escape(it["url"])}">{label}</a>' if it.get("url") else label)
            else:
                parts.append(_wallet_item_html(it))
        lines.append(sep.join(parts))
    if len(items) > limit:
        lines.append(f"…(+{len(items) - limit})")
    return f"{head}\n" + "\n".join(lines)

def _format_wallet_scan(data, limit=30):
    bal = f"{data['balance']:.3f}"
    usd = data.get("usd_price")
    usd_txt = f" = <b>{data['balance'] * usd:,.2f} $</b>" if usd else ""
    addr = data["address"]
    users = data["usernames"]
    # اليوزرات: بتتعرض جنب بعض في سطر واحد
    users_head = f"{e('ton_users')} <b>Users ({len(users)}) :</b>"
    if users:
        shown = users[:limit]
        users_body = " ".join(_wallet_item_html(u) for u in shown)
        if len(users) > limit:
            users_body += f" …(+{len(users) - limit})"
        users_block = f"{users_head} {users_body}"
    else:
        users_block = f"{users_head} ~ No users 🚫."
    auctions = data.get("auctions", [])
    parts = [
        f"{e('ton_wallet')} <b>Wallet :</b>\n<code>{html_escape(addr)}</code>",
        f"{e('ton_money')} <b>Balance :</b> <a href=\"https://tonviewer.com/{html_escape(addr)}\">{bal} TON</a>{usd_txt}",
        users_block,
        _wallet_inline_section("clock", "In Auction", auctions, "No auctions", per_line=2, limit=limit),
        _wallet_inline_section("ton_numbers", "Numbers", data["numbers"], "No number found", per_line=2, sep=", ", limit=limit),
        _wallet_inline_section("ton_gift", "Upgraded gifts", data["gifts"], "No gifts", per_line=2, limit=limit),
        _wallet_inline_section("ton_other", "Other NFT", data["others"], "No other nfts", per_line=1, limit=limit),
    ]
    text = "\n\n".join(parts)
    # لو الرسالة طويلة نقلّل عدد العناصر بدل ما نقص الـ HTML من النص (عشان الوسوم متتكسرش)
    if len(text) > 3900 and limit > 4:
        return _format_wallet_scan(data, limit=max(4, int(limit * 0.6)))
    return text

def _wallet_scan_markup(address):
    markup = types.InlineKeyboardMarkup()
    markup.row(pe.btn(" Open in Tonviewer", url=f"https://tonviewer.com/{address}", icon="ton_wallet", style="success"))
    if len(address) <= 58:
        markup.row(pe.btn(" آخر المعاملات", callback_data=f"wxtx:{address}", icon="document", style="primary"))
    return markup

def _run_wallet_scan(chat_id, bc_id, raw_query, reply_to=None):
    def _worker():
        query = raw_query.strip()
        kwargs = {}
        if reply_to:
            kwargs["reply_parameters"] = types.ReplyParameters(message_id=int(reply_to))
        placeholder = safe_send_business_message(
            chat_id, bc_id, f"{e('clock')} <b>جاري الفحص...</b> ⏳", **kwargs
        )

        def _finish(text, markup=None):
            msg_id = getattr(placeholder, "message_id", None)
            if msg_id:
                try:
                    bot.edit_message_text(
                        text, chat_id, msg_id, business_connection_id=bc_id,
                        reply_markup=markup,
                        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
                    )
                    return
                except Exception as exc:
                    if "not modified" in str(exc).lower():
                        return
                    try:
                        bot.edit_message_text(
                            pe.strip_premium(text), chat_id, msg_id, business_connection_id=bc_id,
                            reply_markup=pe.strip_button_icons(markup) if markup else None,
                            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
                        )
                        return
                    except Exception as exc2:
                        if "not modified" in str(exc2).lower():
                            return
                        print(f"wallet scan edit failed: {exc2}", flush=True)
            safe_send_business_message(chat_id, bc_id, text, reply_markup=markup, **kwargs)

        if wallet_lookup.is_wallet_address(query) or wallet_xray.is_address(query):
            address = query
        else:
            address = wallet_lookup.resolve_fragment_wallet(query)
        if not address:
            _finish(
                f"{e('cross')} <b>مش لاقي محفظة TON وراء «{html_escape(query)}».</b>\n"
                "إما اليوزر مش متحول NFT على Fragment، أو مفيهوش محفظة متربطة بيه."
            )
            return
        try:
            data = wallet_scan.scan(address, api_key=(config.TONAPI_KEY or None))
        except wallet_scan.ScanError as exc:
            print(f"wallet scan failed for {address}: {exc}", flush=True)
            _finish(
                f"{e('cross')} <b>مقدرتش أجيب بيانات المحفظة دلوقتي.</b>\n"
                "اتأكد إن العنوان صحيح أو جرب تاني بعد شوية."
            )
            return
        except Exception as exc:
            print(f"wallet scan crashed for {address}: {exc!r}", flush=True)
            _finish(f"{e('cross')} <b>حصل خطأ أثناء الفحص، جرب تاني.</b>")
            return
        _finish(_format_wallet_scan(data), _wallet_scan_markup(address))

    threading.Thread(target=_worker, daemon=True, name="wallet-scan").start()

def _run_wallet_lookup(chat_id, bc_id, raw_query, reply_to=None):
    _run_wallet_scan(chat_id, bc_id, raw_query, reply_to)

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("wxtx:"))
def cb_wallet_transactions(call):
    bc_id = getattr(call.message, "business_connection_id", None)
    connection = db.get_business_connection(bc_id) if bc_id else None
    if not connection or call.from_user.id != connection["owner_user_id"]:
        bot.answer_callback_query(call.id, "الزرار ده لصاحب الحساب بس.", show_alert=True)
        return
    address = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id, "جاري جلب المعاملات...")
    _run_wallet_xray(call.message.chat.id, bc_id, address, reply_to=call.message.message_id)

def _format_wallet_xray(data, usd_price):
    usd_txt = f" ≈ {data['balance'] * usd_price:.2f}$" if usd_price else ""
    addr = data["address"]
    lines = [
        f"{e('ton_wallet')} <b>بيانات المحفظة (X-Ray)</b>",
        f"<code>{html_escape(addr)}</code>",
    ]
    if data.get("name"):
        lines.append(f"{e('person')} <b>الاسم:</b> {html_escape(data['name'])}")
    lines += [
        "—————«•»—————",
        f"{e('ton_money')} <b>الرصيد:</b> {data['balance']:,.4f} TON{usd_txt}",
        "—————«•»—————",
    ]
    txs = data["txs"]
    if not txs:
        lines.append("لا توجد معاملات مسجّلة.")
    else:
        lines.append(f"<b>آخر {len(txs)} معاملات:</b>")
    body = []
    for i, tx in enumerate(txs, 1):
        if tx["direction"] == "in":
            head, sign = "⬇️ <b>وارد</b>", "+"
        elif tx["direction"] == "out":
            head, sign = "⬆️ <b>صادر</b>", "-"
        else:
            head, sign = "🔁 <b>داخلي</b>", ""
        status = "" if tx["success"] else " ❌ فاشلة"
        memo = tx["memo"] if len(tx["memo"]) <= 120 else tx["memo"][:117] + "..."
        body.append(
            f"\n#{i} {head}{status}\n"
            f"• <b>المبلغ:</b> {sign}{tx['amount']:,.4f} TON <i>(رسوم {tx['fee']:.4f})</i>\n"
            f"• <b>الميمو:</b> {html_escape(memo)}\n"
            f"• <b>من:</b> <code>{html_escape(tx['sender'])}</code>\n"
            f"• <b>إلى:</b> <code>{html_escape(tx['receiver'])}</code>\n"
            f"• <b>الوقت:</b> {html_escape(tx['time'])}\n"
            f"• <b>Hash:</b> <code>{html_escape(tx['hash'])}</code>"
        )
    text = "\n".join(lines)
    for chunk in body:
        if len(text) + len(chunk) > 3800:
            text += "\n\n<i>… (تم اختصار باقي المعاملات)</i>"
            break
        text += chunk
    return text

def _run_wallet_xray(chat_id, bc_id, address, reply_to=None):
    def _worker():
        address_clean = address.strip()
        safe_send_business_message(chat_id, bc_id, f"{e('clock')} <b>جاري فحص المحفظة...</b> ⏳")
        data = wallet_xray.fetch_report(address_clean, api_key=(config.TONAPI_KEY or None))
        if not data:
            safe_send_business_message(
                chat_id, bc_id,
                f"{e('cross')} <b>مقدرتش أجيب بيانات المحفظة دلوقتي.</b>\n"
                "اتأكد إن العنوان صحيح أو جرب تاني بعد شوية.",
            )
            return
        usd_price = wallet_lookup.fetch_ton_usd_price()
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "فتح في Tonviewer", url=f"https://tonviewer.com/{address_clean}"
        ))
        extra = {}
        if reply_to:
            extra["reply_parameters"] = types.ReplyParameters(message_id=int(reply_to))
        safe_send_business_message(
            chat_id, bc_id, _format_wallet_xray(data, usd_price), reply_markup=markup, **extra
        )

    threading.Thread(target=_worker, daemon=True, name="wallet-xray").start()

def _format_gift_price_report(parsed, meta, price):
    slug_display = (meta or {}).get("title") or gift_price.slug_to_words(parsed.get("slug", ""))
    slug_display = re.sub(r"\s*#\d+\s*$", "", slug_display)
    header = f"{e('ton_gift')} <b>{html_escape(slug_display)}</b>"
    if parsed.get("number"):
        header += f" <code>#{parsed['number']}</code>"
    lines = [header, "—————«•»—————"]

    if meta and (meta.get("model") or meta.get("backdrop") or meta.get("symbol")):
        if meta.get("model"):
            lines.append(f"{e('sparkles')} <b>الموديل:</b> {html_escape(meta['model'])}")
        if meta.get("backdrop"):
            lines.append(f"{e('image')} <b>الخلفية:</b> {html_escape(meta['backdrop'])}")
        if meta.get("symbol"):
            lines.append(f"{e('star')} <b>الرمز:</b> {html_escape(meta['symbol'])}")
        if meta.get("supply"):
            lines.append(f"{e('people')} <b>الكمية المصدَرة:</b> {meta['issued']}/{meta['supply']}")
        lines.append("<i>(بيانات رسمية من تيليجرام)</i>")
        lines.append("—————«•»—————")

    if not price:
        lines += [
            f"{e('cross')} <b>تعذر جلب سعر السوق حالياً من المصادر المتاحة.</b>",
            "ممكن الهدية مش متداولة بكثرة، أو المصدر مؤقتاً مش راضي يرد — جرب تاني بعد شوية.",
        ]
        return "\n".join(lines)

    floor = price["floor_ton"]
    usd = gift_price.fetch_ton_usd_price()
    usd_txt = f" (≈ {floor * usd:.2f}$)" if usd else ""
    lines += [
        f"{e('ton_money')} <b>أقل سعر معروض بالسوق حالياً:</b> {floor:.2f} TON{usd_txt}",
        f"<i>المصدر: {html_escape(price['source'])}</i>",
        "—————«•»—————",
        f"{e('point_up')} <b>لو بدك تشتري:</b> هيك تقريباً أرخص سعر متاح دلوقتي.",
        f"{e('point_up')} <b>لو بدك تبيع:</b> سعّر تحت الرقم ده شوي عشان تتباع بسرعة.",
        f"{e('sparkles')} <b>سعر تقريبي من السوق</b>",
    ]
    return "\n".join(lines)

def _run_gift_price_lookup(chat_id, bc_id, raw_text):
    def _worker():
        parsed = gift_price.parse_gift_link(raw_text)
        if not parsed:
            return
        safe_send_business_message(chat_id, bc_id, f"{e('clock')} <b>جاري جلب السعر...</b> ⏳")
        meta = None
        if parsed.get("source") == "telegram" and parsed.get("number"):
            meta = gift_price.fetch_telegram_gift_meta(parsed["slug"], parsed["number"])
        price = gift_price.fetch_gift_price(parsed["slug"], model=(meta or {}).get("model"))
        safe_send_business_message(chat_id, bc_id, _format_gift_price_report(parsed, meta, price))

    threading.Thread(target=_worker, daemon=True, name="gift-price-lookup").start()

def _open_calculator(message, bc_id, initial_expr=""):
    chat_id = message.chat.id
    expr = (initial_expr or "").strip()
    text = _calc_render_text(expr)
    sent = None
    try:
        sent = bot.send_message(
            chat_id, text, business_connection_id=bc_id, reply_markup=calc_keyboard()
        )
    except Exception:
        try:
            sent = bot.send_message(
                chat_id, pe.strip_premium(text), business_connection_id=bc_id,
                reply_markup=pe.strip_button_icons(calc_keyboard()),
            )
        except Exception as exc:
            print(f"Calculator send failed: {exc}", flush=True)
            return
    if sent:
        CALC_SESSIONS[f"{chat_id}:{sent.message_id}"] = {"expr": expr}

def _calc_update_message(call, expr):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    bc_id = getattr(call.message, "business_connection_id", None)
    text = _calc_render_text(expr)
    kwargs = {"reply_markup": calc_keyboard()}
    if bc_id:
        kwargs["business_connection_id"] = bc_id
    try:
        bot.edit_message_text(text, chat_id, msg_id, **kwargs)
    except Exception:
        try:
            fallback_kwargs = _strip_kwargs_icons(kwargs)
            bot.edit_message_text(pe.strip_premium(text), chat_id, msg_id, **fallback_kwargs)
        except Exception as exc:
            print(f"Calculator edit failed: {exc}", flush=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("calcbtn_"))
def cb_calculator(call):
    token = call.data[len("calcbtn_"):]
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    key = f"{chat_id}:{msg_id}"
    session = CALC_SESSIONS.setdefault(key, {"expr": ""})
    expr = session["expr"]
    bc_id = getattr(call.message, "business_connection_id", None)

    if token == "CLOSE":
        CALC_SESSIONS.pop(key, None)
        try:
            if bc_id:
                delete_business_message(bc_id, msg_id)
            else:
                bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    if token == "C":
        expr = ""
    elif token == "DEL":
        expr = expr[:-1]
    elif token == "=":
        try:
            result = safe_eval_calc(expr)
            expr = _calc_format_number(result)
        except Exception:
            bot.answer_callback_query(call.id, " معادلة غير صحيحة.", show_alert=True)
            return
    elif token in "+-*/%":
        if not expr and token != "-":
            bot.answer_callback_query(call.id)
            return
        if expr and expr[-1] in "+-*/%":
            expr = expr[:-1] + token
        else:
            expr += token
    elif token == ".":
        last_num = re.split(r"[+\-*/%]", expr)[-1]
        if "." in last_num:
            bot.answer_callback_query(call.id)
            return
        expr += token
    else:
        expr += token

    session["expr"] = expr
    bot.answer_callback_query(call.id)
    _calc_update_message(call, expr)

def display_name(user):
    if not user:
        return "غير معروف"
    name = user.first_name or ""
    if user.last_name:
        name += " " + user.last_name
    return name.strip() or "غير معروف"

REPLY_VARIABLES_HELP = (
    "\n\n<b>متغيرات جاهزة (اختياري) تقدر تحطها في النص:</b>\n"
    "<code>{الاسم}</code> اسم اللي بيكتب بالكامل\n"
    "<code>{الاسم_الاول}</code> الاسم الأول بس\n"
    "<code>{اليوزر}</code> يوزر تليجرام (أو «بدون يوزر» لو مفيش)\n"
    "<code>{التاريخ}</code> تاريخ النهاردة\n"
    "<code>{الوقت}</code> الوقت دلوقتي\n"
    "وهيتم استبدالهم تلقائيًا وقت إرسال الرد."
)

def apply_reply_variables(text, user):
    if not text:
        return text
    values = {
        "{الاسم}": display_name(user),
        "{الاسم_الاول}": (user.first_name.strip() if user and user.first_name else display_name(user)),
        "{اليوزر}": (f"@{user.username}" if user and getattr(user, "username", None) else "بدون يوزر"),
        "{التاريخ}": time.strftime("%Y-%m-%d"),
        "{الوقت}": time.strftime("%H:%M"),
    }
    for key, val in values.items():
        if key in text:
            text = text.replace(key, html_escape(val))
    return text

def content_of(message):
    if message.content_type == "text":
        return html_escape(message.text or ""), "text"
    caption = html_escape(message.caption) if message.caption else ""
    ctype = message.content_type
    label = {
        "photo": f"{e('camera_2')} <b>صورة</b>",
        "video": f"{e('film')} <b>فيديو</b>",
        "voice": f"{e('mic')} <b>رسالة صوتية</b>",
        "video_note": f"{e('camera_3')} <b>فيديو دائري</b>",
        "sticker": f"{e('heart')} <b>ستيكر</b>",
        "document": f"{e('document')} <b>ملف</b>",
        "audio": f"{e('music')} <b>صوت</b>",
        "animation": f"{e('film_2')} <b>GIF</b>",
    }.get(ctype, ctype)
    text = f"{label}" + (f"\n{caption}" if caption else "")
    return text, ctype

def media_reference(message):
    ctype = getattr(message, "content_type", "")
    if ctype == "photo" and getattr(message, "photo", None):
        return message.photo[-1].file_id, "photo"
    obj = getattr(message, ctype, None)
    if obj is not None and hasattr(obj, "file_id"):
        return obj.file_id, ctype
    return None, None

_SUB_OK_CACHE = {}
_FORCE_SUB_NOTICE_AT = {}
_FORCE_SUB_PENDING = {}   # (bc_id, chat_id, user_id) -> (message, time)  آخر رسالة اتمسحت بسبب الاشتراك
_FORCE_SUB_PENDING_TTL = 600
_SUB_OK_TTL = 180

_SUB_NEG_CACHE = {}
_SUB_NEG_TTL = 4

def channel_membership_ok(user_id, owner_id=None, fresh=False):
    """بس عضوية القنوات (من غير شرط Start)."""
    channels = force_sub_channels(owner_id)
    if not channels:
        return True
    cache_key = (int(user_id), tuple(channels))
    now = time.time()
    hit = _SUB_OK_CACHE.get(cache_key)
    if hit and now - hit < _SUB_OK_TTL:
        return True
    if not fresh:
        neg = _SUB_NEG_CACHE.get(cache_key)
        if neg and now - neg < _SUB_NEG_TTL:
            return False
    for channel in channels:
        try:
            member = bot.get_chat_member(channel, user_id)
            if member.status not in ("member", "administrator", "creator"):
                _SUB_NEG_CACHE[cache_key] = now
                return False
        except Exception as exc:
            print(f"is_subscribed check failed channel={channel} user={user_id}: {exc}", flush=True)
            _SUB_NEG_CACHE[cache_key] = now
            return False
    _SUB_OK_CACHE[cache_key] = now
    _SUB_NEG_CACHE.pop(cache_key, None)
    return True

def is_subscribed(user_id, owner_id=None, fresh=False):
    # المطوّر وصاحب الحساب نفسه (المدير) عمرهم ما بيتطلب منهم اشتراك في قنواته
    if is_admin(user_id):
        return True
    if owner_id is not None and int(user_id) == int(owner_id):
        return True
    if owner_id is not None and db.is_force_sub_exempt(owner_id, user_id):
        return True
    if not force_sub_channels(owner_id):
        return True
    if not channel_membership_ok(user_id, owner_id, fresh=fresh):
        return False
    return True

_BOT_USERNAME = {"v": None}

def bot_username():
    if _BOT_USERNAME["v"]:
        return _BOT_USERNAME["v"]
    try:
        _BOT_USERNAME["v"] = bot.get_me().username
    except Exception as exc:
        print(f"get_me failed: {exc}", flush=True)
    return _BOT_USERNAME["v"]

def _subscription_channel_buttons(owner_id):
    channels = [c for c in force_sub_channels(owner_id) if str(c).startswith("@")]
    base_text = force_sub_button_text(owner_id)
    buttons = []
    for channel in channels:
        buttons.append(pe.btn(
            base_text,
            url=f"https://t.me/{channel.lstrip('@')}",
            style=force_sub_button_style(owner_id, "channel"),
        ))
    return buttons

def subscription_keyboard(owner_id=None):
    markup = types.InlineKeyboardMarkup()
    for btn in _subscription_channel_buttons(owner_id):
        markup.add(btn)
    markup.add(pe.btn(
        " تحقق من الاشتراك",
        callback_data="check_sub",
        icon="6030657343744644592",
        style=force_sub_button_style(owner_id, "verify"),
    ))
    return markup

def business_subscription_keyboard(owner_id=None):
    markup = types.InlineKeyboardMarkup()
    for btn in _subscription_channel_buttons(owner_id):
        markup.add(btn)
    markup.add(pe.btn(
        " تحقق من الاشتراك",
        callback_data=f"check_sub_{owner_id}",
        icon="6030657343744644592",
        style=force_sub_button_style(owner_id, "verify"),
    ))
    return markup

def _forwarded_user(message):
    if not message:
        return None
    user = getattr(message, "forward_from", None)
    if user:
        return user
    origin = getattr(message, "forward_origin", None)
    user = getattr(origin, "sender_user", None) if origin else None
    return user

def _target_from_message(message, owner_id):
    candidate = getattr(message, "reply_to_message", None)
    user = _forwarded_user(candidate) or _forwarded_user(message)
    if user and user.id != owner_id:
        return user
    return None

def _notify_mute_result(chat_id, owner_id, target, muted, edit_message_id=None):
    if muted and target.id == owner_id:
        _edit_prompt_or_send(
            chat_id, edit_message_id, f"{e('cross')} <b>ده حسابك انت، مينفعش تكتم نفسك.</b>"
        )
        return
    target_name = display_name(target)
    target_username = target.username or ""
    username_text = f"@{html_escape(target_username)}" if target_username else "لا يوجد"
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn("فتح بروفايل العميل", url=f"tg://user?id={target.id}", icon="person_3", style="primary"))
    if muted:
        markup.add(pe.btn("إلغاء كتم", callback_data=f"unmute_inline_{target.id}", icon="unlock", style="success"))
        text = (
            f"{e('mute')} <b>تم كتم العميل بنجاح</b>\n\n"
            f"{e('person')} <b>الاسم:</b> {html_escape(target_name)}\n"
            f"{e('link')} <b>اليوزر:</b> {username_text}\n"
            f"<b>ID:</b> <code>{target.id}</code>\n"
            f"<b>الحالة:</b> سيتم حذف رسائله الجديدة ولن تصله ردود تلقائية."
        )
        db.mute_user(owner_id, target.id, target_name, target_username)
    else:
        text = (
            f"{e('unlock')} <b>تم إلغاء كتم العميل</b>\n\n"
            f"{e('person')} <b>الاسم:</b> {html_escape(target_name)}\n"
            f"{e('link')} <b>اليوزر:</b> {username_text}\n"
            f"<b>ID:</b> <code>{target.id}</code>"
        )
        db.unmute_user(owner_id, target.id)
    _edit_prompt_or_send(chat_id, edit_message_id, text, reply_markup=markup)

MUTE_TRIGGER_WORDS = ('كتم', 'يكتم')
UNMUTE_TRIGGER_WORDS = ('الغاء كتم', 'إلغاء كتم', 'فك الكتم', 'فك كتم', 'الغاء الكتم', 'إلغاء الكتم')

_MUTE_NORM_WORDS = {"كتم", "اكتم", "يكتم", "كتمه", "اكتمه", "اكتمو", "ميوت", "mute"}
_UNMUTE_NORM_WORDS = {
    "الغاء كتم", "الغاء الكتم", "الغي كتم", "الغي الكتم", "فك كتم", "فك الكتم", "رفع الكتم", "رفع كتم",
    "الغاء ميوت", "فك ميوت", "فك الميوت", "unmute",
}

def _norm_cmd(text):
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u0652\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()

_EXEMPT_NORM_WORDS = {"استثناء", "استثنيه", "استثني", "استثنائه", "استثناءه", "exempt"}
_UNEXEMPT_NORM_WORDS = {
    "الغاء استثناء", "الغاء الاستثناء", "الغي استثناء", "الغي الاستثناء", "شيل استثناء",
    "شيل الاستثناء", "الغاء استثنائه", "الغاء الاستثنائه", "unexempt",
}

def _exempt_cmd(text):
    """'exempt' / 'unexempt' / None — أمر استثناء عميل من الاشتراك الإجباري."""
    t = _norm_cmd(text)
    if not t:
        return None
    if t in _UNEXEMPT_NORM_WORDS:
        return "unexempt"
    if t in _EXEMPT_NORM_WORDS:
        return "exempt"
    return None

def _mute_cmd(text):
    """'mute' / 'unmute' / None — بيتجاهل النقط والتشكيل والهمزات (كتم، اكتم، كتم.، إلغاء الكتم...)."""
    t = _norm_cmd(text)
    if not t:
        return None
    if t in _UNMUTE_NORM_WORDS:
        return "unmute"
    if t in _MUTE_NORM_WORDS:
        return "mute"
    return None

@bot.message_handler(func=lambda m: _mute_cmd(m.text) is not None)
def private_mute_command(message):
    owner_id = message.from_user.id
    target = _target_from_message(message, owner_id)
    command = message.text.strip()
    is_mute = _mute_cmd(command) == "mute"
    if target:
        _notify_mute_result(message.chat.id, owner_id, target, is_mute)
        return
    prompt = (
        "<b>كتم عميل:</b> حوّل رسالة العميل إلى هنا أو اكتب ID العميل.\n"
        "اكتب <b>الغاء</b> للإلغاء."
        if is_mute else
        "<b>إلغاء كتم عميل:</b> حوّل رسالة العميل إلى هنا أو اكتب ID العميل.\n"
        "اكتب <b>الغاء</b> للإلغاء."
    )
    msg = safe_send_message(message.chat.id, prompt)
    bot.register_next_step_handler(msg, lambda m: step_get_private_mute_target(m, is_mute))

def step_get_private_mute_target(message, muted):
    if (message.text or '').strip().lower() in ('الغاء', 'إلغاء', 'cancel'):
        safe_send_message(message.chat.id, '<b>تم الإلغاء.</b>')
        return
    target = _target_from_message(message, message.from_user.id)
    if target:
        _notify_mute_result(message.chat.id, message.from_user.id, target, muted)
        return
    raw = (message.text or '').strip()
    if raw.isdigit():
        info = db.find_sender_info(message.from_user.id, int(raw))
        if info:
            class Target:
                pass
            target = Target()
            target.id = int(raw)
            target.first_name = info.get('sender_name') or 'غير معروف'
            target.last_name = ''
            target.username = info.get('sender_username') or ''
            _notify_mute_result(message.chat.id, message.from_user.id, target, muted)
            return
    safe_send_message(message.chat.id, '<b>لم أجد العميل.</b> أرسل رسالة محوّلة منه أو ID صحيح.')

def _notify_block_result(chat_id, owner_id, target, blocked, edit_message_id=None):
    target_name = display_name(target)
    target_username = target.username or ""
    username_text = f"@{html_escape(target_username)}" if target_username else "لا يوجد"
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn("فتح بروفايل العميل", url=f"tg://user?id={target.id}", icon="person_3", style="primary"))
    if blocked:
        markup.add(pe.btn("فك الحظر", callback_data=f"unblock_inline_{target.id}", icon="unlock", style="success"))
        text = (
            f"{e('shield')} <b>تم حظر العميل بنجاح</b>\n\n"
            f"{e('person')} <b>الاسم:</b> {html_escape(target_name)}\n"
            f"{e('link')} <b>اليوزر:</b> {username_text}\n"
            f"<b>ID:</b> <code>{target.id}</code>\n"
            f"<b>الحالة:</b> رسائله الجديدة هتتحذف أوتوماتيك ومش هتوصلك أي إشعارات "
            f"(حذف/تعديل) أو ردود تلقائية عنه."
        )
        db.block_user(owner_id, target.id, target_name, target_username)
    else:
        text = (
            f"{e('unlock')} <b>تم فك الحظر عن العميل</b>\n\n"
            f"{e('person')} <b>الاسم:</b> {html_escape(target_name)}\n"
            f"{e('link')} <b>اليوزر:</b> {username_text}\n"
            f"<b>ID:</b> <code>{target.id}</code>"
        )
        db.unblock_user(owner_id, target.id)
    _edit_prompt_or_send(chat_id, edit_message_id, text, reply_markup=markup)

BLOCK_TRIGGER_WORDS = ('حظر', 'يحظر')
UNBLOCK_TRIGGER_WORDS = ('الغاء حظر', 'إلغاء حظر', 'فك الحظر', 'فك حظر', 'الغاء الحظر', 'إلغاء الحظر')
PIN_TRIGGER_WORDS = ('تثبيت',)
UNPIN_TRIGGER_WORDS = ('الغاء تثبيت', 'إلغاء تثبيت', 'فك تثبيت', 'فك التثبيت', 'الغاء التثبيت', 'إلغاء التثبيت')
COPY_TRIGGER_WORDS = ('نسخ',)
COPY_DOWNLOAD_CACHE = {}

def _send_message_copy(dest_chat_id, src):
    ctype = getattr(src, "content_type", None)
    sender_name = display_name(getattr(src, "from_user", None))
    header = (
        f"{e('download')} <b>نسخة من رسالة</b>\n"
        f"{e('person')} <b>من:</b> {html_escape(sender_name)}"
    )
    try:
        if ctype == "text":
            safe_send_message(
                dest_chat_id,
                f"{header}\n\n{e('chat_2')} <b>النص:</b>\n{html_escape(src.text or '')}",
            )
        elif ctype == "photo":
            bot.send_photo(dest_chat_id, src.photo[-1].file_id, caption=header, parse_mode="HTML")
        elif ctype == "video":
            bot.send_video(dest_chat_id, src.video.file_id, caption=header, parse_mode="HTML")
        elif ctype == "animation":
            bot.send_animation(dest_chat_id, src.animation.file_id, caption=header, parse_mode="HTML")
        elif ctype == "document":
            bot.send_document(dest_chat_id, src.document.file_id, caption=header, parse_mode="HTML")
        elif ctype == "audio":
            bot.send_audio(dest_chat_id, src.audio.file_id, caption=header, parse_mode="HTML")
        elif ctype == "sticker":
            safe_send_message(dest_chat_id, header)
            bot.send_sticker(dest_chat_id, src.sticker.file_id)
        elif ctype == "voice":
            token = uuid.uuid4().hex[:12]
            COPY_DOWNLOAD_CACHE[token] = ("voice", src.voice.file_id)
            markup = types.InlineKeyboardMarkup()
            markup.add(pe.btn("تحميل كملف صوتي", callback_data=f"copydl_{token}", icon="download", style="primary"))
            bot.send_voice(dest_chat_id, src.voice.file_id, caption=header, parse_mode="HTML", reply_markup=markup)
        elif ctype == "video_note":
            token = uuid.uuid4().hex[:12]
            COPY_DOWNLOAD_CACHE[token] = ("video_note", src.video_note.file_id)
            markup = types.InlineKeyboardMarkup()
            markup.add(pe.btn("تحميل كفيديو", callback_data=f"copydl_{token}", icon="download", style="primary"))
            bot.send_video_note(dest_chat_id, src.video_note.file_id)
            safe_send_message(dest_chat_id, header, reply_markup=markup)
        else:
            safe_send_message(dest_chat_id, f"{header}\n\n<i>نوع الرسالة ده مش مدعوم للنسخ حاليًا.</i>")
        return True
    except Exception as exc:
        print(f"Copy message failed: {exc}", flush=True)
        try:
            safe_send_message(dest_chat_id, f"{e('cross')} <b>حصل خطأ أثناء عمل النسخة.</b>")
        except Exception:
            pass
        return False

@bot.callback_query_handler(func=lambda c: c.data.startswith("copydl_"))
def cb_copy_download(call):
    token = call.data[len("copydl_"):]
    item = COPY_DOWNLOAD_CACHE.pop(token, None)
    bot.answer_callback_query(call.id)
    if not item:
        try:
            bot.send_message(call.message.chat.id, f"{e('cross')} <b>انتهت صلاحية الزرار، اطلب نسخة جديدة بكلمة «نسخ».</b>")
        except Exception:
            pass
        return
    kind, file_id = item
    try:
        if kind == "voice":
            bot.send_document(
                call.message.chat.id, file_id,
                caption=f"{e('download')} <b>اتحمّل كملف صوتي.</b>", parse_mode="HTML",
            )
        elif kind == "video_note":
            bot.send_video(
                call.message.chat.id, file_id,
                caption=f"{e('download')} <b>اتحمّل كفيديو.</b>", parse_mode="HTML",
            )
    except Exception as exc:
        print(f"Copy download failed: {exc}", flush=True)
        try:
            bot.send_message(call.message.chat.id, f"{e('cross')} <b>حصل خطأ أثناء التحميل.</b>")
        except Exception:
            pass

def private_block_command(message):  # الحظر اتشال - الدالة متسابة بس من غير تفعيل
    owner_id = message.from_user.id
    target = _target_from_message(message, owner_id)
    command = message.text.strip()
    is_block = command in BLOCK_TRIGGER_WORDS
    if target:
        _notify_block_result(message.chat.id, owner_id, target, is_block)
        return
    prompt = (
        "<b>حظر عميل:</b> حوّل رسالة العميل إلى هنا أو اكتب ID العميل.\n"
        "اكتب <b>الغاء</b> للإلغاء."
        if is_block else
        "<b>فك حظر عميل:</b> حوّل رسالة العميل إلى هنا أو اكتب ID العميل.\n"
        "اكتب <b>الغاء</b> للإلغاء."
    )
    msg = safe_send_message(message.chat.id, prompt)
    bot.register_next_step_handler(msg, lambda m: step_get_private_block_target(m, is_block))

def step_get_private_block_target(message, blocked):
    if (message.text or '').strip().lower() in ('الغاء', 'إلغاء', 'cancel'):
        safe_send_message(message.chat.id, '<b>تم الإلغاء.</b>')
        return
    target = _target_from_message(message, message.from_user.id)
    if target:
        _notify_block_result(message.chat.id, message.from_user.id, target, blocked)
        return
    raw = (message.text or '').strip()
    if raw.isdigit():
        info = db.find_sender_info(message.from_user.id, int(raw))
        if info:
            class Target:
                pass
            target = Target()
            target.id = int(raw)
            target.first_name = info.get('sender_name') or 'غير معروف'
            target.last_name = ''
            target.username = info.get('sender_username') or ''
            _notify_block_result(message.chat.id, message.from_user.id, target, blocked)
            return
    safe_send_message(message.chat.id, '<b>لم أجد العميل.</b> أرسل رسالة محوّلة منه أو ID صحيح.')

BUSINESS_LINK_VIDEO_URL = "https://b.top4top.io/m_3913gtahg1.mp4"

def business_link_caption():
    return (
        "<b>البوت لسه مش متصل بحساب الـ Business بتاعك.</b>\n\n"
        "<b>افتح إعدادات حساب الأعمال من الزرار تحت، روح لقسم Chatbots، "
        "واختار البوت ده عشان يشتغل معاك.</b>\n\n"
        "<b>بعد ما تخلّص الربط، دوس زرار «تحقق» تحت.</b>"
    )

def business_link_keyboard():
    markup = types.InlineKeyboardMarkup()

    markup.add(types.InlineKeyboardButton(" ربط الحساب", url="tg://settings/business"))

    markup.add(pe.btn(" تحقق", callback_data="check_business_link", style="success"))
    return markup

def _owner_active_connection(owner_id):
    for c in db.list_connections_for_owner(owner_id):
        if c.get("is_enabled"):
            return c

    for bc_id in db.list_known_bc_ids_for_owner(owner_id):
        healed = _recover_business_connection(bc_id, min_interval=30)
        if healed and healed.get("is_enabled") and healed.get("owner_user_id") == owner_id:
            return healed
    return None

_BC_RECOVER_AT = {}

def _discover_owner_connection(owner_id):
    """بحث إجباري (من غير throttle) عن ربط الـ Business بتاع المستخدم:
    1) الـ ids المعروفة له  2) أي ids متسجلة في الرسائل ومالهاش مالك في الداتابيز."""
    for c in db.list_connections_for_owner(owner_id):
        if c.get("is_enabled"):
            return c
    candidates = list(db.list_known_bc_ids_for_owner(owner_id))
    try:
        candidates += [b for b in db.list_orphan_bc_ids(30) if b not in candidates]
    except Exception as exc:
        print(f"orphan bc scan failed: {exc}", flush=True)
    for bc_id in candidates:
        healed = _recover_business_connection(bc_id, min_interval=0)
        if healed and healed.get("is_enabled") and healed.get("owner_user_id") == owner_id:
            return healed
    for c in db.list_connections_for_owner(owner_id):
        if c.get("is_enabled"):
            return c
    return None

def _recover_business_connection(bc_id, min_interval=300):
    if not bc_id:
        return None
    now = time.time()
    if now - _BC_RECOVER_AT.get(bc_id, 0) < min_interval:
        return None
    _BC_RECOVER_AT[bc_id] = now
    try:
        bc = bot.get_business_connection(bc_id)
    except Exception as exc:
        print(f"getBusinessConnection failed bc={bc_id}: {exc}", flush=True)
        return None
    owner = getattr(bc, "user", None)
    if owner is None:
        return None
    existing = db.get_business_connection(bc_id) or {}
    db.upsert_business_connection(
        bc_id=bc_id,
        owner_user_id=owner.id,
        owner_chat_id=getattr(bc, "user_chat_id", None) or owner.id,
        owner_name=display_name(owner),
        owner_username=owner.username or "",
        is_enabled=1 if getattr(bc, "is_enabled", True) else 0,
        created_at=getattr(bc, "date", None) or existing.get("created_at") or int(time.time()),
    )
    _BC_OWNER_CACHE.pop(bc_id, None)
    print(f"Recovered Business connection from Telegram bc={bc_id} owner={owner.id}", flush=True)
    return db.get_business_connection(bc_id)

def _ensure_business_connection(bc_id):
    connection = db.get_business_connection(bc_id)
    if connection and connection.get("is_enabled"):
        return connection
    return _recover_business_connection(bc_id) or connection

def _require_active_connection(call):
    message = getattr(call, "message", None)
    bc_id = getattr(message, "business_connection_id", None) if message else None
    if not bc_id:
        # لوحة التحكم في الخاص تظل متاحة حتى بعد انتهاء الاشتراك.
        return True
    connection = _ensure_business_connection(bc_id) or db.get_business_connection(bc_id) or {}
    owner_id = int(connection.get("owner_user_id") or call.from_user.id)
    if not is_admin(owner_id) and trial_expired(owner_id):
        bot.answer_callback_query(
            call.id,
            access_expired_alert(owner_id),
            show_alert=True,
        )
        return False
    return True

def welcome_channel_url():
    if config.CHANNEL_URL:
        return config.CHANNEL_URL
    try:
        for c in force_sub_channels(config.ADMIN_ID):
            if str(c).startswith("@"):
                return f"https://t.me/{str(c).lstrip('@')}"
    except Exception:
        pass
    return ""

def welcome_text(user):
    name = html_escape(display_name(user))
    _custom = (db.get_setting("custom_welcome", "") or "").strip()
    if _custom:
        return html_escape(_custom).replace("{name}", name).replace("{id}", str(getattr(user, "id", "")))
    feats = [
        "ردود تلقائية وردود أونلاين للعملاء",
        "كشف الرسائل اللي اتمسحت",
        "حفظ نص الرسالة قبل ما تتعدل",
        "استخراج الميديا ذاتية التدمير",
        "تحميل من يوتيوب وتيك توك وباقي المنصات",
        "وأدوات تانية كتير جوه القائمة",
    ]
    body = "\n".join(f"• {html_escape(f)}" for f in feats)
    return (
        f"{e('welcome_hi')} <b>أهلاً بيك يا {name}</b>\n\n"
        f"{e('welcome_person')} <b>أنا مساعدك الشخصي لإدارة حسابك التجاري على تليجرام، بشتغل معاك في الخلفية وأنت مرتاح.</b>\n\n"
        f"{e('welcome_tools')} <b>أهم اللي أقدر أعمله:</b>\n"
        f"<blockquote>{body}</blockquote>"
    )

def welcome_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    row = [pe.btn(" تواصل", url=f"tg://user?id={config.DEVELOPER_ID}", icon="chat", style="primary")]
    ch = welcome_channel_url()
    if ch:
        row.append(pe.btn(" القناة", url=ch, icon="megaphone", style="primary"))
    markup.row(*row)
    return markup

def _first_welcome_key(owner_id):
    return f"first_link_welcome_{int(owner_id)}"

def _first_menu_key(owner_id):
    return f"first_link_menu_{int(owner_id)}"

def send_owner_main_menu(chat_id, owner_id):
    """Send the main menu and inline controls together in one message."""
    return _send_inline_page(chat_id, main_menu_text(owner_id), owner_panel_markup(owner_id))


def _send_inline_page(chat_id, text, markup):
    """Send the page and its inline controls together so one action makes one message."""
    return safe_send_message(chat_id, text, reply_markup=markup)


def _send_start_link_video(chat_id, caption):
    msg = safe_send_video(chat_id, BUSINESS_LINK_VIDEO_URL, caption=caption,
                          reply_markup=types.ReplyKeyboardRemove())
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg.message_id,
                                      reply_markup=business_link_keyboard())
    except Exception as exc:
        print(f"start video markup update failed: {type(exc).__name__}", flush=True)
        safe_send_message(chat_id, "اربط حساب Business من هنا:", reply_markup=business_link_keyboard())
    return msg

def send_first_link_welcome(chat_id, owner_id, user):
    """Send welcome and the owner menu together, once after the first Business link."""
    key = _first_welcome_key(owner_id)
    mkey = _first_menu_key(owner_id)
    if db.get_setting(key, "0") == "1":
        return False
    db.set_setting(key, "1")
    db.set_setting(mkey, "1")
    try:
        text = f"{welcome_text(user)}\n\n{main_menu_text(owner_id)}"
        _send_inline_page(chat_id, text, owner_panel_markup(owner_id))
        return True
    except Exception as exc:
        db.set_setting(key, "0")
        db.set_setting(mkey, "0")
        print(f"first link welcome failed owner={owner_id}: {exc}", flush=True)
        return False

@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id

    if not is_subscribed(user_id):
        _send_inline_page(
            message.chat.id,
            f"{e('lock')} {force_sub_message()}",
            subscription_keyboard(),
        )
        return

    trial_just_started = False
    if not is_admin(user_id) and access_mode() == "trial_24h" and not is_premium_active(user_id):
        trial_just_started = start_trial_if_needed(user_id)

    text = main_menu_text(user_id)
    if trial_just_started:
        text = "<b>بدأت تجربتك المجانية لمدة 24 ساعة من الآن.</b>\n\n" + text
    elif trial_expired(user_id):
        text = f"{access_expired_text(user_id)}\n\n{text}"
    _send_inline_page(message.chat.id, text, owner_panel_markup(user_id))

@bot.callback_query_handler(func=lambda c: c.data == "check_business_link")
def cb_check_business_link(call):
    user_id = call.from_user.id
    if _owner_active_connection(user_id) or _discover_owner_connection(user_id):
        bot.answer_callback_query(call.id, "تم التحقق، حسابك متصل.")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        call.message.from_user = call.from_user
        call.message._skip_welcome = True
        cmd_start(call.message)
    else:
        bot.answer_callback_query(
            call.id,
            "مش لاقي الربط عندي. لو البوت متضاف في Chatbots فعلًا: ابعت أي رسالة من حسابك لأي شات "
            "وبعدين دوس تحقق تاني، أو شيل البوت وضيفه تاني من الإعدادات.",
            show_alert=True,
        )

def sup_panel_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" الحسابات النشطة", callback_data="adm_accs_0", icon="people", style="success"),
        pe.btn(" الأعضاء المجانيين", callback_data="adm_free", icon="people_2", style="primary"),
    )
    markup.add(
        pe.btn(" حظر مستخدم", callback_data="adm_ban", icon="shield", style="danger"),
        pe.btn(" فك حظر", callback_data="adm_unbanid", icon="unlock", style="success"),
    )
    markup.add(
        pe.btn(" المحظورين", callback_data="adm_banned", icon="lock", style="danger"),
        pe.btn(" بحث عن مستخدم", callback_data="adm_lookup", icon="eye", style="primary"),
    )
    markup.add(
        pe.btn(" إضافة مميز", callback_data="adm_premadd", icon="star", style="success"),
        pe.btn(" مميز دائم", callback_data="adm_premperm", icon="diamond", style="success"),
    )
    markup.add(
        pe.btn(" حذف مميز", callback_data="adm_premrm", icon="trash", style="danger"),
        pe.btn(" المميزين", callback_data="adm_premlist_0", icon="diamond", style="primary"),
    )
    markup.add(pe.btn(" رجوع", callback_data="adm_exit", icon="arrow_left"))
    return markup

def admin_panel_markup(uid=None):
    if uid is not None and not is_admin(uid):
        return sup_panel_markup()
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" الحسابات النشطة", callback_data="adm_accs_0", icon="people", style="success"),
        pe.btn(" إذاعة للجميع", callback_data="adm_bc", icon="megaphone", style="primary"),
    )
    markup.add(
        pe.btn(" حظر مستخدم", callback_data="adm_ban", icon="shield", style="danger"),
        pe.btn(" فك حظر", callback_data="adm_unbanid", icon="unlock", style="success"),
    )
    markup.add(pe.btn(" المحظورين", callback_data="adm_banned", icon="lock", style="danger"))
    markup.add(
        pe.btn(" إضافة مميز", callback_data="adm_premadd", icon="star", style="success"),
        pe.btn(" مميز دائم", callback_data="adm_premperm", icon="diamond", style="success"),
    )
    markup.add(pe.btn(" حذف مميز", callback_data="adm_premrm", icon="trash", style="danger"))
    markup.add(
        pe.btn(" إضافة مشرف", callback_data="adm_supadd", icon="plus", style="success"),
        pe.btn(" المشرفين", callback_data="adm_suplist", icon="people", style="primary"),
    )
    markup.add(
        pe.btn(" بحث عن مستخدم", callback_data="adm_lookup", icon="eye", style="primary"),
        pe.btn(" سجل العمليات", callback_data="adm_log", icon="document", style="primary"),
    )
    markup.add(pe.btn(" رسالة الترحيب", callback_data="adm_welcome", icon="pencil", style="primary"))
    markup.add(
        pe.btn(" المميزين", callback_data="adm_premlist_0", icon="diamond", style="primary"),
        pe.btn(" الأعضاء المجانيين", callback_data="adm_free", icon="people_2", style="primary"),
    )
    mode_info = ACCESS_MODE_INFO[access_mode()]
    markup.add(
        pe.btn(f" وضع الاشتراك: {mode_info['label']}", callback_data="adm_mode_cycle",
               icon=mode_info["icon"], style=mode_info["style"]),
    )
    markup.add(
        pe.btn(" إضافة قناة اشتراك", callback_data="adm_addch", icon="plus", style="success"),
        pe.btn(" إدارة الاشتراك الإجباري", callback_data="admin_forcesub", icon="bullhorn", style="primary"),
    )
    markup.add(pe.btn(" قنوات فحص النصابين", callback_data="adm_scam", icon="shield", style="danger"))
    markup.add(
        pe.btn(" الإحصائيات", callback_data="admin_stats", icon="eye", style="primary"),
        pe.btn(" شرح", callback_data="admin_help", icon="book", style="primary"),
    )
    markup.add(
        pe.btn(" عمل نسخة احتياطية", callback_data="adm_backup_export", icon="download", style="primary"),
        pe.btn(" رفع نسخة احتياطية", callback_data="adm_backup_restore", icon="upload", style="success"),
    )
    try:
        _ck_ok = _yt_ck_state()[0] == "ok"
    except Exception:
        _ck_ok = True
    markup.add(pe.btn(" كوكيز يوتيوب" + ("" if _ck_ok else " ⚠️"), callback_data="adm_ytck",
                      icon="shield", style="primary" if _ck_ok else "danger"))
    markup.add(pe.btn(" رجوع", callback_data="adm_exit", icon="arrow_left"))
    return markup


def _scam_channels():
    try:
        raw = db.get_setting("scam_watch_channels", "[]") or "[]"
        values = json.loads(raw)
    except Exception:
        values = []
    channels = []
    seen = set()
    for value in values if isinstance(values, list) else []:
        try:
            channel = scam_checker.normalize_public_channel(str(value))
        except ValueError:
            continue
        if channel.lower() not in seen:
            channels.append(channel)
            seen.add(channel.lower())
    return channels


def _scam_watch_enabled():
    # A configured watchlist starts working by default; an explicit admin "off" stays off.
    default = "1" if _scam_channels() else "0"
    return db.get_setting("scam_watch_enabled", default) == "1"


def _scam_owner_enabled(owner_id):
    # New/unspecified active accounts are covered unless the admin explicitly disables them.
    return db.get_setting(f"scam_watch_owner_{int(owner_id)}", "1") == "1"


def _scam_active_owners():
    owners = {}
    for row in db.list_active_accounts(limit=10000, offset=0):
        try:
            uid = int(row["user_id"])
        except (KeyError, TypeError, ValueError):
            continue
        owners[uid] = row
    return owners


SCAM_CHANNELS_PAGE_SIZE = 10


def _scam_channels_menu_text(page=0):
    channels = _scam_channels()
    total_pages = max(1, (len(channels) + SCAM_CHANNELS_PAGE_SIZE - 1) // SCAM_CHANNELS_PAGE_SIZE)
    page = max(0, min(int(page), total_pages - 1))
    first = page * SCAM_CHANNELS_PAGE_SIZE
    visible = channels[first:first + SCAM_CHANNELS_PAGE_SIZE]
    listing = "\n".join(
        f"{first + index + 1}. <code>{html_escape(channel)}</code>"
        for index, channel in enumerate(visible)
    ) or "• لا توجد قنوات مضافة."
    status = "مفعّل" if _scam_watch_enabled() else "متوقف"
    enabled_count = sum(1 for uid in _scam_active_owners() if _scam_owner_enabled(uid))
    return (
        f"{e('shield')} <b>فحص البلاغات من القنوات العامة</b>\n\n"
        f"<b>المفتاح العام:</b> {status}\n"
        f"<b>الحسابات المفعّل لها:</b> {enabled_count}\n"
        f"<b>القنوات:</b> {len(channels)} — الصفحة {page + 1}/{total_pages}\n{listing}\n\n"
        "أضف @يوزر القناة العامة أو رابط t.me. الروابط الخاصة/روابط الدعوة غير مدعومة. "
        "الفحص التلقائي واليدوي يراجعان حتى 20 صفحة حديثة من كل قناة مضافة، لكنه لا يبحث في كامل الأرشيف. "
        "الفحص اليدوي للمدير فقط، ويقبل @username أو ID/رقم الكاش.\n\n"
        "التطابق تنبيه للمراجعة فقط، وليس حكمًا مؤكدًا على الشخص."
    )


def _scam_channels_menu_markup(page=0):
    markup = types.InlineKeyboardMarkup(row_width=2)
    channels = _scam_channels()
    total_pages = max(1, (len(channels) + SCAM_CHANNELS_PAGE_SIZE - 1) // SCAM_CHANNELS_PAGE_SIZE)
    page = max(0, min(int(page), total_pages - 1))
    first = page * SCAM_CHANNELS_PAGE_SIZE
    markup.row(
        pe.btn("إضافة قناة عامة", callback_data="adm_scam_add", icon="plus", style="success"),
        pe.btn("فحص مستخدم يدويًا", callback_data="adm_scam_scan", icon="eye", style="primary"),
    )
    markup.row(
        pe.btn("تفعيل/إيقاف لحساب", callback_data="adm_scam_owner", icon="people", style="primary"),
        pe.btn("الحسابات المفعّلة", callback_data="adm_scam_owners", icon="document"),
    )
    for index, channel in enumerate(channels[first:first + SCAM_CHANNELS_PAGE_SIZE], start=first):
        markup.add(pe.btn(f"حذف {channel}", callback_data=f"adm_scam_del_{index}", icon="trash", style="danger"))
    if total_pages > 1:
        navigation = []
        if page > 0:
            navigation.append(pe.btn("السابق", callback_data=f"adm_scam_page_{page - 1}", icon="arrow_left", style="primary"))
        if page + 1 < total_pages:
            navigation.append(pe.btn("التالي", callback_data=f"adm_scam_page_{page + 1}", icon="arrow_right", style="primary"))
        markup.row(*navigation)
    state = "إيقاف الفحص التلقائي" if _scam_watch_enabled() else "تشغيل الفحص التلقائي"
    markup.add(pe.btn(state, callback_data="adm_scam_toggle", icon="shield", style="primary"))
    markup.add(pe.btn("رجوع للوحة الأدمن", callback_data="adm_scam_back", icon="arrow_left"))
    return markup


def _scam_enabled_owners_view():
    owners = _scam_active_owners()
    enabled = [(uid, row) for uid, row in owners.items() if _scam_owner_enabled(uid)]
    if not enabled:
        text = "لا توجد حسابات مفعّل لها الفحص التلقائي حاليًا."
    else:
        text = "الحسابات التي سيفحص البوت رسائل عملائها عند تفعيل المفتاح العام:\n\n" + "\n".join(
            f"• {html_escape(row.get('name') or 'حساب')} — <code>{uid}</code>"
            + (f" — @{html_escape(row['username'])}" if row.get("username") else "")
            for uid, row in enabled[:50]
        )
        if len(enabled) > 50:
            text += f"\n\nمعروض أول 50 من {len(enabled)} حسابًا."
    markup = types.InlineKeyboardMarkup(row_width=1)
    for uid, row in enabled[:50]:
        name = _short_label(row.get("name") or str(uid), 20)
        markup.add(pe.btn(f"إيقاف {name} · {uid}", callback_data=f"adm_scam_owner_toggle_{uid}", icon="cross", style="danger"))
    markup.add(pe.btn("رجوع", callback_data="adm_scam", icon="arrow_left"))
    return text, markup


def _admin_scam_owner_step(message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw.casefold() in ("إلغاء", "الغاء", "cancel", "/cancel"):
        safe_send_message(message.chat.id, "تم إلغاء التغيير.", reply_markup=_scam_channels_menu_markup())
        return
    owners = _scam_active_owners()
    owner_id = None
    if raw.isdigit():
        candidate = int(raw)
        if candidate in owners:
            owner_id = candidate
    else:
        username = raw.lstrip("@").strip().lower()
        owner_id = next((uid for uid, row in owners.items()
                         if (row.get("username") or "").lstrip("@").lower() == username), None)
    if owner_id is None:
        prompt = safe_send_message(message.chat.id,
            "لم أجد صاحب حساب أعمال نشطًا بهذا الـID/اليوزر. أرسل ID أو @username لصاحب الحساب، أو «إلغاء».")
        bot.register_next_step_handler(prompt, _admin_scam_owner_step)
        return
    new_state = not _scam_owner_enabled(owner_id)
    db.set_setting(f"scam_watch_owner_{owner_id}", "1" if new_state else "0")
    row = owners[owner_id]
    name = html_escape(row.get("name") or str(owner_id))
    state = "مفعّل" if new_state else "متوقف"
    if new_state and not _scam_channels():
        state += " (لن يبدأ حتى تضيف قناة عامة)"
    safe_send_message(message.chat.id,
        f"تم تحديث الفحص التلقائي للحساب <b>{name}</b> (<code>{owner_id}</code>): <b>{state}</b>.\n"
        f"المفتاح العام الآن: {'مفعّل' if _scam_watch_enabled() else 'متوقف'}.",
        reply_markup=_scam_channels_menu_markup())


def _scam_result_text(target_label, result):
    hits = result.get("hits", [])
    lines = [f"{e('shield')} <b>نتيجة فحص {html_escape(target_label)}</b>"]
    if hits:
        hit_count = int(result.get("hits_total", len(hits)))
        lines.append(f"\nظهر <b>{hit_count}</b> تطابق محتمل في منشورات القنوات التالية:")
        shown_count = min(8, len(hits))
        for hit in hits[:shown_count]:
            title = html_escape(hit.get("channel_title") or hit.get("channel", "قناة"))
            url = html_escape(hit.get("url", ""), quote=True)
            snippet = html_escape((hit.get("snippet", "") or "")[:180])
            lines.append(f"\n• <b>{title}</b> — <a href=\"{url}\">رابط المنشور</a>")
            if snippet:
                lines.append(f"\n<blockquote>{snippet}</blockquote>")
        if hit_count > shown_count:
            lines.append(f"\nتم إخفاء {hit_count - shown_count} تطابق إضافي لتقصير الرسالة؛ الفحص استمر عبر القنوات.")
        lines.append("\n\nالتطابق يحتاج مراجعة بشرية؛ قد يكون ذكرًا عابرًا أو تشابهًا غير مقصود.")
    else:
        checked_count = len(result.get("checked_channels", []))
        total_channels = int(result.get("total_channels", checked_count))
        if result.get("error_count", len(result.get("errors", []))) or checked_count < total_channels:
            lines.append(
                f"\nلم يظهر تطابق في {checked_count} قناة أمكن فحصها، لكن تعذّر الوصول إلى بعض القنوات؛ "
                "لذلك النتيجة غير مكتملة ولا تعني أن الاسم غير موجود."
            )
        else:
            lines.append(
                "\nلم يظهر تطابق في أحدث المنشورات العامة التي أمكن فحصها. "
                "قد توجد منشورات أقدم من عمق الفحص أو في قنوات خاصة."
            )
    if result.get("error_count", len(result.get("errors", []))):
        errors = ", ".join(html_escape(x.get("channel", "قناة")) for x in result["errors"][:5])
        lines.append(f"\n\nتعذّر فحص {result.get('error_count', len(result.get('errors', [])))} قناة/صفحة؛ منها: {errors}.")
    lines.append(
        f"\n\nتم فحص {result.get('posts', 0)} منشورًا من "
        f"{len(result.get('checked_channels', []))}/{result.get('total_channels', len(result.get('checked_channels', [])))} قناة، "
        f"عبر {result.get('pages', result.get('requests', 0))} صفحة."
    )
    if result.get("truncated"):
        lines.append("\nوصل الفحص إلى الحد الآمن للصفحات/الوقت؛ النتيجة ليست شاملة للأرشيف.")
    return "".join(lines)


_SCAM_AUTO_PAGES_PER_CHANNEL = 20
_SCAM_MANUAL_PAGES_PER_CHANNEL = 20
_SCAM_MANUAL_SEM = threading.BoundedSemaphore(3)


def _manual_scam_scan_worker(targets, target_label, send_result):
    try:
        channels = _scam_channels()
        if not channels:
            send_result("أضف قناة عامة واحدة على الأقل من «قنوات فحص النصابين» أولًا.")
            return
        result = scam_checker.scan_channels(
            channels, targets,
            max_requests=len(channels) * _SCAM_MANUAL_PAGES_PER_CHANNEL,
            max_posts=max(1000, len(channels) * 500),
            deadline_seconds=max(
                300, ((len(channels) + 7) // 8) * _SCAM_MANUAL_PAGES_PER_CHANNEL * 6 + 90
            ),
            timeout=(2, 4),
            max_pages_per_channel=_SCAM_MANUAL_PAGES_PER_CHANNEL,
        )
        send_result(_scam_result_text(target_label, result))
    except Exception as exc:
        print(f"manual scam scan failed: {type(exc).__name__}", flush=True)
        try:
            send_result("تعذر إكمال الفحص الآن؛ حاول لاحقًا.")
        except Exception:
            pass
    finally:
        _SCAM_MANUAL_SEM.release()


def _start_manual_scam_scan(targets, target_label, send_result):
    channels = _scam_channels()
    if not channels:
        send_result("أضف قناة عامة واحدة على الأقل من «قنوات فحص النصابين» أولًا.")
        return False
    if not _SCAM_MANUAL_SEM.acquire(blocking=False):
        send_result("فيه فحوصات شغالة حاليًا؛ استنى شوية وجرب تاني.")
        return False
    try:
        send_result(f"بدأ فحص <b>{len(channels)}</b> قناة عامة؛ النتيجة هتوصلك هنا بعد مراجعة الصفحات المتاحة.")
    except Exception:
        pass
    try:
        threading.Thread(
            target=_manual_scam_scan_worker,
            args=(targets, target_label, send_result),
            daemon=True,
            name="manual-scam-scan",
        ).start()
    except Exception as exc:
        _SCAM_MANUAL_SEM.release()
        print(f"manual scam scan thread failed: {type(exc).__name__}", flush=True)
        send_result("تعذر بدء الفحص الآن؛ حاول لاحقًا.")
        return False
    return True


def _admin_scam_scan_step(message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw.casefold() in ("إلغاء", "الغاء", "cancel", "/cancel"):
        safe_send_message(message.chat.id, "تم إلغاء الفحص.", reply_markup=admin_panel_markup())
        return
    try:
        target = scam_checker.normalize_target(raw)
    except ValueError as exc:
        prompt = safe_send_message(message.chat.id, f"{html_escape(str(exc))}\nأرسل @username أو ID رقميًا، أو اكتب «إلغاء».")
        bot.register_next_step_handler(prompt, _admin_scam_scan_step)
        return
    if not _scam_channels():
        safe_send_message(message.chat.id, "أضف قناة عامة واحدة على الأقل من «قنوات فحص النصابين» أولًا.", reply_markup=admin_panel_markup())
        return
    target_label = ("@" if target["kind"] == "username" else "") + target["value"]
    _start_manual_scam_scan(
        [target], target_label,
        lambda text: safe_send_message(message.chat.id, text, disable_web_page_preview=True),
    )


def _admin_scam_add_step(message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw.casefold() in ("إلغاء", "الغاء", "cancel", "/cancel"):
        safe_send_message(message.chat.id, "تم إلغاء الإضافة.", reply_markup=admin_panel_markup())
        return
    channels = _scam_channels()
    seen = {channel.lower() for channel in channels}
    added, rejected = [], []
    for item in re.split(r"[\n,،]+", raw):
        item = item.strip()
        if not item:
            continue
        try:
            channel = scam_checker.normalize_public_channel(item)
        except ValueError:
            rejected.append(item[:80])
            continue
        if channel.lower() not in seen:
            channels.append(channel)
            seen.add(channel.lower())
            added.append(channel)
    db.set_setting("scam_watch_channels", json.dumps(channels, ensure_ascii=False))
    lines = [f"تم حفظ {len(added)} قناة."]
    if rejected:
        lines.append("تعذر قبول بعض الإدخالات؛ استخدم رابط قناة عامة أو @username.")
    safe_send_message(message.chat.id, "\n".join(lines), reply_markup=_scam_channels_menu_markup())


_SCAM_AUTO_LOCK = threading.Lock()
_SCAM_AUTO_INFLIGHT = set()
_SCAM_AUTO_CACHE = {}
_SCAM_AUTO_SEM = threading.BoundedSemaphore(3)


def _schedule_scam_watch_check(connection, sender):
    if (
        not sender
        or sender.id == connection.get("owner_user_id")
        or not _scam_watch_enabled()
        or not feature_enabled("scam_check")
    ):
        return
    try:
        owner_id = int(connection.get("owner_user_id") or 0)
    except (TypeError, ValueError):
        return
    if (
        not _scam_owner_enabled(owner_id)
        or not owner_feature_on("scam_check", owner_id)
        or not audience_allows("scam_check", owner_id, owner_id)
    ):
        return
    channels = _scam_channels()
    if not channels:
        return
    target = [{"kind": "id", "value": str(sender.id)}]
    if getattr(sender, "username", None):
        target.insert(0, {"kind": "username", "value": sender.username.lower()})
    key = (owner_id, int(sender.id), tuple(channels))
    now = time.time()
    with _SCAM_AUTO_LOCK:
        prior = _SCAM_AUTO_CACHE.get(key)
        if key in _SCAM_AUTO_INFLIGHT or (prior and now - prior[0] < (86400 if prior[1] else 1800)):
            return
        if not _SCAM_AUTO_SEM.acquire(blocking=False):
            return
        _SCAM_AUTO_INFLIGHT.add(key)
        if len(_SCAM_AUTO_CACHE) > 5000:
            for old_key, value in list(_SCAM_AUTO_CACHE.items()):
                if now - value[0] > 86400:
                    _SCAM_AUTO_CACHE.pop(old_key, None)
        snapshot = dict(connection)
        threading.Thread(
            target=_run_scam_watch_check,
            args=(snapshot, sender, target, channels, key),
            daemon=True,
            name="scam-watch-check",
        ).start()


def _run_scam_watch_check(connection, sender, target, channels, key):
    hits_found = False
    try:
        result = scam_checker.scan_channels(
            channels, target,
            max_requests=len(channels) * _SCAM_AUTO_PAGES_PER_CHANNEL,
            max_posts=max(500, len(channels) * 500),
            deadline_seconds=max(
                180, ((len(channels) + 7) // 8) * _SCAM_AUTO_PAGES_PER_CHANNEL * 6 + 60
            ),
            timeout=(2, 4),
            max_pages_per_channel=_SCAM_AUTO_PAGES_PER_CHANNEL,
        )
        hits = result.get("hits", [])
        hits_found = bool(hits)
        if hits:
            name = display_name(sender)
            username = getattr(sender, "username", None)
            label = (f"@{username}" if username else f"ID {sender.id}")
            text = (
                f"{e('shield')} <b>تنبيه مراجعة: تطابق محتمل في قائمة قنواتك</b>\n\n"
                f"<b>الاسم:</b> {html_escape(name)}\n"
                f"<b>الحساب:</b> {html_escape(label)}\n"
                f"<b>ID:</b> <code>{int(sender.id)}</code>\n\n"
                + _scam_result_text(label, result)
            )
            owner_chat_id = connection.get("owner_chat_id") or connection.get("owner_user_id")
            if owner_chat_id:
                safe_send_message(int(owner_chat_id), text, disable_web_page_preview=True)
    except Exception as exc:
        print(f"automatic scam check failed: {type(exc).__name__}", flush=True)
    finally:
        with _SCAM_AUTO_LOCK:
            _SCAM_AUTO_INFLIGHT.discard(key)
            _SCAM_AUTO_CACHE[key] = (time.time(), hits_found)
        _SCAM_AUTO_SEM.release()

@bot.callback_query_handler(func=lambda c: c.data == "adm_mode_cycle")
def cb_adm_mode_cycle(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "مش مسموح.", show_alert=True)
        return
    new_mode = _next_access_mode(access_mode())
    set_access_mode(new_mode)
    bot.answer_callback_query(call.id, f"الوضع دلوقتي: {ACCESS_MODE_INFO[new_mode]['label']}")
    _adm_edit(call, adm_home_text(), admin_panel_markup())

def admin_backup_menu_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn("تصدير نسخة كاملة (Backup)", callback_data="adm_backup_export", icon="download", style="primary"))
    markup.add(pe.btn("استعادة نسخة كاملة (Restore)", callback_data="adm_backup_restore", icon="upload", style="success"))
    markup.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_backup_back", icon="arrow_left", style="danger"))
    return markup

@bot.callback_query_handler(func=lambda c: c.data == "adm_backup_menu")
def cb_adm_backup_menu(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    _adm_edit(
        call,
        adm_home_text(
            f"{e('shield')} <b>النسخة الاحتياطية الكاملة</b>\n\n"
            "دي نسخة من قاعدة بيانات البوت بالكامل: كل الحسابات، كل الإعدادات، كل شيء.\n"
            f"{e('cross')} <i>الاستعادة بتمسح كل البيانات الحالية وتستبدلها بالكامل — استخدمها بحذر.</i>\n\n"
        ),
        admin_backup_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "adm_backup_back")
def cb_adm_backup_back(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    _adm_edit(call, adm_home_text(), admin_panel_markup())

_BACKUP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKUP_EXTRA_FILES = ("youtube_cookies.txt", "cookies.txt", "proxies.txt", "owner_photo.jpg", "default_welcome.jpg")
_BACKUP_SKIP_DIRS = {"venv", ".venv", "__pycache__", "node_modules", ".git", "site-packages"}
_BACKUP_MAX_SEND = 49 * 1024 * 1024
_BACKUP_MAX_RESTORE = 20 * 1024 * 1024

def _build_full_backup_zip(include_hosted=False):
    import zipfile
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        data = db.export_full_backup()
        zf.writestr("db_dump.json", json.dumps(data, ensure_ascii=False))
        zf.writestr("backup_info.txt", f"created_at={int(time.time())}\n")
        for name in _BACKUP_EXTRA_FILES:
            p = os.path.join(_BACKUP_BASE_DIR, name)
            if os.path.isfile(p):
                zf.write(p, "files/" + name)
    return bio.getvalue()

def _make_backup_payload():
    payload = _build_full_backup_zip()
    return payload, ""

def _send_backup_to(chat_id, caption_prefix=""):
    payload, note = _make_backup_payload()
    bio = io.BytesIO(payload)
    bio.name = time.strftime("Full_Backup_%Y-%m-%d_%H-%M.zip")
    size_mb = len(payload) / 1024 / 1024
    warn = ""
    if len(payload) > _BACKUP_MAX_RESTORE:
        warn = "\n⚠️ حجم الملف أكبر من 20MB، فرفعه من تيليجرام للاستعادة مش هيشتغل."
    bot.send_document(
        chat_id, bio,
        caption=f"{caption_prefix}<b>نسخة احتياطية كاملة</b> ({size_mb:.1f}MB)\n"
                "فيها: كل المستخدمين والإعدادات والمميزين والمشرفين والكوكيز."
                f"{note}{warn}\nاحتفظ بيها في مكان آمن.",
        parse_mode="HTML",
    )

@bot.callback_query_handler(func=lambda c: c.data == "adm_backup_export")
def cb_adm_backup_export(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id, "جاري تجهيز النسخة...")
    try:
        _send_backup_to(call.message.chat.id)
    except Exception as exc:
        print(f"Full backup export send failed: {exc}", flush=True)
        safe_send_message(call.message.chat.id, f"{e('cross')} حصل خطأ أثناء عمل النسخة الاحتياطية.")

@bot.callback_query_handler(func=lambda c: c.data == "adm_backup_restore")
def cb_adm_backup_restore(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    msg = safe_send_message(
        call.message.chat.id,
        f"{e('upload')} <b>ابعت ملف النسخة الاحتياطية (.zip)</b>\n\n"
        f"{e('cross')} <i>تحذير: هيمسح بيانات البوت الحالية ويرجّع كل حاجة زي ما كانت في النسخة.</i>\n"
        "ابعت «إلغاء» لو غيرت رأيك.",
    )
    bot.register_next_step_handler(msg, _receive_admin_backup_file)

def _refresh_staff_sets():
    try:
        _BANNED_IDS.clear(); _BANNED_IDS.update(db.list_banned_bot_ids())
        _SUPERVISOR_IDS.clear(); _SUPERVISOR_IDS.update(db.list_supervisor_ids())
    except Exception as exc:
        print(f"refresh staff sets failed: {exc}", flush=True)

def _restore_zip_backup(raw):
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = zf.namelist()
    if "db_dump.json" not in names:
        raise ValueError("not a full backup")
    data = json.loads(zf.read("db_dump.json").decode("utf-8"))
    if "tables" not in data:
        raise ValueError("bad db_dump")
    db.restore_full_backup(data)
    _refresh_staff_sets()
    restored_files = 0
    for n in names:
        if n.startswith("files/") and n[6:] in _BACKUP_EXTRA_FILES:
            with open(os.path.join(_BACKUP_BASE_DIR, n[6:]), "wb") as out:
                out.write(zf.read(n))
            restored_files += 1
    hosted = 0
    return restored_files, hosted

def _receive_admin_backup_file(message):
    if not is_admin(message.from_user.id):
        return
    if (message.text or "").strip() in ("إلغاء", "الغاء", "/cancel"):
        safe_send_message(message.chat.id, f"{e('cross')} اتلغى.", reply_markup=admin_panel_markup())
        return
    if message.content_type != "document" or not message.document:
        msg = safe_send_message(message.chat.id, f"{e('cross')} ابعت ملف النسخة كمرفق (Document) مش نص عادي، أو «إلغاء».")
        bot.register_next_step_handler(msg, _receive_admin_backup_file)
        return
    if (message.document.file_size or 0) > _BACKUP_MAX_RESTORE:
        safe_send_message(message.chat.id, f"{e('cross')} الملف أكبر من 20MB وتيليجرام مش بيسمح للبوت يحمّله.", reply_markup=admin_panel_markup())
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        raw = bot.download_file(file_info.file_path)
    except Exception as exc:
        msg = safe_send_message(message.chat.id, f"{e('cross')} مقدرتش أحمّل الملف ({exc}). ابعته تاني أو «إلغاء».")
        bot.register_next_step_handler(msg, _receive_admin_backup_file)
        return
    try:
        if raw[:2] == b"PK":
            files_n, hosted_n = _restore_zip_backup(raw)
            extra = f"\n• ملفات مستعادة: {files_n}"
        else:
            data = json.loads(raw.decode("utf-8"))
            if "tables" not in data:
                raise ValueError("not a full backup")
            db.restore_full_backup(data)
            _refresh_staff_sets()
            extra = ""
    except Exception as exc:
        print(f"Full backup restore failed: {exc}", flush=True)
        safe_send_message(message.chat.id, f"{e('cross')} الملف ده مش نسخة احتياطية صحيحة أو حصل خطأ في الاستعادة.", reply_markup=admin_panel_markup())
        return
    safe_send_message(
        message.chat.id,
        f"{e('check')} <b>تمت استعادة النسخة الاحتياطية بنجاح!</b>{extra}\n\n"
        "يفضل تعيد تشغيل البوت دلوقتي.",
        reply_markup=admin_panel_markup(),
    )

# ---------- كوكيز يوتيوب من لوحة الأدمن ----------
_YT_CK_DB_KEY = "youtube_cookies_text"
_YT_CK_AUTH_NAMES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO",
                     "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}
_YT_CK_WARN_DAYS = 3
_yt_ck_lock = threading.Lock()
_yt_ck_last_notify = 0.0
_yt_ck_rejected = False

_YT_CK_LABELS = {
    "missing": "❌ مفيش كوكيز متسجلة",
    "invalid": "❌ الملف الحالي مفيهوش كوكيز تسجيل دخول",
    "expired": "❌ الكوكيز انتهت",
    "rejected": "⚠️ يوتيوب رفض الكوكيز الحالية (غالبًا انتهت أو اتلغت)",
    "expiring": "⚠️ الكوكيز هتنتهي قريب",
    "ok": "✅ الكوكيز شغالة",
}

def _yt_ck_path():
    return YOUTUBE_COOKIES_FILE or os.path.join(_BACKUP_BASE_DIR, "youtube_cookies.txt")

def _yt_ck_inspect(text):
    total, auth, expiries = 0, set(), []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r\n")
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, exp, name = parts[0], parts[4], parts[5]
        if "youtube.com" not in domain and "google.com" not in domain:
            continue
        total += 1
        if name in _YT_CK_AUTH_NAMES:
            auth.add(name)
            if exp.isdigit() and int(exp) > 0:
                expiries.append(int(exp))
    return {"total": total, "auth": auth, "expiry": min(expiries) if expiries else None}

def _yt_ck_state():
    try:
        with open(_yt_ck_path(), "r", encoding="utf-8", errors="ignore") as fh:
            info = _yt_ck_inspect(fh.read())
    except OSError:
        return "missing", None, None
    if not info["auth"]:
        return "invalid", info, None
    days = None
    if info["expiry"]:
        days = (info["expiry"] - time.time()) / 86400
        if days <= 0:
            return "expired", info, days
    if _yt_ck_rejected:
        return "rejected", info, days
    if days is not None and days <= _YT_CK_WARN_DAYS:
        return "expiring", info, days
    return "ok", info, days

def _yt_ck_status_text():
    code, info, days = _yt_ck_state()
    lines = [f"{e('shield')} <b>كوكيز يوتيوب</b>\n", _YT_CK_LABELS[code]]
    if info and info["total"]:
        lines.append(f"• عدد الكوكيز: <b>{info['total']}</b>")
    if days is not None:
        lines.append(f"• أقرب انتهاء لكوكيز الدخول: <b>{max(0, int(days))}</b> يوم")
    lines.append("\nدوس «رفع كوكيز جديد» وابعت الملف (.txt بصيغة Netscape).")
    return "\n".join(lines)

def _yt_ck_markup():
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(pe.btn(" رفع كوكيز جديد", callback_data="adm_ytck_up", icon="upload", style="success"))
    m.add(pe.btn(" حذف الكوكيز", callback_data="adm_ytck_del", icon="trash", style="danger"))
    m.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_open", icon="arrow_left"))
    return m

def _yt_ck_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
    os.replace(tmp, path)

def _yt_ck_save(text):
    global YOUTUBE_COOKIES_FILE, _yt_ck_rejected, _yt_ck_last_notify
    data = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\ufeff \n") + "\n"
    first = data.lstrip().split("\n", 1)[0]
    if "Netscape HTTP Cookie File" not in first and "# HTTP Cookie File" not in first:
        data = "# Netscape HTTP Cookie File\n" + data
    path = _yt_ck_path()
    try:
        _yt_ck_write(path, data)
    except OSError:
        path = os.path.join(_BACKUP_BASE_DIR, "youtube_cookies.txt")
        _yt_ck_write(path, data)
    YOUTUBE_COOKIES_FILE = path
    try:
        db.set_setting(_YT_CK_DB_KEY, data)
    except Exception as exc:
        print(f"youtube cookies: فشل الحفظ في قاعدة البيانات: {exc}", flush=True)
    _yt_ck_rejected = False
    _yt_ck_last_notify = 0.0
    return path

def _yt_ck_delete():
    global _yt_ck_rejected
    try:
        os.remove(_yt_ck_path())
    except OSError:
        pass
    try:
        db.set_setting(_YT_CK_DB_KEY, "")
    except Exception:
        pass
    _yt_ck_rejected = False

def _yt_ck_restore_from_db():
    try:
        path = _yt_ck_path()
        if os.path.isfile(path) and _cookie_file_valid(path):
            return
        text = db.get_setting(_YT_CK_DB_KEY, "") or ""
        if text.strip():
            _yt_ck_save(text)
            print("youtube cookies: اتستعادت من قاعدة البيانات", flush=True)
    except Exception as exc:
        print(f"youtube cookies: فشلت الاستعادة: {exc}", flush=True)

def _yt_ck_notify(reason, min_gap):
    global _yt_ck_last_notify
    with _yt_ck_lock:
        now = time.time()
        if now - _yt_ck_last_notify < min_gap:
            return
        _yt_ck_last_notify = now
    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(pe.btn(" رفع كوكيز جديد", callback_data="adm_ytck_up", icon="upload", style="success"))
        safe_send_message(
            int(config.ADMIN_ID),
            f"{e('shield')} <b>تنبيه كوكيز يوتيوب</b>\n\n{reason}\n\n"
            "دوس الزرار وابعت ملف الكوكيز الجديد (.txt).",
            reply_markup=markup,
        )
    except Exception as exc:
        print(f"youtube cookies: فشل إرسال التنبيه: {exc}", flush=True)

def _yt_ck_on_block(had_cookies):
    global _yt_ck_rejected
    try:
        if had_cookies:
            _yt_ck_rejected = True
            _yt_ck_notify(
                "يوتيوب رفض التحميل حتى مع الكوكيز الحالية، غالبًا انتهت أو اتلغت. "
                "ارفع كوكيز جديدة. لو الرفض استمر بعد الرفع، يبقى الـ IP بتاع السيرفر نفسه محجوب.",
                6 * 3600)
        else:
            _yt_ck_notify("يوتيوب طلب تسجيل دخول والبوت مفيش عنده كوكيز. ارفع كوكيز يوتيوب.", 6 * 3600)
    except Exception:
        pass

def _yt_ck_on_success():
    global _yt_ck_rejected
    _yt_ck_rejected = False

def _yt_ck_watch_loop():
    time.sleep(90)
    while True:
        try:
            code, info, days = _yt_ck_state()
            if code == "missing":
                _yt_ck_notify("مفيش كوكيز يوتيوب على السيرفر (ممكن السيرفر اتعاد تشغيله فاتمسحت).", 24 * 3600)
            elif code == "invalid":
                _yt_ck_notify("ملف الكوكيز الحالي مفيهوش كوكيز تسجيل دخول.", 24 * 3600)
            elif code == "expired":
                _yt_ck_notify("كوكيز يوتيوب انتهت.", 24 * 3600)
            elif code == "expiring":
                _yt_ck_notify(f"كوكيز يوتيوب هتنتهي بعد حوالي {max(0, int(days))} يوم.", 24 * 3600)
        except Exception as exc:
            print(f"youtube cookies watcher: {exc}", flush=True)
        time.sleep(6 * 3600)

@bot.callback_query_handler(func=lambda c: c.data == "adm_ytck")
def cb_adm_ytck(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    _adm_edit(call, _yt_ck_status_text(), _yt_ck_markup())

@bot.callback_query_handler(func=lambda c: c.data == "adm_ytck_up")
def cb_adm_ytck_up(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    msg = safe_send_message(
        call.message.chat.id,
        f"{e('upload')} <b>ابعت ملف الكوكيز (.txt بصيغة Netscape)</b>\n\n"
        "الأفضل تبعته كملف. ابعت «إلغاء» للرجوع.",
    )
    bot.register_next_step_handler(msg, _receive_yt_cookies)

@bot.callback_query_handler(func=lambda c: c.data == "adm_ytck_del")
def cb_adm_ytck_del(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    _yt_ck_delete()
    bot.answer_callback_query(call.id, "اتمسحت")
    _adm_edit(call, _yt_ck_status_text(), _yt_ck_markup())

def _receive_yt_cookies(message):
    if not is_admin(message.from_user.id):
        return
    if _adm_step_interrupted(message):
        return
    chat_id = message.chat.id
    if (message.text or "").strip() in ("إلغاء", "الغاء"):
        safe_send_message(chat_id, f"{e('cross')} اتلغى.", reply_markup=_yt_ck_markup())
        return

    def _again(txt):
        m = safe_send_message(chat_id, f"{e('cross')} {txt}")
        bot.register_next_step_handler(m, _receive_yt_cookies)

    text = None
    if message.content_type == "document" and message.document:
        if (message.document.file_size or 0) > 2 * 1024 * 1024:
            return _again("الملف كبير أوي. ابعت ملف الكوكيز الصح أو «إلغاء».")
        try:
            fi = bot.get_file(message.document.file_id)
            text = bot.download_file(fi.file_path).decode("utf-8", errors="ignore")
        except Exception as exc:
            return _again(f"مقدرتش أحمّل الملف ({exc}). ابعته تاني أو «إلغاء».")
    elif message.content_type == "text" and "\t" in (message.text or ""):
        text = message.text
    else:
        return _again("ابعت ملف الكوكيز كمرفق (Document) أو «إلغاء».")

    info = _yt_ck_inspect(text)
    if not info["auth"]:
        return _again("الملف ده مفيهوش كوكيز تسجيل دخول يوتيوب (SID / LOGIN_INFO). "
                      "اتأكد إنك مسجّل دخول وقت التصدير وإنك اخترت ALL، وابعته تاني أو «إلغاء».")
    try:
        _yt_ck_save(text)
    except OSError as exc:
        return _again(f"مقدرتش أحفظ الملف على السيرفر ({exc}).")
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    _adm_log(message.from_user.id, "yt_cookies_upload", None, f"{info['total']} cookies")
    safe_send_message(
        chat_id,
        f"{e('check')} <b>تم حفظ الكوكيز والبوت بيستخدمها فورًا (من غير إعادة تشغيل).</b>\n\n"
        + _yt_ck_status_text(),
        reply_markup=_yt_ck_markup(),
    )

def _auto_backup_loop():
    if os.environ.get("AUTO_BACKUP", "1") == "0":
        return
    try:
        interval = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24")) * 3600
    except Exception:
        interval = 86400
    time.sleep(60)
    while True:
        try:
            last = float(db.get_setting("last_auto_backup", 0) or 0)
            now = time.time()
            if last == 0:
                db.set_setting("last_auto_backup", int(now))
            elif now - last >= interval:
                _send_backup_to(config.ADMIN_ID, "🕒 ")
                db.set_setting("last_auto_backup", int(now))
        except Exception as exc:
            print(f"auto backup failed: {exc}", flush=True)
        time.sleep(600)

FEATURE_LABEL_TO_KEY = {info["label"]: key for key, info in FEATURES.items()}
BACK_TO_ADMIN_LABEL = "رجوع للوحة الأدمن"
TOGGLE_ALL_PREFIX = "البوت بالكامل"

def admin_features_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keys = list(FEATURES.keys())
    for i in range(0, len(keys), 2):
        row_keys = keys[i:i + 2]
        kb.row(*[
            types.KeyboardButton(
                f"{FEATURES[k]['label']} : {'مفعّلة' if feature_enabled(k) else 'معطلة'}"
            )
            for k in row_keys
        ])
    all_state = "مفعّل" if bot_enabled() else "معطل"
    kb.row(types.KeyboardButton(f"{TOGGLE_ALL_PREFIX} : {all_state}"))
    kb.row(types.KeyboardButton(BACK_TO_ADMIN_LABEL))
    return kb

@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and (m.text or "").split(" : ")[0].strip() in FEATURE_LABEL_TO_KEY
)
def admin_feature_kb_toggle(message):
    label = message.text.split(" : ")[0].strip()
    key = FEATURE_LABEL_TO_KEY[label]
    new_state = not feature_enabled(key)
    set_feature_enabled(key, new_state)
    safe_send_message(
        message.chat.id,
        f"{e('check') if new_state else e('cross')} <b>{html_escape(label)}:</b> "
        f"{'اتفعّلت' if new_state else 'اتعطّلت'} لكل حسابات البوت.",
        reply_markup=admin_features_keyboard(),
    )

@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and (m.text or "").split(" : ")[0].strip() == TOGGLE_ALL_PREFIX
)
def admin_feature_kb_toggle_all(message):
    current = bot_enabled()
    db.set_setting("bot_enabled", "0" if current else "1")
    safe_send_message(
        message.chat.id,
        f"{e('check')} <b>البوت بالكامل:</b> {'اتوقف' if current else 'اشتغل'}.",
        reply_markup=admin_features_keyboard(),
    )

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and (m.text or "").strip() == BACK_TO_ADMIN_LABEL)
def admin_feature_kb_back(message):
    safe_send_message(message.chat.id, f"{e('check')} <b>تم إغلاق لوحة الميزات.</b>", reply_markup=types.ReplyKeyboardRemove())
    safe_send_message(message.chat.id, "<b>لوحة تحكم الأدمن</b>", reply_markup=admin_panel_markup())

def force_sub_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" إضافة قناة", callback_data="fsub_addchannel", icon="plus"),
        pe.btn(" القنوات المضافة", callback_data="fsub_listchannels", icon="document"),
    )
    markup.add(pe.btn(" تغيير شكل الرسالة", callback_data="fsub_setmessage", icon="pencil"))
    markup.add(pe.btn(" حذف كل القنوات", callback_data="fsub_disable", icon="cross"))
    markup.add(pe.btn(" رجوع", callback_data="fsub_back", icon="arrow_left"))
    return markup

def force_sub_channels_list_markup(owner_id, back_action):
    channels = db.list_force_sub_channels(owner_id)
    markup = types.InlineKeyboardMarkup()
    for c in channels:
        prefix = "fsub_delchannel_" if owner_id is None else "owner_fsub_delchannel_"
        markup.add(pe.btn(f" حذف {c['channel']}", callback_data=f"{prefix}{c['id']}", icon="trash", style="danger"))
    markup.add(pe.btn(" رجوع", callback_data=back_action, icon="arrow_left"))
    return markup

def _help_btn(key):
    # لو ضفت إيموجي لمبة في EMOJI باسم "bulb" هتظهر هي، وإلا بيستخدم أيقونة الكتاب
    icon = "bulb" if "bulb" in pe.EMOJI else "book"
    return pe.btn(" شرح", callback_data=f"hlp:{key}", icon=icon, style="primary")


_HELP_SHORT_LABELS = {
    "السوشيال ميديا: شغّالة/مقفولة": "حالة السوشيال",
    "رسائل / ملصقات / ملصقات مميزة": "الرسائل والملصقات",
    "فيديوهات / صور / ملفات": "صور وفيديو وملفات",
    "صوتيات / gif / لينكات": "صوتيات وروابط",
    "الميزة: مقفولة / لحسابي فقط / في الشات": "صلاحية المحوّل",
    "أزرار العملات (دولار، جنيه، TON...)": "أزرار العملات",
    "تفعيل الكل / تعطيل الكل": "تفعيل/تعطيل الكل",
    "تصفح العملات للإيداع / بحث": "العملات والإيداع",
    "آخر الإيداعات / السحوبات": "الإيداعات والسحوبات",
    "تصدير الإعدادات (Backup)": "تصدير الإعدادات",
    "استعادة الإعدادات (Restore)": "استعادة الإعدادات",
}


def _help_button_label(label):
    return _HELP_SHORT_LABELS.get(label, label)


def _help_add_pairs(markup, buttons):
    for index in range(0, len(buttons), 2):
        markup.add(*buttons[index:index + 2])


def _help_root_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for key in help_pages.SECTION_ORDER:
        page = help_pages.HELP[key]
        buttons.append(pe.btn(
            f" {_help_button_label(page['title'])}", callback_data=f"hlpsec:{key}",
            icon=page["icon"], style="primary",
        ))
    _help_add_pairs(markup, buttons)
    markup.add(pe.btn(" رجوع للقائمة الرئيسية", callback_data="hlp_home", icon="arrow_left", style="danger"))
    return markup


def _help_section_markup(key, source):
    page = help_pages.HELP[key]
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for index, (icon, name, _description) in enumerate(page["items"]):
        buttons.append(pe.btn(
            f" {_help_button_label(name)}", callback_data=f"hlpit:{key}:{index}:{source}",
            icon=icon, style="primary",
        ))
    _help_add_pairs(markup, buttons)
    back_callback = "hlp:main" if source == "root" else page["back"]
    back_label = "رجوع لقائمة الشروحات" if source == "root" else "رجوع للقسم"
    markup.add(pe.btn(f" {back_label}", callback_data=back_callback, icon="arrow_left", style="danger"))
    return markup


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("hlp:"))
def cb_help_page(call):
    key = (call.data or "").split(":", 1)[1]
    page = help_pages.HELP.get(key)
    if not page:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    if key == "main":
        text, markup = help_pages.help_root_text(), _help_root_markup()
    else:
        text = help_pages.help_section_text(key)
        markup = _help_section_markup(key, "direct")
    safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("hlpsec:"))
def cb_help_root_section(call):
    key = (call.data or "").split(":", 1)[1]
    if key not in help_pages.HELP or key == "main":
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        help_pages.help_section_text(key), call.message.chat.id, call.message.message_id,
        reply_markup=_help_section_markup(key, "root"),
    )


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("hlplist:"))
def cb_help_item_list(call):
    parts = (call.data or "").split(":")
    if len(parts) != 3 or parts[1] not in help_pages.HELP or parts[1] == "main" or parts[2] not in ("root", "direct"):
        bot.answer_callback_query(call.id)
        return
    key, source = parts[1], parts[2]
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        help_pages.help_section_text(key), call.message.chat.id, call.message.message_id,
        reply_markup=_help_section_markup(key, source),
    )


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("hlpit:"))
def cb_help_item(call):
    parts = (call.data or "").split(":")
    if len(parts) != 4 or parts[1] not in help_pages.HELP or parts[1] == "main" or parts[3] not in ("root", "direct"):
        bot.answer_callback_query(call.id)
        return
    key, source = parts[1], parts[3]
    try:
        index = int(parts[2])
        if index < 0 or index >= len(help_pages.HELP[key]["items"]):
            raise ValueError
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(pe.btn(
        " رجوع لشرح القسم", callback_data=f"hlplist:{key}:{source}",
        icon="arrow_left", style="danger",
    ))
    safe_edit_message_text(
        help_pages.help_item_text(key, index), call.message.chat.id, call.message.message_id,
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data == "hlp_home")
def cb_help_home(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    safe_edit_message_text(main_menu_text(uid), call.message.chat.id, call.message.message_id,
                           reply_markup=owner_panel_markup(uid))


@bot.callback_query_handler(func=lambda c: c.data == "ip_back")
def cb_inplace_back(call):
    """رجوع من رسالة طلب إدخال/نتيجة لنفس الصفحة اللي كانت قدامه (وبيلغي أي سؤال مستني)."""
    chat_id, mid = call.message.chat.id, call.message.message_id
    try:
        bot.clear_step_handler_by_chat_id(chat_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)
    entry = _INPLACE_CACHE.pop((chat_id, mid), None)
    if entry and entry.get("text"):
        params = {"chat_id": chat_id, "message_id": mid, "text": entry["text"], "parse_mode": "HTML"}
        if entry.get("markup"):
            params["reply_markup"] = json.dumps(entry["markup"], ensure_ascii=False)
        try:
            apihelper._make_request(config.BOT_TOKEN, "editMessageText", method="post", params=params)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            print(f"ip_back restore failed, showing main panel: {str(exc)[:160]}", flush=True)
    uid = call.from_user.id
    safe_edit_message_text(main_menu_text(uid), chat_id, mid, reply_markup=owner_panel_markup(uid))


def owner_panel_markup(owner_id=None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    owner_id = owner_id or int(config.ADMIN_ID)
    # (المفتاح، النص، callback، الأيقونة، اللون، عرض كامل؟)
    sections = [
        ("auto_replies", " الردود التلقائية", "replies_menu", "robot", None, False),
        ("banned_words", " الكلمات الممنوعة", "banned_menu", "cross", None, False),
        ("muted", " المكتومين", "muted_menu", "mute", None, False),
        ("force_sub", " الاشتراك الإجباري", "owner_forcesub", "bullhorn", None, False),
        ("broadcast", " الإذاعة", "broadcast_menu", "megaphone", None, False),
        ("social_media", " السوشيال ميديا", "social_menu", "download", None, False),
        ("media", " الميديا", "owner_media_menu", "camera", None, False),
        ("currency_convert", " تحويل العملات", "cvtm_menu", "diamond", None, False),
        ("binance_hub", " بينانس", "bn_menu", "diamond", "success", True),
    ]
    pending = []

    def _flush():
        if pending:
            markup.row(*pending)
            pending.clear()

    def _add(button, full=False):
        if full:
            _flush()
            markup.row(button)
            return
        pending.append(button)
        if len(pending) == 2:
            _flush()

    for key, label, callback, icon, style, full in sections:
        if key not in FEATURES:
            continue
        if key == "social_media":
            enabled = social_access(owner_id) != "disabled"
        else:
            enabled = owner_feature_on(key, owner_id)
        if enabled:
            _add(pe.btn(label, callback_data=callback, icon=icon, style="primary"))
    _flush()
    _add(pe.btn(" التحكم في الميزات", callback_data="ctl_menu", icon="features_mgmt", style="primary"))
    _add(pe.btn(" النسخ الاحتياطي", callback_data="backup_menu", icon="download", style="primary"))
    _flush()
    _add(_help_btn("main"))
    _add(pe.btn(
        " ربط الحساب", url="tg://settings/business",
        icon="link", style="primary",
    ))
    if trial_expired(owner_id):
        _add(pe.btn(
            " تواصل مع المطور", url=f"tg://user?id={config.DEVELOPER_ID}",
            icon="chat", style="success",
        ))
    _flush()
    return markup

def _feature_is_on(key, owner_id):
    if key == "social_media":
        return social_access(owner_id) != "disabled"
    return owner_feature_on(key, owner_id)

def features_list_text(owner_id):
    lines = []
    for key, info in FEATURES.items():
        on = _feature_is_on(key, owner_id)
        lines.append(f"{e('check') if on else e('cross')} {html_escape(info['label'])}")
    return f"{e('features_mgmt')} <b>مميزات البوت</b>\n\n" + "\n".join(lines)

@bot.callback_query_handler(func=lambda c: c.data == "features_send")
def cb_features_send(call):
    if not _require_active_connection(call):
        return
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    safe_send_message(chat_id, welcome_text(call.from_user), reply_markup=welcome_keyboard())
    safe_send_message(chat_id, features_list_text(call.from_user.id))

def backup_menu_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn("تصدير الإعدادات (Backup)", callback_data="backup_export", icon="download", style="primary"))
    markup.add(pe.btn("استعادة الإعدادات (Restore)", callback_data="backup_restore", icon="upload", style="success"))
    markup.add(_help_btn("backup"))
    markup.add(pe.btn(" رجوع", callback_data="backup_back", icon="arrow_left", style="danger"))
    return markup

@bot.callback_query_handler(func=lambda c: c.data == "backup_menu")
def cb_backup_menu(call):
    if not _require_active_connection(call):
        return
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"{e('folder')} <b>قسم النسخ الاحتياطي</b>\n\n"
        "يمكنك تصدير إعداداتك (الردود التلقائية، القوائم، تفعيل/تعطيل الميزات) أو استعادتها.",
        call.message.chat.id, call.message.message_id,
        reply_markup=backup_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "backup_back")
def cb_backup_back(call):
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )

@bot.callback_query_handler(func=lambda c: c.data == "backup_export")
def cb_backup_export(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    bot.answer_callback_query(call.id, "جاري التجهيز...")
    data = db.export_owner_backup(owner_id)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    bio = io.BytesIO(payload)
    bio.name = f"Owner_Backup_{owner_id}.json"
    try:
        bot.send_document(
            call.message.chat.id, bio,
            caption=f"{e('check')} <b>دي نسختك الاحتياطية.</b>\n"
                    "احتفظ بالملف ده في مكان آمن، وتقدر ترفعه تاني لاستعادة كل إعداداتك وقت ما تحب.",
            parse_mode="HTML",
        )
    except Exception as exc:
        print(f"Owner backup export send failed: {exc}", flush=True)
        safe_send_message(call.message.chat.id, f"{e('cross')} حصل خطأ أثناء إرسال النسخة الاحتياطية.")

@bot.callback_query_handler(func=lambda c: c.data == "backup_restore")
def cb_backup_restore(call):
    if not _require_active_connection(call):
        return
    bot.answer_callback_query(call.id)
    msg = safe_send_message(
        call.message.chat.id,
        f"{e('upload')} <b>ابعت ملف الباك أب (.json) اللي صدّرته قبل كده.</b>\n\n"
        "<i>تحذير: هيمسح إعداداتك الحالية (الردود، القوائم، تفعيل/تعطيل الميزات) "
        "ويستبدلها باللي في الملف.</i>\n"
        "ابعت «إلغاء» لو غيرت رأيك.",
    )
    bot.register_next_step_handler(msg, _receive_owner_backup_file)

def _receive_owner_backup_file(message):
    owner_id = message.from_user.id
    if (message.text or "").strip() in ("إلغاء", "الغاء", "/cancel"):
        safe_send_message(message.chat.id, f"{e('cross')} اتلغى.", reply_markup=owner_panel_markup(owner_id))
        return
    if message.content_type != "document" or not message.document:
        msg = safe_send_message(message.chat.id, f"{e('cross')} ابعت ملف الباك أب كمرفق (Document) مش نص عادي.")
        bot.register_next_step_handler(msg, _receive_owner_backup_file)
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        raw = bot.download_file(file_info.file_path)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        msg = safe_send_message(message.chat.id, f"{e('cross')} الملف تالف أو مش JSON صحيح ({exc}). ابعت الملف الصح أو «إلغاء».")
        bot.register_next_step_handler(msg, _receive_owner_backup_file)
        return
    backup_owner = data.get("owner_id")
    if backup_owner is not None and int(backup_owner) != int(owner_id):
        safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>الملف ده نسخة احتياطية لحساب تاني، مينفعش تستخدمه هنا.</b>",
            reply_markup=owner_panel_markup(owner_id),
        )
        return
    try:
        db.restore_owner_backup(owner_id, data)
    except Exception as exc:
        print(f"Owner backup restore failed: {exc}", flush=True)
        safe_send_message(message.chat.id, f"{e('cross')} حصل خطأ أثناء الاستعادة.", reply_markup=owner_panel_markup(owner_id))
        return
    safe_send_message(
        message.chat.id,
        f"{e('check')} <b>تم استعادة إعداداتك بنجاح!</b>",
        reply_markup=owner_panel_markup(owner_id),
    )

def calc_menu_markup(owner_id):
    access = calc_access(owner_id)
    info = CALC_ACCESS_INFO[access]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(pe.btn(f" {info['label']}", callback_data="calc_access_cycle", icon=info["icon"], style=info["style"]))
    markup.add(pe.btn(" رجوع", callback_data="calc_back", icon="arrow_left"))
    return markup

def _calc_menu_text(owner_id):
    access = calc_access(owner_id)
    state_txt = CALC_ACCESS_INFO[access]["label"]
    return (
        f"{e('sparkles')} <b>الآلة الحاسبة</b>\n\n"
        "تقدر تفتح لوحة حاسبة جوه أي شات عميل بطريقتين:\n"
        "1- تكتب معادلة مباشرة زي: <code>5+6</code> أو <code>20-4</code>\n"
        "2- تكتب: <code>الحاسبة</code> أو <code>الآلة الحاسبة</code>\n\n"
        f"{e('lock')} <b>الحالة الحالية:</b> {state_txt}\n"
        "<i>(افتراضيًا مقفولة على حسابك بس)</i>\n\n"
        "دوس على الزرار تحت عشان تغيّر مين يقدر يستخدمها."
    )

_SCAM_COMMAND_RE = re.compile(
    r"^(?:فحص\s+كاش(?:\s+(.*))?|/scamcheck(?:@\w+)?(?:\s+(.*))?)$",
    re.S | re.I,
)
_SCAM_LABEL_RE = re.compile(
    r"^(?:رقم\s+(?:(?:ال)?كاش|(?:ال)?(?:آيدي|ايدي|id))|"
    r"(?:ال)?(?:كاش|cash|نصاب|نصابة|محتال|محتالة|قنوات|يوزر|username|user)|"
    r"(?:ال)?(?:آيدي|ايدي|id)|عن|على|هل|هو|هي|ده|دا|دي|هذا|هذه)(?:\s+|$)",
    re.I,
)
_SCAM_NATURAL_INTENT_RE = re.compile(
    r"(?:نصاب|نصابة|محتال|محتالة|احتيال|scammer|"
    r"(?:ال)?(?:كاش|cash|يوزر|username|user|آيدي|ايدي|id))",
    re.I,
)
_SCAM_TARGET_RE = re.compile(
    r"(?:https?://t\.me/[^\s<>]+|t\.me/[^\s<>]+|"
    r"@[A-Za-z0-9_]{5,32}|(?<![0-9٠-٩۰-۹])[0-9٠-٩۰-۹]{4,20}(?![0-9٠-٩۰-۹]))",
    re.I,
)


def _parse_scam_check_command(text, allow_empty=False):
    """Return the target after a scam-check command, or None for another command."""
    text = (text or "").strip()
    match = _SCAM_COMMAND_RE.fullmatch(text)
    if not match:
        return None
    if re.fullmatch(r"فحص\s+كاش", text, re.I):
        return ""
    raw = (match.group(1) if match.group(1) is not None else match.group(2) or "").strip()
    explicit_label = False
    while raw:
        label = _SCAM_LABEL_RE.match(raw)
        if not label:
            break
        explicit_label = True
        raw = raw[label.end():].strip()
    if not raw:
        return "" if (explicit_label or allow_empty) else None
    if (
        not explicit_label
        and ud.extract_url(raw)
        and not re.match(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/", raw, re.I)
    ):
        return None
    return raw

def _redirect_legacy_security(call):
    """Send stale Security Center buttons to the current feature-control menu."""
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    bot.answer_callback_query(call.id, "تم نقل فحص قنوات البلاغات إلى التحكم.")
    safe_edit_message_text(
        _owner_features_text(0, owner_id), call.message.chat.id, call.message.message_id,
        reply_markup=owner_features_keyboard(owner_id, 0),
    )


# Compatibility redirects for inline buttons sent by older bot versions.
@bot.callback_query_handler(func=lambda c: c.data == "security_menu")
def cb_security_menu(call):
    _redirect_legacy_security(call)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sec_toggle_"))
def cb_security_toggle(call):
    _redirect_legacy_security(call)


@bot.callback_query_handler(func=lambda c: c.data in (
    "sec_scancmd_cycle", "sec_scam_audience_cycle", "sec_dom_allow", "sec_dom_block"
))
def cb_security_legacy_controls(call):
    _redirect_legacy_security(call)


@bot.callback_query_handler(func=lambda c: c.data == "sec_back")
def cb_security_back(call):
    bot._owner_panel_context = call.from_user
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )


def _handle_business_scam_check(message, connection, owner_id, bc_id, arg):
    sender = message.from_user
    sender_id = int(sender.id)
    owner_chat_id = int(connection.get("owner_chat_id") or owner_id)
    is_owner = sender_id == int(owner_id)

    def send(text):
        if is_owner:
            safe_send_message(owner_chat_id, text, disable_web_page_preview=True)
        else:
            safe_send_business_message(
                message.chat.id, bc_id, text,
                parse_mode="HTML", disable_web_page_preview=True,
            )

    if not is_owner:
        send("أمر فحص قنوات البلاغات متاح للمدير فقط.")
        return
    if not owner_feature_on("scam_check", owner_id) or not audience_allows(
        "scam_check", owner_id, sender_id
    ):
        send("فحص قنوات البلاغات غير متاح بهذه الصلاحية.")
        return
    if not _scam_channels():
        send("لا توجد قنوات عامة مضافة للفحص حتى الآن.")
        return

    replied = getattr(message, "reply_to_message", None)
    replied_user = getattr(replied, "from_user", None) if replied else None
    targets = []
    if arg:
        try:
            target = scam_checker.normalize_target(arg)
        except ValueError as exc:
            send(f"{html_escape(str(exc))}\nاكتب @username أو ID رقميًا بعد «فحص كاش».")
            return
        targets = [target]
        label = ("@" if target["kind"] == "username" else "ID ") + target["value"]
    else:
        target_user = replied_user or (sender if not is_owner else None)
        if not target_user or getattr(target_user, "is_bot", False) or int(target_user.id) == int(owner_id):
            send("اكتب «فحص كاش» لفحص نفسك، أو أضف @username/ID، أو ردّ على رسالة الشخص المطلوب فحصه.")
            return
        targets.append({"kind": "id", "value": str(int(target_user.id))})
        if getattr(target_user, "username", None):
            targets.insert(0, {"kind": "username", "value": target_user.username.lower()})
        label = f"@{target_user.username}" if getattr(target_user, "username", None) else f"ID {target_user.id}"

    _start_manual_scam_scan(targets, label, send)


def social_menu_markup(owner_id):
    on = social_access(owner_id) != "disabled"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(pe.btn(
        " السوشيال ميديا: " + ("شغّالة" if on else "مقفولة"),
        callback_data="social_access_cycle",
        icon="check" if on else "cross", style="success" if on else "danger",
    ))
    buttons = []
    for key, pf in SOCIAL_PLATFORMS.items():
        info = SONG_ACCESS_INFO[social_p_state(owner_id, key)]
        buttons.append(pe.btn(
            f" {pf['label']} · {info['label']}", callback_data=f"socp_open_{key}",
            icon=pf["icon"], style=info["style"],
        ))
    markup.add(*buttons)
    markup.add(pe.btn(" إعادة ضبط", callback_data="social_reset", icon="admin_panel", style="danger"))
    markup.add(_help_btn("social"))
    markup.add(pe.btn(" رجوع", callback_data="social_back", icon="arrow_left"))
    return markup

def broadcast_menu_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" بدء إذاعة جديدة", callback_data="broadcast_start", icon="megaphone", style="success"))
    markup.add(_help_btn("broadcast"))
    markup.add(pe.btn(" رجوع", callback_data="broadcast_back", icon="arrow_left"))
    return markup

def banned_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" إضافة كلمة", callback_data="banned_add", icon="new", style="success"),
        pe.btn(" مسح كلمة", callback_data="banned_delete_menu", icon="trash", style="danger"),
    )
    markup.add(
        pe.btn(" تخصيص الرد", callback_data="banned_customize", icon="chat_2", style="success"),
        pe.btn(" عرض الكلمات", callback_data="banned_view", icon="document", style="success"),
    )
    markup.add(pe.btn(" مسح جميع الكلمات", callback_data="banned_clear_all", icon="trash", style="danger"))
    markup.add(_help_btn("banned"))
    markup.add(pe.btn(" رجوع", callback_data="banned_back", icon="arrow_left", style="danger"))
    return markup

def muted_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" كتم عميل", callback_data="muted_add", icon="mute"),
        pe.btn(" عرض المكتومين", callback_data="muted_list", icon="document"),
    )
    markup.add(_help_btn("muted"))
    markup.add(pe.btn(" رجوع", callback_data="muted_back", icon="arrow_left"))
    return markup

def blocked_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" حظر عميل", callback_data="blocked_add", icon="shield"),
        pe.btn(" عرض المحظورين", callback_data="blocked_list", icon="document"),
    )
    markup.add(pe.btn(" فك حظر الكل", callback_data="blocked_unblock_all", icon="unlock", style="danger"))
    markup.add(pe.btn(" رجوع", callback_data="blocked_back", icon="arrow_left"))
    return markup

def replies_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" إضافة رد انلاين", callback_data="replies_add", icon="new", style="success"),
        pe.btn(" عرض وحذف الردود", callback_data="replies_view_all", icon="document", style="primary"),
    )
    markup.add(
        pe.btn(" إضافة رد تلقائي", callback_data="online_add", icon="new"),
        pe.btn(" صلاحيات الردود", callback_data="replies_aud_menu_0", icon="lock", style="success"),
    )
    markup.add(_help_btn("replies"))
    markup.add(pe.btn(" رجوع", callback_data="replies_back", icon="arrow_left"))
    return markup


# ---------- صفحات الرسائل الغنية (جداول) مع بديل نصي تلقائي ----------
def _rich_table(headers, rows):
    head = "<tr>" + "".join(f"<td><b>{h}</b></td>" for h in headers) + "</tr>"
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table bordered>{head}{body}</table>"

def _plain_table(headers, rows):
    lines = [" | ".join(f"<b>{h}</b>" for h in headers)] + [" | ".join(row) for row in rows]
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"

SOCIAL_DEMO_VIDEO_URL = os.environ.get("SOCIAL_DEMO_VIDEO_URL", "https://f.top4top.io/m_392740fud1.mp4").strip()

def _social_video_html():
    """بلوك الفيديو اللي بيظهر جوه الرسالة الغنية مع الجداول (نفس فكرة مشغل الصوت في الأغاني)."""
    return '<video src="tg://video?id=demo"></video>'

def _edit_rich_page(chat_id, message_id, rich_html, fallback_html, markup, video=False, rtl=False):
    """editMessageText بـ rich_message (جداول حقيقية).
    video=True: الفيديو يتعرض مع الجدول في نفس الرسالة (من الرابط).
    الترتيب: جدول+فيديو ← جدول بس ← نص عادي."""
    attempts = []
    if video and SOCIAL_DEMO_VIDEO_URL:
        media_ref = SOCIAL_DEMO_VIDEO_URL
        attempts.append({
            "html": rich_html + _social_video_html(),
            "media": [{"id": "demo", "media": {"type": "video", "media": media_ref, "supports_streaming": True}}],
            "skip_entity_detection": True,
        })
    attempts.append({"html": rich_html, "skip_entity_detection": True})
    for rich in attempts:
        if rtl:
            rich["is_rtl"] = True
        try:
            params = {
                "chat_id": chat_id, "message_id": message_id,
                "rich_message": json.dumps(rich, ensure_ascii=False),
            }
            if markup is not None:
                params["reply_markup"] = markup.to_json()
            apihelper._make_request(config.BOT_TOKEN, "editMessageText", method="post", params=params)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            kind = "with video" if rich.get("media") else "table only"
            print(f"rich page edit failed ({kind}), trying next: {str(exc)[:200]}", flush=True)
    try:
        safe_edit_message_text(fallback_html, chat_id, message_id, reply_markup=markup)
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            raise

def _social_commands(key):
    """صفوف جدول الأوامر لكل منصة: (الأمر، الشرح)."""
    mb = ud.MAX_FILE_BYTES // (1024 * 1024)
    link_rows = [
        ("<code>الرابط</code>", "ابعت الرابط لوحده وهيتحمّل"),
        ("<code>تحميل الرابط</code>", "تحميل الرابط (فيديو افتراضيًا)"),
        ("<code>تحميل صوت الرابط</code>", "نفس الرابط كملف صوتي mp3"),
        ("<code>تحميل</code> (ردّ)", "ردّ بيها على رسالة فيها رابط"),
    ]
    if key == "youtube":
        return [
            ("<code>يوت اسم الأغنية</code>", "نتايج بحث ← كارت بصورة الأغنية وأزرار التحميل"),
            ("<code>تحميل اسم الأغنية</code>", "نفس البحث بس صوت فقط (له إعداد مستقل)"),
            ("<code>تحميل الرابط</code>", "رابط يوتيوب مباشر (فيديو)"),
            ("<code>تحميل صوت الرابط</code>", "رابط يوتيوب مباشر (صوت)"),
        ]
    if key == "tiktok":
        return [
            ("<code>تيك توك الرابط</code>", "فيديو بدون علامة مائية"),
            ("<code>تيك توك صوت الرابط</code>", "الصوت بس"),
        ] + link_rows[:3]
    if key == "soundcloud":
        return [
            ("<code>الرابط</code>", "ابعت رابط ساوندكلاود وهيتحمّل صوت mp3"),
            ("<code>تحميل الرابط</code>", f"تحميل الأغنية (لحد {mb}MB)"),
            ("<code>تحميل</code> (ردّ)", "ردّ بيها على رسالة فيها رابط"),
        ]
    if key == "other":
        names = " · ".join(n for n in ud.PLATFORMS if n not in (
            "YouTube", "Instagram", "Facebook", "TikTok", "SoundCloud", "Pinterest", "Snapchat"))
        return [
            ("<code>تحميل الرابط</code>", "بيجرب أي موقع (مئات المواقع مدعومة)"),
            ("<code>تحميل صوت الرابط</code>", "نسخة صوتية من الرابط"),
            ("<code>تحميل</code> (ردّ)", "ردّ بيها على رسالة فيها رابط"),
            ("المواقع الأساسية", html_escape(names)),
        ]
    return link_rows

_STATE_LEGEND = [
    ("مقفول", "المنصة متوقفة خالص"),
    ("الوجهة: خاص", "الملف بتاعك يوصلك في الخاص مع البوت"),
    ("الوجهة: في الشات", "الملف يظهر في نفس الشات"),
    ("الوصول: لحسابي فقط", "إنت بس اللي تقدر تطلب"),
    ("الوصول: عام", "أي حد في الشات يقدر يطلب"),
]

def _social_main_page(owner_id):
    on = social_access(owner_id) != "disabled"
    status_rows = []
    for key, pf in SOCIAL_PLATFORMS.items():
        status_rows.append([
            f"{e(pf['icon'])} {pf['label']}",
            SONG_ACCESS_INFO[social_p_state(owner_id, key)]["label"],
            SOCIAL_TYPES[social_p_types(owner_id, key)],
        ])
    cmd_rows = [
        ("<code>يوت اسم الأغنية</code>", "بحث يوتيوب ← كارت بصورة الأغنية"),
        ("<code>تحميل اسم الأغنية</code>", "بحث ← كارت بصورة + تحميل الصوت فقط"),
        ("<code>تحميل الرابط</code>", "تحميل أي رابط من أي موقع"),
        ("<code>تحميل صوت الرابط</code>", "نفس الرابط كملف صوتي"),
    ]
    title = f"{e('download')} <b>إدارة السوشيال ميديا</b>"
    state_line = (f"{e('check')} شغّالة" if on else f"{e('cross')} مقفولة") + " — اختار منصة من الأزرار لضبط إعداداتها."
    foot = f"الحد الأقصى للملف {ud.MAX_FILE_BYTES // (1024 * 1024)}MB، وأعلى جودة متاحة بدون علامة مائية."
    rich = (
        f"<p>{title}</p><p>{state_line}</p>"
        f"<p><b>حالة المنصات</b></p>{_rich_table(['المنصة', 'الحالة', 'النوع'], status_rows)}"
        f"<p><b>الأوامر</b></p>{_rich_table(['الأمر', 'الشرح'], cmd_rows)}"
        f"<p>{foot}</p>"
    )
    plain = (
        f"{title}\n{state_line}\n\n<b>حالة المنصات</b>\n"
        + _plain_table(["المنصة", "الحالة", "النوع"], status_rows)
        + "\n<b>الأوامر</b>\n" + _plain_table(["الأمر", "الشرح"], cmd_rows) + f"\n{foot}"
    )
    return rich, plain

def _social_platform_page(owner_id, key):
    pf = SOCIAL_PLATFORMS[key]
    state = social_p_state(owner_id, key)
    settings_rows = [
        ["الوصول", "مقفول" if state == "disabled" else ("عام" if state == "public" else "لحسابي فقط")],
        ["الوجهة", "خاص (في الخاص مع البوت)" if social_p_dest(owner_id, key) == "private" else "في الشات"],
        ["النوع", SOCIAL_TYPES[social_p_types(owner_id, key)]],
    ]
    if social_p_has_quality(key):
        settings_rows.append(["جودة الفيديو", SOCIAL_QUALITY[social_p_quality(owner_id, key)]])
    if social_p_has_caption(key):
        settings_rows.append(["الكابشن", "عنوان الفيديو" if social_p_caption(owner_id, key) else "بدون"])
    settings_rows.append(["الانتظار بين طلبات العميل", SOCIAL_COOLDOWN[social_p_cooldown(owner_id, key)]])
    settings_rows.append(["إشعاري بطلبات العملاء", "شغّال" if social_p_notify(owner_id, key) else "مقفول"])
    if key == "youtube":
        settings_rows.append(["أمر «تحميل اسم الأغنية»", SONG_ACCESS_INFO[song_access(owner_id)]["label"]])
    cmd_rows = [list(r) for r in _social_commands(key)]
    legend = [list(r) for r in _STATE_LEGEND]
    title = f"{e(pf['icon'])} <b>{pf['label']}</b>"
    rich = (
        f"<p>{title}</p>"
        f"<p><b>الإعدادات</b></p>{_rich_table(['الإعداد', 'القيمة'], settings_rows)}"
        f"<p><b>الأوامر</b></p>{_rich_table(['الأمر', 'الشرح'], cmd_rows)}"
        f"<p><b>معنى الحالات</b></p>{_rich_table(['الحالة', 'المعنى'], legend)}"
    )
    plain = (
        f"{title}\n\n<b>الإعدادات</b>\n" + _plain_table(["الإعداد", "القيمة"], settings_rows)
        + "\n<b>الأوامر</b>\n" + _plain_table(["الأمر", "الشرح"], cmd_rows)
        + "\n<b>معنى الحالات</b>\n" + _plain_table(["الحالة", "المعنى"], legend)
    )
    return rich, plain

def social_platform_markup(owner_id, key):
    pf = SOCIAL_PLATFORMS[key]
    info = SONG_ACCESS_INFO[social_p_state(owner_id, key)]
    markup = types.InlineKeyboardMarkup(row_width=2)
    cur_state = social_p_state(owner_id, key)
    dest = social_p_dest(owner_id, key)
    is_public = cur_state == "public"
    is_off = cur_state == "disabled"
    markup.row(
        pe.btn(f" تحميل: {'خاص' if dest == 'private' else 'في نفس الشات'}",
               callback_data=f"socp_dt_{key}", icon="lock" if dest == "private" else "chat", style="primary"),
        pe.btn(f" {'عام' if is_public else 'لحسابي فقط'}",
               callback_data=f"socp_ac_{key}", icon="unlock" if is_public else "lock",
               style="success" if is_public else "primary"),
    )
    markup.add(pe.btn(" مقفول ✓" if is_off else " مقفول", callback_data=f"socp_ss_{key}_disabled",
                      icon="cross", style="danger" if is_off else "primary"))
    row1 = []
    if len(pf["types"]) > 1:
        row1.append(pe.btn(f" النوع: {SOCIAL_TYPES[social_p_types(owner_id, key)]}",
                           callback_data=f"socp_ty_{key}", icon="film"))
    if social_p_has_quality(key):
        row1.append(pe.btn(f" الجودة: {SOCIAL_QUALITY[social_p_quality(owner_id, key)]}",
                           callback_data=f"socp_ql_{key}", icon="camera"))
    if row1:
        markup.row(*row1)
    row2 = []
    if social_p_has_caption(key):
        row2.append(pe.btn(f" الكابشن: {'عنوان' if social_p_caption(owner_id, key) else 'بدون'}",
                           callback_data=f"socp_cp_{key}", icon="pencil"))
    row2.append(pe.btn(f" الانتظار: {SOCIAL_COOLDOWN[social_p_cooldown(owner_id, key)]}",
                       callback_data=f"socp_cd_{key}", icon="clock"))
    markup.row(*row2)
    notify_on = social_p_notify(owner_id, key)
    markup.add(pe.btn(f" إشعاري بالطلبات: {'شغّال' if notify_on else 'مقفول'}",
                      callback_data=f"socp_nt_{key}", icon="bell",
                      style="success" if notify_on else "primary"))
    if key == "youtube":
        sinfo = SONG_ACCESS_INFO[song_access(owner_id)]
        markup.add(pe.btn(f" أمر تحميل (أغنية): {sinfo['label']}", callback_data="socp_song",
                          icon="music", style=sinfo["style"]))
    markup.add(pe.btn(" إعادة ضبط", callback_data=f"socp_rs_{key}", icon="admin_panel", style="danger"))
    markup.add(pe.btn(" رجوع", callback_data="social_menu", icon="arrow_left"))
    return markup

def _show_social_main(call, owner_id):
    rich, plain = _social_main_page(owner_id)
    _edit_rich_page(call.message.chat.id, call.message.message_id, rich, plain, social_menu_markup(owner_id), video=True)

def _show_social_platform(call, owner_id, key):
    rich, plain = _social_platform_page(owner_id, key)
    _edit_rich_page(call.message.chat.id, call.message.message_id, rich, plain,
                    social_platform_markup(owner_id, key), video=True)

@bot.callback_query_handler(func=lambda c: c.data == "social_menu")
def cb_social_menu(call):
    if not _require_active_connection(call):
        return
    _show_social_main(call, call.from_user.id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data in ("social_dest_شات", "social_dest_خاص"))
def cb_social_dest_toggle(call):
    owner_id = call.from_user.id
    set_social_dest(owner_id, call.data.split("_", 2)[2])
    bot.answer_callback_query(call.id, "تم الحفظ.")
    _show_social_main(call, owner_id)

@bot.callback_query_handler(func=lambda c: c.data == "social_access_cycle")
def cb_social_access_cycle(call):
    owner_id = call.from_user.id
    new_state = "disabled" if social_access(owner_id) != "disabled" else "owner"
    set_social_access(owner_id, new_state)
    bot.answer_callback_query(call.id, "السوشيال ميديا شغّالة." if new_state != "disabled" else "السوشيال ميديا مقفولة.")
    _show_social_main(call, owner_id)

@bot.callback_query_handler(func=lambda c: c.data == "social_reset")
def cb_social_reset(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    social_reset_all(owner_id)
    bot.answer_callback_query(call.id, "تمت إعادة الضبط: كل المنصات لحسابك فقط.")
    _show_social_main(call, owner_id)

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("socp_"))
def cb_social_platform(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    data = call.data
    if data.startswith("socp_rs_"):
        pkey = data[len("socp_rs_"):]
        if pkey in SOCIAL_PLATFORMS:
            social_reset_platform(owner_id, pkey)
            if pkey == "youtube":
                set_song_access(owner_id, "chat")
            bot.answer_callback_query(call.id, f"{SOCIAL_PLATFORMS[pkey]['label']}: تمت إعادة الضبط لحسابك فقط.")
            _show_social_platform(call, owner_id, pkey)
        else:
            bot.answer_callback_query(call.id)
        return
    if data == "socp_song":
        nxt = SONG_ACCESS_STATES[(SONG_ACCESS_STATES.index(song_access(owner_id)) + 1) % len(SONG_ACCESS_STATES)]
        set_song_access(owner_id, nxt)
        bot.answer_callback_query(call.id, f"أمر تحميل الأغاني: {SONG_ACCESS_INFO[nxt]['label']}")
        _show_social_platform(call, owner_id, "youtube")
        return
    if data.startswith(("socp_dt_", "socp_ac_")):
        kind = data[len("socp_"):len("socp_dt")]
        pkey = data[len("socp_dt_"):]
        if pkey not in SOCIAL_PLATFORMS:
            bot.answer_callback_query(call.id)
            return
        cur = social_p_state(owner_id, pkey)
        if kind == "dt":
            nd = "chat" if social_p_dest(owner_id, pkey) == "private" else "private"
            set_social_p_dest(owner_id, pkey, nd)
            if cur in ("private", "chat"):
                set_social_p_state(owner_id, pkey, nd)
            bot.answer_callback_query(call.id, "التحميل: " + ("خاص" if nd == "private" else "في نفس الشات"))
        else:
            if cur == "public":
                set_social_p_state(owner_id, pkey, _social_owner_state(owner_id, pkey))
                bot.answer_callback_query(call.id, "الوصول: لحسابي فقط")
            else:
                set_social_p_state(owner_id, pkey, "public")
                bot.answer_callback_query(call.id, "الوصول: عام")
        _show_social_platform(call, owner_id, pkey)
        return
    if data.startswith("socp_ss_"):
        pkey, _, st = data[len("socp_ss_"):].rpartition("_")
        if pkey in SOCIAL_PLATFORMS and st in SONG_ACCESS_STATES:
            if st == "disabled" and social_p_state(owner_id, pkey) == "disabled":
                st = "public"
            elif st == social_p_state(owner_id, pkey):
                bot.answer_callback_query(call.id, "دي الحالة الحالية.")
                return
            set_social_p_state(owner_id, pkey, st)
            bot.answer_callback_query(call.id, f"{SOCIAL_PLATFORMS[pkey]['label']}: {SONG_ACCESS_INFO[st]['label']}")
            _show_social_platform(call, owner_id, pkey)
        else:
            bot.answer_callback_query(call.id)
        return
    action, _, key = data[len("socp_"):].partition("_")
    if key not in SOCIAL_PLATFORMS or action not in ("open", "st", "ty", "ql", "cp", "cd", "nt"):
        bot.answer_callback_query(call.id)
        return
    if action == "st":
        cur = social_p_state(owner_id, key)
        # ترتيب الدوران: عام ← في الشات ← خاص ← مقفول ← عام (من المقفول بيرجع شغّال فورًا)
        _order = ["public", "chat", "private", "disabled"]
        nxt = _order[(_order.index(cur) + 1) % len(_order)] if cur in _order else "public"
        set_social_p_state(owner_id, key, nxt)
        bot.answer_callback_query(call.id, f"{SOCIAL_PLATFORMS[key]['label']}: {SONG_ACCESS_INFO[nxt]['label']}")
    elif action == "ty":
        opts = SOCIAL_PLATFORMS[key]["types"]
        cur = social_p_types(owner_id, key)
        nxt = opts[(opts.index(cur) + 1) % len(opts)]
        set_social_p_types(owner_id, key, nxt)
        bot.answer_callback_query(call.id, f"النوع: {SOCIAL_TYPES[nxt]}")
    elif action == "ql":
        nxt = _cycle_key(SOCIAL_QUALITY, social_p_quality(owner_id, key))
        db.set_setting(_social_opt_key("q", key, owner_id), nxt)
        bot.answer_callback_query(call.id, f"الجودة: {SOCIAL_QUALITY[nxt]}")
    elif action == "cp":
        nxt = not social_p_caption(owner_id, key)
        db.set_setting(_social_opt_key("c", key, owner_id), "1" if nxt else "0")
        bot.answer_callback_query(call.id, "الكابشن: عنوان الفيديو" if nxt else "الكابشن: بدون")
    elif action == "cd":
        nxt = _cycle_key(SOCIAL_COOLDOWN, social_p_cooldown(owner_id, key))
        db.set_setting(_social_opt_key("cd", key, owner_id), nxt)
        bot.answer_callback_query(call.id, f"الانتظار بين الطلبات: {SOCIAL_COOLDOWN[nxt]}")
    elif action == "nt":
        nxt = not social_p_notify(owner_id, key)
        db.set_setting(_social_opt_key("n", key, owner_id), "1" if nxt else "0")
        bot.answer_callback_query(call.id, "هيجيلك إشعار بكل طلب." if nxt else "الإشعارات اتقفلت.")
    else:
        bot.answer_callback_query(call.id)
    _show_social_platform(call, owner_id, key)

@bot.callback_query_handler(func=lambda c: c.data == "social_back")
def cb_social_back(call):
    bot._owner_panel_context = call.from_user
    safe_edit_message_text(
        main_menu_text(call.from_user.id),
        call.message.chat.id, call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )

@bot.callback_query_handler(func=lambda c: c.data == "calc_menu")
def cb_calc_menu(call):
    owner_id = call.from_user.id
    safe_edit_message_text(
        _calc_menu_text(owner_id),
        call.message.chat.id, call.message.message_id,
        reply_markup=calc_menu_markup(owner_id),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "calc_access_cycle")
def cb_calc_access_cycle(call):
    owner_id = call.from_user.id
    new_state = _next_calc_access(calc_access(owner_id))
    set_calc_access(owner_id, new_state)
    bot.answer_callback_query(call.id, f"دلوقتي: {CALC_ACCESS_INFO[new_state]['label']}")
    safe_edit_message_text(
        _calc_menu_text(owner_id),
        call.message.chat.id, call.message.message_id,
        reply_markup=calc_menu_markup(owner_id),
    )

@bot.callback_query_handler(func=lambda c: c.data == "calc_back")
def cb_calc_back(call):
    bot._owner_panel_context = call.from_user
    safe_edit_message_text(
        main_menu_text(call.from_user.id),
        call.message.chat.id, call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "check_sub" or c.data.startswith("check_sub_"))
def cb_check_sub(call):
    owner_id = None
    if call.data.startswith("check_sub_"):
        try:
            owner_id = int(call.data.rsplit("_", 1)[1])
        except (TypeError, ValueError):
            bot.answer_callback_query(call.id, "زر تحقق غير صالح.", show_alert=True)
            return
    if is_subscribed(call.from_user.id, owner_id, fresh=True):
        bot.answer_callback_query(call.id, " تم التحقق، اتفضل استخدم البوت.")
        business_connection_id = getattr(call.message, "business_connection_id", None)
        if owner_id is not None and business_connection_id:
            pending = _FORCE_SUB_PENDING.pop(
                (business_connection_id, call.message.chat.id, call.from_user.id), None
            )
            threading.Thread(
                target=lambda: delete_business_message(business_connection_id, call.message.message_id),
                daemon=True,
            ).start()
            if pending and time.time() - pending[1] < _FORCE_SUB_PENDING_TTL:
                # شغّل طلبه الأصلي (اللي اتمسح) فورًا
                threading.Thread(
                    target=handle_business_message, args=(pending[0],), daemon=True
                ).start()
            else:
                safe_send_business_message(
                    call.message.chat.id,
                    business_connection_id,
                    "<b>تم التحقق من اشتراكك بنجاح.</b>",
                )
            return
        user_id = call.from_user.id
        trial_just_started = False
        if not is_admin(user_id) and access_mode() == "trial_24h" and not is_premium_active(user_id):
            trial_just_started = start_trial_if_needed(user_id)
        text = main_menu_text(user_id)
        if trial_just_started:
            text = "<b>بدأت تجربتك المجانية لمدة 24 ساعة من الآن.</b>\n\n" + text
        elif trial_expired(user_id):
            text = f"{access_expired_text(user_id)}\n\n{text}"
        safe_edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=owner_panel_markup(user_id),
        )
    else:
        bot.answer_callback_query(call.id, " لسه مشتركتش في القناة.", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def cb_admin(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, " الخاصية دي للأدمن بس.", show_alert=True)
        return

    action = call.data.split("_", 1)[1]

    if action == "features":
        safe_send_message(
            call.message.chat.id,
            f"{e('lock')} <b>التفعيل والتعطيل</b>\n\n"
            "دوس على أي زرار تحت عشان تفعّل/تعطّل الميزة دي لكل حسابات البوت.\n"
            "دوس على «البوت بالكامل» عشان توقف/تشغّل كل حاجة مرة واحدة.\n"
            "دوس «رجوع» عشان تقفل اللوحة دي.",
            reply_markup=admin_features_keyboard(),
        )
        bot.answer_callback_query(call.id)

    elif action == "media":
        safe_send_message(
            call.message.chat.id,
            f"{e('camera')} <b>إدارة الميديا</b>\n\n"
            "اضغط على الميزة لتغيير حالتها. عند التفعيل تُحذف رسائل العملاء من نوعها تلقائيًا.",
            reply_markup=admin_media_keyboard(),
        )
        bot.answer_callback_query(call.id)

    elif action.startswith("media_toggle_"):
        key = action[len("media_toggle_"):]
        if key not in MEDIA_BLOCKS:
            bot.answer_callback_query(call.id, "إعداد غير معروف.", show_alert=True)
            return
        new_state = not media_block_enabled(key)
        set_media_block_enabled(key, new_state)
        safe_edit_message_text(
            f"{e('camera')} <b>إدارة الميديا</b>\n\n"
            "اضغط على الميزة لتغيير حالتها. عند التفعيل تُحذف رسائل العملاء من نوعها تلقائيًا.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_media_keyboard(),
        )
        bot.answer_callback_query(call.id, "تم تحديث الحالة.")

    elif action == "media_back":
        safe_edit_message_text(
            "<b>لوحة تحكم الأدمن</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_panel_markup(),
        )
        bot.answer_callback_query(call.id)

    elif action == "forcesub":
        channels = force_sub_channels()
        listing = "\n".join(f"• {html_escape(c)}" for c in channels) if channels else "<b>مفيش قنوات مضافة.</b>"
        safe_send_message(
            call.message.chat.id,
            f"{e('bullhorn')} <b>إدارة الاشتراك الإجباري</b>\n\n"
            f"القنوات الحالية ({len(channels)}):\n{listing}\n\n"
            "تقدر تضيف أكتر من قناة براحتك، والمستخدم هيتطلب منه ينضم لكل "
            "القنوات المضافة قبل ما يقدر يستخدم البوت.\n\n"
            "اختر الإجراء المطلوب من الأسفل:",
            reply_markup=force_sub_menu_markup(),
        )
        bot.answer_callback_query(call.id)

    elif action == "stats":
        channels = force_sub_channels()
        channels_text = ", ".join(channels) if channels else "مفيش"
        state = "<b>مفعّل</b>" if bot_enabled() else "<b>متعطل</b>"
        safe_send_message(
            call.message.chat.id,
            f"{e('eye')} <b>إحصائيات البوت</b>\n\n"
            f"الحالة: <b>{state}</b>\n"
            f"{e('people')} الحسابات النشطة: <b>{db.count_active_accounts()}</b>\n"
            f"{e('person')} مستخدمو البوت: <b>{db.count_bot_users()}</b>\n"
            f"{e('shield')} المحظورون: <b>{len(db.list_banned_bot_ids())}</b>\n"
            f"قنوات الاشتراك الإجباري ({len(channels)}): <b>{html_escape(channels_text)}</b>",
        )
        bot.answer_callback_query(call.id)

    elif action == "help":
        safe_send_message(
            call.message.chat.id,
            f"{e('book')} <b>شرح البوت</b>\n\n"
            "1⃣ فعّل البوت كـ Chatbot على حساب الـ Business بتاعك من إعدادات تيليجرام.\n"
            f"2⃣ أي رسالة تتمسح في محادثاتك هيوصلك إشعار فيه: <b>اسم الشخص، اليوزر، الرسالة المحذوفة، وآخر رسالة بعتها</b>.\n"
            "3⃣ تقدر تفعّل اشتراك إجباري عشان محدش يستخدم البوت غير لو مشترك في قناتك.\n"
            "4⃣ من إدارة الردود التلقائية تقدر تضيف ردود بكلمات مفتاحية، بصور، وبأزرار.\n"
            "5⃣ من «تخصيص أشكال الرسائل» تغيّر شكل رسائل الـ ID والحذف والتعديل والربط والقائمة الرئيسية بإيموجياتك ومتغيراتك.\n"
            "6⃣ لعبة إكس أو: اكتب <code>xo</code> في أي شات عميل (أو في الخاص مع البوت)، "
            "وتتقفل وتتفتح من «التفعيل والتعطيل».",
        )
        bot.answer_callback_query(call.id)

def save_force_sub_channel(message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if text in ("الغاء", "الغاء ", "cancel", "إلغاء"):
        safe_send_message(message.chat.id, f"{e('check')} <b>تم إيقاف إضافة القنوات.</b>", reply_markup=force_sub_menu_markup())
        return
    if not text.startswith("@"):
        msg = safe_send_message(message.chat.id, f"{e('cross')} لازم يوزر يبدأ بـ @، جرب تاني:")
        bot.register_next_step_handler(msg, save_force_sub_channel)
        return
    if db.channel_exists(None, text):
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} القناة دي مضافة بالفعل. ابعت قناة تانية أو <code>الغاء</code>:",
        )
        bot.register_next_step_handler(msg, save_force_sub_channel)
        return
    db.add_force_sub_channel(None, text)
    channels = force_sub_channels()
    msg = safe_send_message(
        message.chat.id,
        f"{e('check')} <b>تم إضافة القناة:</b> {text}\n\n"
        f"إجمالي القنوات المضافة دلوقتي: <b>{len(channels)}</b>.\n"
        "تقدر تبعت قناة تانية عشان تضيفها كمان، أو ابعت <code>الغاء</code> عشان توقف الإضافة.",
    )
    bot.register_next_step_handler(msg, save_force_sub_channel)

@bot.callback_query_handler(func=lambda c: c.data.startswith("fsub_"))
def cb_force_sub_menu(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, " الخاصية دي للأدمن بس.", show_alert=True)
        return

    action = call.data.split("_", 1)[1]

    if action == "addchannel":
        msg = safe_send_message(
            call.message.chat.id,
            f"{e('bullhorn')} <b>ابعتلي يوزر القناة</b> بالشكل ده:\n"
            "<code>@channel_username</code>\n\n"
            "تقدر تبعت أكتر من قناة ورا بعض (هيتم إضافتها كلها)، أو ابعت "
            "<code>الغاء</code> عشان توقف الإضافة.",
        )
        bot.register_next_step_handler(msg, save_force_sub_channel)
        bot.answer_callback_query(call.id)

    elif action == "listchannels":
        channels = db.list_force_sub_channels(None)
        if not channels:
            bot.answer_callback_query(call.id, "مفيش قنوات مضافة.", show_alert=True)
            return
        safe_send_message(
            call.message.chat.id,
            f"{e('document')} <b>القنوات المضافة</b> ({len(channels)}):\n\nدوس على أي قناة عشان تحذفها.",
            reply_markup=force_sub_channels_list_markup(None, "fsub_listback"),
        )
        bot.answer_callback_query(call.id)

    elif action.startswith("delchannel_"):
        try:
            channel_id = int(action.split("_", 1)[1])
        except ValueError:
            channel_id = None
        if channel_id is not None:
            db.remove_force_sub_channel(channel_id, None)
        channels = db.list_force_sub_channels(None)
        if not channels:
            safe_edit_message_text(
                f"{e('check')} <b>تم حذف القناة. مفيش قنوات متبقية.</b>",
                call.message.chat.id, call.message.message_id,
                reply_markup=force_sub_menu_markup(),
            )
        else:
            safe_edit_message_text(
                f"{e('document')} <b>القنوات المضافة</b> ({len(channels)}):\n\nدوس على أي قناة عشان تحذفها.",
                call.message.chat.id, call.message.message_id,
                reply_markup=force_sub_channels_list_markup(None, "fsub_listback"),
            )
        bot.answer_callback_query(call.id, pe.strip_premium(f"{e('check')} تم حذف القناة."))
        return

    elif action == "listback":
        channels = force_sub_channels()
        listing = "\n".join(f"• {html_escape(c)}" for c in channels) if channels else "<b>مفيش قنوات مضافة.</b>"
        safe_edit_message_text(
            f"{e('bullhorn')} <b>إدارة الاشتراك الإجباري</b>\n\n"
            f"القنوات الحالية ({len(channels)}):\n{listing}\n\n"
            "اختر الإجراء المطلوب من الأسفل:",
            call.message.chat.id, call.message.message_id,
            reply_markup=force_sub_menu_markup(),
        )
        bot.answer_callback_query(call.id)

    elif action == "setmessage":
        current = force_sub_message()
        msg = safe_send_message(
            call.message.chat.id,
            f"{e('pencil')} <b>ابعتلي شكل رسالة الاشتراك الإجباري الجديدة</b>\n"
            "تقدر تنسّقها عادي من تيليجرام (عريض، مائل، تحته خط...) وتحط "
            "إيموجي مميزة (Premium) من كيبورد الإيموجي وهتتحفظ زي ما هي، "
            "أو تكتب تاجات HTML يدوي (زي &lt;b&gt;عريض&lt;/b&gt;، &lt;u&gt;تحته خط&lt;/u&gt;، &lt;i&gt;مائل&lt;/i&gt;).\n\n"
            f"النص الحالي:\n{current}\n\n"
            "أو ابعت <code>افتراضي</code> عشان ترجّع النص الافتراضي.",
        )
        bot.register_next_step_handler(msg, save_force_sub_message)
        bot.answer_callback_query(call.id)

    elif action == "disable":
        db.clear_force_sub_channels(None)
        bot.answer_callback_query(call.id, pe.strip_premium(f"{e('check')} تم حذف كل القنوات وإلغاء الاشتراك الإجباري."), show_alert=True)

    elif action == "back":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

def save_force_sub_message(message):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw in ("افتراضي", "الافتراضي", "default"):
        db.set_setting("force_sub_message", "")
        safe_send_message(
            message.chat.id,
            f"{e('check')} <b>تم إرجاع رسالة الاشتراك الإجباري الافتراضية.</b>",
        )
        return
    text = (message.html_text or message.text or "").strip()
    if not text:
        msg = safe_send_message(message.chat.id, f"{e('cross')} ابعت نص صحيح، جرب تاني:")
        bot.register_next_step_handler(msg, save_force_sub_message)
        return
    db.set_setting("force_sub_message", text)
    safe_send_message(
        message.chat.id,
        f"{e('check')} <b>تم تحديث رسالة الاشتراك الإجباري.</b>\n\nمعاينة:\n\n"
        f"{e('lock')} {text}",
        reply_markup=subscription_keyboard(),
    )

def owner_force_sub_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" إضافة قناة", callback_data="owner_fsub_addchannel", icon="plus"),
        pe.btn(" القنوات المضافة", callback_data="owner_fsub_listchannels", icon="document"),
    )
    markup.add(pe.btn(" تغيير شكل الرسالة", callback_data="owner_fsub_setmessage", icon="pencil"))
    markup.add(
        pe.btn(" نص زر القناة", callback_data="owner_fsub_setbuttontext", icon="letter_a"),
        pe.btn(" لون زر القناة", callback_data="owner_fsub_color_channel", icon="globe"),
    )
    markup.add(pe.btn(" لون زر التحقق", callback_data="owner_fsub_color_verify", icon="6030657343744644592"))
    markup.add(pe.btn(" حذف كل القنوات", callback_data="owner_fsub_disable", icon="cross"))
    markup.add(_help_btn("forcesub"))
    markup.add(pe.btn(" رجوع", callback_data="owner_fsub_back", icon="arrow_left"))
    return markup

def force_sub_color_markup(button_name):
    markup = types.InlineKeyboardMarkup(row_width=3)
    for label, color in (("أخضر", "success"), ("أحمر", "danger"), ("أزرق", "primary")):
        markup.add(pe.btn(label, callback_data=f"owner_fsub_setcolor_{button_name}_{color}", style=color))
    markup.add(pe.btn("رجوع", callback_data="owner_fsub_back", icon="arrow_left"))
    return markup

@bot.callback_query_handler(func=lambda c: c.data == "owner_forcesub")
def cb_owner_forcesub(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    channels = force_sub_channels(owner_id)
    listing = "\n".join(f"• {html_escape(c)}" for c in channels) if channels else "<b>مفيش قنوات مضافة.</b>"
    safe_edit_message_text(
        f"{e('bullhorn')} <b>إدارة الاشتراك الإجباري</b>\n\n"
        f"القنوات الحالية ({len(channels)}):\n{listing}\n\n"
        "تقدر تضيف أكتر من قناة براحتك، والعميل هيتطلب منه ينضم لكل القنوات "
        "المضافة قبل ما يقدر يتعامل مع حساب الأعمال بتاعك.\n"
        "اختر الإجراء المطلوب من الأسفل:",
        call.message.chat.id, call.message.message_id,
        reply_markup=owner_force_sub_menu_markup(),
    )
    bot.answer_callback_query(call.id)

def save_owner_force_sub_channel(message):
    owner_id = message.from_user.id
    text = (message.text or "").strip()
    if text.lower() in ("الغاء", "إلغاء", "cancel"):
        safe_send_message(
            message.chat.id, f"{e('check')} <b>تم إيقاف إضافة القنوات.</b>", reply_markup=owner_panel_markup(message.from_user.id)
        )
        return
    if not text.startswith("@"):
        msg = safe_send_message(message.chat.id, f"{e('cross')} لازم يوزر القناة يبدأ بـ @، جرب تاني:")
        bot.register_next_step_handler(msg, save_owner_force_sub_channel)
        return
    if db.channel_exists(owner_id, text):
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} القناة دي مضافة بالفعل. ابعت قناة تانية أو <code>إلغاء</code>:",
        )
        bot.register_next_step_handler(msg, save_owner_force_sub_channel)
        return
    try:
        chat = bot.get_chat(text)
        bot_member = bot.get_chat_member(chat.id, bot.get_me().id)
        if bot_member.status not in ("administrator", "creator"):
            raise RuntimeError("البوت مش أدمن في القناة")
    except Exception as exc:
        print(f"Force subscription channel verification failed: {exc}", flush=True)
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} لازم ترفع البوت <b>أدمن في القناة</b> الأول، وبعدها ابعت يوزر القناة تاني.\n"
            "لازم تكون القناة عامة ويكون اليوزر بالشكل <code>@channel_username</code>.",
        )
        bot.register_next_step_handler(msg, save_owner_force_sub_channel)
        return
    db.add_force_sub_channel(owner_id, text)
    channels = force_sub_channels(owner_id)
    msg = safe_send_message(
        message.chat.id,
        f"{e('check')} <b>تم التحقق وإضافة القناة:</b> {text}\n"
        f"إجمالي القنوات المضافة دلوقتي: <b>{len(channels)}</b>.\n\n"
        "تقدر تبعت قناة تانية عشان تضيفها (هيتم التحقق منها برضو)، أو ابعت "
        "<code>إلغاء</code> عشان توقف الإضافة.",
    )
    bot.register_next_step_handler(msg, save_owner_force_sub_channel)

def save_owner_force_sub_button_text(message):
    owner_id = message.from_user.id
    text = (message.text or "").strip()
    if text.lower() in ("افتراضي", "الافتراضي", "default"):
        db.set_setting(_force_sub_key(owner_id, "button_text"), "انضم للقناة")
        safe_send_message(message.chat.id, "<b>تم إرجاع اسم زر القناة الافتراضي بدون إيموجي.</b>", reply_markup=owner_panel_markup(message.from_user.id))
        return
    if not text:
        msg = safe_send_message(message.chat.id, "<b>اكتب اسمًا صحيحًا للزر.</b>")
        bot.register_next_step_handler(msg, save_owner_force_sub_button_text)
        return
    db.set_setting(_force_sub_key(owner_id, "button_text"), text)
    safe_send_message(message.chat.id, "<b>تم حفظ اسم زر القناة.</b>", reply_markup=owner_panel_markup(message.from_user.id))

def save_owner_force_sub_message(message):
    owner_id = message.from_user.id
    raw = (message.text or "").strip()
    if raw.lower() in ("افتراضي", "الافتراضي", "default"):
        db.set_setting(_force_sub_key(owner_id, "message"), "")
        safe_send_message(message.chat.id, f"{e('check')} <b>تم إرجاع الرسالة الافتراضية.</b>", reply_markup=owner_panel_markup(message.from_user.id))
        return
    text = (message.html_text or message.text or "").strip()
    if not text:
        msg = safe_send_message(message.chat.id, f"{e('cross')} ابعت نص صحيح، جرب تاني:")
        bot.register_next_step_handler(msg, save_owner_force_sub_message)
        return
    db.set_setting(_force_sub_key(owner_id, "message"), text)
    safe_send_message(message.chat.id, f"{e('check')} <b>تم تحديث رسالة الاشتراك الإجباري.</b>", reply_markup=owner_panel_markup(message.from_user.id))

@bot.callback_query_handler(func=lambda c: c.data.startswith("owner_fsub_"))
def cb_owner_force_sub_menu(call):
    owner_id = call.from_user.id
    action = call.data.split("_", 2)[2]
    if action == "addchannel":
        msg = safe_send_message(
            call.message.chat.id,
            "ارفع البوت <b>أدمن في القناة</b> أولًا، ثم ابعت يوزرها بالشكل "
            "<code>@channel_username</code>، وسأتحقق من الصلاحية قبل الحفظ.\n"
            "تقدر تبعت أكتر من قناة ورا بعض عشان تضيفها كلها، أو ابعت <code>إلغاء</code>.",
        )
        bot.register_next_step_handler(msg, save_owner_force_sub_channel)
    elif action == "listchannels":
        channels = db.list_force_sub_channels(owner_id)
        if not channels:
            bot.answer_callback_query(call.id, "مفيش قنوات مضافة.", show_alert=True)
            return
        safe_send_message(
            call.message.chat.id,
            f"{e('document')} <b>القنوات المضافة</b> ({len(channels)}):\n\nدوس على أي قناة عشان تحذفها.",
            reply_markup=force_sub_channels_list_markup(owner_id, "owner_fsub_listback"),
        )
    elif action.startswith("delchannel_"):
        try:
            channel_id = int(action.split("_", 1)[1])
        except ValueError:
            channel_id = None
        if channel_id is not None:
            db.remove_force_sub_channel(channel_id, owner_id)
        channels = db.list_force_sub_channels(owner_id)
        if not channels:
            safe_edit_message_text(
                f"{e('check')} <b>تم حذف القناة. مفيش قنوات متبقية.</b>",
                call.message.chat.id, call.message.message_id,
                reply_markup=owner_force_sub_menu_markup(),
            )
        else:
            safe_edit_message_text(
                f"{e('document')} <b>القنوات المضافة</b> ({len(channels)}):\n\nدوس على أي قناة عشان تحذفها.",
                call.message.chat.id, call.message.message_id,
                reply_markup=force_sub_channels_list_markup(owner_id, "owner_fsub_listback"),
            )
        bot.answer_callback_query(call.id, pe.strip_premium(f"{e('check')} تم حذف القناة."))
        return
    elif action == "listback":
        channels = force_sub_channels(owner_id)
        listing = "\n".join(f"• {html_escape(c)}" for c in channels) if channels else "<b>مفيش قنوات مضافة.</b>"
        safe_edit_message_text(
            f"{e('bullhorn')} <b>إدارة الاشتراك الإجباري</b>\n\n"
            f"القنوات الحالية ({len(channels)}):\n{listing}\n\n"
            "اختر الإجراء المطلوب من الأسفل:",
            call.message.chat.id, call.message.message_id,
            reply_markup=owner_force_sub_menu_markup(),
        )
    elif action == "setmessage":
        msg = safe_send_message(
            call.message.chat.id,
            "ابعت رسالة الاشتراك الإجباري الجديدة.\n"
            "تقدر تنسّقها عادي من تيليجرام (عريض، مائل، تحته خط...) وتحط إيموجي "
            "مميزة (Premium) من كيبورد الإيموجي وهتتحفظ زي ما هي، أو تكتب تاجات "
            "HTML يدوي.\n\n"
            "أو اكتب <code>افتراضي</code>.",
        )
        bot.register_next_step_handler(msg, save_owner_force_sub_message)
    elif action == "setbuttontext":
        msg = safe_send_message(
            call.message.chat.id,
            "اكتب اسم زر القناة كما سيظهر للمستخدم، مع دعم HTML، أو اكتب <code>افتراضي</code>.\n"
            "لن أضيف أي إيموجي تلقائيًا.",
        )
        bot.register_next_step_handler(msg, save_owner_force_sub_button_text)
    elif action in ("color_channel", "color_verify"):
        button_name = action.split("_", 1)[1]
        label = "زر الانضمام للقناة" if button_name == "channel" else "زر التحقق من الاشتراك"
        safe_edit_message_text(
            f"<b>اختار لون {label}:</b>\nاللون الافتراضي هو الأخضر.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=force_sub_color_markup(button_name),
        )
    elif action.startswith("setcolor_"):
        _, button_name, color = action.split("_", 2)
        if button_name in ("channel", "verify") and color in ("success", "danger", "primary"):
            db.set_setting(_force_sub_key(owner_id, f"button_{button_name}"), color)
            safe_edit_message_text(
                "<b>تم حفظ لون الزر بنجاح.</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=owner_force_sub_menu_markup(),
            )
    elif action == "disable":
        db.clear_force_sub_channels(owner_id)
        bot.answer_callback_query(call.id, "تم حذف كل القنوات وإلغاء الاشتراك الإجباري.", show_alert=True)
        safe_edit_message_text("<b>تم إلغاء الاشتراك الإجباري لحسابك.</b>", call.message.chat.id, call.message.message_id, reply_markup=owner_panel_markup(call.from_user.id))
        return
    elif action == "back":
        safe_edit_message_text("<b>لوحة إدارة حساب الأعمال</b>", call.message.chat.id, call.message.message_id, reply_markup=owner_panel_markup(call.from_user.id))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "replies_menu")
def cb_replies_menu(call):
    if not _require_active_connection(call):
        return
    safe_edit_message_text(
        f"{e('robot')} <b>إدارة الردود التلقائية</b>\n\n"
        f"{e('point_up')} <b>الرد التلقائي:</b> بيتبعت لما العميل يكتب كلمة معينة.\n"
        f"{e('zap')} <b>رد انلاين:</b> رد بكلمة مفتاحية برضو، بيتفعّل بس لو "
        "رسالة العميل مفيهاش match مع أي رد عادي (وبعدها بيتشيك على كلمته هو).\n\n"
        "كل رد ممكن يكون نص أو نص+صورة، بيدعم HTML وإيموجي، "
        "وتقدر تضيفله زرار وتختار لونه.\n\n"
        f"{e('lock')} <b>صلاحيات الردود:</b> تحدد كل رد يشتغل للعميل (إنت وهو)، أو للمدير (إنت بس).\n\n"
        "<b>اختار من الأسفل:</b>",
        call.message.chat.id, call.message.message_id,
        reply_markup=replies_menu_markup(),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "replies_back")
def cb_replies_back(call):
    safe_edit_message_text(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id, reply_markup=owner_panel_markup(call.from_user.id)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data in ("replies_cat_normal_ad", "replies_cat_promotional_ad"))
def cb_replies_category(call):
    safe_edit_message_text(
        f"{e('robot')} <b>تم توحيد قائمة الردود.</b> اختار إضافة رد أو عرض الردود من هنا.",
        call.message.chat.id, call.message.message_id, reply_markup=replies_menu_markup(),
    )
    bot.answer_callback_query(call.id)

def _begin_auto_reply_add(chat_id, user_id, reply_kind="normal", prompt_message=None):
    prompt_id = getattr(prompt_message, "message_id", None)
    pending_replies[user_id] = {"reply_kind": reply_kind, "prompt_message_id": prompt_id}
    text = f"{e('pencil')} اكتب <b>الكلمة أو الجملة</b> اللي عايز البوت يرد عليها لما العميل يبعتها:"
    if prompt_message is not None:
        safe_edit_message_text(text, chat_id, prompt_id)
        bot.register_next_step_handler(prompt_message, step_get_keyword)
    else:
        msg = safe_send_message(chat_id, text)
        pending_replies[user_id]["prompt_message_id"] = msg.message_id
        bot.register_next_step_handler(msg, step_get_keyword)

@bot.callback_query_handler(func=lambda c: c.data == "replies_add")
def cb_replies_add(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    _begin_auto_reply_add(call.message.chat.id, user_id, "normal", call.message)

@bot.callback_query_handler(func=lambda c: c.data in ("replies_add_normal_ad", "replies_add_promotional_ad"))
def cb_replies_add_category(call):
    bot.answer_callback_query(call.id)
    _begin_auto_reply_add(call.message.chat.id, call.from_user.id, "normal", call.message)

def _entities_to_html(text, entities):
    from html import escape as _esc
    if not text:
        return ""
    ents = []
    for en in (entities or []):
        etype = getattr(en, "type", "")
        if etype == "bold":
            tags = ("<b>", "</b>")
        elif etype == "italic":
            tags = ("<i>", "</i>")
        elif etype == "underline":
            tags = ("<u>", "</u>")
        elif etype == "strikethrough":
            tags = ("<s>", "</s>")
        elif etype == "spoiler":
            tags = ("<tg-spoiler>", "</tg-spoiler>")
        elif etype == "code":
            tags = ("<code>", "</code>")
        elif etype == "pre":
            lang = getattr(en, "language", None)
            tags = (f'<pre><code class="language-{_esc(lang, quote=True)}">', "</code></pre>") if lang else ("<pre>", "</pre>")
        elif etype == "text_link" and getattr(en, "url", None):
            tags = (f'<a href="{_esc(en.url, quote=True)}">', "</a>")
        elif etype == "custom_emoji" and getattr(en, "custom_emoji_id", None):
            tags = (f'<tg-emoji emoji-id="{en.custom_emoji_id}">', "</tg-emoji>")
        elif etype == "blockquote":
            tags = ("<blockquote>", "</blockquote>")
        elif etype == "expandable_blockquote":
            tags = ("<blockquote expandable>", "</blockquote>")
        else:
            continue
        ents.append((en.offset, en.offset + en.length, tags))
    raw = text.encode("utf-16-le")
    total = len(raw) // 2
    opens, closes, cuts = {}, {}, {0, total}
    for idx, (start, end, (o, c)) in enumerate(ents):
        end = min(end, total)
        if start >= end:
            continue
        opens.setdefault(start, []).append((-end, idx, o))
        closes.setdefault(end, []).append((-start, idx, c))
        cuts.update((start, end))
    out = []
    points = sorted(cuts)
    for i, pos in enumerate(points):
        for _, _, c in sorted(closes.get(pos, []), key=lambda t: (t[0], -t[1])):
            out.append(c)
        for _, _, o in sorted(opens.get(pos, []), key=lambda t: (t[0], t[1])):
            out.append(o)
        if i + 1 < len(points):
            out.append(_esc(raw[pos * 2:points[i + 1] * 2].decode("utf-16-le"), quote=False))
    return "".join(out)

def _message_html(message):
    is_text = getattr(message, "content_type", "text") == "text"
    text = (message.text if is_text else message.caption) or ""
    entities = (message.entities if is_text else message.caption_entities) or []
    html = (message.html_text if is_text else message.html_caption) or ""
    if any(getattr(en, "type", "") == "custom_emoji" for en in entities) and "<tg-emoji" not in html:
        html = _entities_to_html(text, entities)
    return html or text


@bot.callback_query_handler(func=lambda c: c.data == "online_add")
def cb_online_add(call):
    user_id = call.from_user.id
    pending_replies[user_id] = {
        "is_online": True,
        "prompt_message_id": call.message.message_id,
    }
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"{e('zap')} <b>الرد الانلاين</b> بردّ بكلمة مفتاحية زي الرد العادي بالظبط، "
        "وبيتفعّل بس لما العميل يبعت نفس الكلمة دي ومفيش رد عادي مطابق ليها "
        "(لو كان مضاف رد انلاين قبل كده، هيتم استبداله).\n\n"
        f"{e('pencil')} اكتب <b>الكلمة أو الجملة</b> اللي عايز البوت يرد عليها:",
        call.message.chat.id, call.message.message_id, reply_markup=types.InlineKeyboardMarkup(),
    )
    bot.register_next_step_handler(call.message, step_get_keyword)

def step_get_keyword(message):
    user_id = message.from_user.id
    if user_id not in pending_replies:
        return
    pending_replies[user_id]["keyword"] = message.text.strip()
    markup = types.InlineKeyboardMarkup()
    markup.add(
        pe.btn(" نص فقط", callback_data="rtype_text", icon="letter_a"),
        pe.btn(" نص + صورة", callback_data="rtype_photo", icon="image"),
    )
    _edit_prompt_or_send(
        message.chat.id, pending_replies[user_id].get("prompt_message_id"),
        "<b>اختار نوع الرد:</b>", reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data in ("rtype_text", "rtype_photo"))
def cb_reply_type(call):
    user_id = call.from_user.id
    if user_id not in pending_replies:
        bot.answer_callback_query(call.id, "ابدأ من جديد بـ /start", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if call.data == "rtype_photo":
        pending_replies[user_id]["content_type"] = "photo"
        safe_edit_message_text(
            f"{e('image')} ابعت الصورة، وحط <b>نص الرد</b> في الـ Caption بتاعها "
            "(تقدر تستخدم HTML وإيموجي مميز)."
            + REPLY_VARIABLES_HELP,
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup(),
        )
        bot.register_next_step_handler(call.message, step_get_photo)
    else:
        pending_replies[user_id]["content_type"] = "text"
        safe_edit_message_text(
            f"{e('chat_2')} ابعت <b>نص الرد</b> (تقدر تستخدم HTML وإيموجي مميز):" + REPLY_VARIABLES_HELP,
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup(),
        )
        bot.register_next_step_handler(call.message, step_get_text)

def step_get_text(message):
    user_id = message.from_user.id
    if user_id not in pending_replies:
        return

    pending_replies[user_id]["text_content"] = _message_html(message)
    if pending_replies[user_id].get("is_online"):
        ask_online_button_type(message.chat.id, user_id)
    else:
        pending_replies[user_id]["has_button"] = False
        ask_reply_pages(message.chat.id, user_id)

def step_get_photo(message):
    user_id = message.from_user.id
    if user_id not in pending_replies:
        return
    if not message.photo:
        _edit_prompt_or_send(
            message.chat.id, pending_replies[user_id].get("prompt_message_id"),
            f"{e('cross')} لازم تبعت صورة، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_photo)
        return
    pending_replies[user_id]["photo_file_id"] = message.photo[-1].file_id
    pending_replies[user_id]["text_content"] = _message_html(message)
    if pending_replies[user_id].get("is_online"):
        ask_online_button_type(message.chat.id, user_id)
    else:
        pending_replies[user_id]["has_button"] = False
        ask_reply_pages(message.chat.id, user_id)

def ask_online_button_type(chat_id, user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("زر رابط", callback_data="online_btype_url"),
        types.InlineKeyboardButton("زر رسالة", callback_data="online_btype_text"),
        types.InlineKeyboardButton("زر نسخ Copy", callback_data="online_btype_copy"),
    )
    _edit_prompt_or_send(
        chat_id, pending_replies.get(user_id, {}).get("prompt_message_id"),
        "اختار نوع الزر للرد الانلاين:", reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("online_btype_"))
def cb_online_button_type(call):
    user_id = call.from_user.id
    data = pending_replies.get(user_id)
    if not data or not data.get("is_online"):
        bot.answer_callback_query(call.id, "ابدأ إضافة الرد الانلاين من جديد.", show_alert=True)
        return
    kind = call.data[len("online_btype_"):]
    bot.answer_callback_query(call.id)
    data["has_button"] = True
    data["button_type"] = "copy" if kind == "copy" else kind
    safe_edit_message_text(
        "اكتب <b>اسم الزر</b> الذي سيظهر للمستخدم:",
        call.message.chat.id, call.message.message_id,
        reply_markup=types.InlineKeyboardMarkup(),
    )
    bot.register_next_step_handler(call.message, step_get_button_text)

def ask_button(chat_id, user_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        pe.btn(" نعم", callback_data="rbtn_yes", icon="thumbs_up"),
        pe.btn(" لأ", callback_data="rbtn_no", icon="cross"),
    )
    safe_send_message(chat_id, "<b>عايز تضيف زرار مع الرد؟</b>", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data in ("rbtn_yes", "rbtn_no"))
def cb_reply_button_choice(call):
    user_id = call.from_user.id
    if user_id not in pending_replies:
        bot.answer_callback_query(call.id, "ابدأ من جديد بـ /start", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if call.data == "rbtn_no":
        pending_replies[user_id]["has_button"] = False
        finalize_reply(call.message.chat.id, user_id)
    else:
        pending_replies[user_id]["has_button"] = True
        safe_edit_message_text(
            f"{e('letter_a')} اكتب <b>اسم الزرار</b> (النص اللي هيظهر عليه):",
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup(),
        )
        bot.register_next_step_handler(call.message, step_get_button_text)

def step_get_button_text(message):
    user_id = message.from_user.id
    if user_id not in pending_replies:
        return
    pending_replies[user_id]["button_text"] = message.text.strip()
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("أخضر", callback_data="rcolor_green", style="success"),
        types.InlineKeyboardButton("أحمر", callback_data="rcolor_red", style="danger"),
        types.InlineKeyboardButton("أزرق", callback_data="rcolor_blue", style="primary"),
    )
    _edit_prompt_or_send(
        message.chat.id, pending_replies[user_id].get("prompt_message_id"),
        "<b>اختار لون الزرار.</b>\n\n"
        "اختار اللون فقط، بدون إيموجي مميزة.",
        reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("rcolor_"))
def cb_reply_color(call):
    user_id = call.from_user.id
    if user_id not in pending_replies:
        bot.answer_callback_query(call.id, "ابدأ من جديد بـ /start", show_alert=True)
        return
    pending_replies[user_id]["button_color"] = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    if pending_replies[user_id].get("button_type") == "copy":
        safe_edit_message_text(
            "اكتب <b>النص الذي سيتم نسخه</b> عند الضغط على الزر:",
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup(),
        )
        bot.register_next_step_handler(call.message, step_get_button_value)
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(
        pe.btn(" رابط (URL)", callback_data="rbtype_url", icon="globe"),
        pe.btn(" رسالة تظهر عند الضغط", callback_data="rbtype_text", icon="chat_3"),
    )
    safe_edit_message_text(
        "<b>اختار نوع الزرار:</b>", call.message.chat.id, call.message.message_id,
        reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data in ("rbtype_url", "rbtype_text"))
def cb_reply_button_type(call):
    user_id = call.from_user.id
    if user_id not in pending_replies:
        bot.answer_callback_query(call.id, "ابدأ من جديد بـ /start", show_alert=True)
        return
    pending_replies[user_id]["button_type"] = "url" if call.data == "rbtype_url" else "text"
    bot.answer_callback_query(call.id)
    if call.data == "rbtype_url":
        prompt = f"{e('globe')} ابعت <b>رابط الزرار</b> (لازم يبدأ بـ https://):"
    else:
        prompt = (
            f"{e('chat_3')} اكتب <b>الرسالة</b> اللي هتظهر للعميل لما يدوس الزرار "
            "(تقدر تستخدم HTML وإيموجي مميز):"
        )
    safe_edit_message_text(
        prompt, call.message.chat.id, call.message.message_id,
        reply_markup=types.InlineKeyboardMarkup(),
    )
    bot.register_next_step_handler(call.message, step_get_button_value)

def step_get_button_value(message):
    user_id = message.from_user.id
    if user_id not in pending_replies:
        return
    button_type = pending_replies[user_id].get("button_type")
    value = message.text.strip()
    if button_type == "url" and not value.startswith(("http://", "https://")):
        _edit_prompt_or_send(
            message.chat.id, pending_replies[user_id].get("prompt_message_id"),
            f"{e('cross')} لازم يبدأ بـ https://، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_button_value)
        return
    if button_type == "text":

        value = _message_html(message) or value
    pending_replies[user_id]["button_value"] = value
    finalize_reply(message.chat.id, user_id)

# ---------------------------------------------------------------------------
# أزرار الصفحات للرد التلقائي: الرد الأساسي فيه أزرار، كل زرار بيفتح صفحته
# وفي الصفحة زرار «رجوع» يرجّع للرد الأساسي (وتقدر تدوس على زرار تاني).
# ---------------------------------------------------------------------------

REPLY_PAGES_MAX = 10
_PAGE_COLOR_STYLE = {"green": "success", "red": "danger", "blue": "primary"}


def ask_reply_pages(chat_id, user_id):
    markup = types.InlineKeyboardMarkup()
    markup.row(
        pe.btn("نعم، أضيف صفحات", callback_data="rpgadd_yes", style="success"),
        pe.btn("لأ، كفاية كده", callback_data="rpgadd_no", style="danger"),
    )
    _edit_prompt_or_send(
        chat_id, pending_replies.get(user_id, {}).get("prompt_message_id"),
        f"{e('document')} <b>عايز تضيف أزرار صفحات للرد؟</b>\n"
        "كل زرار بيفتح صفحة بنصها، وفيها زرار رجوع للرد الأساسي.",
        reply_markup=markup,
    )


def _ask_page_title(chat_id, message_id, reply_to_message):
    safe_edit_message_text(
        f"{e('letter_a')} اكتب <b>اسم زرار الصفحة</b> (النص اللي هيظهر على الزرار):",
        chat_id, message_id, reply_markup=types.InlineKeyboardMarkup(),
    )
    bot.register_next_step_handler(reply_to_message, step_get_page_title)


@bot.callback_query_handler(func=lambda c: c.data in ("rpgadd_yes", "rpgadd_no"))
def cb_reply_pages_choice(call):
    user_id = call.from_user.id
    if user_id not in pending_replies:
        bot.answer_callback_query(call.id, "ابدأ من جديد بـ /start", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if call.data == "rpgadd_no":
        finalize_reply(call.message.chat.id, user_id)
        return
    pending_replies[user_id]["pages"] = []
    _ask_page_title(call.message.chat.id, call.message.message_id, call.message)


def step_get_page_title(message):
    user_id = message.from_user.id
    data = pending_replies.get(user_id)
    if data is None:
        return
    title = (message.text or "").strip()
    if not title:
        _edit_prompt_or_send(
            message.chat.id, data.get("prompt_message_id"),
            f"{e('cross')} لازم تكتب اسم الزرار، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_page_title)
        return
    data["cur_title"] = title[:60]
    _edit_prompt_or_send(
        message.chat.id, data.get("prompt_message_id"),
        f"{e('chat_3')} ابعت <b>نص الصفحة</b> اللي هتظهر لما العميل يدوس على الزرار "
        "(تقدر تستخدم HTML وإيموجي مميز):",
    )
    bot.register_next_step_handler(message, step_get_page_text)


def step_get_page_text(message):
    user_id = message.from_user.id
    data = pending_replies.get(user_id)
    if data is None:
        return
    text = _message_html(message) if getattr(message, "text", None) else ""
    if not text:
        _edit_prompt_or_send(
            message.chat.id, data.get("prompt_message_id"),
            f"{e('cross')} ابعت نص الصفحة (نص عادي)، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_page_text)
        return
    data["cur_text"] = text
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("أخضر", callback_data="rpgcol_green", style="success"),
        types.InlineKeyboardButton("أحمر", callback_data="rpgcol_red", style="danger"),
        types.InlineKeyboardButton("أزرق", callback_data="rpgcol_blue", style="primary"),
    )
    _edit_prompt_or_send(
        message.chat.id, data.get("prompt_message_id"),
        "<b>اختار لون زرار الصفحة:</b>", reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("rpgcol_"))
def cb_reply_page_color(call):
    user_id = call.from_user.id
    data = pending_replies.get(user_id)
    if not data or "cur_title" not in data or "cur_text" not in data:
        bot.answer_callback_query(call.id, "ابدأ من جديد بـ /start", show_alert=True)
        return
    color = call.data.split("_", 1)[1]
    if color not in _PAGE_COLOR_STYLE:
        color = "blue"
    bot.answer_callback_query(call.id)
    data.setdefault("pages", []).append(
        {"title": data.pop("cur_title"), "text": data.pop("cur_text"), "color": color}
    )
    count = len(data["pages"])
    if count >= REPLY_PAGES_MAX:
        finalize_reply(call.message.chat.id, user_id)
        return
    markup = types.InlineKeyboardMarkup()
    markup.row(
        pe.btn("أضيف صفحة تانية", callback_data="rpgmore_yes", style="success"),
        pe.btn("خلاص، احفظ", callback_data="rpgmore_done", style="primary"),
    )
    safe_edit_message_text(
        f"{e('check')} تمت إضافة الصفحة <b>({count})</b>.\nتضيف صفحة تانية؟",
        call.message.chat.id, call.message.message_id, reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data in ("rpgmore_yes", "rpgmore_done"))
def cb_reply_pages_more(call):
    user_id = call.from_user.id
    if user_id not in pending_replies:
        bot.answer_callback_query(call.id, "ابدأ من جديد بـ /start", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if call.data == "rpgmore_done":
        finalize_reply(call.message.chat.id, user_id)
        return
    _ask_page_title(call.message.chat.id, call.message.message_id, call.message)


def _reply_pages_main_markup(pages):
    """أزرار الصفحات للرد الأساسي: اتنين في الصف، كل واحد بلونه."""
    markup = types.InlineKeyboardMarkup()
    buttons = [
        pe.btn(pg["title"], callback_data=f"rpg_o:{pg['id']}",
               style=_PAGE_COLOR_STYLE.get(pg.get("color"), "primary"))
        for pg in pages
    ]
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i:i + 2])
    return markup


def _reply_page_back_markup(reply_id):
    markup = types.InlineKeyboardMarkup()
    markup.row(pe.btn("رجوع", callback_data=f"rpg_b:{reply_id}", style="danger"))
    return markup


def _reply_page_edit(call, text, markup):
    """نعدّل نفس الرسالة (نص أو كابشن صورة، وفي شات Business كمان)."""
    msg = call.message
    chat_id, message_id = msg.chat.id, msg.message_id
    kwargs = {"reply_markup": markup}
    bc_id = getattr(msg, "business_connection_id", None)
    if bc_id:
        kwargs["business_connection_id"] = bc_id
    try:
        if getattr(msg, "content_type", "text") == "text":
            safe_edit_message_text(text, chat_id, message_id, **kwargs)
            return
        try:
            bot.edit_message_caption(text, chat_id, message_id, **kwargs)
        except Exception as exc:
            if _is_benign_tg_error(exc):
                return
            kwargs["reply_markup"] = pe.strip_button_icons(markup)
            bot.edit_message_caption(pe.strip_premium(text), chat_id, message_id, **kwargs)
    except Exception as exc:
        print(f"Reply page edit failed: {str(exc)[:200]}", flush=True)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(("rpg_o:", "rpg_b:")))
def cb_reply_pages_nav(call):
    try:
        kind, num_s = call.data.split(":", 1)
        num = int(num_s)
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    if kind == "rpg_o":
        page = db.get_reply_page(num)
        if not page:
            bot.answer_callback_query(call.id, "الصفحة دي اتحذفت.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        text = apply_reply_variables(page.get("text_content") or "", call.from_user)
        _reply_page_edit(call, text, _reply_page_back_markup(page["reply_id"]))
        return
    reply = db.get_auto_reply(num)
    pages = db.get_reply_pages(num) if reply else []
    if not reply or not pages:
        bot.answer_callback_query(call.id, "الرد ده اتحذف.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    text = apply_reply_variables(reply.get("text_content") or "", call.from_user)
    _reply_page_edit(call, text, _reply_pages_main_markup(pages))


def finalize_reply(chat_id, user_id):
    data = pending_replies.pop(user_id, None)
    if not data:
        return

    if data.get("is_online"):
        db.set_online_reply(
            owner_user_id=user_id,
            keyword=data.get("keyword", ""),
            content_type=data.get("content_type", "text"),
            text_content=data.get("text_content", ""),
            photo_file_id=data.get("photo_file_id"),
            has_button=data.get("has_button", False),
            button_text=data.get("button_text"),
            button_color=data.get("button_color"),
            button_type=data.get("button_type"),
            button_value=data.get("button_value"),
        )
        _edit_prompt_or_send(
            chat_id, data.get("prompt_message_id"),
            f"{e('sparkles')} <b>تم تفعيل الرد الانلاين بنجاح!</b>\n\n"
            f"{e('zap')} <b>الكلمة:</b> <code>{html_escape(data.get('keyword', ''))}</code>\n"
            f"{e('robot_2')} <b>هيرد تلقائيًا لأول عميل يكتبها بالظبط.</b>",
            reply_markup=owner_panel_markup(user_id),
        )
        return

    new_reply_id = db.add_auto_reply(
        owner_user_id=user_id,
        keyword=data["keyword"],
        content_type=data.get("content_type", "text"),
        text_content=data.get("text_content", ""),
        photo_file_id=data.get("photo_file_id"),
        has_button=data.get("has_button", False),
        button_text=data.get("button_text"),
        button_color=data.get("button_color"),
        button_type=data.get("button_type"),
        button_value=data.get("button_value"),
        reply_kind=data.get("reply_kind", "normal"),
    )
    pages = data.get("pages") or []
    for pos, pg in enumerate(pages):
        db.add_reply_page(new_reply_id, user_id, pg["title"], pg["text"], pg["color"], pos)
    pages_line = f"\n{e('document')} <b>عدد الصفحات:</b> {len(pages)}" if pages else ""
    _edit_prompt_or_send(
        chat_id, data.get("prompt_message_id"),
        f"{e('sparkles')} <b>تم إضافة الرد بنجاح!</b>\n\n"
        f"{e('chat_2')} <b>الكلمة:</b> <code>{html_escape(data['keyword'])}</code>{pages_line}\n"
        f"{e('robot_2')} <b>هيرد تلقائيًا لأي حد يكتبها بالظبط.</b>",
        reply_markup=owner_panel_markup(user_id),
    )

def cancel_delete_markup(callback_data):
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn("إلغاء", callback_data=callback_data, icon="cross", style="danger"))
    return markup

@bot.callback_query_handler(func=lambda c: c.data == "online_delete")
def cb_online_delete(call):
    user_id = call.from_user.id
    existing = db.get_online_reply(user_id)
    bot.answer_callback_query(call.id)
    if not existing:
        safe_edit_message_text(
            "<b>مفيش رد انلاين مضاف لسه.</b>", call.message.chat.id, call.message.message_id,
            reply_markup=replies_menu_markup(),
        )
        return
    safe_edit_message_text(
        f"{e('trash')} <b>اكتب حذف</b> لتأكيد حذف الرد الانلاين، أو اضغط <b>إلغاء</b>.",
        call.message.chat.id, call.message.message_id,
        reply_markup=cancel_delete_markup("cancel_delete_online"),
    )
    bot.register_next_step_handler(call.message, step_delete_online_reply, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_delete_online")
def cb_cancel_delete_online(call):
    bot.answer_callback_query(call.id)
    safe_edit_message_text("<b>تم إلغاء حذف الرد الانلاين.</b>", call.message.chat.id, call.message.message_id, reply_markup=replies_menu_markup())

def step_delete_online_reply(message, prompt_message_id=None):
    text = (message.text or "").strip().lower()
    if text not in ("حذف", "احذف", "delete", "نعم"):
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id, "<b>لم يتم الحذف.</b>",
            reply_markup=replies_menu_markup(),
        )
        return
    db.delete_online_reply(message.from_user.id)
    _edit_prompt_or_send(
        message.chat.id, prompt_message_id, f"{e('trash')} <b>تم حذف الرد الانلاين.</b>",
        reply_markup=replies_menu_markup(),
    )

REPLIES_PAGE_SIZE = 8

def _reply_preview_text(r):
    content = (r.get("text_content") or "").strip()
    ctype = r.get("content_type") or "text"
    kind = "رد تلقائي"
    if not content:
        content = {"photo": "[صورة]", "video": "[فيديو]", "sticker": "[ملصق]", "document": "[ملف]"}.get(ctype, "[بدون نص]")
    extra = " | زرار مميز" if r.get("has_button") else ""
    try:
        n_pages = len(db.get_reply_pages(r["id"])) if r.get("id") else 0
    except Exception:
        n_pages = 0
    if n_pages:
        extra += f" | {n_pages} صفحات"
    text = f"[{kind}] {r.get('keyword', '')}\n\n{content}{extra}"
    return text if len(text) <= 190 else text[:187] + "..."

def _short_label(text, limit=18):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"

def _replies_view_content(user_id, page=0):
    replies = db.get_auto_replies(user_id)
    online = db.get_online_reply(user_id)
    total_pages = max(1, (len(replies) + REPLIES_PAGE_SIZE - 1) // REPLIES_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = replies[page * REPLIES_PAGE_SIZE:(page + 1) * REPLIES_PAGE_SIZE]

    lines = [f"{e('robot_2')} <b>الردود التلقائية</b>\n"]
    lines.append(f"{e('chat_2')} <b>الردود التلقائية:</b> {len(replies)}")
    if online:
        kw = html_escape(online["keyword"]) if online.get("keyword") else "-"
        lines.append(f"{e('zap')} <b>الرد الانلاين:</b> مفعّل — الكلمة: {kw}")
    else:
        lines.append(f"{e('zap')} <b>الرد الانلاين:</b> مش مفعّل")
    if replies or online:
        lines.append("\nاضغط على اسم الرد لمعاينته، وعلى 🗑 لحذفه.")
    else:
        lines.append("\nمفيش ردود مضافة لسه.")
    if total_pages > 1:
        lines.append(f"\n{e('book')} صفحة {page + 1} من {total_pages}")

    markup = types.InlineKeyboardMarkup(row_width=2)
    for r in chunk:
        markup.row(
            pe.btn(_short_label(r["keyword"], 22), callback_data=f"replies_peek_{r['id']}", icon="chat_2"),
            pe.btn("حذف", callback_data=f"replies_del_{r['id']}_{page}", icon="trash", style="danger"),
        )
    if page == 0 and online:
        markup.row(
            pe.btn(_short_label(online.get("keyword") or "الرد الانلاين"), callback_data="replies_peek_online", icon="zap"),
            pe.btn("حذف", callback_data="replies_del_online", icon="trash", style="danger"),
        )
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(pe.btn(" السابق", callback_data=f"replies_page_{page - 1}", icon="arrow_left"))
        if page < total_pages - 1:
            nav.append(pe.btn("التالي ", callback_data=f"replies_page_{page + 1}", icon="arrow_right"))
        markup.row(*nav)
    markup.add(pe.btn(" إضافة رد انلاين", callback_data="replies_add", icon="new"))
    markup.add(pe.btn(" إضافة رد تلقائي", callback_data="online_add", icon="new"))
    markup.add(pe.btn(" رجوع", callback_data="replies_menu", icon="arrow_left"))
    return "\n".join(lines), markup

def _show_replies_view(call, page=0):
    text, markup = _replies_view_content(call.from_user.id, page)
    try:
        safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            safe_send_message(call.message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "replies_view_all")
def cb_replies_view_all(call):
    bot.answer_callback_query(call.id)
    _show_replies_view(call, 0)

@bot.callback_query_handler(func=lambda c: c.data.startswith("replies_page_"))
def cb_replies_page(call):
    try:
        page = int(call.data.split("_")[-1])
    except ValueError:
        page = 0
    bot.answer_callback_query(call.id)
    _show_replies_view(call, page)

@bot.callback_query_handler(func=lambda c: c.data.startswith("replies_peek_"))
def cb_replies_peek(call):
    key = call.data[len("replies_peek_"):]
    if key == "online":
        r = db.get_online_reply(call.from_user.id)
    else:
        try:
            r = db.get_auto_reply(int(key))
        except ValueError:
            r = None
        if r and r["owner_user_id"] != call.from_user.id:
            r = None
    if not r:
        bot.answer_callback_query(call.id, "مش لاقي الرد ده.", show_alert=True)
        return
    bot.answer_callback_query(call.id, _reply_preview_text(r), show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data == "replies_list")
def cb_replies_list(call):
    user_id = call.from_user.id
    replies = db.get_auto_replies(user_id)
    bot.answer_callback_query(call.id)
    if not replies:
        safe_edit_message_text(
            "<b>مفيش ردود تلقائية مضافة لسه.</b>", call.message.chat.id, call.message.message_id,
            reply_markup=replies_menu_markup(),
        )
        return
    names = "\n".join(f"- <code>{html_escape(r['keyword'])}</code>" for r in replies)
    safe_edit_message_text(
        f"{e('trash')} <b>حذف رد تلقائي</b>\n\n"
        f"الردود الموجودة:\n{names}\n\n"
        "<b>اكتب اسم الرد بالضبط لحذفه، أو اضغط إلغاء.</b>",
        call.message.chat.id, call.message.message_id,
        reply_markup=cancel_delete_markup("cancel_delete_normal"),
    )
    bot.register_next_step_handler(call.message, step_delete_normal_reply, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_delete_normal")
def cb_cancel_delete_normal(call):
    bot.answer_callback_query(call.id)
    safe_edit_message_text("<b>تم إلغاء حذف الرد.</b>", call.message.chat.id, call.message.message_id, reply_markup=replies_menu_markup())

def step_delete_normal_reply(message, prompt_message_id=None):
    keyword = (message.text or "").strip()
    if not keyword or keyword.lower() in ("الغاء", "إلغاء", "cancel"):
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id, "<b>تم إلغاء حذف الرد.</b>",
            reply_markup=replies_menu_markup(),
        )
        return
    replies = db.get_auto_replies(message.from_user.id)
    match = next((r for r in replies if r["keyword"].strip().casefold() == keyword.casefold()), None)
    if not match:
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id,
            "<b>مش لاقي رد بالاسم ده.</b> اكتب الاسم بالضبط أو اضغط إلغاء.",
            reply_markup=cancel_delete_markup("cancel_delete_normal"),
        )
        bot.register_next_step_handler(message, step_delete_normal_reply, prompt_message_id)
        return
    db.delete_auto_reply(match["id"])
    _edit_prompt_or_send(
        message.chat.id, prompt_message_id,
        f"{e('trash')} <b>تم حذف الرد:</b> <code>{html_escape(match['keyword'])}</code>",
        reply_markup=replies_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("replies_del_"))
def cb_replies_delete(call):
    rest = call.data[len("replies_del_"):]
    if rest == "online":
        if not db.get_online_reply(call.from_user.id):
            bot.answer_callback_query(call.id, "مش لاقي الرد ده.", show_alert=True)
            _show_replies_view(call, 0)
            return
        db.delete_online_reply(call.from_user.id)
        bot.answer_callback_query(call.id, "تم حذف الرد الانلاين.")
        _show_replies_view(call, 0)
        return
    id_str, _, page_str = rest.partition("_")
    try:
        reply_id = int(id_str)
        page = int(page_str) if page_str else 0
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    reply = db.get_auto_reply(reply_id)
    if not reply or reply["owner_user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "مش لاقي الرد ده.", show_alert=True)
        _show_replies_view(call, page)
        return
    db.delete_auto_reply(reply_id)
    bot.answer_callback_query(call.id, f"تم حذف: {_short_label(reply['keyword'], 30)}")
    _show_replies_view(call, page)

def _replies_aud_content(user_id, page=0):
    replies = db.get_auto_replies(user_id)
    online = db.get_online_reply(user_id)
    total_pages = max(1, (len(replies) + REPLIES_PAGE_SIZE - 1) // REPLIES_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = replies[page * REPLIES_PAGE_SIZE:(page + 1) * REPLIES_PAGE_SIZE]

    lines = [
        f"{e('lock')} <b>صلاحيات الردود</b>\n",
        "اضغط على أي رد عشان تبدّل مين يقدر يفعّله بكتابة كلمته:",
        "• <b>للعميل</b>: إنت والعميل الاتنين تقدروا تكتبوا الكلمة ويشتغل الرد.",
        "• <b>للمدير</b>: إنت بس، والعميل لو كتبها ما يحصلش حاجة.",
    ]
    if not replies and not online:
        lines.append("\nمفيش ردود مضافة لسه.")
    if total_pages > 1:
        lines.append(f"\n{e('book')} صفحة {page + 1} من {total_pages}")

    markup = types.InlineKeyboardMarkup(row_width=1)
    for r in chunk:
        info = REPLY_AUD_INFO[_reply_audience(r)]
        markup.add(pe.btn(
            f"{_short_label(r['keyword'], 22)} : {info['label']}",
            callback_data=f"replies_aud_t_{r['id']}_{page}", icon=info["icon"], style=info["style"],
        ))
    if page == 0 and online:
        info = REPLY_AUD_INFO[_reply_audience(online)]
        markup.add(pe.btn(
            f"{_short_label(online.get('keyword') or 'الرد الانلاين', 22)} (انلاين) : {info['label']}",
            callback_data="replies_aud_t_online_0", icon=info["icon"], style=info["style"],
        ))
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(pe.btn(" السابق", callback_data=f"replies_aud_menu_{page - 1}", icon="arrow_left"))
        if page < total_pages - 1:
            nav.append(pe.btn("التالي ", callback_data=f"replies_aud_menu_{page + 1}", icon="arrow_right"))
        markup.row(*nav)
    markup.row(
        pe.btn("الكل للعميل", callback_data=f"replies_aud_all_client_{page}", icon="aud_client", style="success"),
        pe.btn("الكل للمدير", callback_data=f"replies_aud_all_owner_{page}", icon="aud_owner", style="primary"),
    )
    markup.add(pe.btn(" رجوع", callback_data="replies_menu", icon="arrow_left"))
    return "\n".join(lines), markup

def _show_replies_aud(call, page=0):
    text, markup = _replies_aud_content(call.from_user.id, page)
    try:
        safe_edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            safe_send_message(call.message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("replies_aud_menu_"))
def cb_replies_aud_menu(call):
    if not _require_active_connection(call):
        return
    try:
        page = int(call.data[len("replies_aud_menu_"):])
    except ValueError:
        page = 0
    bot.answer_callback_query(call.id)
    _show_replies_aud(call, page)

@bot.callback_query_handler(func=lambda c: c.data.startswith("replies_aud_t_"))
def cb_replies_aud_toggle(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    key, _, page_str = call.data[len("replies_aud_t_"):].partition("_")
    try:
        page = int(page_str) if page_str else 0
    except ValueError:
        page = 0
    if key == "online":
        reply = db.get_online_reply(owner_id)
    else:
        try:
            reply = db.get_auto_reply(int(key))
        except ValueError:
            reply = None
        if reply and reply["owner_user_id"] != owner_id:
            reply = None
    if not reply:
        bot.answer_callback_query(call.id, "مش لاقي الرد ده.", show_alert=True)
        _show_replies_aud(call, page)
        return
    cur = _reply_audience(reply)
    nxt = REPLY_AUD_CYCLE[(REPLY_AUD_CYCLE.index(cur) + 1) % len(REPLY_AUD_CYCLE)]
    if key == "online":
        db.set_online_reply_audience(owner_id, nxt)
    else:
        db.set_auto_reply_audience(int(key), owner_id, nxt)
    bot.answer_callback_query(call.id, f"{_short_label(reply.get('keyword') or 'الرد', 30)}: {REPLY_AUD_INFO[nxt]['label']}")
    _show_replies_aud(call, page)

@bot.callback_query_handler(func=lambda c: c.data.startswith("replies_aud_all_"))
def cb_replies_aud_all(call):
    if not _require_active_connection(call):
        return
    mode, _, page_str = call.data[len("replies_aud_all_"):].partition("_")
    if mode not in REPLY_AUD_INFO:
        bot.answer_callback_query(call.id, "غير معروف.", show_alert=True)
        return
    try:
        page = int(page_str) if page_str else 0
    except ValueError:
        page = 0
    db.set_all_replies_audience(call.from_user.id, mode)
    bot.answer_callback_query(call.id, f"كل الردود: {REPLY_AUD_INFO[mode]['label']}")
    _show_replies_aud(call, page)

def _send_reply_button_message(call, value):
    bc_id = getattr(call.message, "business_connection_id", None)
    try:
        if bc_id:
            safe_send_business_message(call.message.chat.id, bc_id, value)
        else:
            safe_send_message(call.message.chat.id, value)
    except Exception as exc:
        print(f"Reply button message failed: {exc}", flush=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("autoreply_btn_"))
def cb_autoreply_button_click(call):
    reply_id = int(call.data.split("_")[-1])
    reply = db.get_auto_reply(reply_id)
    bot.answer_callback_query(call.id)
    if reply and reply.get("button_type") == "text" and reply.get("button_value"):
        _send_reply_button_message(call, apply_reply_variables(reply["button_value"], call.from_user))

@bot.callback_query_handler(func=lambda c: c.data.startswith("onlinereply_btn_"))
def cb_onlinereply_button_click(call):
    reply_id = int(call.data.split("_")[-1])
    reply = db.get_online_reply_by_id(reply_id)
    bot.answer_callback_query(call.id)
    if reply and reply.get("button_type") == "text" and reply.get("button_value"):
        _send_reply_button_message(call, apply_reply_variables(reply["button_value"], call.from_user))

REPLY_AUD_INFO = {
    "client": {"label": "للعميل", "icon": "aud_client", "style": "success"},
    "owner":  {"label": "للمدير", "icon": "aud_owner",  "style": "primary"},
}
REPLY_AUD_CYCLE = ["client", "owner"]

def _reply_audience(r):
    # «للعميل» = إنت والعميل الاتنين (والقيم القديمة all/client كلها بنفس المعنى)
    return "owner" if (r or {}).get("audience") == "owner" else "client"

def _reply_audience_allows(r, sender_is_owner):
    if _reply_audience(r) == "owner":
        return bool(sender_is_owner)
    return True

def try_auto_reply(owner_user_id, bc_id, message, variables_user=None, sender_is_owner=None):
    if message.content_type != "text" or not message.text:
        return
    if sender_is_owner is None:
        sender_is_owner = bool(message.from_user and message.from_user.id == owner_user_id)
    vars_user = variables_user or message.from_user
    reply = db.find_matching_reply(owner_user_id, message.text)
    is_online = False
    if not reply:
        online = db.get_online_reply(owner_user_id)
        if online and (online.get("keyword") or "").strip():
            text_l = db.normalize_reply_text(message.text)
            keyword_l = db.normalize_reply_text(online["keyword"])
            if keyword_l == text_l:
                reply = online
                is_online = True
    if reply and not _reply_audience_allows(reply, sender_is_owner):
        return False
    if not reply:
        print(
            f"Auto-reply: no match for owner={owner_user_id} text={message.text!r} "
            f"(normalized={db.normalize_reply_text(message.text)!r})",
            flush=True,
        )
        return False

    markup = None
    if reply["has_button"] and reply.get("button_text"):
        label = apply_reply_variables(reply["button_text"].strip(), vars_user)

        button_style = {"green": "success", "red": "danger", "blue": "primary"}.get(
            reply.get("button_color"), "primary"
        )
        markup = types.InlineKeyboardMarkup()
        if reply["button_type"] == "url":
            markup.add(pe.btn(label, url=reply["button_value"], style=button_style))
        elif reply["button_type"] == "copy":
            markup.add(
                pe.btn(
                    label,
                    copy_text=types.CopyTextButton(text=reply["button_value"] or ""),
                    style=button_style,
                )
            )
        else:
            prefix = "onlinereply_btn_" if is_online else "autoreply_btn_"
            markup.add(pe.btn(label, callback_data=f"{prefix}{reply['id']}", style=button_style))

    if not is_online:
        reply_pages = db.get_reply_pages(reply["id"])
        if reply_pages:
            markup = _reply_pages_main_markup(reply_pages)

    content = apply_reply_variables(reply.get("text_content") or "", vars_user)
    if reply["content_type"] != "photo":
        _remember_bot_business_text(bc_id, message.chat.id, content)
    try:
        if reply["content_type"] == "photo" and reply.get("photo_file_id"):
            bot.send_photo(
                message.chat.id, reply["photo_file_id"], caption=content,
                business_connection_id=bc_id, reply_markup=markup,
            )
        else:
            bot.send_message(
                message.chat.id, content, business_connection_id=bc_id, reply_markup=markup,
            )
        return True
    except Exception as exc:
        print(f"Auto-reply with premium emoji failed, retrying plain: {exc}", flush=True)

        try:
            plain_content = pe.strip_premium(content)
            plain_markup = pe.strip_button_icons(markup) if markup is not None else None
            if reply["content_type"] == "photo" and reply.get("photo_file_id"):
                bot.send_photo(
                    message.chat.id, reply["photo_file_id"], caption=plain_content,
                    business_connection_id=bc_id, reply_markup=plain_markup,
                )
            else:
                bot.send_message(
                    message.chat.id, plain_content, business_connection_id=bc_id, reply_markup=plain_markup,
                )
            return True
        except Exception:
            pass
    return False

@bot.callback_query_handler(func=lambda c: c.data == "banned_menu")
def cb_banned_menu(call):
    if not _require_active_connection(call):
        return
    safe_edit_message_text(
        f"{e('cross')} <b>إدارة الكلمات الممنوعة</b>\n\n"
        "سيقوم البوت بحذف أي رسالة من عميل تحتوي على كلمة ممنوعة أوتوماتيك "
        "من شات الـ Business (لازم تكون مفعّل صلاحية «حذف الرسائل» للبوت من "
        "إعدادات Telegram Business -> Chatbots)، وتقدر تحدد رد مخصص يتبعت "
        "للعميل في نفس اللحظة.\n\n"
        "<b>اختار ما تريد فعله:</b>",
        call.message.chat.id, call.message.message_id,
        reply_markup=banned_menu_markup(),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "banned_back")
def cb_banned_back(call):
    safe_edit_message_text(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id, reply_markup=owner_panel_markup(call.from_user.id)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "banned_add")
def cb_banned_add(call):
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"{e('pencil')} اكتب <b>الكلمة أو الجملة</b> اللي عايز البوت يحذف أي رسالة فيها من عملائك:",
        call.message.chat.id, call.message.message_id,
    )
    bot.register_next_step_handler(call.message, step_get_banned_word, call.message.message_id)

def step_get_banned_word(message, prompt_message_id=None):
    word = (message.text or "").strip()
    if not word:
        _edit_prompt_or_send(message.chat.id, prompt_message_id, f"{e('cross')} لازم تكتب كلمة، جرب تاني:")
        bot.register_next_step_handler(message, step_get_banned_word, prompt_message_id)
        return
    db.add_banned_word(message.from_user.id, word)
    _edit_prompt_or_send(
        message.chat.id, prompt_message_id,
        f"{e('check')} تم إضافة الكلمة الممنوعة: <b>{html_escape(word)}</b>",
        reply_markup=banned_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "banned_delete_menu")
def cb_banned_delete_menu(call):
    words = db.get_banned_words(call.from_user.id)
    bot.answer_callback_query(call.id)
    if not words:
        safe_edit_message_text(
            "<b>مفيش كلمات ممنوعة مضافة لسه.</b>",
            call.message.chat.id, call.message.message_id, reply_markup=banned_menu_markup()
        )
        return
    markup = types.InlineKeyboardMarkup()
    for w in words:
        markup.add(pe.btn(f" {w['word']}", callback_data=f"banned_del_{w['id']}", icon="trash"))
    markup.add(pe.btn(" رجوع", callback_data="banned_menu", icon="arrow_left"))
    safe_edit_message_text(
        f"{e('cross')} <b>اختار الكلمة اللي عايز تمسحها:</b>",
        call.message.chat.id, call.message.message_id, reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("banned_del_"))
def cb_banned_delete(call):
    word_id = int(call.data.split("_")[-1])
    word = db.get_banned_word(word_id)
    if not word or word["owner_user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, " مش لاقي الكلمة دي.", show_alert=True)
        return
    db.delete_banned_word(word_id)
    bot.answer_callback_query(call.id, " تم الحذف.")
    safe_edit_message_text(
        f"{e('trash')} تم حذف الكلمة الممنوعة: <b>{html_escape(word['word'])}</b>",
        call.message.chat.id, call.message.message_id, reply_markup=banned_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "banned_view")
def cb_banned_view(call):
    words = db.get_banned_words(call.from_user.id)
    bot.answer_callback_query(call.id)
    if not words:
        safe_edit_message_text(
            "<b>مفيش كلمات ممنوعة مضافة لسه.</b>",
            call.message.chat.id, call.message.message_id, reply_markup=banned_menu_markup()
        )
        return
    listing = "\n".join(f"• {html_escape(w['word'])}" for w in words)
    safe_edit_message_text(
        f"{e('document')} <b>الكلمات الممنوعة الحالية</b> ({len(words)}):\n\n{listing}",
        call.message.chat.id, call.message.message_id, reply_markup=banned_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "banned_clear_all")
def cb_banned_clear_all(call):
    bot.answer_callback_query(call.id)
    words = db.get_banned_words(call.from_user.id)
    if not words:
        safe_edit_message_text(
            "<b>مفيش كلمات ممنوعة أصلًا عشان تتمسح.</b>",
            call.message.chat.id, call.message.message_id, reply_markup=banned_menu_markup()
        )
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(
        pe.btn(" تأكيد المسح", callback_data="banned_clear_confirm", icon="check"),
        pe.btn(" إلغاء", callback_data="banned_menu", icon="cross"),
    )
    safe_edit_message_text(
        f"{e('cross')} <b>متأكد عايز تمسح كل الكلمات الممنوعة؟</b> ({len(words)} كلمة)\nمينفعش ترجع فيه.",
        call.message.chat.id, call.message.message_id, reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data == "banned_clear_confirm")
def cb_banned_clear_confirm(call):
    db.delete_all_banned_words(call.from_user.id)
    bot.answer_callback_query(call.id, " تم مسح كل الكلمات.")
    safe_edit_message_text(
        f"{e('trash')} <b>تم مسح كل الكلمات الممنوعة.</b>",
        call.message.chat.id, call.message.message_id, reply_markup=banned_menu_markup()
    )

@bot.callback_query_handler(func=lambda c: c.data == "banned_customize")
def cb_banned_customize(call):
    bot.answer_callback_query(call.id)
    current = db.get_banned_reply(call.from_user.id)
    hint = f"\n\n<b>الرد الحالي المضاف:</b>\n{current}" if current else ""
    safe_edit_message_text(
        f"{e('chat_2')} ابعت <b>الرد</b> اللي عايز البوت يبعته للعميل لما يستخدم كلمة ممنوعة "
        "(تقدر تستخدم HTML وإيموجي). أو ابعت <code>الغاء</code> عشان توقف إرسال أي رد "
        f"للعميل (هيفضل بس يحذف رسالته ويبلغك انت من غير ما يرد عليه).{hint}",
        call.message.chat.id, call.message.message_id,
    )
    bot.register_next_step_handler(call.message, step_get_banned_reply, call.message.message_id)

def step_get_banned_reply(message, prompt_message_id=None):
    owner_id = message.from_user.id
    text = (message.html_text or message.text or "").strip()
    if text in ("الغاء", "إلغاء", "cancel"):
        db.clear_banned_reply(owner_id)
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id, f"{e('check')} تم إلغاء الرد المخصص.",
            reply_markup=banned_menu_markup(),
        )
        return
    if not text:
        _edit_prompt_or_send(message.chat.id, prompt_message_id, f"{e('cross')} لازم تكتب نص، جرب تاني:")
        bot.register_next_step_handler(message, step_get_banned_reply, prompt_message_id)
        return
    db.set_banned_reply(owner_id, text)
    _edit_prompt_or_send(
        message.chat.id, prompt_message_id,
        f"{e('check')} <b>تم حفظ الرد المخصص.</b> هيتبعت للعميل تلقائيًا لما يستخدم كلمة ممنوعة.",
        reply_markup=banned_menu_markup(),
    )

YOUTUBE_TRIGGER_PREFIX = "يوت"
_YOUTUBE_PICK_CACHE = {}
_YT_META_CACHE = {}

def youtube_search(query, limit=5):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
        "default_search": f"ytsearch{limit}",
    }
    info = None
    last_exc = None
    ydl_opts["default_search"] = f"ytsearch{limit + 5}"  # extra room: filtered entries are dropped
    for _proxy in proxy_pool.plan():
        opts = dict(ydl_opts)
        if _proxy:
            opts.update({"proxy": _proxy, "socket_timeout": 12, "retries": 1,
                         "extractor_retries": 1})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
            proxy_pool.mark_good(_proxy)
            break
        except Exception as exc:
            last_exc = exc
            if _proxy:
                print(f"youtube_search: فشل عبر {proxy_pool.mask(_proxy)}: {str(exc)[:120]}", flush=True)
                proxy_pool.mark_bad(_proxy, proxy_pool.classify_error(str(exc)))
    else:
        if last_exc:
            raise last_exc
    entries = (info or {}).get("entries") or []
    results = []
    for entry in entries:
        if len(results) >= limit:
            break
        video_id = entry.get("id") or ""
        title = entry.get("title") or "بدون عنوان"
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)):
            continue
        if str(entry.get("ie_key") or "Youtube").lower() != "youtube":
            continue
        if entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        if entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
            continue
        if video_id:
            results.append((title, video_id))
            _thumb = None
            try:
                _thumbs = [t for t in (entry.get("thumbnails") or []) if t.get("url")]
                _thumbs = [t for t in _thumbs if (t.get("width") or 0) <= 1280] or _thumbs
                if _thumbs:
                    _thumb = max(_thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)).get("url")
            except Exception:
                _thumb = None
            _YT_META_CACHE[video_id] = {
                "title": title,
                "duration": int(entry.get("duration") or 0),
                "thumb": _thumb,
                "channel": entry.get("channel") or entry.get("uploader") or "",
            }
    return results

YOUTUBE_COOKIES_FILE = os.environ.get(
    "YOUTUBE_COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt"),
)
YTDLP_BROWSER = os.environ.get("YTDLP_BROWSER", "").strip().lower()
DOWNLOAD_MAX_BYTES = 1024 * 1024 * 1024

_COOKIES_HELP = (
    "يوتيوب رفض التحميل من السيرفر (حماية ضد الروبوتات). الحلول بالترتيب:\n"
    "1) ثبّت الاعتماديات المحدثة: pip install -U \"yt-dlp[default]\".\n"
    "2) من متصفحك (مسجل دخول يوتيوب) صدّر ملف youtube_cookies.txt وضعه بجانب bot.py، "
    "أو عرّف YTDLP_BROWSER=chrome.\n"
    "3) إذا استمر الحجب على IP الخاص بالـ VPS، استخدم PO Token عبر YTDLP_PO_TOKEN؛ "
    "هذا حجب من YouTube ولا يمكن للكود تجاوزه وحده.\n"
    "4) أعد إرسال الأمر من جديد."
)

_DOWNLOAD_CACHE = {}
_YTDLP_DOWNLOAD_LOCK = threading.BoundedSemaphore(max(1, int(os.environ.get("YTDLP_MAX_PARALLEL", "2") or 1)))
_YTDLP_UPDATED_ONCE = False

class _BudgetExceeded(RuntimeError):
    pass
_JS_RUNTIME_ARGS = None

def _is_video_url(value):
    return bool(re.match(
        r"^https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s]+|shorts/[^\s]+|live/[^\s]+)|youtu\.be/[^\s]+)",
        (value or "").strip(), re.I))

def _ytdlp_cmd():
    return ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]

def _ytdlp_self_update():
    global _YTDLP_UPDATED_ONCE
    if _YTDLP_UPDATED_ONCE:
        return False
    _YTDLP_UPDATED_ONCE = True
    ok = False
    try:
        if shutil.which("yt-dlp"):
            p1 = subprocess.run(["yt-dlp", "-U"], capture_output=True, timeout=300)
            ok = ok or p1.returncode == 0
        p2 = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]",
                             "--break-system-packages", "-q"],
                            capture_output=True, timeout=600)
        ok = ok or p2.returncode == 0
        print(f"yt-dlp self-update: {'نجح' if ok else 'لم يتأكد النجاح'}", flush=True)
        return ok
    except Exception as exc:
        print(f"yt-dlp self-update failed: {exc}", flush=True)
        return False

def _ytdlp_cookies_args():
    if YTDLP_BROWSER:
        return ["--cookies-from-browser", YTDLP_BROWSER]
    if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE) and _cookie_file_valid(YOUTUBE_COOKIES_FILE):
        return ["--cookies", YOUTUBE_COOKIES_FILE]
    return []

def _ytdlp_proxy_cmd(cmd, proxy):
    n = len(_ytdlp_cmd())
    out = list(cmd[:n]) + ["--proxy", proxy, "--no-continue"] + list(cmd[n:])
    for flag, val in (("--socket-timeout", "10"), ("--retries", "1")):
        if flag in out and out.index(flag) + 1 < len(out):
            out[out.index(flag) + 1] = val
    if proxy.lower().startswith("socks"):
        cleaned, i = [], 0
        while i < len(out):
            if out[i] == "--downloader" and i + 1 < len(out) and out[i + 1] == "aria2c":
                i += 2
                continue
            if out[i] == "--downloader-args" and i + 1 < len(out) and str(out[i + 1]).startswith("aria2c:"):
                i += 2
                continue
            cleaned.append(out[i])
            i += 1
        out = cleaned
    return out

def _ytdlp_js_args():
    global _JS_RUNTIME_ARGS
    if _JS_RUNTIME_ARGS is None:
        _JS_RUNTIME_ARGS = []
        try:
            if shutil.which("node"):
                probe = subprocess.run(_ytdlp_cmd() + ["--help"],
                                       capture_output=True, text=True, timeout=60)
                help_text = (probe.stdout or "") + (probe.stderr or "")
                if "js-runtimes" in help_text:
                    _JS_RUNTIME_ARGS = ["--js-runtimes", "node"]
                    if "remote-components" in help_text:

                        _JS_RUNTIME_ARGS += ["--remote-components", "ejs:github"]
                    print("yt-dlp: JS runtime (node) مفعّل لفك تشفير يوتيوب", flush=True)
        except Exception:
            pass
    return _JS_RUNTIME_ARGS

def _ytdlp_extractor_args():
    args = ["--extractor-args", "youtube:player_client=android,web_safari"]
    po_token = os.environ.get("YTDLP_PO_TOKEN", "").strip()
    if po_token:
        args += ["--extractor-args", f"youtube:po_token={po_token}"]
    args += _ytdlp_js_args()
    return args

def _av_streams(path):
    if not shutil.which("ffprobe"):
        return (True, True)
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        kinds = [s.strip() for s in (proc.stdout or "").replace("\n", ",").split(",")]
        return ("video" in kinds, "audio" in kinds)
    except Exception:
        return (True, True)

def _ensure_av_streams(path, media_type):
    has_v, has_a = _av_streams(path)
    if media_type == "mp3":
        if has_a:
            return True
    elif has_v and has_a:
        return True
    if not shutil.which("ffmpeg"):
        return has_a if media_type == "mp3" else (has_v and has_a)
    fixed = str(path) + ".remux" + os.path.splitext(str(path))[1]
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
                        "-c", "copy", fixed], capture_output=True, timeout=600)
        if os.path.exists(fixed) and os.path.getsize(fixed) > 0:
            has_v2, has_a2 = _av_streams(fixed)
            ok = has_a2 if media_type == "mp3" else (has_v2 and has_a2)
            if ok:
                os.replace(fixed, str(path))
                print("yt-dlp: تم إصلاح الحاوية بإعادة التغليف (ffmpeg -c copy)", flush=True)
                return True
        if os.path.exists(fixed):
            os.remove(fixed)
    except Exception:
        try:
            if os.path.exists(fixed):
                os.remove(fixed)
        except OSError:
            pass
    return False

def _is_valid_media_file(path, media_type):
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if head[:5] == b"<!doc" or head[:5] == b"<?xml" or head[:2] == b"<h":
        return False
    if media_type == "mp3":
        return (head[:3] == b"ID3" or (head and head[0] == 0xFF)
                or head[4:8] == b"ftyp")
    return (head[4:8] == b"ftyp" or head[:4] == b"\x1aE\xdf\xa3"
            or head[:3] == b"\x1a\x45\xdf")

def _ytdlp_probe(video_url):
    default = ("ملف يوتيوب", 0)
    for _proxy in proxy_pool.plan(max_tries=2):
        cookies_args = _ytdlp_cookies_args() if (not _proxy or proxy_pool.USE_COOKIES) else []
        base = ["--skip-download", "--dump-json", "--no-warnings", "--no-playlist"]
        cmd = _ytdlp_cmd() + base + cookies_args + _ytdlp_extractor_args() + [video_url]
        if _proxy:
            cmd = _ytdlp_proxy_cmd(cmd, _proxy)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45 if _proxy else 60)
            if proc.returncode != 0 or not proc.stdout.strip():
                err = (proc.stderr or "").strip()
                if cookies_args and not _proxy and ("not a bot" in err or "Sign in" in err):
                    print("yt-dlp: الكوكيز مرفوضة في فحص الفيديو — إعادة المحاولة بدون كوكيز", flush=True)
                    cmd = (_ytdlp_cmd() + base + _ytdlp_extractor_args() + [video_url])
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if proc.returncode == 0 and proc.stdout.strip():
                        info = json.loads(proc.stdout.strip().splitlines()[-1])
                        return (info.get("title") or "ملف يوتيوب", int(info.get("duration") or 0))
                    err = (proc.stderr or "").strip()
                print(f"yt-dlp: تعذر فحص بيانات الفيديو"
                      f"{' عبر ' + proxy_pool.mask(_proxy) if _proxy else ''}: {err[:150]}", flush=True)
                proxy_pool.mark_bad(_proxy, proxy_pool.classify_error(err))
                continue
            info = json.loads(proc.stdout.strip().splitlines()[-1])
            proxy_pool.mark_good(_proxy)
            return (info.get("title") or "ملف يوتيوب", int(info.get("duration") or 0))
        except subprocess.TimeoutExpired:
            print(f"yt-dlp: فحص بيانات الفيديو تأخر"
                  f"{' عبر ' + proxy_pool.mask(_proxy) if _proxy else ''}", flush=True)
            proxy_pool.mark_bad(_proxy, "proxy")
        except Exception as exc:
            print(f"yt-dlp: فحص بيانات الفيديو فشل (هيتم التحميل برضه): {exc}", flush=True)
            proxy_pool.mark_bad(_proxy, "proxy")
    return default

def _refresh_youtube_cookies_if_stale(cookies_path="youtube_cookies.txt", max_age_hours=20):
    import time
    try:
        if os.path.exists(cookies_path):
            age_hours = (time.time() - os.path.getmtime(cookies_path)) / 3600
            if age_hours < max_age_hours:
                return cookies_path
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser("chrome")
        jar.save(cookies_path, ignore_discard=True, ignore_expires=True)
        print("youtube_cookies.txt: تم التحديث من المتصفح", flush=True)
    except Exception as exc:
        print(f"youtube_cookies.txt: فشل التحديث التلقائي ({exc}) — هيتم استخدام الملف الحالي كما هو", flush=True)
    return cookies_path


def ytdlp_download(video_url, media_type="mp3", quality=720, known_meta=None, debug_cb=None, budget=None, **_kw):
    deadline = time.time() + budget if budget else None
    if not _YTDLP_DOWNLOAD_LOCK.acquire(timeout=budget if budget else None):
        raise _BudgetExceeded("yt-dlp مشغول بتحميل تاني — تم تجاوز المصدر")
    try:
        return _ytdlp_download_locked(video_url, media_type, quality, known_meta, debug_cb,
                                      deadline=deadline, **_kw)
    finally:
        _YTDLP_DOWNLOAD_LOCK.release()


def _ytdlp_download_locked(video_url, media_type="mp3", quality=720, known_meta=None, debug_cb=None, deadline=None, **_kw):
    if not shutil.which("yt-dlp"):
        try:
            import importlib.util
            if importlib.util.find_spec("yt_dlp") is None:
                raise RuntimeError("yt-dlp غير مثبت — تم تجاوز المصدر فورًا")
        except ImportError:
            raise RuntimeError("yt-dlp غير مثبت — تم تجاوز المصدر فورًا")
    if not _is_video_url(video_url):
        raise ValueError("أرسل رابط YouTube مباشر يبدأ بـ https://")
    if known_meta and known_meta[0]:
        title, duration = known_meta[0], int(known_meta[1] or 0)
    else:
        title, duration = _ytdlp_probe(video_url)
    os.makedirs("downloads", exist_ok=True)
    vid_m = re.search(r"(?:v=|shorts/|live/|youtu\.be/)([A-Za-z0-9_-]{6,})", video_url)
    vid = vid_m.group(1) if vid_m else "video"
    out_tmpl = os.path.join("downloads", "%(id)s.%(ext)s")
    if media_type == "mp3":

        fmt = "bestaudio[ext=m4a]"
        post = []
        ext = "m4a"
    else:
        fmt = ("bestvideo[height<=%d][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
               "bestvideo[height<=%d][vcodec^=avc1]+bestaudio/"
               "bestvideo[height<=%d][ext=mp4]+bestaudio[ext=m4a]/"
               "bestvideo[height<=%d]+bestaudio/"
               "best[height<=%d][ext=mp4]/best" % (quality, quality, quality, quality, quality))
        post = ["--merge-output-format", "mp4"]
        ext = "mp4"
    base_core = (_ytdlp_cmd() + ["-f", fmt, "-o", out_tmpl, "--no-playlist",
                                 "--no-warnings", "--no-progress",
                                 "--socket-timeout", "15",
                                 "--retries", "2", "--fragment-retries", "2",

                                 "--concurrent-fragments", "8"] + post + _ytdlp_js_args())
    if shutil.which("aria2c"):

        base_core += ["--downloader", "aria2c",
                      "--downloader-args", "aria2c:-x 8 -s 8 -k 1M"]
    clients_args = _ytdlp_extractor_args()
    cookies_args = _ytdlp_cookies_args()

    _all_errs = []

    def _time_left():
        return None if deadline is None else deadline - time.time()

    def _run_download(cmd, attempts=3, cap=None):
        proc = None
        for attempt in range(attempts):
            left = _time_left()
            if left is not None and left < 4:
                raise _BudgetExceeded("انتهى الوقت المتاح لمصدر yt-dlp")
            eff = 900 if left is None else min(900, left)
            capped = cap is not None and cap < eff
            if capped:
                eff = cap
            _t0 = time.time()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=eff)
            except subprocess.TimeoutExpired:
                if not capped:
                    raise _BudgetExceeded("انتهى الوقت المتاح لمصدر yt-dlp")
                proc = subprocess.CompletedProcess(cmd, 1, "", "ERROR: timed out (البروكسي بطيء جدًا)")
            _t_elapsed = time.time() - _t0
            _ok = proc.returncode == 0
            print(f"yt-dlp: محاولة {attempt + 1}: {_t_elapsed:.1f}ث (نتيجة: {'نجحت' if _ok else 'فشلت'})", flush=True)
            if not _ok:
                _all_errs.append(proc.stderr or "")
                _err_tail = (proc.stderr or "").strip().splitlines()
                _err_snippet = " | ".join(_err_tail[-3:]) if _err_tail else "(لا يوجد stderr)"
                print(f"yt-dlp: تفاصيل الفشل: {_err_snippet[:400]}", flush=True)
            if debug_cb:
                try:
                    debug_cb(f"محاولة{attempt + 1}:{_t_elapsed:.1f}ث{'✅' if _ok else '❌'}")
                except Exception:
                    pass
            if proc.returncode == 0:
                break
            if attempt < attempts - 1:
                time.sleep(3 * (attempt + 1))
        return proc

    path = os.path.join("downloads", f"{vid}.{ext}")
    proc = None
    proxied_ok = False
    t_proxy0 = time.time()
    _attempt_cap = proxy_pool.ATTEMPT_TIMEOUT_VIDEO if media_type == "mp4" else proxy_pool.ATTEMPT_TIMEOUT_AUDIO

    for _proxy in proxy_pool.candidates():
        if time.time() - t_proxy0 > proxy_pool.PHASE_BUDGET:
            print("yt-dlp: خلصت ميزانية وقت البروكسي — هنكمل مباشر", flush=True)
            break
        print(f"yt-dlp: محاولة عبر بروكسي {proxy_pool.mask(_proxy)}", flush=True)
        _extra = cookies_args if proxy_pool.USE_COOKIES else []
        _t_try = time.time()
        try:
            proc = _run_download(_ytdlp_proxy_cmd(base_core + _extra + [video_url], _proxy), attempts=1, cap=_attempt_cap)
            kind = "other"
            if proc.returncode != 0 or not os.path.exists(path):
                kind = proxy_pool.classify_error(proc.stderr)
                if kind == "other":
                    proc = _run_download(
                        _ytdlp_proxy_cmd(base_core + clients_args + _extra + [video_url], _proxy),
                        attempts=1, cap=_attempt_cap)
                    if proc.returncode != 0 or not os.path.exists(path):
                        kind = proxy_pool.classify_error(proc.stderr)
        except _BudgetExceeded:
            proxy_pool.mark_bad(_proxy, "proxy")
            raise
        if proc.returncode == 0 and os.path.exists(path):
            proxy_pool.mark_good(_proxy)
            try:
                proxy_pool.record_speed(_proxy, os.path.getsize(path), time.time() - _t_try)
            except OSError:
                pass
            proxied_ok = True
            break
        proxy_pool.mark_bad(_proxy, kind)
        if kind == "video":
            raise RuntimeError(f"فشل تحميل yt-dlp: {(proc.stderr or '').strip()[:200]}")

    variant_cmds = [base_core + [video_url]]
    variant_cmds.append(base_core + cookies_args + [video_url])
    variant_cmds.append(base_core + clients_args + [video_url])
    if cookies_args:
        variant_cmds.append(base_core + clients_args + cookies_args + [video_url])
    _dedup = []
    for _c in variant_cmds:
        if _c not in _dedup:
            _dedup.append(_c)
    variant_cmds = _dedup

    direct_ran = False
    if not proxied_ok:
        if not proxy_pool.DIRECT_FALLBACK and proc is not None:
            pass
        else:
            direct_ran = True
            proc = _run_download(variant_cmds[0], attempts=2)
            for _alt_cmd in variant_cmds[1:]:
                if proc.returncode == 0:
                    break
                proc = _run_download(_alt_cmd, attempts=1)

    _BLOCK_MARKERS = ("not a bot", "sign in", "player response", "confirm you are human",
                      "http error 403", "http error 429", "unable to extract", "nsig",
                      "po token", "video unavailable", "not available on this app")
    _REMOVED_MARKERS = ("private video", "has been removed", "account associated",
                        "copyright", "terminated", "members-only", "join this channel")

    def _needs_ytdlp_retry(p):
        blob = " ".join(_all_errs + [p.stderr or ""]).lower()
        if any(m in blob for m in _REMOVED_MARKERS):
            return False
        return any(m in blob for m in _BLOCK_MARKERS)

    if (direct_ran and proc.returncode != 0 and _needs_ytdlp_retry(proc)
            and (_time_left() is None or _time_left() > 90) and _ytdlp_self_update()):
        for _alt_cmd in variant_cmds[:2]:
            proc = _run_download(_alt_cmd, attempts=1)
            if proc.returncode == 0:
                break

    if proc.returncode != 0 or not os.path.exists(path):
        err = (proc.stderr or "").strip()
        blob = " ".join(_all_errs + [err])
        print(f"yt-dlp: كل المحاولات فشلت. أخطاء المحاولات: "
              + " || ".join((x.strip().splitlines() or [""])[-1][:160] for x in _all_errs if x.strip()),
              flush=True)
        if "not a bot" in blob or "Sign in" in blob:
            _yt_ck_on_block(bool(cookies_args))
            raise RuntimeError(_COOKIES_HELP)
        # prefer the first informative error (the default-client attempt) over the last, forced-client one
        first = next((x.strip() for x in _all_errs if "ERROR" in x), err)
        first_line = next((l for l in first.splitlines() if "ERROR" in l), first)
        raise RuntimeError(f"فشل تحميل yt-dlp: {first_line[:200]}")
    if not _is_valid_media_file(path, media_type):
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError("الملف الناتج تالف (مش صوت/فيديو صالح) — جرّب تاني")
    if not _ensure_av_streams(path, media_type):
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError("الملف الناتج ناقص (فيديو/صوت) — جاري تجربة مصدر تاني")
    safe_title = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()[:120]
    try:
        _DOWNLOAD_CACHE[(video_url, media_type)] = (path, safe_title, duration)
        if len(_DOWNLOAD_CACHE) > 50:
            _DOWNLOAD_CACHE.pop(next(iter(_DOWNLOAD_CACHE)))
    except Exception:
        pass
    _yt_ck_on_success()
    return path, safe_title, duration

PREXZY_API_BASE = "https://prexzyapis.com/download"

def _prexzy_api_response(session, endpoint, video_url):
    response = session.get(
        f"{PREXZY_API_BASE}/{endpoint}",
        params={"url": video_url},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=(15, 45),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("status"):
        raise RuntimeError(str((payload or {}).get("message") or "Prexzy API أعاد ردًا غير ناجح")[:200])
    download_url = payload.get("download_url")
    if not isinstance(download_url, str) or not download_url.startswith(("http://", "https://")):
        raise RuntimeError("Prexzy API لم يُرجع رابط تنزيل صالحًا")
    info = payload.get("info") or {}
    title = re.sub(r'[\\/:*?"<>|]+', "_", str(info.get("title") or "ملف يوتيوب")).strip()[:120]
    try:
        duration = int(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return download_url, title, duration

def _prexzy_download_file(session, download_url, path, deadline=None):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": "https://prexzyapis.com/"}
    total = 0
    with session.get(download_url, headers=headers, stream=True, timeout=(20, 180)) as response:
        response.raise_for_status()
        with open(path, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if deadline is not None and time.time() > deadline:
                    raise RuntimeError("البروكسي بطيء جدًا في تنزيل الملف")
                if chunk:
                    total += len(chunk)
                    if total > DOWNLOAD_MAX_BYTES:
                        raise RuntimeError("حجم الملف أكبر من الحد المسموح")
                    output.write(chunk)
    if total < 1024:
        raise RuntimeError("Prexzy API أعاد ملفًا فارغًا أو صغيرًا جدًا")
    return total

def prexzy_download(video_url, media_type="mp3", quality=720, known_meta=None, **_kw):
    if not _is_video_url(video_url):
        raise ValueError("أرسل رابط YouTube مباشر يبدأ بـ https://")
    last_exc = None
    for _proxy in proxy_pool.plan(max_tries=1):
        session = requests.Session()
        deadline = None
        if _proxy:
            session.proxies = proxy_pool.requests_proxies(_proxy)
            deadline = time.time() + 75
        try:
            result = _prexzy_download_once(session, video_url, media_type, deadline)
            proxy_pool.mark_good(_proxy)
            return result
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError, requests.exceptions.InvalidSchema) as exc:
            last_exc = exc
            proxy_pool.mark_bad(_proxy, "proxy")
        except Exception as exc:
            last_exc = exc
            if not _proxy:
                raise
        finally:
            session.close()
        print(f"Prexzy: فشل عبر {proxy_pool.mask(_proxy)}: {str(last_exc)[:120]} — هنجرب المسار التالي", flush=True)
    raise last_exc or RuntimeError("Prexzy API فشل")

def _prexzy_download_once(session, video_url, media_type, deadline=None):
    os.makedirs("downloads", exist_ok=True)
    endpoint = "ytmp3" if media_type == "mp3" else "ytmp4"
    download_url, title, duration = _prexzy_api_response(session, endpoint, video_url)
    vid_m = re.search(r"(?:v=|shorts/|live/|youtu\.be/)([A-Za-z0-9_-]{6,})", video_url)
    vid = vid_m.group(1) if vid_m else hashlib.sha1(video_url.encode()).hexdigest()[:12]
    final_path = os.path.join("downloads", f"{vid}.{'mp3' if media_type == 'mp3' else 'mp4'}")
    temp_path = final_path + ".api.part"
    audio_path = final_path + ".audio.api.part"
    try:
        _prexzy_download_file(session, download_url, temp_path, deadline)
        if media_type == "mp3":
            if _is_valid_media_file(temp_path, "mp3"):

                os.replace(temp_path, final_path)
            else:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", temp_path,
                                "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", final_path],
                               check=True, timeout=900)
        else:
            has_v, has_a = _av_streams(temp_path)
            if has_v and not has_a:
                audio_url, _, _ = _prexzy_api_response(session, "ytmp3", video_url)
                _prexzy_download_file(session, audio_url, audio_path, deadline)
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", temp_path,
                                "-i", audio_path, "-map", "0:v:0", "-map", "1:a:0",
                                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                "-movflags", "+faststart", final_path], check=True, timeout=900)
            else:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", temp_path,
                                "-c", "copy", "-movflags", "+faststart", final_path],
                               check=True, timeout=900)
        if not _is_valid_media_file(final_path, media_type):
            raise RuntimeError("Prexzy API أعاد ملفًا غير صالح")
        if not _ensure_av_streams(final_path, media_type):
            raise RuntimeError("الملف الناتج ناقص (فيديو/صوت)")
        result = (final_path, title, duration)
        _DOWNLOAD_CACHE[(video_url, media_type)] = result
        print(f"Prexzy API: نجح تحميل {endpoint}", flush=True)
        return result
    finally:
        for leftover in (temp_path, audio_path):
            try:
                if os.path.exists(leftover):
                    os.remove(leftover)
            except OSError:
                pass

_DOWNLOAD_SOURCES = [
    ("yt-dlp", ytdlp_download),
    ("Prexzy API fallback", prexzy_download),
]

def download_media(video_url, media_type="mp3", quality=720, progress_cb=None, deadline=None,
                    known_meta=None, debug_cb=None):
    source_errors = []
    start_ts = time.time()
    per_source_limit = 420 if media_type == "mp4" else 240
    ytdlp_budget = int(os.environ.get("YTDLP_SOURCE_BUDGET_MP4" if media_type == "mp4" else "YTDLP_SOURCE_BUDGET_MP3",
                                      "240" if media_type == "mp4" else "75"))

    cached = _DOWNLOAD_CACHE.get((video_url, media_type))
    if cached:
        cached_path = cached[0]
        if (os.path.exists(cached_path) and _is_valid_media_file(cached_path, media_type)
                and _ensure_av_streams(cached_path, media_type)):
            print(f"DOWNLOAD: رجّعنا الملف من الكاش (بدون تحميل): {cached_path}", flush=True)
            return cached
        _DOWNLOAD_CACHE.pop((video_url, media_type), None)

    for source_name, source_fn in _DOWNLOAD_SOURCES:
        if deadline is not None:
            remaining = deadline - (time.time() - start_ts)
            if remaining <= 1:
                source_errors.append((source_name, "الوقت المتاح خلص قبل الوصول للمصدر"))
                continue
        if progress_cb:
            try:
                progress_cb(source_name)
            except Exception:
                pass
        print(f"DOWNLOAD: جاري المحاولة عبر المصدر: {source_name}", flush=True)
        holder = {}
        src_limit = min(per_source_limit, ytdlp_budget) if source_name == "yt-dlp" else per_source_limit
        limit_s = src_limit if deadline is None else max(
            1, min(src_limit, deadline - (time.time() - start_ts)))

        def _worker(fn=source_fn, budget=max(1, limit_s - 2)):
            try:
                holder["result"] = fn(video_url, media_type=media_type, quality=quality,
                                       known_meta=known_meta, debug_cb=debug_cb, budget=budget)
            except Exception as exc:
                holder["error"] = exc

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(limit_s)
        if worker.is_alive():
            elapsed = time.time() - start_ts
            print(f"DOWNLOAD: ⏰ المصدر {source_name} تجاوز {limit_s:.1f} ثانية "
                  f"(الإجمالي {elapsed:.1f} ثانية) — تم تجاوزه", flush=True)
            source_errors.append((source_name, f"تجاوز الحد الزمني ({limit_s:.1f} ثانية)"))
            continue
        if "result" in holder:
            print(f"DOWNLOAD: ✅ المصدر {source_name} نجح خلال {time.time() - start_ts:.1f} ثانية", flush=True)
            return holder["result"]
        exc = holder.get("error", "سبب غير معروف")
        print(f"DOWNLOAD: ❌ المصدر {source_name} فشل: {str(exc)[:300]}", flush=True)
        source_errors.append((source_name, str(exc)))
    failed_names = " + ".join(name for name, _ in source_errors)
    first_err = source_errors[0][1] if source_errors else "سبب غير معروف"
    raise RuntimeError(f"فشلت كل مصادر التحميل ({failed_names}). {first_err}")

def youtube_download_audio(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    meta_cached = _YT_META_CACHE.get(video_id, {})
    path, title, duration = download_media(
        url, media_type="mp3", deadline=240,
        known_meta=(meta_cached.get("title"), meta_cached.get("duration")),
    )
    return path, title, duration

def _social_send(chat_id, bc_id, text, **kwargs):
    if bc_id:
        return safe_send_business_message(chat_id, bc_id, text, **kwargs)
    return safe_send_message(chat_id, text, **kwargs)


def _business_status_kwargs(bc_id, status_chat_id, owner_chat_id):
    """لا تمرر business_connection_id عند تعديل رسالة المالك في الخاص."""
    if bc_id and status_chat_id not in (owner_chat_id, config.ADMIN_ID, config.DEVELOPER_ID):
        return {"business_connection_id": bc_id}
    return {}

_SOCIAL_APPROVAL_CACHE = {}

def _request_social_approval(owner_id, owner_chat_id, bc_id, chat_id, kind, **details):
    token = uuid.uuid4().hex[:10]
    _SOCIAL_APPROVAL_CACHE[token] = {
        "owner_id": owner_id,
        "owner_chat_id": owner_chat_id,
        "bc_id": bc_id,
        "chat_id": chat_id,
        "kind": kind,
        **details,
    }
    if kind == "youtube":
        text = (
            f"{e('music')} <b>عميل طلب تشغيل أغنية:</b>\n"
            f"{html_escape(details.get('query', ''))}\n\n"
            "عايز توافق على الطلب؟"
        )
    else:
        mode_label = "صوت" if details.get("mode") == "audio" else "فيديو"
        text = (
            f"{e('download')} <b>عميل طلب تحميل {html_escape(details.get('platform') or 'تيك توك')} ({mode_label}):</b>\n"
            f"{html_escape(details.get('url', ''))}\n\n"
            "عايز توافق على الطلب؟"
        )
    markup = types.InlineKeyboardMarkup()
    markup.add(
        pe.btn(" موافقة", callback_data=f"socapp_yes_{token}", icon="thumbs_up", style="success"),
        pe.btn(" رفض", callback_data=f"socapp_no_{token}", icon="cross", style="danger"),
    )
    safe_send_message(owner_chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("socapp_yes_") or c.data.startswith("socapp_no_"))
def cb_social_approval(call):
    approve = call.data.startswith("socapp_yes_")
    token = call.data.split("_", 2)[2]
    entry = _SOCIAL_APPROVAL_CACHE.pop(token, None)
    if not entry:
        bot.answer_callback_query(call.id, "الطلب ده خلص أو اتلغى قبل كده.", show_alert=True)
        return
    if not approve:
        bot.answer_callback_query(call.id, "تم الرفض.")
        try:
            bot.edit_message_text(
                f"{e('cross')} <b>تم رفض طلب العميل.</b>", call.message.chat.id, call.message.message_id
            )
        except Exception:
            pass
        return
    bot.answer_callback_query(call.id, "تمت الموافقة، جاري التنفيذ...")
    try:
        bot.edit_message_text(
            f"{e('thumbs_up')} <b>تمت الموافقة، جاري التنفيذ...</b>", call.message.chat.id, call.message.message_id
        )
    except Exception:
        pass
    chat_id = entry["chat_id"]
    bc_id = entry["bc_id"]
    owner_chat_id = entry["owner_chat_id"]
    if entry["kind"] == "youtube":
        query = entry.get("query", "")
        _social_send(chat_id, bc_id, f"{e('music')} <b>جاري البحث عن:</b> {html_escape(query)}")
        threading.Thread(
            target=_youtube_search_and_offer, args=(chat_id, bc_id, query), daemon=True
        ).start()
    else:
        mode = entry.get("mode")
        url = entry.get("url")
        icon = "music" if mode == "audio" else "download"
        status = _social_send(chat_id, bc_id, f"{e(icon)} <b>جاري تحميل الرابط...</b>")
        threading.Thread(
            target=_do_universal_download_and_send,
            args=(
                owner_chat_id, chat_id, bc_id, mode, url,
                chat_id, status.message_id if status else None,
            ),
            daemon=True,
            name="universal-business-download-approved",
        ).start()

YT_PREFETCH_AUDIO = os.environ.get("YT_PREFETCH_AUDIO", "0") == "1"
YT_VIDEO_QUALITY = int(os.environ.get("YT_VIDEO_QUALITY", "480") or 480)
YT_TG_UPLOAD_LIMIT = 49 * 1024 * 1024
YT_FILE_KEEP_SECONDS = int(os.environ.get("YT_FILE_KEEP_SECONDS", "300") or 300)

_YT_CARD_CACHE = {}
_YT_LIST_CACHE = {}
_YT_ACTIVE = set()
_YT_ACTIVE_LOCK = threading.Lock()
_YT_INFLIGHT = {}
_YT_INFLIGHT_LOCK = threading.Lock()


def _yt_fmt_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _yt_cleanup_downloads():
    now = time.time()
    try:
        for name in os.listdir("downloads"):
            path = os.path.join("downloads", name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > YT_FILE_KEEP_SECONDS:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def _yt_download_direct(video_id, media_type, quality):
    url = f"https://www.youtube.com/watch?v={video_id}"
    meta = _YT_META_CACHE.get(video_id, {})
    return download_media(
        url, media_type=media_type, quality=quality,
        deadline=420 if media_type == "mp4" else 240,
        known_meta=(meta.get("title"), meta.get("duration")),
    )


# ---------------------------------------------------------------------------
# كاش دائم للأغاني/الفيديوهات: أول تحميل بيتخزّن، وأي طلب بعد كده بيتبعت فورًا
#  1) file_id في الداتابيز (تليجرام بيبعته من عنده: من غير تحميل ولا رفع)
#  2) نسخة على القرص (لو الـ file_id مش متاح أو انتهى): من غير تحميل جديد
# ---------------------------------------------------------------------------
SONG_CACHE_DIR = os.environ.get("SONG_CACHE_DIR", "song_cache")
SONG_CACHE_MAX_BYTES = int(os.environ.get("SONG_CACHE_MAX_MB", "0") or 0) * 1024 * 1024  # 0 = مقفول (مفيش نسخة ثانية على القرص)
_SONG_CACHE_LOCK = threading.Lock()


def _yt_cache_id(video_id, media_type, quality=None):
    q = f"_{int(quality)}" if (media_type == "mp4" and quality) else ""
    return f"{re.sub(r'[^A-Za-z0-9_-]', '', str(video_id))}_{media_type}{q}"


def _yt_fid_get(video_id, kind, quality=None):
    mt = "mp3" if kind == "a" else "mp4"
    try:
        data = json.loads(db.get_setting("ytfid_" + _yt_cache_id(video_id, mt, quality), "") or "null")
    except Exception:
        data = None
    return data if data and data.get("file_id") else None


def _yt_fid_put(video_id, kind, quality, file_id, title, duration, singer=""):
    if not file_id:
        return
    mt = "mp3" if kind == "a" else "mp4"
    try:
        db.set_setting("ytfid_" + _yt_cache_id(video_id, mt, quality), json.dumps(
            {"file_id": file_id, "title": title, "duration": duration, "singer": singer},
            ensure_ascii=False))
    except Exception as exc:
        print(f"YouTube file_id cache save failed: {exc}", flush=True)


def _extract_file_id(resp, key):
    """بيدوّر في رد تليجرام (dict/list) على file_id الخاص بـ audio أو video."""
    if isinstance(resp, dict):
        v = resp.get(key)
        if isinstance(v, dict) and v.get("file_id"):
            return v["file_id"]
        for x in resp.values():
            r = _extract_file_id(x, key)
            if r:
                return r
    elif isinstance(resp, list):
        for x in resp:
            r = _extract_file_id(x, key)
            if r:
                return r
    return None


def _ycache_get(video_id, media_type, quality=None):
    base = os.path.join(SONG_CACHE_DIR, _yt_cache_id(video_id, media_type, quality))
    try:
        with open(base + ".json", "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        path = base + meta.get("ext", "")
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            os.utime(path, None)
            return path, meta.get("title") or "", meta.get("duration")
    except (OSError, ValueError):
        pass
    return None


def _ycache_put(video_id, media_type, quality, path, title, duration):
    if SONG_CACHE_MAX_BYTES <= 0:
        return path
    try:
        if not path or not os.path.isfile(path):
            return path
        if media_type == "mp4" and os.path.getsize(path) > YT_TG_UPLOAD_LIMIT:
            return path
        os.makedirs(SONG_CACHE_DIR, exist_ok=True)
        base = os.path.join(SONG_CACHE_DIR, _yt_cache_id(video_id, media_type, quality))
        ext = os.path.splitext(path)[1]
        dst = base + ext
        with _SONG_CACHE_LOCK:
            if os.path.abspath(dst) != os.path.abspath(path):
                shutil.copy2(path, dst)
            with open(base + ".json", "w", encoding="utf-8") as fh:
                json.dump({"title": title, "duration": duration, "ext": ext}, fh, ensure_ascii=False)
            files = [os.path.join(SONG_CACHE_DIR, n) for n in os.listdir(SONG_CACHE_DIR) if not n.endswith(".json")]
            total = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
            for f in sorted(files, key=lambda x: os.path.getmtime(x)):
                if total <= SONG_CACHE_MAX_BYTES or os.path.abspath(f) == os.path.abspath(dst):
                    continue
                total -= os.path.getsize(f)
                for victim in (f, os.path.splitext(f)[0] + ".json"):
                    try:
                        os.remove(victim)
                    except OSError:
                        pass
        return dst
    except Exception as exc:
        print(f"Song disk cache save failed: {exc}", flush=True)
        return path


def _yt_get_media(video_id, media_type, quality=720):
    """تحميل مع منع التكرار: لو في تحميل (مسبق أو تاني) شغال لنفس الملف بنستناه بدل ما نحمّله مرتين.
    الملف المحمّل بيتخزّن على القرص، فأي طلب بعد كده بيرجع منه فورًا."""
    hit = _ycache_get(video_id, media_type, quality)
    if hit:
        print(f"YouTube: رجّعنا الملف من كاش القرص (بدون تحميل): {video_id} {media_type}", flush=True)
        return hit
    path, title, duration = _yt_get_media_uncached(video_id, media_type, quality)
    return (_ycache_put(video_id, media_type, quality, path, title, duration), title, duration)


def _yt_get_media_uncached(video_id, media_type, quality=720):
    key = (video_id, media_type)
    with _YT_INFLIGHT_LOCK:
        job = _YT_INFLIGHT.get(key)
        owner = job is None
        if owner:
            job = {"event": threading.Event(), "result": None, "error": None}
            _YT_INFLIGHT[key] = job
    if not owner:
        job["event"].wait(timeout=420)
        if job["result"] and os.path.exists(job["result"][0]):
            return job["result"]
        return _yt_download_direct(video_id, media_type, quality)
    try:
        result = _yt_download_direct(video_id, media_type, quality)
        job["result"] = result
        return result
    except Exception as exc:
        job["error"] = exc
        raise
    finally:
        job["event"].set()
        with _YT_INFLIGHT_LOCK:
            _YT_INFLIGHT.pop(key, None)


def _yt_prefetch_audio(video_id):
    if not YT_PREFETCH_AUDIO:
        return

    def _run():
        try:
            _yt_get_media(video_id, "mp3")
            print(f"YouTube prefetch: الصوت جاهز مسبقًا ({video_id})", flush=True)
        except Exception as exc:
            print(f"YouTube prefetch فشل ({video_id}): {str(exc)[:150]}", flush=True)

    threading.Thread(target=_run, daemon=True, name="yt-prefetch").start()


def _yt_card_markup(token, has_next, kinds="av"):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if kinds == "a":
        markup.add(pe.btn(" تحميل الصوت", callback_data=f"ytdl_a_{token}", icon="music", style="success"))
    elif kinds == "v":
        markup.add(pe.btn(" تحميل الفيديو", callback_data=f"ytdl_v_{token}", icon="film", style="success"))
    else:
        markup.add(
            pe.btn(" صوت", callback_data=f"ytdl_a_{token}", icon="music", style="success"),
            pe.btn(" فيديو", callback_data=f"ytdl_v_{token}", icon="film", style="success"),
        )
    if has_next:
        markup.add(pe.btn(" نتيجة تانية", callback_data=f"ytnext_{token}", icon="arrow_left"))
    return markup


def _yt_card_caption(video_id, title):
    meta = _YT_META_CACHE.get(video_id, {})
    lines = [f"{e('music')} <b>{html_escape(title)}</b>"]
    info = []
    if meta.get("channel"):
        info.append(html_escape(str(meta["channel"])[:40]))
    dur = _yt_fmt_duration(meta.get("duration"))
    if dur:
        info.append(dur)
    if info:
        lines.append(" · ".join(info))
    return "\n".join(lines)


def _yt_send_card(chat_id, bc_id, results, idx, query, kinds="av"):
    title, video_id = results[idx]
    if len(_YT_CARD_CACHE) > 300:
        for old in list(_YT_CARD_CACHE)[:100]:
            _YT_CARD_CACHE.pop(old, None)
    token = uuid.uuid4().hex[:10]
    _YT_CARD_CACHE[token] = {
        "results": results, "idx": idx, "query": query,
        "chat_id": chat_id, "bc_id": bc_id,
        "video_id": video_id, "title": title, "kinds": kinds,
    }
    caption = _yt_card_caption(video_id, title) + {
        "a": "\n\nاضغط لتحميل الصوت:", "v": "\n\nاضغط لتحميل الفيديو:"}.get(kinds, "\n\nاختار الصيغة:")
    markup = _yt_card_markup(token, has_next=idx + 1 < len(results), kinds=kinds)
    kwargs = {"reply_markup": markup}
    if bc_id:
        kwargs["business_connection_id"] = bc_id
    meta = _YT_META_CACHE.get(video_id, {})
    thumbs = [t for t in (meta.get("thumb"),
                          f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                          f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg") if t]
    sent = None
    for thumb in thumbs:
        try:
            sent = safe_send_photo(chat_id, thumb, caption=caption, **kwargs)
            break
        except Exception as exc:
            print(f"YouTube card: فشل إرسال الصورة {thumb[:60]}: {str(exc)[:120]}", flush=True)
            if bc_id and "BUSINESS_CONNECTION_INVALID" in str(exc):
                break
    if sent is None:
        try:
            resp = requests.get(thumbs[-1], timeout=8)
            if resp.ok and len(resp.content) > 1024:
                sent = safe_send_photo(chat_id, io.BytesIO(resp.content), caption=caption, **kwargs)
        except Exception as exc:
            print(f"YouTube card: فشل إرسال الصورة كملف: {str(exc)[:120]}", flush=True)
    if sent is None:
        text_kwargs = {"reply_markup": markup}
        sent = _social_send(chat_id, bc_id, caption, **text_kwargs)
    if idx == 0 and sent is not None and "a" in kinds:
        _yt_prefetch_audio(video_id)
    return sent


def _song_soundcloud_fallback(chat_id, bc_id, query):
    """لو يوتيوب مش راضي يرد (حظر/بروكسيات واقعة): نجيب الأغنية من SoundCloud بدل ما نفشل."""
    send_kw = {"business_connection_id": bc_id} if bc_id else {}
    filepath = None
    try:
        ckey = _song_cache_key(query)
        sent = None
        try:
            cached = json.loads(db.get_setting(ckey, "") or "null")
        except Exception:
            cached = None
        if cached and cached.get("file_id"):
            try:
                sent = bot.send_audio(chat_id, cached["file_id"], **send_kw)
            except Exception as exc:
                print(f"Song cache send failed: {exc}", flush=True)
                sent = None
        if sent is None:
            filepath, meta = ud.download_song(query)
            with open(filepath, "rb") as media:
                sent = bot.send_audio(
                    chat_id, media, title=meta["title"][:64],
                    performer=(meta.get("artist") or None), duration=meta.get("duration"), **send_kw,
                )
            fid = getattr(getattr(sent, "audio", None), "file_id", None)
            if fid:
                db.set_setting(ckey, json.dumps({"file_id": fid, "title": meta["title"]}, ensure_ascii=False))
        return True
    except ud.SongNotFound:
        _social_send(chat_id, bc_id, f"{e('cross')} <b>ملقيتش الأغنية دي:</b> {html_escape(query)}")
    except Exception as exc:
        print(f"Song SoundCloud fallback failed: {exc}", flush=True)
        _social_send(chat_id, bc_id, notify_admin_download_error("أغنية", exc, extra_lines=[f"البحث: {query}"]))
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
    return False


def _youtube_search_and_offer(chat_id, bc_id, query, kinds="av"):
    audio_only = str(kinds or "").strip().lower() in ("a", "audio", "mp3")
    try:
        results = youtube_search(query, limit=5)
    except Exception as exc:
        print(f"youtube_search failed: {str(exc)[:200]}", flush=True)
        if audio_only:
            _social_send(chat_id, bc_id, f"{e('clock')} <b>يوتيوب مش راضي يرد، بجيبهالك من مصدر تاني...</b>")
            _song_soundcloud_fallback(chat_id, bc_id, query)
            return
        generic = notify_admin_download_error(
            "بحث يوتيوب", exc, extra_lines=[f"البحث: {query}"]
        )
        _social_send(chat_id, bc_id, generic)
        return
    if not results:
        if audio_only:
            _song_soundcloud_fallback(chat_id, bc_id, query)
            return
        _social_send(chat_id, bc_id, f"{e('cross')} <b>لا توجد نتائج لـ:</b> {html_escape(query)}")
        return
    _yt_send_pick_list(chat_id, bc_id, results, query, kinds=kinds)


def _yt_send_pick_list(chat_id, bc_id, results, query, kinds="av"):
    """قائمة نتائج يختار منها المستخدم — مفيش تحميل قبل الاختيار."""
    if len(_YT_LIST_CACHE) > 300:
        for old in list(_YT_LIST_CACHE)[:100]:
            _YT_LIST_CACHE.pop(old, None)
    token = uuid.uuid4().hex[:10]
    _YT_LIST_CACHE[token] = {"results": results, "query": query, "chat_id": chat_id, "bc_id": bc_id,
                             "kinds": kinds}
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, (title, video_id) in enumerate(results):
        dur = _yt_fmt_duration(_YT_META_CACHE.get(video_id, {}).get("duration"))
        label = f"{i + 1}. {title[:45]}" + (f" ({dur})" if dur else "")
        markup.add(pe.btn(label, callback_data=f"ytsel_{token}_{i}", icon="music"))
    markup.add(pe.btn("إغلاق", callback_data=f"ytsel_{token}_x", icon="trash", style="danger"))
    text = f"{e('music')} <b>نتائج البحث عن:</b> {html_escape(query)}\n\nاختار الأغنية:"
    _social_send(chat_id, bc_id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("ytsel_"))
def cb_youtube_select(call):
    try:
        _, token, idx = call.data.split("_", 2)
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    entry = _YT_LIST_CACHE.get(token)
    if not entry:
        bot.answer_callback_query(call.id, "انتهت صلاحية الاختيار، ابحث تاني.", show_alert=True)
        return

    def _remove_list_message():
        try:
            if entry["bc_id"]:
                delete_business_message(entry["bc_id"], call.message.message_id)
            else:
                bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

    if idx == "x":
        _YT_LIST_CACHE.pop(token, None)
        bot.answer_callback_query(call.id)
        _remove_list_message()
        return
    try:
        i = int(idx)
        entry["results"][i]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    _yt_send_card(entry["chat_id"], entry["bc_id"], entry["results"], i, entry["query"],
                  kinds=entry.get("kinds", "av"))
    _remove_list_message()


@bot.callback_query_handler(func=lambda c: c.data.startswith("ytnext_"))
def cb_youtube_next(call):
    token = call.data.split("_", 1)[1]
    entry = _YT_CARD_CACHE.get(token)
    if not entry:
        bot.answer_callback_query(call.id, "انتهت صلاحية الاختيار، ابحث تاني.", show_alert=True)
        return
    nxt = entry["idx"] + 1
    if nxt >= len(entry["results"]):
        bot.answer_callback_query(call.id, "دي آخر نتيجة.")
        return
    bot.answer_callback_query(call.id)
    _yt_send_card(entry["chat_id"], entry["bc_id"], entry["results"], nxt, entry["query"],
                  kinds=entry.get("kinds", "av"))
    try:
        if entry["bc_id"]:
            delete_business_message(entry["bc_id"], call.message.message_id)
        else:
            bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


def _yt_remove_card(chat_id, message_id, bc_id, is_photo, done_text):
    """بعد ما التحميل يخلص: امسح كارت (الصورة + النص + الزرار) عشان محدش يدوس تحميل تاني.
    لو المسح فشل، نعدّل الكارت ونشيل الزراير خالص."""
    try:
        if bc_id:
            ok = delete_business_message(bc_id, message_id)
            if ok is not False:
                return True
        else:
            bot.delete_message(chat_id, message_id)
            return True
    except Exception as exc:
        print(f"YouTube card delete failed: {str(exc)[:150]}", flush=True)
    _yt_edit_card(chat_id, message_id, bc_id, is_photo, done_text, types.InlineKeyboardMarkup())
    return False


def _yt_edit_card(chat_id, message_id, bc_id, is_photo, text, markup):
    kw = {"reply_markup": markup}
    if bc_id:
        kw["business_connection_id"] = bc_id
    editor = bot.edit_message_caption if is_photo else bot.edit_message_text
    try:
        editor(text, chat_id, message_id, **kw)
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        try:
            kw["reply_markup"] = pe.strip_button_icons(markup)
            editor(pe.strip_premium(text), chat_id, message_id, **kw)
        except Exception as exc2:
            print(f"YouTube card edit failed: {str(exc2)[:150]}", flush=True)


def _song_fmt_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _song_info(video_id, title, duration=None):
    meta = _YT_META_CACHE.get(video_id, {}) if video_id else {}
    singer = str(meta.get("channel") or "").strip()
    dur = _song_fmt_duration(duration or meta.get("duration"))
    return {"title": str(title or "").strip(), "singer": singer, "duration": dur}


_SONG_ROWS = (("music", "Song", "title"), ("clock", "Duration", "duration"), ("speaking", "Singer", "singer"))


def _song_rich_html(info, plain=False):
    """جدول (Song / Duration / Singer) + مشغل الصوت — Rich Message (Bot API 10.1+)."""
    rows = []
    for icon, label, key in _SONG_ROWS:
        value = info.get(key)
        if not value:
            continue
        label_html = f"{e(icon)} {label}"
        if plain:
            label_html = pe.strip_premium(label_html)
        rows.append(f"<tr><td>{label_html}</td><td>{html_escape(value)}</td></tr>")
    return f'<table bordered>{"".join(rows)}</table><audio src="tg://audio?id=song"></audio>'


def _send_song_rich(dest, filepath, info, business_connection_id=None, duration=None, plain=False, file_id=None):
    """sendRichMessage مباشرة (المكتبة مش بتدعمها): جدول + الأغنية في رسالة واحدة.
    لو file_id موجود بنبعت بيه (من غير رفع). بيرجّع رد تليجرام."""
    audio = {"type": "audio", "media": file_id or "attach://song_file", "title": info["title"][:64]}
    if info.get("singer"):
        audio["performer"] = info["singer"][:64]
    if duration:
        audio["duration"] = int(duration)
    rich = {
        "html": _song_rich_html(info, plain=plain),
        "media": [{"id": "song", "media": audio}],
        "skip_entity_detection": True,
    }
    params = {"chat_id": dest, "rich_message": json.dumps(rich, ensure_ascii=False)}
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    if file_id:
        return apihelper._make_request(config.BOT_TOKEN, "sendRichMessage", method="post", params=params)
    with open(filepath, "rb") as fh:
        return apihelper._make_request(config.BOT_TOKEN, "sendRichMessage", method="post",
                                       params=params, files={"song_file": fh})


def _song_caption_lines(info):
    """نسخة احتياطية (لو sendRichMessage مش متاحة): نفس الصفوف كابشن عادي."""
    body = "\n".join(f"{e(icon)} <b>{label}</b>  |  {html_escape(info[key])}"
                     for icon, label, key in _SONG_ROWS if info.get(key))
    return f"<blockquote>{body}</blockquote>"


def _send_song_audio(dest, filepath, info, business_connection_id=None, duration=None, file_id=None):
    """إرسال صوت قياسي مع كابشن؛ لا تستخدم Rich Message لأنه يرفض ملف الصوت أحيانًا."""
    kwargs = {
        "title": str(info.get("title") or "Audio")[:64],
        "duration": duration or None,
        "caption": _song_caption_lines(info),
        "parse_mode": "HTML",
    }
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id
    if info.get("singer"):
        kwargs["performer"] = str(info["singer"])[:64]
    if file_id:
        # أي file_id قديم/غير صالح يعاد التعامل معه في المستدعي بالتنزيل من المصدر.
        sent = bot.send_audio(dest, file_id, **kwargs)
        return file_id or getattr(getattr(sent, "audio", None), "file_id", None)
    if not filepath or not os.path.isfile(filepath) or os.path.getsize(filepath) <= 0:
        raise RuntimeError("ملف الصوت غير موجود أو فارغ؛ تم إيقاف الإرسال لتجنب MEDIA_EMPTY.")
    try:
        with open(filepath, "rb") as fh:
            sent = bot.send_audio(dest, fh, **kwargs)
    except Exception as exc:
        if "parse entities" not in str(exc).lower():
            raise
        kwargs["caption"] = pe.strip_premium(kwargs["caption"])
        with open(filepath, "rb") as fh:
            sent = bot.send_audio(dest, fh, **kwargs)
    return getattr(getattr(sent, "audio", None), "file_id", None)


def _yt_send_file(entry, filepath, title, duration, kind, file_id=None, singer=None):
    """يبعت الملف (أو file_id محفوظ). يرجع (اتحوّل_للمالك؟، file_id الجديد/المستخدم)."""
    bc_id = entry.get("bc_id")
    got = {"fid": None}

    def _send(dest, with_bc, caption=None):
        kwargs = {}
        if with_bc and bc_id:
            kwargs["business_connection_id"] = bc_id
        if caption:
            kwargs["caption"] = caption
        if kind == "a":
            info = _song_info(entry.get("video_id"), title, duration)
            if singer and not info.get("singer"):
                info["singer"] = singer
            got["fid"] = _send_song_audio(dest, filepath, info,
                                          business_connection_id=bc_id if (with_bc and bc_id) else None,
                                          duration=duration, file_id=file_id)
            if caption:
                bot.send_message(dest, caption)
            return
        vkw = {k: v for k, v in kwargs.items() if k != "caption"}
        if file_id:
            sent = bot.send_video(dest, file_id, supports_streaming=True, duration=duration or None,
                                  caption=caption or title[:200], **vkw)
        else:
            with open(filepath, "rb") as fh:
                sent = bot.send_video(dest, fh, supports_streaming=True, duration=duration or None,
                                      caption=caption or title[:200], **vkw)
        got["fid"] = file_id or getattr(getattr(sent, "video", None), "file_id", None)

    try:
        _send(entry["chat_id"], True)
        return False, got["fid"]
    except Exception as exc:
        if not bc_id or "BUSINESS_CONNECTION_INVALID" not in str(exc):
            raise
        print(f"YouTube send: ربط الـ Business غير صالح — تحويل الملف للمالك ({exc})", flush=True)
    connection = db.get_business_connection(bc_id) or {}
    owner_chat = connection.get("owner_chat_id") or config.ADMIN_ID
    _send(owner_chat, False, caption="مقدرتش أبعته للعميل (ربط الـ Business غير صالح) — حوّله بنفسك.")
    return True, got["fid"]


def _yt_entry_quality(entry):
    """جودة الفيديو: من إعداد منصة يوتيوب لو مضبوط، وإلا الافتراضي."""
    try:
        bc_id = entry.get("bc_id")
        if bc_id:
            owner = int((db.get_business_connection(bc_id) or {}).get("owner_user_id") or 0)
        else:
            owner = int(entry.get("chat_id") or 0)
        q = social_p_quality_int(owner, "youtube") if owner else None
        return q or YT_VIDEO_QUALITY
    except Exception:
        return YT_VIDEO_QUALITY


def _yt_download_and_send(entry, kind, msg_chat_id, msg_id, is_photo):
    bc_id = entry.get("bc_id")
    markup = _yt_card_markup(entry["token"], has_next=entry["idx"] + 1 < len(entry["results"]),
                             kinds=entry.get("kinds", "av"))
    base = _yt_card_caption(entry["video_id"], entry["title"])
    label = "الصوت" if kind == "a" else "الفيديو"
    key = (entry["token"], kind)
    try:
        _yt_edit_card(msg_chat_id, msg_id, bc_id, is_photo,
                      f"{base}\n\n{e('download')} <b>جاري تحميل {label}...</b>", markup)
        media_type = "mp3" if kind == "a" else "mp4"
        quality = _yt_entry_quality(entry)
        # كاش: نفس الأغنية/الفيديو اتحمّل قبل كده -> بيتبعت فورًا بالـ file_id (من غير تحميل ولا رفع)
        cached = _yt_fid_get(entry["video_id"], kind, quality)
        if cached:
            try:
                fell_back, _fid = _yt_send_file(
                    entry, None, cached.get("title") or entry["title"], cached.get("duration"), kind,
                    file_id=cached["file_id"], singer=cached.get("singer"))
                done = (f"{e('cross')} <b>ربط الـ Business مش صالح، بعتّلك الملف في الخاص.</b>" if fell_back
                        else f"{e('check')} <b>تم إرسال {label}.</b>")
                _yt_remove_card(msg_chat_id, msg_id, bc_id, is_photo, f"{base}\n\n{done}")
                return
            except Exception as exc:
                print(f"YouTube file_id cache send failed, falling back to download: {str(exc)[:150]}", flush=True)
        path, real_title, duration = _yt_get_media(entry["video_id"], media_type, quality)
        if kind == "v" and os.path.getsize(path) > YT_TG_UPLOAD_LIMIT:
            for lower in (360, 240):
                if lower >= quality:
                    continue
                print(f"YouTube: الفيديو أكبر من حد تيليجرام — إعادة التحميل بجودة {lower}p", flush=True)
                _DOWNLOAD_CACHE.pop((f"https://www.youtube.com/watch?v={entry['video_id']}", "mp4"), None)
                try:
                    os.remove(path)
                except OSError:
                    pass
                path, real_title, duration = _yt_get_media(entry["video_id"], "mp4", lower)
                quality = lower
                if os.path.getsize(path) <= YT_TG_UPLOAD_LIMIT:
                    break
        if os.path.getsize(path) > YT_TG_UPLOAD_LIMIT:
            raise RuntimeError("الملف أكبر من حد تيليجرام (50MB). جرّب الصوت بدل الفيديو." if kind == "v"
                               else "الملف أكبر من حد تيليجرام (50MB).")
        title = real_title or entry["title"]
        fell_back, new_fid = _yt_send_file(entry, path, title, duration, kind)
        if new_fid and not fell_back:
            _yt_fid_put(entry["video_id"], kind, quality, new_fid, title, duration,
                        singer=_YT_META_CACHE.get(entry["video_id"], {}).get("channel") or "")
            # الـ file_id اتحفظ: مش محتاجين نسخة على السيرفر، نمسحها فورًا
            try:
                _DOWNLOAD_CACHE.pop((f"https://www.youtube.com/watch?v={entry['video_id']}", media_type), None)
                if SONG_CACHE_MAX_BYTES <= 0 and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        done = (f"{e('cross')} <b>ربط الـ Business مش صالح، بعتّلك الملف في الخاص.</b>" if fell_back
                else f"{e('check')} <b>تم إرسال {label}.</b>")
        _yt_remove_card(msg_chat_id, msg_id, bc_id, is_photo, f"{base}\n\n{done}")
    except Exception as exc:
        print(f"YouTube download/send failed: {exc}", flush=True)
        generic = notify_admin_download_error(
            "تحميل يوتيوب", exc, extra_lines=[f"الأغنية: {entry.get('title', '')}", f"النوع: {label}"]
        )
        _yt_edit_card(msg_chat_id, msg_id, bc_id, is_photo, f"{base}\n\n{generic}", markup)
    finally:
        with _YT_ACTIVE_LOCK:
            _YT_ACTIVE.discard(key)
        _yt_cleanup_downloads()


@bot.callback_query_handler(func=lambda c: c.data.startswith("ytdl_"))
def cb_youtube_download(call):
    try:
        _, kind, token = call.data.split("_", 2)
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    entry = _YT_CARD_CACHE.get(token)
    if entry and kind in ("a", "v") and kind not in entry.get("kinds", "av"):
        bot.answer_callback_query(call.id, "الصيغة دي مقفولة من إعدادات المنصة.", show_alert=True)
        return
    if not entry or kind not in ("a", "v"):
        bot.answer_callback_query(call.id, "انتهت صلاحية الاختيار، ابحث تاني.", show_alert=True)
        return
    key = (token, kind)
    with _YT_ACTIVE_LOCK:
        if key in _YT_ACTIVE:
            bot.answer_callback_query(call.id, "بيتحمل بالفعل، استنى لحظة...")
            return
        _YT_ACTIVE.add(key)
    bot.answer_callback_query(call.id, "جاري التحميل...")
    entry["token"] = token
    is_photo = bool(getattr(call.message, "photo", None))
    threading.Thread(
        target=_yt_download_and_send,
        args=(entry, kind, call.message.chat.id, call.message.message_id, is_photo),
        daemon=True, name="yt-download",
    ).start()


@bot.callback_query_handler(func=lambda c: c.data.startswith("ytpick_"))
def cb_youtube_pick(call):
    token = call.data.split("_", 1)[1]
    entry = _YOUTUBE_PICK_CACHE.pop(token, None)
    if not entry:
        bot.answer_callback_query(call.id, "انتهت صلاحية الاختيار، ابحث تاني.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "جاري التحميل...")
    status_chat_id = call.message.chat.id
    status_message_id = call.message.message_id
    status_bc_id = entry.get("bc_id")
    status_edit_kwargs = {"business_connection_id": status_bc_id} if status_bc_id else {}
    try:
        bot.edit_message_text(
            f"{e('download')} <b>جاري تحميل:</b> {html_escape(entry['title'])}",
            status_chat_id, status_message_id, **status_edit_kwargs,
        )
    except Exception:
        pass
    threading.Thread(
        target=_youtube_download_and_deliver,
        args=(status_chat_id, status_message_id, entry),
        daemon=True,
    ).start()

def _send_youtube_audio(entry, filepath, title, _duration=None):
    bc_id = entry.get("bc_id")
    info = _song_info(entry.get("video_id"), title, _duration)
    try:
        _send_song_audio(entry["chat_id"], filepath, info,
                         business_connection_id=bc_id, duration=_duration)
        return False
    except Exception as exc:
        if not bc_id or "BUSINESS_CONNECTION_INVALID" not in str(exc):
            raise
        print(f"YouTube send: ربط الـ Business مع العميل غير صالح — تحويل الملف للمالك ({exc})", flush=True)
    connection = db.get_business_connection(bc_id) or {}
    owner_chat = connection.get("owner_chat_id") or config.ADMIN_ID
    with open(filepath, "rb") as audio:
        bot.send_audio(owner_chat, audio, title=title,
                       caption="مقدرتش أبعتها للعميل (ربط الـ Business غير صالح) — حوّلها بنفسك.")
    return True

def _youtube_download_and_deliver(status_chat_id, status_message_id, entry):
    filepath = None
    fell_back = False
    status_bc_id = entry.get("bc_id")
    status_edit_kwargs = {"business_connection_id": status_bc_id} if status_bc_id else {}
    try:
        try:
            filepath, real_title, _duration = youtube_download_audio(entry["video_id"])
            fell_back = _send_youtube_audio(entry, filepath, real_title or entry["title"], _duration)
        except Exception as exc:
            print(f"YouTube download/send failed: {exc}", flush=True)
            generic = notify_admin_download_error(
                "تحميل يوتيوب", exc, extra_lines=[f"الأغنية: {entry.get('title', '')}"]
            )
            try:
                safe_edit_message_text(generic, status_chat_id, status_message_id, **status_edit_kwargs)
            except Exception:
                pass
            return

        try:
            safe_edit_message_text(
                (f"{e('cross')} <b>ربط الـ Business مع العميل مش صالح، بعتّلك الأغنية في الخاص:</b> "
                 if fell_back else f"{e('check')} <b>تم بعت الأغنية للعميل:</b> ")
                + html_escape(real_title or entry['title']),
                status_chat_id, status_message_id, **status_edit_kwargs,
            )
        except Exception as exc:
            print(f"YouTube status edit failed (الملف اتبعت بنجاح فعليًا): {exc}", flush=True)
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass

TIKTOK_COOKIES_FILE = os.environ.get(
    "TIKTOK_COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_cookies.txt"),
)

_TT_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
)

_TT_API_HOSTNAMES = (
    None,
    "api16-normal-c-useast1a.tiktokv.com",
    "api19-normal-c-useast1a.tiktokv.com",
    "api22-normal-c-useast2a.tiktokv.com",
)

_TIKTOK_ACTIVE = set()
_TIKTOK_LOCK = threading.Lock()

_TIKTOK_COMMAND_RE = re.compile(
    r"^/?(?:تحميل\s+)?تيك\s*توك(?:\s+(صوت|فيديو))?\s+(https?://\S+)$", re.I
)

_TIKTOK_PRIVATE_URL_RE = re.compile(
    r"^(?:تيك\s*توك\s+)?(?:(صوت|فيديو)\s+)?(https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+)$", re.I
)

def _cookie_file_valid(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#"):
                    return True
    except OSError:
        pass
    return False

def _tt_common_headers():
    return {
        "User-Agent": random.choice(_TT_UA_POOL),
        "Referer": "https://www.tiktok.com/",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }

def _tt_extract_info(url, download, base_opts=None, timeout_budget=90):
    last_err = None
    start = time.time()
    # جرّب الاتصال المباشر أولًا؛ البروكسيات العامة الميتة تستهلك المهلة قبل الوصول إليه.
    attempts = [(None, None)]
    attempts.extend((None, host) for host in _TT_API_HOSTNAMES if host)
    attempts.extend((proxy, None) for proxy in proxy_pool.candidates(max_tries=2))
    for proxy, host in attempts:
        if timeout_budget is not None and (time.time() - start) >= timeout_budget:
            break
        opts = dict(base_opts or {})
        opts["http_headers"] = {**_tt_common_headers(), **(opts.get("http_headers") or {})}
        opts["socket_timeout"] = 8
        opts["retries"] = 1
        if proxy:
            opts["proxy"] = proxy
        elif TIKTOK_COOKIES_FILE and os.path.exists(TIKTOK_COOKIES_FILE) and _cookie_file_valid(TIKTOK_COOKIES_FILE):
            opts.setdefault("cookiefile", TIKTOK_COOKIES_FILE)
        if host:
            extractor_args = dict(opts.get("extractor_args") or {})
            extractor_args["tiktok"] = {"api_hostname": [host]}
            opts["extractor_args"] = extractor_args
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(url, download=download)
            proxy_pool.mark_good(proxy)
            return result
        except Exception as exc:
            last_err = exc
            if proxy:
                print(f"tiktok: فشل عبر {proxy_pool.mask(proxy)}: {str(exc)[:120]}", flush=True)
                proxy_pool.mark_bad(proxy, proxy_pool.classify_error(str(exc)))
            continue
    raise last_err or RuntimeError("تعذر تنفيذ طلب تيك توك (كل المحاولات فشلت)")

TT_API_URLS = [u.strip() for u in os.environ.get("TIKTOK_API_URLS", "https://www.tikwm.com/api/").split(",") if u.strip()]
TT_TG_LIMIT = 49 * 1024 * 1024
TT_API_MAX_BYTES = 300 * 1024 * 1024


def _tt_api_abs(media_url, base):
    from urllib.parse import urljoin
    if not media_url or not isinstance(media_url, str):
        return None
    if media_url.startswith("//"):
        return "https:" + media_url
    if media_url.startswith("/"):
        return urljoin(base, media_url)
    return media_url if media_url.startswith(("http://", "https://")) else None


def _tt_api_fetch(session, api_url, url):
    last = None
    for attempt in range(2):
        resp = session.get(
            api_url, params={"url": url, "hd": 1},
            headers={"User-Agent": random.choice(_TT_UA_POOL), "Accept": "application/json"},
            timeout=(10, 25),
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
            return payload["data"]
        msg = str((payload or {}).get("msg") or "رد غير ناجح") if isinstance(payload, dict) else "رد غير مفهوم"
        last = RuntimeError(f"API تيك توك: {msg[:150]}")
        if "limit" in msg.lower() and attempt == 0:
            time.sleep(1.5)
            continue
        break
    raise last or RuntimeError("API تيك توك: رد غير ناجح")


def _tt_api_save(session, media_url, path, referer):
    total = 0
    with session.get(media_url, headers={"User-Agent": random.choice(_TT_UA_POOL), "Referer": referer},
                     stream=True, timeout=(15, 60)) as resp:
        resp.raise_for_status()
        with open(path, "wb") as out:
            for chunk in resp.iter_content(512 * 1024):
                if chunk:
                    total += len(chunk)
                    if total > TT_API_MAX_BYTES:
                        raise RuntimeError("حجم الملف أكبر من الحد المسموح")
                    out.write(chunk)
    with open(path, "rb") as fh:
        head = fh.read(16)
    if total < 1024 or head[:5].lower() in (b"<!doc", b"<html") or head[:1] == b"{":
        raise RuntimeError("الـ API رجّع ملف غير صالح")
    return total


def _tt_api_get_files(session, data, api_url, mode, downloads_dir, stamp):
    title = (data.get("title") or "TikTok")[:180]
    if mode == "audio":
        music = _tt_api_abs(data.get("music") or (data.get("music_info") or {}).get("play"), api_url)
        if music:
            mp3 = os.path.join(downloads_dir, f"tiktok_{stamp}.mp3")
            _tt_api_save(session, music, mp3, api_url)
            return mp3, title
    video_urls = []
    for key in ("hdplay", "play"):
        u = _tt_api_abs(data.get(key), api_url)
        if u and u not in video_urls:
            video_urls.append(u)
    if not video_urls:
        raise RuntimeError("المنشور ده مفيهوش فيديو (ممكن يكون صور أو غير متاح).")
    vpath = os.path.join(downloads_dir, f"tiktok_{stamp}.mp4")
    for i, vu in enumerate(video_urls):
        size = _tt_api_save(session, vu, vpath, api_url)
        if size <= TT_TG_LIMIT or i == len(video_urls) - 1:
            break
    if mode != "audio":
        return vpath, title
    mp3 = os.path.join(downloads_dir, f"tiktok_{stamp}.mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", vpath, "-vn",
                    "-codec:a", "libmp3lame", "-b:a", "192k", mp3], check=True, timeout=300)
    try:
        os.remove(vpath)
    except OSError:
        pass
    return mp3, title


def _tt_api_download(url, mode, downloads_dir, stamp):
    """بديل لما yt-dlp يفشل (حظر إقليمي 10231 وغيره): API خارجي بيجيب الفيديو من سيرفر في بلد تانية."""
    last_exc = None
    routes = [None] + list(proxy_pool.candidates(2))
    for proxy in routes:
        session = requests.Session()
        if proxy:
            session.proxies = proxy_pool.requests_proxies(proxy)
        try:
            for api_url in TT_API_URLS:
                try:
                    data = _tt_api_fetch(session, api_url, url)
                    result = _tt_api_get_files(session, data, api_url, mode, downloads_dir, stamp)
                    print(f"tiktok: نجح التحميل عبر الـ API البديل ({api_url})", flush=True)
                    return result
                except requests.exceptions.RequestException as exc:
                    last_exc = exc
                    if proxy:
                        proxy_pool.mark_bad(proxy, "proxy")
                        break
                except Exception as exc:
                    last_exc = exc
        finally:
            session.close()
    raise last_exc or RuntimeError("الـ API البديل فشل")


def tiktok_download(url, mode):
    downloads_dir = os.path.join(tempfile.gettempdir(), "tiktok_downloads")
    os.makedirs(downloads_dir, exist_ok=True)
    stamp = uuid.uuid4().hex[:12]
    outtmpl = os.path.join(downloads_dir, f"tiktok_{stamp}.%(ext)s")
    common = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
    }
    if mode == "audio":
        opts = dict(common)
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    else:
        opts = dict(common)

        opts.update({
            "format": "download_addr/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "format_sort": ["res:1080", "fps", "codec:h264", "size"],
            "merge_output_format": "mp4",
        })
    try:
        info = _tt_extract_info(url, download=True, base_opts=opts, timeout_budget=90)
    except Exception as ytdlp_exc:
        ytdlp_msg = str(ytdlp_exc)
        print(f"tiktok: yt-dlp فشل ({ytdlp_msg[:160]}) — هنجرب الـ API البديل", flush=True)
        for leftover in os.listdir(downloads_dir):
            if stamp in leftover:
                try:
                    os.remove(os.path.join(downloads_dir, leftover))
                except OSError:
                    pass
        try:
            return _tt_api_download(url, mode, downloads_dir, stamp)
        except Exception as api_exc:
            print(f"tiktok: الـ API البديل فشل كمان: {str(api_exc)[:160]}", flush=True)
            hint = " (الفيديو محظور في بلد السيرفر)" if "10231" in ytdlp_msg else ""
            raise RuntimeError(
                f"yt-dlp{hint}: {ytdlp_msg[:220]} | API: {str(api_exc)[:150]}"
            ) from api_exc
    title = (info.get("title") or "TikTok")[:180]
    ext_priority = (".mp3",) if mode == "audio" else (".mp4",)
    candidates = [os.path.join(downloads_dir, f) for f in os.listdir(downloads_dir) if stamp in f]
    candidates.sort(key=lambda p: os.path.splitext(p)[1] not in ext_priority)
    for candidate in candidates:
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate, title
    raise RuntimeError("لم يتم العثور على الملف بعد التحميل")

def _tiktok_parse_business(text):
    m = _TIKTOK_COMMAND_RE.match((text or "").strip())
    if not m:
        return None
    mode = "audio" if (m.group(1) or "").strip() == "صوت" else "video"
    return mode, m.group(2)

def _tiktok_parse_private(text):
    m = _TIKTOK_PRIVATE_URL_RE.match((text or "").strip())
    if not m:
        return None
    mode = "audio" if (m.group(1) or "").strip() == "صوت" else "video"
    return mode, m.group(2)

def _do_tiktok_download_and_send(owner_chat_id, target_chat_id, bc_id, mode, url, status_chat_id, status_message_id,
                                 caption_on=True):
    key = f"{target_chat_id}:{url}"
    with _TIKTOK_LOCK:
        if key in _TIKTOK_ACTIVE:
            safe_send_message(owner_chat_id, f"{e('cross')} <b>لديك تحميل تيك توك قيد التنفيذ بالفعل لنفس الرابط.</b>")
            return
    _TIKTOK_ACTIVE.add(key)
    filepath = None
    status_edit_kwargs = _business_status_kwargs(bc_id, status_chat_id, owner_chat_id)
    try:
        try:
            filepath, title = _resilient_download("TikTok", url, mode)
            icon = "music" if mode == "audio" else "download"
            caption = f"{e(icon)} <b>{html_escape(title)}</b>"
            send_kwargs = {"caption": caption} if caption_on else {}
            if bc_id:
                send_kwargs["business_connection_id"] = bc_id
            with open(filepath, "rb") as media:
                if mode == "audio":
                    bot.send_audio(target_chat_id, media, title=title[:64], **send_kwargs)
                else:
                    bot.send_video(target_chat_id, media, supports_streaming=True, **send_kwargs)
        except Exception as exc:
            print(f"TikTok download/send failed: {exc}", flush=True)
            err_text = notify_admin_download_error(
                "تحميل تيك توك", exc, extra_lines=[f"الرابط: {url}", f"الوضع: {mode}"]
            )
            if status_chat_id and status_message_id:
                try:
                    safe_edit_message_text(err_text, status_chat_id, status_message_id, **status_edit_kwargs)
                except Exception:
                    pass
            return

        if status_chat_id and status_message_id:
            try:
                safe_edit_message_text(
                    f"{e('check')} <b>تم تحميل وإرسال:</b> {html_escape(title)}",
                    status_chat_id, status_message_id, **status_edit_kwargs,
                )
            except Exception as exc:
                print(f"TikTok status edit failed (الملف اتبعت بنجاح فعليًا): {exc}", flush=True)
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
        with _TIKTOK_LOCK:
            _TIKTOK_ACTIVE.discard(key)

@bot.message_handler(
    func=lambda m: m.chat.type == "private" and m.content_type == "text" and _tiktok_parse_private(m.text)
)
def private_tiktok_download(message):
    mode, url = _tiktok_parse_private(message.text)
    if social_p_state(message.from_user.id, "tiktok") == "disabled":
        safe_send_message(message.chat.id, f"{e('cross')} <b>تحميل تيك توك مقفول من إعدادات السوشيال ميديا.</b>")
        return
    mode = social_apply_types(social_p_types(message.from_user.id, "tiktok"), mode)
    status = safe_send_message(message.chat.id, f"{e('download')} <b>جاري تحميل رابط تيك توك...</b>")
    threading.Thread(
        target=_do_tiktok_download_and_send,
        args=(
            message.chat.id, message.chat.id, None, mode, url,
            message.chat.id, status.message_id if status else None,
        ),
        kwargs={"caption_on": social_p_caption(message.from_user.id, "tiktok")},
        daemon=True,
        name="tiktok-private-download",
    ).start()

_UD_ACTIVE = set()
_UD_LOCK = threading.Lock()

def _resilient_download(platform, url, mode, quality=None):
    """تحميل بيعالج نفسه: إعادة محاولة، وتيك توك لو فشل بيجرب المحمّل العام كمان."""
    last_exc = None
    attempts = 1 if platform == "TikTok" else 2
    for attempt in range(attempts):
        try:
            if platform == "TikTok":
                return tiktok_download(url, mode)
            return ud.download(url, mode, quality=quality)
        except Exception as exc:
            last_exc = exc
            print(f"download retry {attempt + 1} ({platform}): {str(exc)[:160]}", flush=True)
            time.sleep(2)
    if platform == "TikTok":
        try:
            return ud.download(url, mode, quality=quality)
        except Exception as exc:
            last_exc = RuntimeError(f"{str(last_exc)[:200]} | generic: {str(exc)[:200]}")
    raise last_exc

def _do_universal_download_and_send(owner_chat_id, target_chat_id, bc_id, mode, url, status_chat_id, status_message_id,
                                    caption_on=True, quality=None):
    """تحميل من أي منصة مدعومة. تيك توك بيروح لمسارها المخصص (كوكيز/بروكسي)."""
    key = f"{target_chat_id}:{url}"
    with _UD_LOCK:
        if key in _UD_ACTIVE:
            safe_send_message(owner_chat_id, f"{e('cross')} <b>الرابط ده قيد التحميل بالفعل.</b>")
            return
    _UD_ACTIVE.add(key)
    filepath = None
    status_edit_kwargs = _business_status_kwargs(bc_id, status_chat_id, owner_chat_id)
    platform = ud.platform_of(url) or "الموقع"
    try:
        try:
            icon = "music" if mode == "audio" else "download"
            sc_fast = platform == "SoundCloud" and mode == "audio"
            sc_meta = {}
            sc_ckey = None
            sent_from_cache = False
            if sc_fast:
                # كاش: نفس الرابط اتبعت قبل كده -> بنبعته بـ file_id فورًا (من غير تحميل ولا رفع)
                sc_ckey = "sccache_" + hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:20]
                try:
                    cached = json.loads(db.get_setting(sc_ckey, "") or "null")
                except Exception:
                    cached = None
                if cached and cached.get("file_id"):
                    title = cached.get("title") or "SoundCloud"
                    cache_kwargs = {"business_connection_id": bc_id} if bc_id else {}
                    if caption_on:
                        cache_kwargs["caption"] = f"{e(icon)} <b>{html_escape(title)}</b>"
                    try:
                        bot.send_audio(target_chat_id, cached["file_id"], **cache_kwargs)
                        sent_from_cache = True
                    except Exception as exc:
                        print(f"SoundCloud cache send failed, re-downloading: {exc}", flush=True)
            if not sent_from_cache:
                if sc_fast and platform != "TikTok":
                    filepath, sc_meta = ud.download_soundcloud(url)
                    title = sc_meta["title"]
                else:
                    filepath, title = _resilient_download(platform, url, mode, quality=quality)
                if os.path.getsize(filepath) > ud.MAX_FILE_BYTES:
                    raise RuntimeError(f"الملف أكبر من {ud.MAX_FILE_BYTES // (1024 * 1024)}MB.")
                send_kwargs = {"caption": f"{e(icon)} <b>{html_escape(title)}</b>"} if caption_on else {}
                if bc_id:
                    send_kwargs["business_connection_id"] = bc_id
                with open(filepath, "rb") as media:
                    if mode == "audio":
                        # بنبعت المدة والفنان مع الملف عشان تليجرام ميضيعش وقت في فحصه
                        sent = bot.send_audio(
                            target_chat_id, media, title=title[:64],
                            performer=(sc_meta.get("artist") or None),
                            duration=sc_meta.get("duration"),
                            **send_kwargs,
                        )
                        fid = getattr(getattr(sent, "audio", None), "file_id", None)
                        if sc_ckey and fid:
                            try:
                                db.set_setting(sc_ckey, json.dumps({"file_id": fid, "title": title}, ensure_ascii=False))
                            except Exception as exc:
                                print(f"SoundCloud cache save failed: {exc}", flush=True)
                    else:
                        bot.send_video(target_chat_id, media, supports_streaming=True, **send_kwargs)
        except Exception as exc:
            print(f"Universal download/send failed ({platform}): {exc}", flush=True)
            err_text = notify_admin_download_error(
                f"تحميل {platform}", exc, extra_lines=[f"الرابط: {url}", f"الوضع: {mode}"]
            )
            if status_chat_id and status_message_id:
                try:
                    safe_edit_message_text(err_text, status_chat_id, status_message_id, **status_edit_kwargs)
                except Exception:
                    pass
            return
        if status_chat_id and status_message_id:
            try:
                safe_edit_message_text(
                    f"{e('check')} <b>تم تحميل وإرسال:</b> {html_escape(title)}",
                    status_chat_id, status_message_id, **status_edit_kwargs,
                )
            except Exception as exc:
                print(f"Universal status edit failed: {exc}", flush=True)
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
        with _UD_LOCK:
            _UD_ACTIVE.discard(key)

def _song_cache_key(query):
    norm = re.sub(r"\s+", " ", _normalize_ar_text(query).lower()).strip()
    return "songcache_" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20]

def _do_song_download_and_send(owner_chat_id, target_chat_id, bc_id, query, status_chat_id, status_message_id):
    """«تحميل <اسم أغنية>»: SoundCloud -> mp3. نفس الأغنية تاني بتتبعت فورًا من كاش file_id."""
    key = f"song:{target_chat_id}:{query.lower()}"
    with _UD_LOCK:
        if key in _UD_ACTIVE:
            return
        _UD_ACTIVE.add(key)
    filepath = None
    send_kw = {"business_connection_id": bc_id} if bc_id else {}
    edit_kw = {"business_connection_id": bc_id} if (bc_id and status_chat_id == target_chat_id) else {}

    def _status(text):
        if status_chat_id and status_message_id:
            try:
                safe_edit_message_text(text, status_chat_id, status_message_id, **edit_kw)
            except Exception as exc:
                print(f"Song status edit failed: {exc}", flush=True)

    try:
        ckey = _song_cache_key(query)
        title = query
        sent = None
        try:
            cached = json.loads(db.get_setting(ckey, "") or "null")
        except Exception:
            cached = None
        if cached and cached.get("file_id"):
            try:
                sent = bot.send_audio(target_chat_id, cached["file_id"], **send_kw)
                title = cached.get("title") or query
            except Exception as exc:
                print(f"Song cache send failed, re-downloading: {exc}", flush=True)
                sent = None
        if sent is None:
            try:
                filepath, meta = ud.download_song(query)
                title = meta["title"]
                with open(filepath, "rb") as media:
                    sent = bot.send_audio(
                        target_chat_id, media, title=title[:64],
                        performer=(meta.get("artist") or None), duration=meta.get("duration"),
                        **send_kw,
                    )
                fid = getattr(getattr(sent, "audio", None), "file_id", None)
                if fid:
                    db.set_setting(ckey, json.dumps({"file_id": fid, "title": title}, ensure_ascii=False))
            except ud.SongNotFound as exc:
                _status(f"{e('cross')} <b>{html_escape(str(exc))}</b>")
                return
            except Exception as exc:
                print(f"Song download/send failed: {exc}", flush=True)
                _status(notify_admin_download_error("أغنية SoundCloud", exc, extra_lines=[f"البحث: {query}"]))
                return
        _status(f"{e('check')} <b>تم إرسال:</b> {html_escape(title)}")
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
        with _UD_LOCK:
            _UD_ACTIVE.discard(key)

def _replied_text(message):
    r = getattr(message, "reply_to_message", None)
    if not r:
        return ""
    return _message_text_with_link_entities(r)


def _message_text_with_link_entities(message):
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
    hidden_urls = [
        getattr(entity, "url", None)
        for entity in entities
        if getattr(entity, "type", None) == "text_link" and getattr(entity, "url", None)
    ]
    return " ".join([text, *hidden_urls]).strip()

@bot.message_handler(
    func=lambda m: m.chat.type == "private"
    and (m.content_type == "text" or bool(getattr(m, "caption", None)))
    and ud.parse_request(_message_text_with_link_entities(m), _replied_text(m))
)
def private_universal_download(message):
    mode, url, _explicit = ud.parse_request(_message_text_with_link_entities(message), _replied_text(message))
    pkey = social_platform_key(url)
    if social_p_state(message.from_user.id, pkey) == "disabled":
        safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>تحميل {SOCIAL_PLATFORMS[pkey]['label']} مقفول من إعدادات السوشيال ميديا.</b>",
        )
        return
    mode = social_apply_types(social_p_types(message.from_user.id, pkey), mode)
    status = safe_send_message(message.chat.id, f"{e('download')} <b>جاري تحميل الرابط...</b>")
    threading.Thread(
        target=_do_universal_download_and_send,
        args=(
            message.chat.id, message.chat.id, None, mode, url,
            message.chat.id, status.message_id if status else None,
        ),
        kwargs={"caption_on": social_p_caption(message.from_user.id, pkey),
                "quality": social_p_quality_int(message.from_user.id, pkey)},
        daemon=True,
        name="universal-private-download",
    ).start()

@bot.message_handler(
    func=lambda m: m.chat.type == "private" and m.content_type == "text"
    and (m.text or "").strip().startswith(YOUTUBE_TRIGGER_PREFIX + " ")
    and (m.text or "").strip()[len(YOUTUBE_TRIGGER_PREFIX):].strip() != ""
)
def private_youtube_command(message):
    uid = message.from_user.id
    if not feature_enabled("social_media") or social_p_state(uid, "youtube") == "disabled":
        safe_send_message(message.chat.id, f"{e('cross')} <b>تحميل يوتيوب مقفول من إعدادات السوشيال ميديا.</b>")
        return
    query = message.text.strip()[len(YOUTUBE_TRIGGER_PREFIX):].strip()
    safe_send_message(message.chat.id, f"{e('music')} <b>جاري البحث عن:</b> {html_escape(query)}")
    threading.Thread(
        target=_youtube_search_and_offer,
        args=(message.chat.id, None, query),
        kwargs={"kinds": social_p_types(uid, "youtube")},
        daemon=True, name="youtube-private-search",
    ).start()

@bot.message_handler(
    func=lambda m: m.chat.type == "private" and m.content_type == "text"
    and owner_feature_on("song_download", m.from_user.id) and ud.parse_song_request(m.text)
)
def private_song_download(message):
    query = ud.parse_song_request(message.text)
    safe_send_message(message.chat.id, f"{e('music')} <b>جاري البحث عن:</b> {html_escape(query)}")
    threading.Thread(
        target=_youtube_search_and_offer,
        args=(message.chat.id, None, query),
        kwargs={"kinds": "a"},
        daemon=True, name="song-private-search",
    ).start()

@bot.callback_query_handler(func=lambda c: c.data == "muted_menu")
def cb_muted_menu(call):
    if not _require_active_connection(call):
        return
    safe_edit_message_text(
        f"{e('mute')} <b>إدارة المكتومين</b>\n\n"
        "العميل المكتوم مش هيوصله رد تلقائي، ومش هتوصلك إشعارات بالرسايل "
        "المحذوفة بتاعته.\n\n"
        "<b>اختار من الأسفل:</b>",
        call.message.chat.id, call.message.message_id,
        reply_markup=muted_menu_markup(),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "muted_back")
def cb_muted_back(call):
    safe_edit_message_text(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id, reply_markup=owner_panel_markup(call.from_user.id)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "muted_add")
def cb_muted_add(call):
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"{e('mute')} <b>حوّل (Forward) أي رسالة من العميل اللي عايز تكتمه</b>، "
        "أو ابعت آيدي (ID) العميل مباشرة لو عارفه.",
        call.message.chat.id, call.message.message_id,
        reply_markup=types.InlineKeyboardMarkup(),
    )
    bot.register_next_step_handler(call.message, step_get_mute_target, call.message.message_id)

def step_get_mute_target(message, prompt_message_id=None):
    owner_id = message.from_user.id
    target_id = None
    target_name = ""
    target_username = ""

    forwarded_user = _forwarded_user(message)
    if forwarded_user:
        target_id = forwarded_user.id
        target_name = display_name(forwarded_user)
        target_username = forwarded_user.username or ""
    else:
        text = (message.text or "").strip()
        if text.isdigit():
            target_id = int(text)
            info = db.find_sender_info(owner_id, target_id)
            if info:
                target_name = info.get("sender_name") or ""
                target_username = info.get("sender_username") or ""

    if not target_id:
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id,
            f"{e('cross')} مش قادر أعرف العميل ده. حوّل رسالة منه أو ابعت آيدي رقمي صحيح، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_mute_target, prompt_message_id)
        return

    if target_id == owner_id:
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id,
            f"{e('cross')} <b>ده حسابك انت، مينفعش تكتم نفسك.</b>\n"
            "حوّل رسالة من العميل أو ابعت آيدي العميل نفسه، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_mute_target, prompt_message_id)
        return

    class Target:
        pass
    target = Target()
    target.id = target_id
    target.first_name = target_name or "غير معروف"
    target.last_name = ""
    target.username = target_username or ""
    _notify_mute_result(message.chat.id, owner_id, target, True, edit_message_id=prompt_message_id)

@bot.callback_query_handler(func=lambda c: c.data == "muted_list")
def cb_muted_list(call):
    muted = db.get_muted_users(call.from_user.id)
    bot.answer_callback_query(call.id)
    if not muted:
        safe_edit_message_text(
            "<b>مفيش عملاء مكتومين لسه.</b>", call.message.chat.id, call.message.message_id,
            reply_markup=muted_menu_markup(),
        )
        return
    markup = types.InlineKeyboardMarkup()
    for m in muted:
        label = m["muted_name"] or str(m["muted_user_id"])
        markup.add(pe.btn(f" إلغاء كتم {label}", callback_data=f"muted_del_{m['muted_user_id']}", icon="unlock"))
    markup.add(pe.btn(" رجوع", callback_data="muted_menu", icon="arrow_left"))
    safe_edit_message_text(
        f"{e('mute')} <b>العملاء المكتومين حاليًا</b> (دوس عشان تلغي الكتم):",
        call.message.chat.id, call.message.message_id, reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("muted_del_"))
def cb_muted_delete(call):
    muted_user_id = int(call.data.split("_")[-1])
    db.unmute_user(call.from_user.id, muted_user_id)
    bot.answer_callback_query(call.id, "تم إلغاء الكتم.")
    safe_edit_message_text(
        f"{e('unlock')} <b>تم إلغاء الكتم عن العميل:</b> <code>{muted_user_id}</code>",
        call.message.chat.id, call.message.message_id, reply_markup=muted_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("unmute_inline_"))
def cb_unmute_inline(call):
    muted_user_id = int(call.data.split("_")[-1])
    if not db.is_muted(call.from_user.id, muted_user_id):
        bot.answer_callback_query(call.id, "العميل غير مكتوم بالفعل.", show_alert=True)
        return
    db.unmute_user(call.from_user.id, muted_user_id)
    bot.answer_callback_query(call.id, "تم إلغاء الكتم.")
    safe_edit_message_text(
        f"{e('unlock')} <b>تم إلغاء كتم العميل.</b>\n<b>ID:</b> <code>{muted_user_id}</code>",
        call.message.chat.id, call.message.message_id, reply_markup=muted_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("unexempt_inline_"))
def cb_unexempt_inline(call):
    try:
        uid = int(call.data.split("_")[-1])
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    db.remove_force_sub_exempt(call.from_user.id, uid)
    bot.answer_callback_query(call.id, "اتلغى الاستثناء.")
    safe_edit_message_text(
        f"{e('lock')} <b>اتلغى استثناء العميل.</b>\n<b>ID:</b> <code>{uid}</code>",
        call.message.chat.id, call.message.message_id,
    )

@bot.callback_query_handler(func=lambda c: c.data == "blocked_menu")
def cb_blocked_menu(call):
    if not _require_active_connection(call):
        return
    safe_edit_message_text(
        f"{e('shield')} <b>إدارة المحظورين</b>\n\n"
        "العميل المحظور بيتحذف كل اللي يبعته أوتوماتيك، ومش هتوصلك أي إشعارات "
        "(حذف/تعديل) أو ردود تلقائية عنه.\n\n"
        "<b>اختار من الأسفل:</b>",
        call.message.chat.id, call.message.message_id,
        reply_markup=blocked_menu_markup(),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "blocked_back")
def cb_blocked_back(call):
    safe_edit_message_text(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id, reply_markup=owner_panel_markup(call.from_user.id)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "blocked_add")
def cb_blocked_add(call):
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"{e('shield')} <b>حوّل (Forward) أي رسالة من العميل اللي عايز تحظره</b>، "
        "أو ابعت آيدي (ID) العميل مباشرة لو عارفه.",
        call.message.chat.id, call.message.message_id,
        reply_markup=types.InlineKeyboardMarkup(),
    )
    bot.register_next_step_handler(call.message, step_get_block_target, call.message.message_id)

def step_get_block_target(message, prompt_message_id=None):
    owner_id = message.from_user.id
    target_id = None
    target_name = ""
    target_username = ""

    forwarded_user = _forwarded_user(message)
    if forwarded_user:
        target_id = forwarded_user.id
        target_name = display_name(forwarded_user)
        target_username = forwarded_user.username or ""
    else:
        text = (message.text or "").strip()
        if text.isdigit():
            target_id = int(text)
            info = db.find_sender_info(owner_id, target_id)
            if info:
                target_name = info.get("sender_name") or ""
                target_username = info.get("sender_username") or ""

    if not target_id:
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id,
            f"{e('cross')} مش قادر أعرف العميل ده. حوّل رسالة منه أو ابعت آيدي رقمي صحيح، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_block_target, prompt_message_id)
        return

    class Target:
        pass
    target = Target()
    target.id = target_id
    target.first_name = target_name or "غير معروف"
    target.last_name = ""
    target.username = target_username or ""
    _notify_block_result(message.chat.id, owner_id, target, True, edit_message_id=prompt_message_id)

@bot.callback_query_handler(func=lambda c: c.data == "blocked_list")
def cb_blocked_list(call):
    blocked = db.get_blocked_users(call.from_user.id)
    bot.answer_callback_query(call.id)
    if not blocked:
        safe_edit_message_text(
            "<b>مفيش عملاء محظورين لسه.</b>", call.message.chat.id, call.message.message_id,
            reply_markup=blocked_menu_markup(),
        )
        return
    markup = types.InlineKeyboardMarkup()
    for b in blocked:
        label = b["blocked_name"] or str(b["blocked_user_id"])
        markup.add(pe.btn(f" فك حظر {label}", callback_data=f"blocked_del_{b['blocked_user_id']}", icon="unlock"))
    markup.add(pe.btn(" فك حظر الكل", callback_data="blocked_unblock_all", icon="unlock", style="danger"))
    markup.add(pe.btn(" رجوع", callback_data="blocked_menu", icon="arrow_left"))
    safe_edit_message_text(
        f"{e('shield')} <b>العملاء المحظورين حاليًا</b> (دوس عشان تفك الحظر):",
        call.message.chat.id, call.message.message_id, reply_markup=markup,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("blocked_del_"))
def cb_blocked_delete(call):
    blocked_user_id = int(call.data.split("_")[-1])
    db.unblock_user(call.from_user.id, blocked_user_id)
    bot.answer_callback_query(call.id, "تم فك الحظر.")
    safe_edit_message_text(
        f"{e('unlock')} <b>تم فك الحظر عن العميل:</b> <code>{blocked_user_id}</code>",
        call.message.chat.id, call.message.message_id, reply_markup=blocked_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("unblock_inline_"))
def cb_unblock_inline(call):
    blocked_user_id = int(call.data.split("_")[-1])
    if not db.is_blocked(call.from_user.id, blocked_user_id):
        bot.answer_callback_query(call.id, "العميل غير محظور بالفعل.", show_alert=True)
        return
    db.unblock_user(call.from_user.id, blocked_user_id)
    bot.answer_callback_query(call.id, "تم فك الحظر.")
    safe_edit_message_text(
        f"{e('unlock')} <b>تم فك حظر العميل.</b>\n<b>ID:</b> <code>{blocked_user_id}</code>",
        call.message.chat.id, call.message.message_id, reply_markup=blocked_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "blocked_unblock_all")
def cb_blocked_unblock_all(call):
    count = db.unblock_all_users(call.from_user.id)
    bot.answer_callback_query(call.id, "تم فك الحظر عن الجميع.")
    safe_edit_message_text(
        f"{e('unlock')} <b>تم فك الحظر عن كل العملاء</b> ({count} عميل).",
        call.message.chat.id, call.message.message_id,
        reply_markup=blocked_menu_markup(),
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("unpin_inline_"))
def cb_unpin_inline(call):
    token = call.data[len("unpin_inline_"):]
    target = PIN_UNDO_CACHE.pop(token, None)
    if not target:
        bot.answer_callback_query(call.id, "انتهت صلاحية الزرار ده.", show_alert=True)
        return
    bc_id, chat_id, message_id = target
    ok = unpin_business_message(chat_id, bc_id, message_id)
    if ok:
        bot.answer_callback_query(call.id, "تم إلغاء التثبيت.")
        safe_edit_message_text(
            f"{e('unlock')} <b>تم إلغاء تثبيت الرسالة.</b>",
            call.message.chat.id, call.message.message_id,
        )
    else:
        bot.answer_callback_query(call.id, "مقدرتش ألغي التثبيت.", show_alert=True)

_BROADCAST_MEDIA_SENDERS = {
    "photo": "send_photo",
    "video": "send_video",
    "document": "send_document",
    "animation": "send_animation",
    "audio": "send_audio",
    "voice": "send_voice",
}

def _broadcast_payload(message):
    ctype = message.content_type
    if ctype == "text":
        return ctype, message.html_text or message.text or "", None

    content = message.html_caption or message.caption or ""
    file_id = None
    if ctype == "photo" and message.photo:
        file_id = message.photo[-1].file_id
    elif ctype == "video" and message.video:
        file_id = message.video.file_id
    elif ctype == "document" and message.document:
        file_id = message.document.file_id
    elif ctype == "animation" and message.animation:
        file_id = message.animation.file_id
    elif ctype == "audio" and message.audio:
        file_id = message.audio.file_id
    elif ctype == "voice" and message.voice:
        file_id = message.voice.file_id
    return ctype, content, file_id

def _send_broadcast_one(chat_id, bc_id, ctype, content, file_id):
    method_name = _BROADCAST_MEDIA_SENDERS.get(ctype)
    if method_name and file_id:
        getattr(bot, method_name)(chat_id, file_id, caption=content, business_connection_id=bc_id)
    else:
        bot.send_message(chat_id, content, business_connection_id=bc_id)

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_menu")
def cb_broadcast_menu(call):
    if not _require_active_connection(call):
        return
    count = db.count_clients(call.from_user.id)
    safe_edit_message_text(
        f"{e('megaphone')} <b>قسم الإذاعة للعملاء</b>\n\n"
        f"{e('people')} عدد العملاء المسجلين لديك: <b>{count}</b> عميل\n\n"
        "تقدر تبعت رسالة توعية، عروض، أو تنبيهات لجميع العملاء المسجلين لديك "
        "بضغطة زر. يتم تسجيل العملاء تلقائيًا بمجرد إرسالهم أي رسالة لحسابك.",
        call.message.chat.id, call.message.message_id,
        reply_markup=broadcast_menu_markup(),
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_back")
def cb_broadcast_back(call):
    safe_edit_message_text(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id, reply_markup=owner_panel_markup(call.from_user.id)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_start")
def cb_broadcast_start(call):
    bot.answer_callback_query(call.id)
    safe_edit_message_text(
        f"{e('megaphone')} <b>ابعت محتوى الإذاعة</b> (نص، أو صورة/فيديو/ملف بكابشن).\n"
        "تقدر تستخدم HTML في النص عشان تنسيق زي <b>عريض</b> أو <i>مائل</i>، "
        "وهتتبعت لكل عملائك المسجلين بصفة حساب الـ Business بتاعك.",
        call.message.chat.id, call.message.message_id,
        reply_markup=types.InlineKeyboardMarkup(),
    )
    bot.register_next_step_handler(call.message, step_get_broadcast_content, call.message.message_id)

def step_get_broadcast_content(message, prompt_message_id=None):
    owner_id = message.from_user.id

    if message.content_type not in ("text",) + tuple(_BROADCAST_MEDIA_SENDERS.keys()):
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id,
            f"{e('cross')} نوع الرسالة ده مش مدعوم للإذاعة. ابعت نص أو صورة أو فيديو أو ملف، جرب تاني:",
        )
        bot.register_next_step_handler(message, step_get_broadcast_content, prompt_message_id)
        return

    ctype, content, file_id = _broadcast_payload(message)
    if ctype == "text" and not content.strip():
        _edit_prompt_or_send(message.chat.id, prompt_message_id, f"{e('cross')} الرسالة فاضية، ابعت محتوى الإذاعة:")
        bot.register_next_step_handler(message, step_get_broadcast_content, prompt_message_id)
        return

    clients = db.list_clients(owner_id)
    if not clients:
        _edit_prompt_or_send(
            message.chat.id, prompt_message_id,
            f"{e('cross')} <b>مفيش عملاء مسجلين تبعتلهم الإذاعة دلوقتي.</b>",
            reply_markup=broadcast_menu_markup(),
        )
        return

    plain_content = pe.strip_premium(content) if content else content
    sent, failed = 0, 0
    for chat_id, bc_id in clients:
        try:
            _send_broadcast_one(chat_id, bc_id, ctype, content, file_id)
            sent += 1
        except Exception:

            try:
                _send_broadcast_one(chat_id, bc_id, ctype, plain_content, file_id)
                sent += 1
            except Exception:
                failed += 1

    result_text = f"{e('check')} <b>تم إرسال الإذاعة.</b>\n\n{e('people_2')} وصلت لـ <b>{sent}</b> عميل"
    if failed:
        result_text += f"، وفشلت مع <b>{failed}</b> عميل."
    else:
        result_text += "."
    _edit_prompt_or_send(
        message.chat.id, prompt_message_id, result_text, reply_markup=broadcast_menu_markup()
    )

TRIAL_DURATION_SECONDS = 24 * 60 * 60

ACCESS_MODES = ["trial_24h", "free", "paid_only"]
ACCESS_MODE_INFO = {
    "trial_24h": {"label": "تجربة 24 ساعة", "icon": "clock", "style": "primary"},
    "free": {"label": "مجاني بالكامل", "icon": "unlock", "style": "success"},
    "paid_only": {"label": "مدفوع فقط (بدون تجربة)", "icon": "lock", "style": "danger"},
}
_ACCESS_MODE_SETTING_KEY = "global_access_mode"

def access_mode():
    mode = db.get_setting(_ACCESS_MODE_SETTING_KEY, "trial_24h")
    return mode if mode in ACCESS_MODES else "trial_24h"

def set_access_mode(mode):
    db.set_setting(_ACCESS_MODE_SETTING_KEY, mode)

def _next_access_mode(current):
    idx = ACCESS_MODES.index(current) if current in ACCESS_MODES else 0
    return ACCESS_MODES[(idx + 1) % len(ACCESS_MODES)]

def _trial_started_key(owner_id):
    return f"trial_started_at_{int(owner_id)}"

def _trial_notified_key(owner_id):
    return f"trial_expired_notified_{int(owner_id)}"

def trial_started_at(owner_id):
    value = db.get_setting(_trial_started_key(owner_id))
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None

def start_trial_if_needed(owner_id):
    if is_admin(owner_id):
        return False
    if trial_started_at(owner_id) is None:
        db.set_setting(_trial_started_key(owner_id), str(int(time.time())))
        return True
    return False

def trial_seconds_left(owner_id):
    started = trial_started_at(owner_id)
    if started is None:
        return None
    return TRIAL_DURATION_SECONDS - (int(time.time()) - started)

def trial_expired(owner_id):
    if is_admin(owner_id):
        return False
    if is_premium_active(owner_id):
        return False
    mode = access_mode()
    if mode == "free":
        return False
    if mode == "paid_only":
        return True
    remaining = trial_seconds_left(owner_id)
    return remaining is not None and remaining <= 0

def trial_expiry_already_notified(owner_id):
    return db.get_setting(_trial_notified_key(owner_id), "0") == "1"

def mark_trial_expiry_notified(owner_id):
    db.set_setting(_trial_notified_key(owner_id), "1")

def developer_contact_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" تواصل مع المطور", url=f"tg://user?id={config.DEVELOPER_ID}", style="success"))
    return markup

def _premium_was_key(owner_id):
    return f"premium_was_{int(owner_id)}"

def is_premium_active(owner_id):
    row = db.get_premium_user(owner_id)
    return bool(row and int(row.get("expires_at") or 0) > time.time())

def _lock_after_premium(owner_id):
    db.set_setting(_premium_was_key(owner_id), "1")
    if not trial_expired(owner_id):
        db.set_setting(
            _trial_started_key(owner_id),
            str(int(time.time()) - TRIAL_DURATION_SECONDS - 1),
        )
    mark_trial_expiry_notified(owner_id)

def access_expired_text(owner_id):
    if db.get_setting(_premium_was_key(owner_id)) == "1":
        return (
            "<b>اشتراكك المميز خلص.</b>\n"
            "<b>القوائم والإعدادات متاحة، لكن البوت مش هيعالج رسائل العملاء لحد ما تجدد.</b>"
        )
    if access_mode() == "paid_only":
        return (
            "<b>البوت ده متاح باشتراك مدفوع بس دلوقتي.</b>\n"
            "<b>القوائم والإعدادات متاحة، لكن البوت مش هيعالج رسائل العملاء لحد تفعيل الاشتراك.</b>"
        )
    return (
        "<b>التجربة المجانية بتاعتك خلصت.</b>\n"
        "<b>القوائم والإعدادات متاحة، لكن البوت مش هيعالج رسائل العملاء لحد تفعيل الاشتراك.</b>"
    )

def access_expired_alert(owner_id):
    if db.get_setting(_premium_was_key(owner_id)) == "1":
        return "اشتراكك المميز خلص؛ تفاعل البوت داخل محادثات العملاء متوقف لحد التجديد."
    if access_mode() == "paid_only":
        return "البوت متاح باشتراك مدفوع؛ تفاعل البوت داخل محادثات العملاء متوقف لحد التفعيل."
    return "التجربة المجانية خلصت؛ تفاعل البوت داخل محادثات العملاء متوقف لحد تفعيل الاشتراك."

def main_menu_text(user_id):
    if is_admin(user_id):
        status, remaining = "أدمن", "غير محدودة"
    else:
        premium = db.get_premium_user(user_id)
        premium_left = int(premium["expires_at"]) - int(time.time()) if premium else 0
        if premium_left > 0:
            status, remaining = "مدفوع", _fmt_remaining_full(premium_left)
        elif access_mode() == "free":
            status, remaining = "مجاني بالكامل", "غير محدودة"
        elif trial_expired(user_id):
            status, remaining = "انتهى الوصول", "يلزم تفعيل الاشتراك"
        else:
            trial_left = trial_seconds_left(user_id)
            if trial_left is None:
                status, remaining = "مفعّل", "غير محدودة"
            else:
                status, remaining = "فترة تجريبية مجانية", _fmt_remaining_full(trial_left)
    _mvals = {
        "الحالة": html_escape(str(status)),
        "المدة_المتبقية": html_escape(str(remaining)),
        "الايدي": str(int(user_id)),
    }
    text = render_message_style(user_id, "main_menu", _mvals)
    return text

def _notify_trial_expired(owner_chat_id, owner_id):
    if trial_expiry_already_notified(owner_id):
        return
    safe_send_message(
        owner_chat_id,
        f"{access_expired_text(owner_id)}\n\n{main_menu_text(owner_id)}",
        reply_markup=owner_panel_markup(owner_id),
    )
    mark_trial_expiry_notified(owner_id)

def _trial_watcher_loop():
    while True:
        try:
            for row in db.list_active_owner_chat_ids():
                owner_id = row.get("owner_user_id")
                owner_chat_id = row.get("owner_chat_id")
                if owner_id is None or owner_chat_id is None:
                    continue
                if trial_expired(owner_id) and not trial_expiry_already_notified(owner_id):
                    _notify_trial_expired(owner_chat_id, owner_id)
        except Exception as exc:
            print(f"Trial watcher error: {exc}", flush=True)
        time.sleep(60)

DISCONNECT_GRACE_SECONDS = 2 * 3600  # بعد الفصل: ساعتين ويتمسح كل حاجة

def _bcoff_key(bc_id):
    return f"bcoff_{bc_id}"

def _notify_admin(text, **kwargs):
    """إشعار للمطوّر/الأدمن (مبيفشلش مهما حصل)."""
    try:
        safe_send_message(int(config.ADMIN_ID), text, **kwargs)
    except Exception as exc:
        print(f"admin notify failed: {exc}", flush=True)

def _who_block(user):
    uname = f"@{html_escape(user.username)}" if getattr(user, "username", None) else "لا يوجد"
    return (
        f"{e('person')} <b>الاسم:</b> {html_escape(display_name(user))}\n"
        f"{e('link')} <b>اليوزر:</b> {uname}\n"
        f"{e('id_badge')} <b>الأيدي:</b> <code>{user.id}</code>"
    )

def _connection_subscription_status(user_id):
    """Return a human-readable access/subscription status for Business-link notices."""
    now = int(time.time())
    if is_admin(user_id):
        return "<b>حالة الاشتراك:</b> حساب إداري — صلاحية غير محدودة.", False
    premium = db.get_premium_user(user_id)
    if premium and int(premium.get("expires_at") or 0) > now:
        granted = int(premium.get("granted_at") or now)
        expires = int(premium.get("expires_at") or 0)
        if expires >= 4_000_000_000:
            return (
                f"{e('diamond')} <b>اشتراك مميز دائم</b> منذ: {_fmt_ts(granted)}.", False
            )
        return (
            f"{e('diamond')} <b>اشتراك مميز فعّال</b> من {_fmt_ts(granted)} "
            f"إلى {_fmt_ts(expires)}.", False
        )
    if access_mode() == "free":
        return "<b>حالة الاشتراك:</b> لا يوجد اشتراك مدفوع، والبوت مجاني بالكامل حاليًا.", False
    if access_mode() == "trial_24h":
        started = trial_started_at(user_id)
        if started is not None and int(started) + TRIAL_DURATION_SECONDS > now:
            ends = int(started) + TRIAL_DURATION_SECONDS
            return (
                f"{e('clock')} <b>التجربة المجانية:</b> من {_fmt_ts(int(started))} "
                f"إلى {_fmt_ts(ends)}. بعد انتهائها تواصل مع المطور للتجديد.", True
            )
    return (
        f"{e('cross')} <b>لا يوجد اشتراك فعّال حاليًا.</b> "
        "تواصل مع المطور لتفعيل الاشتراك.", True
    )

@bot.business_connection_handler()
def handle_business_connection(bc):
    owner = bc.user
    _had_before = db.get_business_connection(bc.id) is not None
    db.upsert_business_connection(
        bc_id=bc.id,
        owner_user_id=owner.id,
        owner_chat_id=bc.user_chat_id,
        owner_name=display_name(owner),
        owner_username=owner.username or "",
        is_enabled=1 if bc.is_enabled else 0,
        created_at=int(time.time()),
    )
    _trial_just_started = start_trial_if_needed(owner.id) if bc.is_enabled else False
    try:
        if bc.is_enabled:
            # رجع خلال المهلة: امسح عدّاد الفصل وكل حاجة زي ما هي
            for _row in db.list_connections_for_owner(owner.id):
                _bid = _row.get("business_connection_id")
                if _bid:
                    db.set_setting(_bcoff_key(_bid), "")
            if not is_admin(owner.id):
                _notify_admin(
                    f"{e('link')} <b>{'إعادة ربط' if _had_before else 'ربط جديد'} للبوت على حساب</b>\n\n"
                    f"{_who_block(owner)}\n"
                    f"<b>Business connection:</b> <code>{html_escape(str(bc.id))}</code>\n\n"
                    f"{_connection_subscription_status(owner.id)[0]}"
                )
        else:
            db.set_setting(_bcoff_key(bc.id), str(int(time.time())))
            if not is_admin(owner.id):
                _notify_admin(
                    f"{e('cross')} <b>فصل البوت من حساب</b>\n\n{_who_block(owner)}\n\n"
                    "هتتمسح بياناته بعد ساعتين لو مرجعش ربط."
                )
    except Exception as exc:
        print(f"business connection notify failed: {exc}", flush=True)
    try:
        if bc.is_enabled:
            _cvals = _ev_values(owner.id, display_name(owner), owner.username or "")
            _send_styled_owner(bc.user_chat_id, owner.id, "connected", _cvals)
            send_first_link_welcome(bc.user_chat_id, owner.id, owner)
            _subscription_text, _contact_dev = _connection_subscription_status(owner.id)
            safe_send_message(
                bc.user_chat_id, _subscription_text,
                reply_markup=developer_contact_keyboard() if _contact_dev else None,
            )

            if _trial_just_started:
                mode = access_mode()
                if mode == "free":
                    safe_send_message(
                        bc.user_chat_id,
                        "<b>البوت شغال مجاني بالكامل دلوقتي.</b>",
                    )
                elif mode == "trial_24h":
                    _send_styled_owner(
                        bc.user_chat_id, owner.id, "trial_start",
                        dict(_cvals, **{"المدة_المتبقية": "24 ساعة"}),
                    )
        else:
            _send_styled_owner(
                bc.user_chat_id, owner.id, "disconnected",
                _ev_values(owner.id, display_name(owner), owner.username or ""),
            )
            safe_send_message(
                bc.user_chat_id,
                f"{e('clock')} <b>لو رجّعت ربط البوت خلال ساعتين</b> كل إعداداتك وبياناتك هترجع زي ما هي. "
                "بعد الساعتين بتتمسح نهائي.",
            )
    except Exception:
        pass

def _disconnect_purge_loop():
    """بعد ساعتين من فصل البوت: مسح كل بيانات الأونر (لو مرجعش ربط)."""
    time.sleep(90)
    while True:
        try:
            now = int(time.time())
            conns = db.list_all_connections()
            enabled_owners = {c["owner_user_id"] for c in conns if c.get("is_enabled")}
            by_owner = {}
            for c in conns:
                if c.get("is_enabled"):
                    continue
                oid = c.get("owner_user_id")
                if oid is None or oid in enabled_owners or is_admin(oid):
                    continue
                by_owner.setdefault(oid, []).append(c)
            for oid, rows in by_owner.items():
                oldest = None
                for c in rows:
                    key = _bcoff_key(c["business_connection_id"])
                    raw = db.get_setting(key, "") or ""
                    if not raw:
                        db.set_setting(key, str(now))   # ابدأ العدّاد
                        raw = str(now)
                    try:
                        ts = int(raw)
                    except ValueError:
                        ts = now
                    oldest = ts if oldest is None else min(oldest, ts)
                if oldest is not None and now - oldest >= DISCONNECT_GRACE_SECONDS:
                    removed = db.purge_owner_data(oid)
                    for c in rows:
                        _BC_OWNER_CACHE.pop(c["business_connection_id"], None)
                    print(f"purged owner={oid} after disconnect grace: {removed}", flush=True)
                    _notify_admin(f"{e('trash')} <b>اتمسحت بيانات الأونر</b> <code>{oid}</code> بعد ساعتين من فصل البوت.")
        except Exception as exc:
            print(f"disconnect purge loop error: {exc}", flush=True)
        time.sleep(300)

_MSG_RETENTION_DAYS = float(os.environ.get("MSG_RETENTION_DAYS", "2"))
_MSG_KEEP_LAST = int(os.environ.get("MSG_KEEP_LAST", "5000") or 5000)

def _sweep_temp_dirs(max_age=900):
    """بيمسح أي ملفات تحميل مؤقتة أقدم من 15 دقيقة (بقايا تحميلات فشلت أو اتقطعت)."""
    now = time.time()
    dirs = ["downloads",
            os.path.join(tempfile.gettempdir(), "tiktok_downloads"),
            os.path.join(tempfile.gettempdir(), "universal_downloads")]
    for d in dirs:
        try:
            for name in os.listdir(d):
                path = os.path.join(d, name)
                try:
                    if os.path.isfile(path) and now - os.path.getmtime(path) > max_age:
                        os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass

def _maintenance_loop():
    """تنظيف دوري: الرسائل القديمة اللي ملهاش لازمة + ضغط الداتابيز عشان المساحة متكبرش."""
    time.sleep(120)
    last_vacuum = float(db.get_setting("last_vacuum", 0) or 0)
    while True:
        try:
            _sweep_temp_dirs()
            n = db.cleanup_messages_older_than(_MSG_RETENTION_DAYS)
            db.cleanup_old_messages(keep_last=_MSG_KEEP_LAST)
            for _cache in (_SUB_NEG_CACHE,):
                if len(_cache) > 5000:
                    _cache.clear()
            do_vacuum = time.time() - last_vacuum > 86400
            db.maintenance_compact(full_vacuum=do_vacuum)
            if do_vacuum:
                last_vacuum = time.time()
                db.set_setting("last_vacuum", int(last_vacuum))
            if n:
                print(f"maintenance: removed {n} old messages", flush=True)
        except Exception as exc:
            print(f"maintenance error: {exc}", flush=True)
        time.sleep(900)

_CHAT_KIND_AR = {"channel": "قناة", "supergroup": "جروب", "group": "جروب"}

@bot.my_chat_member_handler()
def handle_my_chat_member(upd):
    """إشعار للأدمن لما حد يضيف البوت رول/أدمن في قناة أو جروب (أو يشيله)."""
    try:
        chat = upd.chat
        if getattr(chat, "type", "") == "private":
            return
        new = upd.new_chat_member
        old = upd.old_chat_member
        new_status = getattr(new, "status", "")
        old_status = getattr(old, "status", "")
        actor = upd.from_user
        if actor is not None and is_admin(actor.id):
            return
        if new_status == old_status and new_status != "administrator":
            return
        if new_status == "administrator":
            title = "اتضاف رول أدمن للبوت"
            icon = "shield"
        elif new_status == "member":
            title = "البوت اتضاف"
            icon = "plus"
        elif new_status in ("left", "kicked"):
            title = "البوت اتشال"
            icon = "cross"
        else:
            return
        kind = _CHAT_KIND_AR.get(getattr(chat, "type", ""), "شات")
        cu = f"@{html_escape(chat.username)}" if getattr(chat, "username", None) else "خاص (من غير يوزر)"
        rights = ""
        if new_status == "administrator":
            _r = [k for k in ("can_post_messages", "can_edit_messages", "can_delete_messages",
                              "can_invite_users", "can_restrict_members", "can_manage_chat")
                  if getattr(new, k, False)]
            if _r:
                rights = f"\n{e('check')} <b>الصلاحيات:</b> <code>{html_escape(', '.join(_r))}</code>"
        text = (
            f"{e(icon)} <b>{title} في {kind}</b>\n\n"
            f"{e('megaphone')} <b>الاسم:</b> {html_escape(chat.title or '—')}\n"
            f"{e('link')} <b>اليوزر:</b> {cu}\n"
            f"{e('id_badge')} <b>الأيدي:</b> <code>{chat.id}</code>{rights}"
        )
        if actor is not None:
            text += f"\n\n<b>بواسطة:</b>\n{_who_block(actor)}"
        _notify_admin(text)
    except Exception as exc:
        print(f"my_chat_member handler failed: {exc}", flush=True)



# ---------------------------------------------------------------------------
# تحويل العملات: «حول 56» في أي شات -> اختيار العملة -> أسعار لحظية
# ---------------------------------------------------------------------------

def _currency_off_key(owner_id):
    return f"currency_off_{int(owner_id)}"

def currency_disabled_set(owner_id):
    raw = db.get_setting(_currency_off_key(owner_id), "") or ""
    return {c for c in raw.split(",") if c in cvt.CURRENCIES}

def set_currency_disabled(owner_id, codes):
    db.set_setting(_currency_off_key(owner_id), ",".join(c for c in cvt.CURRENCIES if c in codes))

def currency_enabled_list(owner_id):
    off = currency_disabled_set(owner_id)
    return [c for c in cvt.CURRENCIES if c not in off]

_CVT_BTN_NAME = {"USD": "دولار", "EGP": "جنيه", "TON": "TON"}
_CVT_BTN_ICON = {"USD": "usd", "EGP": "egp", "TON": "ton"}
_CVT_BTN_STYLE = {"USD": "success", "EGP": "danger", "TON": "primary"}

def _currency_picker_markup(owner_id, amount):
    token = cvt.amount_token(amount)
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        pe.btn(
            f" {_CVT_BTN_NAME.get(code, code)}", callback_data=f"cvt_p:{token}:{code}",
            icon=_CVT_BTN_ICON.get(code, "diamond"), style=_CVT_BTN_STYLE.get(code, "primary"),
        )
        for code in currency_enabled_list(owner_id)
    ]
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i + 3])
    markup.row(pe.btn(" إغلاق", callback_data="cvt_x", icon="trash", style="danger"))
    return markup

def _currency_result_markup(amount, src):
    token = cvt.amount_token(amount)
    markup = types.InlineKeyboardMarkup()
    markup.row(
        pe.btn(" تحديث", callback_data=f"cvt_r:{token}:{src}", icon="clock", style="success"),
        pe.btn(" عملة تانية", callback_data=f"cvt_c:{token}", icon="sparkles", style="primary"),
    )
    markup.row(pe.btn(" إغلاق", callback_data="cvt_x", icon="trash", style="danger"))
    return markup

STARS_QUICK_QTYS = (50, 100, 250, 500, 1000, 2500)
_STARS_QUICK_STYLES = ("primary", "success", "danger")


def _stars_picker_markup():
    """أزرار الكميات الجاهزة (لما العميل مكتبش كمية)."""
    markup = types.InlineKeyboardMarkup(row_width=3)
    buttons = [
        pe.btn(f" {q:,}", callback_data=f"stars_q:{q}", icon="star",
               style=_STARS_QUICK_STYLES[i % len(_STARS_QUICK_STYLES)])
        for i, q in enumerate(STARS_QUICK_QTYS)
    ]
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i + 3])
    return markup


def _stars_choice_markup(qty):
    """اختيار العملية: سعر السحب (أحمر) / سعر الشراء (أخضر) — من غير رجوع."""
    markup = types.InlineKeyboardMarkup()
    markup.row(
        pe.btn(" سعر السحب", callback_data=f"stars_w:{qty}", icon="sparkles", style="danger"),
        pe.btn(" سعر الشراء", callback_data=f"stars_b:{qty}", icon="cash", style="success"),
    )
    return markup


def _stars_price_markup(qty):
    """تحت كارت السعر: رابط Fragment + رجوع لاختيار العملية."""
    markup = types.InlineKeyboardMarkup()
    markup.row(
        pe.btn(" Fragment", url=frag.FRAGMENT_STARS_URL, icon="ton", style="primary"),
        pe.btn(" رجوع", callback_data=f"stars_q:{qty}", icon="arrow_left", style="danger"),
    )
    return markup


def _send_rich_page(chat_id, rich_html, fallback_html, markup=None, business_connection_id=None):
    """إرسال كارت الأسعار بقالب HTML قياسي واحد، لتطابق الخاص ومحادثات Business."""
    kwargs = {"reply_markup": markup} if markup is not None else {}
    if business_connection_id:
        return safe_send_business_message(chat_id, business_connection_id, fallback_html, **kwargs)
    return safe_send_message(chat_id, fallback_html, **kwargs)


def _edit_price_page(chat_id, message_id, fallback_html, markup, business_connection_id=None):
    """تحديث كارت السعر بنفس القالب القياسي في الخاص ومحادثات Business."""
    kwargs = {"reply_markup": markup}
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id
    safe_edit_message_text(fallback_html, chat_id, message_id, **kwargs)


def _stars_back_to_text(chat_id, message_id, text, markup, business_connection_id=None):
    """نرجّع الرسالة لنص عادي (من كارت الجدول)؛ لو التعديل اترفض نمسحها ونبعت جديدة."""
    kwargs = {"business_connection_id": business_connection_id} if business_connection_id else {}
    try:
        safe_edit_message_text(text, chat_id, message_id, reply_markup=markup, **kwargs)
    except Exception as exc:
        print(f"stars back edit failed, resending: {str(exc)[:200]}", flush=True)
        try:
            if business_connection_id:
                delete_business_message(business_connection_id, message_id)
            else:
                bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        if business_connection_id:
            safe_send_business_message(chat_id, business_connection_id, text, reply_markup=markup)
        else:
            safe_send_message(chat_id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(("stars_w:", "stars_b:", "stars_q:")))
def cb_stars_toggle(call):
    business_connection_id = getattr(call.message, "business_connection_id", None)
    try:
        mode_key, qty_s = call.data.split(":", 1)
        qty = int(qty_s)
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    chat_id, msg_id = call.message.chat.id, call.message.message_id

    def _worker():
        try:
            if mode_key == "stars_q":
                if qty:
                    _stars_back_to_text(chat_id, msg_id, frag.stars_choice_text(qty), _stars_choice_markup(qty),
                                         business_connection_id)
                else:
                    _stars_back_to_text(chat_id, msg_id, frag.stars_help_text(), _stars_picker_markup(),
                                         business_connection_id)
                return
            card = frag.stars_card(qty, "withdraw" if mode_key == "stars_w" else "buy")
            if not card:
                _edit_price_page(chat_id, msg_id, frag._FAIL, _stars_price_markup(qty), business_connection_id)
                return
            _edit_price_page(chat_id, msg_id, card[1], _stars_price_markup(qty), business_connection_id)
        except Exception as exc:
            print(f"Stars callback failed: {exc}", flush=True)

    threading.Thread(target=_worker, daemon=True, name="stars-toggle").start()


@bot.message_handler(func=lambda m: bool(
    getattr(m.chat, "type", "") == "private" and getattr(m, "content_type", "") == "text"
    and frag.parse_request(m.text)
))
def msg_private_fragment_price(message):
    request = frag.parse_request(message.text)
    if not request:
        return
    chat_id = message.chat.id
    if request.get("kind") == "premium":
        cards = frag.premium_cards()
        if not cards:
            safe_send_message(chat_id, frag._FAIL)
            return
        for rich, plain in cards:
            markup = types.InlineKeyboardMarkup()
            markup.row(pe.btn(" Fragment", url=frag.FRAGMENT_PREMIUM_URL, icon="ton", style="primary"))
            _send_rich_page(chat_id, rich, plain, markup)
        return
    qty = request.get("qty")
    if qty:
        safe_send_message(chat_id, frag.stars_choice_text(qty), reply_markup=_stars_choice_markup(qty))
    else:
        safe_send_message(chat_id, frag.stars_help_text(), reply_markup=_stars_picker_markup())

# ---- تحويل العملات في الخاص (شكل TonKit): «1تون» -> أزرار -> سطر النتيجة + رجوع ----

_PV_CVT_CODES = tuple(cvt.CURRENCIES)


def _pv_cvt_grid(buttons):
    markup = types.InlineKeyboardMarkup()
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i:i + 2])
    return markup


def _pv_cvt_source_markup(amount):
    token = cvt.amount_token(amount)
    return _pv_cvt_grid([
        pe.btn(cvt.short_name(c), callback_data=f"pvc_s:{token}:{c}",
               icon=_CVT_BTN_ICON.get(c, "diamond"), style="primary")
        for c in _PV_CVT_CODES
    ])


def _pv_cvt_target_markup(amount, src):
    token = cvt.amount_token(amount)
    return _pv_cvt_grid([
        pe.btn(cvt.short_name(c), callback_data=f"pvc_t:{token}:{src}:{c}",
               icon=_CVT_BTN_ICON.get(c, "diamond"), style="primary")
        for c in _PV_CVT_CODES if c != src
    ])


def _business_pv_cvt_source_markup(owner_id, amount):
    token = cvt.amount_token(amount)
    return _pv_cvt_grid([
        pe.btn(cvt.short_name(code), callback_data=f"pvc_s:{token}:{code}",
               icon=_CVT_BTN_ICON.get(code, "diamond"), style="primary")
        for code in currency_enabled_list(owner_id)
    ])


def _business_pv_cvt_target_markup(owner_id, amount, src):
    token = cvt.amount_token(amount)
    return _pv_cvt_grid([
        pe.btn(cvt.short_name(code), callback_data=f"pvc_t:{token}:{src}:{code}",
               icon=_CVT_BTN_ICON.get(code, "diamond"), style="primary")
        for code in currency_enabled_list(owner_id) if code != src
    ])


def _pv_cvt_result_markup(amount, src):
    token = cvt.amount_token(amount)
    markup = types.InlineKeyboardMarkup()
    markup.row(pe.btn("رجوع", callback_data=f"pvc_s:{token}:{src}", icon="arrow_left", style="danger"))
    return markup


@bot.message_handler(commands=["rates"], func=lambda m: (
    getattr(m.chat, "type", "") == "private" and m.from_user
    and m.from_user.id in (config.ADMIN_ID, config.DEVELOPER_ID)
))
def cmd_rates_check(message):
    """فحص الأسعار (للمدير بس): بيعرض كل مصدر والرقم اللي البوت بيستخدمه."""
    chat_id = message.chat.id

    def _try(fn, fmt):
        try:
            return fmt(fn())
        except Exception as exc:  # noqa: BLE001
            return f"فشل ({str(exc)[:40]})"

    def _worker():
        try:
            mode, override = cvt._egp_settings()
            rates = cvt.get_rates(["TON", "USD", "EGP"], force=True)
            ton_usd = rates.usd.get("TON")
            egp_per_usd = (1.0 / rates.usd["EGP"]) if rates.usd.get("EGP") else None
            lines = [
                f"{e('clock')} <b>فحص الأسعار</b>",
                f"وضع الجنيه: <code>{'ثابت ' + str(override) if override else mode}</code>",
                f"TON بالدولار: <code>{ton_usd:.4f}</code>" if ton_usd else "TON بالدولار: <code>غير متاح</code>",
                "",
                "<b>الدولار بالجنيه من كل مصدر:</b>",
                "• open.er-api: <code>" + _try(cvt._fiat_open_er, lambda d: f"{d['EGP']:.2f}") + "</code>",
                "• currency-api: <code>" + _try(cvt._fiat_fawaz, lambda d: f"{d['EGP']:.2f}") + "</code>",
                "• Binance P2P: <code>" + _try(
                    cvt._egp_binance_p2p,
                    lambda d: f"شراء {d['ask']:.2f} | بيع {d['bid']:.2f} | وسط {d['mid']:.2f}",
                ) + "</code>",
                "",
                f"المستخدم دلوقتي: <code>{egp_per_usd:.2f}</code> جنيه للدولار" if egp_per_usd else "المستخدم دلوقتي: <code>غير متاح</code>",
            ]
            if ton_usd and egp_per_usd:
                lines.append(f"1 TON = <code>{ton_usd * egp_per_usd:.2f}</code> جنيه")
            safe_send_message(chat_id, "\n".join(lines))
        except Exception as exc:
            print(f"/rates failed: {exc}", flush=True)

    threading.Thread(target=_worker, daemon=True, name="rates-check").start()


@bot.message_handler(func=lambda m: bool(
    getattr(m.chat, "type", "") == "private" and getattr(m, "content_type", "") == "text"
    and not frag.parse_request(m.text) and cvt.parse_request(m.text)
))
def msg_private_currency(message):
    request = cvt.parse_request(message.text)
    if not request:
        return
    amount, src = request["amount"], request.get("currency")
    if src in _PV_CVT_CODES:
        safe_send_message(message.chat.id, cvt.private_choice_text(amount, src),
                          reply_markup=_pv_cvt_target_markup(amount, src))
    else:
        safe_send_message(message.chat.id, cvt.private_source_text(amount),
                          reply_markup=_pv_cvt_source_markup(amount))


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(("pvc_s:", "pvc_t:")))
def cb_private_currency(call):
    bc_id = getattr(call.message, "business_connection_id", None)
    owner_id = None
    if bc_id:
        owner_id, validated_bc_id = _cvt_context(call)
        if not owner_id:
            return
        bc_id = validated_bc_id
    parts = call.data.split(":")
    kind = parts[0]
    amount = cvt.amount_from_token(parts[1]) if len(parts) > 1 else None
    src = parts[2] if len(parts) > 2 else None
    dst = parts[3] if len(parts) > 3 else None
    allowed_codes = currency_enabled_list(owner_id) if owner_id else list(_PV_CVT_CODES)
    if amount is None or src not in allowed_codes or (kind == "pvc_t" and dst not in allowed_codes):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    chat_id, msg_id = call.message.chat.id, call.message.message_id

    def _worker():
        try:
            if kind == "pvc_s":
                _stars_back_to_text(chat_id, msg_id, cvt.private_choice_text(amount, src),
                                    (_business_pv_cvt_target_markup(owner_id, amount, src)
                                     if owner_id else _pv_cvt_target_markup(amount, src)), bc_id)
                return
            rates = cvt.get_rates(list(_PV_CVT_CODES))
            text = cvt.private_result_text(amount, src, dst, rates) or frag._FAIL
            _stars_back_to_text(chat_id, msg_id, text, _pv_cvt_result_markup(amount, src), bc_id)
        except Exception as exc:
            print(f"Private currency callback failed: {exc}", flush=True)

    threading.Thread(target=_worker, daemon=True, name="pv-currency").start()


def _open_currency_converter(chat_id, bc_id, owner_id, request):
    """Entry point from a Business chat message ("حول 56" / "حول 56 دولار")."""
    amount = request["amount"]
    code = request.get("currency")
    enabled = currency_enabled_list(owner_id)
    if not enabled:
        safe_send_business_message(
            chat_id, bc_id,
            f"{e('cross')} <b>كل العملات مقفولة.</b> فعّل عملات من قسم «تحويل العملات» في لوحة التحكم.",
        )
        return

    if code and code in enabled:
        safe_send_business_message(
            chat_id, bc_id, cvt.private_choice_text(amount, code),
            reply_markup=_business_pv_cvt_target_markup(owner_id, amount, code),
        )
        return
    if code:
        safe_send_business_message(chat_id, bc_id, "العملة دي مقفولة حاليًا من إعدادات الحساب.")
        return
    safe_send_business_message(
        chat_id, bc_id, cvt.private_source_text(amount),
        reply_markup=_business_pv_cvt_source_markup(owner_id, amount),
    )

def _cvt_context(call):
    """Who pressed a converter button? Returns (owner_id, bc_id) or (None, None) after answering."""
    bc_id = getattr(call.message, "business_connection_id", None)
    connection = db.get_business_connection(bc_id) if bc_id else None
    if not connection:
        bot.answer_callback_query(call.id, "الاتصال بحساب الـ Business مش متاح.", show_alert=True)
        return None, None
    owner_id = connection["owner_user_id"]
    if not owner_feature_on("currency_convert", owner_id):
        bot.answer_callback_query(call.id, "ميزة تحويل العملات مقفولة.", show_alert=True)
        return None, None
    if call.from_user.id != owner_id and owner_feature_access("currency_convert", owner_id) != "public":
        bot.answer_callback_query(call.id, "الميزة دي لصاحب الحساب بس.", show_alert=True)
        return None, None
    return owner_id, bc_id

def _cvt_edit(call, bc_id, text, markup):
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    try:
        safe_edit_message_text(text, chat_id, msg_id, reply_markup=markup, business_connection_id=bc_id)
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            print(f"Currency edit failed: {exc}", flush=True)

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(("cvt_p:", "cvt_r:")))
def cb_currency_result(call):
    owner_id, bc_id = _cvt_context(call)
    if not owner_id:
        return
    try:
        _, token, src = call.data.split(":", 2)
    except ValueError:
        bot.answer_callback_query(call.id)
        return
    amount = cvt.amount_from_token(token)
    enabled = currency_enabled_list(owner_id)
    if amount is None or src not in enabled:
        bot.answer_callback_query(call.id, "العملة دي مقفولة أو البيانات غير صالحة.", show_alert=True)
        return
    force = call.data.startswith("cvt_r:")
    bot.answer_callback_query(call.id, "جاري تحديث الأسعار..." if force else None)

    def _worker():
        try:
            rates = cvt.get_rates(enabled, force=force)
            _cvt_edit(call, bc_id, cvt.result_text(amount, src, enabled, rates),
                      _currency_result_markup(amount, src))
        except Exception as exc:
            print(f"Currency result failed: {exc}", flush=True)

    threading.Thread(target=_worker, daemon=True, name="currency-result").start()

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("cvt_c:"))
def cb_currency_change(call):
    owner_id, bc_id = _cvt_context(call)
    if not owner_id:
        return
    amount = cvt.amount_from_token(call.data.split(":", 1)[1])
    if amount is None:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    _cvt_edit(call, bc_id, cvt.picker_text(amount), _currency_picker_markup(owner_id, amount))

@bot.callback_query_handler(func=lambda c: c.data == "cvt_x")
def cb_currency_close(call):
    owner_id, bc_id = _cvt_context(call)
    if not owner_id:
        return
    bot.answer_callback_query(call.id)
    delete_business_message(bc_id, call.message.message_id)

# ---- قسم التحويل في لوحة المالك: تفعيل/إيقاف كل عملة لوحدها ----

def _currency_menu_text(owner_id):
    info = CURRENCY_ACCESS_INFO[owner_feature_access("currency_convert", owner_id)]
    enabled = currency_enabled_list(owner_id)
    return (
        f"{e('diamond')} <b>قسم تحويل العملات</b>\n\n"
        "اكتب في أي شات <code>حول 56</code> واختار العملة، "
        "أو مباشرة <code>حول 56 دولار</code>.\n\n"
        f"الحالة: <b>{info['label']}</b> (اضغط الزر الأول لتغييرها)\n"
        f"العملات المفعّلة: <b>{len(enabled)}/{len(cvt.CURRENCIES)}</b>\n\n"
        "اضغط على أي عملة لتفعيلها أو إيقافها."
    )

def _currency_menu_markup(owner_id):
    info = CURRENCY_ACCESS_INFO[owner_feature_access("currency_convert", owner_id)]
    off = currency_disabled_set(owner_id)
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.row(pe.btn(f"الميزة: {info['label']}", callback_data="cvtm_acc", icon=info["icon"], style=info["style"]))
    buttons = []
    for code in cvt.CURRENCIES:
        is_off = code in off
        buttons.append(pe.btn(
            f" {_CVT_BTN_NAME.get(code, code)}", callback_data=f"cvtm_t_{code}",
            icon="cross" if is_off else _CVT_BTN_ICON.get(code, "check"),
            style="danger" if is_off else "success",
        ))
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i + 3])
    markup.row(
        pe.btn("تفعيل الكل", callback_data="cvtm_all_on", icon="check", style="success"),
        pe.btn("تعطيل الكل", callback_data="cvtm_all_off", icon="cross", style="danger"),
    )
    markup.row(_help_btn("currency"))
    markup.row(pe.btn(" رجوع", callback_data="cvtm_back", icon="arrow_left", style="danger"))
    return markup

def _currency_menu_refresh(call):
    owner_id = call.from_user.id
    safe_edit_message_text(
        _currency_menu_text(owner_id), call.message.chat.id, call.message.message_id,
        reply_markup=_currency_menu_markup(owner_id),
    )

@bot.callback_query_handler(func=lambda c: c.data == "cvtm_menu")
def cb_currency_menu(call):
    if not _require_active_connection(call):
        return
    bot.answer_callback_query(call.id)
    _currency_menu_refresh(call)

@bot.callback_query_handler(func=lambda c: c.data == "cvtm_acc")
def cb_currency_menu_access(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    current = owner_feature_access("currency_convert", owner_id)
    nxt = _next_access(current, CURRENCY_ACCESS_STATES)
    set_owner_feature_access("currency_convert", owner_id, nxt)
    bot.answer_callback_query(call.id, f"تم تغيير الحالة إلى: {CURRENCY_ACCESS_INFO[nxt]['label']}.")
    _currency_menu_refresh(call)

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("cvtm_t_"))
def cb_currency_menu_toggle(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    code = call.data[len("cvtm_t_"):]
    if code not in cvt.CURRENCIES:
        bot.answer_callback_query(call.id, "عملة غير معروفة.", show_alert=True)
        return
    off = currency_disabled_set(owner_id)
    if code in off:
        off.discard(code)
        msg = f"اتفعّلت {code}."
    else:
        off.add(code)
        msg = f"اتقفلت {code}."
    set_currency_disabled(owner_id, off)
    bot.answer_callback_query(call.id, msg)
    _currency_menu_refresh(call)

@bot.callback_query_handler(func=lambda c: c.data in ("cvtm_all_on", "cvtm_all_off"))
def cb_currency_menu_all(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    turn_on = call.data == "cvtm_all_on"
    set_currency_disabled(owner_id, set() if turn_on else set(cvt.CURRENCIES))
    bot.answer_callback_query(call.id, "اتفعّلت كل العملات." if turn_on else "اتقفلت كل العملات.")
    _currency_menu_refresh(call)

@bot.callback_query_handler(func=lambda c: c.data == "cvtm_back")
def cb_currency_menu_back(call):
    bot._owner_panel_context = call.from_user
    safe_edit_message_text(
        main_menu_text(call.from_user.id),
        call.message.chat.id, call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )
    bot.answer_callback_query(call.id)

OWNER_FEATURES_PAGE_SIZE = 12

def _owner_feature_keys(owner_id=None):
    # بس الميزات اللي تنفع المدير والعميل الاتنين. ميزات المدير فقط شغالة زي ما هي ومش بتظهر هنا.
    return [k for k in FEATURES if "all" in audience_options(k)]

def _owner_features_pages(owner_id=None):
    keys = _owner_feature_keys(owner_id)
    size = OWNER_FEATURES_PAGE_SIZE
    return [keys[i:i + size] for i in range(0, len(keys), size)]

FEATURE_SHORT_LABELS = {
    "delete_detect": "كشف المحذوف", "edit_detect": "كشف المعدّل", "force_sub": "الاشتراك الإجباري",
    "banned_words": "الكلمات الممنوعة",
    "scam_check": "فحص قنوات البلاغات", "profanity_filter": "منع الشتائم",
    "muted": "المكتومين", "blocked": "المحظورين", "pin_clear": "التثبيت والمسح",
    "auto_replies": "الردود التلقائية", "social_media": "السوشيال ميديا", "song_download": "تحميل الأغاني",
    "calculator": "الآلة الحاسبة", "broadcast": "إذاعة العملاء",
    "media": "إدارة الميديا", "anti_repeat": "منع التكرار",
    "id_command": "أمر الآيدي", "message_styles": "أشكال الرسائل",
    "xo_game": "لعبة XO", "wallet_lookup": "فحص المحافظ",
    "wallet_data": "بيانات المحفظة", "gift_price": "سعر الهدايا", "copy_message": "نسخ الرسائل",
    "currency_convert": "محول العملات", "binance_hub": "بينانس",
}

AUDIENCE_CYCLE = ["all", "owner", "closed"]

def _master_audience(owner_id):
    value = db.get_setting(f"aud_master_{int(owner_id)}", "owner")
    return value if value in AUDIENCE_CYCLE else "owner"

def _owner_features_text(page=0, owner_id=None):
    pages = _owner_features_pages(owner_id)
    total = len(pages)
    page = max(0, min(page, total - 1))
    return (
        f"{e('admin_panel')} <b>لوحة تحكم الميزات:</b>\n\n"
        f"{e('point_up')} اضغط على أي ميزة لتبديل صلاحيات عملها فورًا "
        "(يتم الحفظ تلقائيًا في نفس اللحظة).\n\n"
        "<b>معنى الحالات:</b>\n"
        "• <b>للعميل</b>: إنت والعميل الاتنين تقدروا تستخدموها.\n"
        "• <b>للمدير</b>: إنت بس (صاحب الحساب).\n"
        "• <b>مغلق</b>: الميزة موقوفة خالص.\n\n"
        "الميزات اللي هنا هي اللي تنفع العميل (الحاسبة، المحافظ، الهدايا، محول العملات، "
        "تحميل الأغاني، السوشيال ميديا، أمر الآيدي). "
        "باقي ميزات المدير شغالة زي ما هي ومش بتحتاج تحكم.\n\n"
        f"{e('book')} صفحة {page + 1} من {total}"
    )

def _feature_state(key, owner_id):
    if key == "calculator":
        return calc_access(owner_id)
    if key == "social_media":
        return social_access(owner_id)
    if key == "wallet_lookup":
        return wallet_access(owner_id)
    if key == "wallet_data":
        return wallet_data_access(owner_id)
    if key == "gift_price":
        return gift_price_access(owner_id)
    return owner_feature_access(key, owner_id, default="owner")

def _write_legacy_state(key, owner_id, state):
    if key == "calculator":
        set_calc_access(owner_id, state)
    elif key == "social_media":
        set_social_access(owner_id, state)
    elif key == "wallet_lookup":
        set_wallet_access(owner_id, state)
    elif key == "wallet_data":
        set_wallet_data_access(owner_id, state)
    elif key == "gift_price":
        set_gift_price_access(owner_id, state)
    else:
        set_owner_feature_access(key, owner_id, state)

def _set_feature_state(key, owner_id, enabled):
    _write_legacy_state(key, owner_id, "owner" if enabled else "disabled")

def _owner_feature_button_state(key, current):
    if key in ("wallet_lookup", "wallet_data"):
        return WALLET_ACCESS_INFO.get(current, WALLET_ACCESS_INFO["disabled"])
    if key == "gift_price":
        return GIFT_PRICE_ACCESS_INFO.get(current, GIFT_PRICE_ACCESS_INFO["disabled"])
    if key == "song_download":
        return SONG_ACCESS_INFO.get(current, SONG_ACCESS_INFO["disabled"])
    if key == "currency_convert":
        return CURRENCY_ACCESS_INFO.get(current, CURRENCY_ACCESS_INFO["disabled"])
    # باقي الميزات (كشف المحذوف/المعدّل، الاشتراك الإجباري، السوشيال ميديا...) تشغيل/إيقاف بس
    return ACCESS_INFO["disabled" if current == "disabled" else "owner"]

def owner_features_keyboard(owner_id, page=0):
    pages = _owner_features_pages(owner_id)
    total = len(pages)
    page = max(0, min(page, total - 1))
    kb = types.InlineKeyboardMarkup(row_width=2)

    feature_buttons = []
    for key in pages[page]:
        info = AUDIENCE_INFO[feature_audience(key, owner_id)]
        label = FEATURE_SHORT_LABELS.get(key, FEATURES[key]["label"])
        feature_buttons.append(pe.btn(
            f"{info['prefix']}{label} : {info['label']}",
            callback_data=f"ownerfeat_cycle_{page}_{key}", icon=info["icon"], style=info["style"],
        ))
    kb.add(*feature_buttons)

    nav = []
    if page > 0:
        nav.append(pe.btn(" السابق", callback_data=f"ownerfeat_page_{page - 1}", icon="arrow_left"))
    nav.append(pe.btn(f"{page + 1} / {total}", callback_data="ownerfeat_noop"))
    if page < total - 1:
        nav.append(pe.btn("التالي ", callback_data=f"ownerfeat_page_{page + 1}", icon="arrow_right"))
    kb.row(*nav)

    kb.row(
        pe.btn("الكل للعميل", callback_data=f"ownerfeat_setall_all_{page}", icon="aud_all", style="success"),
        pe.btn("الكل للمدير", callback_data=f"ownerfeat_setall_owner_{page}", icon="aud_owner", style="primary"),
        pe.btn("قفل الكل", callback_data=f"ownerfeat_setall_closed_{page}", icon="aud_closed", style="danger"),
    )
    kb.row(pe.btn("حالة الردود", callback_data=f"ownerfeat_status_{page}", icon="chat", style="primary"))
    master = AUDIENCE_INFO[_master_audience(owner_id)]
    kb.row(pe.btn(
        f"{master['prefix']}تفعيل الكل : {master['label']}",
        callback_data=f"ownerfeat_master_{page}", icon=master["icon"], style=master["style"],
    ))
    kb.add(pe.btn(" رجوع", callback_data="ownerfeat_back", icon="arrow_left"))
    return kb

@bot.callback_query_handler(func=lambda c: c.data == "owner_features_menu")
def cb_owner_features_menu(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    safe_edit_message_text(_owner_features_text(0, owner_id), call.message.chat.id, call.message.message_id,
                           reply_markup=owner_features_keyboard(owner_id, 0))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ownerfeat_page_"))
def cb_owner_features_page(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    try:
        page = int(call.data[len("ownerfeat_page_"):])
    except ValueError:
        page = 0
    safe_edit_message_text(_owner_features_text(page, owner_id), call.message.chat.id, call.message.message_id,
                           reply_markup=owner_features_keyboard(owner_id, page))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "ownerfeat_noop")
def cb_owner_features_noop(call):
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ownerfeat_cycle_"))
def cb_owner_feature_cycle(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id

    rest = call.data[len("ownerfeat_cycle_"):]
    page_str, _, key = rest.partition("_")
    try:
        page = int(page_str)
    except ValueError:
        page = 0
    if key not in FEATURES or key not in _owner_feature_keys(owner_id):
        bot.answer_callback_query(call.id, "غير معروف.", show_alert=True)
        return
    options = audience_options(key)
    current = feature_audience(key, owner_id)
    nxt = options[(options.index(current) + 1) % len(options)] if current in options else options[0]
    nxt = set_feature_audience(key, owner_id, nxt)
    label = FEATURE_SHORT_LABELS.get(key, FEATURES[key]["label"])
    bot.answer_callback_query(call.id, f"{label}: {AUDIENCE_INFO[nxt]['label']}")
    safe_edit_message_text(_owner_features_text(page, owner_id), call.message.chat.id, call.message.message_id,
                           reply_markup=owner_features_keyboard(owner_id, page))

MASTER_TOAST = {
    "all": "تم تفعيل جميع الميزات للعميل (وإنت معاه)",
    "owner": "تم تفعيل جميع الميزات للمدير فقط",
    "closed": "تم إغلاق جميع الميزات",
}

@bot.callback_query_handler(func=lambda c: c.data.startswith("ownerfeat_master_"))
def cb_owner_features_master(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    try:
        page = int(call.data[len("ownerfeat_master_"):])
    except ValueError:
        page = 0
    current = _master_audience(owner_id)
    nxt = AUDIENCE_CYCLE[(AUDIENCE_CYCLE.index(current) + 1) % len(AUDIENCE_CYCLE)]
    for key in _owner_feature_keys(owner_id):
        set_feature_audience(key, owner_id, nxt)
    db.set_setting(f"aud_master_{int(owner_id)}", nxt)
    bot.answer_callback_query(call.id, MASTER_TOAST[nxt])
    safe_edit_message_text(_owner_features_text(page, owner_id), call.message.chat.id, call.message.message_id,
                           reply_markup=owner_features_keyboard(owner_id, page))

SETALL_TOAST = {
    "all": "الكل للعميل: الميزات اللي تنفع العميل اتفتحت له (وإنت معاه)، والباقي للمدير بس",
    "owner": "كل الميزات للمدير فقط",
    "closed": "تم قفل كل الميزات",
}

@bot.callback_query_handler(func=lambda c: c.data.startswith("ownerfeat_setall_"))
def cb_owner_features_setall(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    rest = call.data[len("ownerfeat_setall_"):]
    mode, _, page_str = rest.partition("_")
    if mode not in SETALL_TOAST:
        bot.answer_callback_query(call.id, "غير معروف.", show_alert=True)
        return
    try:
        page = int(page_str)
    except ValueError:
        page = 0
    for key in _owner_feature_keys(owner_id):
        # set_feature_audience بترجّع «للمدير» تلقائي للميزات اللي ما تنفعش للعميل
        set_feature_audience(key, owner_id, mode)
    db.set_setting(f"aud_master_{int(owner_id)}", mode)
    bot.answer_callback_query(call.id, SETALL_TOAST[mode], show_alert=True)
    safe_edit_message_text(_owner_features_text(page, owner_id), call.message.chat.id, call.message.message_id,
                           reply_markup=owner_features_keyboard(owner_id, page))

@bot.callback_query_handler(func=lambda c: c.data.startswith("ownerfeat_status_"))
def cb_owner_features_status(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    counts = {k: 0 for k in AUDIENCE_CYCLE}
    for key in _owner_feature_keys(owner_id):
        counts[feature_audience(key, owner_id)] += 1
    text = " | ".join(f"{AUDIENCE_INFO[k]['label']}: {counts[k]}" for k in AUDIENCE_CYCLE)
    bot.answer_callback_query(call.id, f"حالة الميزات ({sum(counts.values())}):\n{text}", show_alert=True)

OWNER_FEATURES_DEFAULT_DISABLED = ()

@bot.callback_query_handler(func=lambda c: c.data.startswith("ownerfeat_reset_"))
def cb_owner_features_reset(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    try:
        page = int(call.data[len("ownerfeat_reset_"):])
    except ValueError:
        page = 0
    for key in _owner_feature_keys(owner_id):
        _set_feature_state(key, owner_id, key not in OWNER_FEATURES_DEFAULT_DISABLED)
    bot.answer_callback_query(call.id, "تمت إعادة الضبط إلى الإعدادات الافتراضية.")
    safe_edit_message_text(_owner_features_text(page, owner_id), call.message.chat.id, call.message.message_id,
                           reply_markup=owner_features_keyboard(owner_id, page))

@bot.callback_query_handler(func=lambda c: c.data == "ownerfeat_back")
def cb_owner_features_back(call):
    bot._owner_panel_context = call.from_user
    safe_edit_message_text(
        main_menu_text(call.from_user.id),
        call.message.chat.id, call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )
    bot.answer_callback_query(call.id)

# ================= لوحة «التحكم في الميزات»: كل ميزة بتتبدل بين للجميع ↔ للمدير فقط =================
# للجميع (أزرق) = أي حد في الشات يستخدمها | للمدير فقط (أحمر) = إنت بس | مغلق (رمادي) = موقوفة
CTL_STYLE = {"all": "primary", "owner": "danger", "closed": "default"}
CTL_LABEL = {"all": "للجميع", "owner": "للمدير فقط", "closed": "مغلق"}

def _ctl_feature_keys():
    return _owner_feature_keys()

def _ctl_text(owner_id):
    return (
        f"{e('features_mgmt')} <b>التحكم في الميزات</b>\n\n"
        "اضغط على أي ميزة عشان تبدّلها:\n"
        "🔵 <b>للجميع</b>: أي حد في الشات يقدر يستخدمها.\n"
        "🔴 <b>للمدير فقط</b>: إنت بس (صاحب الحساب) اللي تستخدمها.\n"
        "\n"
        "<i>التغيير بيتحفظ فورًا. الميزة المغلقة بتتفعّل «للجميع» أول ضغطة.</i>"
    )

def _ctl_keyboard(owner_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for key in _ctl_feature_keys():
        aud = feature_audience(key, owner_id)
        view = "all" if aud in ("all", "client") else aud
        label = FEATURE_SHORT_LABELS.get(key, FEATURES[key]["label"])
        buttons.append(pe.btn(
            f"{label} : {CTL_LABEL.get(view, view)}",
            callback_data=f"ctl_t_{key}",
            icon=AUDIENCE_INFO["all" if view == "all" else view]["icon"],
            style=CTL_STYLE.get(view, "default"),
        ))
    kb.add(*buttons)
    kb.row(
        pe.btn(" الكل للجميع", callback_data="ctl_all_all", icon="aud_all", style="primary"),
        pe.btn(" الكل للمدير", callback_data="ctl_all_owner", icon="aud_owner", style="danger"),
    )
    kb.row(pe.btn(" رجوع", callback_data="ownerfeat_back", icon="arrow_left"))
    return kb

def _ctl_refresh(call, page=0):
    owner_id = call.from_user.id
    try:
        safe_edit_message_text(_owner_features_text(page, owner_id), call.message.chat.id, call.message.message_id,
                               reply_markup=owner_features_keyboard(owner_id, page))
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            print(f"ctl refresh failed: {exc}", flush=True)

@bot.callback_query_handler(func=lambda c: c.data == "ctl_menu")
def cb_ctl_menu(call):
    if not _require_active_connection(call):
        return
    _ctl_refresh(call)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("ctl_t_"))
def cb_ctl_toggle(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    key = call.data[len("ctl_t_"):]
    if key not in _ctl_feature_keys():
        bot.answer_callback_query(call.id, "غير معروف.", show_alert=True)
        return
    current = feature_audience(key, owner_id)
    if key == "scam_check":
        nxt = "closed" if current == "owner" else "owner"
    else:
        nxt = "owner" if current in ("all", "client") else "all"
    set_feature_audience(key, owner_id, nxt)
    label = FEATURE_SHORT_LABELS.get(key, FEATURES[key]["label"])
    bot.answer_callback_query(call.id, f"{label}: {CTL_LABEL[nxt]}")
    _ctl_refresh(call)

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("ctl_all_"))
def cb_ctl_all(call):
    if not _require_active_connection(call):
        return
    owner_id = call.from_user.id
    target = "all" if call.data.endswith("_all") else "owner"
    for key in _ctl_feature_keys():
        set_feature_audience(key, owner_id, target)
    bot.answer_callback_query(call.id, "كل الميزات: " + CTL_LABEL[target])
    _ctl_refresh(call)

STYLE_VAR_HELP = {
    "الاسم": "اسم الشخص",
    "الايدي": "ايدي الشخص",
    "اليوزر": "يوزر الشخص",
    "تاريخ_الحساب": "تاريخ إنشاء الحساب (تقريبي)",
    "رابط_الحساب": "اسم الشخص مدمج برابط حسابه",
    "النص": "نص الرسالة الصوتية بعد التفريغ",
    "الترجمة": "نص الترجمة العربية",
    "النص_الأصلي": "النص الإنجليزي الأصلي",
    "الوقت": "وقت الحدث (تاريخ وساعة)",
    "الرسالة_المحذوفة": "نص الرسالة اللي اتحذفت",
    "آخر_رسالة": "آخر رسالة بعتها الشخص قبل الحذف",
    "قبل_التعديل": "نص الرسالة قبل التعديل",
    "بعد_التعديل": "نص الرسالة بعد التعديل",
    "الحالة": "حالة الحساب (أدمن / مدفوع / تجريبي...)",
    "المدة_المتبقية": "المدة المتبقية في الاشتراك",
}

_STYLE_VAR_ALIASES = {
    "الاسم": ("الاسم", "الإسم", "name"),
    "الايدي": ("الايدي", "الآيدي", "الأيدي", "ايدي", "id"),
    "اليوزر": ("اليوزر", "يوزر", "username"),
    "تاريخ_الحساب": ("تاريخ_الحساب", "تاريخ_الانشاء", "تاريخ_الإنشاء"),
    "رابط_الحساب": ("رابط_الحساب", "رابط"),
    "النص": ("النص",),
    "الترجمة": ("الترجمة", "الترجمه"),
    "النص_الأصلي": ("النص_الأصلي", "النص_الاصلي"),
    "الوقت": ("الوقت", "التوقيت", "time"),
    "الرسالة_المحذوفة": ("الرسالة_المحذوفة", "المحذوفة"),
    "آخر_رسالة": ("آخر_رسالة", "اخر_رسالة", "آخر_رسالة"),
    "قبل_التعديل": ("قبل_التعديل", "قبل"),
    "بعد_التعديل": ("بعد_التعديل", "بعد"),
    "الحالة": ("الحالة", "الحاله"),
    "المدة_المتبقية": ("المدة_المتبقية", "المده_المتبقيه", "المتبقي"),
}
_STYLE_ALIAS_TO_VAR = {
    alias.lower(): canonical
    for canonical, aliases in _STYLE_VAR_ALIASES.items()
    for alias in aliases
}
_STYLE_PLACEHOLDER_RE = re.compile(r"\{([^{}\s]{1,40})\}")

MESSAGE_STYLES = {
    "id": {
        "label": "رسالة الايدي (ID)",
        "icon": "id_badge",
        "default": (
            f"{e('id_badge')} <b>معلومات الحساب</b>\n\n"
            f"{e('person')} <b>الاسم:</b> {{الاسم}} \n"
            f"{e('id_badge')} <b>ID:</b> <code>{{الايدي}}</code>\n"
            f"{e('link')} <b>اليوزر:</b> {{اليوزر}} \n"
            f"{e('clock')} <b>تاريخ الإنشاء:</b> {{تاريخ_الحساب}} \n"
            f"{e('link')} <b>الحساب:</b> {{رابط_الحساب}}"
        ),
        "vars": ("الاسم", "الايدي", "اليوزر", "تاريخ_الحساب", "رابط_الحساب"),
        "required": (),
        "sample": {},
    },
    "deleted": {
        "label": "رسالة الحذف",
        "icon": "deleted_media",
        "default": (
            f"{e('deleted_media')} <b>تم حذف رسالة</b>\n\n"
            f"{e('person')} <b>الاسم:</b> <b>{{الاسم}}</b>\n"
            f"{e('link')} <b>اليوزر:</b> <b>{{اليوزر}}</b>\n"
            f"{e('person_2')} <b>آيدي:</b> <b><code>{{الايدي}}</code></b>\n\n"
            f"{e('chat')} <b>الرسالة المحذوفة:</b>\n<b>{{الرسالة_المحذوفة}}</b>\n\n"
            f"{e('chat_2')} <b>آخر رسالة بعتها:</b>\n<b>{{آخر_رسالة}}</b>"
        ),
        "vars": ("الرسالة_المحذوفة", "آخر_رسالة", "الاسم", "الايدي", "اليوزر", "رابط_الحساب", "الوقت"),
        "required": ("الرسالة_المحذوفة",),
        "sample": {"الرسالة_المحذوفة": "ده مثال لرسالة اتحذفت.", "آخر_رسالة": "ده مثال لآخر رسالة قبلها.",
                   "الوقت": "2026-10-02 14:15"},
    },
    "edited": {
        "label": "رسالة التعديل",
        "icon": "pencil",
        "default": (
            f"{e('pencil')} <b>تم تعديل رسالة</b>\n\n"
            f"{e('person')} <b>الاسم:</b> <b>{{الاسم}}</b>\n"
            f"{e('link')} <b>اليوزر:</b> <b>{{اليوزر}}</b>\n"
            f"{e('person_2')} <b>آيدي:</b> <b><code>{{الايدي}}</code></b>\n\n"
            f"{e('chat_3')} <b>قبل التعديل:</b>\n<b>{{قبل_التعديل}}</b>\n\n"
            f"{e('chat_4')} <b>بعد التعديل:</b>\n<b>{{بعد_التعديل}}</b>"
        ),
        "vars": ("قبل_التعديل", "بعد_التعديل", "الاسم", "الايدي", "اليوزر", "رابط_الحساب", "الوقت"),
        "required": ("بعد_التعديل",),
        "sample": {"قبل_التعديل": "النص القديم", "بعد_التعديل": "النص الجديد بعد التعديل",
                   "الوقت": "2026-10-02 14:15"},
    },
    "connected": {
        "label": "رسالة ربط الحساب",
        "icon": "check",
        "default": (
            "<b>تم ربط حساب الـ Business بتاعك بالبوت بنجاح.</b>\n"
            "<b>من دلوقتي أي رسالة تتمسح في محادثاتك هتوصلك هنا فورًا.</b>"
        ),
        "vars": ("الاسم", "الايدي", "اليوزر", "رابط_الحساب", "الوقت"),
        "required": (),
        "sample": {"الوقت": "2026-10-02 14:15"},
    },
    "disconnected": {
        "label": "رسالة فك الربط",
        "icon": "cross",
        "default": (
            "<b>تم إلغاء ربط حساب الـ Business بتاعك بالبوت.</b>\n"
            "<b>لوحة التحكم مش هتشتغل غير بعد ما تربطه تاني من /start.</b>"
        ),
        "vars": ("الاسم", "الايدي", "اليوزر", "رابط_الحساب", "الوقت"),
        "required": (),
        "sample": {"الوقت": "2026-10-02 14:15"},
    },
    "trial_start": {
        "label": "رسالة بداية التجربة",
        "icon": "clock",
        "default": "<b>ليك اشتراك 24 ساعة مجاني.</b>",
        "vars": ("الاسم", "الايدي", "اليوزر", "المدة_المتبقية"),
        "required": (),
        "sample": {"المدة_المتبقية": "24 ساعة"},
    },
    "main_menu": {
        "label": "رسالة القائمة الرئيسية",
        "icon": "wave_2",
        "default": (
            f"{e('ton_fire')} <b>مرحبًا بك في بوت إدارة الأعمال</b>\n\n"
            f"<blockquote>\u200f{e('person')}\u00a0\u00a0<b>حالة الحساب:</b> {{الحالة}} {e('party')}</blockquote>\n"
            f"<blockquote>\u200f{e('clock')}\u00a0\u00a0<b>المدة المتبقية:</b> {{المدة_المتبقية}}</blockquote>\n\n"
            "إدارة إعدادات حسابك بالكامل تتم من هنا:"
        ),
        "vars": ("الحالة", "المدة_المتبقية", "الاسم", "الايدي", "اليوزر"),
        "required": (),
        "sample": {"الحالة": "مدفوع", "المدة_المتبقية": "25 يوم"},
    },
}

_TG_ID_ANCHORS = [
    (2_768_409, (2013, 11)),
    (7_679_610, (2013, 12)),
    (11_538_514, (2014, 2)),
    (44_634_663, (2014, 5)),
    (54_845_238, (2014, 9)),
    (63_263_518, (2014, 10)),
    (101_260_938, (2015, 3)),
    (200_097_591, (2016, 5)),
    (400_169_472, (2017, 7)),
    (616_816_630, (2018, 6)),
    (925_078_064, (2019, 7)),
]
_TG_ID_MODERN_START = (5_065_327_552, (2021, 12))

def _tg_account_date_text(user_id):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return "غير معروف"
    if uid <= 0:
        return "غير معروف"
    anchors = _TG_ID_ANCHORS
    modern_id, (modern_y, modern_m) = _TG_ID_MODERN_START
    if uid <= anchors[0][0]:
        return "2013"
    if uid >= modern_id:
        return f"بعد {modern_m:02d}/{modern_y}"
    last_id, (last_y, _last_m) = anchors[-1]
    if uid > last_id:
        return f"{last_y}–{modern_y}"
    for (id1, (y1, m1)), (id2, (y2, m2)) in zip(anchors, anchors[1:]):
        if id1 <= uid <= id2:
            t1 = y1 * 12 + (m1 - 1)
            t2 = y2 * 12 + (m2 - 1)
            months = int(round(t1 + (uid - id1) / (id2 - id1) * (t2 - t1)))
            year, month0 = divmod(months, 12)
            return f"≈ {month0 + 1:02d}/{year}"
    return "غير معروف"

def _style_user_values(user):
    uid = getattr(user, "id", 0) or 0
    name = display_name(user)
    username = getattr(user, "username", "") or ""
    return {
        "الاسم": html_escape(name),
        "الايدي": str(uid),
        "اليوزر": f"@{html_escape(username)}" if username else "لا يوجد",
        "تاريخ_الحساب": html_escape(_tg_account_date_text(uid)),
        "رابط_الحساب": f'<a href="tg://user?id={uid}">{html_escape(name)}</a>',
    }

def _style_sample_values(user, key):
    values = _style_user_values(user)
    for var, sample in MESSAGE_STYLES[key]["sample"].items():
        values[var] = html_escape(sample)
    return values

def _fill_style(template, values):
    def _sub(match):
        canonical = _STYLE_ALIAS_TO_VAR.get(match.group(1).lower())
        if canonical and canonical in values:
            return values[canonical]
        return match.group(0)
    return _STYLE_PLACEHOLDER_RE.sub(_sub, template)

def _style_used_vars(template):
    used = set()
    for match in _STYLE_PLACEHOLDER_RE.finditer(template):
        canonical = _STYLE_ALIAS_TO_VAR.get(match.group(1).lower())
        if canonical:
            used.add(canonical)
    return used

def _style_key(owner_id, key):
    return f"msgstyle_{int(owner_id)}_{key}"

def _style_get(owner_id, key):
    return db.get_setting(_style_key(owner_id, key), "") or ""

def _style_set(owner_id, key, template):
    db.set_setting(_style_key(owner_id, key), template or "")

def _style_is_custom(owner_id, key):
    return False  # ميزة تخصيص أشكال الرسائل اتشالت: الشكل الافتراضي دايمًا

def render_message_style(owner_id, key, values, force_default=False):
    template = MESSAGE_STYLES[key]["default"]
    if not force_default and _style_is_custom(owner_id, key):
        template = _style_get(owner_id, key)
    return _fill_style(template, values)

def _send_styled_business(chat_id, bc_id, owner_id, key, values, **kwargs):
    sent = safe_send_business_message(
        chat_id, bc_id, render_message_style(owner_id, key, values), **kwargs
    )
    if sent is None and _style_is_custom(owner_id, key):
        print(f"Custom message style failed key={key} owner={owner_id}; using default", flush=True)
        sent = safe_send_business_message(
            chat_id, bc_id,
            render_message_style(owner_id, key, values, force_default=True),
            **kwargs,
        )
    return sent

def _send_styled_owner(chat_id, owner_id, key, values, **kwargs):
    try:
        return safe_send_message(chat_id, render_message_style(owner_id, key, values), **kwargs)
    except Exception as exc:
        if not _style_is_custom(owner_id, key):
            raise
        print(f"Custom message style failed key={key} owner={owner_id}: {exc}; using default", flush=True)
        return safe_send_message(
            chat_id, render_message_style(owner_id, key, values, force_default=True), **kwargs
        )

def _ev_values(user_id, name, username, **extra):
    uid = int(user_id or 0)
    name = name or "غير معروف"
    username = (username or "").lstrip("@")
    values = {
        "الاسم": html_escape(name),
        "الايدي": str(uid),
        "اليوزر": f"@{html_escape(username)}" if username else "لا يوجد",
        "تاريخ_الحساب": html_escape(_tg_account_date_text(uid)),
        "رابط_الحساب": f'<a href="tg://user?id={uid}">{html_escape(name)}</a>',
        "الوقت": time.strftime("%Y-%m-%d %H:%M"),
    }
    values.update(extra)
    return values

def msgstyle_menu_text():
    return (
        f"{e('pencil')} <b>تخصيص أشكال الرسائل</b>\n\n"
        "اختر الرسالة التي تريد تغيير شكلها وإضافة إيموجياتك المميزة لها:"
    )

def msgstyle_menu_markup(owner_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, spec in MESSAGE_STYLES.items():
        custom = bool(_style_get(owner_id, key))
        kb.add(pe.btn(
            f" {spec['label']}" + (" (مخصّصة)" if custom else ""),
            callback_data=f"msgstyle_edit_{key}",
            icon=spec["icon"],
            style="success" if custom else "primary",
        ))
    kb.add(pe.btn(" إعادة الكل للافتراضي", callback_data="msgstyle_reset_all", icon="trash", style="danger"))
    kb.add(pe.btn(" رجوع", callback_data="msgstyle_back", icon="arrow_left"))
    return kb

def msgstyle_edit_text(key):
    spec = MESSAGE_STYLES[key]
    lines = [
        f"{e('pencil')} <b>أرسل الآن شكل {spec['label']} الجديد.</b>",
        "",
        "<b>استخدم هذه المتغيرات:</b>",
    ]
    for var in spec["vars"]:
        lines.append(f"<code>{{{var}}}</code> = {STYLE_VAR_HELP[var]}")
    lines += [
        "",
        "تقدر تستخدم التنسيق (عريض، مائل، كود...) والإيموجيات المميزة.",
        "ابعت <code>الغاء</code> للتراجع.",
    ]
    return "\n".join(lines)

def msgstyle_edit_markup(key):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        pe.btn(" معاينة الحالي", callback_data=f"msgstyle_preview_{key}", icon="eye"),
        pe.btn(" للافتراضي", callback_data=f"msgstyle_reset_{key}", icon="trash", style="danger"),
    )
    kb.add(pe.btn(" رجوع", callback_data="msgstyle_menu", icon="arrow_left"))
    return kb

def _msgstyle_guard(call):
    if not _require_active_connection(call):
        return False
    bot.answer_callback_query(call.id, "قسم تخصيص أشكال الرسائل متوقف حاليًا.", show_alert=True)
    return False

def _edit_quiet(text, chat_id, message_id, **kwargs):
    try:
        safe_edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as exc:
        print(f"edit skipped: {exc}", flush=True)

@bot.callback_query_handler(func=lambda c: c.data == "msgstyle_menu")
def cb_msgstyle_menu(call):
    if not _msgstyle_guard(call):
        return
    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    _edit_quiet(
        msgstyle_menu_text(), call.message.chat.id, call.message.message_id,
        reply_markup=msgstyle_menu_markup(call.from_user.id),
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("msgstyle_edit_"))
def cb_msgstyle_edit(call):
    if not _msgstyle_guard(call):
        return
    key = call.data[len("msgstyle_edit_"):]
    if key not in MESSAGE_STYLES:
        bot.answer_callback_query(call.id, "رسالة غير معروفة.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    _edit_quiet(
        msgstyle_edit_text(key), chat_id, call.message.message_id,
        reply_markup=msgstyle_edit_markup(key),
    )
    bot.clear_step_handler_by_chat_id(chat_id)
    bot.register_next_step_handler_by_chat_id(chat_id, _msgstyle_receive, key)

def _msgstyle_send_preview(chat_id, user, key):
    text = render_message_style(user.id, key, _style_sample_values(user, key))
    return safe_send_message(chat_id, text)

@bot.callback_query_handler(func=lambda c: c.data.startswith("msgstyle_preview_"))
def cb_msgstyle_preview(call):
    if not _msgstyle_guard(call):
        return
    key = call.data[len("msgstyle_preview_"):]
    if key not in MESSAGE_STYLES:
        bot.answer_callback_query(call.id, "رسالة غير معروفة.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    try:
        preview = render_message_style(
            call.from_user.id, key, _style_sample_values(call.from_user, key)
        )
        safe_edit_message_text(
            preview, call.message.chat.id, call.message.message_id,
            reply_markup=msgstyle_edit_markup(key),
        )
    except Exception as exc:
        print(f"msgstyle preview failed: {exc}", flush=True)
        safe_edit_message_text(
            f"{e('cross')} <b>تعذّر عرض المعاينة.</b>",
            call.message.chat.id, call.message.message_id,
            reply_markup=msgstyle_edit_markup(key),
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith("msgstyle_reset_"))
def cb_msgstyle_reset(call):
    if not _msgstyle_guard(call):
        return
    owner_id = call.from_user.id
    target = call.data[len("msgstyle_reset_"):]
    if target == "all":
        for key in MESSAGE_STYLES:
            _style_set(owner_id, key, "")
        done = "تمت إعادة كل الرسائل للشكل الافتراضي."
    elif target in MESSAGE_STYLES:
        _style_set(owner_id, target, "")
        done = "تمت إعادة الرسالة للشكل الافتراضي."
    else:
        bot.answer_callback_query(call.id, "رسالة غير معروفة.", show_alert=True)
        return
    bot.answer_callback_query(call.id, done)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    _edit_quiet(
        msgstyle_menu_text(), call.message.chat.id, call.message.message_id,
        reply_markup=msgstyle_menu_markup(owner_id),
    )

@bot.callback_query_handler(func=lambda c: c.data == "msgstyle_back")
def cb_msgstyle_back(call):
    bot._owner_panel_context = call.from_user
    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    _edit_quiet(
        main_menu_text(call.from_user.id), call.message.chat.id, call.message.message_id,
        reply_markup=owner_panel_markup(call.from_user.id),
    )

def _msgstyle_receive(message, key):
    chat_id = message.chat.id
    owner_id = message.from_user.id
    spec = MESSAGE_STYLES.get(key)
    if not spec:
        return
    plain = (message.text or "").strip() if message.content_type == "text" else ""

    def _again(reason):
        safe_send_message(chat_id, f"{e('cross')} {reason}\nابعت الشكل تاني، أو ابعت <code>الغاء</code>.")
        bot.register_next_step_handler_by_chat_id(chat_id, _msgstyle_receive, key)

    def _back_to_menu(text):
        safe_send_message(chat_id, text, reply_markup=msgstyle_menu_markup(owner_id))

    if plain.lower() in ("الغاء", "إلغاء", "cancel") or plain.startswith("/"):
        _back_to_menu(f"{e('check')} <b>تم التراجع.</b> ما اتغيّرش أي شكل.")
        return
    if not plain:
        _again("لازم تبعت نص (مش صورة أو ملف).")
        return
    _back_to_menu(f"{e('cross')} <b>قسم تخصيص أشكال الرسائل متوقف حاليًا.</b>")
    return

    template = (message.html_text or plain).strip()
    if len(template) > 2500:
        _again("الشكل طويل زيادة (الحد الأقصى 2500 حرف).")
        return
    used = _style_used_vars(template)
    missing = [v for v in spec["required"] if v not in used]
    if missing:
        names = "، ".join(f"<code>{{{v}}}</code>" for v in missing)
        _again(f"لازم الشكل يحتوي على {names}.")
        return

    preview = _fill_style(template, _style_sample_values(message.from_user, key))
    plain_preview = re.sub(r"<[^>]+>", "", preview).strip()

    if plain_preview.lower() == "id" or _xo_is_trigger(plain_preview) or _calc_is_keyword(plain_preview):
        _again("الشكل ده نصه بيطابق أمر من أوامر البوت وممكن يعمل تكرار.")
        return
    try:
        safe_send_message(chat_id, preview)
    except Exception as exc:
        print(f"msgstyle template rejected owner={owner_id} key={key}: {exc}", flush=True)
        _again("الشكل ده تيليجرام مش قادر يعرضه (غالبًا تنسيق متداخل غلط، زي المتغير جوه كود).")
        return

    _style_set(owner_id, key, template)
    confirm_kb = msgstyle_edit_markup(key)
    safe_send_message(
        chat_id,
        f"{e('check')} <b>تم حفظ الشكل الجديد لـ {spec['label']}.</b>\n"
        "فوق معاينة بيانات حسابك؛ العملاء هيشوفوه بقيمهم هم.",
        reply_markup=confirm_kb,
    )

XO_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
_XO_TRIGGER_RAW = [
    "xo", "x o", "اكس او", "اكس أو", "إكس أو", "اكس اوه", "لعبة xo", "لعبة اكس او",
    "لعبة إكس أو", "العب xo", "العب اكس او",
]

def _xo_norm(text):
    text = _normalize_ar_text(text).lower().replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()

XO_TRIGGER_SET = {_xo_norm(w) for w in _XO_TRIGGER_RAW}

def _xo_is_trigger(text):
    return _xo_norm(text or "") in XO_TRIGGER_SET

def _xo_winner(board):
    for a, b, c in XO_LINES:
        if board[a] != "-" and board[a] == board[b] == board[c]:
            return board[a], (a, b, c)
    return None, None

def _xo_turn(board):
    return "X" if board.count("X") == board.count("O") else "O"

def _xo_valid_board(board):
    return len(board) == 9 and set(board) <= set("-XO")

def _xo_bot_move(board):
    empty = [i for i, c in enumerate(board) if c == "-"]
    if not empty:
        return None

    def _finishing_move(mark):
        for i in empty:
            trial = board[:i] + mark + board[i + 1:]
            if _xo_winner(trial)[0] == mark:
                return i
        return None

    for mark in ("O", "X"):
        move = _finishing_move(mark)
        if move is not None:
            return move
    if 4 in empty and random.random() < 0.8:
        return 4
    corners = [i for i in (0, 2, 6, 8) if i in empty]
    if corners and random.random() < 0.7:
        return random.choice(corners)
    return random.choice(empty)

def _chat_display_name(chat):
    name = " ".join(
        p for p in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if p
    ).strip()
    if name:
        return name
    if getattr(chat, "title", None):
        return chat.title
    if getattr(chat, "username", None):
        return f"@{chat.username}"
    return "العميل"

def _xo_text(board, name_x, name_o):
    winner, _line = _xo_winner(board)
    over = bool(winner) or "-" not in board
    head = (
        f"{e('party')} <b>لعبة إكس أو (XO)</b>\n\n"
        f"❌ <b>{html_escape(name_x)}</b>\n"
        f"⭕ <b>{html_escape(name_o)}</b>\n\n"
    )
    if winner:
        who, mark = (name_x, "❌") if winner == "X" else (name_o, "⭕")
        return head + f"🏆 <b>الفائز: {mark} {html_escape(who)}</b>"
    if over:
        return head + "🤝 <b>تعادل!</b>"
    turn = _xo_turn(board)
    who, mark = (name_x, "❌") if turn == "X" else (name_o, "⭕")
    return head + f"الدور على: {mark} <b>{html_escape(who)}</b>"

def _xo_keyboard(board, mode):
    winner, line = _xo_winner(board)
    if winner:

        return types.InlineKeyboardMarkup()
    over = "-" not in board
    kb = types.InlineKeyboardMarkup(row_width=3)
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            mark = board[i]
            if mark == "X":
                label, style = "❌", "danger"
            elif mark == "O":
                label, style = "⭕", "primary"
            else:
                label, style = "▫️", None
            if line and i in line:
                style = "success"
            data = f"xo|{mode}|{board}|{i}" if (mark == "-" and not over) else "xo|n"
            if style:
                row.append(pe.btn(label, callback_data=data, style=style))
            else:
                row.append(types.InlineKeyboardButton(label, callback_data=data))
        kb.row(*row)
    if over:
        kb.row(
            pe.btn(" لعبة جديدة", callback_data=f"xo|new|{mode}", icon="zap", style="success"),
            pe.btn(" إغلاق", callback_data="xo|x", icon="trash", style="danger"),
        )
    else:
        kb.row(pe.btn(" إغلاق", callback_data="xo|x", icon="trash", style="danger"))
    return kb

def _xo_edit(msg, bc_id, board, mode, name_x, name_o):
    text = _xo_text(board, name_x, name_o)
    kwargs = {"reply_markup": _xo_keyboard(board, mode)}
    if bc_id:
        kwargs["business_connection_id"] = bc_id
    try:
        bot.edit_message_text(text, msg.chat.id, msg.message_id, **kwargs)
    except Exception:
        try:
            bot.edit_message_text(
                pe.strip_premium(text), msg.chat.id, msg.message_id, **_strip_kwargs_icons(kwargs)
            )
        except Exception as exc:
            print(f"XO edit failed: {exc}", flush=True)

def _xo_start_business(chat_id, bc_id, name_x, name_o):
    board = "-" * 9
    safe_send_business_message(
        chat_id, bc_id, _xo_text(board, name_x, name_o),
        reply_markup=_xo_keyboard(board, "b"),
    )

_XO_APPROVAL_CACHE = {}

def _request_xo_approval(owner_chat_id, bc_id, chat_id, owner_name, client_name):
    token = uuid.uuid4().hex[:10]
    _XO_APPROVAL_CACHE[token] = {
        "bc_id": bc_id,
        "chat_id": chat_id,
        "owner_name": owner_name,
        "client_name": client_name,
    }
    text = (
        f"{e('party')} <b>العميل {html_escape(client_name)} عايز يلعب معاك XO.</b>\n\n"
        "عايز توافق؟"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(
        pe.btn(" قبول", callback_data=f"xoapp_yes_{token}", icon="thumbs_up", style="success"),
        pe.btn(" رفض", callback_data=f"xoapp_no_{token}", icon="cross", style="danger"),
    )
    safe_send_message(owner_chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("xoapp_yes_") or c.data.startswith("xoapp_no_"))
def cb_xo_approval(call):
    approve = call.data.startswith("xoapp_yes_")
    token = call.data.split("_", 2)[2]
    entry = _XO_APPROVAL_CACHE.pop(token, None)
    if not entry:
        bot.answer_callback_query(call.id, "الطلب ده خلص أو اتلغى قبل كده.", show_alert=True)
        return
    if not approve:
        bot.answer_callback_query(call.id, "تم الرفض.")
        try:
            bot.edit_message_text(
                f"{e('cross')} <b>تم رفض طلب لعبة XO.</b>", call.message.chat.id, call.message.message_id
            )
        except Exception:
            pass
        return
    bot.answer_callback_query(call.id, "تمت الموافقة!")
    try:
        bot.edit_message_text(
            f"{e('thumbs_up')} <b>تمت الموافقة، جاري بدء اللعبة...</b>",
            call.message.chat.id, call.message.message_id,
        )
    except Exception:
        pass
    _xo_start_business(entry["chat_id"], entry["bc_id"], entry["owner_name"], entry["client_name"])

def _xo_start_private(chat_id, user):
    board = "-" * 9
    safe_send_message(
        chat_id, _xo_text(board, display_name(user), "البوت"),
        reply_markup=_xo_keyboard(board, "p"),
    )

@bot.message_handler(commands=["xo"])
@bot.message_handler(
    func=lambda m: m.chat.type == "private" and m.content_type == "text" and _xo_is_trigger(m.text)
)
def private_xo_command(message):
    if not owner_feature_on("xo_game", message.from_user.id):
        safe_send_message(message.chat.id, f"{e('lock')} <b>لعبة XO متوقفة حاليًا.</b>")
        return
    _xo_start_private(message.chat.id, message.from_user)

@bot.callback_query_handler(func=lambda c: c.data == "xo_start_private")
def cb_xo_start_private(call):
    if not _require_active_connection(call):
        return
    if not owner_feature_on("xo_game", call.from_user.id):
        bot.answer_callback_query(call.id, "لعبة XO متوقفة حاليًا.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _xo_start_private(call.message.chat.id, call.from_user)

@bot.callback_query_handler(func=lambda c: c.data.startswith("xo|"))
def cb_xo(call):
    parts = call.data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    msg = call.message
    bc_id = getattr(msg, "business_connection_id", None)

    if action == "n":
        bot.answer_callback_query(call.id)
        return
    if action == "x":
        bot.answer_callback_query(call.id)
        try:
            if bc_id:
                delete_business_message(bc_id, msg.message_id)
            else:
                bot.delete_message(msg.chat.id, msg.message_id)
        except Exception:
            pass
        return

    mode = parts[2] if action == "new" and len(parts) > 2 else action
    if mode not in ("b", "p"):
        bot.answer_callback_query(call.id)
        return

    if mode == "b":
        connection = _ensure_business_connection(bc_id) if bc_id else None
        if not connection:
            bot.answer_callback_query(call.id, "تعذّر تحديد صاحب الحساب.", show_alert=True)
            return
        owner_id = connection["owner_user_id"]
        if not owner_feature_on("xo_game", owner_id):
            bot.answer_callback_query(call.id, "لعبة XO متوقفة حاليًا.", show_alert=True)
            return
        name_x = connection.get("owner_name") or "صاحب الحساب"
        name_o = _chat_display_name(msg.chat)
        my_mark = "X" if call.from_user.id == owner_id else "O"
    else:
        if not owner_feature_on("xo_game", call.from_user.id):
            bot.answer_callback_query(call.id, "لعبة XO متوقفة حاليًا.", show_alert=True)
            return
        name_x = display_name(call.from_user)
        name_o = "البوت"
        my_mark = "X"

    if action == "new":
        bot.answer_callback_query(call.id)
        _xo_edit(msg, bc_id, "-" * 9, mode, name_x, name_o)
        return

    try:
        board = parts[2]
        idx = int(parts[3])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id)
        return
    if not _xo_valid_board(board) or not 0 <= idx <= 8 or board[idx] != "-":
        bot.answer_callback_query(call.id, "الخانة دي مش متاحة.")
        return
    if _xo_winner(board)[0] or "-" not in board:
        bot.answer_callback_query(call.id)
        return

    turn = _xo_turn(board)
    if mode == "b" and my_mark != turn:
        waiting = name_x if turn == "X" else name_o
        bot.answer_callback_query(call.id, f"مش دورك، الدور على {waiting}.")
        return
    if mode == "p" and turn != "X":
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)
    board = board[:idx] + turn + board[idx + 1:]
    if mode == "p" and not _xo_winner(board)[0] and "-" in board:
        reply = _xo_bot_move(board)
        if reply is not None:
            board = board[:reply] + "O" + board[reply + 1:]
    _xo_edit(msg, bc_id, board, mode, name_x, name_o)

class _StoredReplyUser:
    __slots__ = ("id", "first_name", "last_name", "username")

    def __init__(self, sender_id, sender_name, sender_username):
        self.id = sender_id
        self.first_name = sender_name or "غير معروف"
        self.last_name = ""
        self.username = sender_username or ""

def _resolve_business_reply_target(message, bc_id):
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return None
    target = getattr(replied, "from_user", None)
    if target:
        return target
    stored = db.get_message(bc_id, message.chat.id, replied.message_id)
    if not stored or not stored.get("sender_id"):
        return None
    return _StoredReplyUser(
        stored["sender_id"], stored.get("sender_name"), stored.get("sender_username")
    )


@bot.business_message_handler(func=lambda m: True, content_types=telebot.util.content_type_media)
def handle_business_message(message):
    bc_id = message.business_connection_id
    sender = message.from_user
    print(
        f"Business message received bc={bc_id} chat={message.chat.id} sender={getattr(sender, 'id', None)} type={message.content_type}",
        flush=True,
    )
    text, ctype = content_of(message)
    media_file_id, media_kind = media_reference(message)

    db.save_message(
        bc_id=bc_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        sender_id=sender.id if sender else 0,
        sender_name=display_name(sender),
        sender_username=(sender.username if sender else "") or "",
        content=text,
        content_type=ctype,
        date=message.date,
        media_file_id=media_file_id,
        media_kind=media_kind,
    )

    connection = _ensure_business_connection(bc_id)
    if not sender:
        return

    if not connection and sender.id == config.ADMIN_ID:
        db.upsert_business_connection(
            bc_id=bc_id,
            owner_user_id=sender.id,
            owner_chat_id=sender.id,
            owner_name=display_name(sender),
            owner_username=(sender.username or ""),
            is_enabled=1,
            created_at=int(time.time()),
        )
        connection = db.get_business_connection(bc_id)
        print(f"Recovered missing Business connection bc={bc_id} owner={sender.id}", flush=True)
    if not connection:
        return

    owner_id = connection["owner_user_id"]

    if _is_recent_bot_business_text(bc_id, message.chat.id, message.text or ""):
        print(
            f"Ignored recent bot-sent Business message bc={bc_id} chat={message.chat.id}",
            flush=True,
        )
        return

    # بعد انتهاء الوصول تظل لوحة الخاص متاحة للإعداد، لكن لا تُنفذ أي ميزة
    # ولا تُرسل ردودًا داخل محادثات Business حتى يتجدد الاشتراك.
    if trial_expired(owner_id):
        print(f"Business automation paused for expired owner={owner_id}", flush=True)
        return

    client_scam_command = False
    if sender.id != owner_id:
        incoming_text = (message.text or "").strip() if message.content_type == "text" else ""
        client_scam_command = _parse_scam_check_command(incoming_text) is not None
        if not client_scam_command:
            _schedule_scam_watch_check(connection, sender)

    # ===== الكتم: أول حاجة. رسائل العميل المكتوم بتتمسح فورًا ومفيش أي ميزة بتشتغل له =====
    if sender.id != owner_id and owner_feature_on("muted", owner_id) and db.is_muted(owner_id, sender.id):
        deleted_ok, delete_error = delete_business_message_checked(bc_id, message.message_id)
        if not deleted_ok and delete_error_is_permission(delete_error):
            notify_mute_permission_missing(
                connection["owner_chat_id"], owner_id, display_name(sender), detail=delete_error
            )
        return

    # ===== الاشتراك الإجباري: بيتطبق على العملاء بس (مش على صاحب الحساب) وقبل أي ميزة =====
    # أي رسالة من عميل مش مشترك بتتمسح فورًا ويتبعتله «اشترك»، ومفيش أي أمر بيشتغل له.
    if (
        sender.id != owner_id
        and owner_feature_on("force_sub", owner_id)
        and force_sub_channels(owner_id)
        and not is_subscribed(sender.id, owner_id)
    ):
        print(f"Force subscription triggered owner={owner_id} client={sender.id}", flush=True)
        # نحفظ الرسالة عشان نشغّلها فورًا أول ما العميل يشترك (بدل ما يعيد كتابتها)
        if message.content_type == "text" and (message.text or "").strip():
            _FORCE_SUB_PENDING[(bc_id, message.chat.id, sender.id)] = (message, time.time())
        # المسح في خيط منفصل عشان رسالة «اشترك» تظهر فورًا من غير ما تستنى المسح
        threading.Thread(
            target=lambda: delete_business_message(bc_id, message.message_id), daemon=True
        ).start()
        _notice_key = (bc_id, message.chat.id)
        if time.time() - _FORCE_SUB_NOTICE_AT.get(_notice_key, 0) > 20:
            _FORCE_SUB_NOTICE_AT[_notice_key] = time.time()
            safe_send_business_message(
                message.chat.id,
                bc_id,
                f"{e('lock')} {force_sub_message(owner_id)}\n\n"
                "بعد الاشتراك اضغط «تحقق من الاشتراك» للمتابعة.",
                reply_markup=business_subscription_keyboard(owner_id),
        )
        return

    if client_scam_command:
        safe_send_business_message(
            message.chat.id, bc_id, "أمر فحص قنوات البلاغات متاح للمدير فقط."
        )
        return

    command_text = (
        (message.text or getattr(message, "caption", None) or "").strip()
        if message.content_type == "text" or getattr(message, "caption", None) else ""
    )
    scam_check_arg = _parse_scam_check_command(command_text) if command_text else None
    if scam_check_arg is not None:
        _handle_business_scam_check(
            message, connection, owner_id, bc_id, scam_check_arg
        )
        return

    _mute_action = _mute_cmd(command_text) if (sender.id == owner_id and command_text) else None
    if _mute_action and owner_feature_on("muted", owner_id):
        # الهدف: صاحب الرسالة اللي اتعمله reply، ولو مفيش reply يبقى العميل اللي في الشات نفسه
        target = _resolve_business_reply_target(message, bc_id)
        if (not target or target.id == owner_id) and getattr(message.chat, "type", "") == "private" \
                and message.chat.id != owner_id:
            target = _StoredReplyUser(
                message.chat.id,
                " ".join(x for x in (getattr(message.chat, "first_name", "") or "",
                                     getattr(message.chat, "last_name", "") or "") if x).strip(),
                getattr(message.chat, "username", "") or "",
            )
        if not target or target.id == owner_id:
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('cross')} <b>مقدرتش أحدد العميل.</b> اكتب «كتم» جوه شات العميل نفسه، أو كرد على رسالته.",
            )
            return
        target_name = display_name(target)
        target_username = target.username or ""
        username_text = f"@{html_escape(target_username)}" if target_username else "لا يوجد"
        profile_markup = types.InlineKeyboardMarkup()
        profile_markup.add(
            pe.btn("فتح بروفايل العميل", url=f"tg://user?id={target.id}", icon="person_3", style="primary")
        )
        if _mute_action == "unmute":
            db.unmute_user(owner_id, target.id)
            profile_markup.add(
                pe.btn("إلغاء كتم", callback_data=f"unmute_inline_{target.id}", icon="unlock", style="success")
            )
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('unlock')} <b>تم إلغاء كتم العميل</b>\n\n"
                f"{e('person')} <b>الاسم:</b> {html_escape(target_name)}\n"
                f"{e('link')} <b>اليوزر:</b> {username_text}\n"
                f"<b>ID:</b> <code>{target.id}</code>",
                reply_markup=profile_markup,
            )
        else:
            db.mute_user(owner_id, target.id, target_name, target_username)
            profile_markup.add(
                pe.btn("إلغاء كتم", callback_data=f"unmute_inline_{target.id}", icon="unlock", style="success")
            )
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('mute')} <b>تم كتم العميل بنجاح</b>\n\n"
                f"{e('person')} <b>الاسم:</b> {html_escape(target_name)}\n"
                f"{e('link')} <b>اليوزر:</b> {username_text}\n"
                f"<b>ID:</b> <code>{target.id}</code>\n"
                f"<b>الحالة:</b> سيتم حذف رسائله الجديدة ولن تصله ردود تلقائية.",
                reply_markup=profile_markup,
            )

            if business_delete_rights_missing(bc_id):
                notify_mute_permission_missing(
                    connection["owner_chat_id"], owner_id, target_name, force=True
                )
        return

    _ex_action = _exempt_cmd(command_text) if (sender.id == owner_id and command_text) else None
    if _ex_action and owner_feature_on("force_sub", owner_id) and force_sub_channels(owner_id):
        # الهدف: صاحب الرسالة اللي اتعمله reply، ولو مفيش reply يبقى العميل اللي في الشات نفسه
        target = _resolve_business_reply_target(message, bc_id)
        if (not target or target.id == owner_id) and getattr(message.chat, "type", "") == "private" \
                and message.chat.id != owner_id:
            target = _StoredReplyUser(
                message.chat.id,
                " ".join(x for x in (getattr(message.chat, "first_name", "") or "",
                                     getattr(message.chat, "last_name", "") or "") if x).strip(),
                getattr(message.chat, "username", "") or "",
            )
        if not target or target.id == owner_id:
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('cross')} <b>مقدرتش أحدد العميل.</b> اكتب «استثناء» جوه شات العميل نفسه، أو كرد على رسالته.",
            )
            return
        target_name = display_name(target)
        if _ex_action == "unexempt":
            db.remove_force_sub_exempt(owner_id, target.id)
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('lock')} <b>اتلغى استثناء {html_escape(target_name)}</b> من الاشتراك الإجباري.",
            )
        else:
            db.add_force_sub_exempt(owner_id, target.id, target_name, target.username or "")
            ex_markup = types.InlineKeyboardMarkup()
            ex_markup.add(pe.btn("إلغاء الاستثناء", callback_data=f"unexempt_inline_{target.id}",
                                 icon="lock", style="danger"))
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('check')} <b>تم استثناء {html_escape(target_name)}</b> من الاشتراك الإجباري، يتكلم عادي.",
                reply_markup=ex_markup,
            )
        return

    if (
        owner_feature_on("pin_clear", owner_id)
        and sender.id == owner_id
        and command_text in PIN_TRIGGER_WORDS + UNPIN_TRIGGER_WORDS
    ):
        replied = getattr(message, "reply_to_message", None)
        if not replied:
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('cross')} <b>اكتب الأمر كرد على الرسالة اللي عايز تثبتها/تشيلها.</b>",
            )
            return
        target_user = getattr(replied, "from_user", None)
        target_name = html_escape(display_name(target_user)) if target_user else "—"
        chat_markup = types.InlineKeyboardMarkup()
        if target_user:
            chat_markup.add(pe.btn("فتح بروفايل العميل", url=f"tg://user?id={target_user.id}", icon="person_3", style="primary"))
        if command_text in PIN_TRIGGER_WORDS:
            ok = pin_business_message(message.chat.id, bc_id, replied.message_id)
            if ok:
                token = uuid.uuid4().hex[:12]
                PIN_UNDO_CACHE[token] = (bc_id, message.chat.id, replied.message_id)
                chat_markup.add(pe.btn("إلغاء التثبيت", callback_data=f"unpin_inline_{token}", icon="unlock"))
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('star')} <b>تم تثبيت الرسالة بنجاح</b>\n\n"
                    f"{e('person')} <b>من:</b> {target_name}\n"
                    f"{e('chat')} <b>الرسالة:</b>\n{content_of(replied)[0] or '—'}",
                    reply_markup=chat_markup,
                )
            else:
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('cross')} <b>مقدرتش أثبت الرسالة.</b>\n"
                    "تأكد إن صلاحية «تثبيت الرسائل» مفعّلة للبوت من "
                    "Settings ← Telegram Business ← Chatbots ← صلاحيات البوت.",
                )
        else:
            ok = unpin_business_message(message.chat.id, bc_id, replied.message_id)
            if ok:
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('unlock')} <b>تم إلغاء تثبيت الرسالة.</b>",
                    reply_markup=chat_markup,
                )
            else:
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('cross')} <b>مقدرتش ألغي التثبيت.</b> تأكد من صلاحيات البوت.",
                )
        return

    if owner_feature_on("pin_clear", owner_id) and sender.id == owner_id and command_text:
        clear_n_match = re.match(r"^مسح\s+([0-9٠-٩]{1,4})$", command_text)
        if clear_n_match:
            n = min(int(clear_n_match.group(1).translate(_AR_DIGITS_MAP)), 2000)
            ids = db.get_recent_message_ids(bc_id, message.chat.id, n, exclude_message_id=message.message_id)
            label = f"آخر {n} رسالة"
            if not ids:
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('cross')} <b>مفيش رسائل مخزّنة في الشات ده أقدر أمسحها.</b>",
                )
                return
            deleted_count, had_error, last_error = delete_business_messages_bulk(bc_id, ids)
            if had_error and deleted_count == 0:
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('cross')} <b>مقدرتش أمسح الرسائل.</b>\n"
                    "الغالب إن صلاحية «حذف الرسائل» مش مفعّلة للبوت من "
                    "Settings ← Telegram Business ← Chatbots ← صلاحيات البوت.\n"
                    f"<i>تفاصيل الخطأ: {html_escape(last_error)}</i>",
                )
                return
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('trash')} <b>تم مسح {label}</b>\n\n"
                f"{e('check')} <b>عدد الرسائل:</b> <code>{deleted_count}</code>\n"
                "<i>ملحوظة: تيليجرام بيمنع حذف رسائل Business أقدم من 48 ساعة، "
                "فأي رسالة أقدم من كده ممكن تفضل زي ما هي.</i>",
            )
            return

    if (
        owner_feature_on("copy_message", owner_id)
        and sender.id == owner_id
        and command_text in COPY_TRIGGER_WORDS
    ):
        replied = getattr(message, "reply_to_message", None)
        if not replied:
            safe_send_message(
                connection["owner_chat_id"],
                f"{e('cross')} <b>اكتب «نسخ» كرد على الرسالة اللي عايز نسخة منها.</b>",
            )
            return
        _send_message_copy(connection["owner_chat_id"], replied)
        return

    if (
        owner_feature_on("id_command", owner_id) and command_text and command_text.strip().lower() == "id"
        and audience_allows("id_command", owner_id, sender.id)
    ):
        # المدير: بيانات حسابه، أو صاحب الرسالة لو كتبها كرد. العميل: بياناته هو بس دايمًا.
        if sender.id == owner_id:
            id_target = _resolve_business_reply_target(message, bc_id) or sender
        else:
            id_target = sender
        _send_styled_business(message.chat.id, bc_id, owner_id, "id", _style_user_values(id_target))
        return

    if owner_feature_on("xo_game", owner_id) and command_text and _xo_is_trigger(command_text):
        owner_name = connection.get("owner_name") or "صاحب الحساب"
        client_name = _chat_display_name(message.chat)
        if sender.id == owner_id:
            _xo_start_business(message.chat.id, bc_id, owner_name, client_name)
        else:
            _request_xo_approval(
                connection["owner_chat_id"], bc_id, message.chat.id, owner_name, client_name
            )
        return

    if sender.id == owner_id and command_text and owner_feature_on("binance_hub", owner_id):
        try:
            if _bn.handle_business_text(message, owner_id, bc_id, connection["owner_chat_id"]):
                return
        except Exception as exc:
            print(f"Binance business command failed: {exc}", flush=True)

    # نفس أسعار Stars/Premium التي تظهر في الخاص، مع إرسالها داخل Business chat.
    fragment_request = frag.parse_request(command_text) if command_text else None
    if fragment_request:
        if fragment_request.get("kind") == "premium":
            cards = frag.premium_cards()
            if not cards:
                safe_send_business_message(message.chat.id, bc_id, frag._FAIL)
            else:
                for _rich_html, fallback_html in cards:
                    markup = types.InlineKeyboardMarkup()
                    markup.row(pe.btn(" Fragment", url=frag.FRAGMENT_PREMIUM_URL,
                                      icon="ton", style="primary"))
                    safe_send_business_message(message.chat.id, bc_id, fallback_html,
                                               reply_markup=markup)
        else:
            qty = fragment_request.get("qty")
            if qty:
                safe_send_business_message(message.chat.id, bc_id, frag.stars_choice_text(qty),
                                           reply_markup=_stars_choice_markup(qty))
            else:
                safe_send_business_message(message.chat.id, bc_id, frag.stars_help_text(),
                                           reply_markup=_stars_picker_markup())
        return

    if owner_feature_on("currency_convert", owner_id) and command_text:
        cvt_request = cvt.parse_request(command_text)
        if cvt_request:
            cvt_allowed = audience_allows("currency_convert", owner_id, sender.id)
            if cvt_allowed:
                _open_currency_converter(message.chat.id, bc_id, owner_id, cvt_request)
                return

    if feature_enabled("calculator") and calc_access(owner_id) != "disabled" and command_text:
        calc_is_owner_sender = sender.id == owner_id
        calc_allowed = audience_allows("calculator", owner_id, sender.id)
        if calc_allowed and (_calc_is_expression(command_text) or _calc_is_keyword(command_text)):
            initial = command_text if _calc_is_expression(command_text) else ""
            _open_calculator(message, bc_id, initial_expr=initial)
            return

    if feature_enabled("wallet_data") and wallet_data_access(owner_id) != "disabled" and command_text:
        wdata_allowed = audience_allows("wallet_data", owner_id, sender.id)
        if wdata_allowed and wallet_xray.is_address(command_text):
            _run_wallet_scan(message.chat.id, bc_id, command_text, reply_to=message.message_id)
            return

    if feature_enabled("wallet_lookup") and wallet_access(owner_id) != "disabled" and command_text:
        wallet_is_owner_sender = sender.id == owner_id
        wallet_allowed = audience_allows("wallet_lookup", owner_id, sender.id)
        if wallet_allowed and (
            wallet_lookup.is_probable_username(command_text) or wallet_lookup.is_wallet_address(command_text)
        ):
            _run_wallet_scan(message.chat.id, bc_id, command_text, reply_to=message.message_id)
            return

    if feature_enabled("gift_price") and gift_price_access(owner_id) != "disabled" and command_text:
        parsed_gift_link = gift_price.parse_gift_link(command_text)
        if parsed_gift_link:
            gift_price_is_owner_sender = sender.id == owner_id
            gift_price_allowed = audience_allows("gift_price", owner_id, sender.id)
            if gift_price_allowed:
                _run_gift_price_lookup(message.chat.id, bc_id, command_text)
                return

    is_owner_sender = sender.id == owner_id
    real_social_access = social_access(owner_id)
    access = "owner" if is_owner_sender else real_social_access
    social_media_on = feature_enabled("social_media") and real_social_access != "disabled"
    is_youtube_cmd = command_text == YOUTUBE_TRIGGER_PREFIX or command_text.startswith(
        YOUTUBE_TRIGGER_PREFIX + " "
    )
    if is_youtube_cmd:
        yt_ok, target_chat_id, send_bc_id = social_p_route_notify(
            owner_id, "youtube", social_media_on, sender, is_owner_sender,
            connection["owner_chat_id"], message.chat.id, bc_id,
        )
        if yt_ok:
            query = command_text[len(YOUTUBE_TRIGGER_PREFIX):].strip()
            if not query:
                if is_owner_sender:
                    _social_send(
                        target_chat_id, send_bc_id,
                        f"{e('cross')} <b>اكتب اسم الأغنية بعد «يوت»، مثلاً:</b> يوت اسم الأغنية",
                    )
                return
            if not social_p_client_gate(owner_id, "youtube", sender, is_owner_sender,
                                        connection["owner_chat_id"], message.chat.id, bc_id):
                return
            _social_send(
                target_chat_id, send_bc_id,
                f"{e('music')} <b>جاري البحث عن:</b> {html_escape(query)}",
            )
            threading.Thread(
                target=_youtube_search_and_offer,
                args=(target_chat_id, send_bc_id, query),
                kwargs={"kinds": social_p_types(owner_id, "youtube")},
                daemon=True,
            ).start()
            return

    if command_text:
        parsed_tiktok = _tiktok_parse_business(command_text)
        if parsed_tiktok:
            mode, tiktok_url = parsed_tiktok
            owner_chat_id = connection["owner_chat_id"]
            tt_ok, target_chat_id, send_bc_id = social_p_route_notify(
                owner_id, "tiktok", social_media_on, sender, is_owner_sender,
                owner_chat_id, message.chat.id, bc_id,
            )
            if tt_ok:
                if not social_p_client_gate(owner_id, "tiktok", sender, is_owner_sender,
                                            owner_chat_id, message.chat.id, bc_id):
                    return
                mode = social_apply_types(social_p_types(owner_id, "tiktok"), mode)
                icon = "music" if mode == "audio" else "download"
                if is_owner_sender:
                    status = safe_send_message(owner_chat_id, f"{e(icon)} <b>جاري تحميل رابط تيك توك...</b>")
                    status_chat_id = owner_chat_id
                else:
                    status = _social_send(target_chat_id, send_bc_id, f"{e(icon)} <b>جاري تحميل رابط تيك توك...</b>")
                    status_chat_id = target_chat_id
                threading.Thread(
                    target=_do_tiktok_download_and_send,
                    args=(
                        owner_chat_id, target_chat_id, send_bc_id, mode, tiktok_url,
                        status_chat_id, status.message_id if status else None,
                    ),
                    kwargs={"caption_on": social_p_caption(owner_id, "tiktok")},
                    daemon=True,
                    name="tiktok-business-download",
                ).start()
                return

    if command_text:
        parsed_ud = ud.parse_request(_message_text_with_link_entities(message), _replied_text(message))
        if parsed_ud:
            mode, ud_url, _explicit = parsed_ud
            owner_chat_id = connection["owner_chat_id"]
            pkey = social_platform_key(ud_url)
            ud_ok, target_chat_id, send_bc_id = social_p_route_notify(
                owner_id, pkey, social_media_on, sender, is_owner_sender,
                owner_chat_id, message.chat.id, bc_id,
            )
            if ud_ok:
                if not social_p_client_gate(owner_id, pkey, sender, is_owner_sender,
                                            owner_chat_id, message.chat.id, bc_id):
                    return
                mode = social_apply_types(social_p_types(owner_id, pkey), mode)
                icon = "music" if mode == "audio" else "download"
                if is_owner_sender:
                    status = safe_send_message(owner_chat_id, f"{e(icon)} <b>جاري تحميل الرابط...</b>")
                    status_chat_id = owner_chat_id
                else:
                    status = _social_send(target_chat_id, send_bc_id, f"{e(icon)} <b>جاري تحميل الرابط...</b>")
                    status_chat_id = target_chat_id
                threading.Thread(
                    target=_do_universal_download_and_send,
                    args=(
                        owner_chat_id, target_chat_id, send_bc_id, mode, ud_url,
                        status_chat_id, status.message_id if status else None,
                    ),
                    kwargs={"caption_on": social_p_caption(owner_id, pkey),
                            "quality": social_p_quality_int(owner_id, pkey)},
                    daemon=True,
                    name="universal-business-download",
                ).start()
                return

    song_state = song_access(owner_id) if feature_enabled("song_download") else "disabled"
    if command_text and song_state != "disabled" and audience_allows("song_download", owner_id, sender.id):
        song_query = ud.parse_song_request(command_text)
        if song_query:
            owner_chat_id = connection["owner_chat_id"]
            if song_state == "private":
                target_chat_id, send_bc_id = owner_chat_id, None
            else:
                target_chat_id, send_bc_id = message.chat.id, bc_id
            _social_send(
                target_chat_id,
                send_bc_id,
                f"{e('music')} <b>جاري البحث عن:</b> {html_escape(song_query)}",
            )
            threading.Thread(
                target=_youtube_search_and_offer,
                args=(target_chat_id, send_bc_id, song_query),
                kwargs={"kinds": "a"},
                daemon=True, name="song-search",
            ).start()
            return

    if sender.id == owner_id:
        if (
            owner_feature_on("auto_replies", owner_id)
            and message.content_type == "text"
            and not getattr(message, "sender_business_bot", None)
        ):
            try_auto_reply(owner_id, bc_id, message, variables_user=message.chat, sender_is_owner=True)
        return

    db.upsert_client(
        owner_user_id=owner_id,
        chat_id=message.chat.id,
        bc_id=bc_id,
        client_user_id=sender.id,
        client_name=display_name(sender),
        client_username=sender.username or "",
        last_message_at=message.date,
    )

    blocked_key = _blocked_media_key(message, owner_id)
    if blocked_key:
        print(
            f"Media blocked owner={owner_id} chat={message.chat.id} type={blocked_key}",
            flush=True,
        )
        try:
            delete_business_message(bc_id, message.message_id)
        except Exception:
            pass
        return

    if owner_feature_on("banned_words", owner_id) and message.content_type == "text" and message.text:
        banned = db.find_banned_word_in_text(owner_id, message.text)
        if banned:
            try:
                delete_business_message(bc_id, message.message_id)
            except Exception:
                pass
            try:
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('cross')} <b>تم حذف رسالة من عميل فيها كلمة ممنوعة</b> "
                    f"(<code>{html_escape(banned['word'])}</code>)\n"
                    f"{e('person')} <b>الاسم:</b> {html_escape(display_name(sender))}",
                )
            except Exception:
                pass
            custom_reply = db.get_banned_reply(owner_id)
            if custom_reply:
                try:
                    bot.send_message(message.chat.id, custom_reply, business_connection_id=bc_id)
                except Exception:
                    try:
                        bot.send_message(
                            message.chat.id, pe.strip_premium(custom_reply), business_connection_id=bc_id
                        )
                    except Exception:
                        pass
            return

    if owner_feature_on("profanity_filter", owner_id) and message.content_type == "text" and message.text:
        bad_word = _find_profanity_word(message.text)
        if bad_word:
            try:
                delete_business_message(bc_id, message.message_id)
            except Exception:
                pass
            try:
                safe_send_message(
                    connection["owner_chat_id"],
                    f"{e('mute')} <b>تم حذف رسالة من عميل فيها سب/شتيمة</b>\n"
                    f"{e('person')} <b>الاسم:</b> {html_escape(display_name(sender))}",
                )
            except Exception:
                pass
            try:
                bot.send_message(message.chat.id, PROFANITY_REPLY_TEXT, business_connection_id=bc_id)
            except Exception:
                pass
            return

    if (
        owner_feature_on("anti_repeat", owner_id)
        and message.content_type == "text"
        and (message.text or "").strip()
    ):
        content_key = _repeat_text_key(message)
        if content_key:
            repeat_count = track_repeat_count(bc_id, message.chat.id, sender.id, content_key)
            if repeat_count >= REPEAT_LIMIT:
                try:
                    delete_business_message(bc_id, message.message_id)
                except Exception:
                    pass
                try:
                    safe_send_message(
                        connection["owner_chat_id"],
                        f"{e('bell_2')} <b>عميل بيكرر نفس الرسالة كتير</b>\n"
                        f"{e('person')} <b>الاسم:</b> {html_escape(display_name(sender))}\n"
                        f"{e('chat')} <b>الرسالة:</b> {html_escape(message.text.strip()[:200])}\n\n"
                        f"{e('trash')} <i>اتحذفت تلقائيًا عشان التكرار.</i>",
                    )
                except Exception:
                    pass
                return

    if (
        owner_feature_on("muted", owner_id)
        and sender.id != owner_id
        and db.is_muted(owner_id, sender.id)
    ):
        deleted_ok, delete_error = delete_business_message_checked(bc_id, message.message_id)
        if not deleted_ok and delete_error_is_permission(delete_error):
            notify_mute_permission_missing(
                connection["owner_chat_id"], owner_id, display_name(sender), detail=delete_error
            )
        return

    replied = False
    if owner_feature_on("auto_replies", owner_id):
        replied = bool(try_auto_reply(owner_id, bc_id, message, sender_is_owner=False))
    elif command_text:
        print(f"Auto-replies feature is off for owner={owner_id} (state={_feature_state('auto_replies', owner_id)!r})", flush=True)


@bot.edited_business_message_handler(func=lambda m: True, content_types=telebot.util.content_type_media)
def handle_edited_business_message(message):
    bc_id = message.business_connection_id
    sender = message.from_user
    text, ctype = content_of(message)
    media_file_id, media_kind = media_reference(message)

    original = db.get_message(bc_id, message.chat.id, message.message_id)

    db.save_message(
        bc_id=bc_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        sender_id=sender.id if sender else 0,
        sender_name=display_name(sender),
        sender_username=(sender.username if sender else "") or "",
        content=text,
        content_type=ctype,
        date=message.date,
        media_file_id=media_file_id,
        media_kind=media_kind,
    )

    notify_edited_business_message(message, original, sender, text)

def notify_edited_business_message(message, original, sender, new_content):
    bc_id = message.business_connection_id
    connection = _ensure_business_connection(bc_id)
    if not connection:
        return
    if not owner_feature_on("edit_detect", connection["owner_user_id"]):
        return

    owner_chat_id = connection["owner_chat_id"]
    sender_id = sender.id if sender else (original["sender_id"] if original else 0)

    if sender_id == connection["owner_user_id"]:

        return

    if db.is_muted(connection["owner_user_id"], sender_id):

        return

    if db.is_blocked(connection["owner_user_id"], sender_id):

        return

    if not original:

        return

    old_content = original["content"] or "—"
    if old_content == new_content:

        return

    sender_name = html_escape(display_name(sender)) if sender else html_escape(original["sender_name"] or "غير معروف")
    sender_username = f"@{sender.username}" if (sender and sender.username) else "لا يوجد"

    _evals = _ev_values(
        sender_id,
        display_name(sender) if sender else (original["sender_name"] or "غير معروف"),
        (sender.username if sender else original.get("sender_username")) or "",
        **{"قبل_التعديل": old_content, "بعد_التعديل": new_content},
    )

    markup = types.InlineKeyboardMarkup()

    old_media_id = original.get("media_file_id")
    new_media_id, _ = media_reference(message)
    if old_media_id and old_media_id != new_media_id:
        token = uuid.uuid4().hex[:12]
        DELETED_MEDIA_CACHE[token] = original
        markup.add(
            pe.btn(
                f"تحميل {deleted_media_label(original.get('media_kind') or original.get('content_type'))} القديم",
                callback_data=f"deleted_media_download_{token}",
                icon="download",
                style="success",
            )
        )

    markup.add(pe.btn("فتح بروفايل العميل", url=f"tg://user?id={sender_id}", icon="person_3"))

    try:
        _send_styled_owner(owner_chat_id, connection["owner_user_id"], "edited", _evals, reply_markup=markup)
    except Exception:
        pass

def resend_deleted_media(chat_id, original):
    file_id = original.get("media_file_id")
    kind = original.get("media_kind") or original.get("content_type")
    if not file_id:
        return False
    try:
        if kind == "photo":
            bot.send_photo(chat_id, file_id)
        elif kind == "video":
            bot.send_video(chat_id, file_id)
        elif kind == "document":
            bot.send_document(chat_id, file_id)
        elif kind == "audio":
            bot.send_audio(chat_id, file_id)
        elif kind == "voice":
            bot.send_voice(chat_id, file_id)
        elif kind == "animation":
            bot.send_animation(chat_id, file_id)
        elif kind == "sticker":
            bot.send_sticker(chat_id, file_id)
        elif kind == "video_note":
            bot.send_video_note(chat_id, file_id)
        else:
            return False
        return True
    except Exception as exc:
        print(f"Deleted media resend failed: {exc}", flush=True)
        return False

def deleted_media_label(kind):
    return {
        "photo": "الصورة",
        "video": "الفيديو",
        "document": "الملف",
        "audio": "الصوت",
        "voice": "الرسالة الصوتية",
        "animation": "الـ GIF",
        "sticker": "الملصق",
        "video_note": "الفيديو الدائري",
    }.get(kind, "الوسائط")

@bot.callback_query_handler(func=lambda c: c.data.startswith("deleted_media_download_"))
def cb_deleted_media_download(call):
    token = call.data[len("deleted_media_download_"):]
    original = DELETED_MEDIA_CACHE.pop(token, None)
    if not original:
        bot.answer_callback_query(call.id, "انتهت صلاحية زر التحميل.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "جاري إرسال الوسائط...")
    if resend_deleted_media(call.message.chat.id, original):
        label = deleted_media_label(original.get("media_kind") or original.get("content_type"))
        safe_edit_message_text(
            f"{e('check')} <b>تم تحميل {label} وإرساله بنجاح.</b>",
            call.message.chat.id,
            call.message.message_id,
        )
    else:
        safe_edit_message_text(
            f"{e('cross')} <b>تعذر تحميل الوسائط المحذوفة.</b>",
            call.message.chat.id,
            call.message.message_id,
        )

@bot.deleted_business_messages_handler(func=lambda m: True)
def handle_deleted_business_messages(deleted):
    bc_id = deleted.business_connection_id
    connection = _ensure_business_connection(bc_id)
    if not connection:
        return
    if not owner_feature_on("delete_detect", connection["owner_user_id"]):
        return

    owner_chat_id = connection["owner_chat_id"]

    for msg_id in deleted.message_ids:
        original = db.get_message(bc_id, deleted.chat.id, msg_id)
        if not original:

            continue

        if db.is_muted(connection["owner_user_id"], original["sender_id"]):

            continue

        if db.is_blocked(connection["owner_user_id"], original["sender_id"]):

            continue

        if original["sender_id"] == connection["owner_user_id"]:

            continue

        last_msg = db.get_last_message_from_sender(
            bc_id, deleted.chat.id, original["sender_id"], exclude_message_id=msg_id
        )

        sender_name = html_escape(original["sender_name"] or "غير معروف")
        sender_username = f"@{original['sender_username']}" if original["sender_username"] else "لا يوجد"

        deleted_content = original["content"] or "—"
        last_content = last_msg["content"] if last_msg else "لا توجد رسائل سابقة"

        _dvals = _ev_values(
            original["sender_id"], original["sender_name"], original["sender_username"],
            **{"الرسالة_المحذوفة": deleted_content, "آخر_رسالة": last_content},
        )

        markup = None
        if original.get("media_file_id"):
            token = uuid.uuid4().hex[:12]
            DELETED_MEDIA_CACHE[token] = original
            markup = types.InlineKeyboardMarkup()
            markup.add(
                pe.btn(
                    f"تحميل {deleted_media_label(original.get('media_kind') or original.get('content_type'))}",
                    callback_data=f"deleted_media_download_{token}",
                    icon="download",
                    style="success",
                )
            )
        markup = markup or types.InlineKeyboardMarkup()
        markup.add(pe.btn("فتح بروفايل العميل", url=f"tg://user?id={original['sender_id']}", icon="person_3"))

        try:
            _send_styled_owner(owner_chat_id, connection["owner_user_id"], "deleted", _dvals, reply_markup=markup)
        except Exception:
            pass

ACC_PAGE_SIZE = 8

BANNED_NOTICE_PLAIN = "أنت محظور من استخدام البوت."
BANNED_NOTICE_HTML = f"<b>{BANNED_NOTICE_PLAIN}</b>"

_BANNED_IDS = set(db.list_banned_bot_ids())
_SUPERVISOR_IDS = set(db.list_supervisor_ids())
_BC_OWNER_CACHE = {}
_TRACKED_AT = {}
_BAN_NOTICE_AT = {}
_ADM_PENDING_BC = {}
_BOT_SELF_ID = None

def _fmt_ts(ts):
    try:
        if int(ts) >= 4_000_000_000:
            return "مدى الحياة ♾"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return "—"

def _is_cancel_text(message):
    return (getattr(message, "text", "") or "").strip().lower() in ("الغاء", "إلغاء", "cancel")

_SUP_ALLOWED_ACTIONS = ("open", "cancel", "noop", "exit", "accs_", "ban", "banned", "unban_", "unbanx_",
                        "unbanid", "premadd", "premperm", "premrm", "premlist_", "free", "premdelq_",
                        "lookup", "banid_", "premfor_", "prempermid_")

def _adm_only(call, action=None):
    if is_admin(call.from_user.id):
        return True
    if action is not None and is_supervisor(call.from_user.id):
        if any(action == a or (a.endswith("_") and action.startswith(a)) for a in _SUP_ALLOWED_ACTIONS):
            return True
    bot.answer_callback_query(call.id, "الخاصية دي للأدمن بس.", show_alert=True)
    return False

def _adm_edit(call, text, markup=None):
    try:
        safe_edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=markup,
        )
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        safe_send_message(call.message.chat.id, text, reply_markup=markup)

def adm_cancel_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" إلغاء", callback_data="adm_cancel", icon="cross", style="danger"))
    return markup

def adm_home_text(prefix=""):
    return (
        f"{prefix}"
        f"{e('admin_panel')} <b>لوحة تحكم الأدمن</b>\n\n"
        f"{e('people')} الحسابات النشطة: <b>{db.count_active_accounts()}</b>\n"
        f"{e('person')} مستخدمو البوت: <b>{db.count_bot_users()}</b>\n"
        f"{e('shield')} المحظورون: <b>{len(_BANNED_IDS)}</b>\n"
        f"{e('star')} المميزون: <b>{db.count_premium_users()}</b>\n"
        f"{e('diamond')} وضع الاشتراك: <b>{ACCESS_MODE_INFO[access_mode()]['label']}</b>"
    )

def _adm_send_home(chat_id, prefix=""):
    _send_inline_page(chat_id, adm_home_text(prefix), admin_panel_markup(chat_id))

def _adm_step_interrupted(message):
    if getattr(message, "content_type", "") == "text" and (message.text or "").startswith("/"):
        try:
            bot.process_new_messages([message])
        except Exception as exc:
            print(f"admin step interrupt failed: {exc}", flush=True)
        return True
    return False

def _adm_log(actor_id, action, target_id=None, details=""):
    try:
        db.log_admin_action(actor_id, action, target_id, details)
    except Exception as exc:
        print(f"admin log failed: {exc}", flush=True)

def _adm_actor_label(actor_id):
    if is_admin(actor_id):
        return "المالك"
    for r in db.list_supervisors():
        if int(r["user_id"]) == int(actor_id):
            return f"مشرف {r.get('name') or actor_id}"
    return str(actor_id)

def _adm_step_common(message):
    if not is_staff(message.from_user.id):
        return True
    if _adm_step_interrupted(message):
        return True
    if _is_cancel_text(message):
        _ADM_PENDING_BC.pop(message.from_user.id, None)
        _adm_send_home(message.chat.id, f"{e('check')} <b>تم الإلغاء.</b>\n\n")
        return True
    return False


def _open_admin_scam_scan(chat_id, raw=""):
    if not _scam_channels():
        safe_send_message(chat_id, "أضف قناة عامة واحدة على الأقل من «قنوات فحص النصابين» أولًا.",
                          reply_markup=admin_panel_markup())
        return
    if raw:
        try:
            target = scam_checker.normalize_target(raw)
        except ValueError as exc:
            safe_send_message(chat_id, f"{html_escape(str(exc))}\nاكتب @username أو ID رقميًا.")
            return
        target_label = ("@" if target["kind"] == "username" else "") + target["value"]
        _start_manual_scam_scan(
            [target], target_label,
            lambda text: safe_send_message(chat_id, text, disable_web_page_preview=True),
        )
        return
    prompt = safe_send_message(chat_id,
        "أرسل @username أو ID رقميًا لفحصه في منشورات القنوات العامة، أو اكتب «إلغاء». ")
    bot.register_next_step_handler(prompt, _admin_scam_scan_step)


def _private_owner_can_scamcheck(user_id):
    return bool(
        user_id
        and _owner_active_connection(user_id)
        and is_premium_active(user_id)
        and owner_feature_on("scam_check", user_id)
        and audience_allows("scam_check", user_id, user_id)
    )


def _public_scam_check_allowed(chat_id, user_id):
    if is_admin(user_id):
        return True
    if access_mode() == "trial_24h" and not is_premium_active(user_id):
        start_trial_if_needed(user_id)
    return True


def _public_scam_scan_submit(chat_id, user_id, raw):
    raw = (raw or "").strip()
    if raw.casefold() in ("إلغاء", "الغاء", "cancel", "/cancel"):
        safe_send_message(chat_id, "تم إلغاء الفحص.")
        return
    if not _public_scam_check_allowed(chat_id, user_id):
        return
    if not _scam_channels():
        safe_send_message(chat_id, "فحص النصابين غير متاح حاليًا؛ لم تُضف قنوات عامة للفحص بعد.")
        return
    parsed = _parse_scam_check_command(raw)
    target_raw = parsed if parsed else raw
    try:
        target = scam_checker.normalize_target(target_raw)
    except ValueError as exc:
        prompt = safe_send_message(
            chat_id,
            f"{html_escape(str(exc))}\nأرسل @username أو ID رقميًا، أو اكتب «إلغاء».",
        )
        bot.register_next_step_handler(prompt, _public_scam_scan_step)
        return
    target_label = ("@" if target["kind"] == "username" else "ID ") + target["value"]
    _start_manual_scam_scan(
        [target], target_label,
        lambda text: safe_send_message(chat_id, text, disable_web_page_preview=True),
    )


def _public_scam_scan_step(message):
    if not message.from_user or message.chat.type != "private":
        return
    _public_scam_scan_submit(message.chat.id, message.from_user.id, message.text or "")


def _open_public_scam_scan(message, raw=""):
    user_id = message.from_user.id
    if not _public_scam_check_allowed(message.chat.id, user_id):
        return
    if not _scam_channels():
        safe_send_message(message.chat.id, "فحص النصابين غير متاح حاليًا؛ لم تُضف قنوات عامة للفحص بعد.")
        return
    if raw:
        _public_scam_scan_submit(message.chat.id, user_id, raw)
        return
    prompt = safe_send_message(
        message.chat.id,
        "ابعت @username أو ID رقمي للشخص اللي عايز تفحصه، أو اكتب «إلغاء».\n"
        "الفحص يبحث في منشورات القنوات العامة المضافة، ومش حكم قاطع على الشخص.",
    )
    bot.register_next_step_handler(prompt, _public_scam_scan_step)


def _owner_scam_scan_submit(chat_id, user_id, raw):
    raw = (raw or "").strip()
    if raw.casefold() in ("إلغاء", "الغاء", "cancel", "/cancel"):
        safe_send_message(chat_id, "تم إلغاء الفحص.")
        return
    try:
        target = scam_checker.normalize_target(raw)
    except ValueError as exc:
        prompt = safe_send_message(
            chat_id,
            f"{html_escape(str(exc))}\nأرسل @username أو ID رقميًا، أو اكتب «إلغاء».",
        )
        bot.register_next_step_handler(prompt, _owner_scam_scan_step)
        return
    target_label = ("@" if target["kind"] == "username" else "") + target["value"]
    _start_manual_scam_scan(
        [target], target_label,
        lambda text: safe_send_message(chat_id, text, disable_web_page_preview=True),
    )


def _owner_scam_scan_step(message):
    if not message.from_user or message.chat.type != "private":
        return
    if not _private_owner_can_scamcheck(message.from_user.id):
        safe_send_message(message.chat.id, "الفحص الخاص متاح لصاحب حساب Business المشترك فقط.")
        return
    _owner_scam_scan_submit(message.chat.id, message.from_user.id, message.text or "")


def _open_owner_scam_scan(message, raw=""):
    user_id = message.from_user.id if message.from_user else 0
    if not _private_owner_can_scamcheck(user_id):
        safe_send_message(
            message.chat.id,
            "الفحص في الخاص متاح للمدير صاحب حساب Business لديه اشتراك فعّال فقط.",
        )
        return
    if not _scam_channels():
        safe_send_message(message.chat.id, "لا توجد قنوات عامة مضافة للفحص حتى الآن.")
        return
    if raw:
        _owner_scam_scan_submit(message.chat.id, user_id, raw)
        return
    prompt = safe_send_message(
        message.chat.id,
        "أرسل @username أو ID رقميًا لفحصه في منشورات القنوات العامة، أو اكتب «إلغاء».",
    )
    bot.register_next_step_handler(prompt, _owner_scam_scan_step)


@bot.message_handler(commands=["scamcheck"])
def cmd_scamcheck(message):
    if message.chat.type != "private" or not message.from_user:
        return
    raw = _parse_scam_check_command(message.text or "", allow_empty=True) or ""
    if is_admin(message.from_user.id):
        _open_admin_scam_scan(message.chat.id, raw)
        return
    _open_public_scam_scan(message, raw)


@bot.message_handler(func=lambda m: bool(
    m.chat.type == "private" and m.from_user
    and _parse_scam_check_command(m.text or "", allow_empty=True) is not None
))
def text_scamcheck(message):
    raw = _parse_scam_check_command(message.text or "", allow_empty=True) or ""
    if is_admin(message.from_user.id):
        _open_admin_scam_scan(message.chat.id, raw)
    else:
        _open_public_scam_scan(message, raw)

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_staff(message.from_user.id):
        return
    try:
        bot.clear_step_handler_by_chat_id(message.chat.id)
    except Exception:
        pass
    _clear_pending_state(message.from_user.id)
    _adm_send_home(message.chat.id)

@bot.message_handler(
    func=lambda m: m.chat.type == "private" and m.content_type == "text"
    and _step_norm(m.text) in _STEP_ADMIN_WORDS and is_staff(m.from_user.id)
)
def text_admin(message):
    cmd_admin(message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def cb_adm(call):
    action = call.data[len("adm_"):]
    if not _adm_only(call, action):
        return
    if action.startswith("scam") and not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "هذه الإدارة للمدير الأساسي فقط.", show_alert=True)
        return
    chat_id = call.message.chat.id

    if action == "scam":
        safe_edit_message_text(_scam_channels_menu_text(), chat_id, call.message.message_id,
                               reply_markup=_scam_channels_menu_markup())
        bot.answer_callback_query(call.id)

    elif action == "scam_back":
        _adm_edit(call, adm_home_text(), admin_panel_markup(call.from_user.id))
        bot.answer_callback_query(call.id)

    elif action.startswith("scam_page_"):
        try:
            page = int(action[len("scam_page_"):])
        except ValueError:
            page = 0
        safe_edit_message_text(
            _scam_channels_menu_text(page), chat_id, call.message.message_id,
            reply_markup=_scam_channels_menu_markup(page),
        )
        bot.answer_callback_query(call.id)

    elif action == "scam_add":
        prompt = safe_send_message(chat_id,
            "أرسل يوزر القناة العامة مثل <code>@channel</code> أو رابط t.me. "
            "يمكن إرسال أكثر من قناة، كل واحدة في سطر، أو اكتب «إلغاء».")
        bot.register_next_step_handler(prompt, _admin_scam_add_step)
        bot.answer_callback_query(call.id)

    elif action == "scam_owner":
        prompt = safe_send_message(chat_id,
            "أرسل ID أو @username لصاحب حساب الأعمال لتبديل حالة الفحص التلقائي، أو اكتب «إلغاء».")
        bot.register_next_step_handler(prompt, _admin_scam_owner_step)
        bot.answer_callback_query(call.id)

    elif action == "scam_owners":
        text, markup = _scam_enabled_owners_view()
        safe_edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif action.startswith("scam_owner_toggle_"):
        try:
            owner_id = int(action[len("scam_owner_toggle_"):])
            owners = _scam_active_owners()
            if owner_id not in owners:
                raise ValueError("inactive owner")
            db.set_setting(f"scam_watch_owner_{owner_id}", "0")
            bot.answer_callback_query(call.id, "تم إيقاف الفحص لهذا الحساب.")
        except (ValueError, TypeError):
            bot.answer_callback_query(call.id, "الحساب غير نشط أو غير صحيح.", show_alert=True)
        text, markup = _scam_enabled_owners_view()
        safe_edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)

    elif action.startswith("scam_del_"):
        try:
            index = int(action[len("scam_del_"):])
            channels = _scam_channels()
            removed = channels.pop(index)
            db.set_setting("scam_watch_channels", json.dumps(channels, ensure_ascii=False))
            bot.answer_callback_query(call.id, f"حُذفت {removed}")
            page = min(index // SCAM_CHANNELS_PAGE_SIZE,
                       max(0, (len(channels) + SCAM_CHANNELS_PAGE_SIZE - 1) // SCAM_CHANNELS_PAGE_SIZE - 1))
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "القناة غير موجودة.", show_alert=True)
            page = 0
        safe_edit_message_text(_scam_channels_menu_text(page), chat_id, call.message.message_id,
                               reply_markup=_scam_channels_menu_markup(page))

    elif action == "scam_toggle":
        if not _scam_channels():
            bot.answer_callback_query(call.id, "أضف قناة عامة أولًا.", show_alert=True)
            return
        new_state = not _scam_watch_enabled()
        db.set_setting("scam_watch_enabled", "1" if new_state else "0")
        bot.answer_callback_query(call.id, "الفحص التلقائي اتفعّل." if new_state else "الفحص التلقائي اتوقف.")
        safe_edit_message_text(_scam_channels_menu_text(), chat_id, call.message.message_id,
                               reply_markup=_scam_channels_menu_markup())

    elif action == "scam_scan":
        if not _scam_channels():
            bot.answer_callback_query(call.id, "أضف قناة عامة أولًا.", show_alert=True)
            return
        prompt = safe_send_message(chat_id,
            "أرسل @username أو ID رقميًا لفحصه في منشورات القنوات العامة، أو اكتب «إلغاء».")
        bot.register_next_step_handler(prompt, _admin_scam_scan_step)
        bot.answer_callback_query(call.id)

    elif action == "open":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(call, adm_home_text(), admin_panel_markup(call.from_user.id))
        bot.answer_callback_query(call.id)

    elif action == "exit":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            main_menu_text(call.from_user.id),
            owner_panel_markup(call.from_user.id),
        )
        bot.answer_callback_query(call.id)

    elif action == "cancel":
        bot.clear_step_handler_by_chat_id(chat_id)
        _ADM_PENDING_BC.pop(call.from_user.id, None)
        _adm_edit(call, adm_home_text(f"{e('check')} <b>تم الإلغاء.</b>\n\n"), admin_panel_markup(call.from_user.id))
        bot.answer_callback_query(call.id, "تم الإلغاء.")

    elif action == "noop":
        bot.answer_callback_query(call.id)

    elif action.startswith("accs_"):
        try:
            page = int(action.split("_", 1)[1])
        except ValueError:
            page = 0
        text, markup = _adm_accounts_view(page)
        _adm_edit(call, text, markup)
        bot.answer_callback_query(call.id)

    elif action == "bc":
        bot.clear_step_handler_by_chat_id(chat_id)
        total = len(db.list_broadcast_recipients())
        _adm_edit(
            call,
            f"{e('megaphone')} <b>إذاعة للجميع</b>\n\n"
            f"{e('people')} هتوصل لـ <b>{total}</b> مستخدم من مستخدمي البوت "
            "(اللي دخلوا البوت وكلّموه في الخاص، من غير المحظورين).\n\n"
            "ابعت دلوقتي محتوى الإذاعة (نص، صورة، فيديو، ملف...) وهتتبعت "
            "زي ما هي بنفس التنسيق، وهتشوف معاينة قبل التأكيد.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_broadcast)
        bot.answer_callback_query(call.id)

    elif action == "bcgo":
        pending = _ADM_PENDING_BC.pop(call.from_user.id, None)
        if not pending:
            bot.answer_callback_query(call.id, "مفيش إذاعة معلّقة، ابدأ من جديد.", show_alert=True)
            return
        recipients = db.list_broadcast_recipients()
        _adm_edit(call, f"{e('clock')} <b>جاري إرسال الإذاعة لـ {len(recipients)} مستخدم...</b>")
        bot.answer_callback_query(call.id)
        threading.Thread(
            target=_run_admin_broadcast,
            args=(chat_id, call.message.message_id, pending, recipients),
            daemon=True,
        ).start()

    elif action == "ban":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            f"{e('shield')} <b>حظر مستخدم من البوت</b>\n\n"
            "ابعت واحد من دول:\n"
            "• آيدي المستخدم (أرقام)\n"
            "• يوزره <code>@username</code>\n"
            "• أو حوّل رسالة منه\n\n"
            "المستخدم المحظور مش هيقدر يستخدم البوت، وحساب الـ Business بتاعه "
            "هيتوقف تمامًا.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_ban)
        bot.answer_callback_query(call.id)

    elif action == "banned":
        rows = db.list_banned_bot_users()
        if not rows:
            bot.answer_callback_query(call.id, "مفيش مستخدمين محظورين.", show_alert=True)
            return
        text, markup = _adm_banned_view(rows)
        _adm_edit(call, text, markup)
        bot.answer_callback_query(call.id)

    elif action.startswith("unban_") or action.startswith("unbanx_"):
        from_result = action.startswith("unbanx_")
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "آيدي غير صالح.", show_alert=True)
            return
        info = db.find_known_user(user_id=uid) or {}
        removed = db.unban_bot_user(uid)
        _BANNED_IDS.discard(uid)
        if removed:
            _adm_log(call.from_user.id, "فك حظر", uid, info.get("name", ""))
            try:
                bot.send_message(uid, "<b>تم فك الحظر عنك، تقدر تستخدم البوت تاني.</b>")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "تم فك الحظر." if removed else "المستخدم مش محظور.")
        rows = db.list_banned_bot_users()
        if from_result or not rows:
            markup = types.InlineKeyboardMarkup()
            markup.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_open", icon="admin_panel", style="primary"))
            _adm_edit(
                call,
                f"{e('unlock')} <b>تم فك الحظر عن:</b> {html_escape(info.get('name') or 'المستخدم')} "
                f"(<code>{uid}</code>)",
                markup,
            )
        else:
            text, markup = _adm_banned_view(rows)
            _adm_edit(call, text, markup)

    elif action == "lookup":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            f"{e('eye')} <b>بحث عن مستخدم</b>\n\n"
            "ابعت آيدي المستخدم أو @يوزره أو حوّل رسالة منه.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_lookup)
        bot.answer_callback_query(call.id)

    elif action.startswith("banid_"):
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id)
            return
        if is_admin(uid) or (is_supervisor(uid) and not is_admin(call.from_user.id)):
            bot.answer_callback_query(call.id, "مينفعش تحظر أدمن أو مشرف.", show_alert=True)
            return
        info = db.get_bot_user_row(uid) or {}
        db.ban_bot_user(uid, info.get("name", ""), info.get("username", ""))
        _BANNED_IDS.add(uid)
        _adm_log(call.from_user.id, "حظر", uid, info.get("name", ""))
        bot.answer_callback_query(call.id, "تم الحظر.")
        text, markup = _adm_lookup_view(uid, call.from_user.id)
        _adm_edit(call, text, markup)

    elif action.startswith("premfor_"):
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id)
            return
        info = db.get_bot_user_row(uid) or {}
        target = {"user_id": uid, "name": info.get("name") or str(uid), "username": info.get("username") or ""}
        bot.clear_step_handler_by_chat_id(chat_id)
        msg = safe_send_message(
            chat_id,
            f"{e('star')} ابعت <b>عدد الأيام</b> (1 لـ {PREMIUM_MAX_DAYS})، أو كلمة <b>دائم</b>:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_prem_days, target)
        bot.answer_callback_query(call.id)

    elif action.startswith("prempermid_"):
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id)
            return
        info = db.get_bot_user_row(uid) or {}
        target = {"user_id": uid, "name": info.get("name") or str(uid), "username": info.get("username") or ""}
        bot.answer_callback_query(call.id, "تم تفعيل الاشتراك الدائم.")
        _adm_grant_premium(chat_id, target, None)

    elif action == "log":
        rows = db.list_admin_log(25)
        if not rows:
            text = f"{e('document')} <b>سجل العمليات</b>\n\nمفيش عمليات متسجلة لسه."
        else:
            lines = []
            for r in rows:
                who = html_escape(_adm_actor_label(r["actor_id"]))
                tgt = f" ← <code>{r['target_id']}</code>" if r.get("target_id") else ""
                det = f" ({html_escape(r['details'])})" if r.get("details") else ""
                lines.append(f"• {_fmt_ts(r['ts'])}\n  <b>{who}</b>: {html_escape(r['action'])}{tgt}{det}")
            text = f"{e('document')} <b>سجل العمليات (آخر {len(rows)})</b>\n\n" + "\n".join(lines)
        markup = types.InlineKeyboardMarkup()
        markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
        _adm_edit(call, text[:4000], markup)
        bot.answer_callback_query(call.id)

    elif action in ("welcome", "welcome_reset"):
        if action == "welcome_reset":
            db.set_setting("custom_welcome", "")
            bot.answer_callback_query(call.id, "رجعت الترحيب الافتراضي.")
        cur = db.get_setting("custom_welcome", "") or ""
        markup = types.InlineKeyboardMarkup()
        markup.add(pe.btn(" تغيير الرسالة", callback_data="adm_welcome_set", icon="pencil", style="success"))
        if cur:
            markup.add(pe.btn(" رجوع للافتراضي", callback_data="adm_welcome_reset", icon="trash", style="danger"))
        markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
        shown = html_escape(cur) if cur else "<i>الافتراضية</i>"
        _adm_edit(
            call,
            f"{e('pencil')} <b>رسالة الترحيب</b>\n\nالحالية:\n{shown}\n\n"
            "تقدر تكتب <code>{name}</code> مكان اسم المستخدم و<code>{id}</code> مكان أيديه.",
            markup,
        )
        if action == "welcome":
            bot.answer_callback_query(call.id)

    elif action == "welcome_set":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(call, f"{e('pencil')} <b>ابعت نص رسالة الترحيب الجديدة:</b>", adm_cancel_markup())
        bot.register_next_step_handler(call.message, adm_step_welcome)
        bot.answer_callback_query(call.id)

    elif action == "unbanid":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            f"{e('unlock')} <b>فك حظر مستخدم</b>\n\n"
            "ابعت آيدي المستخدم (أرقام) أو يوزره <code>@username</code> أو حوّل رسالة منه.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_unban)
        bot.answer_callback_query(call.id)

    elif action == "premperm":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            f"{e('diamond')} <b>إضافة مميز دائم (مدى الحياة)</b>\n\n"
            "ابعت آيدي المستخدم أو @يوزره أو حوّل رسالة منه، وهيتفعّل له اشتراك من غير نهاية.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_prem_user, "perm")
        bot.answer_callback_query(call.id)

    elif action == "supadd":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            f"{e('people')} <b>إضافة مشرف</b>\n\n"
            "ابعت آيدي الشخص أو @يوزره أو حوّل رسالة منه.\n"
            "المشرف يقدر: يحظر ويفك الحظر، ويضيف ويحذف مميزين، ويشوف الحسابات والمميزين والمحظورين.\n"
            "ومقدرش: يعمل إذاعة أو نسخ احتياطي أو يعدّل الإعدادات أو يضيف مشرفين.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_sup_add)
        bot.answer_callback_query(call.id)

    elif action == "suplist":
        rows = db.list_supervisors()
        text, markup = _adm_sup_view(rows)
        _adm_edit(call, text, markup)
        bot.answer_callback_query(call.id)

    elif action.startswith("supdel_"):
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "آيدي غير صالح.", show_alert=True)
            return
        removed = db.remove_supervisor(uid)
        _SUPERVISOR_IDS.discard(uid)
        if removed:
            _adm_log(call.from_user.id, "حذف مشرف", uid)
        if removed:
            try:
                bot.send_message(uid, "<b>تم إلغاء صلاحية الإشراف عنك.</b>")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "تم حذف المشرف." if removed else "مش مشرف.")
        text, markup = _adm_sup_view(db.list_supervisors())
        _adm_edit(call, text, markup)

    elif action == "premadd":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            f"{e('star')} <b>إضافة مميز</b>\n\n"
            "ابعت واحد من دول:\n"
            "• آيدي المستخدم (أرقام)\n"
            "• يوزره <code>@username</code>\n"
            "• أو حوّل رسالة منه\n\n"
            "وبعدها هسألك عن عدد الأيام.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_prem_user, "add")
        bot.answer_callback_query(call.id)

    elif action == "premrm":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(
            call,
            f"{e('trash')} <b>حذف مميز</b>\n\n"
            "ابعت آيدي المستخدم أو @يوزره أو حوّل رسالة منه، أو دوس "
            "<b>المميزين</b> من اللوحة واختار منها.",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_prem_user, "del")
        bot.answer_callback_query(call.id)

    elif action.startswith("premlist_"):
        try:
            page = int(action.split("_", 1)[1])
        except ValueError:
            page = 0
        text, markup = _adm_premium_view(page)
        _adm_edit(call, text, markup)
        bot.answer_callback_query(call.id)

    elif action == "free":
        text, markup = _adm_free_members_view()
        _adm_edit(call, text, markup)
        bot.answer_callback_query(call.id)

    elif action.startswith("premdelq_"):
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "آيدي غير صالح.", show_alert=True)
            return
        row = db.get_premium_user(uid)
        if not row:
            bot.answer_callback_query(call.id, "المستخدم مش مميز.", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            pe.btn(" تأكيد الحذف", callback_data=f"adm_premdel_{uid}", icon="check", style="danger"),
            pe.btn(" رجوع", callback_data="adm_premlist_0", icon="arrow_left"),
        )
        _adm_edit(
            call,
            f"{e('trash')} <b>تأكيد حذف المميز</b>\n\n"
            f"{e('person')} <b>الاسم:</b> {html_escape(row.get('name') or 'غير معروف')}\n"
            f"{e('person_2')} <b>ID:</b> <code>{uid}</code>\n"
            f"{e('clock')} <b>المتبقي:</b> {_fmt_remaining(int(row['expires_at']) - int(time.time()))}\n\n"
            "الحذف هيقفل حسابه فورًا ومش هيرجّع الأيام المتبقية.",
            markup,
        )
        bot.answer_callback_query(call.id)

    elif action.startswith("premdel_"):
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "آيدي غير صالح.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        _adm_premium_remove(chat_id, uid, edit_call=call)

    elif action == "addch":
        bot.clear_step_handler_by_chat_id(chat_id)
        channels = force_sub_channels()
        listing = "\n".join(f"• {html_escape(c)}" for c in channels) if channels else "<b>مفيش قنوات مضافة.</b>"
        _adm_edit(
            call,
            f"{e('bullhorn')} <b>إضافة قناة اشتراك إجباري</b>\n\n"
            "ابعت يوزر القناة بالشكل ده:\n<code>@channel_username</code>\n\n"
            "لازم البوت يكون <b>أدمن</b> في القناة عشان التحقق من الاشتراك يشتغل.\n\n"
            f"القنوات الحالية ({len(channels)}):\n{listing}",
            adm_cancel_markup(),
        )
        bot.register_next_step_handler(call.message, adm_step_add_channel)
        bot.answer_callback_query(call.id)

    elif action == "chdone":
        bot.clear_step_handler_by_chat_id(chat_id)
        _adm_edit(call, adm_home_text(f"{e('check')} <b>تم حفظ القنوات.</b>\n\n"), admin_panel_markup())
        bot.answer_callback_query(call.id)

    else:
        bot.answer_callback_query(call.id)

def _adm_accounts_view(page):
    total = db.count_active_accounts()
    pages = max(1, -(-total // ACC_PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    rows = db.list_active_accounts(ACC_PAGE_SIZE, page * ACC_PAGE_SIZE)

    markup = types.InlineKeyboardMarkup(row_width=3)
    if not rows:
        text = f"{e('people')} <b>الحسابات النشطة</b>\n\nمفيش حسابات نشطة دلوقتي."
    else:
        lines = []
        for i, r in enumerate(rows, start=page * ACC_PAGE_SIZE + 1):
            name = html_escape(r["name"] or "غير معروف")
            uname = f"@{html_escape(r['username'])}" if r["username"] else "بدون يوزر"
            last = _fmt_ts(r["last_activity"]) if r["last_activity"] else "—"
            lines.append(
                f"<b>{i}.</b> <a href=\"tg://user?id={r['user_id']}\">{name}</a> — {uname}\n"
                f"{e('person_2')} <b>ID:</b> <code>{r['user_id']}</code>\n"
                f"{e('people_2')} العملاء: <b>{r['clients']}</b> · آخر نشاط: <b>{last}</b>"
            )
        text = (
            f"{e('people')} <b>الحسابات النشطة</b> ({total})\n"
            "<i>أصحاب حسابات Business رابطينها بالبوت ومفعّلة.</i>\n\n"
            + "\n\n".join(lines)
        )
        nav = []
        if page > 0:
            nav.append(pe.btn(" السابق", callback_data=f"adm_accs_{page - 1}", icon="arrow_right", style="primary"))
        nav.append(pe.btn(f"{page + 1}/{pages}", callback_data="adm_noop"))
        if page < pages - 1:
            nav.append(pe.btn(" التالي", callback_data=f"adm_accs_{page + 1}", icon="arrow_left", style="primary"))
        markup.row(*nav)
    markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
    return text, markup

def adm_step_broadcast(message):
    if _adm_step_common(message):
        return
    admin_id = message.from_user.id
    try:

        bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    except Exception:
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} النوع ده مش مدعوم للإذاعة. ابعت نص أو صورة أو فيديو أو ملف:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_broadcast)
        return

    _ADM_PENDING_BC[admin_id] = (message.chat.id, message.message_id)
    total = len(db.list_broadcast_recipients())
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" تأكيد الإرسال", callback_data="adm_bcgo", icon="check", style="success"),
        pe.btn(" إلغاء", callback_data="adm_cancel", icon="cross", style="danger"),
    )
    safe_send_message(
        message.chat.id,
        f"{e('megaphone')} <b>دي معاينة الإذاعة فوق.</b>\n"
        f"{e('people')} هتتبعت لـ <b>{total}</b> مستخدم. راجعها وبعدين دوس تأكيد.",
        reply_markup=markup,
    )

def _copy_with_retry(chat_id, from_chat_id, message_id):
    try:
        return bot.copy_message(chat_id, from_chat_id, message_id)
    except apihelper.ApiTelegramException as exc:
        if getattr(exc, "error_code", None) == 429:
            retry_after = 1
            try:
                retry_after = int(exc.result_json.get("parameters", {}).get("retry_after", 1))
            except Exception:
                pass
            time.sleep(min(retry_after, 30) + 1)
            return bot.copy_message(chat_id, from_chat_id, message_id)
        raise

def _run_admin_broadcast(admin_chat_id, status_message_id, pending, recipients):
    from_chat, msg_id = pending
    sent = failed = blocked = 0
    for uid in recipients:
        if is_admin(uid):
            continue
        try:
            _copy_with_retry(uid, from_chat, msg_id)
            sent += 1
        except apihelper.ApiTelegramException as exc:
            if getattr(exc, "error_code", None) == 403:
                blocked += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.05)

    text = (
        f"{e('check')} <b>تم إرسال الإذاعة.</b>\n\n"
        f"{e('people_2')} وصلت لـ <b>{sent}</b> مستخدم\n"
        f"{e('shield')} حاظرين البوت: <b>{blocked}</b>\n"
        f"{e('cross')} فشلت: <b>{failed}</b>"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_open", icon="admin_panel", style="primary"))
    try:
        safe_edit_message_text(text, admin_chat_id, status_message_id, reply_markup=markup)
    except Exception:
        safe_send_message(admin_chat_id, text, reply_markup=markup)

def _adm_resolve_target(message):
    forwarded = _forwarded_user(message)
    if forwarded:
        return {"user_id": forwarded.id, "name": display_name(forwarded), "username": forwarded.username or ""}

    text = (message.text or "").strip()
    if not text:
        return None

    if re.fullmatch(r"\d{5,15}", text):
        uid = int(text)
        info = db.find_known_user(user_id=uid) or {}
        return {"user_id": uid, "name": info.get("name") or "غير معروف", "username": info.get("username") or ""}

    m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,31})", text)
    if m:
        uname = m.group(1)
        info = db.find_known_user(username=uname)
        if info:
            return {"user_id": info["user_id"], "name": info.get("name") or "غير معروف", "username": info.get("username") or uname}
        try:
            chat = bot.get_chat(f"@{uname}")
            if getattr(chat, "type", "") == "private":
                name = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "غير معروف"
                return {"user_id": chat.id, "name": name, "username": chat.username or uname}
        except Exception:
            pass
    return None

def adm_step_ban(message):
    if _adm_step_common(message):
        return
    target = _adm_resolve_target(message)
    if not target:
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>مقدرتش أحدد المستخدم.</b> ابعت آيدي رقمي، أو @يوزر معروف للبوت، أو حوّل رسالة منه:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_ban)
        return

    uid = target["user_id"]
    if is_admin(uid) or (is_supervisor(uid) and not is_admin(message.from_user.id)):
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>ده حساب أدمن أو مشرف، مينفعش يتحظر.</b> ابعت مستخدم تاني:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_ban)
        return

    already = uid in _BANNED_IDS
    db.ban_bot_user(uid, target["name"], target["username"])
    _adm_log(message.from_user.id, "حظر", uid, target["name"])
    _BANNED_IDS.add(uid)

    uname = f"@{html_escape(target['username'])}" if target["username"] else "لا يوجد"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" فك الحظر", callback_data=f"adm_unbanx_{uid}", icon="unlock", style="success"),
        pe.btn(" فتح البروفايل", url=f"tg://user?id={uid}", icon="person_3", style="primary"),
    )
    markup.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_open", icon="admin_panel", style="primary"))
    safe_send_message(
        message.chat.id,
        f"{e('shield')} <b>{'المستخدم كان محظور بالفعل' if already else 'تم حظر المستخدم من البوت'}</b>\n\n"
        f"{e('person')} <b>الاسم:</b> {html_escape(target['name'])}\n"
        f"{e('link')} <b>اليوزر:</b> {uname}\n"
        f"{e('person_2')} <b>ID:</b> <code>{uid}</code>",
        reply_markup=markup,
    )
    if not already:
        try:
            bot.send_message(uid, "<b>تم حظرك من استخدام البوت.</b>")
        except Exception:
            pass

def adm_step_welcome(message):
    if _adm_step_common(message):
        return
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or len(text) > 3000:
        msg = safe_send_message(message.chat.id, f"{e('cross')} ابعت نص (لحد 3000 حرف):", reply_markup=adm_cancel_markup())
        bot.register_next_step_handler(msg, adm_step_welcome)
        return
    db.set_setting("custom_welcome", text)
    _adm_log(message.from_user.id, "تغيير رسالة الترحيب")
    safe_send_message(message.chat.id, f"{e('check')} <b>تم حفظ رسالة الترحيب.</b>", reply_markup=admin_panel_markup(message.from_user.id))

def _adm_lookup_view(uid, viewer_id):
    row = db.get_bot_user_row(uid) or {}
    prem = db.get_premium_user(uid)
    prem_txt = "مش مميز"
    if prem and int(prem.get("expires_at") or 0) > time.time():
        prem_txt = f"مميز — ينتهي {_fmt_ts(prem['expires_at'])} ({_fmt_remaining(int(prem['expires_at']) - int(time.time()))})"
    try:
        connected = bool(_owner_active_connection(uid))
    except Exception:
        connected = False
    uname = f"@{html_escape(row['username'])}" if row.get("username") else "لا يوجد"
    role = "المالك" if is_admin(uid) else ("مشرف" if is_supervisor(uid) else "عضو")
    banned = uid in _BANNED_IDS
    text = (
        f"{e('eye')} <b>بيانات المستخدم</b>\n\n"
        f"{e('person')} <b>الاسم:</b> {html_escape(row.get('name') or 'غير معروف')}\n"
        f"{e('link')} <b>اليوزر:</b> {uname}\n"
        f"{e('person_2')} <b>ID:</b> <code>{uid}</code>\n"
        f"{e('star')} <b>الصفة:</b> {role}\n"
        f"{e('diamond')} <b>الاشتراك:</b> {prem_txt}\n"
        f"{e('lock')} <b>محظور:</b> {'نعم' if banned else 'لا'}\n"
        f"{e('check')} <b>حساب Business متصل:</b> {'نعم' if connected else 'لا'}\n"
        f"{e('clock')} <b>أول دخول:</b> {_fmt_ts(row['first_seen']) if row.get('first_seen') else '—'}\n"
        f"{e('clock')} <b>آخر نشاط:</b> {_fmt_ts(row['last_seen']) if row.get('last_seen') else '—'}"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    if not is_admin(uid):
        if banned:
            markup.add(pe.btn(" فك الحظر", callback_data=f"adm_unban_{uid}", icon="unlock", style="success"))
        else:
            markup.add(pe.btn(" حظر", callback_data=f"adm_banid_{uid}", icon="lock", style="danger"))
    markup.add(
        pe.btn(" اشتراك بمدة", callback_data=f"adm_premfor_{uid}", icon="star", style="success"),
        pe.btn(" اشتراك دائم", callback_data=f"adm_prempermid_{uid}", icon="diamond", style="success"),
    )
    if prem and int(prem.get("expires_at") or 0) > time.time():
        markup.add(pe.btn(" حذف الاشتراك", callback_data=f"adm_premdelq_{uid}", icon="trash", style="danger"))
    markup.add(pe.btn(" فتح البروفايل", url=f"tg://user?id={uid}", icon="person_3", style="primary"))
    markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
    return text, markup

def adm_step_lookup(message):
    if _adm_step_common(message):
        return
    target = _adm_resolve_target(message)
    if not target:
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>مقدرتش أحدد المستخدم.</b> ابعت آيدي رقمي، أو @يوزر معروف للبوت، أو حوّل رسالة منه:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_lookup)
        return
    text, markup = _adm_lookup_view(target["user_id"], message.from_user.id)
    safe_send_message(message.chat.id, text, reply_markup=markup)

def adm_step_unban(message):
    if _adm_step_common(message):
        return
    target = _adm_resolve_target(message)
    if not target:
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>مقدرتش أحدد المستخدم.</b> ابعت آيدي رقمي، أو @يوزر معروف للبوت، أو حوّل رسالة منه:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_unban)
        return
    uid = target["user_id"]
    removed = db.unban_bot_user(uid)
    _BANNED_IDS.discard(uid)
    if removed:
        _adm_log(message.from_user.id, "فك حظر", uid, target["name"])
        try:
            bot.send_message(uid, "<b>تم فك الحظر عنك، تقدر تستخدم البوت تاني.</b>")
        except Exception:
            pass
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_open", icon="admin_panel", style="primary"))
    safe_send_message(
        message.chat.id,
        (f"{e('unlock')} <b>تم فك الحظر عن:</b> {html_escape(target['name'])} (<code>{uid}</code>)"
         if removed else
         f"{e('cross')} <b>المستخدم ده مش محظور.</b> (<code>{uid}</code>)"),
        reply_markup=markup,
    )

def _adm_sup_view(rows):
    markup = types.InlineKeyboardMarkup()
    if not rows:
        text = f"{e('people')} <b>المشرفين</b>\n\nمفيش مشرفين دلوقتي."
    else:
        lines = []
        for i, r in enumerate(rows, start=1):
            name = html_escape(r.get("name") or "غير معروف")
            uname = f"@{html_escape(r['username'])}" if r.get("username") else "بدون يوزر"
            lines.append(f"<b>{i}.</b> <a href=\"tg://user?id={r['user_id']}\">{name}</a> — {uname}\n<b>ID:</b> <code>{r['user_id']}</code>")
            label = (r.get("name") or str(r["user_id"]))[:22]
            markup.add(pe.btn(f" حذف {label}", callback_data=f"adm_supdel_{r['user_id']}", icon="trash", style="danger"))
        text = f"{e('people')} <b>المشرفين</b> ({len(rows)})\n\n" + "\n\n".join(lines)
    markup.add(pe.btn(" إضافة مشرف", callback_data="adm_supadd", icon="plus", style="success"))
    markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
    return text, markup

def adm_step_sup_add(message):
    if _adm_step_common(message):
        return
    if not is_admin(message.from_user.id):
        return
    target = _adm_resolve_target(message)
    if not target:
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>مقدرتش أحدد المستخدم.</b> ابعت آيدي رقمي، أو @يوزر معروف للبوت، أو حوّل رسالة منه:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_sup_add)
        return
    uid = target["user_id"]
    if is_admin(uid):
        safe_send_message(message.chat.id, f"{e('cross')} ده حساب الأدمن الأساسي، مش محتاج يتضاف.", reply_markup=admin_panel_markup(message.from_user.id))
        return
    db.add_supervisor(uid, target["name"], target["username"])
    _SUPERVISOR_IDS.add(uid)
    _adm_log(message.from_user.id, "إضافة مشرف", uid, target["name"])
    delivered = True
    try:
        bot.send_message(uid, "<b>تم تعيينك مشرف على البوت.</b>\nابعت /admin عشان تفتح لوحة الإشراف.")
    except Exception:
        delivered = False
    text, markup = _adm_sup_view(db.list_supervisors())
    safe_send_message(
        message.chat.id,
        f"{e('check')} <b>تم إضافة المشرف:</b> {html_escape(target['name'])} (<code>{uid}</code>)\n"
        + ("" if delivered else "⚠️ مقدرتش أبعتله إشعار (لسه ما بدأش البوت)، خليه يبعت /start الأول.\n")
        + "\n" + text,
        reply_markup=markup,
    )

def _adm_banned_view(rows):
    markup = types.InlineKeyboardMarkup()
    for r in rows[:30]:
        label = (r.get("name") or str(r["user_id"]))[:24]
        markup.add(pe.btn(f" فك حظر {label}", callback_data=f"adm_unban_{r['user_id']}", icon="unlock", style="success"))
    markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
    text = (
        f"{e('shield')} <b>المحظورون</b> ({len(rows)})\n\n"
        "دوس على أي مستخدم عشان تفك الحظر عنه."
    )
    return text, markup

def _bot_self_id():
    global _BOT_SELF_ID
    if _BOT_SELF_ID is None:
        _BOT_SELF_ID = bot.get_me().id
    return _BOT_SELF_ID

def adm_step_add_channel(message):
    if _adm_step_common(message):
        return
    text = (message.text or "").strip()
    link = re.search(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})", text)
    if link:
        text = "@" + link.group(1)

    def retry(problem):
        msg = safe_send_message(
            message.chat.id, f"{e('cross')} {problem}", reply_markup=adm_cancel_markup()
        )
        bot.register_next_step_handler(msg, adm_step_add_channel)

    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", text):
        retry("لازم يوزر قناة صحيح يبدأ بـ @، جرب تاني:")
        return
    if db.channel_exists(None, text):
        retry("القناة دي مضافة بالفعل. ابعت قناة تانية:")
        return

    try:
        chat = bot.get_chat(text)
        if getattr(chat, "type", "") not in ("channel", "supergroup", "group"):
            retry("اليوزر ده مش قناة. ابعت يوزر قناة:")
            return
        member = bot.get_chat_member(text, _bot_self_id())
        if member.status not in ("administrator", "creator"):
            raise RuntimeError("bot is not admin")
    except Exception:
        retry(
            "<b>مقدرتش أتأكد إن البوت أدمن في القناة دي.</b>\n"
            "ارفع البوت أدمن في القناة وابعت اليوزر تاني:"
        )
        return

    db.add_force_sub_channel(None, text)
    channels = force_sub_channels()

    safe_send_message(
        message.chat.id,
        f"{e('lock')} {force_sub_message()}",
        reply_markup=subscription_keyboard(),
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" تم", callback_data="adm_chdone", icon="check", style="success"))
    msg = safe_send_message(
        message.chat.id,
        f"{e('check')} <b>تم إضافة القناة:</b> {html_escape(text)}\n\n"
        f"إجمالي القنوات: <b>{len(channels)}</b>\n"
        "الرسالة اللي فوق هي شكل رسالة الاشتراك عند المستخدمين.\n"
        "ابعت قناة تانية عشان تضيفها، أو دوس <b>تم</b>.",
        reply_markup=markup,
    )
    bot.register_next_step_handler(msg, adm_step_add_channel)

PREMIUM_MAX_DAYS = 3650
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

def _fmt_remaining_full(seconds):
    """شكل القائمة الرئيسية: 0 يوم، 23 ساعة، 55 دقيقة."""
    seconds = int(seconds)
    if seconds <= 0:
        return "انتهى"
    if seconds > 3_000_000_000:
        return "دائم ♾"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days} يوم، {hours} ساعة، {rem // 60} دقيقة"

def _fmt_remaining(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return "انتهى"
    if seconds > 3_000_000_000:
        return "دائم ♾"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days and hours:
        return f"{days} يوم و {hours} ساعة"
    if days:
        return f"{days} يوم"
    if hours and minutes:
        return f"{hours} ساعة و {minutes} دقيقة"
    if hours:
        return f"{hours} ساعة"
    return f"{max(1, minutes)} دقيقة"

def _send_to_user(user_id, text, markup=None):
    chat_ids = [int(user_id)]
    try:
        for c in db.list_connections_for_owner(int(user_id)):
            cid = c.get("owner_chat_id")
            if cid and int(cid) not in chat_ids:
                chat_ids.append(int(cid))
    except Exception:
        pass
    for cid in chat_ids:
        try:
            safe_send_message(cid, text, reply_markup=markup)
            return True
        except Exception:
            continue
    return False

def _adm_free_members_stats():
    admin_id = int(config.ADMIN_ID)
    members = {int(uid) for uid in db.list_member_ids()} - {admin_id}
    premium_ids = {int(r["user_id"]) for r in db.list_premium_users()}
    active_ids = {
        int(r["owner_user_id"]) for r in db.list_active_owner_chat_ids()
        if r.get("owner_user_id") is not None
    }
    trial_started = {}
    for key, value in db.list_settings("trial_started_at_"):
        try:
            trial_started[int(key[len("trial_started_at_"):])] = int(value)
        except (TypeError, ValueError):
            continue

    now = int(time.time())
    stats = {
        "total": len(members),
        "premium": len(members & premium_ids),
        "free": 0,
        "trial_running": 0,
        "trial_expired": 0,
        "no_trial_record": 0,
        "not_connected": 0,
    }
    for uid in members - premium_ids:
        stats["free"] += 1
        if uid not in active_ids:
            stats["not_connected"] += 1
            continue
        started = trial_started.get(uid)
        if started is None:
            stats["no_trial_record"] += 1
        elif TRIAL_DURATION_SECONDS - (now - started) <= 0:
            stats["trial_expired"] += 1
        else:
            stats["trial_running"] += 1
    return stats

def _adm_free_members_view():
    st = _adm_free_members_stats()
    lines = [
        f"{e('people_2')} <b>الأعضاء المجانيين</b>",
        "",
        f"{e('people_2')} <b>عدد المجانيين: {st['free']}</b>",
        "",
        f"{e('person')} إجمالي الأعضاء: <b>{st['total']}</b>",
        f"{e('star')} المميزين: <b>{st['premium']}</b>",
        "",
        "<b>تفصيل المجانيين:</b>",
        f"{e('clock')} تجربتهم شغالة: <b>{st['trial_running']}</b>",
        f"{e('lock')} تجربتهم خلصت: <b>{st['trial_expired']}</b>",
        f"{e('link')} لسه ما ربطوش حساب Business (أو فاصلينه): <b>{st['not_connected']}</b>",
    ]
    if st["no_trial_record"]:
        lines.append(f"{e('unlock')} حسابات قديمة مفتوحة من غير تجربة: <b>{st['no_trial_record']}</b>")
    lines += ["", "<i>المجاني = أي عضو مالوش اشتراك مميز شغال (من غير الأدمن والمحظورين).</i>"]
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" تحديث", callback_data="adm_free", icon="check", style="success"))
    markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
    return "\n".join(lines), markup

def _adm_premium_view(page):
    total = db.count_premium_users()
    pages = max(1, -(-total // ACC_PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    rows = db.list_premium_users(ACC_PAGE_SIZE, page * ACC_PAGE_SIZE)

    markup = types.InlineKeyboardMarkup()
    if not rows:
        text = f"{e('diamond')} <b>المميزون</b>\n\nمفيش مستخدمين مميزين دلوقتي."
    else:
        now = int(time.time())
        lines = []
        for i, r in enumerate(rows, start=page * ACC_PAGE_SIZE + 1):
            name = html_escape(r["name"] or "غير معروف")
            uname = f"@{html_escape(r['username'])}" if r["username"] else "بدون يوزر"
            lines.append(
                f"<b>{i}.</b> <a href=\"tg://user?id={r['user_id']}\">{name}</a> — {uname}\n"
                f"{e('person_2')} <b>ID:</b> <code>{r['user_id']}</code>\n"
                f"{e('clock')} المتبقي: <b>{_fmt_remaining(int(r['expires_at']) - now)}</b> · "
                f"ينتهي: <b>{_fmt_ts(r['expires_at'])}</b>"
            )
            label = (r["name"] or str(r["user_id"]))[:22]
            markup.add(pe.btn(
                f" حذف {label}", callback_data=f"adm_premdelq_{r['user_id']}",
                icon="trash", style="danger",
            ))
        text = f"{e('diamond')} <b>المميزون</b> ({total})\n\n" + "\n\n".join(lines)
        nav = []
        if page > 0:
            nav.append(pe.btn(" السابق", callback_data=f"adm_premlist_{page - 1}", icon="arrow_right", style="primary"))
        nav.append(pe.btn(f"{page + 1}/{pages}", callback_data="adm_noop"))
        if page < pages - 1:
            nav.append(pe.btn(" التالي", callback_data=f"adm_premlist_{page + 1}", icon="arrow_left", style="primary"))
        markup.row(*nav)
    markup.add(pe.btn(" رجوع", callback_data="adm_open", icon="arrow_left"))
    return text, markup

def adm_step_prem_user(message, mode):
    if _adm_step_common(message):
        return
    target = _adm_resolve_target(message)
    if not target:
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>مقدرتش أحدد المستخدم.</b> ابعت آيدي رقمي، أو @يوزر معروف للبوت، أو حوّل رسالة منه:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_prem_user, mode)
        return

    uid = target["user_id"]
    if is_admin(uid):
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>ده حساب الأدمن، مش محتاج اشتراك.</b> ابعت مستخدم تاني:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_prem_user, mode)
        return

    if mode == "del":
        _adm_premium_remove(message.chat.id, uid, target=target)
        return
    if mode == "perm":
        _adm_grant_premium(message.chat.id, target, None)
        return

    current = db.get_premium_user(uid)
    status = ""
    if current and int(current["expires_at"]) > time.time():
        status = (
            f"\n{e('clock')} <b>مميز حاليًا</b> — المتبقي "
            f"<b>{_fmt_remaining(int(current['expires_at']) - int(time.time()))}</b>. "
            "الأيام الجديدة هتتضاف عليه (تمديد).\n"
        )
    uname = f"@{html_escape(target['username'])}" if target["username"] else "لا يوجد"
    msg = safe_send_message(
        message.chat.id,
        f"{e('star')} <b>إضافة مميز</b>\n\n"
        f"{e('person')} <b>الاسم:</b> {html_escape(target['name'])}\n"
        f"{e('link')} <b>اليوزر:</b> {uname}\n"
        f"{e('person_2')} <b>ID:</b> <code>{uid}</code>\n"
        f"{status}\n"
        f"ابعت <b>عدد الأيام</b> (رقم من 1 لـ {PREMIUM_MAX_DAYS})، أو كلمة <b>دائم</b> لاشتراك بلا نهاية:",
        reply_markup=adm_cancel_markup(),
    )
    bot.register_next_step_handler(msg, adm_step_prem_days, target)

_PERM_WORDS = ("دائم", "دايم", "مدى الحياة", "للابد", "للأبد", "بلا نهاية", "لانهائي", "permanent", "forever", "unlimited")

def adm_step_prem_days(message, target):
    if _adm_step_common(message):
        return
    text = (message.text or "").strip().translate(_AR_DIGITS)
    if text.lower() in _PERM_WORDS:
        _adm_grant_premium(message.chat.id, target, None)
        return
    if not re.fullmatch(r"\d{1,4}", text) or not (1 <= int(text) <= PREMIUM_MAX_DAYS):
        msg = safe_send_message(
            message.chat.id,
            f"{e('cross')} <b>عدد الأيام لازم يكون رقم من 1 لـ {PREMIUM_MAX_DAYS}، أو كلمة «دائم».</b> جرب تاني:",
            reply_markup=adm_cancel_markup(),
        )
        bot.register_next_step_handler(msg, adm_step_prem_days, target)
        return
    _adm_grant_premium(message.chat.id, target, int(text))

def _adm_grant_premium(chat_id, target, days):
    permanent = days is None
    uid = target["user_id"]
    if permanent:
        result = db.add_premium_permanent(uid, target["name"], target["username"])
    else:
        result = db.add_premium_user(uid, target["name"], target["username"], days)
    expires_at = result["expires_at"]
    total_left = expires_at - int(time.time())
    days_label = "دائم ♾" if permanent else f"{days} يوم"
    _adm_log(chat_id, "اشتراك دائم" if permanent else f"اشتراك {days} يوم", uid, target["name"])

    notice = (
        f"{e('star')} <b>{'تم تمديد اشتراكك المميز.' if result['extended'] else 'تم تفعيل اشتراكك المميز.'}</b>\n\n"
        f"{e('clock')} <b>المدة:</b> {days_label}\n"
        f"{e('check')} <b>ينتهي في:</b> {_fmt_ts(expires_at)}\n"
        f"{e('diamond')} <b>المتبقي:</b> {_fmt_remaining(total_left)}"
    )
    panel = None
    try:
        if _owner_active_connection(uid):
            panel = owner_panel_markup(uid)
    except Exception:
        panel = None
    delivered = _send_to_user(uid, notice, panel)

    uname = f"@{html_escape(target['username'])}" if target["username"] else "لا يوجد"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        pe.btn(" حذف المميز", callback_data=f"adm_premdelq_{uid}", icon="trash", style="danger"),
        pe.btn(" فتح البروفايل", url=f"tg://user?id={uid}", icon="person_3", style="primary"),
    )
    markup.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_open", icon="admin_panel", style="primary"))
    safe_send_message(
        chat_id,
        f"{e('star')} <b>{'تم تمديد اشتراك المميز' if result['extended'] else 'تم تفعيل المميز'}</b>\n\n"
        f"{e('person')} <b>الاسم:</b> {html_escape(target['name'])}\n"
        f"{e('link')} <b>اليوزر:</b> {uname}\n"
        f"{e('person_2')} <b>ID:</b> <code>{uid}</code>\n"
        f"{e('clock')} <b>المدة:</b> {days_label}\n"
        f"{e('check')} <b>ينتهي في:</b> {_fmt_ts(expires_at)} ({_fmt_remaining(total_left)})\n\n"
        + (
            f"{e('bell')} وصله إشعار بالاشتراك."
            if delivered else
            f"{e('cross')} <b>مقدرتش أوصله إشعار</b> (لسه ما بدأش البوت). الاشتراك اتفعّل برضه."
        ),
        reply_markup=markup,
    )

def _adm_premium_remove(admin_chat_id, uid, target=None, edit_call=None):
    info = db.get_premium_user(uid) or {}
    name = info.get("name") or (target or {}).get("name") or "غير معروف"
    markup = types.InlineKeyboardMarkup()
    markup.add(pe.btn(" المميزين", callback_data="adm_premlist_0", icon="diamond", style="primary"))
    markup.add(pe.btn(" رجوع للوحة الأدمن", callback_data="adm_open", icon="admin_panel", style="primary"))

    removed = db.remove_premium_user(uid)
    if removed:
        _adm_log(admin_chat_id, "حذف اشتراك", uid, (target or {}).get("name", "") if isinstance(target, dict) else "")
    if not removed:
        text = f"{e('cross')} <b>المستخدم ده مش مميز.</b>\n{e('person_2')} <b>ID:</b> <code>{uid}</code>"
    else:
        _lock_after_premium(uid)
        _send_to_user(
            uid,
            f"{e('cross')} <b>تم إلغاء اشتراكك المميز.</b>\n"
            "<b>القوائم والإعدادات متاحة، لكن البوت مش هيعالج رسائل العملاء لحد تفعيل الاشتراك.</b>\n\n"
            + main_menu_text(uid),
            owner_panel_markup(uid),
        )
        text = (
            f"{e('trash')} <b>تم حذف المميز</b>\n\n"
            f"{e('person')} <b>الاسم:</b> {html_escape(name)}\n"
            f"{e('person_2')} <b>ID:</b> <code>{uid}</code>"
        )
    if edit_call is not None:
        _adm_edit(edit_call, text, markup)
    else:
        safe_send_message(admin_chat_id, text, reply_markup=markup)

def _premium_remind_soon():
    """تذكير واحد فقط خلال آخر 24 ساعة من الاشتراك المحدد؛ الاشتراك الدائم لا يُذكّر."""
    now = int(time.time())
    for row in db.list_premium_users():
        exp = int(row.get("expires_at") or 0)
        left = exp - now
        if exp >= 4_000_000_000 or left <= 0 or left > 86400:
            continue
        uid = row["user_id"]
        key = f"prem_remind_{uid}_{exp}"
        if db.get_setting(key, "0") == "1":
            continue
        db.set_setting(key, "1")
        _send_to_user(
            uid,
            f"{e('bell')} <b>اشتراكك المميز هيخلص قريب.</b>\n\n"
            f"{e('clock')} <b>المتبقي:</b> {_fmt_remaining(left)}\n"
            f"{e('check')} <b>ينتهي في:</b> {_fmt_ts(exp)}\n\n"
            "<b>تواصل مع المطور عشان تجدد قبل ما يتقفل عليك.</b>",
            developer_contact_keyboard(),
        )

def _premium_watcher_loop():
    while True:
        try:
            _premium_remind_soon()
        except Exception as exc:
            print(f"premium remind error: {exc}", flush=True)
        try:
            for row in db.pop_expired_premium():
                uid = row["user_id"]
                try:
                    _lock_after_premium(uid)
                except Exception as exc:
                    print(f"premium lock failed for {uid}: {exc}", flush=True)
                _send_to_user(
                    uid,
                    f"{e('clock')} <b>انتهى اشتراكك المميز.</b>\n"
                    "<b>القوائم والإعدادات متاحة، لكن البوت مش هيعالج رسائل العملاء لحد التجديد.</b>\n\n"
                    + main_menu_text(uid),
                    owner_panel_markup(uid),
                )
                uname = f"@{html_escape(row['username'])}" if row.get("username") else "لا يوجد"
                try:
                    safe_send_message(
                        config.ADMIN_ID,
                        f"{e('bell')} <b>انتهى اشتراك مميز</b>\n\n"
                        f"{e('person')} <b>الاسم:</b> {html_escape(row.get('name') or 'غير معروف')}\n"
                        f"{e('link')} <b>اليوزر:</b> {uname}\n"
                        f"{e('person_2')} <b>ID:</b> <code>{uid}</code>",
                    )
                except Exception as exc:
                    print(f"premium expiry admin notice failed: {exc}", flush=True)
        except Exception as exc:
            print(f"Premium watcher error: {exc}", flush=True)
        time.sleep(60)

def _bc_owner_banned(bc_id):
    if not _BANNED_IDS or not bc_id:
        return False
    owner = _BC_OWNER_CACHE.get(bc_id)
    if owner is None:
        conn = db.get_business_connection(bc_id)
        if not conn:
            return False
        owner = conn["owner_user_id"]
        _BC_OWNER_CACHE[bc_id] = owner
    return owner in _BANNED_IDS

def _track_user(user):
    if not user or getattr(user, "is_bot", False):
        return
    now = time.time()
    if now - _TRACKED_AT.get(user.id, 0) < 3600:
        return
    _TRACKED_AT[user.id] = now
    try:
        _is_new = not db.bot_user_exists(user.id)
        db.upsert_bot_user(user.id, display_name(user), user.username or "")
        if _is_new and not is_admin(user.id):
            _notify_new_user(user)
    except Exception as exc:
        print(f"track user failed: {exc}", flush=True)

def _notify_new_user(user):
    import html as _html
    try:
        uname = f"@{user.username}" if user.username else "لا يوجد"
        text = (
            "<b>👤 مستخدم جديد دخل البوت</b>\n\n"
            f"• <b>الاسم:</b> {_html.escape(display_name(user))}\n"
            f"• <b>اليوزر:</b> {_html.escape(uname)}\n"
            f"• <b>الأيدي:</b> <code>{user.id}</code>\n"
            f"• <b>إجمالي المستخدمين:</b> {db.count_bot_users()}"
        )
        bot.send_message(config.ADMIN_ID, text, parse_mode="HTML")
    except Exception as exc:
        print(f"new user notice failed: {exc}", flush=True)

def _screen_update(update):
    for name in ("business_message", "edited_business_message"):
        obj = getattr(update, name, None)
        if obj is not None:
            return not _bc_owner_banned(getattr(obj, "business_connection_id", None))
    obj = getattr(update, "deleted_business_messages", None)
    if obj is not None:
        return not _bc_owner_banned(getattr(obj, "business_connection_id", None))
    obj = getattr(update, "business_connection", None)
    if obj is not None:
        owner = getattr(obj, "user", None)
        if owner is not None and not is_admin(owner.id):
            if owner.id in _BANNED_IDS:
                return False
        return True

    msg = getattr(update, "message", None) or getattr(update, "edited_message", None)
    cq = getattr(update, "callback_query", None)
    user = None
    if msg is not None:
        user = msg.from_user
    elif cq is not None:
        user = cq.from_user
    else:
        for name in ("inline_query", "chosen_inline_result", "my_chat_member", "chat_member",
                     "chat_join_request", "pre_checkout_query", "shipping_query"):
            obj = getattr(update, name, None)
            if obj is not None:
                user = getattr(obj, "from_user", None)
                break

    if user is None or is_admin(user.id):
        return True

    if user.id in _BANNED_IDS:

        try:
            if cq is not None:
                bot.answer_callback_query(cq.id, BANNED_NOTICE_PLAIN, show_alert=True)
                chat = getattr(getattr(cq, "message", None), "chat", None)
                notice_chat = chat.id if chat is not None and getattr(chat, "type", "") == "private" else None
            elif msg is not None and getattr(msg.chat, "type", "") == "private":
                notice_chat = msg.chat.id
            else:
                notice_chat = None
            if notice_chat is not None and time.time() - _BAN_NOTICE_AT.get(user.id, 0) > 3:
                _BAN_NOTICE_AT[user.id] = time.time()
                bot.send_message(notice_chat, BANNED_NOTICE_HTML)
        except Exception:
            pass
        return False

    if (msg is not None and getattr(msg.chat, "type", "") == "private") or cq is not None:
        _track_user(user)
    return True

_orig_process_new_updates = bot.process_new_updates

_UPDATE_KINDS = ("message", "edited_message", "callback_query", "inline_query", "business_connection",
                 "business_message", "edited_business_message", "deleted_business_messages",
                 "my_chat_member", "chat_member", "chat_join_request", "pre_checkout_query")

def _process_new_updates_screened(updates):
    kept = []
    for upd in updates:
        try:
            _kind = next((k for k in _UPDATE_KINDS if getattr(upd, k, None) is not None), "other")
            _who = None
            for _k in ("message", "callback_query"):
                _o = getattr(upd, _k, None)
                if _o is not None and getattr(_o, "from_user", None) is not None:
                    _who = _o.from_user.id
            if _kind in ("message", "callback_query"):
                print(f"update id={upd.update_id} kind={_kind} from={_who}", flush=True)
        except Exception:
            pass
        try:
            ok = _screen_update(upd)
        except Exception as exc:
            print(f"update screening failed: {exc}", flush=True)
            ok = True
        if ok:
            kept.append(upd)
    if kept:
        _orig_process_new_updates(kept)

bot.process_new_updates = _process_new_updates_screened

def _handler_exception(exc):
    import traceback
    name = type(exc).__name__
    msg = str(exc)
    # أخطاء شبكة مؤقتة (timeout / انقطاع) - infinity_polling بيعيد المحاولة لوحده
    if name in ("ReadTimeout", "ReadTimeoutError", "ConnectTimeout", "ConnectionError",
                "ProtocolError", "ChunkedEncodingError", "SSLError", "TimeoutError", "ConnectionResetError"):
        print(f"network hiccup ({name}) - retrying", flush=True)
        return True
    if _is_benign_tg_error(exc):
        print(f"telegram notice: {msg[-160:]}", flush=True)
        return True
    print("HANDLER ERROR: " + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), flush=True)
    return True

bot.exception_handler = type("EH", (), {"handle": staticmethod(_handler_exception)})()

def _require_active_msg(message):
    uid = message.from_user.id
    if is_admin(uid):
        return True
    if not _owner_active_connection(uid):
        safe_send_message(message.chat.id, f"{e('cross')} حساب الـ Business مش متصل. اربطه من زر «ربط الحساب» في /start.")
        return False
    return True

_bn = binance_hub.register(bot, binance_hub.Ctx(
    e=e, pe=pe, types=types, html_escape=html_escape,
    send=safe_send_message, edit=safe_edit_message_text,
    send_business=lambda chat_id, bc_id, text, **kw: safe_send_business_message(chat_id, bc_id, text, **kw),
    delete_business_message=delete_business_message,
    require_active=_require_active_connection, require_active_msg=_require_active_msg,
    owner_connection=_owner_active_connection,
    panel=lambda uid: (main_menu_text(uid), owner_panel_markup(uid)),
    feature_on=lambda uid: owner_feature_on("binance_hub", uid),
    admin_id=int(config.ADMIN_ID),
))

def _sync_existing_connections():
    """On startup: verify every stored Business connection against Telegram, refresh its state,
    and for already-linked owners who never got the main menu, send it (no unlink/relink needed)."""
    try:
        rows = db.list_all_connections()
    except Exception as exc:
        print(f"startup sync: cannot list connections: {exc}", flush=True)
        return
    handled_owners = set()
    for row in rows:
        bc_id = row.get("business_connection_id")
        if not bc_id:
            continue
        try:
            bc = bot.get_business_connection(bc_id)
        except Exception as exc:
            msg = str(exc)
            if "BUSINESS_CONNECTION_INVALID" in msg or "connection not found" in msg.lower():
                db.set_connection_enabled(bc_id, False)
                _BC_OWNER_CACHE.pop(bc_id, None)
                print(f"startup sync: bc={bc_id} is gone, marked disabled", flush=True)
            else:
                print(f"startup sync: getBusinessConnection failed bc={bc_id}: {exc}", flush=True)
            time.sleep(0.3)
            continue
        owner = getattr(bc, "user", None)
        if owner is None:
            continue
        chat_id = getattr(bc, "user_chat_id", None) or owner.id
        enabled = bool(getattr(bc, "is_enabled", True))
        db.upsert_business_connection(
            bc_id=bc_id,
            owner_user_id=owner.id,
            owner_chat_id=chat_id,
            owner_name=display_name(owner),
            owner_username=owner.username or "",
            is_enabled=1 if enabled else 0,
            created_at=getattr(bc, "date", None) or row.get("created_at") or int(time.time()),
        )
        _BC_OWNER_CACHE.pop(bc_id, None)
        if not enabled or owner.id in handled_owners:
            continue
        handled_owners.add(owner.id)
        try:
            if owner.id in _BANNED_IDS:
                continue
            start_trial_if_needed(owner.id)
            if db.get_setting(_first_welcome_key(owner.id), "0") != "1":
                send_first_link_welcome(chat_id, owner.id, owner)
        except Exception as exc:
            print(f"startup sync: owner={owner.id} failed: {exc}", flush=True)
        time.sleep(0.5)
    print("startup sync: done", flush=True)

def _keep_alive(fn):
    """بيشغّل اللوب تاني لو وقع بأي خطأ (مع انتظار بسيط) بدل ما يموت للأبد."""
    def _runner(*a, **kw):
        delay = 5
        while True:
            try:
                fn(*a, **kw)
                return
            except Exception as exc:
                print(f"background loop {getattr(fn, '__name__', '?')} crashed, restarting: {exc}", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 120)
    _runner.__name__ = getattr(fn, "__name__", "loop")
    return _runner

# ---------- Uptime (Flask) لريندر: بيفتح بورت علشان الخدمة تفضل شغالة ----------
def _start_uptime_server():
    from flask import Flask
    import logging

    app = Flask("uptime")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.route("/")
    def _home():
        return "Bot is running", 200

    @app.route("/health")
    def _health():
        return "OK", 200

    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False),
        daemon=True, name="uptime-server",
    ).start()

    # بينج ذاتي كل 10 دقايق على رابط ريندر الخارجي (ريندر بيضيفه تلقائيًا في RENDER_EXTERNAL_URL)
    ext_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if ext_url:
        def _self_ping():
            while True:
                time.sleep(240)   # كل 4 دقايق (ريندر بينيّم الخدمة بعد 15 دقيقة من غير زيارات)
                try:
                    requests.get(ext_url + "/health", timeout=15)
                except Exception:
                    pass
        threading.Thread(target=_self_ping, daemon=True, name="uptime-ping").start()

if __name__ == "__main__":
    _start_uptime_server()
    proxy_pool.start()
    threading.Thread(target=_keep_alive(_sync_existing_connections), daemon=True, name="startup-bc-sync").start()
    threading.Thread(target=_keep_alive(_trial_watcher_loop), daemon=True, name="trial-watcher").start()
    threading.Thread(target=_keep_alive(_premium_watcher_loop), daemon=True, name="premium-watcher").start()
    threading.Thread(target=_keep_alive(_auto_backup_loop), daemon=True, name="auto-backup").start()
    threading.Thread(target=_keep_alive(_disconnect_purge_loop), daemon=True, name="disconnect-purge").start()
    threading.Thread(target=_keep_alive(_maintenance_loop), daemon=True, name="maintenance").start()
    _yt_ck_restore_from_db()
    try:
        cvt.start_warmer()
    except Exception as exc:
        print(f"currency warmer failed to start: {exc}", flush=True)
    if os.environ.get("YT_COOKIES_WATCH", "1") != "0":
        threading.Thread(target=_keep_alive(_yt_ck_watch_loop), daemon=True, name="yt-cookies-watch").start()
    _bn.start_background()
    try:
        _pending = bot.get_updates(timeout=0, limit=100, allowed_updates=["business_connection"])
        if _pending:
            print(f"startup: processing {len(_pending)} pending business_connection update(s)", flush=True)
            bot.process_new_updates(_pending)
    except Exception as exc:
        print(f"startup: pending business_connection scan failed: {exc}", flush=True)
    # IMPORTANT: the startup scan above calls getUpdates with allowed_updates=["business_connection"].
    # Telegram remembers that filter for later calls that don't specify one, so without an explicit
    # list here the bot would only receive business_connection updates (no /start, no buttons).
    _ALL_UPDATES = [
        "message", "edited_message", "callback_query", "inline_query", "chosen_inline_result",
        "business_connection", "business_message", "edited_business_message",
        "deleted_business_messages", "pre_checkout_query", "shipping_query",
        "my_chat_member", "chat_member", "chat_join_request",
    ]
    _restart_delay = 3
    _first_run = True
    while True:
        try:
            bot.infinity_polling(
                skip_pending=_first_run, timeout=30, long_polling_timeout=25, allowed_updates=_ALL_UPDATES,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"polling crashed, restarting in {_restart_delay}s: {exc}", flush=True)
        _first_run = False
        time.sleep(_restart_delay)
        _restart_delay = min(_restart_delay * 2, 60)
