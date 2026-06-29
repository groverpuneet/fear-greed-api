# Fear & Greed API

A single, standalone **FastAPI** microservice that serves a clean JSON
Fear & Greed Index for **India** and the **United States**.

> **🟢 Live:** https://fear-greed-api-3x70.onrender.com
> &nbsp;·&nbsp; [API](https://fear-greed-api-3x70.onrender.com/api/fear-greed)
> &nbsp;·&nbsp; [Gauges](https://fear-greed-api-3x70.onrender.com/)
> &nbsp;·&nbsp; [Health](https://fear-greed-api-3x70.onrender.com/health)

- 🇮🇳 **India** — computed live from public **NSE** APIs + news sentiment (6 equal-weight components).
- 🇺🇸 **US** — fetched from **CNN's** free public Fear & Greed API (CNN does the computation).
- No database. No authentication. No personal data.
- In-memory cache for **1 hour** to avoid rate limiting.
- Designed to be **always on** on the **Render.com free tier**.

---

## Endpoints

| Method | Path                | Description                                  |
|--------|---------------------|----------------------------------------------|
| `GET`  | `/api/fear-greed`   | India + US fear & greed as JSON              |
| `GET`  | `/health`           | Liveness probe `{"status":"ok", ...}`        |
| `GET`  | `/`                 | HTML page with two live gauges               |

### Example: `GET /api/fear-greed`

```json
{
  "india": {
    "score": 66,
    "label": "Greed",
    "direction": "up",
    "previous_score": 64,
    "date": "2026-06-29",
    "last_updated": "2026-06-29T10:30:00+00:00",
    "components": {
      "vix":       {"value": 13.05, "score": 72, "label": "Low Volatility"},
      "pcr":       {"value": 1.06,  "score": 58, "label": "Neutral"},
      "fii_flow":  {"value": 383.76,"score": 65, "label": "Buying"},
      "breadth":   {"value": 67,    "score": 67, "label": "Bullish"},
      "momentum":  {"value": 1.02,  "score": 62, "label": "Above MA"},
      "sentiment": {"value": 0.12,  "score": 56, "label": "Slightly Positive"}
    }
  },
  "us": {
    "score": 25,
    "label": "Extreme Fear",
    "direction": "down",
    "previous_score": 28,
    "date": "2026-06-29",
    "last_updated": "2026-06-29T10:30:00+00:00"
  },
  "cached": true,
  "cache_expires_in_seconds": 3420
}
```

---

## How the India index is computed

Six components, each normalised to **0–100** (0 = extreme fear, 100 = extreme greed),
then averaged with equal weight:

| Component   | Source (public, no auth)                              | Greed when…            |
|-------------|-------------------------------------------------------|------------------------|
| VIX         | `nseindia.com/api/allIndices` (India VIX)             | VIX low (`<12` → 100)  |
| PCR         | `nseindia.com/api/option-chain-indices?symbol=NIFTY`  | PCR high (`>1.2`)       |
| FII flow    | `nseindia.com/api/fiidiiTradeReact`                   | Net buying             |
| Breadth     | `nseindia.com/api/allIndices` (advances/declines)     | More advancers         |
| Momentum    | `nseindia.com/api/allIndices` (NIFTY 50 vs ~125-day)  | Above moving average   |
| Sentiment   | ET / NDTV Profit / Moneycontrol RSS + **VADER**       | Positive headlines     |

Overall label buckets: `0–25` Extreme Fear · `26–45` Fear · `46–55` Neutral ·
`56–75` Greed · `76–100` Extreme Greed.

> **Note on NSE:** NSE's endpoints are bot-protected and often block datacenter
> IPs (including cloud hosts). Every component degrades gracefully — on failure
> it falls back to the last known good value, flagged `"stale": true`, and the
> service **never returns 500**. The US block returns `null` if CNN is down.

---

## Run locally

```bash
# 1. (optional) create a virtualenv
python3 -m venv .venv && source .venv/bin/activate

# 2. install deps
pip install -r requirements.txt

# 3. run with autoreload
uvicorn main:app --reload

# 4. test
curl http://localhost:8000/api/fear-greed
open http://localhost:8000/        # the gauge page
```

---

## Deploy to Render.com (free tier)

This repo ships a `render.yaml` (Infrastructure-as-Code) and a `Procfile`.

1. Push this repo to GitHub (e.g. `fear-greed-api`).
2. Go to **render.com → New → Web Service** and connect the repo.
   Render auto-detects `render.yaml`, or set manually:
   - **Environment:** Python
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free
   - **Health check path:** `/health`
3. Deploy. Render assigns a permanent URL — this service is live at
   **`https://fear-greed-api-3x70.onrender.com`**.
4. Verify:
   ```bash
   curl https://fear-greed-api-3x70.onrender.com/api/fear-greed
   ```

## iPhone widget

A Scriptable home-screen widget for both gauges lives in
[`scripts/iphone_widget.js`](scripts/iphone_widget.js) — its `WEBAPP_URL` is
already pointed at the live Render deployment. Setup steps:
[`scripts/IPHONE_WIDGET_SETUP.md`](scripts/IPHONE_WIDGET_SETUP.md).

> Free-tier services sleep after inactivity; the first request after idle may
> take ~30s to wake. The 1-hour cache keeps subsequent calls instant.

---

## Tech

FastAPI · Uvicorn · httpx · feedparser · vaderSentiment · python-dateutil.
No database, no secrets, no auth — just clean JSON.

## License

MIT
