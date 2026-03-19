"""
cabbit.py — Кеббит мини-игра
"""
import asyncio
import logging
import random
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

# ─── Константы ────────────────────────────────────────────────────────────────

BOX_INTERVAL    = 30 * 60    # 30 минут
WARN_12H        = 12 * 3600  # предупреждение через 12ч без кормёжки
WARN_23H        = 23 * 3600  # критическое предупреждение через 23ч
DEATH_24H       = 24 * 3600  # смерть через 24ч
NAMING_STATE    = 1

FOOD_TABLE = [
    ("Морковь",   "🥕", 60,  80),
    ("Корм",      "🍗", 20,  200),
    ("Вкусность", "✨", 20,  500),
]

def xp_for_level(level: int) -> int:
    return int(100 * (level ** 1.6))


# ─── Хранилище ────────────────────────────────────────────────────────────────

class CabbitStorage:
    FILE = "cabbit.json"

    def _load(self) -> dict:
        import json, os
        if not os.path.exists(self.FILE):
            return {}
        try:
            with open(self.FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        import json
        with open(self.FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, uid: str) -> dict | None:
        return self._load().get(uid)

    def create(self, uid: str, name: str) -> dict:
        now = int(time.time())
        cabbit = {
            "name": name,
            "xp": 0,
            "level": 1,
            "box_available": True,
            "box_ts": 0,
            "last_fed": now,          # время последней кормёжки
            "warned_12h": False,      # уже отправили предупреждение 12ч?
            "warned_23h": False,      # уже отправили предупреждение 23ч?
            "dead": False,
            "food_counts": {"Морковь": 0, "Корм": 0, "Вкусность": 0},
        }
        data = self._load()
        data[uid] = cabbit
        self._save(data)
        return cabbit

    def save_cabbit(self, uid: str, cabbit: dict) -> None:
        data = self._load()
        data[uid] = cabbit
        self._save(data)

    def get_all(self) -> dict:
        return self._load()


cabbit_db = CabbitStorage()


# ─── Игровая логика ───────────────────────────────────────────────────────────

def roll_food() -> tuple[str, str, int]:
    r = random.randint(1, 100)
    cum = 0
    for name, emoji, chance, xp in FOOD_TABLE:
        cum += chance
        if r <= cum:
            return name, emoji, xp
    return FOOD_TABLE[0][0], FOOD_TABLE[0][1], FOOD_TABLE[0][3]


def hunger_bar(cabbit: dict) -> str:
    """Показывает индикатор голода."""
    now       = int(time.time())
    last_fed  = cabbit.get("last_fed", now)
    elapsed   = now - last_fed
    pct_alive = max(0, 100 - int(elapsed / DEATH_24H * 100))

    filled = pct_alive // 10
    bar    = "❤️" * filled + "🖤" * (10 - filled)

    if pct_alive > 60:
        mood = "Сытый и довольный 😊"
    elif pct_alive > 30:
        mood = "Немного голоден 😐"
    elif pct_alive > 10:
        mood = "Очень голоден! 😨"
    else:
        mood = "Умирает от голода! 💀"

    return f"{bar} {pct_alive}%\n{mood}"


def cabbit_status(cabbit: dict) -> str:
    name   = cabbit["name"]
    level  = cabbit["level"]
    xp     = cabbit["xp"]
    needed = xp_for_level(level)
    pct    = min(int(xp / needed * 100), 100)

    filled = pct // 10
    bar    = "█" * filled + "░" * (10 - filled)

    box_ts    = cabbit.get("box_ts", 0)
    now       = int(time.time())
    box_avail = cabbit.get("box_available", True) or now >= box_ts

    if box_avail:
        box_str = "📦 Коробка готова!"
    else:
        secs_left = max(0, box_ts - now)
        mins = secs_left // 60
        secs = secs_left % 60
        box_str = f"⏳ Следующая коробка через {mins}м {secs}с"

    counts    = cabbit.get("food_counts", {})
    food_str  = " | ".join(f"{e} {counts.get(n, 0)}" for n, e, _, _ in FOOD_TABLE)
    hunger    = hunger_bar(cabbit)

    return (
        f"🐰 <b>{name}</b>\n"
        f"⭐️ Уровень: <b>{level}</b>\n"
        f"📊 XP: <b>{xp}</b> / <b>{needed}</b>\n"
        f"[{bar}] {pct}%\n\n"
        f"❤️ Здоровье:\n{hunger}\n\n"
        f"🍽 Съедено: {food_str}\n\n"
        f"{box_str}"
    )


def cabbit_keyboard(cabbit: dict) -> InlineKeyboardMarkup:
    now       = int(time.time())
    box_ts    = cabbit.get("box_ts", 0)
    box_avail = cabbit.get("box_available", True) or now >= box_ts
    buttons   = []
    if box_avail:
        buttons.append([InlineKeyboardButton("📦 Открыть коробку", callback_data="cabbit:box")])
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

CABBIT_PHOTO = "cabbit.jpg"


async def cmd_cabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)

    if not cabbit:
        await update.message.reply_text(
            "🐰 У тебя ещё нет кеббита!\n\nКак ты хочешь его назвать?"
        )
        return NAMING_STATE

    if cabbit.get("dead"):
        await update.message.reply_text(
            f"💀 <b>{cabbit['name']} умер от голода...</b>\n\n"
            f"Ты не кормил его 24 часа. Кеббит ушёл в лучший мир.\n\n"
            f"Хочешь завести нового? Напиши имя:",
            parse_mode="HTML",
        )
        # Удаляем мёртвого кеббита
        data = cabbit_db._load()
        data.pop(uid, None)
        cabbit_db._save(data)
        return NAMING_STATE

    await _send_cabbit_card(update.message, uid, cabbit)
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
        f"Твой кеббит готов к приключениям.\n"
        f"Каждые 30 минут появляется коробка с едой — не забывай кормить!\n"
        f"⚠️ Если не кормить 24 часа — кеббит умрёт.",
        parse_mode="HTML",
    )
    await _send_cabbit_card(update.message, uid, cabbit)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def _send_cabbit_card(msg, uid: str, cabbit: dict):
    import os
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
        await q.answer("💀 Твой кеббит умер. Напиши /cabbit чтобы завести нового.", show_alert=True)
        return

    if action == "refresh":
        status = cabbit_status(cabbit)
        kb     = cabbit_keyboard(cabbit)
        try:
            await q.edit_message_caption(caption=status, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await q.edit_message_text(status, parse_mode="HTML", reply_markup=kb)
        return

    if action == "box":
        now    = int(time.time())
        box_ts = cabbit.get("box_ts", 0)
        if not (cabbit.get("box_available", True) or now >= box_ts):
            await q.answer("⏳ Коробка ещё не готова!", show_alert=True)
            return

        food_name, food_emoji, food_xp = roll_food()
        leveled_up, new_level = apply_xp(cabbit, food_xp)

        counts = cabbit.setdefault("food_counts", {"Морковь": 0, "Корм": 0, "Вкусность": 0})
        counts[food_name] = counts.get(food_name, 0) + 1

        cabbit["box_available"] = False
        cabbit["box_ts"]        = now + BOX_INTERVAL
        cabbit["last_fed"]      = now
        cabbit["warned_12h"]    = False   # сброс предупреждений после кормёжки
        cabbit["warned_23h"]    = False

        cabbit_db.save_cabbit(uid, cabbit)

        text = (
            f"📦 <b>Коробка открыта!</b>\n\n"
            f"Выпало: {food_emoji} <b>{food_name}</b>\n"
            f"✨ +{food_xp} XP\n"
        )
        if leveled_up:
            text += f"\n🎉 <b>УРОВЕНЬ {new_level}!</b> Кеббит растёт!\n"

        text += f"\n{cabbit_status(cabbit)}"
        kb = cabbit_keyboard(cabbit)

        try:
            await q.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


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

                name      = cabbit.get("name", "Кеббит")
                last_fed  = cabbit.get("last_fed", now)
                elapsed   = now - last_fed

                # ── Смерть через 24ч ──────────────────────────────────────
                if elapsed >= DEATH_24H:
                    cabbit["dead"] = True
                    cabbit_db.save_cabbit(uid, cabbit)
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=(
                                f"💀 <b>{name} умер от голода...</b>\n\n"
                                f"Ты не кормил его 24 часа. Кеббит ушёл в лучший мир.\n"
                                f"Напиши /cabbit чтобы завести нового. 😢"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"death notify uid={uid}: {e}")
                    continue

                # ── Критическое предупреждение 23ч ───────────────────────
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
                                f"Если не покормить в течение {mins_left} минут — он умрёт навсегда!\n\n"
                                f"Скорее напиши /cabbit!"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"warn_23h notify uid={uid}: {e}")
                    continue

                # ── Предупреждение 12ч ────────────────────────────────────
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
                                f"Напиши /cabbit чтобы открыть коробку."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"warn_12h notify uid={uid}: {e}")

                # ── Коробка готова ────────────────────────────────────────
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
                                f"Напиши /cabbit чтобы открыть."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"box notify uid={uid}: {e}")

        except Exception as e:
            logger.error(f"box_notifier error: {e}")
