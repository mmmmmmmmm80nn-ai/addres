"""Read-only scanner for publicly accessible Telegram channel preview pages.

This intentionally does not log in to Telegram and cannot inspect private channels
or guarantee coverage of an entire channel history. It searches a bounded number
of recent public posts and reports source links as evidence, not as a verdict.
"""
from __future__ import annotations

from html.parser import HTMLParser
from html import unescape
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import time

import requests


_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_ID_RE = re.compile(r"^[1-9][0-9]{3,19}$")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_public_channel(value: str) -> str:
    """Return @username for a public channel link/username, else raise ValueError."""
    value = (value or "").strip()
    if not value:
        raise ValueError("أدخل يوزر قناة عامة أو رابطها.")
    if value.startswith("@"): 
        name = value[1:]
    elif "://" in value or value.lower().startswith("t.me/"):
        parsed = urlparse(value if "://" in value else "https://" + value)
        if (parsed.hostname or "").lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            raise ValueError("الرابط يجب أن يكون من t.me لقناة عامة.")
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0].lower() == "s":
            parts = parts[1:]
        if len(parts) != 1 or parts[0].startswith(("+", "joinchat")) or parts[0].lower() == "c":
            raise ValueError("روابط الدعوة والقنوات الخاصة غير مدعومة؛ أرسل رابط القناة العامة.")
        name = parts[0]
    else:
        name = value
    if not _CHANNEL_RE.fullmatch(name):
        raise ValueError("يوزر القناة غير صالح. استخدم @channel أو رابط t.me/channel عام.")
    return "@" + name


def normalize_target(value: str) -> dict:
    """Parse a numeric Telegram user ID or public username."""
    value = (value or "").strip().translate(_DIGIT_TRANSLATION)
    if re.fullmatch(r"[0-9][0-9\s.,،_\-‐‑–—\u200b]*", value):
        value = re.sub(r"[\s.,،_\-‐‑–—\u200b]+", "", value)
    if not value:
        raise ValueError("أدخل @username أو رقم ID.")
    if value.startswith("https://t.me/") or value.startswith("http://t.me/") or value.startswith("t.me/"):
        parsed = urlparse(value if "://" in value else "https://" + value)
        if (parsed.hostname or "").lower() not in {"t.me", "www.t.me"}:
            raise ValueError("رابط المستخدم يجب أن يكون من t.me.")
        parts = [p for p in parsed.path.split("/") if p]
        value = parts[0] if len(parts) == 1 else ""
    if value.startswith("@"): 
        value = value[1:]
    if _ID_RE.fullmatch(value):
        return {"kind": "id", "value": value}
    if _USERNAME_RE.fullmatch(value):
        return {"kind": "username", "value": value.lower()}
    raise ValueError("القيمة غير صالحة؛ استخدم @username أو رقم ID رقميًا موجبًا.")


class _PostParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self.current = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.current is None:
            data_post = attrs.get("data-post") if tag == "div" else None
            if data_post:
                post_id = None
                m = re.search(r"/(\d+)$", data_post)
                if m:
                    post_id = int(m.group(1))
                self.current = {"id": post_id, "text_parts": [], "links": []}
                self.depth = 1
            return
        if tag == "a" and attrs.get("href"):
            self.current["links"].append(attrs["href"])
        if tag not in _VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.current is not None and tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.current["links"].append(href)

    def handle_endtag(self, tag):
        if self.current is None or tag in _VOID_TAGS:
            return
        self.depth -= 1
        if self.depth <= 0:
            post = self.current
            post["text"] = " ".join(" ".join(post.pop("text_parts")).split())
            self.posts.append(post)
            self.current = None
            self.depth = 0

    def handle_data(self, data):
        if self.current is not None and data:
            self.current["text_parts"].append(data)


def _channel_title(page: str, fallback: str) -> str:
    patterns = (
        r"<meta\b[^>]*property=[\"']og:title[\"'][^>]*content=[\"']([^\"']*)",
        r"<meta\b[^>]*content=[\"']([^\"']*)[\"'][^>]*property=[\"']og:title[\"']",
        r"<div\b[^>]*class=[\"'][^\"']*tgme_page_title[^\"']*[\"'][^>]*>(.*?)</div>",
    )
    for pattern in patterns:
        m = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"<[^>]+>", " ", m.group(1))
            title = " ".join(unescape(title).split())
            if title:
                return title[:120]
    return "@" + fallback


def _is_match(post: dict, target: dict) -> bool:
    if target["kind"] == "id":
        value = target["value"]
        text = post.get("text", "").translate(_DIGIT_TRANSLATION)
        if re.search(r"(?<!\d)" + re.escape(value) + r"(?!\d)", text):
            return True
        separator = r"[\s.,،_\-‐‑–—\u200b]{0,3}"
        spaced_id = separator.join(re.escape(digit) for digit in value)
        if re.search(r"(?<!\d)" + spaced_id + r"(?!\d)", text):
            return True
        for href in post.get("links", []):
            parsed = urlparse(href)
            query_id = parse_qs(parsed.query).get("id", [])
            if any(str(x).translate(_DIGIT_TRANSLATION) == value for x in query_id):
                return True
            if re.search(r"(?:tg://user\?id=|user\?id=)" + re.escape(value) + r"(?:\D|$)", href, re.IGNORECASE):
                return True
        return False

    username = target["value"]
    pattern = re.compile(r"(?<![A-Za-z0-9_])@?" + re.escape(username) + r"(?![A-Za-z0-9_])", re.IGNORECASE)
    if pattern.search(post.get("text", "")):
        return True
    for href in post.get("links", []):
        parsed = urlparse(href)
        host = (parsed.hostname or "").lower()
        parts = [p for p in parsed.path.split("/") if p]
        if host in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"} and parts and parts[0].lower() == username:
            return True
    return False


def scan_channels(channels, target, *, max_requests=12, max_posts=240, deadline_seconds=45,
                  timeout=(2, 4), max_pages_per_channel=None, max_workers=8) -> dict:
    """Search recent public posts in fair, parallel rounds across every configured channel.

    `target` may be one normalized target or a list (username and numeric ID). A channel's
    newest page is attempted before any channel advances to an older page. The request
    worker count is bounded; channel count itself is not capped here.
    """
    if isinstance(target, dict):
        targets = [target]
    elif isinstance(target, (list, tuple)):
        targets = [item if isinstance(item, dict) else normalize_target(str(item)) for item in target]
    else:
        targets = [normalize_target(str(target))]
    if not targets:
        raise ValueError("يجب توفير يوزر أو ID صالح للفحص.")

    canonical, seen_channels = [], set()
    for channel in channels or []:
        try:
            item = normalize_public_channel(str(channel))
        except ValueError:
            continue
        if item.lower() not in seen_channels:
            canonical.append(item)
            seen_channels.add(item.lower())

    hits, errors, checked = [], [], []
    checked_set = set()
    request_count = 0
    logical_pages = 0
    post_count = 0
    hits_total = 0
    error_count = 0
    truncated = False
    max_requests = max(0, int(max_requests))
    max_posts = max(0, int(max_posts))
    deadline = time.monotonic() + max(1, float(deadline_seconds))
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.8",
    }
    page_limit = min(20, max(1, int(max_pages_per_channel))) if max_pages_per_channel is not None else 20
    worker_count = max(1, min(16, int(max_workers), len(canonical) or 1))
    states = {
        channel: {"name": channel[1:], "before": None, "seen": set(), "done": False, "checked": False}
        for channel in canonical
    }
    stored_per_channel = {}
    stop_all = False

    def fetch_page(channel, before):
        name = channel[1:]
        url = f"https://t.me/s/{name}"
        if before:
            url += f"?before={before}"
        attempts = 0
        for attempt in range(2):
            attempts += 1
            try:
                response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
                if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    if response.status_code == 429:
                        try:
                            delay = float(response.headers.get("Retry-After", "0.3"))
                        except (TypeError, ValueError):
                            delay = 0.3
                        time.sleep(min(2.0, max(0.2, delay)))
                    else:
                        time.sleep(0.25)
                    continue
                if response.status_code != 200:
                    return {"attempts": attempts, "error": f"HTTP {response.status_code}"}
                page = response.text[:1_500_000]
                parser = _PostParser()
                parser.feed(page)
                parser.close()
                if not parser.posts:
                    return {"attempts": attempts, "error": "لا توجد منشورات عامة قابلة للقراءة"}
                return {
                    "attempts": attempts,
                    "posts": parser.posts,
                    "title": _channel_title(page, name),
                }
            except requests.RequestException as exc:
                if attempt == 0:
                    time.sleep(0.15)
                    continue
                return {"attempts": attempts, "error": f"فشل الوصول ({type(exc).__name__})"}
            except Exception as exc:
                return {"attempts": attempts, "error": f"تعذر تحليل الصفحة ({type(exc).__name__})"}
        return {"attempts": attempts, "error": "تعذر جلب الصفحة بعد إعادة المحاولة"}

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tg-channel-scan") as pool:
        for page_index in range(page_limit):
            pending = [channel for channel in canonical if not states[channel]["done"]]
            if not pending:
                break
            batch_size = worker_count * 4
            for offset in range(0, len(pending), batch_size):
                if time.monotonic() >= deadline or logical_pages >= max_requests:
                    truncated = True
                    stop_all = True
                    break
                batch = pending[offset:offset + batch_size]
                batch = batch[:max_requests - logical_pages]
                if not batch:
                    truncated = True
                    stop_all = True
                    break
                futures = {
                    pool.submit(fetch_page, channel, states[channel]["before"]): channel
                    for channel in batch
                }
                logical_pages += len(batch)
                for future in as_completed(futures):
                    channel = futures[future]
                    state = states[channel]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = {"attempts": 1, "error": f"فشل الوصول ({type(exc).__name__})"}
                    request_count += outcome.get("attempts", 1)
                    if "error" in outcome:
                        error_count += 1
                        if len(errors) < 100:
                            errors.append({"channel": channel, "error": outcome["error"]})
                        state["done"] = True
                        continue
                    posts = outcome["posts"]
                    state["checked"] = True
                    if channel not in checked_set:
                        checked.append(channel)
                        checked_set.add(channel)
                    title = outcome["title"]
                    for post in posts:
                        if post_count >= max_posts:
                            truncated = True
                            stop_all = True
                            break
                        post_count += 1
                        if any(_is_match(post, item) for item in targets):
                            hits_total += 1
                            if len(hits) < 200 and stored_per_channel.get(channel, 0) < 5:
                                text = re.sub(r"\s+", " ", post.get("text", "")).strip()
                                post_id = post.get("id")
                                hits.append({
                                    "channel": channel,
                                    "channel_title": title,
                                    "post_id": post_id,
                                    "url": f"https://t.me/{state['name']}/{post_id}" if post_id else f"https://t.me/s/{state['name']}",
                                    "snippet": text[:240],
                                })
                                stored_per_channel[channel] = stored_per_channel.get(channel, 0) + 1
                    ids = [post["id"] for post in posts if isinstance(post.get("id"), int)]
                    if not ids:
                        state["done"] = True
                        continue
                    next_before = min(ids)
                    if next_before in state["seen"] or (
                        state["before"] is not None and next_before >= state["before"]
                    ):
                        state["done"] = True
                        continue
                    state["seen"].add(next_before)
                    state["before"] = next_before
                    if page_index + 1 >= page_limit:
                        truncated = True
                        state["done"] = True
                if stop_all:
                    break
            if stop_all or time.monotonic() >= deadline:
                truncated = True
                break
            if all(state["done"] for state in states.values()):
                break

    if any(not state["done"] for state in states.values()):
        truncated = True
    return {
        "hits": hits,
        "hits_total": hits_total,
        "hits_limited": hits_total > len(hits),
        "checked_channels": checked,
        "total_channels": len(canonical),
        "errors": errors,
        "error_count": error_count,
        "truncated": truncated,
        "requests": request_count,
        "pages": logical_pages,
        "posts": post_count,
    }
