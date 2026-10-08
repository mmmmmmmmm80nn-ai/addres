"""بيانات محفظة TON (X-Ray): الرصيد + آخر المعاملات (وارد/صادر، المبلغ، الميمو، الأطراف، الوقت، الهاش).

مبني على سكريبت Mo_Ton_X-Ray لكن للبوت: بيرجع نتايج منظمة بدل الطباعة في الترمنال.
"""
import base64
import re
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://tonapi.io/v2"

_USER_FRIENDLY = re.compile(r"(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_\-]{46}")
_RAW = re.compile(r"-?[01]:[0-9a-fA-F]{64}")


def is_address(text):
    text = (text or "").strip()
    return bool(_USER_FRIENDLY.fullmatch(text) or _RAW.fullmatch(text))


def _headers(api_key=None):
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _get(path, api_key=None, params=None, timeout=12):
    import time as _time
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{API_BASE}{path}", headers=_headers(api_key), params=params, timeout=(5, timeout)
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in (429, 500, 502, 503, 504):
                return None
        except Exception:
            pass
        _time.sleep(1.3 * (attempt + 1))
    return None


def fetch_balance(address, api_key=None):
    data = _get(f"/accounts/{address}", api_key)
    if data is None:
        return None
    return int(data.get("balance", 0)) / 1e9, data


def fetch_transactions(address, api_key=None, limit=6):
    data = _get(f"/blockchain/accounts/{address}/transactions", api_key, params={"limit": limit})
    if data is None:
        return None
    return data.get("transactions", [])


def _addr(obj, default="—"):
    if isinstance(obj, dict):
        return obj.get("address") or default
    return obj or default


def _message_text(msg):
    if not msg:
        return "—"
    decoded = msg.get("decoded_body")
    if isinstance(decoded, dict):
        if decoded.get("text"):
            return str(decoded["text"])
        if "comment" in decoded:
            return str(decoded["comment"])
    raw_body = msg.get("raw_body")
    if raw_body:
        try:
            raw = base64.b64decode(raw_body)
            if len(raw) > 4 and raw[:4] == b"\x00\x00\x00\x00":
                return raw[4:].decode("utf-8", errors="ignore") or "—"
            clean = "".join(ch for ch in raw.decode("utf-8", errors="ignore") if ch.isprintable())
            if clean:
                return clean
        except Exception:
            pass
    op = msg.get("decoded_op_name")
    return f"[Op: {op}]" if op else "—"


def parse_transaction(tx, wallet, tz_hours=3):
    utime = tx.get("utime", 0)
    when = "N/A"
    if utime:
        local = datetime.fromtimestamp(utime, tz=timezone.utc) + timedelta(hours=tz_hours)
        when = local.strftime("%Y-%m-%d %I:%M:%S %p")
    fee = int(tx.get("total_fees", 0)) / 1e9
    in_msg = tx.get("in_msg") or {}
    out_msgs = tx.get("out_msgs") or []

    if in_msg and int(in_msg.get("value", 0) or 0) > 0:
        direction = "in"
        amount = int(in_msg.get("value", 0)) / 1e9
        sender = _addr(in_msg.get("source"), "Unknown")
        receiver = _addr(in_msg.get("destination"), wallet)
        memo = _message_text(in_msg)
    elif out_msgs:
        direction = "out"
        amount = sum(int(m.get("value", 0) or 0) for m in out_msgs) / 1e9
        sender = wallet
        receiver = _addr(out_msgs[0].get("destination"), "Unknown")
        memo = _message_text(out_msgs[0])
    else:
        direction = "internal"
        amount = 0.0
        sender, receiver, memo = "Contract", wallet, "—"

    return {
        "hash": tx.get("hash", "N/A"),
        "time": when,
        "direction": direction,
        "amount": amount,
        "fee": fee,
        "sender": sender,
        "receiver": receiver,
        "memo": memo,
        "success": bool(tx.get("success", True)),
    }


def fetch_report(address, api_key=None, limit=6):
    """يرجع dict(balance, name, status, txs) أو None لو الخدمة مش متاحة."""
    bal = fetch_balance(address, api_key)
    if bal is None:
        return None
    balance, acc = bal
    raw_txs = fetch_transactions(address, api_key, limit=limit) or []
    return {
        "address": address,
        "balance": balance,
        "name": acc.get("name") or "",
        "status": acc.get("status") or "",
        "is_wallet": bool(acc.get("is_wallet", True)),
        "txs": [parse_transaction(t, address) for t in raw_txs],
    }
