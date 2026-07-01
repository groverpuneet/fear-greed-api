"""Persistence for the India Fear & Greed daily history via a secret GitHub Gist.

A free, no-database key-value store. Two environment variables configure it:

    GITHUB_GIST_TOKEN  — a Personal Access Token with ONLY the ``gist`` scope
    GITHUB_GIST_ID     — id of the gist holding ``india_fear_greed_history.json``

The gist file content is ``{"history": [{"date": "YYYY-MM-DD", "score": int}, ...]}``.

If either env var is missing, or GitHub is unreachable, we degrade gracefully to
an in-memory rolling history (the previous behaviour) and log a warning — the API
never fails because of persistence.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List

import httpx

logger = logging.getLogger("fear_greed.gist")

GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN")
GIST_ID = os.environ.get("GITHUB_GIST_ID")
GIST_FILENAME = "india_fear_greed_history.json"
GIST_API = "https://api.github.com/gists"
HISTORY_DAYS = 30

# In-memory mirror of the history. Newest entry last.
_history: List[Dict] = []


def enabled() -> bool:
    """True when both gist env vars are configured."""
    return bool(GIST_TOKEN and GIST_ID)


def get_history() -> List[Dict]:
    """Return a copy of the current history (safe to embed in responses)."""
    return list(_history)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fear-greed-api",
    }


def _apply(history) -> None:
    """Replace the in-memory history from raw gist data, sanitising + capping."""
    global _history
    clean: List[Dict] = []
    for p in history or []:
        if not isinstance(p, dict):
            continue
        date, score = p.get("date"), p.get("score")
        if date is None or score is None:
            continue
        try:
            clean.append({"date": str(date), "score": int(score)})
        except (TypeError, ValueError):
            continue
    _history = clean[-HISTORY_DAYS:]


def record_point(date: str, score: int) -> bool:
    """Append/update one point per calendar date, capped at ``HISTORY_DAYS``.

    Returns True when the history actually changed (so the caller can decide to
    persist), False when today's value was already recorded unchanged.
    """
    if not date:
        return False
    if _history and _history[-1].get("date") == date:
        if _history[-1].get("score") == score:
            return False
        _history[-1]["score"] = int(score)
        return True
    _history.append({"date": date, "score": int(score)})
    del _history[:-HISTORY_DAYS]
    return True


async def load(client: httpx.AsyncClient) -> None:
    """Load history from the gist into memory. Safe/no-op on any failure."""
    if not enabled():
        logger.info(
            "Gist persistence disabled (set GITHUB_GIST_TOKEN + GITHUB_GIST_ID "
            "to enable); using in-memory India history only"
        )
        return
    try:
        resp = await client.get(f"{GIST_API}/{GIST_ID}", headers=_headers(), timeout=15.0)
        resp.raise_for_status()
        files = resp.json().get("files", {}) or {}
        entry = files.get(GIST_FILENAME)
        if not entry:
            logger.warning("Gist %s has no %s file yet — starting empty", GIST_ID, GIST_FILENAME)
            return
        data = json.loads(entry.get("content") or "{}")
        _apply(data.get("history", []))
        logger.info("Loaded %d India history point(s) from gist", len(_history))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("Gist load failed (%s); using in-memory India history", exc)


async def save(client: httpx.AsyncClient) -> None:
    """Persist the in-memory history back to the gist. Safe/no-op on failure."""
    if not enabled():
        return
    body = {
        "files": {
            GIST_FILENAME: {"content": json.dumps({"history": _history}, indent=2)}
        }
    }
    try:
        resp = await client.patch(
            f"{GIST_API}/{GIST_ID}", headers=_headers(), json=body, timeout=15.0
        )
        resp.raise_for_status()
        logger.info("Saved %d India history point(s) to gist", len(_history))
    except httpx.HTTPError as exc:
        logger.warning("Gist save failed (%s); kept in-memory only", exc)
