"""
reaction.py — мини-игра на реакцию
Раз в 1-3 часа бот кидает кнопку, первый нажавший получает XP.
"""
import asyncio
import logging
import random
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_event = {
    "active": False,
    "ts": 0,
    "winner_uid": None,
    "participants": set(),
    "reward": 0,
}

MIN_INTERVAL = 3600
MAX_INTERVAL = 3 * 3600
TIMEOUT      = 300
BIG_REWARD   = (200, 500)
SMALL_REWARD = 50


async def reaction_notifier(app) -> None:
    logger.info("Reaction notifier started.")
    await asyncio.sleep(300)
    while True:
        wait = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        await asyncio.sleep(wait)
        try:
            from cabbit import cabbit_db
            all_ = cabbit_db.get_all()
            alive = [uid for uid, c in all_.items() if not c.get("dead")]
            if not alive:
                continue

            _event["active"] = True
            _event["ts"] = int(time.time())
            _event["winner_uid"] = None
            _event["participants"] = set()
            _event["reward"] = random.randint(*BIG_REWARD)

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⚡️ ЖМАКНИ!", callback_data="reaction:press")
            ]])

            for uid in alive:
                try:
                    await app.bot.send_message(
                        chat_id=int(uid),
                        text="⚡️ <b>РЕАКЦИЯ!</b>\n\nПервый нажавший получит XP!",
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    pass

            for _ in range(TIMEOUT):
                if not _event["active"]:
                    break
                await asyncio.sleep(1)
            _event["active"] = False

        except Exception as e:
            logger.error(f"reaction_notifier error: {e}")


async def callback_reaction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db, apply_xp
    from achievements import check_achievements, unlock_achievements

    q   = update.callback_query
    uid = str(q.from_user.id)

    if not _event["active"] and _event["winner_uid"] is not None:
        await q.answer("⏰ Уже закончилось!", show_alert=True)
        return

    if not _event["active"]:
        await q.answer("⏰ Время вышло!", show_alert=True)
        return

    if uid in _event["participants"]:
        await q.answer("Ты уже нажал!", show_alert=True)
        return

    _event["participants"].add(uid)

    cabbit = cabbit_db.get(uid)
    if not cabbit or cabbit.get("dead"):
        await q.answer("❌ Нет живого кеббита!", show_alert=True)
        return

    if _event["winner_uid"] is None:
        _event["winner_uid"] = uid
        _event["active"] = False
        reward = _event["reward"]
        apply_xp(cabbit, reward)
        stats = cabbit.setdefault("stats", {})
        stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + reward

        new_achs = check_achievements(cabbit)
        ach_text = ""
        if new_achs:
            bonus = unlock_achievements(cabbit, new_achs)
            cabbit["xp"] += bonus
            ach_text = f"\n\n{'━' * 20}\n🏆 <b>ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>"
            for a in new_achs:
                ach_text += f"\n  {a['emoji']} <b>{a['name']}</b> — {a['desc']}\n  💰 +{a['reward']} XP"
            ach_text += f"\n{'━' * 20}"

        cabbit_db.save_cabbit(uid, cabbit)
        elapsed = time.time() - _event["ts"]
        await q.edit_message_text(
            f"⚡️ <b>ПОБЕДА!</b>\n\n"
            f"Реакция: <b>{elapsed:.1f}с</b>\n"
            f"💰 +{reward} XP{ach_text}",
            parse_mode="HTML",
        )
    else:
        reward = SMALL_REWARD
        apply_xp(cabbit, reward)
        stats = cabbit.setdefault("stats", {})
        stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + reward
        cabbit_db.save_cabbit(uid, cabbit)
        await q.edit_message_text(
            f"⚡️ Не первый... но +{reward} XP за участие!",
            parse_mode="HTML",
        )
