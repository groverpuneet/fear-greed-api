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

    # India has no public historical Fear & Greed source and we keep no database,
    # so we accumulate a rolling 30-day history in memory (one point per day).
    # This builds up over time and resets if the instance restarts.
    if india is not None and india.get("score") is not None:
        _record_india_history(india.get("date"), india["score"])
        india["history"] = list(_india_history)

    return {"india": india, "us": us}


# Rolling in-memory daily history for India: [{date, score}], newest last.
_india_history: list = []
_INDIA_HISTORY_DAYS = 30


def _record_india_history(date: Optional[str], score: int) -> None:
    """Append today's India score, one entry per calendar date, capped at 30."""
    if not date:
        return
    if _india_history and _india_history[-1].get("date") == date:
        _india_history[-1]["score"] = score  # same day → keep latest value
    else:
        _india_history.append({"date": date, "score": score})
    del _india_history[:-_INDIA_HISTORY_DAYS]


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


async def _get_result(force: bool = False) -> dict:
    """Return cached result if fresh, else rebuild. Returns the full response.

    When ``force`` is True the cache is bypassed: we always fetch fresh data and
    overwrite the cache with it.
    """
    now = time.time()
    age = now - _cache["stored_at"]

    if not force and _cache["payload"] is not None and age < CACHE_TTL_SECONDS:
        return _shape_response(_cache["payload"], cached=True, age=age)

    async with _cache_lock:
        # Re-check after acquiring the lock (another request may have refreshed).
        now = time.time()
        age = now - _cache["stored_at"]
        if not force and _cache["payload"] is not None and age < CACHE_TTL_SECONDS:
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
async def api_fear_greed(refresh: bool = False) -> JSONResponse:
    try:
        result = await _get_result(force=refresh)
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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta http-equiv="refresh" content="300" />
<meta name="theme-color" content="#0b0e14" />
<title>Fear &amp; Greed Index</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0b0e14; color: #e6e6e6; min-height: 100vh;
    padding: 1.25rem 1rem calc(2rem + env(safe-area-inset-bottom));
  }
  .wrap { max-width: 520px; margin: 0 auto; }
  h1 { font-weight: 600; letter-spacing: .3px; font-size: 1.35rem; text-align: center; margin: .2rem 0 1rem; }
  .controls { display: flex; align-items: center; justify-content: center; gap: .6rem; margin-bottom: 1.25rem; }
  button#refresh {
    background: #1c2533; color: #e6e6e6; border: 1px solid #2c3850; border-radius: 10px;
    padding: .6rem 1.1rem; font-size: .95rem; font-weight: 600; cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  button#refresh:active { background: #243047; }
  button#refresh:disabled { opacity: .55; }
  .spinner { width: 18px; height: 18px; border: 2px solid #2c3850; border-top-color: #9acd32; border-radius: 50%; display: inline-block; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .card {
    background: #151a23; border: 1px solid #232a36; border-radius: 16px;
    padding: 1.25rem 1.25rem 1rem; margin-bottom: 1.25rem;
    box-shadow: 0 8px 30px rgba(0,0,0,.35);
  }
  .card h2 { margin: 0 0 .9rem; font-size: 1.05rem; color: #cbd3df; font-weight: 600; text-align: center; }
  .top { display: flex; align-items: center; gap: 1rem; }
  .score { font-size: 2.7rem; font-weight: 800; line-height: 1; min-width: 86px; text-align: center; }
  .top-r { flex: 1; }
  .label { font-size: 1.15rem; font-weight: 700; }
  .dir { font-size: .85rem; color: #9aa4b2; margin-top: .15rem; }
  .arrow-up { color: #2ecc71; } .arrow-down { color: #e74c3c; } .arrow-flat { color: #95a5a6; }
  .bar { height: 8px; border-radius: 5px; background: linear-gradient(90deg,#e74c3c,#e67e22,#f1c40f,#9acd32,#2ecc71); position: relative; margin: .9rem 0 .3rem; }
  .bar i { position:absolute; top:-4px; width:16px; height:16px; border-radius:50%; background:#fff; transform:translateX(-50%); box-shadow:0 0 0 2px #151a23; }
  .updated { color: #79828f; font-size: .78rem; margin-top: .6rem; }
  .spark { margin-top: .8rem; }
  .spark svg { display: block; width: 100%; height: 64px; }
  .spark-row { display: flex; justify-content: space-between; font-size: .72rem; color: #9aa4b2; }
  .spark-x { display: flex; justify-content: space-between; font-size: .68rem; color: #59606b; margin-top: .15rem; }
  .spark-empty { font-size: .74rem; color: #59606b; padding: .4rem 0; text-align: center; }
  details { margin-top: .9rem; border-top: 1px solid #232a36; padding-top: .4rem; }
  summary { cursor: pointer; list-style: none; color: #9acd32; font-size: .9rem; font-weight: 600; padding: .35rem 0; -webkit-tap-highlight-color: transparent; }
  summary::-webkit-details-marker { display: none; }
  summary::after { content: " ▾"; color: #59606b; }
  details[open] summary::after { content: " ▴"; }
  .comp { margin: .7rem 0; }
  .comp-top { display: flex; justify-content: space-between; font-size: .9rem; }
  .comp-name { color: #d3dae5; font-weight: 600; }
  .comp-val { color: #e6e6e6; font-variant-numeric: tabular-nums; }
  .pbar { height: 6px; border-radius: 4px; background: #222a36; margin: .35rem 0; overflow: hidden; }
  .pfill { height: 100%; border-radius: 4px; }
  .comp-exp { font-size: .76rem; color: #8b95a3; }
  .verdict { font-weight: 700; }
  .stale { color: #e0a030; font-size: .7rem; }
  .footer { color: #59606b; font-size: .76rem; text-align:center; margin-top: 1rem; line-height: 1.5; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Fear &amp; Greed Index</h1>
  <div class="controls">
    <button id="refresh" onclick="doRefresh()">⟳ Refresh Now</button>
    <span id="spinner" class="spinner" style="display:none"></span>
  </div>

  <div class="card" id="india-card">
    <h2>🇮🇳 India</h2>
    <div class="top">
      <div class="score" id="india-score">–</div>
      <div class="top-r">
        <div class="label" id="india-label">Loading…</div>
        <div class="dir" id="india-dir"></div>
      </div>
    </div>
    <div class="bar"><i id="india-pin" style="left:50%"></i></div>
    <div class="updated" id="india-updated"></div>
    <div class="spark" id="india-spark"></div>
    <details id="india-details">
      <summary>📊 Component breakdown</summary>
      <div id="india-components"></div>
    </details>
  </div>

  <div class="card" id="us-card">
    <h2>🇺🇸 United States</h2>
    <div class="top">
      <div class="score" id="us-score">–</div>
      <div class="top-r">
        <div class="label" id="us-label">Loading…</div>
        <div class="dir" id="us-dir"></div>
      </div>
    </div>
    <div class="bar"><i id="us-pin" style="left:50%"></i></div>
    <div class="updated" id="us-updated"></div>
    <div class="spark" id="us-spark"></div>
    <details id="us-details">
      <summary>📊 Component breakdown (CNN)</summary>
      <div id="us-components"></div>
    </details>
  </div>

  <div class="footer">
    Data: NSE (India) &amp; CNN (US). Cached up to 1 hour; page auto-refreshes every 5 min.<br/>
    Not investment advice. · <span id="cache-info"></span>
  </div>
</div>
<script>
function color(s){ if(s==null)return '#7f8c8d'; if(s<=25)return '#e74c3c'; if(s<=45)return '#e67e22'; if(s<=55)return '#f1c40f'; if(s<=75)return '#9acd32'; return '#2ecc71'; }
function verdict(s){ if(s>=56)return 'Greed'; if(s>=46)return 'Neutral'; return 'Fear'; }
function arrow(d){ if(d==='up')return '<span class="arrow-up">▲ up</span>'; if(d==='down')return '<span class="arrow-down">▼ down</span>'; return '<span class="arrow-flat">▬ flat</span>'; }
function fmtIST(iso){
  if(!iso) return '';
  const d=new Date(iso); if(isNaN(d)) return '';
  try{ return d.toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true})+' IST'; }
  catch(e){ return d.toLocaleString(); }
}
function fmtShort(s){ const d=new Date((s||'')+'T12:00:00'); if(isNaN(d)) return s||''; return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'}); }

// Build a simple no-library SVG sparkline from a [{date, score}] history.
// Line is colour-graded vertically (green high → red low); min/max + date range labelled.
function sparkline(id, history){
  if(!history || history.length < 2){
    return '<div class="spark-empty">📈 30-day trend builds up as data is collected.</div>';
  }
  const W=300, H=56, P=4;
  const ys = history.map(p=>p.score);
  const n = history.length;
  let min=Math.min(...ys), max=Math.max(...ys);
  const span = (max-min) || 1;
  const px = i => P + (i/(n-1))*(W-2*P);
  const py = v => P + (1-(v-min)/span)*(H-2*P);
  const pts = history.map((p,i)=>px(i).toFixed(1)+','+py(p.score).toFixed(1)).join(' ');
  const gid = 'grad-'+id;
  const last = history[n-1];
  let svg = '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'
    + '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'
    + '<stop offset="0%" stop-color="#2ecc71"/><stop offset="50%" stop-color="#f1c40f"/>'
    + '<stop offset="100%" stop-color="#e74c3c"/></linearGradient></defs>'
    + '<polyline fill="none" stroke="url(#'+gid+')" stroke-width="2.5" stroke-linejoin="round" '
    + 'stroke-linecap="round" vector-effect="non-scaling-stroke" points="'+pts+'"/>'
    + '<circle cx="'+px(n-1).toFixed(1)+'" cy="'+py(last.score).toFixed(1)+'" r="3" fill="'+color(last.score)+'"/>'
    + '</svg>';
  return '<div class="spark-row"><span>High '+max+'</span><span>Low '+min+'</span></div>'
    + svg
    + '<div class="spark-x"><span>'+fmtShort(history[0].date)+'</span>'
    + '<span>'+n+'-day trend</span><span>'+fmtShort(last.date)+'</span></div>';
}

// India component display metadata: emoji, label, value formatter, explanation.
const INDIA_META = {
  vix:       {emoji:'📊', name:'VIX',       explain:'lower VIX = calmer market = greed',     fmt:v=>(+v).toFixed(2)},
  pcr:       {emoji:'⚖️', name:'PCR',       explain:'ratio of puts to calls, ~1.0 = neutral', fmt:v=>(+v).toFixed(2)},
  fii_flow:  {emoji:'🏦', name:'FII Flow',  explain:'foreign funds buying India = bullish',    fmt:v=>(v>=0?'+':'−')+'₹'+Math.abs(Math.round(v))+'Cr'},
  breadth:   {emoji:'📈', name:'Breadth',   explain:'% of stocks advancing / above 50-day MA', fmt:v=>Math.round(v)+'%'},
  momentum:  {emoji:'🚀', name:'Momentum',  explain:'Nifty 50 vs its 125-day average',         fmt:v=>(+v).toFixed(2)+'×'},
  sentiment: {emoji:'📰', name:'Sentiment', explain:'average news sentiment today',            fmt:v=>(+v).toFixed(2)},
};
const INDIA_ORDER = ['vix','pcr','fii_flow','breadth','momentum','sentiment'];

function compBar(emoji,name,valStr,score,ownLabel,explain,stale){
  const v = verdict(score), c = color(score);
  return '<div class="comp">'
    + '<div class="comp-top"><span class="comp-name">'+emoji+' '+name+(stale?' <span class="stale">(stale)</span>':'')+'</span>'
    + '<span class="comp-val">'+valStr+'</span></div>'
    + '<div class="pbar"><div class="pfill" style="width:'+Math.max(2,Math.min(100,score))+'%;background:'+c+'"></div></div>'
    + '<div class="comp-exp"><span class="verdict" style="color:'+c+'">'+v+'</span> · score '+score
    + (ownLabel?' · '+ownLabel:'')+' — '+explain+'</div></div>';
}

function renderIndiaComponents(comps){
  if(!comps) return '';
  let html='';
  for(const k of INDIA_ORDER){
    const c=comps[k]; if(!c) continue;
    const m=INDIA_META[k];
    html += compBar(m.emoji, m.name, m.fmt(c.value), c.score, c.label, m.explain, c.stale);
  }
  return html;
}

function renderUsComponents(comps){
  if(!comps || !comps.length) return '<div class="comp-exp">Sub-components unavailable.</div>';
  return comps.map(c => compBar('📊', c.name, c.score, c.score, c.label, c.explain, false)).join('');
}

function render(id, data, kind){
  const sc=document.getElementById(id+'-score');
  if(!data){
    sc.textContent='–'; sc.style.color='#7f8c8d';
    document.getElementById(id+'-label').textContent='Unavailable';
    document.getElementById(id+'-dir').textContent='';
    document.getElementById(id+'-updated').textContent='';
    document.getElementById(id+'-spark').innerHTML='';
    document.getElementById(id+'-components').innerHTML='<div class="comp-exp">No data right now — try Refresh.</div>';
    return;
  }
  const s=data.score;
  sc.textContent=s; sc.style.color=color(s);
  document.getElementById(id+'-label').textContent=data.label;
  document.getElementById(id+'-pin').style.left=Math.max(0,Math.min(100,s))+'%';
  document.getElementById(id+'-dir').innerHTML=arrow(data.direction)+' from '+(data.previous_score==null?'–':data.previous_score)+' · '+(data.date||'');
  document.getElementById(id+'-updated').textContent='Last updated: '+fmtIST(data.last_updated);
  document.getElementById(id+'-spark').innerHTML=sparkline(id, data.history);
  document.getElementById(id+'-components').innerHTML =
    kind==='india' ? renderIndiaComponents(data.components) : renderUsComponents(data.components);
}

function paint(d){
  render('india', d.india, 'india');
  render('us', d.us, 'us');
  document.getElementById('cache-info').textContent =
    (d.cached?'served from cache':'freshly computed')+
    ' · next auto-refresh in '+Math.round((d.cache_expires_in_seconds||0)/60)+' min';
}

async function load(force){
  const btn=document.getElementById('refresh'), sp=document.getElementById('spinner');
  if(force){ btn.disabled=true; sp.style.display='inline-block'; }
  try{
    const r=await fetch('/api/fear-greed'+(force?'?refresh=true':''), {cache:'no-store'});
    paint(await r.json());
  }catch(e){
    document.getElementById('cache-info').textContent='Failed to load data — try again';
  }finally{
    if(force){ btn.disabled=false; sp.style.display='none'; }
  }
}
function doRefresh(){ load(true); }
load(false);
</script>
</body>
</html>"""
