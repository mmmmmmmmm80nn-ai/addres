import os


def _load_dotenv(path):
    """تحميل بسيط لملف .env (بدون مكتبات إضافية). المتغيرات الموجودة فعلًا في البيئة لها الأولوية."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and not os.environ.get(key):
                    os.environ[key] = value
    except FileNotFoundError:
        pass


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# بنقرا .env وبعده bot.env (ملف ظاهر، مفيد لو الاستضافة بتتجاهل الملفات المخفية)
for _name in (".env", "bot.env"):
    _load_dotenv(os.path.join(_BASE_DIR, _name))


def _float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


# ---------------------------------------------------------------------------
# الأسرار: القيم الافتراضية تحت، وممكن تتغير من متغيرات البيئة أو من ملف .env
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8868410601:AAGcmX5lW7Se5gOG6E_MY31oTTLZr0IPXoQ").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN مش متظبّط. حطه في متغيرات البيئة أو في ملف .env جنب config.py "
        "(شوف .env.example)."
    )

ADMIN_ID = _int_env("ADMIN_ID", 8386397923)
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID مش متظبّط. حط الـ ID بتاعك في متغيرات البيئة أو في ملف .env.")

DEVELOPER_ID = _int_env("DEVELOPER_ID", ADMIN_ID)

CHANNEL_URL = os.environ.get("CHANNEL_URL", "").strip()

DB_PATH = os.environ.get("DB_PATH", "bot_database.db")

TONAPI_KEY = os.environ.get("TONAPI_KEY", "").strip()

# أسعار النجوم بالدولار للنجمة الواحدة (عدّلها لو اتغيرت)
STARS_BUY_USD = _float_env("STARS_BUY_USD", 0.017)
STARS_WITHDRAW_USD = _float_env("STARS_WITHDRAW_USD", 0.013)
# أسعار بريميوم بالدولار (بتتستخدم لو الجلب من Fragment فشل)
PREMIUM_USD = {
    3: _float_env("PREMIUM_USD_3", 11.99),
    6: _float_env("PREMIUM_USD_6", 15.99),
    12: _float_env("PREMIUM_USD_12", 28.99),
}


# سعر الجنيه: "official" = السعر الرسمي | "p2p" = سعر سوق Binance P2P اللحظي
EGP_RATE_MODE = os.environ.get("EGP_RATE_MODE", "p2p").strip().lower()
# لو عايز تثبّت سعر الدولار بالجنيه بإيدك (مثلاً 53.0). 0 = أوتوماتيك
EGP_USD_OVERRIDE = _float_env("EGP_USD_OVERRIDE", 0)
