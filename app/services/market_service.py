"""
Business logic layer for market data derived metrics and fetching.
Concurrent Price Aggregator for 4 exchanges (Wallex, Bitpin, Exir, Zipodo)
with Simple Average Index Price calculation and Dynamic Circuit Breaker / Cooldown handling.
"""
import asyncio
import logging
import statistics
import time
from typing import Optional, Union, TypedDict, List, Dict

import httpx

from app.database import get_db, insert_snapshot, insert_snapshots_batch
from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0)
TOLERANCE_SECONDS = 60 * 60  # +/- 60 minutes

# نگهداری آخرین قیمت مرجع معتبر جهت تعادل و پوشش قطعی کامل
_last_known_index_price: Optional[float] = None

# دیکشنری نگهداری زمان پایان کول‌داون برای هر سورس {source_name: expire_timestamp}
_cooldown_until: Dict[str, float] = {}


class MarketData(TypedDict):
    price: float
    volume: Optional[float]
    source: str
    change_24h: Optional[float]


class AggregatedMarketData(TypedDict):
    index_price: float
    sources_data: List[MarketData]
    timestamp: int


def _is_in_cooldown(source: str) -> bool:
    """Check if a given exchange source is currently in cooldown period."""
    expire_time = _cooldown_until.get(source, 0)
    now = time.time()
    if now < expire_time:
        remaining_minutes = int((expire_time - now) // 60)
        logger.debug("Source %s is in cooldown (%d minutes remaining). Skipping fetch.", source, remaining_minutes)
        return True
    return False


def _set_cooldown(source: str, duration_seconds: int) -> None:
    """Put an exchange source into cooldown for specified duration."""
    expire_time = time.time() + duration_seconds
    _cooldown_until[source] = expire_time
    logger.warning("Source %s put into cooldown for %d seconds (until timestamp %.0f).", source, duration_seconds, expire_time)


def _debug_dump(source: str, data) -> None:
    if isinstance(data, dict):
        preview = f"dict with keys: {list(data.keys())}"
    elif isinstance(data, list):
        sample = data[0] if data else None
        preview = f"list of {len(data)} items, first item: {sample!r}"[:500]
    else:
        preview = repr(data)[:500]
    logger.error("%s response shape did not match expectations -> %s", source, preview)


async def _fetch_wallex(client: httpx.AsyncClient) -> Optional[MarketData]:
    """Fetch price from Wallex API."""
    source_name = "wallex"
    if _is_in_cooldown(source_name):
        return None

    try:
        resp = await client.get(settings.wallex_api_url)
        resp.raise_for_status()
        data = resp.json()

        stats = data["result"]["symbols"]["USDTTMN"]["stats"]
        price = float(stats["lastPrice"])
        volume_tmn = stats.get("24h_tmnVolume")
        change_24h = stats.get("24h_ch")

        return MarketData(
            price=price,
            volume=float(volume_tmn) if volume_tmn is not None else None,
            source=source_name,
            change_24h=float(change_24h) if change_24h is not None else None,
        )

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            logger.error("Wallex hit rate limit (429). Triggering cooldown.")
            _set_cooldown(source_name, settings.market_cooldown_seconds)
        else:
            logger.warning("Wallex HTTP error %s: %s", exc.response.status_code, exc)
        return None
    except (KeyError, TypeError) as exc:
        _debug_dump(source_name, data if 'data' in locals() else str(exc))
        return None
    except Exception as exc:
        logger.warning("Failed to fetch Wallex: %s", exc)
        return None


async def _fetch_bitpin(client: httpx.AsyncClient) -> Optional[MarketData]:
    """Fetch price from Bitpin API."""
    source_name = "bitpin"
    if _is_in_cooldown(source_name):
        return None

    try:
        resp = await client.get(settings.bitpin_api_url)
        resp.raise_for_status()
        payload = resp.json()

        markets = payload["results"]
        market = next(
            m
            for m in markets
            if m["currency1"]["code"] == "USDT" and m["currency2"]["code"] == "IRT"
        )
        price = float(market["price"])

        volume = None
        order_book_info = market.get("order_book_info") or {}
        if order_book_info.get("value") is not None:
            volume = float(order_book_info["value"])

        change_24h = None
        price_info = market.get("price_info") or {}
        if price_info.get("change") is not None:
            change_24h = float(price_info["change"])

        return MarketData(
            price=price,
            volume=volume,
            source=source_name,
            change_24h=change_24h,
        )

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            logger.error("Bitpin hit rate limit (429). Triggering cooldown.")
            _set_cooldown(source_name, settings.market_cooldown_seconds)
        else:
            logger.warning("Bitpin HTTP error %s: %s", exc.response.status_code, exc)
        return None
    except (KeyError, TypeError, StopIteration) as exc:
        _debug_dump(source_name, payload if 'payload' in locals() else str(exc))
        return None
    except Exception as exc:
        logger.warning("Failed to fetch Bitpin: %s", exc)
        return None


async def _fetch_exir(client: httpx.AsyncClient) -> Optional[MarketData]:
    """Fetch price from Exir API (Converts Rials to Tomans if needed)."""
    source_name = "exir"
    if _is_in_cooldown(source_name):
        return None

    try:
        resp = await client.get(settings.exir_api_url)
        resp.raise_for_status()
        data = resp.json()

        # Exir returns prices in Rials -> divide by 10 for Tomans
        raw_price = float(data.get("last", 0) or data.get("close", 0))
        price = raw_price / 10.0 if raw_price > 200000 else raw_price
        volume = float(data["volume"]) if "volume" in data and data["volume"] else None
        change_24h = float(data["change"]) if "change" in data and data["change"] else None

        return MarketData(
            price=price,
            volume=volume,
            source=source_name,
            change_24h=change_24h,
        )

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            logger.error("Exir hit rate limit (429). Triggering cooldown.")
            _set_cooldown(source_name, settings.market_cooldown_seconds)
        else:
            logger.warning("Exir HTTP error %s: %s", exc.response.status_code, exc)
        return None
    except (KeyError, TypeError, ValueError) as exc:
        _debug_dump(source_name, data if 'data' in locals() else str(exc))
        return None
    except Exception as exc:
        logger.warning("Failed to fetch Exir: %s", exc)
        return None


async def _fetch_zipodo(client: httpx.AsyncClient) -> Optional[MarketData]:
    """Fetch price from Zipodo API."""
    source_name = "zipodo"
    if _is_in_cooldown(source_name):
        return None

    try:
        resp = await client.get(settings.zipodo_api_url)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list) and len(data) > 0:
            item = data[0]
        else:
            item = data

        price = float(item.get("price") or item.get("last_price") or item.get("val"))
        volume = float(item["volume"]) if item.get("volume") is not None else None
        change_24h = float(item["change_24h"]) if item.get("change_24h") is not None else None

        return MarketData(
            price=price,
            volume=volume,
            source=source_name,
            change_24h=change_24h,
        )

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            logger.error("Zipodo hit rate limit (429). Triggering cooldown.")
            _set_cooldown(source_name, settings.market_cooldown_seconds)
        else:
            logger.warning("Zipodo HTTP error %s: %s", exc.response.status_code, exc)
        return None
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        _debug_dump(source_name, data if 'data' in locals() else str(exc))
        return None
    except Exception as exc:
        logger.warning("Failed to fetch Zipodo: %s", exc)
        return None


async def fetch_all_sources(
    custom_client: Optional[httpx.AsyncClient] = None,
) -> List[MarketData]:
    """Fetch market data concurrently from active, non-cooldowned sources."""
    close_client = False
    if custom_client is None:
        client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
        close_client = True
    else:
        client = custom_client

    try:
        tasks = [
            _fetch_wallex(client),
            _fetch_bitpin(client),
            _fetch_exir(client),
            _fetch_zipodo(client),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if close_client:
            await client.aclose()

    valid_data: List[MarketData] = []
    for res in results:
        if isinstance(res, dict) and "price" in res and res["price"] > 0:
            valid_data.append(res)
        elif isinstance(res, Exception):
            logger.warning("Unhandled market source exception: %s", res)

    return valid_data


async def get_market_data(
    custom_client: Optional[httpx.AsyncClient] = None,
) -> Optional[AggregatedMarketData]:
    """
    Fetches prices from all available sources concurrently, computes the simple average (Index Price),
    and returns aggregated market state.
    """
    global _last_known_index_price

    sources_data = await fetch_all_sources(custom_client=custom_client)
    now = int(time.time())

    if not sources_data:
        logger.error("All market sources failed or are currently in cooldown.")
        if _last_known_index_price is not None:
            return AggregatedMarketData(
                index_price=_last_known_index_price,
                sources_data=[],
                timestamp=now,
            )
        return None

    prices = [d["price"] for d in sources_data]
    index_price = float(statistics.mean(prices))
    _last_known_index_price = index_price

    return AggregatedMarketData(
        index_price=index_price,
        sources_data=sources_data,
        timestamp=now,
    )


async def get_latest_snapshot() -> Optional[dict]:
    """Return the most recent market_snapshots row, prioritizing index_median."""
    conn = get_db()
    cursor = await conn.execute(
        "SELECT id, timestamp, price, volume, source "
        "FROM market_snapshots WHERE source = 'index_median' "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    if row is None:
        cursor = await conn.execute(
            "SELECT id, timestamp, price, volume, source "
            "FROM market_snapshots ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()

    await cursor.close()
    if row is None:
        return None

    return {
        "id": row[0],
        "timestamp": row[1],
        "price": row[2],
        "volume": row[3],
        "source": row[4],
    }


async def _find_closest_snapshot(target_timestamp: int) -> Optional[dict]:
    """Find the snapshot whose timestamp is closest to target_timestamp."""
    conn = get_db()
    cursor = await conn.execute(
        "SELECT id, timestamp, price, volume, source "
        "FROM market_snapshots WHERE source = 'index_median' "
        "ORDER BY ABS(timestamp - ?) LIMIT 1",
        (target_timestamp,),
    )
    row = await cursor.fetchone()
    if row is None:
        cursor = await conn.execute(
            "SELECT id, timestamp, price, volume, source "
            "FROM market_snapshots ORDER BY ABS(timestamp - ?) LIMIT 1",
            (target_timestamp,),
        )
        row = await cursor.fetchone()

    await cursor.close()
    if row is None:
        return None

    return {
        "id": row[0],
        "timestamp": row[1],
        "price": row[2],
        "volume": row[3],
        "source": row[4],
    }


async def get_price_change_24h(
    current: Optional[dict] = None, target_hours: float = 24.0
) -> Union[float, str]:
    """Compute 24h percentage price change for the index price vs target_hours ago."""
    if current is None:
        current = await get_latest_snapshot()
    if current is None or current.get("price") is None:
        return "N/A"

    target_timestamp = int(current["timestamp"] - target_hours * 3600)
    past = await _find_closest_snapshot(target_timestamp)
    if past is None:
        return "N/A"

    if abs(past["timestamp"] - target_timestamp) > TOLERANCE_SECONDS:
        return "N/A"

    if past.get("price") is None or past["price"] == 0:
        return "N/A"

    change_pct = ((current["price"] - past["price"]) / past["price"]) * 100
    return round(change_pct, 2)


async def get_volume_change(
    current: Optional[dict] = None, target_hours: float = 24.0
) -> Union[float, str]:
    """Compute rolling volume momentum as a percentage change vs target_hours ago."""
    if current is None:
        current = await get_latest_snapshot()
    if current is None or current.get("volume") is None:
        return "N/A"

    target_timestamp = int(current["timestamp"] - target_hours * 3600)
    past = await _find_closest_snapshot(target_timestamp)
    if past is None:
        return "N/A"

    if abs(past["timestamp"] - target_timestamp) > TOLERANCE_SECONDS:
        return "N/A"

    if past.get("volume") is None or past["volume"] == 0:
        return "N/A"

    change_pct = ((current["volume"] - past["volume"]) / past["volume"]) * 100
    return round(change_pct, 2)


async def record_snapshot(
    price: float,
    volume: Optional[float],
    source: str,
    change_24h: Optional[float] = None,
) -> None:
    """Convenience wrapper used by external callers to persist a single snapshot."""
    await insert_snapshot(
        timestamp=int(time.time()),
        price=price,
        volume=volume,
        source=source,
        change_24h=change_24h,
    )


async def record_aggregated_snapshots(aggregated_data: AggregatedMarketData) -> None:
    """Persist both individual exchange snapshot records and the index_median snapshot."""
    now = aggregated_data["timestamp"]
    index_price = aggregated_data["index_price"]
    sources_data = aggregated_data["sources_data"]

    snapshots_to_insert = []

    # 1. Record individual source snapshots
    for data in sources_data:
        snapshots_to_insert.append(
            {
                "timestamp": now,
                "price": data["price"],
                "volume": data["volume"],
                "source": data["source"],
                "change_24h": data["change_24h"],
            }
        )

    # 2. Record index price snapshot
    snapshots_to_insert.append(
        {
            "timestamp": now,
            "price": index_price,
            "volume": None,
            "source": "index_median",
            "change_24h": None,
        }
    )

    await insert_snapshots_batch(snapshots_to_insert)