import asyncio
import email
import imaplib
import json
import logging
import random
import re
import string
from email.header import decode_header

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, ZOHO_EMAIL, ZOHO_PASSWORD, ZOHO_DOMAIN, ZOHO_IMAP_HOST
from storage import Storage
from cabbit import (
    cmd_cabbit, receive_name, receive_name_from_rules, cancel,
    callback_cabbit, callback_rules,
    callback_kill, cmd_knife, cmd_leaderboard, cmd_raid,
    cmd_prestige, callback_use_item, cmd_bancabbit, cmd_cabbitlist,
    cmd_broadcast, cmd_addxp,
    cmd_skins, callback_skin_select, cmd_shop, callback_shop_buy,
    cmd_profile,
    cmd_addskin, cmd_skindrop, cmd_skinlevel, cmd_skinprice,
    cmd_removeskin, cmd_giveskin, cmd_addcoins, cmd_listskins,
    box_notifier, NAMING_STATE, cabbit_db,
)
from duel import (
    callback_duel_send, callback_duel_stake,
    callback_duel_accept, callback_duel_decline, callback_duel_move,
)
from promo import cmd_promo, cmd_createpromo, cmd_listpromos, cmd_deletepromo
from casino import cmd_casino
from quests import cmd_quests, callback_quest_claim
from achievements import cmd_achievements
from reaction import callback_reaction, reaction_notifier

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

storage = Storage()

MAILTM_API  = "https://api.mail.tm"
MERCURE_HUB = "https://mercure.mail.tm/.well-known/mercure"

_sse_tasks: dict[str, dict[str, asyncio.Task]] = {}
_zoho_task: asyncio.Task | None = None


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def random_password(length: int = 16) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits + "!@#$%", k=length))


def random_username(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_body_mailtm(detail: dict) -> str:
    body = detail.get("text", "") or ""
    if not body:
        html = (detail.get("html") or [""])[0]
        body = re.sub(r"<[^>]+>", " ", html)
        body = re.sub(r"\s+", " ", body).strip()
    return body


def decode_mime_header(value: str) -> str:
    parts = decode_header(value)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


# ─── mail.tm API ──────────────────────────────────────────────────────────────

async def mailtm_get(session, path: str, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with session.get(f"{MAILTM_API}{path}", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json() if r.status == 200 else None
    except Exception as e:
        logger.error(f"mailtm GET {path}: {e}")
        return None


async def mailtm_post(session, path: str, payload: dict):
    try:
        async with session.post(f"{MAILTM_API}{path}", json=payload,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json() if r.status in (200, 201) else None
    except Exception as e:
        logger.error(f"mailtm POST {path}: {e}")
        return None


async def mailtm_get_domains(session) -> list[str]:
    data = await mailtm_get(session, "/domains")
    return [d["domain"] for d in data.get("hydra:member", [])] if isinstance(data, dict) else []


async def mailtm_get_token(session, address: str, password: str) -> str | None:
    data = await mailtm_post(session, "/token", {"address": address, "password": password})
    return data.get("token") if data else None


async def mailtm_get_messages(session, token: str) -> list[dict]:
    data = await mailtm_get(session, "/messages", token)
    return data.get("hydra:member", []) if isinstance(data, dict) else []


async def mailtm_get_detail(session, token: str, msg_id: str):
    return await mailtm_get(session, f"/messages/{msg_id}", token)


# ─── Zoho IMAP ────────────────────────────────────────────────────────────────

def zoho_imap_connect() -> imaplib.IMAP4_SSL | None:
    try:
        imap = imaplib.IMAP4_SSL(ZOHO_IMAP_HOST, 993)
        imap.login(ZOHO_EMAIL, ZOHO_PASSWORD)
        return imap
    except Exception as e:
        logger.error(f"IMAP connect error: {e}")
        return None


def _safe_decode(payload: bytes, charset: str) -> str:
    """Decode bytes with fallback for unknown/broken charsets."""
    charset = (charset or "utf-8").lower().strip()
    # Map broken charset names to valid ones
    # Normalize and map broken charset names
    charset_map = {
        "unknown-8bit": "latin-1",
        "unknown-8Bit": "latin-1",
        "unknown-8BIT": "latin-1",
        "x-unknown": "latin-1",
        "unknown": "latin-1",
        "default": "utf-8",
    }
    charset = charset_map.get(charset, charset_map.get(charset.lower(), charset))
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("latin-1", errors="replace")


def _parse_body(msg) -> str:
    """Extract text body from email message with full encoding fallback."""
    body = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = _safe_decode(payload, charset)
                        break
            if not body:
                # fallback: try html part
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            html = _safe_decode(payload, charset)
                            body = re.sub(r"<[^>]+>", " ", html)
                            body = re.sub(r"\s+", " ", body).strip()
                            break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = _safe_decode(payload, charset)
    except Exception as e:
        logger.warning(f"Body parse error: {e}")
    return body


def zoho_fetch_new_messages(imap: imaplib.IMAP4_SSL) -> list[dict]:
    messages = []
    try:
        imap.select("INBOX")
        _, data = imap.search(None, "ALL")
        ids = data[0].split()
        for uid in ids:
            try:
                _, msg_data = imap.fetch(uid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = decode_mime_header(msg.get("Subject", "(no subject)"))
                from_   = decode_mime_header(msg.get("From", "unknown"))
                to_     = decode_mime_header(msg.get("To", ""))
                date_   = msg.get("Date", "")
                body    = _parse_body(msg)

                messages.append({
                    "id": uid.decode(),
                    "subject": subject,
                    "from": from_,
                    "to": to_,
                    "date": date_,
                    "body": body,
                })
            except Exception as e:
                logger.warning(f"IMAP skip msg {uid}: {e}")
                continue
    except Exception as e:
        logger.error(f"IMAP fetch error: {e}")
    return messages


async def poll_zoho(app: Application) -> None:
    """Поллинг Zoho IMAP каждые 5 секунд."""
    logger.info("Zoho IMAP poller started.")
    while True:
        try:
            loop = asyncio.get_event_loop()
            messages = await loop.run_in_executor(None, _zoho_fetch_sync)

            logger.info(f"Zoho poll: fetched {len(messages)} messages total")

            all_zoho = storage.get_all_zoho_addresses()
            logger.info(f"Zoho poll: watching addresses: {all_zoho}")

            for msg in messages:
                to_addr = msg["to"].lower()
                logger.info(f"Zoho poll: checking msg id={msg['id']} to={to_addr}")
                for uid, addresses in all_zoho.items():
                    for addr in addresses:
                        if addr.lower() in to_addr:
                            known = storage.get_known_ids(uid, addr, source="zoho")
                            logger.info(f"Zoho poll: match! addr={addr} known_count={len(known)} msg_id={msg['id']} new={msg['id'] not in known}")
                            if msg["id"] not in known:
                                storage.add_known_id(uid, addr, msg["id"], source="zoho")
                                await _notify_zoho(app, int(uid), addr, msg)
                                logger.info(f"Zoho poll: notified uid={uid} addr={addr}")

        except Exception as e:
            logger.error(f"Zoho poll error: {e}", exc_info=True)

        await asyncio.sleep(5)


def _zoho_fetch_sync() -> list[dict]:
    imap = zoho_imap_connect()
    if not imap:
        return []
    msgs = zoho_fetch_new_messages(imap)
    try:
        imap.logout()
    except Exception:
        pass
    return msgs


async def _notify_zoho(app, user_id: int, address: str, msg: dict) -> None:
    body    = msg.get("body", "")
    preview = (body[:800] + "…") if len(body) > 800 else body
    text = (
        f"📬 <b>Новое письмо!</b>\n\n"
        f"📧 Ящик: <code>{escape(address)}</code>\n"
        f"👤 От: <b>{escape(msg['from'])}</b>\n"
        f"📌 Тема: <b>{escape(msg['subject'])}</b>\n"
        f"🕐 Дата: {escape(msg['date'])}\n\n"
        f"<pre>{escape(preview)}</pre>"
    )
    try:
        await app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"notify_zoho uid={user_id}: {e}")


# ─── mail.tm Mercure SSE ──────────────────────────────────────────────────────

async def listen_sse(app: Application, uid: str, address: str) -> None:
    while True:
        acc_data = storage.get_mailtm_accounts(uid).get(address)
        if not acc_data:
            return

        token      = acc_data.get("token", "")
        account_id = acc_data.get("account_id", "")
        if not token or not account_id:
            await asyncio.sleep(10)
            continue

        url     = f"{MERCURE_HUB}?topic=/accounts/{account_id}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

        try:
            connector = aiohttp.TCPConnector(ssl=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=None, connect=15)) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(30)
                        continue

                    logger.info(f"SSE [{address}]: connected ✓")
                    buf: list[str] = []
                    async for raw in resp.content:
                        line = raw.decode("utf-8", errors="replace").rstrip("\n")
                        if line.startswith("data:"):
                            buf.append(line[5:].strip())
                        elif line == "" and buf:
                            buf = []
                            await _on_mailtm_event(app, session, uid, address, token)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"SSE [{address}] error: {e} — retry in 15s")
            await asyncio.sleep(15)


async def _on_mailtm_event(app, session, uid: str, address: str, token: str) -> None:
    messages  = await mailtm_get_messages(session, token)
    known_ids = storage.get_known_ids(uid, address, source="mailtm")
    for msg in messages:
        if msg["id"] not in known_ids:
            storage.add_known_id(uid, address, msg["id"], source="mailtm")
            detail = await mailtm_get_detail(session, token, msg["id"])
            await _notify_mailtm(app, int(uid), address, msg, detail)


async def _notify_mailtm(app, user_id: int, address: str, msg: dict, detail) -> None:
    from_addr = msg.get("from", {}).get("address", "unknown")
    subject   = msg.get("subject", "(no subject)")
    body      = extract_body_mailtm(detail) if detail else ""
    preview   = (body[:800] + "…") if len(body) > 800 else body
    text = (
        f"📬 <b>Новое письмо!</b>\n\n"
        f"📧 Ящик: <code>{escape(address)}</code>\n"
        f"👤 От: <b>{escape(from_addr)}</b>\n"
        f"📌 Тема: <b>{escape(subject)}</b>\n\n"
        f"<pre>{escape(preview)}</pre>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Читать полностью", callback_data=f"read:{address}:{msg['id']}")
    ]])
    try:
        await app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"notify_mailtm uid={user_id}: {e}")


def _start_sse(app, uid: str, address: str) -> None:
    _sse_tasks.setdefault(uid, {})
    t = _sse_tasks[uid].get(address)
    if t and not t.done():
        return
    _sse_tasks[uid][address] = asyncio.create_task(listen_sse(app, uid, address))


def _stop_sse(uid: str, address: str) -> None:
    t = _sse_tasks.get(uid, {}).get(address)
    if t and not t.done():
        t.cancel()


async def restore_listeners(app: Application) -> None:
    await asyncio.sleep(2)
    for uid, accounts in storage.get_all_mailtm_accounts().items():
        for address in accounts:
            _start_sse(app, uid, address)


# ─── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>MailBot</b>\n\n"
        "📬 <b>Почта:</b>\n"
        "  /new — создать почту\n"
        "  /list — все почты\n"
        "  /inbox — читать письма\n"
        "  /remove — удалить почту\n",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_helpcabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🐰 <b>Кеббит — команды:</b>\n\n"
        "  /cabbit — твой питомец\n"
        "  /casino СТАВКА — слот-машина\n"
        "  /raid — украсть XP\n"
        "  /quests — ежедневные квесты\n"
        "  /achievements — достижения\n"
        "  /leaderboard — топ игроков\n"
        "  /prestige — престиж (ур. 30+)\n"
        "  /knife — использовать нож\n",
        parse_mode="HTML",
    )


# /new — выбор домена через кнопки
async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = str(update.effective_user.id)
    username = re.sub(r"[^a-z0-9._-]", "", ctx.args[0].strip().lower())[:40] if ctx.args else ""

    # Сохраняем желаемый username во временное хранилище контекста
    ctx.user_data["pending_username"] = username

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📦 mail.tm", callback_data="create:tm"),
        InlineKeyboardButton(f"🌐 {ZOHO_DOMAIN}", callback_data="create:zh"),
    ]])
    await update.message.reply_text(
        "Выбери домен для новой почты:",
        reply_markup=kb,
    )


async def callback_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q        = update.callback_query
    await q.answer()
    uid      = str(q.from_user.id)
    source   = q.data.split(":")[1]  # tm или zh
    username = ctx.user_data.pop("pending_username", "")

    if source == "tm":
        await _create_mailtm(q, ctx, uid, username)
    else:
        await _create_zoho(q, uid, username)


async def _create_mailtm(query, ctx, uid: str, username: str) -> None:
    if len(storage.get_mailtm_accounts(uid)) >= 10:
        await query.edit_message_text("❌ Лимит: 10 mail.tm почт.")
        return

    await query.edit_message_text("⏳ Создаю почту на mail.tm…")

    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        domains = await mailtm_get_domains(session)
        if not domains:
            await query.edit_message_text("❌ Нет доменов. Попробуй позже.")
            return

        domain   = domains[0]
        uname    = username or random_username()
        address  = f"{uname}@{domain}"
        password = random_password()

        if address in storage.get_mailtm_accounts(uid):
            await query.edit_message_text(f"⚠️ <code>{escape(address)}</code> уже есть.", parse_mode="HTML")
            return

        acc = await mailtm_post(session, "/accounts", {"address": address, "password": password})
        if not acc:
            uname    = random_username()
            address  = f"{uname}@{domain}"
            password = random_password()
            acc = await mailtm_post(session, "/accounts", {"address": address, "password": password})

        if not acc:
            await query.edit_message_text("❌ Не удалось создать. Попробуй позже.")
            return

        account_id = acc.get("id", "")
        token      = await mailtm_get_token(session, address, password)
        storage.add_mailtm_account(uid, address, password, token or "", account_id)

    await query.edit_message_text(
        f"✅ <b>Почта mail.tm создана!</b>\n\n"
        f"📧 Адрес: <code>{address}</code>\n"
        f"🔑 Пароль: <code>{password}</code>\n\n"
        f"⚡️ Real-time мониторинг запущен.",
        parse_mode="HTML",
    )
    _start_sse(ctx.application, uid, address)


async def _create_zoho(query, uid: str, username: str) -> None:
    zoho_addresses = storage.get_zoho_addresses(uid)
    if len(zoho_addresses) >= 20:
        await query.edit_message_text("❌ Лимит: 20 адресов на домене.")
        return

    uname   = username or random_username()
    address = f"{uname}@{ZOHO_DOMAIN}"

    if address in zoho_addresses:
        await query.edit_message_text(
            f"⚠️ <code>{escape(address)}</code> уже отслеживается.", parse_mode="HTML"
        )
        return

    storage.add_zoho_address(uid, address)
    await query.edit_message_text(
        f"✅ <b>Адрес добавлен!</b>\n\n"
        f"📧 <code>{address}</code>\n\n"
        f"Все письма на этот адрес будут приходить сюда.",
        parse_mode="HTML",
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    mailtm  = storage.get_mailtm_accounts(uid)
    zoho    = storage.get_zoho_addresses(uid)

    if not mailtm and not zoho:
        await update.message.reply_text(
            "📭 Нет почт.\n/new — mail.tm\n/zoho — свой домен", parse_mode="HTML"
        )
        return

    lines = ["📬 <b>Твои почты:</b>\n"]
    if mailtm:
        lines.append("📦 <b>mail.tm:</b>")
        for addr in sorted(mailtm):
            t = _sse_tasks.get(uid, {}).get(addr)
            s = "🟢" if t and not t.done() else "🔴"
            lines.append(f"  {s} <code>{addr}</code>")

    if zoho:
        lines.append(f"\n🌐 <b>@{ZOHO_DOMAIN}:</b>")
        for addr in sorted(zoho):
            lines.append(f"  🟢 <code>{addr}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid    = str(update.effective_user.id)
    mailtm = storage.get_mailtm_accounts(uid)
    zoho   = storage.get_zoho_addresses(uid)
    all_   = list(sorted(mailtm)) + list(sorted(zoho))

    if not all_:
        await update.message.reply_text("📭 Нет почт.")
        return

    if len(all_) == 1:
        address = all_[0]
        if address in mailtm:
            await _show_mailtm_inbox(update.message, ctx, uid, address)
        else:
            await _show_zoho_inbox(update.message, uid, address)
        return

    buttons = []
    for a in sorted(mailtm):
        buttons.append([InlineKeyboardButton(f"📦 {a}", callback_data=f"inbox:tm:{a}")])
    for a in sorted(zoho):
        buttons.append([InlineKeyboardButton(f"🌐 {a}", callback_data=f"inbox:zh:{a}")])
    await update.message.reply_text("Выбери почту:", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_mailtm_inbox(target, ctx, uid: str, address: str) -> None:
    is_query = hasattr(target, "edit_message_text")
    loader   = f"🔄 Загружаю <code>{escape(address)}</code>…"
    if is_query:
        await target.edit_message_text(loader, parse_mode="HTML")
        send = target.edit_message_text
    else:
        sm = await target.reply_text(loader, parse_mode="HTML")
        send = sm.edit_text

    acc_data  = storage.get_mailtm_accounts(uid).get(address, {})
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        token    = acc_data.get("token") or await mailtm_get_token(session, address, acc_data.get("password", ""))
        messages = await mailtm_get_messages(session, token) if token else []

    if not messages:
        await send(f"📭 Ящик <code>{escape(address)}</code> пуст.", parse_mode="HTML")
        return

    lines   = [f"📬 <b>{escape(address)}</b> — {len(messages)} письмо(а):\n"]
    buttons = []
    for i, m in enumerate(messages[:15], 1):
        from_addr = m.get("from", {}).get("address", "?")
        subject   = m.get("subject", "(no subject)")
        icon      = "✉️" if not m.get("seen") else "📭"
        lines.append(f"{icon} {i}. <b>{escape(subject)}</b>\n   👤 {escape(from_addr)}")
        buttons.append([InlineKeyboardButton(f"📖 #{i} {subject[:35]}", callback_data=f"read:{address}:{m['id']}")])

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"inbox:tm:{address}")])
    await send("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_zoho_inbox(target, uid: str, address: str) -> None:
    is_query = hasattr(target, "edit_message_text")
    loader   = f"🔄 Загружаю <code>{escape(address)}</code>…"
    if is_query:
        await target.edit_message_text(loader, parse_mode="HTML")
        send = target.edit_message_text
    else:
        sm = await target.reply_text(loader, parse_mode="HTML")
        send = sm.edit_text

    loop     = asyncio.get_event_loop()
    messages = await loop.run_in_executor(None, _zoho_fetch_for_address, address)

    if not messages:
        await send(f"📭 Нет писем для <code>{escape(address)}</code>.", parse_mode="HTML")
        return

    lines   = [f"📬 <b>{escape(address)}</b> — {len(messages)} письмо(а):\n"]
    buttons = []
    for i, m in enumerate(messages[:15], 1):
        lines.append(f"✉️ {i}. <b>{escape(m['subject'])}</b>\n   👤 {escape(m['from'])}")
        buttons.append([InlineKeyboardButton(
            f"📖 #{i} {m['subject'][:35]}",
            callback_data=f"readzh:{address}:{m['id']}",
        )])

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"inbox:zh:{address}")])
    await send("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


def _zoho_fetch_for_address(address: str) -> list[dict]:
    imap = zoho_imap_connect()
    if not imap:
        return []
    try:
        imap.select("INBOX")
        _, data = imap.search(None, f'TO "{address}"')
        ids = data[0].split()[-15:]  # последние 15
        messages = []
        for uid in reversed(ids):
            _, msg_data = imap.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject", "(no subject)"))
            from_   = decode_mime_header(msg.get("From", "unknown"))
            date_   = msg.get("Date", "")
            body    = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        charset = part.get_content_charset() or "utf-8"
                        body = part.get_payload(decode=True).decode(charset, errors="replace")
                        break
            else:
                charset = msg.get_content_charset() or "utf-8"
                body = msg.get_payload(decode=True).decode(charset, errors="replace")
            messages.append({"id": uid.decode(), "subject": subject, "from": from_, "date": date_, "body": body})
        return messages
    except Exception as e:
        logger.error(f"zoho fetch for {address}: {e}")
        return []
    finally:
        try:
            imap.logout()
        except Exception:
            pass


async def callback_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    _, source, address = q.data.split(":", 2)
    if source == "tm":
        await _show_mailtm_inbox(q, ctx, uid, address)
    else:
        await _show_zoho_inbox(q, uid, address)


async def callback_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    await q.answer("⏳ Загружаю…")
    uid = str(q.from_user.id)
    _, address, msg_id = q.data.split(":", 2)

    acc_data = storage.get_mailtm_accounts(uid).get(address)
    if not acc_data:
        await q.edit_message_text("❌ Почта не найдена.")
        return

    await q.edit_message_text("🔄 Читаю письмо…")
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        token  = acc_data.get("token") or await mailtm_get_token(session, address, acc_data["password"])
        detail = await mailtm_get_detail(session, token, msg_id) if token else None

    if not detail:
        await q.edit_message_text("❌ Письмо не найдено.")
        return

    from_addr = detail.get("from", {}).get("address", "?")
    subject   = detail.get("subject", "(no subject)")
    body      = extract_body_mailtm(detail)
    preview   = (body[:2500] + "\n<i>…обрезано</i>") if len(body) > 2500 else body

    text = (
        f"📨 <b>{escape(subject)}</b>\n\n"
        f"👤 От: {escape(from_addr)}\n"
        f"📧 Ящик: <code>{escape(address)}</code>\n"
        f"{'─'*28}\n\n<pre>{escape(preview)}</pre>"
    )
    await q.edit_message_text(text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"inbox:tm:{address}")]]))


async def callback_read_zoho(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    await q.answer("⏳ Загружаю…")
    _, address, msg_id = q.data.split(":", 2)

    await q.edit_message_text("🔄 Читаю письмо…")
    loop    = asyncio.get_event_loop()
    messages = await loop.run_in_executor(None, _zoho_fetch_for_address, address)
    msg     = next((m for m in messages if m["id"] == msg_id), None)

    if not msg:
        await q.edit_message_text("❌ Письмо не найдено.")
        return

    body    = msg.get("body", "")
    preview = (body[:2500] + "\n<i>…обрезано</i>") if len(body) > 2500 else body

    text = (
        f"📨 <b>{escape(msg['subject'])}</b>\n\n"
        f"👤 От: {escape(msg['from'])}\n"
        f"📧 Ящик: <code>{escape(address)}</code>\n"
        f"🕐 Дата: {escape(msg['date'])}\n"
        f"{'─'*28}\n\n<pre>{escape(preview)}</pre>"
    )
    await q.edit_message_text(text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data=f"inbox:zh:{address}")
        ]]))


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid    = str(update.effective_user.id)
    mailtm = storage.get_mailtm_accounts(uid)
    zoho   = storage.get_zoho_addresses(uid)

    if not mailtm and not zoho:
        await update.message.reply_text("📭 Нет почт для удаления.")
        return

    buttons = []
    for a in sorted(mailtm):
        buttons.append([InlineKeyboardButton(f"🗑 📦 {a}", callback_data=f"rm:tm:{a}")])
    for a in sorted(zoho):
        buttons.append([InlineKeyboardButton(f"🗑 🌐 {a}", callback_data=f"rm:zh:{a}")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="rm:cancel")])
    await update.message.reply_text("Выбери почту для удаления:", reply_markup=InlineKeyboardMarkup(buttons))


async def callback_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)

    if q.data == "rm:cancel":
        await q.edit_message_text("Отменено.")
        return

    _, source, address = q.data.split(":", 2)
    if source == "tm":
        _stop_sse(uid, address)
        storage.remove_mailtm_account(uid, address)
    else:
        storage.remove_zoho_address(uid, address)

    await q.edit_message_text(f"🗑 Удалено: <code>{escape(address)}</code>", parse_mode="HTML")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("helpcabbit", cmd_helpcabbit))
    app.add_handler(CommandHandler("new",    cmd_new))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("inbox",  cmd_inbox))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(callback_create, pattern=r"^create:"))
    app.add_handler(CallbackQueryHandler(callback_inbox,  pattern=r"^inbox:"))
    app.add_handler(CallbackQueryHandler(callback_read,    pattern=r"^read:"))
    app.add_handler(CallbackQueryHandler(callback_read_zoho, pattern=r"^readzh:"))
    app.add_handler(CallbackQueryHandler(callback_remove, pattern=r"^rm:"))

    # Cabbit conversation handler
    cabbit_conv = ConversationHandler(
        entry_points=[CommandHandler("cabbit", cmd_cabbit)],
        states={NAMING_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(cabbit_conv)
    app.add_handler(CallbackQueryHandler(callback_rules,   pattern=r"^rules:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name_from_rules), group=1)
    app.add_handler(CallbackQueryHandler(callback_cabbit,  pattern=r"^cabbit:"))
    app.add_handler(CallbackQueryHandler(callback_kill,         pattern=r"^kill:"))
    app.add_handler(CallbackQueryHandler(callback_duel_send,    pattern=r"^duel_send:"))
    app.add_handler(CallbackQueryHandler(callback_duel_stake,   pattern=r"^duel_stake:"))
    app.add_handler(CallbackQueryHandler(callback_duel_accept,  pattern=r"^duel_accept:"))
    app.add_handler(CallbackQueryHandler(callback_duel_decline, pattern=r"^duel_decline:"))
    app.add_handler(CallbackQueryHandler(callback_duel_move,    pattern=r"^duel_move:"))
    app.add_handler(CommandHandler("knife",        cmd_knife))
    app.add_handler(CommandHandler("leaderboard",  cmd_leaderboard))
    app.add_handler(CommandHandler("casino",       cmd_casino))
    app.add_handler(CommandHandler("quests",       cmd_quests))
    app.add_handler(CommandHandler("achievements", cmd_achievements))
    app.add_handler(CommandHandler("raid",         cmd_raid))
    app.add_handler(CommandHandler("prestige",     cmd_prestige))
    app.add_handler(CommandHandler("bancabbit",    cmd_bancabbit))
    app.add_handler(CommandHandler("cabbitlist",   cmd_cabbitlist))
    app.add_handler(CommandHandler("broadcast",    cmd_broadcast))
    app.add_handler(CommandHandler("addxp",        cmd_addxp))
    # Skins — players
    app.add_handler(CommandHandler("skins",        cmd_skins))
    app.add_handler(CommandHandler("shop",         cmd_shop))
    app.add_handler(CommandHandler("profile",      cmd_profile))
    app.add_handler(CallbackQueryHandler(callback_skin_select, pattern=r"^skin_sel:"))
    app.add_handler(CallbackQueryHandler(callback_shop_buy,    pattern=r"^shop_buy:"))
    # Skins — admin
    app.add_handler(CommandHandler("addskin",      cmd_addskin))
    app.add_handler(CommandHandler("skindrop",     cmd_skindrop))
    app.add_handler(CommandHandler("skinlevel",    cmd_skinlevel))
    app.add_handler(CommandHandler("skinprice",    cmd_skinprice))
    app.add_handler(CommandHandler("removeskin",   cmd_removeskin))
    app.add_handler(CommandHandler("giveskin",     cmd_giveskin))
    app.add_handler(CommandHandler("addcoins",     cmd_addcoins))
    app.add_handler(CommandHandler("listskins",    cmd_listskins))
    app.add_handler(CommandHandler("promo",        cmd_promo))
    app.add_handler(CommandHandler("createpromo",  cmd_createpromo))
    app.add_handler(CommandHandler("listpromos",   cmd_listpromos))
    app.add_handler(CommandHandler("deletepromo",  cmd_deletepromo))
    app.add_handler(CallbackQueryHandler(callback_use_item,    pattern=r"^use_item:"))
    app.add_handler(CallbackQueryHandler(callback_quest_claim, pattern=r"^quest_claim:"))
    app.add_handler(CallbackQueryHandler(callback_reaction,    pattern=r"^reaction:"))

    app.job_queue.run_once(lambda ctx: asyncio.create_task(restore_listeners(app)), when=2)
    app.job_queue.run_once(lambda ctx: asyncio.create_task(poll_zoho(app)), when=3)
    app.job_queue.run_once(lambda ctx: asyncio.create_task(box_notifier(app)), when=4)
    app.job_queue.run_once(lambda ctx: asyncio.create_task(reaction_notifier(app)), when=5)

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
