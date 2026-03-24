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

logger = logging.getLogger(__name__)

# ─── Константы ────────────────────────────────────────────────────────────────

BOX_INTERVAL = 30 * 60
WARN_12H     = 12 * 3600
WARN_23H     = 23 * 3600
DEATH_24H    = 24 * 3600
NAMING_STATE = 1
KNIFE_CHANCE = 0.5

FOOD_TABLE = [
    ("Морковь",   "🥕", 60,  80),
    ("Корм",      "🍗", 20,  200),
    ("Вкусность", "✨", 20,  500),
]

# Сколько часов здоровья восстанавливает каждая еда
FOOD_HEAL = {
    "Морковь":   3 * 3600,
    "Корм":      6 * 3600,
    "Вкусность": 12 * 3600,
}

# Эволюции
EVOLUTIONS = [
    {"name": "Малыш",      "emoji": "🐣", "min_level": 1,  "xp_mult": 1.0, "box_cd": 30 * 60},
    {"name": "Подросток",   "emoji": "🐰", "min_level": 5,  "xp_mult": 1.2, "box_cd": 30 * 60},
    {"name": "Воин",        "emoji": "⚔️",  "min_level": 15, "xp_mult": 1.5, "box_cd": 30 * 60},
    {"name": "Легенда",     "emoji": "👑", "min_level": 30, "xp_mult": 2.0, "box_cd": 30 * 60},
]

# Рандомные события при открытии коробки
RANDOM_EVENTS = [
    {"text": "🎁 Кеббит нашёл клад!",            "xp": 300,  "chance": 5},
    {"text": "💫 Кеббит поскользнулся!",          "xp": -50,  "chance": 10},
    {"text": "🐦 Подружился с птичкой!",           "tokens": 1, "chance": 8},
    {"text": "🌀 Портал! Телепорт +1 уровень!",   "level_up": True, "chance": 2},
    {"text": "💎 Нашёл алмаз!",                   "xp": 500,  "chance": 3},
    {"text": "🌧 Дождь, кеббит грустит",          "xp": -30,  "chance": 8},
    {"text": "🍀 Четырёхлистный клевер!",          "xp": 150,  "chance": 7},
]

# Предметы из коробок
ITEM_TABLE = [
    ("Щит",      "🛡", 0.5),
    ("Зелье",    "🧪", 2),
    ("Магнит",   "🧲", 1.5),
    ("Корона",   "👑", 1.5),
    ("Таблетка", "💊", 3),
]

SICKNESS_CHANCE   = 5
SICKNESS_DURATION = 6 * 3600
RAID_COOLDOWN     = 2 * 3600

RULES_TEXT = (
    "📜 <b>Правила игры «Кеббит»</b>\n\n"
    "1. 🚫 <b>Запрещены мультиаккаунты.</b>\n"
    "   Один человек — один кеббит. Использование нескольких аккаунтов "
    "для получения преимуществ приведёт к бану кеббита.\n\n"
    "Нажми <b>✅ Принимаю</b> чтобы продолжить."
)


def xp_for_level(level: int) -> int:
    return int(100 * (level ** 1.6))


def get_evolution(level: int) -> dict:
    result = EVOLUTIONS[0]
    for evo in EVOLUTIONS:
        if level >= evo["min_level"]:
            result = evo
    return result


def get_box_interval(cabbit: dict) -> int:
    return get_evolution(cabbit.get("level", 1))["box_cd"]


def roll_item() -> tuple[str, str] | None:
    r = random.random() * 100
    cum = 0
    for name, emoji, chance in ITEM_TABLE:
        cum += chance
        if r < cum:
            return name, emoji
    return None


def roll_event() -> dict | None:
    for ev in RANDOM_EVENTS:
        if random.random() * 100 < ev["chance"]:
            return ev
    return None


def check_sickness(cabbit: dict) -> bool:
    """Auto-cure if time passed. Returns True if still sick."""
    if not cabbit.get("sick"):
        return False
    if int(time.time()) >= cabbit.get("sick_until", 0):
        cabbit["sick"] = False
        cabbit["sick_until"] = 0
        return False
    return True


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
            "duel_tokens": 0,
            "inventory": {},
            "sick": False,
            "sick_until": 0,
            "crown_boxes": 0,
            "last_raid": 0,
            "achievements": [],
            "stats": {},
            "quests": {},
            "prestige_stars": 0,
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
        for uid, c in self._load().items():
            if c.get("has_knife") and not c.get("dead"):
                return uid
        return None


cabbit_db = CabbitStorage()


# ─── Игровая логика ───────────────────────────────────────────────────────────

def roll_box(uid: str) -> tuple[str, str, int, bool]:
    """Returns (food_name, emoji, xp, got_knife)."""
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

    if check_sickness(cabbit):
        left = max(0, cabbit.get("sick_until", 0) - now)
        mood += f"\n🤒 Болен! (осталось {left // 3600}ч {(left % 3600) // 60}м)"

    return f"{bar} {pct}%\n{mood}"


def cabbit_status(cabbit: dict) -> str:
    name    = cabbit["name"]
    level   = cabbit["level"]
    xp      = cabbit["xp"]
    needed  = xp_for_level(level)
    pct     = min(int(xp / needed * 100), 100)
    bar     = "█" * (pct // 10) + "░" * (10 - pct // 10)

    evo = get_evolution(level)

    now       = int(time.time())
    box_ts    = cabbit.get("box_ts", 0)
    box_avail = cabbit.get("box_available", True) or now >= box_ts

    if box_avail:
        box_str = "📦 Коробка готова!"
    else:
        left = max(0, box_ts - now)
        box_str = f"⏳ Коробка через {left // 60}м {left % 60}с"

    counts   = cabbit.get("food_counts", {})
    food_str = " | ".join(f"{e} {counts.get(n, 0)}" for n, e, _, _ in FOOD_TABLE)

    inv = cabbit.get("inventory", {})
    inv_items = [f"{e}{inv[n]}" for n, e, _ in ITEM_TABLE if inv.get(n, 0) > 0]
    inv_str = " | ".join(inv_items) if inv_items else "пусто"

    knife_str  = "\n🔪 <b>Есть нож!</b> /knife" if cabbit.get("has_knife") else ""
    tokens     = cabbit.get("duel_tokens", 0)
    token_str  = f"\n🥊 Жетонов: <b>{tokens}</b>" if tokens > 0 else ""
    crown_str  = f"\n👑 Корона: x2 XP ещё <b>{cabbit.get('crown_boxes', 0)}</b> коробок" if cabbit.get("crown_boxes", 0) > 0 else ""

    stars = cabbit.get("prestige_stars", 0)
    stars_str = f" {'⭐' * stars}" if stars > 0 else ""

    return (
        f"{evo['emoji']} <b>{name}</b> [{evo['name']}]{stars_str}\n"
        f"Уровень: <b>{level}</b>\n"
        f"📊 XP: <b>{xp}</b> / <b>{needed}</b>\n"
        f"[{bar}] {pct}%\n\n"
        f"❤️ Здоровье:\n{hunger_bar(cabbit)}\n\n"
        f"🍽 Еда: {food_str}\n"
        f"🎒 Инвентарь: {inv_str}"
        f"{knife_str}{token_str}{crown_str}\n\n"
        f"{box_str}"
    )


def cabbit_keyboard(cabbit: dict) -> InlineKeyboardMarkup:
    now       = int(time.time())
    box_ts    = cabbit.get("box_ts", 0)
    box_avail = cabbit.get("box_available", True) or now >= box_ts
    buttons   = []

    if box_avail:
        buttons.append([InlineKeyboardButton("📦 Открыть коробку", callback_data="cabbit:box")])

    inv = cabbit.get("inventory", {})
    item_btns = []
    if inv.get("Зелье", 0) > 0:
        item_btns.append(InlineKeyboardButton(f"🧪{inv['Зелье']}", callback_data="use_item:Зелье"))
    if inv.get("Таблетка", 0) > 0 and cabbit.get("sick"):
        item_btns.append(InlineKeyboardButton(f"💊{inv['Таблетка']}", callback_data="use_item:Таблетка"))
    if inv.get("Магнит", 0) > 0:
        item_btns.append(InlineKeyboardButton(f"🧲{inv['Магнит']}", callback_data="use_item:Магнит"))
    if item_btns:
        buttons.append(item_btns)

    if cabbit.get("has_knife"):
        buttons.append([InlineKeyboardButton("🔪 Нож", callback_data="cabbit:knife")])
    if cabbit.get("duel_tokens", 0) > 0:
        buttons.append([InlineKeyboardButton("🥊 Дуэль", callback_data="cabbit:duel")])

    last_raid = cabbit.get("last_raid", 0)
    if now >= last_raid + RAID_COOLDOWN:
        buttons.append([InlineKeyboardButton("🏴‍☠️ Рейд", callback_data="cabbit:raid")])

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="cabbit:refresh")])
    return InlineKeyboardMarkup(buttons)


def apply_xp(cabbit: dict, xp: int) -> tuple[bool, int]:
    cabbit["xp"] += xp
    leveled_up = False
    while cabbit["xp"] >= xp_for_level(cabbit["level"]):
        cabbit["xp"] -= xp_for_level(cabbit["level"])
        cabbit["level"] += 1
        leveled_up = True
    stats = cabbit.setdefault("stats", {})
    stats["max_level"] = max(stats.get("max_level", 0), cabbit["level"])
    return leveled_up, cabbit["level"]


# ─── Хендлеры ─────────────────────────────────────────────────────────────────

CABBIT_PHOTO = "/app/cabbit.jpg"


async def cmd_cabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)

    if not cabbit:
        # Новый игрок — показываем правила
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Принимаю", callback_data="rules:accept")],
        ])
        await update.message.reply_text(RULES_TEXT, parse_mode="HTML", reply_markup=kb)
        return ConversationHandler.END

    if cabbit.get("dead"):
        name = cabbit.get("name", "Кеббит")
        await update.message.reply_text(
            f"💀 <b>{name} умер от голода...</b>\n\n"
            f"Ты не кормил его 24 часа. Кеббит ушёл в лучший мир.",
            parse_mode="HTML",
        )
        with cabbit_db._lock:
            data = cabbit_db._load()
            data.pop(uid, None)
            cabbit_db._save(data)
        # Заводит нового — правила заново
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Принимаю", callback_data="rules:accept")],
        ])
        await update.message.reply_text(RULES_TEXT, parse_mode="HTML", reply_markup=kb)
        return ConversationHandler.END

    # Существующий живой кеббит — проверяем принял ли правила
    if not cabbit.get("rules_accepted"):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Принимаю", callback_data="rules:accept")],
        ])
        await update.message.reply_text(RULES_TEXT, parse_mode="HTML", reply_markup=kb)
        return ConversationHandler.END

    check_sickness(cabbit)
    cabbit_db.save_cabbit(uid, cabbit)
    await _send_cabbit_card(update.message, cabbit)
    return ConversationHandler.END


async def callback_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка ✅ Принимаю — принять правила."""
    q   = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)

    cabbit = cabbit_db.get(uid)

    if cabbit and not cabbit.get("dead"):
        # Существующий игрок — просто отмечаем и показываем карточку
        cabbit["rules_accepted"] = True
        check_sickness(cabbit)
        cabbit_db.save_cabbit(uid, cabbit)
        await q.edit_message_text("✅ Правила приняты!")
        await _send_cabbit_card(q.message, cabbit)
        return

    # Новый или dead — нужно имя
    await q.edit_message_text(
        "✅ Правила приняты!\n\n"
        "🐰 Как ты хочешь назвать своего кеббита?"
    )
    # Запускаем ConversationHandler вручную не получится —
    # поэтому ставим флаг и ловим следующее текстовое сообщение через отдельный хендлер
    ctx.user_data["awaiting_cabbit_name"] = True


async def receive_name_from_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ловит имя после принятия правил через callback (вне ConversationHandler)."""
    if not ctx.user_data.get("awaiting_cabbit_name"):
        return  # не наш случай
    ctx.user_data.pop("awaiting_cabbit_name", None)

    uid  = str(update.effective_user.id)
    name = update.message.text.strip()[:20]
    if not name:
        await update.message.reply_text("Имя не может быть пустым. Напиши /cabbit чтобы начать заново.")
        return

    cabbit = cabbit_db.create(uid, name)
    cabbit["rules_accepted"] = True
    cabbit_db.save_cabbit(uid, cabbit)
    await update.message.reply_text(
        f"🎉 Познакомьтесь — <b>{name}</b>!\n\n"
        f"Каждые 30 минут появляется коробка с едой — не забывай кормить!\n"
        f"⚠️ Если не кормить 24 часа — кеббит умрёт.\n\n"
        f"Новые команды: /casino, /raid, /quests, /achievements",
        parse_mode="HTML",
    )
    await _send_cabbit_card(update.message, cabbit)


async def receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    name = update.message.text.strip()[:20]

    if not name:
        await update.message.reply_text("Имя не может быть пустым, попробуй ещё раз:")
        return NAMING_STATE

    cabbit = cabbit_db.create(uid, name)
    cabbit["rules_accepted"] = True
    cabbit_db.save_cabbit(uid, cabbit)
    await update.message.reply_text(
        f"🎉 Познакомьтесь — <b>{name}</b>!\n\n"
        f"Каждые 30 минут появляется коробка с едой — не забывай кормить!\n"
        f"⚠️ Если не кормить 24 часа — кеббит умрёт.\n\n"
        f"Новые команды: /casino, /raid, /quests, /achievements",
        parse_mode="HTML",
    )
    await _send_cabbit_card(update.message, cabbit)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def _send_cabbit_card(msg, cabbit: dict):
    check_sickness(cabbit)
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

    check_sickness(cabbit)

    if action == "refresh":
        cabbit_db.save_cabbit(uid, cabbit)
        await _edit_card(q, cabbit)
        return

    if action == "knife":
        if not cabbit.get("has_knife"):
            await q.answer("У тебя нет ножа!", show_alert=True)
            return
        await _show_knife_targets(q, uid)
        return

    if action == "raid":
        now = int(time.time())
        if now < cabbit.get("last_raid", 0) + RAID_COOLDOWN:
            left = cabbit.get("last_raid", 0) + RAID_COOLDOWN - now
            await q.answer(f"⏳ Рейд через {left // 60}м", show_alert=True)
            return
        await _do_raid(q, ctx, uid, cabbit)
        return

    if action == "duel":
        if cabbit.get("duel_tokens", 0) <= 0:
            await q.answer("У тебя нет жетонов дуэли!", show_alert=True)
            return
        all_   = cabbit_db.get_all()
        others = [(u, c) for u, c in all_.items() if u != uid and not c.get("dead")]
        if not others:
            await q.answer("Нет других живых кеббитов!", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(
                f"🐰 {c['name']} (ур. {c['level']}) — {c['xp']} XP",
                callback_data=f"duel_send:{u}",
            )]
            for u, c in others
        ]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="duel_send:cancel")])
        text = "🥊 <b>Выбери противника для дуэли</b>\n\nСтавка: <b>500 XP</b> | Best of 3"
        try:
            await q.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "box":
        from quests import update_quest_progress
        from achievements import check_achievements, unlock_achievements

        now    = int(time.time())
        box_ts = cabbit.get("box_ts", 0)
        if not (cabbit.get("box_available", True) or now >= box_ts):
            await q.answer("⏳ Коробка ещё не готова!", show_alert=True)
            return

        stats = cabbit.setdefault("stats", {})
        stats["boxes_opened"] = stats.get("boxes_opened", 0) + 1

        is_sick = check_sickness(cabbit)
        evo     = get_evolution(cabbit.get("level", 1))
        box_cd  = evo["box_cd"]

        food_name, food_emoji, food_xp, got_knife = roll_box(uid)
        item_drop = roll_item()
        event     = roll_event()
        actual_xp = 0

        text_parts = ["📦 <b>Коробка открыта!</b>\n"]

        if got_knife:
            cabbit["has_knife"] = True
            text_parts.append("\n🔪 <b>ВАУ! Выпал НОЖ!</b>\nМожешь убить чужого кеббита!\n")
            # Уведомляем всех остальных
            all_notify = cabbit_db.get_all()
            for other_uid, other_cab in all_notify.items():
                if other_uid == uid or other_cab.get("dead"):
                    continue
                try:
                    await ctx.application.bot.send_message(
                        chat_id=int(other_uid),
                        text=(
                            "🔪 <b>Кто-то нашёл нож!</b>\n\n"
                            "В одной из коробок был обнаружен нож.\n"
                            "Один из кеббитов теперь вооружён — будь осторожен! 👀"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(f"knife notify uid={other_uid}: {e}")
        else:
            xp_mult = evo["xp_mult"] + cabbit.get("prestige_stars", 0) * 0.1
            if cabbit.get("crown_boxes", 0) > 0:
                xp_mult *= 2
                cabbit["crown_boxes"] -= 1
                text_parts.append("\n👑 Корона активна! x2 XP")
            if is_sick:
                xp_mult *= 0.5
                text_parts.append("\n🤒 Болен — XP снижен вдвое")

            actual_xp = int(food_xp * xp_mult)
            leveled_up, new_level = apply_xp(cabbit, actual_xp)
            counts = cabbit.setdefault("food_counts", {"Морковь": 0, "Корм": 0, "Вкусность": 0})
            counts[food_name] = counts.get(food_name, 0) + 1
            stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + actual_xp

            text_parts.append(f"\n{food_emoji} <b>{food_name}</b> — +{actual_xp} XP")
            if xp_mult != 1.0:
                text_parts.append(f" (x{xp_mult:.1f})")

            if leveled_up:
                new_evo = get_evolution(new_level)
                text_parts.append(f"\n\n🎉 <b>УРОВЕНЬ {new_level}!</b>")
                if new_evo != evo:
                    text_parts.append(f"\n✨ <b>ЭВОЛЮЦИЯ: {new_evo['emoji']} {new_evo['name']}!</b>")

        # Item drop
        if item_drop:
            item_name, item_emoji = item_drop
            inv = cabbit.setdefault("inventory", {})
            if item_name == "Корона":
                cabbit["crown_boxes"] = cabbit.get("crown_boxes", 0) + 3
                text_parts.append(f"\n\n🎁 {item_emoji} <b>{item_name}</b> — x2 XP на 3 коробки!")
            else:
                inv[item_name] = inv.get(item_name, 0) + 1
                text_parts.append(f"\n\n🎁 Предмет: {item_emoji} <b>{item_name}</b>")

        # Random event
        if event:
            text_parts.append(f"\n\n⚡️ {event['text']}")
            if event.get("xp"):
                if event["xp"] > 0:
                    ev_leveled, _ = apply_xp(cabbit, event["xp"])
                    stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + event["xp"]
                else:
                    cabbit["xp"] = max(0, cabbit.get("xp", 0) + event["xp"])
                sign = "+" if event["xp"] > 0 else ""
                text_parts.append(f"\n  {sign}{event['xp']} XP")
            if event.get("tokens"):
                cabbit["duel_tokens"] = cabbit.get("duel_tokens", 0) + event["tokens"]
                text_parts.append(f"\n  +{event['tokens']} жетон дуэли")
            if event.get("level_up"):
                cabbit["level"] = cabbit.get("level", 1) + 1
                cabbit["xp"] = 0
                stats["max_level"] = max(stats.get("max_level", 0), cabbit["level"])
                text_parts.append(f"\n  Новый уровень: {cabbit['level']}!")

        # Sickness roll
        if not cabbit.get("sick") and random.randint(1, 100) <= SICKNESS_CHANCE:
            cabbit["sick"] = True
            cabbit["sick_until"] = now + SICKNESS_DURATION
            text_parts.append("\n\n🤒 <b>О нет! Кеббит заболел!</b> XP снижен. Найди 💊 или жди 6ч.")

        # Common updates
        cabbit["box_available"] = False
        cabbit["box_ts"]        = now + box_cd
        if not got_knife:
            heal = FOOD_HEAL.get(food_name, 3 * 3600)
            cabbit["last_fed"] = min(now, cabbit.get("last_fed", now) + heal)
        elapsed_after = now - cabbit.get("last_fed", now)
        if elapsed_after < WARN_12H:
            cabbit["warned_12h"] = False
        if elapsed_after < WARN_23H:
            cabbit["warned_23h"] = False
        cabbit["duel_tokens"]   = cabbit.get("duel_tokens", 0) + 1

        # Quest progress
        update_quest_progress(cabbit, "open_boxes")
        if not got_knife:
            update_quest_progress(cabbit, "feed_cabbit")
            update_quest_progress(cabbit, "earn_xp", actual_xp)

        # Achievements
        new_achs = check_achievements(cabbit)
        if new_achs:
            bonus = unlock_achievements(cabbit, new_achs)
            apply_xp(cabbit, bonus)
            text_parts.append(f"\n\n{'━' * 20}")
            text_parts.append("\n🏆 <b>ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>")
            for a in new_achs:
                text_parts.append(f"\n  {a['emoji']} <b>{a['name']}</b> — {a['desc']}\n  💰 +{a['reward']} XP")
            text_parts.append(f"\n{'━' * 20}")

        cabbit_db.save_cabbit(uid, cabbit)

        text_parts.append(f"\n\n🥊 +1 жетон дуэли\n\n{cabbit_status(cabbit)}")
        await _edit_card(q, cabbit, "".join(text_parts))


async def _edit_card(q, cabbit: dict, text: str = None):
    check_sickness(cabbit)
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

    # Shield check
    target_inv = target.get("inventory", {})
    if target_inv.get("Щит", 0) > 0:
        target_inv["Щит"] -= 1
        attacker["has_knife"] = False
        cabbit_db.save_cabbit(target_uid, target)
        cabbit_db.save_cabbit(attacker_uid, attacker)
        try:
            await ctx.application.bot.send_message(
                chat_id=int(target_uid),
                text=(
                    f"🛡 <b>Щит спас {target_name}!</b>\n\n"
                    f"🔪 {attacker_name} пытался убить, но щит заблокировал удар!\n"
                    f"Щит сломался, нож тоже."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        text = (
            f"🛡 <b>Удар заблокирован!</b>\n\n"
            f"{target_name} использовал щит. Нож сломался.\n\n"
            f"{cabbit_status(attacker)}"
        )
        await _edit_card(q, attacker, text)
        return

    # Kill
    target["dead"] = True
    cabbit_db.save_cabbit(target_uid, target)

    attacker["has_knife"] = False
    a_stats = attacker.setdefault("stats", {})
    a_stats["kills"] = a_stats.get("kills", 0) + 1
    cabbit_db.save_cabbit(attacker_uid, attacker)

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

    from achievements import check_achievements, unlock_achievements
    new_achs = check_achievements(attacker)
    ach_text = ""
    if new_achs:
        bonus = unlock_achievements(attacker, new_achs)
        apply_xp(attacker, bonus)
        cabbit_db.save_cabbit(attacker_uid, attacker)
        ach_text = f"\n\n{'━' * 20}\n🏆 <b>ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>"
        for a in new_achs:
            ach_text += f"\n  {a['emoji']} <b>{a['name']}</b> — {a['desc']}\n  💰 +{a['reward']} XP"
        ach_text += f"\n{'━' * 20}"

    # Уведомляем всех остальных
    all_ = cabbit_db.get_all()
    for other_uid, other_cab in all_.items():
        if other_uid in (attacker_uid, target_uid) or other_cab.get("dead"):
            continue
        try:
            await ctx.application.bot.send_message(
                chat_id=int(other_uid),
                text=(
                    f"💀 <b>Убийство!</b>\n\n"
                    f"🔪 <b>{attacker_name}</b> использовал нож и убил <b>{target_name}</b>!\n"
                    f"Нож сломался. Мир снова в безопасности... или нет? 👀"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"kill broadcast uid={other_uid}: {e}")

    text = (
        f"🔪 <b>{target_name} убит!</b>\n\n"
        f"Нож сломался.{ach_text}\n\n"
        f"{cabbit_status(attacker)}"
    )
    await _edit_card(q, attacker, text)


# ─── Рейд ─────────────────────────────────────────────────────────────────────

async def _do_raid(q, ctx, uid: str, cabbit: dict):
    from quests import update_quest_progress
    from achievements import check_achievements, unlock_achievements

    all_   = cabbit_db.get_all()
    others = [(u, c) for u, c in all_.items() if u != uid and not c.get("dead") and c.get("xp", 0) > 0]
    if not others:
        await q.answer("Нет целей для рейда!", show_alert=True)
        return

    target_uid, target_cab = random.choice(others)
    cabbit["last_raid"] = int(time.time())
    stats = cabbit.setdefault("stats", {})
    update_quest_progress(cabbit, "do_raid")

    if random.randint(1, 100) <= 40:
        stolen = max(1, int(target_cab.get("xp", 0) * 0.1))
        stolen = min(stolen, 500)
        target_cab["xp"] = max(0, target_cab.get("xp", 0) - stolen)
        leveled, _ = apply_xp(cabbit, stolen)
        stats["raids_ok"] = stats.get("raids_ok", 0) + 1
        stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + stolen
        cabbit_db.save_cabbit(target_uid, target_cab)

        try:
            await ctx.application.bot.send_message(
                chat_id=int(target_uid),
                text=(
                    f"🏴‍☠️ <b>Рейд!</b>\n\n"
                    f"{cabbit['name']} украл <b>{stolen} XP</b> у {target_cab['name']}!"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        text = (
            f"🏴‍☠️ <b>Рейд успешен!</b>\n\n"
            f"Украл <b>{stolen} XP</b> у {target_cab['name']}!\n"
        )
    else:
        lost = max(1, int(cabbit.get("xp", 0) * 0.05))
        cabbit["xp"] = max(0, cabbit.get("xp", 0) - lost)
        stats["raids_fail"] = stats.get("raids_fail", 0) + 1
        text = (
            f"🏴‍☠️ <b>Рейд провалился!</b>\n\n"
            f"Попался при попытке ограбить {target_cab['name']}.\n"
            f"💸 -{lost} XP\n"
        )

    new_achs = check_achievements(cabbit)
    if new_achs:
        bonus = unlock_achievements(cabbit, new_achs)
        apply_xp(cabbit, bonus)
        text += f"\n\n{'━' * 20}\n🏆 <b>ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>"
        for a in new_achs:
            text += f"\n  {a['emoji']} <b>{a['name']}</b> — {a['desc']}\n  💰 +{a['reward']} XP"
        text += f"\n{'━' * 20}"

    cabbit_db.save_cabbit(uid, cabbit)
    text += f"\n\n{cabbit_status(cabbit)}"
    await _edit_card(q, cabbit, text)


async def cmd_raid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await update.message.reply_text("❌ Сначала создай кеббита через /cabbit")
        return

    now = int(time.time())
    if now < cabbit.get("last_raid", 0) + RAID_COOLDOWN:
        left = cabbit.get("last_raid", 0) + RAID_COOLDOWN - now
        await update.message.reply_text(f"⏳ Рейд доступен через {left // 60}м {left % 60}с")
        return

    await update.message.reply_text(
        "🏴‍☠️ <b>Рейд</b>\n\n"
        "40% — украсть 10% XP случайного игрока (макс 500)\n"
        "60% — потеряешь 5% своего XP\n"
        "Кулдаун: 2 часа\n\n"
        "Жми кнопку в /cabbit чтобы начать!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏴‍☠️ Начать рейд", callback_data="cabbit:raid")
        ]]),
    )


# ─── Использование предметов ──────────────────────────────────────────────────

async def callback_use_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    uid  = str(q.from_user.id)
    item = q.data.split(":")[1]

    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await q.answer("❌ Кеббит не найден.", show_alert=True)
        return

    inv = cabbit.get("inventory", {})
    if inv.get(item, 0) <= 0:
        await q.answer("У тебя нет этого предмета!", show_alert=True)
        return

    inv[item] -= 1
    text = ""

    if item == "Зелье":
        cabbit["last_fed"] = int(time.time())
        cabbit["warned_12h"] = False
        cabbit["warned_23h"] = False
        text = f"🧪 <b>Зелье использовано!</b>\n\nГолод сброшен!\n\n{cabbit_status(cabbit)}"

    elif item == "Таблетка":
        cabbit["sick"] = False
        cabbit["sick_until"] = 0
        text = f"💊 <b>Таблетка!</b>\n\nКеббит здоров!\n\n{cabbit_status(cabbit)}"

    elif item == "Магнит":
        all_   = cabbit_db.get_all()
        others = [(u, c) for u, c in all_.items() if u != uid and not c.get("dead") and c.get("xp", 0) > 0]
        if not others:
            inv[item] += 1
            await q.answer("Нет целей!", show_alert=True)
            cabbit_db.save_cabbit(uid, cabbit)
            return

        t_uid, t_cab = random.choice(others)
        stolen = random.randint(100, 300)
        stolen = min(stolen, t_cab.get("xp", 0))
        t_cab["xp"] = max(0, t_cab.get("xp", 0) - stolen)
        apply_xp(cabbit, stolen)
        cabbit_db.save_cabbit(t_uid, t_cab)

        try:
            await ctx.application.bot.send_message(
                chat_id=int(t_uid),
                text=f"🧲 <b>Магнит!</b> {cabbit['name']} украл <b>{stolen} XP</b>!",
                parse_mode="HTML",
            )
        except Exception:
            pass

        text = (
            f"🧲 <b>Магнит!</b>\n\n"
            f"Украл <b>{stolen} XP</b> у {t_cab['name']}!\n\n"
            f"{cabbit_status(cabbit)}"
        )

    cabbit_db.save_cabbit(uid, cabbit)
    await _edit_card(q, cabbit, text)


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


async def cmd_prestige(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await update.message.reply_text("❌ Сначала создай кеббита через /cabbit")
        return

    if cabbit.get("level", 1) < 30:
        await update.message.reply_text(
            f"❌ Нужен 30 уровень для престижа. Сейчас: {cabbit.get('level', 1)}"
        )
        return

    stars = cabbit.get("prestige_stars", 0) + 1
    cabbit["prestige_stars"] = stars
    cabbit["level"] = 1
    cabbit["xp"] = 0
    cabbit["food_counts"] = {"Морковь": 0, "Корм": 0, "Вкусность": 0}
    cabbit["last_fed"] = int(time.time())
    cabbit["warned_12h"] = False
    cabbit["warned_23h"] = False
    cabbit["sick"] = False
    cabbit["sick_until"] = 0
    cabbit["crown_boxes"] = 0
    cabbit_db.save_cabbit(uid, cabbit)

    await update.message.reply_text(
        f"{'━' * 20}\n"
        f"🌟 <b>ПРЕСТИЖ {stars}!</b>\n"
        f"{'━' * 20}\n\n"
        f"Уровень сброшен до 1\n"
        f"{'⭐' * stars} Постоянный бонус: <b>+{stars * 10}%</b> XP\n\n"
        f"Инвентарь, достижения и статистика сохранены.\n\n"
        f"{cabbit_status(cabbit)}",
        parse_mode="HTML",
    )


# ─── Админ: бан кеббита ──────────────────────────────────────────────────────

async def cmd_bancabbit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /bancabbit <user_id> <причина>
    Админ убивает (банит) чужого кеббита с указанием причины.
    """
    from config import ADMIN_ID

    caller = update.effective_user.id
    if caller != ADMIN_ID:
        await update.message.reply_text("❌ Только администратор может банить кеббитов.")
        return

    args = (update.message.text or "").split(maxsplit=2)
    # /bancabbit <uid> <reason>
    if len(args) < 3:
        # Показать список живых кеббитов
        all_ = cabbit_db.get_all()
        alive = [(uid, c) for uid, c in all_.items() if not c.get("dead") and not c.get("banned")]
        if not alive:
            await update.message.reply_text("Нет живых кеббитов для бана.")
            return
        lines = ["🔨 <b>Живые кеббиты:</b>\n"]
        for uid, c in alive:
            evo = get_evolution(c["level"])
            lines.append(
                f"  {evo['emoji']} <b>{c['name']}</b> — ур. {c['level']} "
                f"(владелец: <code>{uid}</code>)"
            )
        lines.append(f"\n<i>Использование: /bancabbit &lt;user_id&gt; &lt;причина&gt;</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    target_uid = args[1].strip()
    reason = args[2].strip()

    target_cab = cabbit_db.get(target_uid)
    if not target_cab:
        await update.message.reply_text(f"❌ Кеббит с uid <code>{target_uid}</code> не найден.", parse_mode="HTML")
        return
    if target_cab.get("dead"):
        await update.message.reply_text("❌ Этот кеббит уже мёртв.")
        return
    if target_cab.get("banned"):
        await update.message.reply_text("❌ Этот кеббит уже забанен.")
        return

    target_name = target_cab.get("name", "Кеббит")

    # Баним: помечаем как dead + banned + причина
    target_cab["dead"] = True
    target_cab["banned"] = True
    target_cab["ban_reason"] = reason
    target_cab["banned_by"] = caller
    target_cab["banned_at"] = int(time.time())
    cabbit_db.save_cabbit(target_uid, target_cab)

    # Уведомляем владельца
    try:
        await ctx.application.bot.send_message(
            chat_id=int(target_uid),
            text=(
                f"🔨 <b>{target_name} был забанен администратором!</b>\n\n"
                f"Причина: <i>{reason}</i>\n\n"
                f"Напиши /cabbit чтобы завести нового."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"bancabbit notify uid={target_uid}: {e}")

    await update.message.reply_text(
        f"🔨 <b>Кеббит «{target_name}» (владелец {target_uid}) забанен.</b>\n"
        f"Причина: <i>{reason}</i>",
        parse_mode="HTML",
    )


async def cmd_cabbitlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /cabbitlist — только для админа.
    Показывает всех кеббитов с uid владельцев, уровнем, статусом.
    """
    from config import ADMIN_ID

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Только администратор может использовать эту команду.")
        return

    all_ = cabbit_db.get_all()
    if not all_:
        await update.message.reply_text("Кеббитов пока нет.")
        return

    lines = ["📋 <b>Все кеббиты (админ):</b>\n"]
    for uid, c in all_.items():
        evo = get_evolution(c.get("level", 1))
        status_parts = []
        if c.get("dead") and c.get("banned"):
            status_parts.append(f"🔨 забанен: {c.get('ban_reason', '—')}")
        elif c.get("dead"):
            status_parts.append("💀 мёртв")
        else:
            status_parts.append("✅ жив")
        if c.get("has_knife"):
            status_parts.append("🔪")
        if c.get("sick"):
            status_parts.append("🤒")

        stars = c.get("prestige_stars", 0)
        stars_str = f" {'⭐' * stars}" if stars > 0 else ""
        status = " | ".join(status_parts)

        lines.append(
            f"{evo['emoji']} <b>{c.get('name', '?')}</b>{stars_str} — "
            f"ур. {c.get('level', 1)} ({c.get('xp', 0)} XP)\n"
            f"   👤 ID: <code>{uid}</code> | {status}"
        )

    # Telegram лимит 4096 символов — разбиваем если надо
    text = "\n".join(lines)
    if len(text) <= 4000:
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        # Отправляем частями
        chunk_lines = [lines[0]]
        for line in lines[1:]:
            test = "\n".join(chunk_lines + [line])
            if len(test) > 4000:
                await update.message.reply_text("\n".join(chunk_lines), parse_mode="HTML")
                chunk_lines = [line]
            else:
                chunk_lines.append(line)
        if chunk_lines:
            await update.message.reply_text("\n".join(chunk_lines), parse_mode="HTML")


# ─── Админ: рассылка ─────────────────────────────────────────────────────────

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /broadcast <текст>
    Отправляет сообщение всем пользователям бота (у кого есть кеббит).
    Только для админа.
    """
    from config import ADMIN_ID

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Только администратор может делать рассылку.")
        return

    text = (update.message.text or "").split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text(
            "Использование: /broadcast <текст сообщения>\n\n"
            "Сообщение получат все пользователи у которых есть (или был) кеббит."
        )
        return

    message_text = text[1].strip()
    all_ = cabbit_db.get_all()
    sent = 0
    failed = 0

    for uid in all_:
        try:
            await ctx.application.bot.send_message(
                chat_id=int(uid),
                text=f"📢 <b>Объявление:</b>\n\n{message_text}",
                parse_mode="HTML",
            )
            sent += 1
        except Exception as e:
            logger.warning(f"broadcast to {uid} failed: {e}")
            failed += 1

    await update.message.reply_text(
        f"📢 Рассылка завершена.\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    all_  = cabbit_db.get_all()
    alive = [(uid, c) for uid, c in all_.items() if not c.get("dead")]
    if not alive:
        await update.message.reply_text("🏆 Пока нет живых кеббитов.")
        return

    alive.sort(key=lambda x: (x[1].get("prestige_stars", 0), x[1]["level"], x[1]["xp"]), reverse=True)
    lines = ["🏆 <b>Лидерборд кеббитов:</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, c) in enumerate(alive[:10], 1):
        medal = medals[i-1] if i <= 3 else f"{i}."
        knife = " 🔪" if c.get("has_knife") else ""
        evo   = get_evolution(c["level"])
        achs  = len(c.get("achievements", []))
        stars = c.get("prestige_stars", 0)
        stars_str = f" {'⭐' * stars}" if stars > 0 else ""
        lines.append(
            f"{medal} {evo['emoji']} <b>{c['name']}</b>{stars_str} — ур. {c['level']} "
            f"({c['xp']} XP){knife} 🏅{achs}"
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
                                f"Осталось <b>{mins_left} минут</b>!\n\n"
                                f"Скорее /cabbit!"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"warn_23h uid={uid}: {e}")
                    continue

                if elapsed >= WARN_12H and not cabbit.get("warned_12h"):
                    cabbit["warned_12h"] = True
                    cabbit_db.save_cabbit(uid, cabbit)
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=(
                                f"⚠️ <b>{name} голодает!</b>\n\n"
                                f"Кеббит не ел уже 12 часов.\n"
                                f"Покорми или он умрёт через 12 часов!\n\n"
                                f"/cabbit → 📦 Открыть коробку"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"warn_12h uid={uid}: {e}")

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
                            disable_notification=True,
                        )
                    except Exception as e:
                        logger.warning(f"box notify uid={uid}: {e}")

        except Exception as e:
            logger.error(f"box_notifier error: {e}")
