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

BOX_INTERVAL   = 30 * 60   # 30 минут в секундах
NAMING_STATE   = 1         # состояние ConversationHandler

# Еда: (название, эмодзи, шанс%, XP)
FOOD_TABLE = [
    ("Морковь",   "🥕", 60,  80),
    ("Корм",      "🍗", 20,  200),
    ("Вкусность", "✨", 20,  500),
]

# XP нужно для каждого уровня (индекс = уровень)
# level 1 = 100 xp, level 2 = 250, level 3 = 500 ...
def xp_for_level(level: int) -> int:
    return int(100 * (level ** 1.6))


def total_xp_for_level(level: int) -> int:
    """Суммарный XP нужный чтобы достичь level."""
    return sum(xp_for_level(i) for i in range(1, level))


# ─── Хранилище кеббита ────────────────────────────────────────────────────────

class CabbitStorage:
    """Кеббит хранится в отдельном файле cabbit.json."""

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
        cabbit = {
            "name": name,
            "xp": 0,
            "level": 1,
            "box_available": True,
            "box_ts": 0,         # когда появится следующая коробка (unix)
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
    """Возвращает (название, эмодзи, xp) случайной еды."""
    r = random.randint(1, 100)
    cum = 0
    for name, emoji, chance, xp in FOOD_TABLE:
        cum += chance
        if r <= cum:
            return name, emoji, xp
    return FOOD_TABLE[0][0], FOOD_TABLE[0][1], FOOD_TABLE[0][3]


def cabbit_status(cabbit: dict) -> str:
    name   = cabbit["name"]
    level  = cabbit["level"]
    xp     = cabbit["xp"]
    needed = xp_for_level(level)
    pct    = min(int(xp / needed * 100), 100)

    # прогресс-бар
    filled = pct // 10
    bar    = "█" * filled + "░" * (10 - filled)

    box_ts  = cabbit.get("box_ts", 0)
    now     = int(time.time())
    box_avail = cabbit.get("box_available", True) or now >= box_ts

    if box_avail:
        box_str = "📦 Коробка готова!"
    else:
        secs_left = max(0, box_ts - now)
        mins = secs_left // 60
        secs = secs_left % 60
        box_str = f"⏳ Следующая коробка через {mins}м {secs}с"

    counts = cabbit.get("food_counts", {})
    food_str = " | ".join(
        f"{e} {counts.get(n, 0)}" for n, e, _, _ in FOOD_TABLE
    )

    return (
        f"🐰 <b>{name}</b>\n"
        f"⭐️ Уровень: <b>{level}</b>\n"
        f"📊 XP: <b>{xp}</b> / <b>{needed}</b>\n"
        f"[{bar}] {pct}%\n\n"
        f"🍽 Съедено: {food_str}\n\n"
        f"{box_str}"
    )


def cabbit_keyboard(cabbit: dict) -> InlineKeyboardMarkup:
    now     = int(time.time())
    box_ts  = cabbit.get("box_ts", 0)
    box_avail = cabbit.get("box_available", True) or now >= box_ts

    buttons = []
    if box_avail:
        buttons.append([InlineKeyboardButton("📦 Открыть коробку", callback_data="cabbit:box")])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="cabbit:refresh")])
    return InlineKeyboardMarkup(buttons)


def apply_xp(cabbit: dict, xp: int) -> tuple[bool, int]:
    """Добавляет XP, обрабатывает левел-ап. Возвращает (leveled_up, new_level)."""
    cabbit["xp"] += xp
    leveled_up = False
    while cabbit["xp"] >= xp_for_level(cabbit["level"]):
        cabbit["xp"] -= xp_for_level(cabbit["level"])
        cabbit["level"] += 1
        leveled_up = True
    return leveled_up, cabbit["level"]


# ─── Хендлеры ─────────────────────────────────────────────────────────────────

CABBIT_PHOTO = "cabbit.jpg"   # файл с фото кеббита


async def cmd_cabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)

    if not cabbit:
        await update.message.reply_text(
            "🐰 У тебя ещё нет кеббита!\n\nКак ты хочешь его назвать?"
        )
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
        f"Твой кеббит готов к приключениям. "
        f"Каждые 30 минут появляется коробка с едой — не забывай кормить!",
        parse_mode="HTML",
    )
    await _send_cabbit_card(update.message, uid, cabbit)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def _send_cabbit_card(msg, uid: str, cabbit: dict):
    """Отправляет карточку кеббита с фото."""
    import os
    status = cabbit_status(cabbit)
    kb     = cabbit_keyboard(cabbit)

    if os.path.exists(CABBIT_PHOTO):
        try:
            with open(CABBIT_PHOTO, "rb") as f:
                await msg.reply_photo(
                    photo=f,
                    caption=status,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            return
        except Exception:
            pass
    # fallback без фото
    await msg.reply_text(status, parse_mode="HTML", reply_markup=kb)


async def callback_cabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    uid    = str(q.from_user.id)
    action = q.data.split(":")[1]
    cabbit = cabbit_db.get(uid)

    if not cabbit:
        await q.edit_message_caption("❌ Сначала создай кеббита через /cabbit")
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

        # Открываем коробку
        food_name, food_emoji, food_xp = roll_food()
        leveled_up, new_level = apply_xp(cabbit, food_xp)

        # Обновляем счётчики
        counts = cabbit.setdefault("food_counts", {"Морковь": 0, "Корм": 0, "Вкусность": 0})
        counts[food_name] = counts.get(food_name, 0) + 1

        # Следующая коробка через 30 минут
        cabbit["box_available"] = False
        cabbit["box_ts"]        = now + BOX_INTERVAL

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


# ─── Фоновый таймер: уведомления о коробке ───────────────────────────────────

async def box_notifier(app) -> None:
    """Каждые 60 секунд проверяем кому пора получить коробку и шлём уведомление."""
    logger.info("Cabbit box notifier started.")
    while True:
        await asyncio.sleep(60)
        try:
            now  = int(time.time())
            all_ = cabbit_db.get_all()
            for uid, cabbit in all_.items():
                box_ts    = cabbit.get("box_ts", 0)
                box_avail = cabbit.get("box_available", True)

                # Коробка стала доступна (время вышло, но ещё не уведомляли)
                if not box_avail and now >= box_ts:
                    cabbit["box_available"] = True
                    cabbit_db.save_cabbit(uid, cabbit)
                    name = cabbit.get("name", "Кеббит")
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=(
                                f"📦 <b>Новая коробка с едой!</b>\n\n"
                                f"🐰 {name} голодает — не забудь покормить!\n"
                                f"Напиши /cabbit чтобы открыть."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"box notify uid={uid}: {e}")
        except Exception as e:
            logger.error(f"box_notifier error: {e}")
