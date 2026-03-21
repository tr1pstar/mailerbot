"""
achievements.py — система достижений кеббитов
"""
from telegram import Update
from telegram.ext import ContextTypes

ACHIEVEMENTS = [
    {"id": "first_box",  "name": "Первая коробка",  "emoji": "📦", "desc": "Открой первую коробку",       "stat": "boxes_opened",    "need": 1,     "reward": 50},
    {"id": "boxes_10",   "name": "Коллекционер",    "emoji": "📦", "desc": "Открой 10 коробок",            "stat": "boxes_opened",    "need": 10,    "reward": 100},
    {"id": "boxes_50",   "name": "Охотник за едой", "emoji": "🍽",  "desc": "Открой 50 коробок",            "stat": "boxes_opened",    "need": 50,    "reward": 300},
    {"id": "boxes_100",  "name": "Обжора",          "emoji": "🎁", "desc": "Открой 100 коробок",           "stat": "boxes_opened",    "need": 100,   "reward": 500},
    {"id": "level_5",    "name": "Подросток",       "emoji": "🌱", "desc": "Достигни 5 уровня",            "stat": "max_level",       "need": 5,     "reward": 200},
    {"id": "level_15",   "name": "Воин",            "emoji": "⚔️",  "desc": "Достигни 15 уровня",           "stat": "max_level",       "need": 15,    "reward": 500},
    {"id": "level_30",   "name": "Легенда",         "emoji": "👑", "desc": "Достигни 30 уровня",           "stat": "max_level",       "need": 30,    "reward": 1000},
    {"id": "duel_win",   "name": "Дуэлянт",        "emoji": "🥊", "desc": "Выиграй первую дуэль",         "stat": "duels_won",       "need": 1,     "reward": 100},
    {"id": "duel_10",    "name": "Чемпион",         "emoji": "🏆", "desc": "Выиграй 10 дуэлей",            "stat": "duels_won",       "need": 10,    "reward": 500},
    {"id": "kill_1",     "name": "Убийца",          "emoji": "🔪", "desc": "Убей кеббита",                 "stat": "kills",           "need": 1,     "reward": 200},
    {"id": "raid_1",     "name": "Налётчик",        "emoji": "💰", "desc": "Успешный рейд",                "stat": "raids_ok",        "need": 1,     "reward": 100},
    {"id": "raid_10",    "name": "Грабитель",       "emoji": "🦹", "desc": "10 успешных рейдов",            "stat": "raids_ok",        "need": 10,    "reward": 400},
    {"id": "casino_win", "name": "Везунчик",        "emoji": "🎰", "desc": "Выиграй в казино",             "stat": "casino_wins",     "need": 1,     "reward": 100},
    {"id": "casino_10",  "name": "Картёжник",       "emoji": "🃏", "desc": "Выиграй 10 раз в казино",       "stat": "casino_wins",     "need": 10,    "reward": 300},
    {"id": "xp_1000",    "name": "Тысячник",        "emoji": "💫", "desc": "Заработай 1000 XP суммарно",    "stat": "xp_earned_total", "need": 1000,  "reward": 200},
    {"id": "xp_10000",   "name": "Магнат",          "emoji": "💎", "desc": "Заработай 10000 XP суммарно",   "stat": "xp_earned_total", "need": 10000, "reward": 1000},
]


def check_achievements(cabbit: dict) -> list[dict]:
    stats = cabbit.setdefault("stats", {})
    stats["max_level"] = max(stats.get("max_level", 0), cabbit.get("level", 1))
    earned = set(cabbit.get("achievements", []))
    new = []
    for ach in ACHIEVEMENTS:
        if ach["id"] not in earned and stats.get(ach["stat"], 0) >= ach["need"]:
            new.append(ach)
    return new


def unlock_achievements(cabbit: dict, new_achs: list[dict]) -> int:
    earned = cabbit.setdefault("achievements", [])
    total_xp = 0
    for ach in new_achs:
        earned.append(ach["id"])
        total_xp += ach["reward"]
    return total_xp


async def cmd_achievements(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db
    uid = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await update.message.reply_text("❌ Сначала создай кеббита через /cabbit")
        return

    earned = set(cabbit.get("achievements", []))
    lines = ["🏅 <b>Достижения:</b>\n"]
    count = len(earned)
    total = len(ACHIEVEMENTS)
    lines.append(f"Открыто: <b>{count}/{total}</b>\n")
    for ach in ACHIEVEMENTS:
        if ach["id"] in earned:
            lines.append(f"  ✅ {ach['emoji']} <b>{ach['name']}</b> — {ach['desc']}")
        else:
            stat_val = cabbit.get("stats", {}).get(ach["stat"], 0)
            lines.append(f"  ⬜ {ach['emoji']} {ach['name']} — {ach['desc']} ({stat_val}/{ach['need']})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
