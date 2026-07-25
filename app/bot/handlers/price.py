"""
/price command, the inline refresh button, and the shared rate limiter.
Enhanced to display Index Price alongside active exchange sources.
"""
import asyncio
import logging
import statistics
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app import database
from app.bot.handlers.formatting import (
    _format_24h_change_label,
    _format_exchange_name,
    _format_tehran_hhmm,
    _is_market_price_valid,
)
from app.bot.handlers.keyboards import _get_price_keyboard
from app.services.market_service import get_price_change_24h

logger = logging.getLogger(__name__)

REFRESH_COOLDOWN_TIME = 5.0
STALE_THRESHOLD_SECONDS = 60.0

PRICE_RATE_LIMIT = 20
PRICE_RATE_WINDOW = 60

_price_request_log: dict[int, list[float]] = {}


def _is_rate_limited(chat_id: int) -> bool:
    now = time.time()
    window_start = now - PRICE_RATE_WINDOW
    timestamps = _price_request_log.get(chat_id, [])

    timestamps = [t for t in timestamps if t > window_start]
    _price_request_log[chat_id] = timestamps

    if len(timestamps) >= PRICE_RATE_LIMIT:
        return True

    _price_request_log[chat_id].append(now)
    return False


async def _build_price_card(now: float) -> tuple[str | None, str]:
    try:
        # دریافت تمام اسنپ‌شات‌های آخرین تیک ثبت شده
        snapshots = await database.get_latest_tick_snapshots()
    except Exception as e:
        logger.critical("SYSTEM PARALYSIS: Database query failed! Error: %s", e)
        return None, "db_error"

    if not snapshots:
        logger.critical("SYSTEM PARALYSIS: Database is empty!")
        return None, "empty"

    latest_ts = snapshots[0]["timestamp"]
    recent_snapshots = [s for s in snapshots if _is_market_price_valid(s["price"])]

    if not recent_snapshots:
        logger.critical("SYSTEM PARALYSIS: All recent snapshots in database are invalid!")
        return None, "all_invalid"

    # تفکیک رکورد شاخص و صرافی‌ها
    index_snap = next((s for s in recent_snapshots if s["source"] == "index_median"), None)
    exchanges_snaps = [s for s in recent_snapshots if s["source"] != "index_median"]

    if index_snap:
        index_price = index_snap["price"]
        current_ref_snap = index_snap
    elif exchanges_snaps:
        index_price = statistics.mean([s["price"] for s in exchanges_snaps])
        current_ref_snap = {"timestamp": latest_ts, "price": index_price, "volume": None, "source": "index_median"}
    else:
        index_price = recent_snapshots[0]["price"]
        current_ref_snap = recent_snapshots[0]

    # محاسبه تغییرات ۲۴ ساعته شاخص و فرمت‌دهی با ایموجی/خط تیره
    price_change_24h = await get_price_change_24h(current=current_ref_snap)
    formatted_change = _format_24h_change_label(price_change_24h)

    stale_line = ""
    is_stale = (now - latest_ts) > STALE_THRESHOLD_SECONDS
    if is_stale:
        stale_line = "⚠️ قیمت قدیمی‌ست؛ به‌روزرسانی جدید ناموفق بود\n\n"

    time_label = _format_tehran_hhmm(latest_ts)

    # ساخت خطوط اصلی کارت
    card_lines = [
        f"{stale_line}📊 **شاخص قیمت تتر**",
        "――――――――――――",
        f"💎 **قیمت میانگین بازار:** `{index_price:,.0f}` تومان",
        f"📊 **تغییر ۲۴ ساعته قیمت:** {formatted_change}",
    ]

    # اضافه کردن لیست صرافی‌های فعال با کاما
    if exchanges_snaps:
        raw_names = [_format_exchange_name(s["source"]) for s in exchanges_snaps]
        unique_names = list(dict.fromkeys(raw_names))
        sources_str = "، ".join(unique_names)
        card_lines.append(f"🌐 **صرافی‌های فعال:** {sources_str}")

    card_lines.extend([
        f"🕒 **زمان بروزرسانی:** {time_label} (به وقت تهران)",
        "",
    ])

    text = "\n".join(card_lines)
    return text, ""


_FETCH_FAILURE_TEXT = "❌ الان نتونستیم قیمت رو بگیریم.\nچند لحظه دیگه دوباره امتحان کن."
_RATE_LIMIT_TEXT = (
    "⚠️ کمی آروم‌تر! در یک دقیقه می‌توانی حداکثر ۲۰ بار قیمت را بررسی کنی.\n"
    "لطفاً چند لحظه صبر کن."
)


async def _render_price_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now = time.time()

    cooldown_until = context.user_data.get("price_cooldown_until", 0.0)
    if now < cooldown_until:
        return

    context.user_data["price_cooldown_until"] = now + REFRESH_COOLDOWN_TIME

    last_msg_id = context.user_data.get("last_price_message_id")
    if last_msg_id:
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=last_msg_id, reply_markup=None)
        except BadRequest:
            context.user_data.pop("last_price_message_id", None)

    current_msg = await update.message.reply_text("⏳ در حال محاسبه شاخص قیمت هم‌زمان بازار…")

    if _is_rate_limited(chat_id):
        await current_msg.edit_text(_RATE_LIMIT_TEXT)
        return

    text, _ = await _build_price_card(now)
    if text is None:
        await current_msg.edit_text(_FETCH_FAILURE_TEXT, reply_markup=_get_price_keyboard())
        return

    new_msg = await current_msg.edit_text(text, reply_markup=_get_price_keyboard(), parse_mode="Markdown")
    context.user_data["last_price_message_id"] = new_msg.message_id


async def handle_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("invalid_text_count", None)
    context.user_data.pop("invalid_command_count", None)
    await _render_price_logic(update, context)


async def handle_price_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    now = time.time()

    cooldown_until = context.user_data.get("price_cooldown_until", 0.0)

    if now < cooldown_until:
        if context.user_data.get("refresh_animation_running"):
            return
        context.user_data["refresh_animation_running"] = True

        try:
            while True:
                current_now = time.time()
                remaining = int(round(cooldown_until - current_now))
                if remaining <= 0:
                    break

                temp_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"⏳ {remaining} ثانیه دیگر صبر کنید...", callback_data="cooldown_waiting")]
                ])
                await query.edit_message_reply_markup(reply_markup=temp_keyboard)
                await asyncio.sleep(1.0)
        finally:
            await query.edit_message_reply_markup(reply_markup=_get_price_keyboard())
            context.user_data["refresh_animation_running"] = False
        return

    context.user_data["price_cooldown_until"] = now + REFRESH_COOLDOWN_TIME

    context.user_data.pop("invalid_command_count", None)
    context.user_data.pop("invalid_text_count", None)

    if _is_rate_limited(chat_id):
        await query.edit_message_text(_RATE_LIMIT_TEXT)
        return

    text, _ = await _build_price_card(now)
    if text is None:
        await query.edit_message_text(_FETCH_FAILURE_TEXT, reply_markup=_get_price_keyboard())
        return

    try:
        new_msg = await query.edit_message_text(text, reply_markup=_get_price_keyboard(), parse_mode="Markdown")
        context.user_data["last_price_message_id"] = new_msg.message_id
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise e