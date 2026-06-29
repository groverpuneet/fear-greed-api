// Fear & Greed — iOS home-screen widget (Scriptable)
// ---------------------------------------------------------------------------
// Shows the India + US Fear & Greed Index from the fear-greed-api service.
//
// Setup: install "Scriptable" from the App Store, create a new script, paste
// this file, then add a Scriptable widget to your home screen and pick this
// script. See scripts/IPHONE_WIDGET_SETUP.md for full instructions.
//
// Widget sizes:
//   small  -> India only (or pass "us" as the widget parameter for US only)
//   medium -> India + US side by side
// ---------------------------------------------------------------------------

const WEBAPP_URL = "https://fear-greed-api-3x70.onrender.com";

// ---------------------------------------------------------------------------

const API = `${WEBAPP_URL}/api/fear-greed`;

async function fetchData() {
  const req = new Request(API);
  req.timeoutInterval = 30; // Render free tier can cold-start (~30s)
  return await req.loadJSON();
}

// Colour ramp: red (fear) -> yellow (neutral) -> green (greed).
function scoreColor(score) {
  if (score == null) return new Color("#7f8c8d");
  if (score <= 25) return new Color("#e74c3c");
  if (score <= 45) return new Color("#e67e22");
  if (score <= 55) return new Color("#f1c40f");
  if (score <= 75) return new Color("#9acd32");
  return new Color("#2ecc71");
}

function arrow(direction) {
  if (direction === "up") return "▲";
  if (direction === "down") return "▼";
  return "▬";
}

// Format an ISO timestamp as a local "29 Jun, 2:30 PM" string.
function formatStamp(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const df = new DateFormatter();
  df.dateFormat = "d MMM, h:mm a";
  return df.string(d);
}

// Render one market column (flag, score, label, direction) into a stack.
function renderMarket(stack, flag, market) {
  const col = stack.addStack();
  col.layoutVertically();
  col.centerAlignContent();

  const title = col.addText(flag);
  title.font = Font.systemFont(15);
  title.centerAlignText();
  col.addSpacer(4);

  if (!market) {
    const na = col.addText("—");
    na.font = Font.boldSystemFont(30);
    na.textColor = new Color("#7f8c8d");
    na.centerAlignText();
    const lbl = col.addText("unavailable");
    lbl.font = Font.systemFont(10);
    lbl.textColor = new Color("#7f8c8d");
    lbl.centerAlignText();
    return;
  }

  const score = col.addText(String(market.score));
  score.font = Font.boldSystemFont(34);
  score.textColor = scoreColor(market.score);
  score.centerAlignText();

  const label = col.addText(market.label || "");
  label.font = Font.mediumSystemFont(12);
  label.textColor = Color.white();
  label.centerAlignText();

  const prev = market.previous_score == null ? "–" : market.previous_score;
  const meta = col.addText(`${arrow(market.direction)} ${prev}`);
  meta.font = Font.systemFont(10);
  meta.textColor = new Color("#9aa4b2");
  meta.centerAlignText();
}

async function buildWidget() {
  const w = new ListWidget();
  w.backgroundColor = new Color("#0b0e14");
  w.setPadding(14, 14, 14, 14);

  let data;
  try {
    data = await fetchData();
  } catch (e) {
    const err = w.addText("Fear & Greed\nunreachable");
    err.font = Font.mediumSystemFont(14);
    err.textColor = new Color("#e74c3c");
    err.centerAlignText();
    return w;
  }

  const header = w.addText("Fear & Greed");
  header.font = Font.semiboldSystemFont(13);
  header.textColor = new Color("#9aa4b2");
  w.addSpacer(8);

  const family = config.widgetFamily || "medium";
  const param = (args.widgetParameter || "").toLowerCase();

  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  if (family === "small") {
    // One market only — US if explicitly requested, else India.
    if (param === "us") {
      renderMarket(row, "🇺🇸 US", data.us);
    } else {
      renderMarket(row, "🇮🇳 India", data.india);
    }
  } else {
    renderMarket(row, "🇮🇳 India", data.india);
    row.addSpacer();
    renderMarket(row, "🇺🇸 US", data.us);
  }

  w.addSpacer(8);

  // Full date + time of the last data refresh, e.g. "29 Jun, 2:30 PM".
  const updated = (data.india && data.india.last_updated) ||
                  (data.us && data.us.last_updated);
  const foot = w.addText(updated ? `Updated ${formatStamp(updated)}` : "Updated —");
  foot.font = Font.systemFont(9);
  foot.textColor = new Color("#79828f");
  foot.centerAlignText();

  // Tap hint — the whole widget opens the mobile details page.
  const hint = w.addText("Tap for details");
  hint.font = Font.systemFont(9);
  hint.textColor = new Color("#59606b");
  hint.centerAlignText();

  // Tapping the widget opens the mobile-friendly gauge page at the root URL.
  w.url = WEBAPP_URL;

  // Refresh roughly hourly to match the server cache.
  w.refreshAfterDate = new Date(Date.now() + 60 * 60 * 1000);
  return w;
}

const widget = await buildWidget();

// Guarantee the tap URL is set on the FINAL presented widget object,
// regardless of size or how it was built.
widget.url = WEBAPP_URL;

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentMedium();
}
Script.complete();
