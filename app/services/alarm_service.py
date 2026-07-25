"""
Alarm evaluation and trigger engine.

Extracted from main.py to satisfy layering rules: all alarm math and Telegram
dispatch for triggered alarms lives here.
"""
import logging
import time

from telegram.ext import ExtBot

from app import database

logger = logging.getLogger(__name__)

# 0.3% neutral band used to require a genuine crossing (not noise) before
# an alarm is allowed to rearm after firing (Strictly for non-daily modes).
_HYSTERESIS_BAND = 0.003

# Cooldown windows per frequency mode, in seconds.
_EVERY_TIME_COOLDOWN_SECONDS = 180

_CONDITION_LABELS = {
    "above": "📈 بالاتر از",
    "below": "📉 پایین‌تر از",
    "percentage_up": "🚀 افزایش بیش از",
    "percentage_down": "🔻 کاهش بیش از",
}


async def evaluate_and_trigger_alarms(
    current_price: float, source: str, bot_token: str
) -> None:
    """
    موتور اصلی ارزیابی و شلیک هشدارهای قیمت بر اساس شاخص مرجع میانه بازار.
    """
    try:
        # دریافت تمام هشدارهای فعال (حتی هشدارهایی که مسلح نیستند)
        active_alarms = await database.get_active_alarms()
        if not active_alarms:
            return

        now = time.time()

        for alarm in active_alarms:
            alarm_id = alarm["id"]
            chat_id = alarm["chat_id"]
            target_price = alarm["target_price"]
            condition = alarm["condition"]
            frequency = alarm["frequency"]
            last_triggered = alarm["last_triggered_at"]
            is_armed = alarm["is_armed"]

            # ---------------------------------------------------------------------------
            # لایه اول: ارزیابی وضعیت مسلح بودن (Rearm) و تشخیص وضعیت جاری کانال قیمت
            # ---------------------------------------------------------------------------
            is_condition_met = False
            in_opposite_channel = False

            if condition == "above":
                is_condition_met = current_price >= target_price
                in_opposite_channel = current_price <= target_price

                if not is_armed:
                    if frequency == "every_time":
                        if current_price <= target_price * (1 - _HYSTERESIS_BAND):
                            await database.update_alarm_armed_status(alarm_id, 1)
                            logger.info("Alarm %s (every_time) re-armed.", alarm_id)
                            continue
                    elif frequency == "daily":
                        if in_opposite_channel:
                            await database.update_alarm_armed_status(alarm_id, 1)
                            logger.info("Daily alarm %s re-armed by returning to safe channel.", alarm_id)
                            continue
                    else:  # once
                        if current_price < target_price:
                            await database.update_alarm_armed_status(alarm_id, 1)
                            continue

            elif condition == "below":
                is_condition_met = current_price <= target_price
                in_opposite_channel = current_price >= target_price

                if not is_armed:
                    if frequency == "every_time":
                        if current_price >= target_price * (1 + _HYSTERESIS_BAND):
                            await database.update_alarm_armed_status(alarm_id, 1)
                            logger.info("Alarm %s (every_time) re-armed.", alarm_id)
                            continue
                    elif frequency == "daily":
                        if in_opposite_channel:
                            await database.update_alarm_armed_status(alarm_id, 1)
                            logger.info("Daily alarm %s re-armed by returning to safe channel.", alarm_id)
                            continue
                    else:  # once
                        if current_price > target_price:
                            await database.update_alarm_armed_status(alarm_id, 1)
                            continue

            elif condition == "percentage_up":
                is_condition_met = current_price >= target_price
                if not is_armed and current_price <= target_price * (1 - _HYSTERESIS_BAND):
                    await database.update_alarm_armed_status(alarm_id, 1)
                    continue

            elif condition == "percentage_down":
                is_condition_met = current_price <= target_price
                if not is_armed and current_price >= target_price * (1 + _HYSTERESIS_BAND):
                    await database.update_alarm_armed_status(alarm_id, 1)
                    continue

            # ---------------------------------------------------------------------------
            # لایه دوم: گیت‌های کنترلی تناوب، جلوگیری از اسپم و مدیریت تعویق (Daily Control)
            # ---------------------------------------------------------------------------

            # منطق اختصاصی هشدارهای روزانه (Hybrid Daily Control)
            if frequency == "daily":
                triggered_today = database.is_alarm_triggered_today(last_triggered)

                if is_armed == 1:
                    if is_condition_met:
                        if not triggered_today:
                            pass
                        else:
                            await database.update_alarm_armed_status(alarm_id, 0)
                            logger.info("Daily alarm %s broke boundary but postponed due to daily limit.", alarm_id)
                            continue
                    else:
                        continue
                else:
                    if is_condition_met and not triggered_today:
                        cond_str = _CONDITION_LABELS.get(condition, condition)
                        message_text = (
                            f"🌅 **گزارش روز جدید!**\n"
                            f"――――――――――――\n\n"
                            f"🎯 شرط هدف: {cond_str} {target_price:,.0f} تومان\n"
                            f"💰 قیمت فعلی شاخص بازار: {current_price:,.0f} تومان\n"
                            f"🌐 مرجع قیمت: {source}\n\n"
                            f"🔁 تناوب هشدار: روزی یک‌بار (گزارش ماندگاری در ناحیه شکست)"
                        )
                        try:
                            local_bot = ExtBot(token=bot_token)
                            async with local_bot:
                                await local_bot.send_message(chat_id=chat_id, text=message_text, parse_mode="Markdown")
                            await database.update_alarm_triggered_at(alarm_id, int(now), is_armed=0)
                            logger.info("Executed postponed daily notification for new calendar day on alarm %s", alarm_id)
                        except Exception as tg_err:
                            logger.error("Failed to send postponed daily alert: %s", tg_err)
                        continue
                    else:
                        continue

            # منطق سایر فرکانس‌ها (every_time, once)
            else:
                if not is_condition_met:
                    continue

                if not is_armed:
                    continue

                if frequency == "every_time":
                    if now - last_triggered < _EVERY_TIME_COOLDOWN_SECONDS:
                        continue

            # ---------------------------------------------------------------------------
            # لایه سوم: شلیک اعلان، ارسال تلگرام و به‌روزرسانی اتمیک دیتابیس
            # ---------------------------------------------------------------------------
            cond_str = _CONDITION_LABELS.get(condition, condition)

            message_text = (
                f"🔔 **هشدار قیمت تتر محقق شد!**\n"
                f"――――――――――――\n\n"
                f"🎯 شرط هدف: {cond_str} {target_price:,.0f} تومان\n"
                f"💰 قیمت فعلی شاخص بازار: {current_price:,.0f} تومان\n"
                f"🌐 مرجع قیمت: {source}\n\n"
            )

            if frequency == "once":
                message_text += "💡 این هشدار یک‌بار مصرف بود و اکنون غیرفعال شد. جای خالی برای شما آزاد گردید."
            else:
                message_text += f"🔁 تناوب هشدار: {frequency == 'daily' and 'روزی یک‌بار' or 'هر بار (با رعایت نوسان)'}"

            try:
                local_bot = ExtBot(token=bot_token)
                async with local_bot:
                    await local_bot.send_message(
                        chat_id=chat_id,
                        text=message_text,
                        parse_mode="Markdown"
                    )
                logger.info("Successfully dispatched alarm alert to chat_id=%s", chat_id)

                if frequency == "once":
                    await database.deactivate_alarm(alarm_id)
                else:
                    await database.update_alarm_triggered_at(alarm_id, int(now), is_armed=0)

            except Exception as tg_err:
                logger.error("Failed to send Telegram alert for alarm %s: %s", alarm_id, tg_err)

    except Exception as e:
        logger.error("Error in alarm evaluation process loop: %s", e, exc_info=True)