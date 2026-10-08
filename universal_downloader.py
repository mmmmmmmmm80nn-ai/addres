"""أداة تحميل الفيديوهات والوسائط من منصات كتير (مبنية على yt-dlp).

- الروابط المباشرة (بدون أمر) بتتقبل بس من الدومينات الموجودة في PLATFORMS.
- الأمر `.تحميل <رابط>` / `.تنزيل <رابط>` (أو الرد على رسالة فيها رابط) بيجرب أي رابط،
  لأن yt-dlp بيدعم مئات المواقع غير اللي في القايمة.
"""
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urlparse

import yt_dlp

MAX_FILE_BYTES = int(os.environ.get("DOWNLOAD_MAX_MB", "50")) * 1024 * 1024

# اسم المنصة -> الدومينات (أي دومين فرعي بيتقبل)
PLATFORMS = {
    "TikTok": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    "Instagram": ("instagram.com", "instagr.am"),
    "Facebook": ("facebook.com", "fb.watch", "fb.com"),
    "YouTube": ("youtube.com", "youtu.be", "music.youtube.com"),
    "Twitter (X)": ("twitter.com", "x.com", "t.co"),
    "Pinterest": ("pinterest.com", "pin.it"),
    "Snapchat": ("snapchat.com",),
    "Reddit": ("reddit.com", "redd.it", "v.redd.it"),
    "Threads": ("threads.net", "threads.com"),
    "LinkedIn": ("linkedin.com",),
    "Likee": ("likee.video", "likee.com"),
    "Kwai": ("kwai.com", "kw.ai", "kwai-video.com"),
    "Vimeo": ("vimeo.com",),
    "Dailymotion": ("dailymotion.com", "dai.ly"),
    "Twitch": ("twitch.tv", "clips.twitch.tv"),
    "Tumblr": ("tumblr.com",),
    "Bilibili": ("bilibili.com", "b23.tv"),
    "VK": ("vk.com", "vkvideo.ru"),
    "OK.ru": ("ok.ru",),
    "Rumble": ("rumble.com",),
    "Streamable": ("streamable.com",),
    "Imgur": ("imgur.com",),
    "9GAG": ("9gag.com",),
    "Bluesky": ("bsky.app",),
    "SoundCloud": ("soundcloud.com", "on.soundcloud.com"),
    "Bandcamp": ("bandcamp.com",),
    "Mixcloud": ("mixcloud.com",),
    "Douyin": ("douyin.com",),
    "Weibo": ("weibo.com", "weibo.cn"),
    "Odysee": ("odysee.com",),
    "Loom": ("loom.com",),
    "Coub": ("coub.com",),
    "Flickr": ("flickr.com",),
    "Lemon8": ("lemon8-app.com",),
}

# روابط تيليجرام عمرها ما بتتحمّل (حتى مع أمر .تحميل / تنزيل)
_TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.dog", "telesco.pe")


def is_telegram_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == h or host.endswith("." + h) for h in _TELEGRAM_HOSTS)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_CMD_RE = re.compile(r"^[./]?(?:تحميل|تنزيل)(?:\s+(صوت|فيديو))?(?:\s+(.*))?$", re.S)

_DOMAIN_TO_NAME = {}
for _name, _domains in PLATFORMS.items():
    for _d in _domains:
        _DOMAIN_TO_NAME[_d] = _name


def extract_url(text):
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(").,،") if m else None


def platform_of(url):
    """اسم المنصة لو الدومين معروف، وإلا None."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in _DOMAIN_TO_NAME:
            return _DOMAIN_TO_NAME[cand]
    return None


def extract_supported_url(text):
    """Return the first supported social URL embedded anywhere in a message."""
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(").,،؛!?؟]}»")
        if platform_of(url) and not is_telegram_url(url):
            return url
    return None


def parse_request(text, replied_text=None):
    """يرجع (mode, url, explicit) أو None.

    mode: "video" | "audio". explicit: True لو المستخدم كتب الأمر (.تحميل / .تنزيل)
    فنجرب أي موقع، وإلا بنقبل بس المنصات المعروفة.
    """
    text = (text or "").strip()
    m = _CMD_RE.match(text)
    if m:
        mode = "audio" if m.group(1) == "صوت" else "video"
        url = extract_url(m.group(2) or "") or extract_url(replied_text or "")
        if url and is_telegram_url(url):
            return None
        return (mode, url, True) if url else None
    # روابط المنصات المعروفة تبدأ التنزيل مباشرة، حتى لو معها كلام أو علامة ترقيم.
    url = extract_supported_url(text)
    if url:
        prefix = re.match(r"^(صوت|فيديو)(?:\s+|$)", text, re.IGNORECASE)
        mode = "audio" if prefix and prefix.group(1) == "صوت" else "video"
        return mode, url, False
    return None


def supported_platforms_text():
    return " · ".join(PLATFORMS)


def download(url, mode="video", quality=None):
    """ينزّل الرابط ويرجع (filepath, title). يرفع RuntimeError برسالة مفهومة."""
    if is_telegram_url(url):
        raise RuntimeError("روابط تيليجرام مش مدعومة للتحميل.")
    if mode == "audio" and platform_of(url) == "SoundCloud":
        path, meta = download_soundcloud(url)
        return path, meta["title"]
    out_dir = os.path.join(tempfile.gettempdir(), "universal_downloads")
    os.makedirs(out_dir, exist_ok=True)
    stamp = uuid.uuid4().hex[:12]
    mb = MAX_FILE_BYTES // (1024 * 1024)
    opts = {
        "outtmpl": os.path.join(out_dir, f"dl_{stamp}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "max_filesize": MAX_FILE_BYTES,
    }
    if mode == "audio":
        opts.update({
            "format": f"bestaudio[filesize<{mb}M]/bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                 "preferredquality": "192"}],
        })
    else:
        # أعلى جودة داخل حد الحجم، مع أفضلية للصيغ اللي تليجرام بيشغّلها (mp4/h264).
        # download_addr = نسخة تيك توك بدون علامة مائية لو متاحة.
        opts.update({
            "format": (
                f"download_addr[filesize<{mb}M]/"
                f"bestvideo[ext=mp4][filesize<{mb}M]+bestaudio[ext=m4a]/"
                f"bestvideo[filesize<{mb}M]+bestaudio/"
                f"best[filesize<{mb}M]/"
                "best[height<=720]/best"
            ),
            "format_sort": ["res", "fps", "codec:h264", "size"],
            "merge_output_format": "mp4",
        })
        if quality:
            q = int(quality)
            opts["format"] = (
                f"bestvideo[height<={q}][ext=mp4][filesize<{mb}M]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={q}][filesize<{mb}M]+bestaudio/"
                f"best[height<={q}][filesize<{mb}M]/best[height<={q}]/best"
            )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if "Unsupported URL" in msg:
            raise RuntimeError("الرابط ده من موقع مش مدعوم.") from exc
        if "larger than max-filesize" in msg or "File is larger" in msg:
            raise RuntimeError(f"الملف أكبر من {mb}MB.") from exc
        if any(k in msg.lower() for k in ("login", "private", "sign in", "cookies")):
            raise RuntimeError("المحتوى خاص أو محتاج تسجيل دخول.") from exc
        raise RuntimeError(msg[:200]) from exc

    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    title = ((info or {}).get("title") or platform_of(url) or "Video")[:180]

    want = (".mp3",) if mode == "audio" else (".mp4", ".mov", ".webm", ".mkv")
    files = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if stamp in f]
    files.sort(key=lambda p: os.path.splitext(p)[1].lower() not in want)
    for f in files:
        if os.path.isfile(f) and os.path.getsize(f) > 0:
            if os.path.getsize(f) > MAX_FILE_BYTES:
                os.remove(f)
                raise RuntimeError(f"الملف أكبر من {mb}MB.")
            return f, title
    raise RuntimeError("لم يتم العثور على الملف بعد التحميل")


# ---------------------------------------------------------------------------
# ساوندكلاود: مسار سريع (هدفه أقل من ~6 ثواني لأغنية عادية)
#  - استخراج واحد بس (من غير extract مرتين)
#  - بنختار نسخة الـ mp3 الجاهزة (http) فمفيش أي إعادة ترميز
#  - لو الصيغة m4a/aac بنبعتها زي ما هي (تليجرام بيشغّلها) بدل ما نحوّلها
#  - التحويل بـ ffmpeg مجرد حل أخير (opus/ogg/webm) وبإعدادات سريعة
# ---------------------------------------------------------------------------
_SC_FORMAT = (
    "bestaudio[ext=mp3][protocol^=http]/"
    "bestaudio[ext=mp3]/"
    "bestaudio[ext=m4a]/bestaudio[ext=aac]/"
    "bestaudio/best"
)
_TG_AUDIO_EXTS = (".mp3", ".m4a", ".aac")


def download_soundcloud(url):
    """ينزّل رابط ساوندكلاود صوت. يرجع (filepath, meta) و meta فيها title/artist/duration."""
    out_dir = os.path.join(tempfile.gettempdir(), "universal_downloads")
    os.makedirs(out_dir, exist_ok=True)
    stamp = uuid.uuid4().hex[:12]
    mb = MAX_FILE_BYTES // (1024 * 1024)
    opts = {
        "outtmpl": os.path.join(out_dir, f"sc_{stamp}.%(ext)s"),
        "format": _SC_FORMAT,
        "noplaylist": True, "quiet": True, "no_warnings": True, "restrictfilenames": True,
        "socket_timeout": 15, "retries": 2, "fragment_retries": 2,
        "max_filesize": MAX_FILE_BYTES,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,
        "noprogress": True, "nopart": True, "updatetime": False,
        "writethumbnail": False, "writeinfojson": False, "writedescription": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if "larger than max-filesize" in msg or "File is larger" in msg:
            raise RuntimeError(f"الأغنية أكبر من {mb}MB.") from exc
        if any(k in msg.lower() for k in ("login", "private", "sign in", "geo", "not available", "403", "404")):
            raise RuntimeError("الأغنية دي مش متاحة للتحميل.") from exc
        raise RuntimeError(msg[:200]) from exc

    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    info = info or {}

    files = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith(f"sc_{stamp}")]
    files = [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0]
    if not files:
        raise RuntimeError("لم يتم العثور على الملف بعد التحميل")
    src = next((f for f in files if f.lower().endswith(_TG_AUDIO_EXTS)), files[0])
    for f in files:
        if f != src and os.path.exists(f):
            os.remove(f)

    if not src.lower().endswith(_TG_AUDIO_EXTS):
        # حل أخير: صيغة تليجرام مش بيشغّلها كصوت (opus/ogg/webm)
        if not shutil.which("ffmpeg"):
            raise RuntimeError("السيرفر محتاج ffmpeg لتحويل الأغنية لـ mp3.")
        dst = os.path.join(out_dir, f"sc_{stamp}_conv.mp3")
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-threads", "0", "-i", src, "-vn",
                            "-codec:a", "libmp3lame", "-b:a", "160k", dst],
                           check=True, timeout=120)
        finally:
            if os.path.exists(src):
                os.remove(src)
        src = dst

    if os.path.getsize(src) > MAX_FILE_BYTES:
        os.remove(src)
        raise RuntimeError(f"الأغنية أكبر من {mb}MB.")
    meta = {
        "title": (info.get("title") or "SoundCloud")[:180],
        "artist": (info.get("uploader") or info.get("artist") or "")[:100],
        "duration": int(info["duration"]) if info.get("duration") else None,
    }
    return src, meta


# ---------------------------------------------------------------------------
# تحميل أغنية بالاسم من SoundCloud:  «تحميل <اسم الأغنية>»  -> ملف mp3
# ---------------------------------------------------------------------------
_SONG_RE = re.compile(r"^[./]?تحميل\s+(.{2,100})$", re.S)
_SONG_PREFIX_RE = re.compile(r"^(?:اغنيه|اغنية|أغنية|أغنيه|song|صوت)\s+", re.I)


class SongNotFound(RuntimeError):
    pass


def parse_song_request(text):
    """'تحميل سيدي منصور' -> 'سيدي منصور'. None لو فيه رابط (ده بيروح للتحميل العادي) أو مفيش اسم."""
    m = _SONG_RE.match((text or "").strip())
    if not m:
        return None
    q = m.group(1).strip()
    if extract_url(q):
        return None
    q = _SONG_PREFIX_RE.sub("", q).strip()
    if len(q) < 2 or q in ("صوت", "فيديو"):
        return None
    return q


def _sc_search(query):
    """بحث خفيف (بدون تحميل). يرجع أول نتيجة كاملة (مش مقطع 30 ثانية)."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "socket_timeout": 15, "retries": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"scsearch5:{query}", download=False)
    entries = [e for e in ((res or {}).get("entries") or []) if e and (e.get("url") or e.get("webpage_url"))]
    for e in entries:
        d = e.get("duration")
        if d is None or d >= 60:
            return e
    return entries[0] if entries else None


def download_song(query):
    """يبحث في SoundCloud وينزّل أول نتيجة mp3. يرجع (filepath, meta).

    السرعة: بنستخدم نسخة الـ mp3 الجاهزة من SoundCloud (http_mp3) فمفيش تحويل،
    ولو مش موجودة بنحوّل بـ ffmpeg.
    """
    entry = _sc_search(query)
    if not entry:
        raise SongNotFound("ملقيتش الأغنية دي على SoundCloud.")
    url = entry.get("url") or entry.get("webpage_url")

    out_dir = os.path.join(tempfile.gettempdir(), "universal_downloads")
    os.makedirs(out_dir, exist_ok=True)
    stamp = uuid.uuid4().hex[:12]
    mb = MAX_FILE_BYTES // (1024 * 1024)
    opts = {
        "outtmpl": os.path.join(out_dir, f"song_{stamp}.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True, "restrictfilenames": True,
        "socket_timeout": 20, "retries": 2, "fragment_retries": 2,
        "max_filesize": MAX_FILE_BYTES, "concurrent_fragment_downloads": 4,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            fmts = info.get("formats") or []
            has_mp3 = any(f.get("ext") == "mp3" and f.get("protocol") in ("http", "https") for f in fmts)
            ydl.params["format"] = (
                "bestaudio[ext=mp3][protocol^=http]/bestaudio[ext=mp3]/bestaudio/best" if has_mp3
                else "bestaudio/best"
            )
            info = ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if "larger than max-filesize" in msg or "File is larger" in msg:
            raise RuntimeError(f"الأغنية أكبر من {mb}MB.") from exc
        if any(k in msg.lower() for k in ("login", "private", "sign in", "geo", "not available", "403")):
            raise SongNotFound("الأغنية دي مش متاحة للتحميل.") from exc
        raise RuntimeError(msg[:200]) from exc

    files = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if stamp in f]
    files = [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0]
    if not files:
        raise RuntimeError("لم يتم العثور على الملف بعد التحميل")
    src = next((f for f in files if f.lower().endswith(".mp3")), files[0])
    if not src.lower().endswith(".mp3"):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("السيرفر محتاج ffmpeg لتحويل الأغنية لـ mp3.")
        dst = os.path.join(out_dir, f"song_{stamp}_conv.mp3")
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn",
                            "-codec:a", "libmp3lame", "-b:a", "192k", dst],
                           check=True, timeout=120)
        finally:
            for f in files:
                if f != dst and os.path.exists(f):
                    os.remove(f)
        src = dst
    else:
        for f in files:
            if f != src and os.path.exists(f):
                os.remove(f)
    if os.path.getsize(src) > MAX_FILE_BYTES:
        os.remove(src)
        raise RuntimeError(f"الأغنية أكبر من {mb}MB.")
    info = info or {}
    meta = {
        "title": (info.get("title") or entry.get("title") or query)[:180],
        "artist": (info.get("uploader") or info.get("artist") or "")[:100],
        "duration": int(info["duration"]) if info.get("duration") else None,
    }
    return src, meta
