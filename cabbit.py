"""
cabbit.py — Кеббит мини-игра
Обратно совместимо: все новые поля читаются через .get() с дефолтами.
"""
import asyncio
import json
import logging
import os
import random
import time
from threading import Lock

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
)
import duel as duel_module

logger = logging.getLogger(__name__)

# ─── Константы ────────────────────────────────────────────────────────────────

BOX_INTERVAL = 30 * 60
WARN_12H     = 12 * 3600
WARN_23H     = 23 * 3600
DEATH_24H    = 24 * 3600
NAMING_STATE = 1
KNIFE_CHANCE = 0.5          # 0.5% шанс выпадения ножа

FOOD_TABLE = [
    ("Морковь",   "🥕", 60,  80),
    ("Корм",      "🍗", 20,  200),
    ("Вкусность", "✨", 20,  500),
]

def xp_for_level(level: int) -> int:
    return int(100 * (level ** 1.6))


# ─── Хранилище ────────────────────────────────────────────────────────────────

class CabbitStorage:
    FILE  = "/app/data/cabbit.json"
    _lock = Lock()

    def _load(self) -> dict:
        if not os.path.exists(self.FILE):
            return {}
        try:
            with open(self.FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        with open(self.FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, uid: str) -> dict | None:
        return self._load().get(uid)

    def create(self, uid: str, name: str) -> dict:
        now    = int(time.time())
        cabbit = {
            "name": name,
            "xp": 0,
            "level": 1,
            "box_available": True,
            "box_ts": 0,
            "last_fed": now,
            "warned_12h": False,
            "warned_23h": False,
            "dead": False,
            "has_knife": False,
            "food_counts": {"Морковь": 0, "Корм": 0, "Вкусность": 0},
        }
        with self._lock:
            data = self._load()
            data[uid] = cabbit
            self._save(data)
        return cabbit

    def save_cabbit(self, uid: str, cabbit: dict) -> None:
        with self._lock:
            data = self._load()
            data[uid] = cabbit
            self._save(data)

    def get_all(self) -> dict:
        return self._load()

    def knife_owner(self) -> str | None:
        """Возвращает uid владельца ножа или None."""
        for uid, c in self._load().items():
            if c.get("has_knife") and not c.get("dead"):
                return uid
        return None


cabbit_db = CabbitStorage()


# ─── Игровая логика ───────────────────────────────────────────────────────────

def roll_box(uid: str) -> tuple[str, str, int, bool]:
    """
    Возвращает (food_name, emoji, xp, got_knife).
    got_knife=True только если: выпал шанс И нож никем не занят.
    """
    # Проверяем нож
    knife_roll = random.random() * 100 < KNIFE_CHANCE
    if knife_roll and cabbit_db.knife_owner() is None:
        return "Нож", "🔪", 0, True

    r   = random.randint(1, 100)
    cum = 0
    for name, emoji, chance, xp in FOOD_TABLE:
        cum += chance
        if r <= cum:
            return name, emoji, xp, False
    return FOOD_TABLE[0][0], FOOD_TABLE[0][1], FOOD_TABLE[0][3], False


def hunger_bar(cabbit: dict) -> str:
    now      = int(time.time())
    last_fed = cabbit.get("last_fed", now)
    elapsed  = now - last_fed
    pct      = max(0, 100 - int(elapsed / DEATH_24H * 100))
    filled   = pct // 10
    bar      = "❤️" * filled + "🖤" * (10 - filled)

    if pct > 60:   mood = "Сытый и довольный 😊"
    elif pct > 30: mood = "Немного голоден 😐"
    elif pct > 10: mood = "Очень голоден! 😨"
    else:          mood = "Умирает от голода! 💀"
    return f"{bar} {pct}%\n{mood}"


def cabbit_status(cabbit: dict) -> str:
    name    = cabbit["name"]
    level   = cabbit["level"]
    xp      = cabbit["xp"]
    needed  = xp_for_level(level)
    pct     = min(int(xp / needed * 100), 100)
    bar     = "█" * (pct // 10) + "░" * (10 - pct // 10)

    now       = int(time.time())
    box_ts    = cabbit.get("box_ts", 0)
    box_avail = cabbit.get("box_available", True) or now >= box_ts

    if box_avail:
        box_str = "📦 Коробка готова!"
    else:
        left = max(0, box_ts - now)
        box_str = f"⏳ Следующая коробка через {left//60}м {left%60}с"

    counts   = cabbit.get("food_counts", {})
    food_str = " | ".join(f"{e} {counts.get(n,0)}" for n,e,_,_ in FOOD_TABLE)
    knife_str = "\n🔪 <b>У тебя есть нож!</b> /knife чтобы использовать." if cabbit.get("has_knife") else ""

    return (
        f"🐰 <b>{name}</b>\n"
        f"⭐️ Уровень: <b>{level}</b>\n"
        f"📊 XP: <b>{xp}</b> / <b>{needed}</b>\n"
        f"[{bar}] {pct}%\n\n"
        f"❤️ Здоровье:\n{hunger_bar(cabbit)}\n\n"
        f"🍽 Съедено: {food_str}"
        f"{knife_str}"
        f"{token_str}\n\n"
        f"{box_str}"
    )


def cabbit_keyboard(cabbit: dict) -> InlineKeyboardMarkup:
    now       = int(time.time())
    box_ts    = cabbit.get("box_ts", 0)
    box_avail = cabbit.get("box_available", True) or now >= box_ts
    buttons   = []
    if box_avail:
        buttons.append([InlineKeyboardButton("📦 Открыть коробку", callback_data="cabbit:box")])
    if cabbit.get("has_knife"):
        buttons.append([InlineKeyboardButton("🔪 Использовать нож", callback_data="cabbit:knife")])
    if cabbit.get("duel_tokens", 0) > 0:
        buttons.append([InlineKeyboardButton("🥊 Вызвать на дуэль", callback_data="cabbit:duel")])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="cabbit:refresh")])
    return InlineKeyboardMarkup(buttons)


def apply_xp(cabbit: dict, xp: int) -> tuple[bool, int]:
    cabbit["xp"] += xp
    leveled_up = False
    while cabbit["xp"] >= xp_for_level(cabbit["level"]):
        cabbit["xp"] -= xp_for_level(cabbit["level"])
        cabbit["level"] += 1
        leveled_up = True
    return leveled_up, cabbit["level"]


# ─── Хендлеры ─────────────────────────────────────────────────────────────────

CABBIT_PHOTO = "/app/cabbit.jpg"


async def cmd_cabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)

    if not cabbit:
        await update.message.reply_text(
            "🐰 У тебя ещё нет кеббита!\n\nКак ты хочешь его назвать?"
        )
        return NAMING_STATE

    if cabbit.get("dead"):
        name = cabbit.get("name", "Кеббит")
        await update.message.reply_text(
            f"💀 <b>{name} умер от голода...</b>\n\n"
            f"Ты не кормил его 24 часа. Кеббит ушёл в лучший мир.\n\n"
            f"Хочешь завести нового? Напиши имя:",
            parse_mode="HTML",
        )
        with cabbit_db._lock:
            data = cabbit_db._load()
            data.pop(uid, None)
            cabbit_db._save(data)
        return NAMING_STATE

    await _send_cabbit_card(update.message, cabbit)
    return ConversationHandler.END


async def receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    name = update.message.text.strip()[:20]

    if not name:
        await update.message.reply_text("Имя не может быть пустым, попробуй ещё раз:")
        return NAMING_STATE

    cabbit = cabbit_db.create(uid, name)
    await update.message.reply_text(
        f"🎉 Познакомьтесь — <b>{name}</b>!\n\n"
        f"Каждые 30 минут появляется коробка с едой — не забывай кормить!\n"
        f"⚠️ Если не кормить 24 часа — кеббит умрёт.",
        parse_mode="HTML",
    )
    await _send_cabbit_card(update.message, cabbit)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def _send_cabbit_card(msg, cabbit: dict):
    status = cabbit_status(cabbit)
    kb     = cabbit_keyboard(cabbit)
    if os.path.exists(CABBIT_PHOTO):
        try:
            with open(CABBIT_PHOTO, "rb") as f:
                await msg.reply_photo(photo=f, caption=status, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await msg.reply_text(status, parse_mode="HTML", reply_markup=kb)


async def callback_cabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    uid    = str(q.from_user.id)
    action = q.data.split(":")[1]
    cabbit = cabbit_db.get(uid)

    if not cabbit:
        await q.answer("❌ Сначала создай кеббита через /cabbit", show_alert=True)
        return
    if cabbit.get("dead"):
        await q.answer("💀 Твой кеббит умер. Напиши /cabbit", show_alert=True)
        return

    if action == "refresh":
        await _edit_card(q, cabbit)
        return

    if action == "knife":
        if not cabbit.get("has_knife"):
            await q.answer("У тебя нет ножа!", show_alert=True)
            return
        await _show_knife_targets(q, uid)
        return

    if action == "duel":
        if cabbit.get("duel_tokens", 0) <= 0:
            await q.answer("У тебя нет жетонов дуэли!", show_alert=True)
            return
        await duel_module.show_duel_targets(q, uid, cabbit_db, _edit_card)
        return

    if action == "box":
        now    = int(time.time())
        box_ts = cabbit.get("box_ts", 0)
        if not (cabbit.get("box_available", True) or now >= box_ts):
            await q.answer("⏳ Коробка ещё не готова!", show_alert=True)
            return

        food_name, food_emoji, food_xp, got_knife = roll_box(uid)

        if got_knife:
            cabbit["has_knife"]     = True
            cabbit["box_available"] = False
            cabbit["box_ts"]        = now + BOX_INTERVAL
            cabbit["last_fed"]      = now
            cabbit["warned_12h"]    = False
            cabbit["warned_23h"]    = False
            cabbit_db.save_cabbit(uid, cabbit)
            text = (
                f"📦 <b>Коробка открыта!</b>\n\n"
                f"🔪 <b>ВАУ! Выпал НОЖ!</b>\n"
                f"Ты можешь убить чужого кеббита!\n"
                f"Нажми кнопку ниже или напиши /knife\n\n"
                f"{cabbit_status(cabbit)}"
            )
            await _edit_card(q, cabbit, text)
            return

        leveled_up, new_level = apply_xp(cabbit, food_xp)
        counts = cabbit.setdefault("food_counts", {"Морковь": 0, "Корм": 0, "Вкусность": 0})
        counts[food_name] = counts.get(food_name, 0) + 1
        cabbit["box_available"] = False
        cabbit["box_ts"]        = now + BOX_INTERVAL
        cabbit["last_fed"]      = now
        cabbit["warned_12h"]    = False
        cabbit["warned_23h"]    = False
        cabbit["duel_tokens"]   = cabbit.get("duel_tokens", 0) + 1
        cabbit_db.save_cabbit(uid, cabbit)

        text = f"📦 <b>Коробка открыта!</b>\n\nВыпало: {food_emoji} <b>{food_name}</b>\n✨ +{food_xp} XP\n🥊 +1 жетон дуэли\n"
        if leveled_up:
            text += f"\n🎉 <b>УРОВЕНЬ {new_level}!</b> Кеббит растёт!\n"
        text += f"\n{cabbit_status(cabbit)}"
        await _edit_card(q, cabbit, text)


async def _edit_card(q, cabbit: dict, text: str = None):
    status = text or cabbit_status(cabbit)
    kb     = cabbit_keyboard(cabbit)
    try:
        await q.edit_message_caption(caption=status, parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await q.edit_message_text(status, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


async def _show_knife_targets(q, attacker_uid: str):
    all_   = cabbit_db.get_all()
    others = [
        (uid, c) for uid, c in all_.items()
        if uid != attacker_uid and not c.get("dead")
    ]
    if not others:
        await q.answer("Нет других живых кеббитов для атаки!", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(
            f"🐰 {c['name']} (ур. {c['level']})",
            callback_data=f"kill:{uid}"
        )]
        for uid, c in others
    ]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="kill:cancel")])
    try:
        await q.edit_message_caption(
            caption="🔪 <b>Выбери жертву:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        await q.edit_message_text(
            "🔪 <b>Выбери жертву:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def callback_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q            = update.callback_query
    await q.answer()
    attacker_uid = str(q.from_user.id)
    target_uid   = q.data.split(":")[1]

    if target_uid == "cancel":
        cabbit = cabbit_db.get(attacker_uid)
        if cabbit:
            await _edit_card(q, cabbit)
        return

    attacker = cabbit_db.get(attacker_uid)
    target   = cabbit_db.get(target_uid)

    if not attacker or not attacker.get("has_knife"):
        await q.answer("У тебя нет ножа!", show_alert=True)
        return
    if not target or target.get("dead"):
        await q.answer("Этот кеббит уже мёртв!", show_alert=True)
        return

    target_name   = target["name"]
    attacker_name = attacker["name"]

    # Убиваем
    target["dead"] = True
    cabbit_db.save_cabbit(target_uid, target)

    # Забираем нож
    attacker["has_knife"] = False
    cabbit_db.save_cabbit(attacker_uid, attacker)

    # Уведомляем жертву
    try:
        await ctx.application.bot.send_message(
            chat_id=int(target_uid),
            text=(
                f"💀 <b>{target_name} был убит!</b>\n\n"
                f"🔪 Кеббит <b>{attacker_name}</b> нанёс смертельный удар ножом.\n"
                f"Напиши /cabbit чтобы завести нового."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"kill notify target={target_uid}: {e}")

    text = (
        f"🔪 <b>{target_name} убит!</b>\n\n"
        f"Нож сломался после использования.\n\n"
        f"{cabbit_status(attacker)}"
    )
    await _edit_card(q, attacker, text)


async def cmd_knife(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await update.message.reply_text("❌ У тебя нет живого кеббита.")
        return
    if not cabbit.get("has_knife"):
        await update.message.reply_text("🔪 У тебя нет ножа.")
        return

    all_   = cabbit_db.get_all()
    others = [(u, c) for u, c in all_.items() if u != uid and not c.get("dead")]
    if not others:
        await update.message.reply_text("Нет других живых кеббитов для атаки!")
        return

    buttons = [
        [InlineKeyboardButton(f"🐰 {c['name']} (ур. {c['level']})", callback_data=f"kill:{u}")]
        for u, c in others
    ]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="kill:cancel")])
    await update.message.reply_text(
        "🔪 <b>Выбери жертву:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    all_  = cabbit_db.get_all()
    alive = [(uid, c) for uid, c in all_.items() if not c.get("dead")]
    if not alive:
        await update.message.reply_text("🏆 Пока нет живых кеббитов.")
        return

    alive.sort(key=lambda x: (x[1]["level"], x[1]["xp"]), reverse=True)
    lines = ["🏆 <b>Лидерборд кеббитов:</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, c) in enumerate(alive[:10], 1):
        medal = medals[i-1] if i <= 3 else f"{i}."
        knife = " 🔪" if c.get("has_knife") else ""
        lines.append(
            f"{medal} <b>{c['name']}</b> — ур. {c['level']} "
            f"({c['xp']} XP){knife}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── Фоновый таймер ───────────────────────────────────────────────────────────

async def box_notifier(app) -> None:
    logger.info("Cabbit box notifier started.")
    while True:
        await asyncio.sleep(60)
        try:
            now  = int(time.time())
            all_ = cabbit_db.get_all()

            for uid, cabbit in all_.items():
                if cabbit.get("dead"):
                    continue

                name     = cabbit.get("name", "Кеббит")
                last_fed = cabbit.get("last_fed", now)
                elapsed  = now - last_fed

                # Смерть 24ч
                if elapsed >= DEATH_24H:
                    cabbit["dead"] = True
                    cabbit_db.save_cabbit(uid, cabbit)
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=(
                                f"💀 <b>{name} умер от голода...</b>\n\n"
                                f"Ты не кормил его 24 часа.\n"
                                f"Напиши /cabbit чтобы завести нового. 😢"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"death notify uid={uid}: {e}")
                    continue

                # Критическое 23ч
                if elapsed >= WARN_23H and not cabbit.get("warned_23h"):
                    cabbit["warned_23h"] = True
                    cabbit_db.save_cabbit(uid, cabbit)
                    mins_left = (DEATH_24H - elapsed) // 60
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=(
                                f"☠️ <b>СРОЧНО! {name} умирает!</b>\n\n"
                                f"Кеббит не ел уже 23 часа!\n"
                                f"Осталось <b>{mins_left} минут</b> — потом он умрёт навсегда!\n\n"
                                f"Скорее /cabbit!"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"warn_23h uid={uid}: {e}")
                    continue

                # Предупреждение 12ч
                if elapsed >= WARN_12H and not cabbit.get("warned_12h"):
                    cabbit["warned_12h"] = True
                    cabbit_db.save_cabbit(uid, cabbit)
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=(
                                f"⚠️ <b>{name} голодает!</b>\n\n"
                                f"Кеббит не ел уже 12 часов.\n"
                                f"Покорми его или он умрёт через 12 часов!\n\n"
                                f"/cabbit → 📦 Открыть коробку"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"warn_12h uid={uid}: {e}")

                # Коробка готова — беззвучно
                box_ts    = cabbit.get("box_ts", 0)
                box_avail = cabbit.get("box_available", True)
                if not box_avail and now >= box_ts:
                    cabbit["box_available"] = True
                    cabbit_db.save_cabbit(uid, cabbit)
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=(
                                f"📦 <b>Новая коробка с едой!</b>\n\n"
                                f"🐰 {name} ждёт — не забудь покормить!\n"
                                f"/cabbit чтобы открыть."
                            ),
                            parse_mode="HTML",
                            disable_notification=True,   # беззвучно
                        )
                    except Exception as e:
                        logger.warning(f"box notify uid={uid}: {e}")

        except Exception as e:
            logger.error(f"box_notifier error: {e}")
