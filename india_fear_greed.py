"""India Fear & Greed Index — computed from live public NSE APIs + news RSS.

Six equally-weighted components, each normalised to 0-100 (0 = extreme fear,
100 = extreme greed):

    1. VIX          — India VIX volatility (low = greed)
    2. PCR          — NIFTY put/call ratio (high = greed)
    3. FII flow     — foreign institutional net flow (buying = greed)
    4. Breadth      — share of stocks advancing / above their average
    5. Momentum     — NIFTY 50 vs its ~125-day average
    6. Sentiment    — VADER sentiment over Indian market news RSS feeds

NSE endpoints are aggressively bot-protected and frequently block datacenter
IPs (e.g. Render). Every component is therefore best-effort: on failure we fall
back to the last known good value (marked ``stale``) or a neutral default, and
never raise. The whole index always returns *something*.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import httpx
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger("fear_greed.india")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

NSE_BASE = "https://www.nseindia.com"
NSE_HOME = NSE_BASE + "/"
ALL_INDICES = NSE_BASE + "/api/allIndices"
# Legacy option-chain endpoint is deprecated (404); current flow is
# contract-info (for expiry list) -> option-chain-v3 (with that expiry).
OPTION_CHAIN_CONTRACT = NSE_BASE + "/api/option-chain-contract-info?symbol=NIFTY"
OPTION_CHAIN_V3 = NSE_BASE + "/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry="
FII_DII = NSE_BASE + "/api/fiidiiTradeReact"
INDEX_HISTORY = NSE_BASE + "/api/historicalOR/indicesHistory"

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_BASE + "/",
    "Connection": "keep-alive",
}

# Indian market news RSS feeds for sentiment.
RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://feeds.feedburner.com/ndtvprofit-latest",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/business.xml",
]

_analyzer = SentimentIntensityAnalyzer()

# Last known good component values, used when a live fetch fails.
# Seeded with neutral-ish defaults so a totally cold start still works.
_last_good: dict[str, dict] = {
    "vix": {"value": 14.0, "score": 50, "label": "Moderate Volatility"},
    "pcr": {"value": 0.95, "score": 50, "label": "Neutral"},
    "fii_flow": {"value": 0.0, "score": 50, "label": "Neutral"},
    "breadth": {"value": 50, "score": 50, "label": "Mixed"},
    "momentum": {"value": 1.0, "score": 50, "label": "At MA"},
    "sentiment": {"value": 0.0, "score": 50, "label": "Neutral"},
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _stale(component: str) -> dict:
    """Return the last known good value for a component, flagged stale."""
    out = dict(_last_good[component])
    out["stale"] = True
    return out


def _store(component: str, payload: dict) -> dict:
    """Remember a fresh component value as the new last-known-good."""
    _last_good[component] = {k: v for k, v in payload.items() if k != "stale"}
    return payload


async def _warm_cookies(client: httpx.AsyncClient) -> None:
    """Hit the NSE homepage to obtain the cookies its API endpoints require."""
    try:
        await client.get(NSE_HOME, headers=NSE_HEADERS, timeout=15.0)
    except httpx.HTTPError as exc:
        logger.warning("NSE cookie warm-up failed: %s", exc)


async def _nse_get_json(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        resp = await client.get(url, headers=NSE_HEADERS, timeout=15.0)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("NSE fetch failed for %s: %s", url, exc)
        return None


# --------------------------------------------------------------------------- #
# Component computations — each returns {value, score, label} or stale fallback
# --------------------------------------------------------------------------- #

def _vix_score(vix: float) -> int:
    # VIX < 12 -> 100 (greed), VIX > 30 -> 0 (fear), linear between.
    return round(_clamp((30.0 - vix) / (30.0 - 12.0) * 100.0))


def _vix_label(vix: float) -> str:
    if vix < 14:
        return "Low Volatility"
    if vix < 20:
        return "Moderate Volatility"
    return "High Volatility"


async def component_vix(client: httpx.AsyncClient, indices: Optional[dict]) -> dict:
    data = indices or await _nse_get_json(client, ALL_INDICES)
    if not data:
        return _stale("vix")
    vix = None
    for idx in data.get("data", []):
        name = (idx.get("index") or idx.get("indexSymbol") or "").upper()
        if "VIX" in name:
            vix = idx.get("last") or idx.get("lastPrice")
            break
    if vix is None:
        return _stale("vix")
    vix = float(vix)
    return _store("vix", {
        "value": round(vix, 2),
        "score": _vix_score(vix),
        "label": _vix_label(vix),
    })


def _pcr_score(pcr: float) -> int:
    # PCR < 0.7 -> fear (0), PCR > 1.2 -> greed (100), linear between.
    return round(_clamp((pcr - 0.7) / (1.2 - 0.7) * 100.0))


def _pcr_label(pcr: float) -> str:
    if pcr < 0.8:
        return "Bearish"
    if pcr <= 1.1:
        return "Neutral"
    return "Bullish"


async def component_pcr(client: httpx.AsyncClient) -> dict:
    # 1. Get the nearest NIFTY option expiry.
    info = await _nse_get_json(client, OPTION_CHAIN_CONTRACT)
    expiries = (info or {}).get("expiryDates") if isinstance(info, dict) else None
    if not expiries:
        return _stale("pcr")

    # 2. Pull the option chain for that expiry and aggregate open interest.
    url = OPTION_CHAIN_V3 + urllib.parse.quote(expiries[0])
    data = await _nse_get_json(client, url)
    if not isinstance(data, dict):
        return _stale("pcr")
    records = data.get("filtered") or data.get("records") or {}
    rows = records.get("data") if isinstance(records, dict) else None
    if not rows:
        return _stale("pcr")

    total_pe = sum((r.get("PE", {}) or {}).get("openInterest", 0) for r in rows)
    total_ce = sum((r.get("CE", {}) or {}).get("openInterest", 0) for r in rows)
    if not total_ce:
        return _stale("pcr")
    pcr = float(total_pe) / float(total_ce)
    return _store("pcr", {
        "value": round(pcr, 2),
        "score": _pcr_score(pcr),
        "label": _pcr_label(pcr),
    })


def _fii_score(net_cr: float) -> int:
    # Smoothly map net flow (in ₹ crore) to 0-100 around a ±2500cr reference.
    import math
    return round(_clamp(50.0 + 50.0 * math.tanh(net_cr / 2500.0)))


def _fii_label(net_cr: float) -> str:
    if net_cr > 250:
        return "Buying"
    if net_cr < -250:
        return "Selling"
    return "Neutral"


async def component_fii(client: httpx.AsyncClient) -> dict:
    data = await _nse_get_json(client, FII_DII)
    if not data or not isinstance(data, list):
        return _stale("fii_flow")
    net_cr = None
    for row in data:
        category = (row.get("category") or "").upper()
        if "FII" in category or "FPI" in category:
            net = row.get("netValue") or row.get("net")
            if net is not None:
                net_cr = float(str(net).replace(",", ""))
                break
    if net_cr is None:
        return _stale("fii_flow")
    return _store("fii_flow", {
        "value": round(net_cr, 2),
        "score": _fii_score(net_cr),
        "label": _fii_label(net_cr),
    })


def _breadth_label(pct: float) -> str:
    if pct >= 60:
        return "Bullish"
    if pct >= 40:
        return "Mixed"
    return "Bearish"


async def component_breadth(client: httpx.AsyncClient) -> dict:
    """Approximate breadth from NIFTY 500 advances vs declines.

    The ``allIndices`` payload exposes ``advances``/``declines``/``unchanged``
    per index, which is a solid live proxy for "% of stocks rising".
    """
    data = await _nse_get_json(client, ALL_INDICES)
    if not data:
        return _stale("breadth")
    target = None
    for idx in data.get("data", []):
        name = (idx.get("index") or "").upper()
        if name in ("NIFTY 500", "NIFTY 50"):
            target = idx
            if name == "NIFTY 500":
                break
    if not target:
        return _stale("breadth")
    try:
        adv = float(target.get("advances", 0))
        dec = float(target.get("declines", 0))
    except (TypeError, ValueError):
        return _stale("breadth")
    total = adv + dec
    if total <= 0:
        return _stale("breadth")
    pct = adv / total * 100.0
    return _store("breadth", {
        "value": round(pct),
        "score": round(_clamp(pct)),
        "label": _breadth_label(pct),
    })


def _momentum_score(ratio: float) -> int:
    # ratio = current / 125-day average. 1.0 -> 50, scaled ±10% -> full range.
    return round(_clamp(50.0 + (ratio - 1.0) * 500.0))


def _momentum_label(ratio: float) -> str:
    if ratio >= 1.005:
        return "Above MA"
    if ratio <= 0.995:
        return "Below MA"
    return "At MA"


async def component_momentum(client: httpx.AsyncClient, indices: Optional[dict]) -> dict:
    """NIFTY 50 spot vs an approximate 125-day average.

    We approximate the 125-day average from the index's open/previous-close and
    year range when a true historical series is unavailable, then refine with
    the historical endpoint if it responds.
    """
    data = indices or await _nse_get_json(client, ALL_INDICES)
    if not data:
        return _stale("momentum")
    nifty = None
    for idx in data.get("data", []):
        if (idx.get("index") or "").upper() == "NIFTY 50":
            nifty = idx
            break
    if not nifty:
        return _stale("momentum")
    try:
        current = float(nifty.get("last") or nifty.get("lastPrice"))
    except (TypeError, ValueError):
        return _stale("momentum")

    ma = await _nifty_ma125(client)
    if ma is None:
        # Proxy: midpoint of year high/low as a coarse long-run average.
        try:
            yh = float(nifty.get("yearHigh"))
            yl = float(nifty.get("yearLow"))
            ma = (yh + yl) / 2.0
        except (TypeError, ValueError):
            return _stale("momentum")
    if not ma:
        return _stale("momentum")

    ratio = current / ma
    return _store("momentum", {
        "value": round(ratio, 2),
        "score": _momentum_score(ratio),
        "label": _momentum_label(ratio),
    })


async def _nifty_ma125(client: httpx.AsyncClient) -> Optional[float]:
    """Best-effort ~125-day average close of NIFTY 50 from NSE history.

    Returns ``None`` on any failure so the caller can fall back to a proxy.
    """
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=250)
    url = (
        INDEX_HISTORY
        + "?indexType=" + urllib.parse.quote("NIFTY 50")
        + "&from=" + frm.strftime("%d-%m-%Y")
        + "&to=" + to.strftime("%d-%m-%Y")
    )
    data = await _nse_get_json(client, url)
    if not isinstance(data, dict):
        return None
    rows = data.get("data")
    if not isinstance(rows, list):
        return None
    try:
        closes = [
            float(r["EOD_CLOSE_INDEX_VAL"])
            for r in rows
            if isinstance(r, dict) and r.get("EOD_CLOSE_INDEX_VAL") is not None
        ]
    except (TypeError, ValueError):
        return None
    closes = closes[-125:]
    if len(closes) >= 20:
        return sum(closes) / len(closes)
    return None


def _sentiment_label(compound: float) -> str:
    if compound >= 0.5:
        return "Very Positive"
    if compound >= 0.15:
        return "Positive"
    if compound > 0.05:
        return "Slightly Positive"
    if compound >= -0.05:
        return "Neutral"
    if compound > -0.15:
        return "Slightly Negative"
    if compound > -0.5:
        return "Negative"
    return "Very Negative"


def _parse_feed(url: str) -> list[str]:
    """Blocking RSS parse — run in a thread by the caller."""
    try:
        parsed = feedparser.parse(url)
        headlines = []
        for entry in parsed.entries[:25]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            text = (title + ". " + summary).strip()
            if text:
                headlines.append(text)
        return headlines
    except Exception as exc:  # feedparser is liberal; guard anything
        logger.warning("RSS parse failed for %s: %s", url, exc)
        return []


async def component_sentiment() -> dict:
    headlines: list[str] = []
    results = await asyncio.gather(
        *(asyncio.to_thread(_parse_feed, url) for url in RSS_FEEDS),
        return_exceptions=True,
    )
    for res in results:
        if isinstance(res, list):
            headlines.extend(res)

    if not headlines:
        return _stale("sentiment")

    scores = [_analyzer.polarity_scores(h)["compound"] for h in headlines]
    compound = sum(scores) / len(scores)
    return _store("sentiment", {
        "value": round(compound, 2),
        "score": round(_clamp(50.0 + compound * 50.0)),
        "label": _sentiment_label(compound),
    })


# --------------------------------------------------------------------------- #
# Top-level index
# --------------------------------------------------------------------------- #

def _index_label(score: float) -> str:
    if score <= 25:
        return "Extreme Fear"
    if score <= 45:
        return "Fear"
    if score <= 55:
        return "Neutral"
    if score <= 75:
        return "Greed"
    return "Extreme Greed"


async def compute_india_fear_greed(
    client: httpx.AsyncClient,
    previous_score: Optional[int] = None,
) -> dict:
    """Compute the full India Fear & Greed block.

    Never raises: any component failure degrades to its last known value.
    """
    await _warm_cookies(client)

    # Fetch allIndices once and share it across VIX / breadth / momentum.
    indices = await _nse_get_json(client, ALL_INDICES)

    vix, pcr, fii, breadth, momentum, sentiment = await asyncio.gather(
        component_vix(client, indices),
        component_pcr(client),
        component_fii(client),
        component_breadth(client),
        component_momentum(client, indices),
        component_sentiment(),
    )

    components = {
        "vix": vix,
        "pcr": pcr,
        "fii_flow": fii,
        "breadth": breadth,
        "momentum": momentum,
        "sentiment": sentiment,
    }

    score = round(sum(c["score"] for c in components.values()) / len(components))

    direction = "flat"
    if previous_score is not None:
        if score > previous_score:
            direction = "up"
        elif score < previous_score:
            direction = "down"

    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "score": score,
        "label": _index_label(score),
        "direction": direction,
        "previous_score": previous_score,
        "date": now.date().isoformat(),
        "last_updated": now.isoformat(),
        "components": components,
    }
