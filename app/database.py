"""
aiosqlite connection lifecycle and schema management.
"""
import logging
import time
from datetime import datetime, date

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

_CREATE_MARKET_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  INTEGER NOT NULL,
    price      REAL NOT NULL,
    volume     REAL,
    source     TEXT NOT NULL,
    change_24h REAL
);
"""

_ALTER_MARKET_SNAPSHOTS_ADD_CHANGE_24H_SQL = """
ALTER TABLE market_snapshots ADD COLUMN change_24h REAL;
"""

_CREATE_TIMESTAMP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_market_snapshots_timestamp
ON market_snapshots (timestamp);
"""

_CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    chat_id    INTEGER PRIMARY KEY,
    first_name TEXT,
    created_at INTEGER NOT NULL
);
"""

_CREATE_ALARMS_SQL = """
CREATE TABLE IF NOT EXISTS alarms (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL,
    target_price      REAL NOT NULL,
    condition         TEXT NOT NULL,
    frequency         TEXT NOT NULL,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        INTEGER NOT NULL,
    last_triggered_at INTEGER NOT NULL DEFAULT 0,
    is_armed          INTEGER NOT NULL DEFAULT 1
);
"""

_ALTER_ALARMS_ADD_LAST_TRIGGERED_SQL = """
ALTER TABLE alarms ADD COLUMN last_triggered_at INTEGER NOT NULL DEFAULT 0;
"""

_CREATE_ALARMS_CHAT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_alarms_chat_id_active
ON alarms (chat_id, is_active);
"""

_CREATE_NEWS_SUBSCRIPTIONS_SQL = """
CREATE TABLE IF NOT EXISTS news_subscriptions (
    chat_id INTEGER NOT NULL,
    source  TEXT NOT NULL,
    PRIMARY KEY (chat_id, source)
);
"""

_CREATE_NEWS_FEED_SQL = """
CREATE TABLE IF NOT EXISTS news_feed (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    summary      TEXT,
    link         TEXT UNIQUE,
    source_name  TEXT NOT NULL,
    source_slug  TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT 'economic',
    score        INTEGER NOT NULL,
    published_at INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);
"""

_CREATE_NEWS_FEED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_news_feed_published_slug
ON news_feed (published_at DESC, source_slug);
"""

_connection: aiosqlite.Connection | None = None


async def init_db() -> aiosqlite.Connection:
    """Open the shared connection, apply pragmas, and ensure schema exists."""
    global _connection

    if _connection is not None:
        return _connection

    conn = await aiosqlite.connect(settings.database_path)

    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")

    await conn.execute(_CREATE_MARKET_SNAPSHOTS_SQL)
    await conn.execute(_CREATE_TIMESTAMP_INDEX_SQL)
    await conn.execute(_CREATE_USERS_SQL)
    await conn.execute(_CREATE_ALARMS_SQL)
    await conn.execute(_CREATE_ALARMS_CHAT_INDEX_SQL)
    await conn.execute(_CREATE_NEWS_SUBSCRIPTIONS_SQL)
    await conn.execute(_CREATE_NEWS_FEED_SQL)
    await conn.execute(_CREATE_NEWS_FEED_INDEX_SQL)

    try:
        await conn.execute(_ALTER_ALARMS_ADD_LAST_TRIGGERED_SQL)
        logger.info("Migration applied: added last_triggered_at to alarms")
    except Exception:
        pass

    try:
        await conn.execute(_ALTER_MARKET_SNAPSHOTS_ADD_CHANGE_24H_SQL)
        logger.info("Migration applied: added change_24h to market_snapshots")
    except Exception:
        pass

    try:
        await conn.execute("ALTER TABLE alarms ADD COLUMN is_armed INTEGER NOT NULL DEFAULT 1;")
        logger.info("Migration applied: added is_armed to alarms")
    except Exception:
        pass

    await conn.commit()

    _connection = conn
    logger.info("Database initialized at %s (WAL mode)", settings.database_path)
    return _connection


async def close_db() -> None:
    """Close the shared connection cleanly on shutdown."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None
        logger.info("Database connection closed")


def get_db() -> aiosqlite.Connection:
    """Return the shared connection. Raises if init_db() hasn't run yet."""
    if _connection is None:
        raise RuntimeError(
            "Database not initialized. Call init_db() during app startup "
            "before using get_db()."
        )
    return _connection


async def insert_snapshot(
    timestamp: int,
    price: float,
    volume: float | None,
    source: str,
    change_24h: float | None = None,
) -> None:
    conn = get_db()
    async with conn.execute(
        "INSERT INTO market_snapshots (timestamp, price, volume, source, change_24h) "
        "VALUES (?, ?, ?, ?, ?)",
        (timestamp, price, volume, source, change_24h),
    ):
        await conn.commit()


async def insert_snapshots_batch(snapshots: list[dict]) -> None:
    """Insert a batch of market snapshots in a single transaction."""
    if not snapshots:
        return
    conn = get_db()
    data_tuples = [
        (
            s["timestamp"],
            s["price"],
            s.get("volume"),
            s["source"],
            s.get("change_24h"),
        )
        for s in snapshots
    ]
    await conn.executemany(
        "INSERT INTO market_snapshots (timestamp, price, volume, source, change_24h) "
        "VALUES (?, ?, ?, ?, ?)",
        data_tuples,
    )
    await conn.commit()


async def get_latest_snapshots(limit: int = 2) -> list[dict]:
    conn = get_db()
    # Fetch latest snapshot records prioritizing the aggregated index price if available
    async with conn.execute(
        "SELECT timestamp, price, volume, source, change_24h FROM market_snapshots "
        "WHERE source = 'index_median' "
        "ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        async with conn.execute(
            "SELECT timestamp, price, volume, source, change_24h FROM market_snapshots "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        {
            "timestamp": r[0],
            "price": r[1],
            "volume": r[2],
            "source": r[3],
            "change_24h": r[4],
        }
        for r in rows
    ]


async def get_latest_tick_snapshots() -> list[dict]:
    """Fetch all snapshots belonging to the single most recent timestamp cycle."""
    conn = get_db()
    async with conn.execute("SELECT MAX(timestamp) FROM market_snapshots") as cursor:
        row = await cursor.fetchone()
        latest_ts = row[0] if row else None

    if not latest_ts:
        return []

    async with conn.execute(
        "SELECT timestamp, price, volume, source, change_24h "
        "FROM market_snapshots "
        "WHERE timestamp = ? "
        "ORDER BY source ASC",
        (latest_ts,),
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "timestamp": r[0],
            "price": r[1],
            "volume": r[2],
            "source": r[3],
            "change_24h": r[4],
        }
        for r in rows
    ]


async def get_latest_snapshot() -> dict | None:
    results = await get_latest_snapshots(limit=1)
    return results[0] if results else None


async def upsert_user(chat_id: int, first_name: str | None) -> None:
    conn = get_db()
    async with conn.execute(
        "INSERT INTO users (chat_id, first_name, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET first_name = excluded.first_name",
        (chat_id, first_name, int(time.time())),
    ):
        await conn.commit()


async def count_active_alarms(chat_id: int) -> int:
    conn = get_db()
    async with conn.execute(
        "SELECT COUNT(*) FROM alarms WHERE chat_id = ? AND is_active = 1",
        (chat_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0


async def insert_alarm(
    chat_id: int, target_price: float, condition: str, frequency: str
) -> int:
    conn = get_db()
    async with conn.execute(
        "INSERT INTO alarms (chat_id, target_price, condition, frequency, is_active, created_at, last_triggered_at, is_armed) "
        "VALUES (?, ?, ?, ?, 1, ?, 0, 1)",
        (chat_id, target_price, condition, frequency, int(time.time())),
    ) as cursor:
        await conn.commit()
        return cursor.lastrowid


async def insert_alarm_with_quota_check(
    chat_id: int, target_price: float, condition: str, frequency: str, max_limit: int = 3
) -> int:
    conn = get_db()
    query = """
    INSERT INTO alarms (chat_id, target_price, condition, frequency, is_active, created_at, last_triggered_at, is_armed)
    SELECT ?, ?, ?, ?, 1, ?, 0, 1
    WHERE (SELECT COUNT(*) FROM alarms WHERE chat_id = ? AND is_active = 1) < ?
    """
    async with conn.execute(
        query,
        (chat_id, target_price, condition, frequency, int(time.time()), chat_id, max_limit)
    ) as cursor:
        await conn.commit()
        return cursor.lastrowid if cursor.rowcount > 0 else 0


async def get_active_alarms() -> list[dict]:
    conn = get_db()
    async with conn.execute(
        "SELECT id, chat_id, target_price, condition, frequency, last_triggered_at, is_armed "
        "FROM alarms WHERE is_active = 1"
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "id": r[0],
            "chat_id": r[1],
            "target_price": r[2],
            "condition": r[3],
            "frequency": r[4],
            "last_triggered_at": r[5],
            "is_armed": r[6],
        }
        for r in rows
    ]


async def deactivate_alarm(alarm_id: int) -> None:
    conn = get_db()
    async with conn.execute(
        "UPDATE alarms SET is_active = 0 WHERE id = ?",
        (alarm_id,),
    ) as cursor:
        await conn.commit()


async def update_alarm_triggered_at(alarm_id: int, triggered_at: int, is_armed: int = 0) -> None:
    conn = get_db()
    async with conn.execute(
        "UPDATE alarms SET last_triggered_at = ?, is_armed = ? WHERE id = ?",
        (triggered_at, is_armed, alarm_id),
    ) as cursor:
        await conn.commit()


async def update_alarm_armed_status(alarm_id: int, is_armed: int) -> None:
    conn = get_db()
    async with conn.execute(
        "UPDATE alarms SET is_armed = ? WHERE id = ?",
        (is_armed, alarm_id),
    ) as cursor:
        await conn.commit()


def is_alarm_triggered_today(last_triggered_timestamp: int) -> bool:
    if not last_triggered_timestamp:
        return False
    return datetime.fromtimestamp(last_triggered_timestamp).date() == date.today()


async def get_user_news_sources(chat_id: int) -> list[str]:
    conn = get_db()
    async with conn.execute(
        "SELECT source FROM news_subscriptions WHERE chat_id = ?",
        (chat_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def toggle_news_source(chat_id: int, source: str) -> bool:
    conn = get_db()
    async with conn.execute(
        "SELECT 1 FROM news_subscriptions WHERE chat_id = ? AND source = ?",
        (chat_id, source),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        async with conn.execute(
            "INSERT INTO news_subscriptions (chat_id, source) VALUES (?, ?)",
            (chat_id, source),
        ):
            await conn.commit()
            return True
    else:
        async with conn.execute(
            "DELETE FROM news_subscriptions WHERE chat_id = ? AND source = ?",
            (chat_id, source),
        ):
            await conn.commit()
            return False


async def get_user_alarms(chat_id: int) -> list[dict]:
    conn = get_db()
    async with conn.execute(
        "SELECT id, chat_id, target_price, condition, frequency, is_active, "
        "created_at, last_triggered_at, is_armed FROM alarms "
        "WHERE chat_id = ? AND is_active = 1 ORDER BY created_at DESC",
        (chat_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "id": r[0],
            "chat_id": r[1],
            "target_price": r[2],
            "condition": r[3],
            "frequency": r[4],
            "is_active": bool(r[5]),
            "created_at": r[6],
            "last_triggered_at": r[7],
            "is_armed": r[8],
        }
        for r in rows
    ]


async def delete_alarm(alarm_id: int, chat_id: int) -> None:
    conn = get_db()
    async with conn.execute(
        "DELETE FROM alarms WHERE id = ? AND chat_id = ?",
        (alarm_id, chat_id),
    ):
        await conn.commit()


async def update_alarm_target_price(alarm_id: int, chat_id: int, target_price: float) -> None:
    conn = get_db()
    async with conn.execute(
        "UPDATE alarms SET target_price = ?, is_active = 1, last_triggered_at = 0, is_armed = 1 "
        "WHERE id = ? AND chat_id = ?",
        (target_price, alarm_id, chat_id),
    ):
        await conn.commit()


async def update_alarm_condition(alarm_id: int, chat_id: int, condition: str) -> None:
    conn = get_db()
    async with conn.execute(
        "UPDATE alarms SET condition = ?, is_active = 1, last_triggered_at = 0, is_armed = 1 "
        "WHERE id = ? AND chat_id = ?",
        (condition, alarm_id, chat_id),
    ):
        await conn.commit()


async def update_alarm_frequency(alarm_id: int, chat_id: int, frequency: str) -> None:
    conn = get_db()
    async with conn.execute(
        "UPDATE alarms SET frequency = ?, is_active = 1, last_triggered_at = 0 "
        "WHERE id = ? AND chat_id = ?",
        (frequency, alarm_id, chat_id),
    ):
        await conn.commit()


async def get_alarm_by_id(alarm_id: int, chat_id: int) -> dict | None:
    conn = get_db()
    async with conn.execute(
        "SELECT id, chat_id, target_price, condition, frequency, is_active, "
        "created_at, last_triggered_at, is_armed FROM alarms "
        "WHERE id = ? AND chat_id = ?",
        (alarm_id, chat_id),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    return {
        "id": row[0],
        "chat_id": row[1],
        "target_price": row[2],
        "condition": row[3],
        "frequency": row[4],
        "is_active": bool(row[5]),
        "created_at": row[6],
        "last_triggered_at": row[7],
        "is_armed": row[8],
    }


async def insert_news_item(
    title: str,
    summary: str,
    link: str,
    source_name: str,
    source_slug: str,
    category: str,
    score: int,
    published_at: int,
) -> bool:
    conn = get_db()
    now = int(time.time())
    query = """
    INSERT OR IGNORE INTO news_feed 
    (title, summary, link, source_name, source_slug, category, score, published_at, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    async with conn.execute(
        query,
        (title, summary, link, source_name, source_slug, category, score, published_at, now),
    ) as cursor:
        await conn.commit()
        return cursor.rowcount > 0


async def get_filtered_news(
    chat_id: int,
    page: int = 1,
    page_size: int = 5,
    ignore_user_sources: bool = False,
) -> tuple[list[dict], int]:
    conn = get_db()
    cutoff_timestamp = int(time.time()) - (24 * 3600)
    offset = (page - 1) * page_size

    if ignore_user_sources:
        where_clause = "WHERE published_at >= ?"
        params = [cutoff_timestamp]
    else:
        user_sources = await get_user_news_sources(chat_id)
        if not user_sources:
            return [], 0

        placeholders = ",".join("?" for _ in user_sources)
        where_clause = f"WHERE published_at >= ? AND source_slug IN ({placeholders})"
        params = [cutoff_timestamp] + user_sources

    count_query = f"SELECT COUNT(*) FROM news_feed {where_clause}"
    async with conn.execute(count_query, params) as cursor:
        row = await cursor.fetchone()
        total_count = row[0] if row else 0

    if total_count == 0:
        return [], 0

    fetch_query = f"""
    SELECT id, title, summary, link, source_name, source_slug, category, score, published_at
    FROM news_feed
    {where_clause}
    ORDER BY published_at DESC
    LIMIT ? OFFSET ?
    """
    fetch_params = params + [page_size, offset]

    async with conn.execute(fetch_query, fetch_params) as cursor:
        rows = await cursor.fetchall()

    news_items = [
        {
            "id": r[0],
            "title": r[1],
            "summary": r[2],
            "link": r[3],
            "source_name": r[4],
            "source_slug": r[5],
            "category": r[6],
            "score": r[7],
            "published_at": r[8],
        }
        for r in rows
    ]

    return news_items, total_count


async def purge_expired_news(ttl_hours: int = 24) -> int:
    conn = get_db()
    cutoff_timestamp = int(time.time()) - (ttl_hours * 3600)

    async with conn.execute(
        "DELETE FROM news_feed WHERE published_at < ?",
        (cutoff_timestamp,),
    ) as cursor:
        await conn.commit()
        deleted_count = cursor.rowcount
        if deleted_count > 0:
            logger.info("Purged %d expired news items older than %d hours.", deleted_count, ttl_hours)
        return deleted_count