# iPhone Widget Setup (Scriptable)

A home-screen widget that shows the **India + US Fear & Greed Index** from the
`fear-greed-api` service, powered by [Scriptable](https://scriptable.app).

**Live API:** https://fear-greed-api-3x70.onrender.com
**Widget script:** [`scripts/iphone_widget.js`](./iphone_widget.js)

---

## 1. Install Scriptable

Install **Scriptable** (free) from the App Store:
https://apps.apple.com/app/scriptable/id1405459188

## 2. Add the script

1. Open **Scriptable**.
2. Tap **+** (top-right) to create a new script.
3. Delete the placeholder content and **paste the entire contents** of
   [`scripts/iphone_widget.js`](./iphone_widget.js).
4. Rename it (tap the title) to e.g. **Fear & Greed**.
5. Tap **Done**. Tap the ▶︎ run button once to confirm it previews correctly.

> The `WEBAPP_URL` constant at the top is already set to the live Render
> deployment:
> ```js
> const WEBAPP_URL = "https://fear-greed-api-3x70.onrender.com";
> ```
> If you ever redeploy to a different URL, update this one line.

## 3. Add the widget to your home screen

1. Long-press an empty area of the home screen → tap **+** (top-left).
2. Search for **Scriptable** and choose a size:
   - **Small** — one market (India by default).
   - **Medium** — India **and** US side by side (recommended).
3. Tap **Add Widget**, then **long-press the widget → Edit Widget**.
4. Set **Script** to **Fear & Greed**.
5. (Optional) For a **Small** widget showing the **US** index, set the
   **Parameter** field to `us`.

## 4. Done

The widget shows each market's score (0–100), label (Extreme Fear → Extreme
Greed), and the direction arrow vs. the previous reading. It refreshes about
once per hour, matching the server's 1-hour cache.

---

## Reference

- `GET /api/fear-greed` — JSON consumed by the widget
- `GET /health` — liveness probe
- `GET /` — web page with both gauges (open the URL in a browser)

### Colour key

| Score  | Meaning        | Colour |
|--------|----------------|--------|
| 0–25   | Extreme Fear   | 🔴 red |
| 26–45  | Fear           | 🟠 orange |
| 46–55  | Neutral        | 🟡 yellow |
| 56–75  | Greed          | 🟢 yellow-green |
| 76–100 | Extreme Greed  | 🟢 green |

### Troubleshooting

- **"unreachable" / blank:** Render's free tier sleeps after inactivity; the
  first request can take ~30s to wake the service. Re-run the script or wait a
  moment and the widget will populate on its next refresh.
- **US shows "—":** CNN's API was momentarily unavailable; the India side is
  unaffected and US returns on the next refresh.
