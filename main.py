"""Fear & Greed API — standalone FastAPI microservice.

GET /api/fear-greed  -> India (computed from NSE) + US (CNN) fear & greed.
GET /health          -> liveness probe.
GET /                 -> simple HTML page with both gauges.

In-memory 60-minute cache. No database, no auth, no personal data.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from india_fear_greed import compute_india_fear_greed
from us_fear_greed import fetch_us_fear_greed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fear_greed.main")

CACHE_TTL_SECONDS = 60 * 60  # 1 hour

app = FastAPI(
    title="Fear & Greed API",
    description="India (NSE) + US (CNN) Fear & Greed Index. No database, no auth.",
    version="1.0.0",
)

# --------------------------------------------------------------------------- #
# In-memory cache
# --------------------------------------------------------------------------- #

_cache: dict = {"payload": None, "stored_at": 0.0}
_cache_lock = asyncio.Lock()
_http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup() -> None:
    global _http_client
    _http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=20.0,
        headers={"Accept-Encoding": "gzip, deflate"},
    )
    logger.info("Fear & Greed API started")


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http_client is not None:
        await _http_client.aclose()


async def _build_payload() -> dict:
    """Fetch + compute a fresh result. Never raises."""
    assert _http_client is not None

    prev = _cache.get("payload") or {}
    prev_india = (prev.get("india") or {}).get("score")
    prev_us = (prev.get("us") or {}).get("score")

    india, us = await asyncio.gather(
        _safe_india(prev_india),
        _safe_us(),
        return_exceptions=False,
    )

    # Wire US previous_score/direction from the prior cached US value when CNN
    # did not supply its own previous close.
    if us is not None and us.get("previous_score") is None and prev_us is not None:
        us["previous_score"] = prev_us
        if us["score"] > prev_us:
            us["direction"] = "up"
        elif us["score"] < prev_us:
            us["direction"] = "down"

    return {"india": india, "us": us}


async def _safe_india(prev_score: Optional[int]) -> dict:
    try:
        return await compute_india_fear_greed(_http_client, prev_score)
    except Exception:  # defensive: never let India break the response
        logger.exception("India computation crashed; returning neutral fallback")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return {
            "score": 50,
            "label": "Neutral",
            "direction": "flat",
            "previous_score": prev_score,
            "date": now.date().isoformat(),
            "last_updated": now.isoformat(),
            "components": {},
            "stale": True,
        }


async def _safe_us() -> Optional[dict]:
    try:
        return await fetch_us_fear_greed(_http_client)
    except Exception:
        logger.exception("US fetch crashed; returning null US block")
        return None


async def _get_result() -> dict:
    """Return cached result if fresh, else rebuild. Returns the full response."""
    now = time.time()
    age = now - _cache["stored_at"]

    if _cache["payload"] is not None and age < CACHE_TTL_SECONDS:
        return _shape_response(_cache["payload"], cached=True, age=age)

    async with _cache_lock:
        # Re-check after acquiring the lock (another request may have refreshed).
        now = time.time()
        age = now - _cache["stored_at"]
        if _cache["payload"] is not None and age < CACHE_TTL_SECONDS:
            return _shape_response(_cache["payload"], cached=True, age=age)

        logger.info("Cache miss — fetching fresh fear & greed data")
        payload = await _build_payload()
        _cache["payload"] = payload
        _cache["stored_at"] = time.time()
        return _shape_response(payload, cached=False, age=0.0)


def _shape_response(payload: dict, cached: bool, age: float) -> dict:
    expires_in = max(0, int(CACHE_TTL_SECONDS - age))
    return {
        "india": payload.get("india"),
        "us": payload.get("us"),
        "cached": cached,
        "cache_expires_in_seconds": expires_in,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/api/fear-greed")
async def api_fear_greed() -> JSONResponse:
    try:
        result = await _get_result()
        return JSONResponse(result)
    except Exception:
        # Absolute last resort — still return best available data, never 500.
        logger.exception("Unexpected error in /api/fear-greed")
        fallback = _cache.get("payload") or {"india": None, "us": None}
        return JSONResponse(
            {
                "india": fallback.get("india"),
                "us": fallback.get("us"),
                "cached": True,
                "cache_expires_in_seconds": 0,
            }
        )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


# --------------------------------------------------------------------------- #
# HTML page (two live gauges)
# --------------------------------------------------------------------------- #

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Fear &amp; Greed Index</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0b0e14; color: #e6e6e6; min-height: 100vh;
    display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
  }
  h1 { font-weight: 600; letter-spacing: .5px; margin: .2rem 0 1.5rem; }
  .cards { display: flex; gap: 1.5rem; flex-wrap: wrap; justify-content: center; }
  .card {
    background: #151a23; border: 1px solid #232a36; border-radius: 16px;
    padding: 1.5rem 2rem; width: 300px; text-align: center;
    box-shadow: 0 8px 30px rgba(0,0,0,.35);
  }
  .card h2 { margin: 0 0 1rem; font-size: 1.1rem; color: #9aa4b2; font-weight: 500; }
  .gauge { position: relative; width: 220px; height: 120px; margin: 0 auto; }
  .score { font-size: 3rem; font-weight: 700; line-height: 1; }
  .label { font-size: 1.15rem; margin-top: .4rem; font-weight: 600; }
  .meta { color: #79828f; font-size: .82rem; margin-top: .8rem; }
  .arrow-up { color: #2ecc71; } .arrow-down { color: #e74c3c; } .arrow-flat { color: #95a5a6; }
  table { width: 100%; margin-top: 1rem; border-collapse: collapse; font-size: .82rem; }
  td { padding: .25rem .2rem; color: #b6c0cd; text-align: left; }
  td.v { text-align: right; color: #e6e6e6; font-variant-numeric: tabular-nums; }
  .footer { margin-top: 2rem; color: #59606b; font-size: .78rem; text-align:center; }
  .bar { height: 6px; border-radius: 4px; background: linear-gradient(90deg,#e74c3c,#f1c40f,#2ecc71); position: relative; margin: .6rem 0; }
  .bar i { position:absolute; top:-3px; width:12px; height:12px; border-radius:50%; background:#fff; transform:translateX(-50%); box-shadow:0 0 0 2px #151a23; }
</style>
</head>
<body>
  <h1>Fear &amp; Greed Index</h1>
  <div class="cards">
    <div class="card" id="india">
      <h2>🇮🇳 India</h2>
      <div class="bar"><i id="india-pin" style="left:50%"></i></div>
      <div class="score" id="india-score">–</div>
      <div class="label" id="india-label">Loading…</div>
      <div class="meta" id="india-meta"></div>
      <table id="india-components"></table>
    </div>
    <div class="card" id="us">
      <h2>🇺🇸 United States</h2>
      <div class="bar"><i id="us-pin" style="left:50%"></i></div>
      <div class="score" id="us-score">–</div>
      <div class="label" id="us-label">Loading…</div>
      <div class="meta" id="us-meta"></div>
    </div>
  </div>
  <div class="footer">
    Data: NSE (India) &amp; CNN (US). Cached up to 1 hour. Not investment advice.<br/>
    <span id="cache-info"></span>
  </div>
<script>
function color(s){ if(s<=25)return '#e74c3c'; if(s<=45)return '#e67e22'; if(s<=55)return '#f1c40f'; if(s<=75)return '#9acd32'; return '#2ecc71'; }
function arrow(d){ if(d==='up')return '<span class="arrow-up">▲</span>'; if(d==='down')return '<span class="arrow-down">▼</span>'; return '<span class="arrow-flat">▬</span>'; }
function render(id, data){
  if(!data){ document.getElementById(id+'-label').textContent='Unavailable'; return; }
  const s=data.score;
  document.getElementById(id+'-score').textContent=s;
  document.getElementById(id+'-score').style.color=color(s);
  document.getElementById(id+'-label').textContent=data.label;
  document.getElementById(id+'-pin').style.left=Math.max(0,Math.min(100,s))+'%';
  let meta = arrow(data.direction)+' from '+(data.previous_score==null?'–':data.previous_score)+' · '+(data.date||'');
  document.getElementById(id+'-meta').innerHTML=meta;
  if(data.components){
    let html='';
    for(const [k,c] of Object.entries(data.components)){
      html += '<tr><td>'+k.replace('_',' ')+(c.stale?' *':'')+'</td><td class="v">'+c.value+'</td><td class="v" style="color:'+color(c.score)+'">'+c.score+'</td></tr>';
    }
    document.getElementById(id+'-components').innerHTML=html;
  }
}
async function load(){
  try{
    const r=await fetch('/api/fear-greed'); const d=await r.json();
    render('india', d.india); render('us', d.us);
    const lu = (d.india&&d.india.last_updated) || (d.us&&d.us.last_updated);
    let when = '';
    if(lu){ const t=new Date(lu); if(!isNaN(t)) when='Last updated '+t.toLocaleString(); }
    document.getElementById('cache-info').textContent =
      (when?when+' · ':'')+
      (d.cached?'served from cache':'freshly computed')+
      ' · refreshes in '+Math.round((d.cache_expires_in_seconds||0)/60)+' min';
  }catch(e){ document.getElementById('cache-info').textContent='Failed to load data'; }
}
load(); setInterval(load, 5*60*1000);
</script>
</body>
</html>"""
