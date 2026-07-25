"""
Main entry point for FastAPI application, lifespan context, and background task management.
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app import database
from app.bot.handlers import build_application
from app.workers.market_poll import market_poll_loop, news_poll_loop

# تنظیم لایه لاگر پروژه
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# وهله‌سازی از ربات تلگرام بر اساس ساختار استاندارد PTB
bot_app = build_application()

# Global HTTP client session shared across the lifespan
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    مدیریت طول عمر برنامه FastAPI، کلاینت یکپارچه HTTP و پروسه‌های پس‌زمینه.
    """
    global http_client

    # ۱. مقداردهی اولیه به کلاینت دیتابیس
    await database.init_db()

    # ۲. ایجاد کلاینت یکپارچه HTTP برای شبکه
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
    )

    # ۳. شروع به کار ربات تلگرام (Polling Mode)
    await bot_app.initialize()

    if bot_app.job_queue:
        logger.info("🚀 JobQueue initialized successfully. Starting the queue...")
        await bot_app.job_queue.start()
    else:
        logger.warning("❌ JobQueue is NOT available. Check your dependencies.")

    await bot_app.updater.start_polling(drop_pending_updates=True)
    await bot_app.start()
    logger.info("Telegram bot initialized and started via POLLING mode safely.")

    # ۴. ثبت و اجرای تسک‌های پس‌زمینه ورکرها
    market_task = asyncio.create_task(market_poll_loop(bot_app))
    news_task = asyncio.create_task(news_poll_loop())

    yield

    # ۵. فرآیند اتمام کار تسک‌ها هنگام خاموش شدن سرور
    market_task.cancel()
    news_task.cancel()
    try:
        await asyncio.gather(market_task, news_task, return_exceptions=True)
    except Exception as e:
        logger.error("Error while canceling background polling tasks: %s", e)

    # ۶. اتمام کار ربات تلگرام
    await bot_app.updater.stop()
    await bot_app.stop()

    if bot_app.job_queue:
        await bot_app.job_queue.stop()

    await bot_app.shutdown()

    # ۷. بستن تمیز کلاینت شبکه HTTP
    if http_client is not None:
        await http_client.aclose()

    await database.close_db()
    logger.info("Application and resources stopped cleanly.")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}