"""
promo.py — система промокодов для админа
"""
import json
import logging
import os
from threading import Lock

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

PROMO_FILE = "/app/data/promos.json"
_lock      = Lock()

# Типы промокодов
PROMO_TYPES = {
    "морковь":   {"emoji": "🥕", "food": "Морковь",   "xp": 80},
    "корм":      {"emoji": "🍗", "food": "Корм",       "xp": 200},
    "вкусность": {"emoji": "✨", "food": "Вкусность",  "xp": 500},
    "жетон":     {"emoji": "🥊", "food": None,         "xp": 0},
    "xp":        {"emoji": "💰", "food": None,         "xp": 0},  # кастомный XP
}


def _load() -> dict:
    if not os.path.exists(PROMO_FILE):
        return {}
    try:
        with open(PROMO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    with open(PROMO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def create_promo(code: str, promo_type: str, uses: int = 1, xp_amount: int = 0) -> bool:
    """Создать промокод. Возвращает False если уже существует."""
    with _lock:
        data = _load()
        if code in data:
            return False
        data[code] = {
            "type":      promo_type,
            "uses_left": uses,
            "used_by":   [],
            "xp_amount": xp_amount,
        }
        _save(data)
    return True


def delete_promo(code: str) -> bool:
    with _lock:
        data = _load()
        if code not in data:
            return False
        del data[code]
        _save(data)
    return True


def use_promo(code: str, uid: str) -> tuple[bool, str, dict | None]:
    """
    Активировать промокод.
    Возвращает (success, error_msg, promo_data).
    """
    with _lock:
        data = _load()
        if code not in data:
            return False, "Промокод не найден.", None

        promo = data[code]
        if uid in promo["used_by"]:
            return False, "Ты уже использовал этот промокод.", None
        if promo["uses_left"] <= 0:
            return False, "Промокод уже недействителен.", None

        promo["used_by"].append(uid)
        promo["uses_left"] -= 1
        _save(data)
    return True, "", promo


def list_promos() -> list[dict]:
    data = _load()
    result = []
    for code, p in data.items():
        result.append({
            "code":       code,
            "type":       p["type"],
            "uses_left":  p["uses_left"],
            "used_count": len(p["used_by"]),
            "xp_amount":  p.get("xp_amount", 0),
        })
    return result


# ─── Хендлеры ─────────────────────────────────────────────────────────────────

async def cmd_promo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пользователь активирует промокод: /promo КОД"""
    from cabbit import cabbit_db, apply_xp, cabbit_status

    uid = str(update.effective_user.id)

    if not ctx.args:
        await update.message.reply_text(
            "Использование: <code>/promo КОД</code>",
            parse_mode="HTML",
        )
        return

    code   = ctx.args[0].strip().upper()
    cabbit = cabbit_db.get(uid)

    if not cabbit or cabbit.get("dead"):
        await update.message.reply_text(
            "❌ Сначала создай кеббита через /cabbit"
        )
        return

    ok, err, promo = use_promo(code, uid)
    if not ok:
        await update.message.reply_text(f"❌ {err}")
        return

    ptype = promo["type"]
    info  = PROMO_TYPES.get(ptype)

    if not info:
        await update.message.reply_text("❌ Неизвестный тип промокода.")
        return

    if ptype == "жетон":
        cabbit["duel_tokens"] = cabbit.get("duel_tokens", 0) + 1
        cabbit_db.save_cabbit(uid, cabbit)
        await update.message.reply_text(
            f"✅ Промокод активирован!\n\n"
            f"🥊 +1 жетон дуэли\n\n"
            f"{cabbit_status(cabbit)}",
            parse_mode="HTML",
        )
    elif ptype == "xp":
        xp_amount = promo.get("xp_amount", 0)
        if xp_amount <= 0:
            await update.message.reply_text("❌ Промокод повреждён (0 XP).")
            return
        leveled_up, new_level = apply_xp(cabbit, xp_amount)
        stats = cabbit.setdefault("stats", {})
        stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + xp_amount

        from achievements import check_achievements, unlock_achievements
        new_achs = check_achievements(cabbit)
        ach_text = ""
        if new_achs:
            bonus = unlock_achievements(cabbit, new_achs)
            apply_xp(cabbit, bonus)
            ach_text = f"\n\n{'━' * 20}\n🏆 <b>ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>"
            for a in new_achs:
                ach_text += f"\n  {a['emoji']} <b>{a['name']}</b> — {a['desc']}\n  💰 +{a['reward']} XP"
            ach_text += f"\n{'━' * 20}"

        cabbit_db.save_cabbit(uid, cabbit)
        text = f"✅ Промокод активирован!\n\n💰 <b>+{xp_amount} XP</b>\n"
        if leveled_up:
            text += f"🎉 <b>УРОВЕНЬ {new_level}!</b>\n"
        text += f"{ach_text}\n{cabbit_status(cabbit)}"
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        food_name = info["food"]
        food_xp   = info["xp"]
        leveled_up, new_level = apply_xp(cabbit, food_xp)
        counts = cabbit.setdefault("food_counts", {"Морковь": 0, "Корм": 0, "Вкусность": 0})
        counts[food_name] = counts.get(food_name, 0) + 1

        stats = cabbit.setdefault("stats", {})
        stats["xp_earned_total"] = stats.get("xp_earned_total", 0) + food_xp

        from achievements import check_achievements, unlock_achievements
        new_achs = check_achievements(cabbit)
        ach_text = ""
        if new_achs:
            bonus = unlock_achievements(cabbit, new_achs)
            from cabbit import apply_xp as _apply_xp
            _apply_xp(cabbit, bonus)
            ach_text = f"\n\n{'━' * 20}\n🏆 <b>ДОСТИЖЕНИЕ РАЗБЛОКИРОВАНО!</b>"
            for a in new_achs:
                ach_text += f"\n  {a['emoji']} <b>{a['name']}</b> — {a['desc']}\n  💰 +{a['reward']} XP"
            ach_text += f"\n{'━' * 20}"

        cabbit_db.save_cabbit(uid, cabbit)

        text = (
            f"✅ Промокод активирован!\n\n"
            f"{info['emoji']} <b>{food_name}</b> — +{food_xp} XP\n"
        )
        if leveled_up:
            text += f"🎉 <b>УРОВЕНЬ {new_level}!</b>\n"
        text += f"{ach_text}\n{cabbit_status(cabbit)}"
        await update.message.reply_text(text, parse_mode="HTML")


async def cmd_createpromo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ создаёт промокод: /createpromo КОД ТИП [USES]
    Типы: морковь, корм, вкусность, жетон
    Пример: /createpromo SUMMER24 вкусность 10
    """
    from config import ADMIN_ID
    uid = str(update.effective_user.id)

    if uid != str(ADMIN_ID):
        await update.message.reply_text("❌ Нет доступа.")
        return

    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text(
            "Использование: <code>/createpromo КОД ТИП [кол-во]</code>\n\n"
            "Типы: <code>морковь</code>, <code>корм</code>, <code>вкусность</code>, <code>жетон</code>\n"
            "XP: <code>/createpromo КОД xp КОЛИЧЕСТВО_XP [кол-во_использований]</code>\n"
            "Пример: <code>/createpromo SUMMER24 вкусность 10</code>\n"
            "Пример: <code>/createpromo BUGFIX xp 500 50</code>",
            parse_mode="HTML",
        )
        return

    code  = ctx.args[0].strip().upper()
    ptype = ctx.args[1].strip().lower()

    if ptype not in PROMO_TYPES:
        await update.message.reply_text(
            f"❌ Неизвестный тип. Доступны: {', '.join(PROMO_TYPES.keys())}"
        )
        return

    if ptype == "xp":
        # /createpromo CODE xp 500 [uses]
        if len(ctx.args) < 3 or not ctx.args[2].isdigit():
            await update.message.reply_text(
                "Для XP промокода укажи количество:\n"
                "<code>/createpromo КОД xp 500 [кол-во_использований]</code>",
                parse_mode="HTML",
            )
            return
        xp_amount = int(ctx.args[2])
        uses = int(ctx.args[3]) if len(ctx.args) >= 4 and ctx.args[3].isdigit() else 1
        ok = create_promo(code, ptype, uses, xp_amount=xp_amount)
        if not ok:
            await update.message.reply_text(f"❌ Промокод <code>{code}</code> уже существует.", parse_mode="HTML")
            return
        await update.message.reply_text(
            f"✅ Промокод создан!\n\n"
            f"🔑 Код: <code>{code}</code>\n"
            f"Тип: 💰 XP — <b>{xp_amount} XP</b>\n"
            f"Использований: <b>{uses}</b>",
            parse_mode="HTML",
        )
    else:
        uses = int(ctx.args[2]) if len(ctx.args) >= 3 and ctx.args[2].isdigit() else 1
        ok = create_promo(code, ptype, uses)
        if not ok:
            await update.message.reply_text(f"❌ Промокод <code>{code}</code> уже существует.", parse_mode="HTML")
            return
        info = PROMO_TYPES[ptype]
        await update.message.reply_text(
            f"✅ Промокод создан!\n\n"
            f"🔑 Код: <code>{code}</code>\n"
            f"Тип: {info['emoji']} {ptype}\n"
            f"Использований: <b>{uses}</b>",
            parse_mode="HTML",
        )


async def cmd_listpromos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ смотрит все промокоды: /listpromos"""
    from config import ADMIN_ID
    uid = str(update.effective_user.id)

    if uid != str(ADMIN_ID):
        await update.message.reply_text("❌ Нет доступа.")
        return

    promos = list_promos()
    if not promos:
        await update.message.reply_text("📭 Нет активных промокодов.")
        return

    lines = ["🔑 <b>Активные промокоды:</b>\n"]
    for p in promos:
        info = PROMO_TYPES.get(p["type"], {})
        emoji = info.get("emoji", "?")
        type_str = p["type"]
        if p["type"] == "xp":
            type_str = f"xp ({p.get('xp_amount', '?')} XP)"
        lines.append(
            f"{emoji} <code>{p['code']}</code> — {type_str} "
            f"| осталось: <b>{p['uses_left']}</b> | использовано: {p['used_count']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_deletepromo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ удаляет промокод: /deletepromo КОД"""
    from config import ADMIN_ID
    uid = str(update.effective_user.id)

    if uid != str(ADMIN_ID):
        await update.message.reply_text("❌ Нет доступа.")
        return

    if not ctx.args:
        await update.message.reply_text("Использование: <code>/deletepromo КОД</code>", parse_mode="HTML")
        return

    code = ctx.args[0].strip().upper()
    ok   = delete_promo(code)
    if ok:
        await update.message.reply_text(f"🗑 Промокод <code>{code}</code> удалён.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Промокод <code>{code}</code> не найден.", parse_mode="HTML")
