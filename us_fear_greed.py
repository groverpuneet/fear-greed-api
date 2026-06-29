"""US Fear & Greed Index — fetched directly from CNN's free public API.

CNN already computes the index, so there is no local computation here.
We just fetch, extract the relevant fields, and shape them for our API.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("fear_greed.us")

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# CNN's endpoint rejects requests that do not look like a browser.
CNN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.cnn.com",
    "Referer": "https://www.cnn.com/",
}


# CNN's 7 sub-indicators, in display order, mapped to friendly names + a
# one-line plain-English explanation for the mobile page.
CNN_COMPONENTS = [
    ("market_momentum_sp500", "Market Momentum", "S&P 500 vs its 125-day average"),
    ("stock_price_strength", "Stock Price Strength", "52-week highs vs lows on the NYSE"),
    ("stock_price_breadth", "Stock Price Breadth", "advancing vs declining volume"),
    ("put_call_options", "Put/Call Ratio", "puts vs calls — hedging demand"),
    ("market_volatility_vix", "Market Volatility", "the VIX level (lower = calmer)"),
    ("safe_haven_demand", "Safe Haven Demand", "stocks vs bonds returns"),
    ("junk_bond_demand", "Junk Bond Demand", "risk appetite in credit markets"),
]


def _extract_components(data: dict) -> list:
    """Pull CNN's 7 sub-indicators into a uniform list for the API/page."""
    components = []
    for key, name, explain in CNN_COMPONENTS:
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        try:
            score = round(float(block.get("score")))
        except (TypeError, ValueError):
            continue
        rating = block.get("rating")
        label = rating.title() if isinstance(rating, str) else _label_from_score(score)
        components.append({
            "key": key,
            "name": name,
            "score": score,
            "label": label,
            "explain": explain,
        })
    return components


def _label_from_score(score: float) -> str:
    """Map CNN's 0-100 score onto a human label matching CNN's own buckets."""
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score < 55:
        return "Neutral"
    if score < 75:
        return "Greed"
    return "Extreme Greed"


def _date_from_timestamp(ts) -> Optional[str]:
    """Convert CNN's timestamp to an ISO date string.

    CNN currently returns an ISO 8601 string (e.g. ``2026-06-26T23:59:57+00:00``)
    but has historically used epoch milliseconds, so we handle both.
    """
    if ts is None:
        return None
    # ISO 8601 string form.
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return None
    # Epoch milliseconds form.
    try:
        return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


async def fetch_us_fear_greed(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch the US Fear & Greed index from CNN.

    Returns a dict shaped for the API response, or ``None`` if CNN is
    unreachable / returns unexpected data. The caller treats ``None`` as
    "no US data available" rather than failing the whole request.
    """
    try:
        resp = await client.get(CNN_URL, headers=CNN_HEADERS, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("CNN US fear & greed fetch failed: %s", exc)
        return None

    fg = data.get("fear_and_greed")
    if not isinstance(fg, dict):
        logger.warning("CNN response missing 'fear_and_greed' block")
        return None

    try:
        score = round(float(fg.get("score")))
    except (TypeError, ValueError):
        logger.warning("CNN response missing numeric score")
        return None

    previous_close = fg.get("previous_close")
    previous_score: Optional[int] = None
    if previous_close is not None:
        try:
            previous_score = round(float(previous_close))
        except (TypeError, ValueError):
            previous_score = None

    rating = fg.get("rating")
    label = rating.title() if isinstance(rating, str) else _label_from_score(score)

    direction = "flat"
    if previous_score is not None:
        if score > previous_score:
            direction = "up"
        elif score < previous_score:
            direction = "down"

    date = _date_from_timestamp(fg.get("timestamp"))

    return {
        "score": score,
        "label": label,
        "direction": direction,
        "previous_score": previous_score,
        "date": date,
        "last_updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "components": _extract_components(data),
    }
