"""
Background polling loops: market price ticks and RSS news checks.

Extracted from main.py. These are the two long-running asyncio.Task loops
that main.py's FastAPI lifespan spawns on startup and cancels on shutdown.
"""
import asyncio
import logging

from telegram.ext import Application

from app.bot.bot_state import poller_status
from app.config import settings
from app.services.alarm_service import evaluate_and_trigger_alarms
from app.services.market_service import get_market_data, record_aggregated_snapshots
from app.services.news_service import fetch_and_process_news

logger = logging.getLogger(__name__)


async def market_poll_loop(bot_app: Application) -> None:
    """
    چرخه اصلی پولینگ هم‌زمان بازار و محاسبه قیمت مرجع (هر ۱۰ ثانیه یک‌بار).
    """
    logger.info(
        "Starting market polling loop. Poll interval: %d seconds.",
        settings.market_poll_interval
    )

    while True:
        start_time = asyncio.get_event_loop().time()
        try:
            # دریافت اطلاعات مجتمع ۴ صرافی و محاسبه قیمت مرجع (Index Price)
            aggregated_data = await get_market_data()

            if aggregated_data is not None:
                index_price = aggregated_data["index_price"]
                sources_count = len(aggregated_data["sources_data"])

                # بروزرسانی آنی حافظه موقت (رم) برای اعتبارسنجی‌ها
                poller_status.last_price = index_price
                poller_status.last_source = "index_median"
                poller_status.last_market_success = start_time
                poller_status.last_market_error = None

                # ارزیابی فوری و شلیک هشدارهای زنده مارکت بر اساس قیمت مرجع میانگین
                bot_token = bot_app.bot.token
                await evaluate_and_trigger_alarms(
                    current_price=index_price,
                    source="شاخص قیمت تتر",
                    bot_token=bot_token,
                )

                # ذخیره‌سازی مستقیم در دیتابیس همراه با هر سیکل استعلام (هر ۱۰ ثانیه)
                await record_aggregated_snapshots(aggregated_data)
                logger.info(
                    "Recorded aggregated snapshots to DB: Index Price %.2f IRT from %d active sources",
                    index_price,
                    sources_count,
                )
            else:
                poller_status.last_market_error = "No valid data received from any source"

        except Exception as e:
            logger.error("Error in market polling cycle: %s", e, exc_info=True)
            poller_status.last_market_error = str(e)

        elapsed = asyncio.get_event_loop().time() - start_time
        sleep_time = max(0.1, settings.market_poll_interval - elapsed)
        await asyncio.sleep(sleep_time)


async def news_poll_loop() -> None:
    """
    چرخه بررسی اخبار روز تتر و ذخیره در سیستم.
    """
    logger.info("Starting news polling loop. Poll interval: %d seconds.", settings.rss_poll_interval)
    while True:
        start_time = asyncio.get_event_loop().time()
        try:
            # اجرای سرویس دریافت، ارزیابی و ذخیره‌سازی اخبار
            saved_count = await fetch_and_process_news()

            poller_status.last_news_success = start_time
            poller_status.last_news_error = None
            logger.info("News poll cycle finished successfully. Saved %d new items.", saved_count)

        except Exception as e:
            logger.error("Error in news polling cycle: %s", e, exc_info=True)
            poller_status.last_news_error = str(e)

        elapsed = asyncio.get_event_loop().time() - start_time
        sleep_time = max(1.0, settings.rss_poll_interval - elapsed)
        await asyncio.sleep(sleep_time)