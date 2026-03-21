"""
casino.py — слот-машина для кеббитов
"""
import random

from telegram import Update
from telegram.ext import ContextTypes

REELS = ["🍒", "🍋", "🔔", "💎", "7️⃣", "🍀"]


def spin_slots() -> tuple[list[str], float]:
    result = [random.choice(REELS) for _ in range(3)]
    if result[0] == result[1] == result[2]:
        if result[0] == "💎":
            return result, 15.0
        if result[0] == "7️⃣":
            return result, 10.0
        return result, 5.0
    if result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
        return result, 2.0
    return result, 0.0


async def cmd_casino(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db
    from quests import update_quest_progress
    from achievements import check_achievements, unlock_achievements

    uid = str(update.effective_user.id)
    cabbit = cabbit_db.get(uid)

    if not cabbit or cabbit.get("dead"):
        await update.message.reply_text("❌ Сначала создай кеббита через /cabbit")
        return

    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "🎰 <b>Казино</b>\n\n"
            "Использование: <code>/casino СТАВКА</code>\n"
            "Пример: <code>/casino 100</code>\n\n"
            "💎💎💎 = x15 | 7️⃣7️⃣7️⃣ = x10\n"
            "Три одинаковых = x5 | Два = x2\n"
            "Ничего = проигрыш",
            parse_mode="HTML",
        )
        return

    bet = int(ctx.args[0])
    if bet < 1:
        await update.message.reply_text("❌ Минимальная ставка: 1 XP")
        return
    if bet > cabbit.get("xp", 0):
        await update.message.reply_text(f"❌ Недостаточно XP! У тебя: {cabbit.get('xp', 0)}")
        return
    if bet > 5000:
        await update.message.reply_text("❌ Максимальная ставка: 5000 XP")
        return

    symbols, mult = spin_slots()
    display = " | ".join(symbols)
    stats = cabbit.setdefault("stats", {})

    if mult > 0:
        winnings = int(bet * mult)
        net = winnings - bet
        cabbit["xp"] = cabbit.get("xp", 0) + net
        stats["casino_wins"] = stats.get("casino_wins", 0) + 1
        stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + net
        text = (
            f"🎰 <b>КАЗИНО</b>\n\n"
            f"[ {display} ]\n\n"
            f"🎉 <b>ВЫИГРЫШ x{mult:.0f}!</b>\n"
            f"💰 +{net} XP\n"
        )
    else:
        cabbit["xp"] = max(0, cabbit.get("xp", 0) - bet)
        stats["casino_losses"] = stats.get("casino_losses", 0) + 1
        text = (
            f"🎰 <b>КАЗИНО</b>\n\n"
            f"[ {display} ]\n\n"
            f"💀 <b>Проигрыш!</b>\n"
            f"💸 -{bet} XP\n"
        )

    update_quest_progress(cabbit, "use_casino")

    new_achs = check_achievements(cabbit)
    if new_achs:
        bonus = unlock_achievements(cabbit, new_achs)
        cabbit["xp"] = cabbit.get("xp", 0) + bonus
        text += f"\n\n{'━' * 20}\n🏆 <b>ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>"
        for a in new_achs:
            text += f"\n  {a['emoji']} <b>{a['name']}</b> — {a['desc']}\n  💰 +{a['reward']} XP"
        text += f"\n{'━' * 20}"

    cabbit_db.save_cabbit(uid, cabbit)
    text += f"\n\n💰 Баланс: <b>{cabbit.get('xp', 0)} XP</b>"
    await update.message.reply_text(text, parse_mode="HTML")
