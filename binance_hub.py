"""Telegram layer for the Binance section (menus, confirmations, notifications, schedulers).

Wired from bot.py with:
    binance_hub.register(bot, ctx)            # handlers
    binance_hub.start_background(bot, ctx)    # watcher + invoice scheduler threads
    binance_hub.handle_business_text(...)     # owner commands typed inside a client chat
"""

import os
import re
import threading
import time
from decimal import Decimal

import binance_core as core
from binance_core import BinanceError, BinanceNetworkError, dec, fmt_dec

CANCEL_WORDS = {"الغاء", "إلغاء", "cancel", "/cancel", "الغي"}
WATCH_INTERVAL = 45
INVOICE_INTERVAL = 60
ALERT_ADMIN_ON_FLAG = os.environ.get("BINANCE_ALERT_ADMIN_ON_FLAG", "1").strip() != "0"


class Ctx:
    """Bridge to bot.py helpers (kept explicit so this module never imports bot.py)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _cancelled(m):
    return (getattr(m, "text", "") or "").strip().lower() in {w.lower() for w in CANCEL_WORDS}


def register(bot, ctx):
    e, pe, types, esc = ctx.e, ctx.pe, ctx.types, ctx.html_escape
    core.init_tables()

    # ------------------------------------------------------------------ helpers
    def btn(text, data, icon=None, style=None):
        return pe.btn(text, callback_data=data, icon=icon, style=style)

    def kb(*rows):
        m = types.InlineKeyboardMarkup()
        for row in rows:
            m.row(*row) if isinstance(row, (list, tuple)) else m.row(row)
        return m

    DIVIDER = e("dash") * 14
    LIST_DIVIDER = e("dash") * 6   # قصير عشان القوائم الطويلة ماتعديش حد الإيموجي في الرسالة

    def card(icon, title, lines, footer=None):
        """Card-style message: bold title, rows separated by premium dash dividers."""
        head = f"{e(icon)} <b>{esc(title)}</b>\n\n" if icon else ""
        body = f"\n{DIVIDER}\n".join(l for l in lines if l)
        foot = f"\n{DIVIDER}\n{footer}" if footer else ""
        return head + body + foot

    def section_on(owner_id):
        """False when the Binance section is switched off from the enable/disable panel."""
        fn = getattr(ctx, "feature_on", None)
        try:
            return True if fn is None else bool(fn(owner_id))
        except Exception:
            return True

    def is_owner_ok(call):
        if not section_on(call.from_user.id):
            try:
                bot.answer_callback_query(call.id, "قسم بينانس متعطل من التفعيل والتعطيل.", show_alert=True)
            except Exception:
                pass
            return False
        return ctx.require_active(call)

    def linked(owner_id):
        return core.get_account(owner_id)

    def delete_quiet(chat_id, message_id):
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    def ask(chat_id, text, handler, *args):
        msg = ctx.send(chat_id, text)
        bot.register_next_step_handler(msg, handler, *args)

    def onoff(v):
        return "مفعّل" if v else "مقفول"

    # ------------------------------------------------------------------ main menu
    def menu_text(owner_id):
        acc = linked(owner_id)
        head = f"{e('qb_robot')} <b>لوحة الإدارة لـ Binance</b>\n\n"
        if not acc:
            return (
                head
                + "الحساب لسه مش مربوط. اربط حساب بينانس بتاعك وأدير كل حاجة من البوت:\n"
                "• إشعارات فورية للإيداع والتحويل (Pay) والسحب\n"
                "• عرض الرصيد الشامل وآخر الإيداعات والسحوبات\n"
                "• عناوين الإيداع لكل عملة وشبكة\n"
                "• تحويل من البوت بتأكيد + فواتير مجدولة للعملاء\n\n"
                f"{e('shield')} <b>نصيحة أمان:</b> اعمل API Key مخصص للبوت، وفعّل الـ IP whitelist، "
                "ومتفعّلش صلاحية Trading."
            )
        recs = len(core.list_recipients(owner_id))
        invs = len([i for i in core.list_invoices(owner_id) if i["status"] == "active"])
        return (
            head
            + f"{e('check')} <i>الحساب متصل</i>  •  الإشعارات: <b>{onoff(acc['notify_on'])}</b>\n"
            f"المستلمين: <b>{recs}</b>  •  الفواتير النشطة: <b>{invs}</b>\n\n"
            "اختر الإجراء المطلوب:"
        )

    def menu_markup(owner_id):
        if not linked(owner_id):
            return kb([btn(" ربط الحساب", "bn_link", "link", "success")],
                      [btn(" رجوع لشبكة المنصات", "bn_menu", "arrow_left", "danger")])
        return kb(
            [btn(" فحص أمان الـ API", "bn_sec", "lock", "success")],
            [btn(" عرض الرصيد الشامل", "bn_bal", "wallet")],
            [btn(" تصفح العملات للإيداع", "bn_coins", "download"), btn(" بحث عن عملة", "bn_search", "eye")],
            [btn(" آخر الإيداعات", "bn_dep", "download"), btn(" آخر السحوبات", "bn_wd", "upload")],
            [btn(" المستلمين", "bn_rcp", "people"), btn(" الفواتير", "bn_inv", "document")],
            [btn(" ملخص", "bn_sum", "sparkles"), btn(" الإعدادات", "bn_set", "pencil")],
            [btn(" حذف مفاتيح المنصة", "bn_unlink", "trash", "danger")],
            [btn(" شرح", "hlp:binance", "bulb" if "bulb" in pe.EMOJI else "book", "primary")],
            [btn(" رجوع لشبكة المنصات", "bn_menu", "arrow_left", "danger")],
        )

    def show_menu(call_or_msg_chat, message_id, owner_id):
        ctx.edit(menu_text(owner_id), call_or_msg_chat, message_id, reply_markup=menu_markup(owner_id))

    # ---- platforms & wallets network (entry screen)
    def hub_text():
        return (
            f"أهلاً بك في شبكة المنصات والمحافظ {e('qb_robot')}\n\n"
            "يمكنك ربط حساباتك وإدارتها بالكامل:\n"
            "اختر المنصة أو الشبكة التي تريد إدارتها أو ربطها:"
        )

    def hub_markup(owner_id):
        icon = "check" if linked(owner_id) else "link"
        return kb(
            [btn(" Binance", "bn_dash", icon, "success")],
            [btn(" رجوع", "bn_back", "arrow_left", "danger")],
        )

    @bot.callback_query_handler(func=lambda c: c.data == "bn_menu")
    def cb_menu(call):
        if not is_owner_ok(call):
            return
        bot.answer_callback_query(call.id)
        ctx.edit(hub_text(), call.message.chat.id, call.message.message_id, reply_markup=hub_markup(call.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data == "bn_dash")
    def cb_dash(call):
        if not is_owner_ok(call):
            return
        bot.answer_callback_query(call.id)
        show_menu(call.message.chat.id, call.message.message_id, call.from_user.id)

    @bot.message_handler(commands=["binance"])
    def cmd_binance(message):
        if message.chat.type != "private":
            return
        if not section_on(message.from_user.id) or not ctx.require_active_msg(message):
            return
        ctx.send(message.chat.id, menu_text(message.from_user.id), reply_markup=menu_markup(message.from_user.id))

    # /binance must win over generic private handlers registered earlier in bot.py.
    bot.message_handlers.insert(0, bot.message_handlers.pop())

    @bot.callback_query_handler(func=lambda c: c.data == "bn_back")
    def cb_back(call):
        bot.answer_callback_query(call.id)
        bot._owner_panel_context = call.from_user
        text, markup = ctx.panel(call.from_user.id)
        ctx.edit(text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    # ------------------------------------------------------------------ link / unlink
    @bot.callback_query_handler(func=lambda c: c.data == "bn_link")
    def cb_link(call):
        if not is_owner_ok(call):
            return
        bot.answer_callback_query(call.id)
        ask(
            call.message.chat.id,
            f"{e('lock')} <b>ربط بينانس</b>\n\n"
            "1) من بينانس: Account → API Management → Create API (System generated)\n"
            "2) فعّل <b>Enable Reading</b>. لو عايز تحويلات فعّل كمان <b>Enable Withdrawals</b> "
            "(بينانس هيطلب IP whitelist للسحب). <b>متفعّلش Trading.</b>\n\n"
            "ابعتلي دلوقتي الـ <b>API Key</b> (هيتمسح من الشات فورًا).\n<i>للإلغاء اكتب: إلغاء</i>",
            got_key,
        )

    def got_key(m):
        delete_quiet(m.chat.id, m.message_id)
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        key = m.text.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{32,128}", key):
            return ask(m.chat.id, f"{e('cross')} الـ Key شكله غلط. ابعته تاني أو اكتب إلغاء.", got_key)
        ask(m.chat.id, f"تمام {e('check')} دلوقتي ابعت الـ <b>Secret Key</b> (هيتمسح برضو).", got_secret, key)

    def got_secret(m, key):
        delete_quiet(m.chat.id, m.message_id)
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        status = ctx.send(m.chat.id, f"{e('clock')} <b>جاري التحقق من بينانس...</b>")

        def work():
            try:
                perms = core.link_account(m.from_user.id, key, m.text)
            except ValueError as exc:
                return ctx.edit(f"{e('cross')} {esc(str(exc))}", m.chat.id, status.message_id)
            except BinanceError as exc:
                return ctx.edit(f"{e('cross')} {esc(core.explain_error(exc))}", m.chat.id, status.message_id)
            except Exception as exc:
                return ctx.edit(f"{e('cross')} مقدرتش أتصل ببينانس: {esc(str(exc)[:150])}", m.chat.id, status.message_id)
            lines = [f"{e('check')} <b>تم ربط Binance بنجاح!</b>\n"]
            lines.append(f"• قراءة: {e('check')}")
            lines.append(f"• سحب: {e('check') if perms['can_withdraw'] else e('cross')}"
                         f"{'' if perms['can_withdraw'] else ' (التحويلات مش هتشتغل)'}")
            lines.append(f"• IP whitelist: {e('check') if perms['ip_restricted'] else e('cross')}")
            if perms["trading"]:
                lines.append(f"\n{e('shield')} <b>تنبيه:</b> المفتاح ده عليه صلاحية Trading. الأأمن تقفلها.")
            if perms["can_withdraw"] and not perms["ip_restricted"]:
                lines.append(f"\n{e('shield')} <b>تنبيه:</b> السحب من غير IP whitelist خطر.")
            lines.append("\nالتحويلات من البوت <b>مقفولة افتراضيًا</b> - فعّلها من الإعدادات بعد ما تحط الحدود وPIN.")
            lines.append("\nأرسل /binance للبدء.")
            ctx.edit("\n".join(lines), m.chat.id, status.message_id, reply_markup=menu_markup(m.from_user.id))

        threading.Thread(target=work, daemon=True, name="bn-link").start()

    @bot.callback_query_handler(func=lambda c: c.data == "bn_unlink")
    def cb_unlink(call):
        if not is_owner_ok(call):
            return
        bot.answer_callback_query(call.id)
        ctx.edit(
            f"{e('cross')} <b>تفك ربط بينانس؟</b>\nهتتمسح مفاتيح الـ API من البوت. الفواتير النشطة هتتوقف مؤقتًا.",
            call.message.chat.id, call.message.message_id,
            reply_markup=kb([btn(" أيوه، فك الربط", "bn_unlink_yes", "trash", "danger"), btn(" لأ", "bn_dash", "arrow_left")]),
        )

    @bot.callback_query_handler(func=lambda c: c.data == "bn_unlink_yes")
    def cb_unlink_yes(call):
        if not is_owner_ok(call):
            return
        core.delete_account(call.from_user.id)
        bot.answer_callback_query(call.id, "اتفك الربط.")
        show_menu(call.message.chat.id, call.message.message_id, call.from_user.id)

    # ------------------------------------------------------------------ balance / history / summary
    def need_link(call):
        if not linked(call.from_user.id):
            bot.answer_callback_query(call.id, "اربط حساب بينانس الأول.", show_alert=True)
            return False
        return True

    def balance_text(owner_id):
        c = core.client_for(owner_id)
        totals, parts = {}, []
        for title, fn in (("Funding (التمويل)", c.funding_balances), ("Spot (السبوت)", c.spot_balances)):
            try:
                bal = fn()
            except BinanceError as exc:
                parts.append(f"<b>{title}:</b> {esc(core.explain_error(exc))}")
                continue
            for k, v in bal.items():
                totals[k] = totals.get(k, 0) + v
            if bal:
                rows = "\n".join(f"  • {k}: <code>{fmt_dec(v)}</code>" for k, v in sorted(bal.items(), key=lambda kv: -kv[1])[:15])
                parts.append(f"<b>{title}:</b>\n{rows}")
            else:
                parts.append(f"<b>{title}:</b> فاضي")
        total_usd = Decimal(0)
        for coin, amt in totals.items():
            try:
                usd = core.usd_value(c, coin, dec(amt, Decimal(0)))
            except Exception:
                usd = None
            if usd:
                total_usd += usd
        total_line = f"\n\n{e('cash')} <b>الإجمالي التقريبي: ≈ ${fmt_dec(total_usd, 2)}</b>" if totals else ""
        return f"{e('wallet')} <b>الرصيد الشامل على بينانس</b>\n\n" + "\n\n".join(parts) + total_line

    def send_balance(chat_id, owner_id, edit_msg=None):
        def work():
            try:
                text = balance_text(owner_id)
            except Exception as exc:
                text = f"{e('cross')} {esc(core.explain_error(exc) if isinstance(exc, BinanceError) else str(exc)[:150])}"
            back = kb([btn(" رجوع", "bn_dash", "arrow_left")])
            if edit_msg:
                ctx.edit(text, chat_id, edit_msg, reply_markup=back)
            else:
                ctx.send(chat_id, text, reply_markup=back)
        threading.Thread(target=work, daemon=True, name="bn-balance").start()

    @bot.callback_query_handler(func=lambda c: c.data == "bn_bal")
    def cb_bal(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id, "بجيب الرصيد...")
        send_balance(call.message.chat.id, call.from_user.id, call.message.message_id)

    def history_text(owner_id):
        rows = core.ledger_recent(owner_id, 12)
        if not rows:
            return f"{e('document')} <b>السجل</b>\n\nمفيش عمليات لسه."
        icon = {"sent": "✅", "ok": "💰", "failed": "❌", "unknown": "❓"}
        lines = []
        for r in rows:
            who = f" → {esc(r['recipient'])}" if r["kind"] == "transfer" and r["recipient"] else (f" ← {esc(r['recipient'])}" if r["recipient"] else "")
            lines.append(f"{icon.get(r['status'], '•')} {core.fmt_ts(r['ts'])}  <b>{esc(r['amount'])} {esc(r['coin'])}</b>{who}")
        return f"{e('document')} <b>آخر العمليات</b>\n\n" + "\n".join(lines)

    @bot.callback_query_handler(func=lambda c: c.data == "bn_hist")
    def cb_hist(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        ctx.edit(history_text(call.from_user.id), call.message.chat.id, call.message.message_id,
                 reply_markup=kb([btn(" رجوع", "bn_dash", "arrow_left")]))

    def summary_text(owner_id):
        s = core.summary(owner_id)
        fmt = lambda d: "، ".join(f"{fmt_dec(v)} {k}" for k, v in d.items()) or "لا شيء"
        return (
            f"{e('sparkles')} <b>ملخص آخر 24 ساعة</b>\n\n"
            f"داخل: <b>{fmt(s['in'])}</b>\n"
            f"خارج (تحويلاتك من البوت): <b>{fmt(s['out'])}</b>\n\n"
            f"فواتير مفتوحة: <b>{s['open']}</b> (إجمالي {fmt(s['due_total'])})\n"
            f"متأخرة: <b>{s['overdue']}</b>"
        )

    @bot.callback_query_handler(func=lambda c: c.data == "bn_sum")
    def cb_sum(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        ctx.edit(summary_text(call.from_user.id), call.message.chat.id, call.message.message_id,
                 reply_markup=kb([btn(" رجوع", "bn_dash", "arrow_left")]))

    # ------------------------------------------------------------------ API security check
    def run_async(call, fn, name):
        def work():
            try:
                fn()
            except BinanceError as exc:
                ctx.edit(f"{e('cross')} {esc(core.explain_error(exc))}", call.message.chat.id, call.message.message_id,
                         reply_markup=kb([btn(" رجوع", "bn_dash", "arrow_left")]))
            except Exception as exc:
                ctx.edit(f"{e('cross')} حصل خطأ: {esc(str(exc)[:150])}", call.message.chat.id, call.message.message_id,
                         reply_markup=kb([btn(" رجوع", "bn_dash", "arrow_left")]))
        threading.Thread(target=work, daemon=True, name=name).start()

    def yn(v):
        return e("check") if v else e("cross")

    @bot.callback_query_handler(func=lambda c: c.data == "bn_sec")
    def cb_sec(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id, "بفحص الـ API...")

        def job():
            r = core.client_for(call.from_user.id).api_restrictions()
            reading, wd = bool(r.get("enableReading", True)), bool(r.get("enableWithdrawals"))
            trade = bool(r.get("enableSpotAndMarginTrading") or r.get("enableFutures") or r.get("enableMargin"))
            ip = bool(r.get("ipRestrict"))
            internal = bool(r.get("enableInternalTransfer"))
            created = int(r.get("createTime") or 0) // 1000
            score, notes = 100, []
            if wd and not ip:
                score -= 50; notes.append("السحب مفعّل من غير IP whitelist - خطر كبير.")
            if trade:
                score -= 25; notes.append("صلاحية Trading مفعّلة - البوت مش محتاجها، اقفلها.")
            if not ip:
                score -= 15; notes.append("فعّل تقييد الـ IP على المفتاح.")
            if internal:
                score -= 5; notes.append("التحويل الداخلي مفعّل.")
            score = max(score, 0)
            level = "ممتاز" if score >= 85 else ("مقبول" if score >= 60 else "ضعيف")
            lines = [
                f"{e('lock')} <b>فحص أمان الـ API</b>\n",
                f"{yn(reading)} القراءة (Reading)",
                f"{yn(not wd)} السحب: <b>{'مفعّل' if wd else 'مقفول'}</b>",
                f"{yn(not trade)} التداول: <b>{'مفعّل' if trade else 'مقفول'}</b>",
                f"{yn(ip)} تقييد الـ IP: <b>{'مفعّل' if ip else 'غير مفعّل'}</b>",
                f"{yn(not internal)} التحويل الداخلي: <b>{'مفعّل' if internal else 'مقفول'}</b>",
            ]
            if created:
                lines.append(f"{e('clock')} تاريخ إنشاء المفتاح: {core.fmt_ts(created)}")
            lines.append(f"\n{e('shield')} <b>التقييم: {score}/100 - {level}</b>")
            if notes:
                lines.append("\n" + "\n".join(f"• {esc(n)}" for n in notes))
            ctx.edit("\n".join(lines), call.message.chat.id, call.message.message_id,
                     reply_markup=kb([btn(" فحص تاني", "bn_sec", "lock", "success")], [btn(" رجوع", "bn_dash", "arrow_left", "danger")]))

        run_async(call, job, "bn-sec")

    # ------------------------------------------------------------------ deposit coins / search
    POPULAR = ["USDT", "USDC", "BTC", "ETH", "BNB", "TRX", "SOL", "XRP", "TON", "LTC", "DOGE", "ADA"]

    @bot.callback_query_handler(func=lambda c: c.data == "bn_coins")
    def cb_coins(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        rows = [[btn(f" {c}", f"bn_coin:{c}", "wallet") for c in POPULAR[i:i + 3]] for i in range(0, len(POPULAR), 3)]
        rows.append([btn(" بحث عن عملة", "bn_search", "eye")])
        rows.append([btn(" رجوع", "bn_dash", "arrow_left", "danger")])
        ctx.edit(f"{e('download')} <b>تصفح العملات للإيداع</b>\n\nاختر العملة عشان تشوف الشبكات وعنوان الإيداع:",
                 call.message.chat.id, call.message.message_id, reply_markup=kb(*rows))

    def coin_networks(owner_id, coin):
        info = core.client_for(owner_id).coin_info(coin)
        if not info:
            return None, []
        nets = [n for n in info.get("networkList", []) if n.get("depositEnable")]
        return info, nets

    def show_coin(chat_id, owner_id, coin, edit_msg=None):
        def work():
            def out(text, markup):
                if edit_msg:
                    ctx.edit(text, chat_id, edit_msg, reply_markup=markup)
                else:
                    ctx.send(chat_id, text, reply_markup=markup)
            back = kb([btn(" العملات", "bn_coins", "arrow_left")], [btn(" رجوع للوحة", "bn_dash", "arrow_left", "danger")])
            try:
                info, nets = coin_networks(owner_id, coin)
            except BinanceError as exc:
                return out(f"{e('cross')} {esc(core.explain_error(exc))}", back)
            except Exception as exc:
                return out(f"{e('cross')} {esc(str(exc)[:150])}", back)
            if not info:
                return out(f"{e('cross')} العملة <b>{esc(coin)}</b> مش موجودة على بينانس.", back)
            if not nets:
                return out(f"{e('cross')} الإيداع متوقف حاليًا لعملة <b>{esc(coin)}</b>.", back)
            rows = [[btn(f" {n['network']}", f"bn_dnet:{coin}:{n['network']}", "globe")] for n in nets[:12]]
            rows += [[btn(" العملات", "bn_coins", "arrow_left")], [btn(" رجوع للوحة", "bn_dash", "arrow_left", "danger")]]
            out(f"{e('wallet')} <b>{esc(info.get('name') or coin)} ({esc(coin)})</b>\n\nاختر شبكة الإيداع:", kb(*rows))
        threading.Thread(target=work, daemon=True, name="bn-coin").start()

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("bn_coin:"))
    def cb_coin(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        show_coin(call.message.chat.id, call.from_user.id, call.data.split(":", 1)[1], call.message.message_id)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("bn_dnet:"))
    def cb_dnet(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        _, coin, network = call.data.split(":", 2)
        bot.answer_callback_query(call.id, "بجيب العنوان...")

        def job():
            c = core.client_for(call.from_user.id)
            r = c.deposit_address(coin, network)
            net = next((n for n in (c.coin_info(coin) or {}).get("networkList", []) if n.get("network") == network), {})
            lines = [f"{e('download')} <b>إيداع {esc(coin)}</b>", f"{e('globe')} الشبكة: <b>{esc(network)}</b>\n",
                     f"العنوان:\n<code>{esc(r.get('address', ''))}</code>"]
            if r.get("tag"):
                lines.append(f"\nMemo/Tag: <code>{esc(r['tag'])}</code>")
            if net.get("minConfirm"):
                lines.append(f"\nتأكيدات الشبكة المطلوبة: <b>{net['minConfirm']}</b>")
            lines.append(f"\n{e('shield')} <b>تنبيه:</b> ابعت {esc(coin)} على شبكة <b>{esc(network)}</b> بس، غير كده الفلوس هتضيع.")
            ctx.edit("\n".join(lines), call.message.chat.id, call.message.message_id,
                     reply_markup=kb([btn(" الشبكات", f"bn_coin:{coin}", "arrow_left")], [btn(" رجوع للوحة", "bn_dash", "arrow_left", "danger")]))

        run_async(call, job, "bn-dnet")

    @bot.callback_query_handler(func=lambda c: c.data == "bn_search")
    def cb_search(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        ask(call.message.chat.id, f"{e('eye')} <b>بحث عن عملة</b>\n\nاكتب رمز العملة (مثال: <code>USDT</code> أو <code>BTC</code>).\n<i>للإلغاء اكتب: إلغاء</i>", got_search, call.from_user.id)

    def got_search(m, owner_id):
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        coin = re.sub(r"[^A-Za-z0-9]", "", m.text).upper()
        if not coin:
            return ask(m.chat.id, f"{e('cross')} الرمز غلط. اكتبه تاني أو اكتب إلغاء.", got_search, owner_id)
        show_coin(m.chat.id, owner_id, coin)

    # ------------------------------------------------------------------ last deposits / withdrawals
    DEP_STATUS = {0: "قيد التأكيد", 1: "مكتمل بنجاح", 6: "مكتمل بنجاح", 2: "مرفوض", 7: "إيداع خاطئ", 8: "بانتظار تأكيدك"}
    WD_STATUS = {0: "إيميل مرسل", 1: "ملغي", 2: "بانتظار الموافقة", 3: "مرفوض", 4: "قيد التنفيذ", 5: "فشل", 6: "مكتمل بنجاح"}

    def deposits_text(owner_id):
        c = core.client_for(owner_id)
        start = int((time.time() - 89 * 86400) * 1000)
        rows = sorted(c.deposits(start, 100), key=lambda d: int(d.get("insertTime", 0)), reverse=True)[:8]
        if not rows:
            return f"{e('download')} <b>آخر الإيداعات</b>\n\nمفيش إيداعات في آخر 90 يوم."
        blocks = []
        for d in rows:
            st = DEP_STATUS.get(d.get("status"), "—")
            ok = d.get("status") in (1, 6)
            blocks.append(
                f"{e('check') if ok else e('clock')} <b>+{esc(d['amount'])} {esc(d['coin'])}</b>\n"
                f"{e('globe')} {esc(d.get('network') or '—')}  •  {esc(st)}\n"
                f"{e('clock')} {core.fmt_ts_full(int(d.get('insertTime', 0)) // 1000)}"
            )
        return f"{e('download')} <b>آخر الإيداعات</b>\n\n" + f"\n{LIST_DIVIDER}\n".join(blocks)

    def withdrawals_text(owner_id):
        c = core.client_for(owner_id)
        start = int((time.time() - 89 * 86400) * 1000)
        rows = sorted(c.withdrawals(start, 100), key=lambda w: w.get("applyTime", ""), reverse=True)[:8]
        if not rows:
            return f"{e('upload')} <b>آخر السحوبات</b>\n\nمفيش سحوبات في آخر 90 يوم."
        blocks = []
        for w in rows:
            st = WD_STATUS.get(w.get("status"), "—")
            ok = w.get("status") == 6
            blocks.append(
                f"{e('check') if ok else e('clock')} <b>-{esc(w['amount'])} {esc(w['coin'])}</b>\n"
                f"{e('globe')} {esc(w.get('network') or '—')}  •  {esc(st)}\n"
                f"{e('wallet')} <code>{esc(core.mask_addr(w.get('address') or ''))}</code>\n"
                f"{e('clock')} {esc(w.get('applyTime', ''))}"
            )
        return f"{e('upload')} <b>آخر السحوبات</b>\n\n" + f"\n{LIST_DIVIDER}\n".join(blocks)

    def history_cb(call, builder, name):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id, "بجيب البيانات...")

        def job():
            ctx.edit(builder(call.from_user.id), call.message.chat.id, call.message.message_id,
                     reply_markup=kb([btn(" رجوع", "bn_dash", "arrow_left", "danger")]))
        run_async(call, job, name)

    @bot.callback_query_handler(func=lambda c: c.data == "bn_dep")
    def cb_dep(call):
        history_cb(call, deposits_text, "bn-dep")

    @bot.callback_query_handler(func=lambda c: c.data == "bn_wd")
    def cb_wd(call):
        history_cb(call, withdrawals_text, "bn-wd")

    # ------------------------------------------------------------------ settings
    def settings_markup(owner_id):
        a = linked(owner_id)
        tick = lambda v: e("check") if v else e("cross")
        return kb(
            [btn(f" إشعارات الأموال: {onoff(a['notify_on'])}", "bn_t_notify", "bell", "success" if a["notify_on"] else "danger")],
            [btn(f" التحويلات من البوت: {onoff(a['transfers_on'])}", "bn_t_transfers", "lock", "success" if a["transfers_on"] else "danger")],
            [btn(f" المحفظة: {'Funding' if a['wallet_type'] == 1 else 'Spot'}", "bn_t_wallet", "diamond")],
            [btn(f" حد العملية: {a['per_tx_limit']}$", "bn_lim_tx", "shield"), btn(f" الحد اليومي: {a['daily_limit']}$", "bn_lim_day", "shield")],
            [btn(f" تحقق صارم لكل تحويل: {onoff(a['strict_verify'])}", "bn_t_strict", "shield", "success" if a["strict_verify"] else "danger")],
            [btn(f" PIN: {'متفعّل' if a['pin_hash'] else 'مش متفعّل'}", "bn_pin_set", "lock"),
             *([btn(" مسح PIN", "bn_pin_clear", "trash", "danger")] if a["pin_hash"] else [])],
            [btn(" طريقة استلام الدفع (للفواتير)", "bn_recv", "link")],
            [btn(f" تذكير العملاء المتأخرين: {onoff(a['auto_remind'])}", "bn_t_remind", "clock")],
            [btn(f" إيصال تلقائي بعد الدفع: {onoff(a['auto_receipt'])}", "bn_t_receipt", "check")],
            [btn(" رجوع", "bn_dash", "arrow_left", "danger")],
        )

    def settings_text(owner_id):
        a = linked(owner_id)
        warn = ""
        if a["transfers_on"] and not a["pin_hash"]:
            warn = f"\n\n{e('shield')} <i>مفيش PIN: أي حد يوصل لحسابك على تيليجرام يقدر يأكد تحويل. الأفضل تفعّل PIN.</i>"
        if a["strict_verify"] and not a["pin_hash"]:
            warn += f"\n\n{e('shield')} <i>التحقق الصارم شغال بس مفيش PIN: التحويلات هتتمنع لحد ما تفعّل PIN.</i>"
        recv = esc(a["receive_text"]) if a["receive_text"] else "<i>لسه مش محددة</i>"
        used = core.sent_usd_last_24h(owner_id)
        daily = dec(a["daily_limit"], Decimal(0))
        usage = f"\n\n{e('shield')} استخدمت <b>{fmt_dec(used)}$</b> من <b>{fmt_dec(daily)}$</b> في آخر 24 ساعة." if a["transfers_on"] else ""
        return f"{e('lock')} <b>إعدادات بينانس</b>\n\nطريقة استلام الدفع:\n{recv}{usage}{warn}"

    def refresh_settings(call):
        ctx.edit(settings_text(call.from_user.id), call.message.chat.id, call.message.message_id,
                 reply_markup=settings_markup(call.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data == "bn_set")
    def cb_set(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        refresh_settings(call)

    _TOGGLES = {
        "bn_t_notify": "notify_on", "bn_t_transfers": "transfers_on", "bn_t_remind": "auto_remind",
        "bn_t_receipt": "auto_receipt", "bn_t_strict": "strict_verify",
    }

    @bot.callback_query_handler(func=lambda c: c.data in _TOGGLES or c.data == "bn_t_wallet")
    def cb_toggle(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        owner_id = call.from_user.id
        a = linked(owner_id)
        if call.data == "bn_t_wallet":
            core.update_account(owner_id, wallet_type=0 if a["wallet_type"] == 1 else 1)
        else:
            field = _TOGGLES[call.data]
            if field == "transfers_on" and not a["transfers_on"] and not a["can_withdraw"]:
                return bot.answer_callback_query(call.id, "الـ API Key مفيهوش Enable Withdrawals. فعّلها من بينانس واربط تاني.", show_alert=True)
            core.update_account(owner_id, **{field: 0 if a[field] else 1})
        bot.answer_callback_query(call.id)
        refresh_settings(call)

    def ask_number(call, prompt, field):
        bot.answer_callback_query(call.id)
        ask(call.message.chat.id, prompt, got_limit, field, call.message.message_id)

    def got_limit(m, field, menu_mid):
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        v = dec(core.to_ascii_digits(m.text))
        if v is None or v <= 0 or v > 1_000_000:
            return ask(m.chat.id, f"{e('cross')} رقم غير صحيح. ابعت رقم تاني أو إلغاء.", got_limit, field, menu_mid)
        core.update_account(m.from_user.id, **{field: fmt_dec(v, 2)})
        ctx.send(m.chat.id, f"{e('check')} اتحفظ: <b>{fmt_dec(v, 2)}$</b>", reply_markup=settings_markup(m.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data == "bn_lim_tx")
    def cb_lim_tx(call):
        if is_owner_ok(call) and need_link(call):
            ask_number(call, "ابعت <b>أقصى مبلغ للعملية الواحدة</b> بالدولار (مثال: 100):", "per_tx_limit")

    @bot.callback_query_handler(func=lambda c: c.data == "bn_lim_day")
    def cb_lim_day(call):
        if is_owner_ok(call) and need_link(call):
            ask_number(call, "ابعت <b>الحد اليومي</b> (آخر 24 ساعة) بالدولار (مثال: 300):", "daily_limit")

    @bot.callback_query_handler(func=lambda c: c.data == "bn_pin_set")
    def cb_pin_set(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        ask(call.message.chat.id, "ابعت <b>PIN</b> من 4 لـ 8 أرقام (هيتمسح من الشات). هيتطلب مع كل تأكيد تحويل.", got_pin)

    def got_pin(m):
        delete_quiet(m.chat.id, m.message_id)
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        pin = core.to_ascii_digits(m.text.strip())
        if not re.fullmatch(r"\d{4,8}", pin):
            return ask(m.chat.id, f"{e('cross')} لازم 4 لـ 8 أرقام. ابعت تاني أو إلغاء.", got_pin)
        core.set_pin(m.from_user.id, pin)
        ctx.send(m.chat.id, f"{e('check')} <b>اتفعّل الـ PIN.</b> متنساهوش - مفيش طريقة لاسترجاعه (بتمسحه وتعمل واحد جديد).",
                 reply_markup=settings_markup(m.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data == "bn_pin_clear")
    def cb_pin_clear(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        core.clear_pin(call.from_user.id)
        bot.answer_callback_query(call.id, "اتمسح الـ PIN.")
        refresh_settings(call)

    @bot.callback_query_handler(func=lambda c: c.data == "bn_recv")
    def cb_recv(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        ask(call.message.chat.id,
            "ابعت النص اللي هيظهر للعميل في الفاتورة كـ <b>طريقة الدفع</b>. مثال:\n"
            "<code>Binance Pay ID: 123456789\nأو USDT (TRC20): T....</code>\n<i>(حد أقصى 300 حرف)</i>",
            got_recv)

    def got_recv(m):
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        core.update_account(m.from_user.id, receive_text=m.text.strip()[:300])
        ctx.send(m.chat.id, f"{e('check')} اتحفظت طريقة الدفع.", reply_markup=settings_markup(m.from_user.id))

    # ------------------------------------------------------------------ recipients
    def recipients_markup(owner_id):
        rows = [[btn(f" {r['name']} • {r['coin']}/{r['network']}", f"bn_rv_{r['id']}", "person")] for r in core.list_recipients(owner_id)]
        rows.append([btn(" إضافة مستلم", "bn_rcp_add", "upload", "success")])
        rows.append([btn(" رجوع", "bn_dash", "arrow_left", "danger")])
        return kb(*rows)

    @bot.callback_query_handler(func=lambda c: c.data == "bn_rcp")
    def cb_rcp(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        ctx.edit(
            f"{e('people')} <b>المستلمين المحفوظين</b>\n\nالتحويل بيتم <b>للمستلمين المحفوظين بس</b> (حماية من أي عنوان غريب). "
            f"أي مستلم جديد بيستنى {core.NEW_RECIPIENT_COOLDOWN // 60} دقايق قبل أول تحويل.",
            call.message.chat.id, call.message.message_id, reply_markup=recipients_markup(call.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_rv_"))
    def cb_rv(call):
        if not is_owner_ok(call):
            return
        r = core.get_recipient(call.from_user.id, int(call.data[6:]))
        if not r:
            return bot.answer_callback_query(call.id, "مش موجود.")
        bot.answer_callback_query(call.id)
        ctx.edit(
            f"{e('person')} <b>{esc(r['name'])}</b>\nالعملة: {r['coin']}  •  الشبكة: {r['network']}\n"
            f"العنوان: <code>{esc(r['address'])}</code>" + (f"\nMemo/Tag: <code>{esc(r['tag'])}</code>" if r["tag"] else ""),
            call.message.chat.id, call.message.message_id,
            reply_markup=kb([btn(" حذف", f"bn_rd_{r['id']}", "trash", "danger")], [btn(" رجوع", "bn_rcp", "arrow_left")]))

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_rd_"))
    def cb_rd(call):
        if not is_owner_ok(call):
            return
        core.delete_recipient(call.from_user.id, int(call.data[6:]))
        bot.answer_callback_query(call.id, "اتحذف.")
        ctx.edit(f"{e('people')} <b>المستلمين المحفوظين</b>", call.message.chat.id, call.message.message_id,
                 reply_markup=recipients_markup(call.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data == "bn_rcp_add")
    def cb_rcp_add(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        if core.recipient_count(call.from_user.id) >= core.MAX_RECIPIENTS:
            return bot.answer_callback_query(call.id, f"وصلت لأقصى عدد مستلمين ({core.MAX_RECIPIENTS}). احذف حد الأول.", show_alert=True)
        bot.answer_callback_query(call.id)
        ask(call.message.chat.id, "اسم المستلم (اللي هتكتبه في الأمر، مثال: أحمد):", rcp_name)

    def rcp_name(m):
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        name = m.text.strip()
        if len(name) > 40 or name.isdigit():
            return ask(m.chat.id, f"{e('cross')} الاسم لازم يكون كلمة/كلمتين (مش رقم) وأقل من 40 حرف.", rcp_name)
        ask(m.chat.id, "العملة والشبكة، مثال: <code>USDT TRX</code> أو <code>USDT BSC</code>\n"
                       "<i>الشبكات: TRX (TRC20) • BSC (BEP20) • ETH • SOL • TON • MATIC</i>", rcp_coin, name)

    def rcp_coin(m, name):
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        parts = m.text.upper().split()
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Z0-9]{2,15}", p) for p in parts):
            return ask(m.chat.id, f"{e('cross')} اكتب العملة ثم الشبكة، مثال: <code>USDT TRX</code>", rcp_coin, name)
        ask(m.chat.id, "ابعت <b>عنوان المحفظة</b> (أو عنوان الإيداع الداخلي لمستخدم بينانس):", rcp_address, name, parts[0], parts[1])

    def rcp_address(m, name, coin, network):
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        addr = m.text.strip()
        if not re.fullmatch(r"[A-Za-z0-9:_\-.]{15,120}", addr):
            return ask(m.chat.id, f"{e('cross')} العنوان شكله غلط (بدون مسافات). ابعته تاني أو إلغاء.", rcp_address, name, coin, network)
        ask(m.chat.id, "Memo/Tag لو الشبكة بتحتاجه (XRP/TON/..). لو مفيش اكتب <code>-</code>", rcp_tag, name, coin, network, addr)

    def rcp_tag(m, name, coin, network, addr):
        if _cancelled(m) or not m.text:
            return ctx.send(m.chat.id, "اتلغى.")
        tag = None if m.text.strip() in ("-", "لا", "مفيش") else m.text.strip()[:60]
        owner_id = m.from_user.id
        existing = core.get_recipient_by_exact_name(owner_id, name)
        if existing and (existing["address"] != addr.strip() or existing["network"] != network or existing["coin"] != coin):
            pid = core.create_pending(owner_id, "rcp_overwrite",
                                       {"name": name, "coin": coin, "network": network, "addr": addr, "tag": tag}, ttl=180)
            return ctx.send(m.chat.id,
                f"{e('shield')} <b>فيه مستلم بنفس الاسم خالص!</b>\n\n"
                f"القديم: <code>{esc(core.mask_addr(existing['address']))}</code> ({existing['coin']}/{existing['network']})\n"
                f"الجديد: <code>{esc(core.mask_addr(addr.strip()))}</code> ({coin}/{network})\n\n"
                f"لو تكمّل، أي تحويل جاي لـ «{esc(name)}» هيروح للعنوان الجديد.",
                reply_markup=kb([btn(" استبدال العنوان", f"bn_rcow_{pid}", "check", "danger"),
                                 btn(" إلغاء", f"bn_rcow_no_{pid}", "cross")]))
        save_recipient(owner_id, m.chat.id, name, coin, network, addr, tag)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_rcow_no_"))
    def cb_rcow_no(call):
        core.cancel_pending(call.from_user.id, call.data[11:])
        bot.answer_callback_query(call.id, "اتلغى.")
        ctx.edit(f"{e('cross')} اتلغى التحديث.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_rcow_"))
    def cb_rcow_yes(call):
        if not is_owner_ok(call):
            return
        owner_id, pid = call.from_user.id, call.data[8:]
        row = core.get_pending(owner_id, pid)
        if not row or row["status"] != "pending" or not core.claim_pending(owner_id, pid):
            return bot.answer_callback_query(call.id, "انتهت الصلاحية.", show_alert=True)
        core.finish_pending(pid, "done")
        bot.answer_callback_query(call.id)
        p = row["payload"]
        save_recipient(owner_id, call.message.chat.id, p["name"], p["coin"], p["network"], p["addr"], p["tag"],
                       edit_msg=call.message.message_id)

    def save_recipient(owner_id, chat_id, name, coin, network, addr, tag, edit_msg=None):
        def out(text, **kw):
            if edit_msg:
                return ctx.edit(text, chat_id, edit_msg, **kw)
            return ctx.send(chat_id, text, **kw)

        def work():
            # verify the network exists for this coin on Binance and whether a tag is required
            try:
                info = core.client_for(owner_id).coin_info(coin)
                if info:
                    nets = {n["network"]: n for n in info.get("networkList", [])}
                    if network not in nets:
                        return out(f"{e('cross')} الشبكة {network} مش متاحة لـ {coin}. المتاح: {', '.join(nets) or '—'}")
                    if nets[network].get("withdrawTag") and not tag:
                        return out(f"{e('cross')} الشبكة دي بتطلب Memo/Tag. ضيف المستلم من جديد ومعاه الـ Tag.")
                    tag_ = None if (tag and not nets[network].get("withdrawTag")) else tag
                else:
                    tag_ = tag
            except Exception:
                tag_ = tag
            try:
                core.add_recipient(owner_id, name, coin, network, addr, tag_)
            except ValueError as exc:
                return out(f"{e('cross')} {esc(str(exc))}")
            out(f"{e('check')} اتحفظ <b>{esc(name)}</b> ({coin}/{network}) <code>{esc(core.mask_addr(addr))}</code>\n"
                f"أول تحويل ليه بعد {core.NEW_RECIPIENT_COOLDOWN // 60} دقايق (حماية).",
                reply_markup=recipients_markup(owner_id))

        threading.Thread(target=work, daemon=True, name="bn-rcp").start()

    # ------------------------------------------------------------------ transfers (draft -> confirm -> execute)
    def confirm_text(d):
        lines = []
        if d.get("source"):
            lines.append(f"{e('person')} شات العميل: <b>{esc(d['source'])}</b>")
        lines.append(f"{e('person')} المستلم: <b>{esc(d['recipient_name'])}</b>")
        lines.append(f"{e('globe')} الشبكة: {d['network']}  •  <code>{esc(core.mask_addr(d['address']))}</code>")
        lines.append(f"{e('cash')} المبلغ: <b>{d['amount']} {d['coin']}</b> (~{d['usd']}$)")
        if d["fee"]:
            lines.append(f"{e('cash_2')} رسوم الشبكة: {d['fee']} {d['coin']}")
        lines.append(f"{e('wallet')} المحفظة: {'Funding' if d['wallet_type'] == 1 else 'Spot'}")
        for f in d["flags"]:
            lines.append(f"{e('shield')} تنبيه: {esc(f)}")
        verify_note = " • هيطلب منك عنوان المحفظة كامل + الـ PIN" if d.get("strict") else (" • هيطلب منك الـ PIN" if d["need_pin"] else "")
        return card("lock", "تأكيد تحويل", lines, footer=f"<i>الطلب صالح دقيقتين{verify_note}.</i>")

    def start_transfer(chat_id, owner_id, recipient, amount, coin, source=None):
        status = ctx.send(chat_id, f"{e('clock')} <b>بتحقق من الحدود والرصيد...</b>")

        def work():
            try:
                client = core.client_for(owner_id)
                draft, err = core.prepare_transfer(owner_id, client, recipient, amount, coin)
                if draft and source:
                    draft["source"] = source
            except BinanceError as exc:
                draft, err = None, core.explain_error(exc)
            except Exception as exc:
                draft, err = None, f"مقدرتش أتحقق: {str(exc)[:120]}"
            if err:
                return ctx.edit(f"{e('cross')} <b>التحويل اتمنع:</b> {esc(err)}", chat_id, status.message_id)
            pid = core.create_pending(owner_id, "transfer", draft)
            ctx.edit(confirm_text(draft), chat_id, status.message_id,
                     reply_markup=kb([btn(" تأكيد", f"bn_ok_{pid}", "check", "success"), btn(" إلغاء", f"bn_no_{pid}", "cross", "danger")]))

        threading.Thread(target=work, daemon=True, name="bn-prepare").start()

    def run_execution(chat_id, message_id, owner_id, pid):
        ctx.edit(f"{e('clock')} <b>جاري إرسال الطلب لبينانس...</b>", chat_id, message_id)

        def work():
            row = core.get_pending(owner_id, pid)
            flags = (row["payload"].get("flags") if row else None) or []
            status, info = core.execute_transfer(owner_id, pid)
            if status == "sent":
                text = card("check", "تم تقديم طلب السحب",
                            [f"{e('bang')} رقم العملية: <code>{esc(info)}</code>"],
                            footer="<i>هيجيلك إشعار لما بينانس يكمّلها.</i>")
                if flags and ALERT_ADMIN_ON_FLAG:
                    admin_id = getattr(ctx, "admin_id", None)
                    p = row["payload"]
                    if admin_id and admin_id != owner_id:
                        ctx.send(admin_id, card("shield", "تنبيه أمان: تحويل بينانس شاذ",
                            [f"{e('person')} المالك: <code>{owner_id}</code>",
                             f"{e('person')} المستلم: {esc(p['recipient_name'])}",
                             f"{e('cash')} المبلغ: {p['amount']} {p['coin']} (~{p['usd']}$)"]
                            + [f"{e('shield')} {esc(f)}" for f in flags]))
            elif status == "unknown":
                text = card("shield", "مش متأكد إن التحويل تم", [esc(info)])
            else:
                text = card("cross", "التحويل متنفّذش", [esc(info)])
            ctx.edit(text, chat_id, message_id, reply_markup=kb([btn(" السجل", "bn_hist", "document")]))

        threading.Thread(target=work, daemon=True, name="bn-exec").start()

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_ok_"))
    def cb_ok(call):
        if not is_owner_ok(call):
            return
        owner_id, pid = call.from_user.id, call.data[6:]
        row = core.get_pending(owner_id, pid)
        if not row or row["status"] != "pending" or row["expires_at"] < core.now_ts():
            bot.answer_callback_query(call.id, "الطلب انتهت صلاحيته أو اتنفذ.", show_alert=True)
            return ctx.edit(f"{e('clock')} الطلب انتهت صلاحيته. ابعت الأمر من جديد.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
        if row["kind"] == "invoice":
            return finish_invoice_confirm(call, owner_id, pid, row)
        if row["payload"].get("strict"):
            ctx.edit(f"{e('lock')} <b>تحقق من العنوان</b>\n\n"
                     f"اكتب <b>عنوان المحفظة كامل</b> للمستلم <b>{esc(row['payload']['recipient_name'])}</b> "
                     f"({esc(row['payload']['network'])}) للتأكيد.\n"
                     f"<i>(هيتمسح من الشات) • للإلغاء: إلغاء</i>",
                     call.message.chat.id, call.message.message_id)
            return bot.register_next_step_handler(call.message, got_confirm_address, pid, call.message.message_id, 0)
        if row["payload"].get("need_pin"):
            msg = ctx.edit(f"{e('lock')} <b>اكتب الـ PIN لتأكيد التحويل</b>\n<i>(هيتمسح من الشات) • للإلغاء: إلغاء</i>",
                           call.message.chat.id, call.message.message_id)
            return bot.register_next_step_handler(call.message, got_confirm_pin, pid, call.message.message_id)
        run_execution(call.message.chat.id, call.message.message_id, owner_id, pid)

    def got_confirm_address(m, pid, menu_mid, tries):
        delete_quiet(m.chat.id, m.message_id)
        owner_id = m.from_user.id
        if _cancelled(m) or not m.text:
            core.cancel_pending(owner_id, pid)
            return ctx.edit(f"{e('cross')} اتلغى التحويل.", m.chat.id, menu_mid)
        row = core.get_pending(owner_id, pid)
        if not row or row["status"] != "pending" or row["expires_at"] < core.now_ts():
            return ctx.edit(f"{e('clock')} الطلب انتهت صلاحيته. ابعت الأمر من جديد.", m.chat.id, menu_mid)
        if not core.address_matches(row["payload"]["address"], m.text):
            tries += 1
            if tries >= 3:
                core.cancel_pending(owner_id, pid)
                return ctx.edit(f"{e('cross')} العنوان غلط 3 مرات - اتلغى التحويل.", m.chat.id, menu_mid)
            ctx.send(m.chat.id, f"{e('cross')} العنوان مش مطابق للمحفوظ ({3 - tries} محاولات باقية). اكتبه تاني أو إلغاء.")
            return bot.register_next_step_handler(m, got_confirm_address, pid, menu_mid, tries)
        ctx.send(m.chat.id, f"{e('lock')} <b>العنوان مظبوط. اكتب الـ PIN لإتمام التحويل</b>\n<i>(هيتمسح من الشات) • للإلغاء: إلغاء</i>")
        bot.register_next_step_handler(m, got_confirm_pin, pid, menu_mid)

    def got_confirm_pin(m, pid, menu_mid):
        delete_quiet(m.chat.id, m.message_id)
        owner_id = m.from_user.id
        if _cancelled(m) or not m.text:
            core.cancel_pending(owner_id, pid)
            return ctx.edit(f"{e('cross')} اتلغى التحويل.", m.chat.id, menu_mid)
        row = core.get_pending(owner_id, pid)
        if not row or row["status"] != "pending" or row["expires_at"] < core.now_ts():
            return ctx.edit(f"{e('clock')} الطلب انتهت صلاحيته. ابعت الأمر من جديد.", m.chat.id, menu_mid)
        ok, msg = core.check_pin(owner_id, core.to_ascii_digits(m.text.strip()))
        if not ok:
            if "اتقفلت" in msg or "مقفول" in msg:
                core.cancel_pending(owner_id, pid)
                return ctx.edit(f"{e('cross')} {esc(msg)}", m.chat.id, menu_mid)
            ctx.send(m.chat.id, f"{e('cross')} {esc(msg)} اكتب الـ PIN تاني أو إلغاء.")
            return bot.register_next_step_handler(m, got_confirm_pin, pid, menu_mid)
        core.mark_verified(owner_id, pid)
        run_execution(m.chat.id, menu_mid, owner_id, pid)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_no_"))
    def cb_no(call):
        core.cancel_pending(call.from_user.id, call.data[6:])
        bot.answer_callback_query(call.id, "اتلغى.")
        ctx.edit(f"{e('cross')} اتلغى.", call.message.chat.id, call.message.message_id)

    # ------------------------------------------------------------------ invoices
    def invoices_markup(owner_id):
        rows = []
        for i in core.list_invoices(owner_id):
            tag = {"active": "▶️", "paused": "⏸"}.get(i["status"], "•")
            rep = {"monthly": "شهري", "weekly": "أسبوعي", "once": "مرة"}[i["repeat"]]
            rows.append([btn(f"{tag} #{i['id']} {i['client_name'][:14]} • {i['amount']} {i['coin']} • {rep}", f"bn_iv_{i['id']}", "document")])
        rows.append([btn(" المستحقات المفتوحة", "bn_runs", "clock")])
        rows.append([btn(" رجوع", "bn_dash", "arrow_left", "danger")])
        return kb(*rows)

    def invoices_text():
        return (f"{e('document')} <b>الفواتير المجدولة</b>\n\n"
                "لإنشاء فاتورة: افتح شات العميل واكتب (هتتمسح من الشات):\n"
                "<code>فاتورة 100 USDT شهريا عن اشتراك الموقع</code>\n"
                "أو <code>فاتورة 50</code> (مرة واحدة) / <code>فاتورة 30 أسبوعيا</code>")

    @bot.callback_query_handler(func=lambda c: c.data == "bn_inv")
    def cb_inv(call):
        if not is_owner_ok(call) or not need_link(call):
            return
        bot.answer_callback_query(call.id)
        ctx.edit(invoices_text(), call.message.chat.id, call.message.message_id, reply_markup=invoices_markup(call.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_iv_"))
    def cb_iv(call):
        if not is_owner_ok(call):
            return
        i = core.get_invoice(call.from_user.id, int(call.data[6:]))
        if not i:
            return bot.answer_callback_query(call.id, "مش موجودة.")
        bot.answer_callback_query(call.id)
        nxt = core.fmt_ts(i["next_run"]) if i["next_run"] else "—"
        toggle = ("bn_ip_", " إيقاف مؤقت", "clock") if i["status"] == "active" else ("bn_ia_", " تشغيل", "check")
        ctx.edit(
            f"{e('document')} <b>فاتورة #{i['id']}</b>\nالعميل: {esc(i['client_name'])}\nالمبلغ: <b>{i['amount']} {i['coin']}</b>\n"
            f"التكرار: {i['repeat']}\nالبيان: {esc(i['title'] or '—')}\nالإصدار الجاي: {nxt}\nاتبعتت: {i['runs_count']} مرة\nالحالة: {i['status']}",
            call.message.chat.id, call.message.message_id,
            reply_markup=kb([btn(toggle[1], f"{toggle[0]}{i['id']}", toggle[2])],
                            [btn(" حذف", f"bn_id_{i['id']}", "trash", "danger")], [btn(" رجوع", "bn_inv", "arrow_left")]))

    @bot.callback_query_handler(func=lambda c: c.data.startswith(("bn_ip_", "bn_ia_", "bn_id_")))
    def cb_inv_action(call):
        if not is_owner_ok(call):
            return
        act, iid = call.data[:6], int(call.data[6:])
        if act == "bn_ip_":
            core.set_invoice_status(call.from_user.id, iid, "paused")
        elif act == "bn_ia_":
            core.set_invoice_status(call.from_user.id, iid, "active")
        else:
            core.delete_invoice(call.from_user.id, iid)
        bot.answer_callback_query(call.id, "تم.")
        ctx.edit(invoices_text(), call.message.chat.id, call.message.message_id, reply_markup=invoices_markup(call.from_user.id))

    @bot.callback_query_handler(func=lambda c: c.data == "bn_runs")
    def cb_runs(call, answered=False):
        if not is_owner_ok(call) or not need_link(call):
            return
        if not answered:
            bot.answer_callback_query(call.id)
        runs = core.open_runs(call.from_user.id)
        rows = [[btn(f"✅ دفع #{r['id']} {r['client_name'][:12]} • {r['amount']} {r['coin']}", f"bn_paid_{r['id']}", "check")] for r in runs[:15]]
        rows.append([btn(" رجوع", "bn_inv", "arrow_left")])
        ctx.edit(f"{e('clock')} <b>المستحقات المفتوحة</b>\n" + ("\nدوس على الفاتورة لتأكيد دفعها يدويًا (كاش/خارج بينانس)." if runs else "\nمفيش مستحقات."),
                 call.message.chat.id, call.message.message_id, reply_markup=kb(*rows))

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_paid_"))
    def cb_paid(call):
        if not is_owner_ok(call):
            return
        rid = int(call.data[8:])
        run = next((r for r in core.open_runs(call.from_user.id) if r["id"] == rid), None)
        if run:
            core.mark_run_paid(rid, "manual")
            send_receipt(run)
        bot.answer_callback_query(call.id, "اتسجلت كمدفوعة.")
        cb_runs(call, answered=True)

    def invoice_client_text(inv_row, run, owner_id):
        a = linked(owner_id)
        how = esc(a["receive_text"]) if a and a["receive_text"] else "تواصل معايا لتفاصيل الدفع."
        return (
            f"🧾 <b>فاتورة #{run['id']}</b>\n\n"
            f"المطلوب: <b>{run['amount']} {run['coin']}</b>\n"
            + (f"البيان: {esc(inv_row['title'])}\n" if inv_row.get("title") else "")
            + f"آخر موعد للدفع: {core.fmt_ts(run['due_at'])}\n\n"
            f"<b>طريقة الدفع:</b>\n{how}\n\n"
            "هنأكد استلام الدفع تلقائيًا بعد وصوله ✅"
        )

    def send_invoice_run(inv, run):
        owner_id = inv["owner_id"]
        text = invoice_client_text(inv, run, owner_id)
        sent = ctx.send_business(inv["chat_id"], inv["bc_id"], text)
        conn = ctx.owner_connection(owner_id)
        if sent is None and conn:
            ctx.send(conn["owner_chat_id"],
                     f"{e('cross')} <b>مقدرتش أبعت فاتورة #{run['id']} للعميل {esc(inv['client_name'])}</b> (الاتصال Business مقطوع؟). "
                     "انسخها وابعتها يدويًا:\n\n" + text)
        elif conn:
            ctx.send(conn["owner_chat_id"], f"{e('check')} اتبعتت فاتورة #{run['id']} للعميل <b>{esc(inv['client_name'])}</b> ({run['amount']} {run['coin']}).")

    def send_receipt(run):
        a = linked(run["owner_id"])
        if a and a["auto_receipt"]:
            ctx.send_business(run["chat_id"], run["bc_id"],
                              f"✅ <b>تم استلام دفعة الفاتورة #{run['id']}</b> ({run['amount']} {run['coin']}). شكرًا ليك!")

    def finish_invoice_confirm(call, owner_id, pid, row):
        p = row["payload"]
        if not core.claim_pending(owner_id, pid):
            return
        a = linked(owner_id)
        if not a or not a["receive_text"]:
            core.finish_pending(pid, "failed")
            return ctx.edit(f"{e('cross')} حدد <b>طريقة استلام الدفع</b> من الإعدادات الأول (عشان العميل يعرف يدفع فين).",
                            call.message.chat.id, call.message.message_id)
        iid = core.create_invoice(owner_id, p["bc_id"], p["chat_id"], p["client_name"], dec(p["amount"]), p["coin"], p["repeat"], p["title"])
        core.finish_pending(pid, "done")
        ctx.edit(f"{e('check')} <b>اتنشأت فاتورة #{iid}</b> وهتتبعت للعميل خلال دقيقة.", call.message.chat.id, call.message.message_id)
        run_invoice_cycle()

    def draft_invoice(owner_id, owner_chat_id, bc_id, client_chat_id, client_name, parsed):
        coin = (parsed["coin"] or "USDT").upper()
        payload = {"bc_id": bc_id, "chat_id": client_chat_id, "client_name": client_name or "عميل",
                   "amount": fmt_dec(parsed["amount"]), "coin": coin, "repeat": parsed["repeat"], "title": parsed["title"] or ""}
        pid = core.create_pending(owner_id, "invoice", payload, ttl=300)
        rep = {"monthly": "كل شهر", "weekly": "كل أسبوع", "once": "مرة واحدة"}[parsed["repeat"]]
        ctx.send(owner_chat_id,
                 f"{e('document')} <b>تأكيد إنشاء فاتورة</b>\nالعميل: <b>{esc(payload['client_name'])}</b>\n"
                 f"المبلغ: <b>{payload['amount']} {coin}</b>\nالتكرار: {rep}\nالبيان: {esc(payload['title'] or '—')}",
                 reply_markup=kb([btn(" تأكيد وإرسال", f"bn_ok_{pid}", "check", "success"), btn(" إلغاء", f"bn_no_{pid}", "cross", "danger")]))

    # ------------------------------------------------------------------ commands (private chat)
    def _private_match(m):
        try:
            if m.chat.type != "private" or m.content_type != "text" or not m.text or not linked(m.from_user.id):
                return False
            if not section_on(m.from_user.id):
                return False
            t = m.text.strip()
            if (core.parse_binance_transfer(t) or core.parse_transfer(t, private=True)
                    or core.parse_transfer_no_name(t) or core.parse_simple(t)):
                return True
            return False
        except Exception:
            return False

    def run_intent(chat_id, owner_id, intent):
        if intent == "balance":
            return send_balance(chat_id, owner_id)
        if intent == "ledger":
            return ctx.send(chat_id, history_text(owner_id))
        if intent == "summary":
            return ctx.send(chat_id, summary_text(owner_id))
        if intent == "invoices":
            return ctx.send(chat_id, invoices_text(), reply_markup=invoices_markup(owner_id))

    def transfer_from_parsed(chat_id, owner_id, parsed, source=None):
        rec = core.find_recipient(owner_id, parsed["name"])
        if not rec:
            names = "، ".join(r["name"] for r in core.list_recipients(owner_id)) or "لسه مفيش"
            return ctx.send(chat_id, f"{e('cross')} مفيش مستلم باسم «{esc(parsed['name'])}».\nالمحفوظين: {esc(names)}",
                            reply_markup=kb([btn(" إضافة مستلم", "bn_rcp_add", "upload", "success")]))
        start_transfer(chat_id, owner_id, rec, parsed["amount"], parsed.get("coin"), source=source)

    def pick_recipient(chat_id, owner_id, parsed):
        recs = core.list_recipients(owner_id)
        if not recs:
            return ctx.send(chat_id, f"{e('cross')} مفيش مستلمين محفوظين.", reply_markup=kb([btn(" إضافة مستلم", "bn_rcp_add", "upload", "success")]))
        key = core.create_pending(owner_id, "pick", {"amount": fmt_dec(parsed["amount"]), "coin": parsed.get("coin")}, ttl=120)
        rows = [[btn(f" {r['name']} ({r['coin']}/{r['network']})", f"bn_pk_{key}_{r['id']}", "person")] for r in recs[:10]]
        rows.append([btn(" إلغاء", f"bn_no_{key}", "cross", "danger")])
        ctx.send(chat_id, f"{e('lock')} تحوّل <b>{fmt_dec(parsed['amount'])}</b> لمين؟", reply_markup=kb(*rows))

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bn_pk_"))
    def cb_pick(call):
        if not is_owner_ok(call):
            return
        _, _, key, rid = call.data.split("_", 3)
        owner_id = call.from_user.id
        row = core.get_pending(owner_id, key)
        rec = core.get_recipient(owner_id, int(rid))
        if not row or row["kind"] != "pick" or not core.claim_pending(owner_id, key) or not rec:
            return bot.answer_callback_query(call.id, "انتهت صلاحية الاختيار.", show_alert=True)
        core.finish_pending(key, "done")
        bot.answer_callback_query(call.id)
        delete_quiet(call.message.chat.id, call.message.message_id)
        start_transfer(call.message.chat.id, owner_id, rec, dec(row["payload"]["amount"]), row["payload"]["coin"])

    @bot.message_handler(func=_private_match, content_types=["text"])
    def private_commands(message):
        owner_id, chat_id, text = message.from_user.id, message.chat.id, message.text.strip()
        if not ctx.require_active_msg(message):
            return
        simple = core.parse_simple(text)
        if simple:
            return run_intent(chat_id, owner_id, simple)
        explicit = core.parse_binance_transfer(text)
        if explicit:
            if explicit["name"]:
                return transfer_from_parsed(chat_id, owner_id, explicit)
            return pick_recipient(chat_id, owner_id, explicit)
        parsed = core.parse_transfer(text, private=True)
        if parsed:
            return transfer_from_parsed(chat_id, owner_id, parsed)
        bare = core.parse_transfer_no_name(text)
        if bare:
            return pick_recipient(chat_id, owner_id, bare)

    # Our commands must win over generic private handlers registered earlier in bot.py.
    bot.message_handlers.insert(0, bot.message_handlers.pop())

    # ------------------------------------------------------------------ owner commands inside a client chat
    def handle_business_text(message, owner_id, bc_id, owner_chat_id):
        """Called by bot.py for messages the OWNER types in a client chat. Returns True if consumed.

        The command is deleted from the client chat (the client must never see recipients/amounts)
        and every confirmation goes to the owner's private chat.
        """
        text = (message.text or "").strip()
        if not text or not linked(owner_id) or not section_on(owner_id):
            return False
        explicit = core.parse_binance_transfer(text)
        if explicit:
            # explicit "بينانس": always ours (never the currency converter); hide it from the client
            ctx.delete_business_message(bc_id, message.message_id)
            chat = message.chat
            client_name = " ".join(filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)])) or getattr(chat, "title", None) or "عميل"
            if explicit["name"]:
                transfer_from_parsed(owner_chat_id, owner_id, explicit, source=client_name)
            else:
                pick_recipient(owner_chat_id, owner_id, explicit)
            return True
        parsed = core.parse_transfer(text, private=False)
        if parsed and core.find_recipient(owner_id, parsed["name"]):
            ctx.delete_business_message(bc_id, message.message_id)
            chat = message.chat
            client_name = " ".join(filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)])) or getattr(chat, "title", None) or "عميل"
            transfer_from_parsed(owner_chat_id, owner_id, parsed, source=client_name)
            return True
        inv = core.parse_invoice(text)
        if inv:
            ctx.delete_business_message(bc_id, message.message_id)
            chat = message.chat
            name = " ".join(filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)])) or getattr(chat, "title", None) or "عميل"
            draft_invoice(owner_id, owner_chat_id, bc_id, chat.id, name, inv)
            return True
        return False

    # ------------------------------------------------------------------ background workers
    def notify_event(owner_id, chat_id, ev):
        k = ev["kind"]
        if k == "deposit":
            ts = ev.get("ts") or core.now_ts()
            status = f"{e('clock')} قيد التأكيد" if ev["pending"] else f"{e('check')} مكتمل بنجاح"
            ctx.send(chat_id,
                     f"🔵 <b>إشعار إيداع جديد</b>\n\n"
                     f"🏢 المنصة: <b>Binance</b>\n"
                     f"💰 المبلغ: <b>{esc(ev['amount'])} {esc(ev['coin'])}</b>\n"
                     f"{e('clock')} الوقت: <code>{core.fmt_ts_full(ts)}</code>\n"
                     f"{e('link')} النوع: شبكة ({esc(ev.get('network') or 'Network')})\n"
                     f"الحالة: {status}")
        elif k == "pay_in":
            ctx.send(chat_id, card("cash", "استلمت تحويل Binance Pay",
                [f"{e('cash')} المبلغ: <b>+{esc(ev['amount'])} {esc(ev['coin'])}</b>"]
                + ([f"{e('person')} من: {esc(ev['who'])}"] if ev.get("who") else [])))
        elif k == "pay_out":
            ctx.send(chat_id, card("cash_2", "تحويل Pay خارج",
                [f"{e('cash_2')} المبلغ: <b>-{esc(ev['amount'])} {esc(ev['coin'])}</b>"]
                + ([f"{e('person')} إلى: {esc(ev['who'])}"] if ev.get("who") else [])))
        elif k == "withdraw":
            icon_map = {"done": "check", "cancelled": "shield", "rejected": "cross", "failed": "cross"}
            title_map = {"done": "اكتمل السحب", "cancelled": "اتلغى السحب", "rejected": "اترفض السحب", "failed": "فشل السحب"}
            ctx.send(chat_id, card(icon_map[ev["state"]], title_map[ev["state"]],
                [f"{e('cash')} المبلغ: <b>{esc(ev['amount'])} {esc(ev['coin'])}</b>",
                 f"{e('globe')} الشبكة: {esc(ev.get('network') or '—')}"]))

    def handle_incoming_payment(owner_id, chat_id, ev):
        """Auto-match money received against open invoices."""
        run, ambiguous = core.match_payment(owner_id, ev["coin"], ev["amount"])
        if run:
            core.mark_run_paid(run["id"], ev["ref"])
            ctx.send(chat_id, f"🧾 <b>فاتورة #{run['id']} اتسددت تلقائيًا</b> ✅\nالعميل: {esc(run['client_name'])} • {run['amount']} {run['coin']}")
            send_receipt(run)
        elif ambiguous:
            names = "، ".join(f"#{r['id']} {r['client_name']}" for r in ambiguous[:5])
            ctx.send(chat_id, f"{e('shield')} الدفعة دي ({esc(ev['amount'])} {esc(ev['coin'])}) تنفع لأكتر من فاتورة: {esc(names)}.\n"
                              "أكّد الصح يدويًا من: الفواتير ← المستحقات المفتوحة.")

    def watcher_cycle():
        for owner_id in core.linked_owner_ids():
            if not section_on(owner_id):
                continue
            acc = core.get_account(owner_id)
            conn = ctx.owner_connection(owner_id)
            chat_id = conn["owner_chat_id"] if conn else owner_id
            try:
                events = core.poll_events(owner_id)
            except Exception as exc:
                print(f"[binance] watcher error owner={owner_id}: {exc}", flush=True)
                continue
            for ev in events:
                try:
                    if ev["kind"] in ("deposit", "pay_in") and not ev.get("pending"):
                        core.ledger_add(owner_id, "in", ev["coin"], ev["amount"], None, ev.get("who") or ev.get("network"), "ok", str(ev["ref"]))
                    if acc["notify_on"]:
                        notify_event(owner_id, chat_id, ev)
                    if ev["kind"] in ("deposit", "pay_in") and not ev.get("pending"):
                        handle_incoming_payment(owner_id, chat_id, ev)
                except Exception as exc:
                    print(f"[binance] notify error owner={owner_id}: {exc}", flush=True)

    def run_invoice_cycle():
        for inv in core.due_invoices():
            if not section_on(inv["owner_id"]):
                continue
            try:
                run = core.issue_run(inv)
                send_invoice_run(inv, run)
            except Exception as exc:
                print(f"[binance] invoice issue error #{inv['id']}: {exc}", flush=True)
        for r in core.runs_needing_attention():
            if not section_on(r["owner_id"]):
                continue
            try:
                a = core.get_account(r["owner_id"])
                core.bump_reminder(r["id"])
                conn = ctx.owner_connection(r["owner_id"])
                if a and a["auto_remind"]:
                    ctx.send_business(r["chat_id"], r["bc_id"],
                                      f"⏰ <b>تذكير ودّي:</b> فاتورة #{r['id']} ({r['amount']} {r['coin']}) عدّى موعد سدادها. لو دفعت تجاهل الرسالة 🙏")
                if conn:
                    ctx.send(conn["owner_chat_id"], f"⏰ فاتورة #{r['id']} متأخرة: {esc(r['client_name'])} • {r['amount']} {r['coin']} (تذكير {r['reminders'] + 1}/3)")
            except Exception as exc:
                print(f"[binance] reminder error run #{r['id']}: {exc}", flush=True)

    def _loop(fn, interval, name):
        def runner():
            time.sleep(10)
            while True:
                try:
                    fn()
                except Exception as exc:
                    print(f"[binance] {name} crashed: {exc}", flush=True)
                time.sleep(interval)
        threading.Thread(target=runner, daemon=True, name=name).start()

    def start_background():
        _loop(watcher_cycle, WATCH_INTERVAL, "bn-watcher")
        _loop(run_invoice_cycle, INVOICE_INTERVAL, "bn-invoices")

    return Ctx(handle_business_text=handle_business_text, start_background=start_background,
               menu_text=menu_text, menu_markup=menu_markup)
