"""
duel.py — система дуэлей кеббитов (Камень-Ножницы-Бумага, Best of 3)
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

DUEL_XP = 500
BEATS   = {"камень": "ножницы", "ножницы": "бумага", "бумага": "камень"}
EMOJI   = {"камень": "✊", "ножницы": "✌️", "бумага": "🖐"}

# Активные дуэли в памяти: {challenger_uid: {...}}
_duels: dict = {}


def _move_kb(challenger_uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✊", callback_data=f"duel_move:{challenger_uid}:камень"),
        InlineKeyboardButton("✌️", callback_data=f"duel_move:{challenger_uid}:ножницы"),
        InlineKeyboardButton("🖐",  callback_data=f"duel_move:{challenger_uid}:бумага"),
    ]])


async def show_duel_targets(q, uid: str, cabbit_db, edit_card_fn):
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
    text = (
        "🥊 <b>Выбери противника для дуэли</b>\n\n"
        "Ставка: <b>500 XP</b> | Формат: Best of 3\n"
        "Камень-Ножницы-Бумага"
    )
    try:
        await q.edit_message_caption(
            caption=text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        await q.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def callback_duel_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db, cabbit_status, _edit_card
    q          = update.callback_query
    await q.answer()
    challenger = str(q.from_user.id)
    target_uid = q.data.split(":")[1]

    if target_uid == "cancel":
        cab = cabbit_db.get(challenger)
        if cab:
            await _edit_card(q, cab)
        return

    c_cab = cabbit_db.get(challenger)
    t_cab = cabbit_db.get(target_uid)

    if not c_cab or c_cab.get("dead"):
        await q.answer("У тебя нет живого кеббита!", show_alert=True)
        return
    if not t_cab or t_cab.get("dead"):
        await q.answer("Этот кеббит мёртв!", show_alert=True)
        return
    if c_cab.get("duel_tokens", 0) <= 0:
        await q.answer("У тебя нет жетонов дуэли!", show_alert=True)
        return
    if c_cab.get("xp", 0) < DUEL_XP:
        await q.answer(f"Нужно минимум {DUEL_XP} XP!", show_alert=True)
        return
    if t_cab.get("xp", 0) < DUEL_XP:
        await q.answer("У противника недостаточно XP!", show_alert=True)
        return

    # Списываем жетон
    c_cab["duel_tokens"] = c_cab.get("duel_tokens", 0) - 1
    cabbit_db.save_cabbit(challenger, c_cab)

    _duels[challenger] = {
        "target": target_uid,
        "round":  1,
        "scores": {challenger: 0, target_uid: 0},
        "moves":  {},
        "status": "pending",
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"duel_accept:{challenger}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"duel_decline:{challenger}"),
    ]])
    invite = (
        f"🥊 <b>{c_cab['name']} вызывает тебя на дуэль!</b>\n\n"
        f"Ставка: <b>{DUEL_XP} XP</b> | Best of 3\n"
        f"Принять?"
    )
    try:
        await ctx.application.bot.send_message(
            chat_id=int(target_uid), text=invite,
            parse_mode="HTML", reply_markup=kb,
        )
    except Exception as e:
        logger.warning(f"duel invite: {e}")

    confirm = f"✅ Вызов отправлен <b>{t_cab['name']}</b>!\n\n{cabbit_status(c_cab)}"
    await _edit_card(q, c_cab, confirm)


async def callback_duel_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db
    q          = update.callback_query
    await q.answer()
    target_uid = str(q.from_user.id)
    challenger = q.data.split(":")[1]

    duel = _duels.get(challenger)
    if not duel or duel["status"] != "pending" or duel["target"] != target_uid:
        await q.edit_message_text("❌ Дуэль недействительна.")
        return

    duel["status"] = "active"
    duel["moves"]  = {}

    c_cab = cabbit_db.get(challenger)
    t_cab = cabbit_db.get(target_uid)
    text  = (
        f"⚔️ <b>{c_cab['name']} vs {t_cab['name']}</b>\n\n"
        f"🎯 Раунд 1 из 3 | Ставка: {DUEL_XP} XP\n\n"
        f"Выбери ход:"
    )
    kb = _move_kb(challenger)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    try:
        await ctx.application.bot.send_message(
            chat_id=int(challenger), text=text,
            parse_mode="HTML", reply_markup=kb,
        )
    except Exception as e:
        logger.warning(f"duel start notify: {e}")


async def callback_duel_decline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db
    q          = update.callback_query
    await q.answer()
    target_uid = str(q.from_user.id)
    challenger = q.data.split(":")[1]

    _duels.pop(challenger, None)
    t_cab = cabbit_db.get(target_uid)
    name  = t_cab["name"] if t_cab else "Противник"
    await q.edit_message_text("❌ Ты отказался от дуэли.")

    c_cab = cabbit_db.get(challenger)
    if c_cab:
        c_cab["duel_tokens"] = c_cab.get("duel_tokens", 0) + 1
        cabbit_db.save_cabbit(challenger, c_cab)
    try:
        await ctx.application.bot.send_message(
            chat_id=int(challenger),
            text=f"😔 <b>{name}</b> отказался. Жетон возвращён.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"duel decline notify: {e}")


async def callback_duel_move(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from cabbit import cabbit_db
    q          = update.callback_query
    await q.answer()
    uid        = str(q.from_user.id)
    parts      = q.data.split(":")
    challenger = parts[1]
    move       = parts[2]

    duel = _duels.get(challenger)
    if not duel or duel["status"] != "active":
        await q.answer("Дуэль уже завершена!", show_alert=True)
        return

    target_uid = duel["target"]
    if uid not in (challenger, target_uid):
        await q.answer("Это не твоя дуэль!", show_alert=True)
        return
    if uid in duel["moves"]:
        await q.answer("Ты уже сделал ход, ждём противника...", show_alert=True)
        return

    duel["moves"][uid] = move
    await q.edit_message_text(
        f"✅ Ход: <b>{EMOJI.get(move,'')} {move}</b>\n\nОжидаем противника...",
        parse_mode="HTML",
    )

    if len(duel["moves"]) == 2:
        await _resolve_round(ctx.application, challenger, target_uid, duel)


async def _resolve_round(app, challenger: str, target_uid: str, duel: dict):
    from cabbit import cabbit_db
    c_move = duel["moves"][challenger]
    t_move = duel["moves"][target_uid]
    round_ = duel["round"]
    c_cab  = cabbit_db.get(challenger)
    t_cab  = cabbit_db.get(target_uid)
    c_name = c_cab["name"] if c_cab else "?"
    t_name = t_cab["name"] if t_cab else "?"

    if c_move == t_move:
        result = "🤝 Ничья в раунде!"
    elif BEATS[c_move] == t_move:
        duel["scores"][challenger] += 1
        result = f"🏆 Раунд за <b>{c_name}</b>!"
    else:
        duel["scores"][target_uid] += 1
        result = f"🏆 Раунд за <b>{t_name}</b>!"

    cs = duel["scores"][challenger]
    ts = duel["scores"][target_uid]
    round_text = (
        f"⚔️ Раунд {round_}:\n"
        f"🐰 {c_name}: {EMOJI.get(c_move,'')} {c_move}\n"
        f"🐰 {t_name}: {EMOJI.get(t_move,'')} {t_move}\n\n"
        f"{result}\n"
        f"Счёт: <b>{cs} — {ts}</b>\n"
    )

    if cs >= 2 or ts >= 2 or round_ >= 3:
        await _finish_duel(app, challenger, target_uid, duel, round_text)
        return

    duel["round"] += 1
    duel["moves"]  = {}
    next_text = round_text + f"\n🎯 Раунд {duel['round']} из 3 — выбери ход:"
    kb = _move_kb(challenger)
    for uid in (challenger, target_uid):
        try:
            await app.bot.send_message(
                chat_id=int(uid), text=next_text,
                parse_mode="HTML", reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"round notify {uid}: {e}")


async def _finish_duel(app, challenger: str, target_uid: str, duel: dict, last_text: str):
    from cabbit import cabbit_db
    _duels.pop(challenger, None)

    cs    = duel["scores"][challenger]
    ts    = duel["scores"][target_uid]
    c_cab = cabbit_db.get(challenger)
    t_cab = cabbit_db.get(target_uid)
    if not c_cab or not t_cab:
        return

    if cs == ts:
        text = last_text + "\n🤝 <b>Ничья! XP не меняется.</b>"
        for uid in (challenger, target_uid):
            try:
                await app.bot.send_message(chat_id=int(uid), text=text, parse_mode="HTML")
            except Exception:
                pass
        return

    if cs > ts:
        winner_uid, loser_uid = challenger, target_uid
        winner_cab, loser_cab = c_cab, t_cab
        score_str = f"{cs}:{ts}"
    else:
        winner_uid, loser_uid = target_uid, challenger
        winner_cab, loser_cab = t_cab, c_cab
        score_str = f"{ts}:{cs}"

    winner_cab["xp"] = winner_cab.get("xp", 0) + DUEL_XP
    loser_cab["xp"]  = max(0, loser_cab.get("xp", 0) - DUEL_XP)
    cabbit_db.save_cabbit(winner_uid, winner_cab)
    cabbit_db.save_cabbit(loser_uid, loser_cab)

    try:
        await app.bot.send_message(
            chat_id=int(winner_uid),
            text=last_text + f"\n🏆 <b>{winner_cab['name']} победил {score_str}!</b>\n✨ +{DUEL_XP} XP",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"finish winner: {e}")
    try:
        await app.bot.send_message(
            chat_id=int(loser_uid),
            text=last_text + f"\n💀 <b>{loser_cab['name']} проиграл {score_str}!</b>\n💔 -{DUEL_XP} XP",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"finish loser: {e}")
