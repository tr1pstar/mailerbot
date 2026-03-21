"""
quests.py — ежедневные квесты кеббитов
"""
import random
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

QUEST_POOL = [
    {"id": "open_boxes",  "desc": "Открой {n} коробок",       "targets": [2, 3, 5],  "rewards": [100, 200, 350]},
    {"id": "win_duel",    "desc": "Выиграй дуэль",            "targets": [1],         "rewards": [250]},
    {"id": "use_casino",  "desc": "Сыграй {n} раз в казино",  "targets": [2, 3, 5],   "rewards": [100, 150, 250]},
    {"id": "do_raid",     "desc": "Проведи {n} рейдов",       "targets": [1, 2],      "rewards": [150, 250]},
    {"id": "earn_xp",     "desc": "Заработай {n} XP",         "targets": [200, 500],  "rewards": [100, 200]},
    {"id": "feed_cabbit", "desc": "Покорми кеббита {n} раз",  "targets": [3, 5],      "rewards": [150, 300]},
]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _generate_quests() -> list[dict]:
    pool = random.sample(QUEST_POOL, min(3, len(QUEST_POOL)))
    quests = []
    for q in pool:
        idx = random.randrange(len(q["targets"]))
        quests.append({
            "id": q["id"],
            "desc": q["desc"].format(n=q["targets"][idx]),
            "target": q["targets"][idx],
            "progress": 0,
            "reward": q["rewards"][idx],
            "claimed": False,
        })
    return quests


def get_or_refresh_quests(cabbit: dict) -> list[dict]:
    qdata = cabbit.get("quests", {})
    if qdata.get("date") != _today():
        tasks = _generate_quests()
        cabbit["quests"] = {"date": _today(), "tasks": tasks}
    return cabbit["quests"]["tasks"]


def update_quest_progress(cabbit: dict, action: str, amount: int = 1):
    tasks = get_or_refresh_quests(cabbit)
    for t in tasks:
        if t["id"] == action and not t["claimed"]:
            t["progress"] = min(t["progress"] + amount, t["target"])


async def cmd_quests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db
    uid = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await update.message.reply_text("❌ Сначала создай кеббита через /cabbit")
        return

    tasks = get_or_refresh_quests(cabbit)
    cabbit_db.save_cabbit(uid, cabbit)

    lines = ["📋 <b>Ежедневные квесты:</b>\n"]
    buttons = []
    for i, t in enumerate(tasks):
        if t["claimed"]:
            status = "✅"
        elif t["progress"] >= t["target"]:
            status = "🎁"
        else:
            status = "⬜"
        lines.append(
            f"  {status} {t['desc']}\n"
            f"    [{t['progress']}/{t['target']}] — награда: +{t['reward']} XP"
        )
        if not t["claimed"] and t["progress"] >= t["target"]:
            buttons.append([InlineKeyboardButton(
                f"🎁 Забрать: {t['desc']}", callback_data=f"quest_claim:{i}"
            )])

    kb = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


async def callback_quest_claim(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db
    from achievements import check_achievements, unlock_achievements

    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    idx = int(q.data.split(":")[1])

    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await q.edit_message_text("❌ Кеббит не найден.")
        return

    tasks = get_or_refresh_quests(cabbit)
    if idx >= len(tasks):
        await q.answer("❌ Квест не найден.", show_alert=True)
        return

    task = tasks[idx]
    if task["claimed"]:
        await q.answer("Уже забрано!", show_alert=True)
        return
    if task["progress"] < task["target"]:
        await q.answer("Квест не выполнен!", show_alert=True)
        return

    task["claimed"] = True
    reward = task["reward"]
    cabbit["xp"] = cabbit.get("xp", 0) + reward
    stats = cabbit.setdefault("stats", {})
    stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + reward

    new_achs = check_achievements(cabbit)
    ach_text = ""
    if new_achs:
        bonus = unlock_achievements(cabbit, new_achs)
        cabbit["xp"] += bonus
        for a in new_achs:
            ach_text += f"\n🏅 <b>{a['emoji']} {a['name']}</b> (+{a['reward']} XP)"

    cabbit_db.save_cabbit(uid, cabbit)
    text = f"✅ Квест выполнен!\n\n+{reward} XP{ach_text}\n\n💰 Баланс: <b>{cabbit.get('xp', 0)} XP</b>"
    await q.edit_message_text(text, parse_mode="HTML")
