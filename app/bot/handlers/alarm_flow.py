"""
Alarm creation ConversationHandler: condition -> target number -> frequency.
"""
import re
import warnings

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from app import database
from app.bot.bot_state import poller_status
from app.bot.handlers.formatting import _CONDITION_LABELS, _FREQUENCY_LABELS, _parse_number, _validate_target_price
from app.bot.handlers.keyboards import (
    BTN_ALARM,
    MAIN_MENU_KEYBOARD,
    NAV_MAIN_MENU_CALLBACK,
    _MENU_BUTTON_TEXTS,
    _condition_option_rows,
    _frequency_option_rows,
    _with_cancel_footer,
)
from app.bot.handlers.nav import handle_flow_cancel_callback, handle_global_interrupt, handle_nav_main_menu
from app.bot.handlers.quota_flow import (
    handle_quota_alarms_menu,
    handle_quota_delete_alarm,
    handle_quota_main_menu,
    handle_quota_start_new_alarm,
)
from app.bot.handlers.states import MAX_ACTIVE_ALARMS, WAITING_CONDITION, WAITING_FREQUENCY, WAITING_NUMBER
from app.bot.handlers.timeout_job import _clear_alarm_timeout_job, _handle_alarm_timeout_trigger

_MENU_BUTTON_REGEX = "^(" + "|".join(re.escape(t) for t in _MENU_BUTTON_TEXTS) + ")$"


def _alarm_number_prompt(condition: str) -> str:
    if condition in ("percentage_up", "percentage_down"):
        return "یک عدد درصد وارد کن (مثلاً ۲.۵):"
    return "یک عدد برای قیمت هدف وارد کن (مثلاً ۶۰۰۰۰):"


async def _get_current_price_formatted() -> str:
    current_price = poller_status.last_price
    if current_price is None:
        snapshot = await database.get_latest_snapshot()
        current_price = snapshot["price"] if snapshot else None

    return f"{current_price:,.0f}" if current_price else "نامشخص"


async def handle_alarm_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    context.user_data.pop("alarm_condition", None)
    context.user_data.pop("alarm_target_price", None)
    context.user_data.pop("alarm_message_id", None)
    context.user_data.pop("alarm_flow_expired", None)

    _clear_alarm_timeout_job(chat_id, context)

    active_count = await database.count_active_alarms(chat_id)
    if active_count >= MAX_ACTIVE_ALARMS:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 مدیریت و حذف هشدارها", callback_data="quota:show_alarms")],
            [InlineKeyboardButton("🏠 منوی اصلی", callback_data=NAV_MAIN_MENU_CALLBACK)],
        ])
        sent_msg = await update.message.reply_text(
            "⚠️ شما به سقف ۱۰  هشدار فعال رسیده‌اید.\n"
            "برای ساخت هشدار جدید, ابتدا یکی از هشدارهای قبلی را حذف کنید.",
            reply_markup=keyboard,
        )
        context.user_data["alarm_message_id"] = sent_msg.message_id
        return WAITING_CONDITION

    price_formatted = await _get_current_price_formatted()

    keyboard = _with_cancel_footer(_condition_option_rows())
    sent_msg = await update.message.reply_text(
        "🔔 هشدار جدید\n"
        "――――――――――――\n\n"
        f"📊 قیمت فعلی تتر: {price_formatted} تومان\n\n"
        "اول نوع شرط هشدار را انتخاب کن:",
        reply_markup=keyboard,
    )
    context.user_data["alarm_message_id"] = sent_msg.message_id

    if context.application.job_queue:
        context.application.job_queue.run_once(
            _handle_alarm_timeout_trigger,
            when=900,
            name=f"alarm_timeout_{chat_id}",
            data={"chat_id": chat_id, "message_id": sent_msg.message_id}
        )

    return WAITING_CONDITION


async def handle_alarm_condition_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    alarm_msg_id = context.user_data.get("alarm_message_id")

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
    except BadRequest:
        pass

    if alarm_msg_id:
        keyboard = _with_cancel_footer([])
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=alarm_msg_id,
                text="⚠️ انتخاب نامعتبر!\n\nبرای تنظیم هشدار، لطفاً ابتدا یکی از شرایط بالا را انتخاب کنید یا در صورت تمایل فرآیند را لغو کنید.",
                reply_markup=keyboard
            )
        except BadRequest:
            pass
    return WAITING_CONDITION


async def handle_alarm_condition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    condition = query.data.split(":", 1)[1]
    context.user_data["alarm_condition"] = condition

    price_formatted = await _get_current_price_formatted()
    condition_label = _CONDITION_LABELS.get(condition, condition)

    keyboard = _with_cancel_footer([], back_callback="nav:back_condition")
    await query.edit_message_text(
        f"شرط انتخابی: {condition_label} ✅\n\n"
        f"📊 قیمت فعلی تتر: {price_formatted} تومان\n\n"
        f"✍️ یک عدد برای قیمت هدف وارد کن:\n"
        f"*(می‌توانی عدد را به فارسی یا انگلیسی، ساده یا با کاما بنویسی؛ مثلاً: `60,000` یا `۶۰۰۰۰`)*",
        reply_markup=keyboard,
    )

    _clear_alarm_timeout_job(query.message.chat_id, context)
    if context.application.job_queue:
        context.application.job_queue.run_once(
            _handle_alarm_timeout_trigger,
            when=900,
            name=f"alarm_timeout_{query.message.chat_id}",
            data={"chat_id": query.message.chat_id, "message_id": context.user_data["alarm_message_id"]}
        )

    return WAITING_NUMBER


async def handle_alarm_back_to_condition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    price_formatted = await _get_current_price_formatted()

    keyboard = _with_cancel_footer(_condition_option_rows())
    await query.edit_message_text(
        "🔔 هشدار جدید\n"
        "――――――――――――\n\n"
        f"📊 قیمت فعلی تتر: {price_formatted} تومان\n\n"
        "اول نوع شرط هشدار را انتخاب کن:",
        reply_markup=keyboard,
    )
    return WAITING_CONDITION


async def handle_alarm_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if context.user_data.get("alarm_flow_expired"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except BadRequest:
            pass
        context.user_data.clear()
        return ConversationHandler.END

    condition = context.user_data.get("alarm_condition")
    alarm_msg_id = context.user_data.get("alarm_message_id")

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
    except BadRequest:
        pass

    if condition is None or alarm_msg_id is None:
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ خطایی رخ داد. لطفاً فرآیند ثبت هشدار را مجدداً شروع کنید.",
            reply_markup=MAIN_MENU_KEYBOARD
        )
        return ConversationHandler.END

    text_received = update.message.text or ""
    keyboard = _with_cancel_footer([], back_callback="nav:back_condition")
    value = _parse_number(text_received)

    if value is None or text_received in _MENU_BUTTON_TEXTS or text_received.startswith("/"):
        error_msg = (
            "متوجه نشدم 🤔 لطفاً فقط یک عدد درصد معتبر وارد کن (مثلاً ۲.۵):"
            if condition in ("percentage_up", "percentage_down")
            else "متوجه نشدم 🤔 لطفاً فقط یک قیمت معتبر به عدد وارد کن (مثلاً ۶۰,۰۰۰):"
        )
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=alarm_msg_id,
                text=error_msg, reply_markup=keyboard
            )
        except BadRequest:
            pass
        return WAITING_NUMBER

    if condition in ("above", "below"):
        is_valid, err_msg = _validate_target_price(condition, value)
        if not is_valid:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=alarm_msg_id,
                    text=err_msg, reply_markup=keyboard
                )
            except BadRequest:
                pass
            return WAITING_NUMBER

    if condition in ("percentage_up", "percentage_down"):
        current_price = poller_status.last_price
        if current_price is None:
            snapshot = await database.get_latest_snapshot()
            current_price = snapshot["price"] if snapshot else None

        if current_price is None:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=alarm_msg_id,
                text="هنوز داده‌ای از بازار دریافت نشده. فرآیند لغو شد."
            )
            context.user_data.clear()
            return ConversationHandler.END

        pct = value / 100.0
        target_price = current_price * (1 + pct) if condition == "percentage_up" else current_price * (1 - pct)
        explanation = (
            f"💰 قیمت فعلی: {current_price:,.0f} تومان\n"
            f"📐 درصد: {value:g}٪\n"
            f"🎯 قیمت هدف محاسبه‌شده: {target_price:,.0f} تومان"
        )
    else:
        target_price = value
        explanation = f"🎯 قیمت هدف: {target_price:,.0f} تومان"

    context.user_data["alarm_target_price"] = target_price

    frequency_keyboard = _with_cancel_footer(_frequency_option_rows(), back_callback="nav:back_number")
    await context.bot.edit_message_text(
        chat_id=chat_id, message_id=alarm_msg_id,
        text=f"{explanation}\n\nچند بار می‌خوای هشدار بگیری؟",
        reply_markup=frequency_keyboard
    )

    _clear_alarm_timeout_job(chat_id, context)
    if context.application.job_queue:
        context.application.job_queue.run_once(
            _handle_alarm_timeout_trigger,
            when=900,
            name=f"alarm_timeout_{chat_id}",
            data={"chat_id": chat_id, "message_id": alarm_msg_id}
        )

    return WAITING_FREQUENCY


async def handle_alarm_back_to_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    condition = context.user_data.get("alarm_condition")

    if condition is None:
        context.user_data.clear()
        await query.edit_message_text(
            "⚠️ جلسه‌ی کاری شما به پایان رسیده است. لطفاً فرآیند تنظیم هشدار را مجدداً شروع کنید."
        )
        return ConversationHandler.END

    price_formatted = await _get_current_price_formatted()
    condition_label = _CONDITION_LABELS.get(condition, condition)

    keyboard = _with_cancel_footer([], back_callback="nav:back_condition")
    await query.edit_message_text(
        f"شرط انتخابی: {condition_label} ✅\n\n"
        f"📊 قیمت فعلی تتر: {price_formatted} تومان\n\n"
        f"✍️ یک عدد برای قیمت هدف وارد کن:\n"
        f"*(می‌توانی عدد را به فارسی یا انگلیسی، ساده یا با کاما بنویسی؛ مثلاً: `60,000` یا `۶۰۰۰۰`)*",
        reply_markup=keyboard,
    )
    return WAITING_NUMBER


async def handle_alarm_frequency_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if context.user_data.get("alarm_flow_expired"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except BadRequest:
            pass
        context.user_data.clear()
        return ConversationHandler.END

    keyboard = _with_cancel_footer(_frequency_option_rows(), back_callback="nav:back_number")
    await update.message.reply_text(
        "⚠️ تعیین تناوب الزامی است!\n\n"
        "لطفاً برای مشخص کردن تعداد دفعات دریافت هشدار، حتماً یکی از گزینه‌های بالا را انتخاب کنید یا فرآیند را لغو کنید.",
        reply_markup=keyboard,
    )
    return WAITING_FREQUENCY


async def handle_alarm_frequency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    frequency = query.data.split(":", 1)[1]

    target_price = context.user_data.get("alarm_target_price")
    condition = context.user_data.get("alarm_condition")
    alarm_msg_id = context.user_data.get("alarm_message_id")

    if target_price is None or condition is None or alarm_msg_id is None:
        await query.edit_message_text("اطلاعات هشدار از دست رفت. دوباره با /alarm شروع کن.")
        return ConversationHandler.END

    _clear_alarm_timeout_job(chat_id, context)

    # استفاده از تابع اتمیک جدید با کنترل هم‌زمانی سهمیه
    alarm_id = await database.insert_alarm_with_quota_check(
        chat_id=chat_id,
        target_price=target_price,
        condition=condition,
        frequency=frequency,
        max_limit=MAX_ACTIVE_ALARMS
    )

    if alarm_id == 0:
        context.user_data.pop("alarm_condition", None)
        context.user_data.pop("alarm_target_price", None)
        context.user_data.pop("alarm_message_id", None)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 مدیریت هشدارها", callback_data="go_to_profile_alarms")],
            [InlineKeyboardButton("🏠 منوی اصلی", callback_data=NAV_MAIN_MENU_CALLBACK)],
        ])
        await query.edit_message_text(
            "⚠️ **در همین حین به سقف ۱۰ هشدار فعال رسیده‌اید!**\n\n"
            "این هشدار ذخیره نشد. برای ثبت هشدار جدید، لطفاً ابتدا یکی از هشدارهای قبلی خود را حذف کنید.",
            reply_markup=keyboard
        )
        return ConversationHandler.END

    context.user_data.pop("alarm_condition", None)
    context.user_data.pop("alarm_target_price", None)
    context.user_data.pop("alarm_message_id", None)

    main_menu_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data=NAV_MAIN_MENU_CALLBACK)]])
    await query.edit_message_text(
        "✅ هشدار با موفقیت ثبت شد!\n――――――――――――\n\n"
        f"⚙️ شرط: {_CONDITION_LABELS[condition]}\n"
        f"🎯 قیمت هدف: {target_price:,.0f} تومان\n"
        f"🔁 تناوب: {_FREQUENCY_LABELS[frequency]}\n\n"
        "به محض برقرار شدن شرط بهتون خبر می‌دیم 🔔",
        reply_markup=main_menu_markup
    )
    return ConversationHandler.END


async def handle_alarm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("alarm_condition", None)
    context.user_data.pop("alarm_target_price", None)

    await update.message.reply_text(
        "عملیات لغو شد. هر وقت خواستید با /alarm دوباره شروع کنید."
    )
    return ConversationHandler.END


def build_alarm_conversation_handler() -> ConversationHandler:
    text_filter_excluding_menu = filters.TEXT & ~filters.COMMAND & ~filters.Regex(_MENU_BUTTON_REGEX)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=PTBUserWarning)
        return ConversationHandler(
            entry_points=[
                CommandHandler("alarm", handle_alarm_entry),
                MessageHandler(filters.Regex(f"^{re.escape(BTN_ALARM)}$"), handle_alarm_entry),
            ],
            states={
                WAITING_CONDITION: [
                    CallbackQueryHandler(handle_quota_alarms_menu, pattern=r"^quota:show_alarms$"),
                    CallbackQueryHandler(handle_quota_delete_alarm, pattern=r"^quota_del:\d+$"),
                    CallbackQueryHandler(handle_quota_start_new_alarm, pattern=r"^quota:start_new_alarm$"),
                    CallbackQueryHandler(handle_quota_main_menu, pattern=r"^quota:main_menu$"),
                    CallbackQueryHandler(handle_alarm_condition, pattern=r"^condition:"),
                    CallbackQueryHandler(handle_flow_cancel_callback, pattern=r"^nav:cancel_flow$"),
                    MessageHandler(text_filter_excluding_menu, handle_alarm_condition_fallback),
                ],
                WAITING_NUMBER: [
                    MessageHandler(text_filter_excluding_menu, handle_alarm_number),
                    CallbackQueryHandler(handle_alarm_back_to_condition, pattern=r"^nav:back_condition$"),
                    CallbackQueryHandler(handle_flow_cancel_callback, pattern=r"^nav:cancel_flow$"),
                ],
                WAITING_FREQUENCY: [
                    CallbackQueryHandler(handle_alarm_frequency, pattern=r"^frequency:"),
                    CallbackQueryHandler(handle_alarm_back_to_number, pattern=r"^nav:back_number$"),
                    CallbackQueryHandler(handle_flow_cancel_callback, pattern=r"^nav:cancel_flow$"),
                    MessageHandler(text_filter_excluding_menu, handle_alarm_frequency_fallback),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", handle_alarm_cancel),
                CallbackQueryHandler(handle_nav_main_menu, pattern=r"^nav:main_menu$"),
                CallbackQueryHandler(handle_flow_cancel_callback, pattern=r"^nav:cancel_flow$"),
                MessageHandler(filters.COMMAND | filters.Regex(_MENU_BUTTON_REGEX), handle_global_interrupt),
            ],
            per_message=False,
        )