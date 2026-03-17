import asyncio
import json
import logging
import re
import random
import string

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from config import BOT_TOKEN
from storage import Storage

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

storage = Storage()

MAILTM_API  = "https://api.mail.tm"
MERCURE_HUB = "https://mercure.mail.tm/.well-known/mercure"

_sse_tasks: dict[str, dict[str, asyncio.Task]] = {}


# ─── API helpers ──────────────────────────────────────────────────────────────

def random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(random.choices(chars, k=length))


async def api_get(session, path: str, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with session.get(
            f"{MAILTM_API}{path}", headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json() if r.status == 200 else None
    except Exception as e:
        logger.error(f"GET {path}: {e}")
        return None


async def api_post(session, path: str, payload: dict):
    try:
        async with session.post(
            f"{MAILTM_API}{path}", json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status in (200, 201):
                return await r.json()
            logger.warning(f"POST {path} → {r.status}: {await r.text()}")
            return None
    except Exception as e:
        logger.error(f"POST {path}: {e}")
        return None


async def get_domains(session) -> list[str]:
    data = await api_get(session, "/domains")
    return [d["domain"] for d in data.get("hydra:member", [])] if isinstance(data, dict) else []


async def get_token(session, address: str, password: str) -> str | None:
    data = await api_post(session, "/token", {"address": address, "password": password})
    return data.get("token") if data else None


async def get_messages(session, token: str) -> list[dict]:
    data = await api_get(session, "/messages", token)
    return data.get("hydra:member", []) if isinstance(data, dict) else []


async def get_message_detail(session, token: str, msg_id: str):
    return await api_get(session, f"/messages/{msg_id}", token)


def escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_body(detail: dict) -> str:
    body = detail.get("text", "") or ""
    if not body:
        html = (detail.get("html") or [""])[0]
        body = re.sub(r"<[^>]+>", " ", html)
        body = re.sub(r"\s+", " ", body).strip()
    return body


# ─── Mercure SSE listener ─────────────────────────────────────────────────────

async def listen_sse(app: Application, uid: str, address: str) -> None:
    while True:
        acc_data = storage.get_accounts(uid).get(address)
        if not acc_data:
            logger.info(f"SSE [{address}]: removed, stopping.")
            return

        token      = acc_data.get("token", "")
        account_id = acc_data.get("account_id", "")

        if not token or not account_id:
            await asyncio.sleep(10)
            continue

        url     = f"{MERCURE_HUB}?topic=/accounts/{account_id}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

        logger.info(f"SSE [{address}]: connecting…")
        try:
            connector = aiohttp.TCPConnector(ssl=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=None, connect=15)) as resp:
                    if resp.status != 200:
                        logger.warning(f"SSE [{address}] status {resp.status}")
                        await asyncio.sleep(30)
                        continue

                    logger.info(f"SSE [{address}]: connected ✓")
                    buf: list[str] = []

                    async for raw in resp.content:
                        line = raw.decode("utf-8", errors="replace").rstrip("\n")
                        if line.startswith("data:"):
                            buf.append(line[5:].strip())
                        elif line == "" and buf:
                            payload_str = "\n".join(buf)
                            buf = []
                            try:
                                payload = json.loads(payload_str)
                                await _on_event(app, session, uid, address, token)
                            except json.JSONDecodeError:
                                pass

        except asyncio.CancelledError:
            logger.info(f"SSE [{address}]: cancelled.")
            return
        except Exception as e:
            logger.warning(f"SSE [{address}] error: {e} — retry in 15s")
            await asyncio.sleep(15)


async def _on_event(app, session, uid: str, address: str, token: str) -> None:
    try:
        messages  = await get_messages(session, token)
        known_ids = storage.get_known_ids(uid, address)
        for msg in messages:
            if msg["id"] not in known_ids:
                storage.add_known_id(uid, address, msg["id"])
                detail = await get_message_detail(session, token, msg["id"])
                await _notify(app, int(uid), address, msg, detail)
    except Exception as e:
        logger.error(f"_on_event [{address}]: {e}")


async def _notify(app, user_id: int, address: str, msg: dict, detail) -> None:
    from_addr = msg.get("from", {}).get("address", "unknown")
    subject   = msg.get("subject", "(no subject)")
    body      = extract_body(detail) if detail else ""
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
        logger.error(f"notify uid={user_id}: {e}")


def _start_sse(app, uid: str, address: str) -> None:
    _sse_tasks.setdefault(uid, {})
    t = _sse_tasks[uid].get(address)
    if t and not t.done():
        return
    _sse_tasks[uid][address] = asyncio.create_task(listen_sse(app, uid, address))
    logger.info(f"SSE task started: [{address}]")


def _stop_sse(uid: str, address: str) -> None:
    t = _sse_tasks.get(uid, {}).get(address)
    if t and not t.done():
        t.cancel()


async def restore_sse_listeners(app: Application) -> None:
    await asyncio.sleep(2)
    for uid, accounts in storage.get_all_accounts().items():
        for address in accounts:
            _start_sse(app, uid, address)


# ─── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Mail.tm Monitor Bot</b>\n\n"
        "⚡️ Уведомления <b>мгновенные</b> — через real-time SSE, без поллинга!\n\n"
        "Команды:\n"
        "  /new [username] — создать новую почту\n"
        "  /list — список твоих почт\n"
        "  /inbox — просмотреть письма\n"
        "  /remove — удалить почту\n\n"
        "Пример:\n"
        "  <code>/new</code> — рандомное имя\n"
        "  <code>/new myname</code> — создаст <code>myname@domain</code>",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = str(update.effective_user.id)
    accounts = storage.get_accounts(uid)
    if len(accounts) >= 10:
        await update.message.reply_text("❌ Лимит: 10 почт на пользователя.")
        return

    msg = await update.message.reply_text("⏳ Создаю почту…")

    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        domains = await get_domains(session)
        if not domains:
            await msg.edit_text("❌ Нет доменов. Попробуй позже.")
            return

        domain   = domains[0]
        username = re.sub(r"[^a-z0-9._-]", "", ctx.args[0].strip().lower())[:40] if ctx.args else \
                   "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        address  = f"{username}@{domain}"
        password = random_password()

        if address in accounts:
            await msg.edit_text(f"⚠️ <code>{escape(address)}</code> уже есть.", parse_mode="HTML")
            return

        acc = await api_post(session, "/accounts", {"address": address, "password": password})
        if not acc:
            username = username + "".join(random.choices(string.digits, k=4))
            address  = f"{username}@{domain}"
            password = random_password()
            acc = await api_post(session, "/accounts", {"address": address, "password": password})

        if not acc:
            await msg.edit_text("❌ Не удалось создать почту. Попробуй другое имя.")
            return

        account_id = acc.get("id", "")
        token      = await get_token(session, address, password)
        storage.add_account(uid, address, password, token or "", account_id)

    await msg.edit_text(
        f"✅ <b>Почта создана!</b>\n\n"
        f"📧 Адрес: <code>{address}</code>\n"
        f"🔑 Пароль: <code>{password}</code>\n\n"
        f"⚠️ Сохрани пароль — он больше не отображается!\n"
        f"⚡️ Real-time мониторинг запущен.",
        parse_mode="HTML",
    )
    _start_sse(ctx.application, uid, address)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = str(update.effective_user.id)
    accounts = storage.get_accounts(uid)
    if not accounts:
        await update.message.reply_text("📭 Нет почт. Создай: <code>/new</code>", parse_mode="HTML")
        return

    lines = [f"📬 <b>Твои почты ({len(accounts)}):</b>\n"]
    for addr in sorted(accounts):
        t = _sse_tasks.get(uid, {}).get(addr)
        status = "🟢" if t and not t.done() else "🔴"
        lines.append(f"{status} <code>{addr}</code>")
    lines.append("\n🟢 real-time активен  🔴 нет соединения")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = str(update.effective_user.id)
    accounts = storage.get_accounts(uid)
    if not accounts:
        await update.message.reply_text("📭 Нет почт. Создай: <code>/new</code>", parse_mode="HTML")
        return

    if ctx.args:
        address = re.sub(r"[^a-z0-9@._-]", "", ctx.args[0].strip().lower())
        if address not in accounts:
            await update.message.reply_text(f"❌ <code>{escape(address)}</code> не найден.", parse_mode="HTML")
            return
        await _show_inbox(update.message, ctx, uid, address)
    elif len(accounts) == 1:
        await _show_inbox(update.message, ctx, uid, list(accounts)[0])
    else:
        buttons = [[InlineKeyboardButton(f"📬 {a}", callback_data=f"inbox:{a}")] for a in sorted(accounts)]
        await update.message.reply_text("Выбери почту:", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_inbox(target, ctx, uid: str, address: str) -> None:
    is_query = hasattr(target, "edit_message_text")
    loader   = f"🔄 Загружаю <code>{escape(address)}</code>…"
    if is_query:
        await target.edit_message_text(loader, parse_mode="HTML")
        send = target.edit_message_text
    else:
        sm   = await target.reply_text(loader, parse_mode="HTML")
        send = sm.edit_text

    acc_data = storage.get_accounts(uid).get(address, {})
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        token    = acc_data.get("token") or await get_token(session, address, acc_data.get("password", ""))
        messages = await get_messages(session, token) if token else []

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

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"inbox:{address}")])
    await send("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def callback_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await _show_inbox(q, ctx, str(q.from_user.id), q.data.split(":", 1)[1])


async def callback_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    await q.answer("⏳ Загружаю…")
    uid = str(q.from_user.id)
    _, address, msg_id = q.data.split(":", 2)

    acc_data = storage.get_accounts(uid).get(address)
    if not acc_data:
        await q.edit_message_text("❌ Почта не найдена.")
        return

    await q.edit_message_text("🔄 Читаю письмо…")
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        token  = acc_data.get("token") or await get_token(session, address, acc_data["password"])
        detail = await get_message_detail(session, token, msg_id) if token else None

    if not detail:
        await q.edit_message_text("❌ Письмо не найдено.")
        return

    from_addr = detail.get("from", {}).get("address", "?")
    subject   = detail.get("subject", "(no subject)")
    body      = extract_body(detail)
    preview   = (body[:2500] + "\n<i>…обрезано</i>") if len(body) > 2500 else body

    text = (
        f"📨 <b>{escape(subject)}</b>\n\n"
        f"👤 От: {escape(from_addr)}\n"
        f"📧 Ящик: <code>{escape(address)}</code>\n"
        f"{'─'*28}\n\n<pre>{escape(preview)}</pre>"
    )
    await q.edit_message_text(text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"inbox:{address}")]]))


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = str(update.effective_user.id)
    accounts = storage.get_accounts(uid)
    if not accounts:
        await update.message.reply_text("📭 Нет почт для удаления.")
        return
    buttons = [[InlineKeyboardButton(f"🗑 {a}", callback_data=f"rm:{a}")] for a in sorted(accounts)]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="rm:cancel")])
    await update.message.reply_text("Выбери почту для удаления:", reply_markup=InlineKeyboardMarkup(buttons))


async def callback_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    if q.data == "rm:cancel":
        await q.edit_message_text("Отменено.")
        return
    address = q.data.split(":", 1)[1]
    _stop_sse(uid, address)
    storage.remove_account(uid, address)
    await q.edit_message_text(f"🗑 Удалено: <code>{escape(address)}</code>", parse_mode="HTML")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("new",    cmd_new))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("inbox",  cmd_inbox))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(callback_inbox,  pattern=r"^inbox:"))
    app.add_handler(CallbackQueryHandler(callback_read,   pattern=r"^read:"))
    app.add_handler(CallbackQueryHandler(callback_remove, pattern=r"^rm:"))
    app.job_queue.run_once(lambda ctx: asyncio.create_task(restore_sse_listeners(app)), when=2)
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
