import requests
import time
import threading
import json
import os
from datetime import datetime
from collections import deque
from flask import Flask, jsonify, render_template_string, request

# ================= Config =================

REFRESH_INTERVAL = 10
MAX_HOURS = 24
MAX_POINTS = int(3600 / REFRESH_INTERVAL * MAX_HOURS)

SIGNAL_CONFIG = {
    "funding_strong": -0.018,
    "funding_warn": -0.012,
    "oi_drop_pct": -0.06,        # 30 min
    "price_break_pct": -0.008,  # below VWAP
    "pullback_reject_pct": 0.004,
    "cooldown_sec": 1200,
}

SYMBOLS = ["RIVERUSDT", "HYPEUSDT", "BTCUSDT", "ETHUSDT"]

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# ================= App =================

app = Flask(__name__)
symbols_state = {}

# ================= Persistence =================

def log_file(symbol):
    return os.path.join(DATA_DIR, f"{symbol}.jsonl")


def load_history(symbol):
    state = {
        "ts": deque(maxlen=MAX_POINTS),
        "price_avg": deque(maxlen=MAX_POINTS),
        "funding_avg": deque(maxlen=MAX_POINTS),
        "oi_avg": deque(maxlen=MAX_POINTS),

        "price_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},
        "funding_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},
        "oi_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},

        "signals": deque(maxlen=300),
        "last_signal_ts": 0,
    }

    path = log_file(symbol)
    if not os.path.exists(path):
        return state

    try:
        with open(path) as f:
            for line in f:
                row = json.loads(line)
                snap = row["data"]
                ts = row.get("ts")

                prices = []
                fundings = []
                ois = []

                for ex, v in snap.items():
                    prices.append(v["price"])
                    fundings.append(v["funding"])
                    ois.append(v["oi"])

                    state["price_ex"][ex].append(v["price"])
                    state["funding_ex"][ex].append(v["funding"])
                    state["oi_ex"][ex].append(v["oi"])

                state["ts"].append(ts)
                state["price_avg"].append(mean(prices))
                state["funding_avg"].append(mean(fundings))
                state["oi_avg"].append(mean(ois))
    except Exception as e:
        print("load history error:", e)

    return state


def persist(symbol, snapshot):
    record = {
        "ts": datetime.utcnow().isoformat(),
        "symbol": symbol,
        "data": snapshot,
    }
    with open(log_file(symbol), "a") as f:
        f.write(json.dumps(record) + "\n")


# ================= Utils =================

def get_json(url):
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    return r.json()


def now_ts():
    return int(time.time())


def mean(xs):
    return sum(xs) / len(xs) if xs else 0


# ================= Exchange APIs =================

def binance_data(symbol):
    p = get_json(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}")
    oi = get_json(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}")
    return {
        "funding": float(p["lastFundingRate"]),
        "price": float(p["markPrice"]),
        "oi": float(oi["openInterest"]),
    }


def bybit_data(symbol):
    r = get_json(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}")
    item = r["result"]["list"][0]
    return {
        "funding": float(item["fundingRate"]),
        "price": float(item["markPrice"]),
        "oi": float(item["openInterest"]),
    }


def okx_data(symbol):
    inst = symbol.replace("USDT", "-USDT-SWAP")
    f = get_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}")
    oi = get_json(f"https://www.okx.com/api/v5/public/open-interest?instId={inst}")
    p = get_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst}")
    return {
        "funding": float(f["data"][0]["fundingRate"]),
        "oi": float(oi["data"][0]["oi"]),
        "price": float(p["data"][0]["last"]),
    }


def bitget_data(symbol):
    r = get_json(
        f"https://api.bitget.com/api/v2/mix/market/ticker"
        f"?symbol={symbol}&productType=USDT-FUTURES"
    )
    item = r["data"][0]
    return {
        "funding": float(item["fundingRate"]),
        "price": float(item["markPrice"]),
        "oi": float(item["holdingAmount"]),
    }


EXCHANGE_FUNCS = {
    "binance": binance_data,
    "bybit": bybit_data,
    "okx": okx_data,
    "bitget": bitget_data,
}

# ================= Signal Engine =================

def compute_signal(state):
    if len(state["price_avg"]) < 60:
        return None

    prices = list(state["price_avg"])
    fundings = list(state["funding_avg"])
    ois = list(state["oi_avg"])
    ts_list = list(state["ts"])

    funding_now = fundings[-1]
    price_now = prices[-1]
    oi_now = ois[-1]
    ts_now = ts_list[-1]

    funding_warn = funding_now <= SIGNAL_CONFIG["funding_warn"]
    funding_strong = funding_now <= SIGNAL_CONFIG["funding_strong"]

    window = int(1800 / REFRESH_INTERVAL)
    if len(ois) > window:
        oi_prev = ois[-window]
        oi_change = (oi_now - oi_prev) / oi_prev if oi_prev else 0
    else:
        oi_change = 0

    oi_break = oi_change <= SIGNAL_CONFIG["oi_drop_pct"]

    vwap = mean(prices[-window:])
    price_break = (price_now - vwap) / vwap <= SIGNAL_CONFIG["price_break_pct"]

    # ---- Pullback Short ----
    pullback_short = False
    if len(prices) >= window * 2:
        prev_leg_low = min(prices[-window*2:-window])
        rebound = (price_now - prev_leg_low) / prev_leg_low
        reject = (price_now - vwap) / vwap <= SIGNAL_CONFIG["pullback_reject_pct"]
        pullback_short = rebound > 0.01 and reject and funding_warn and oi_break

    # ---- Pullback Long ----
    pullback_long = False
    if len(prices) >= window * 2:
        prev_leg_high = max(prices[-window*2:-window])
        pullback = (price_now - prev_leg_high) / prev_leg_high
        support = (price_now - vwap) / vwap >= -SIGNAL_CONFIG["pullback_reject_pct"]
        pullback_long = pullback < -0.01 and support and funding_now >= 0.01 and oi_change >= 0.04

    now = now_ts()
    if now - state["last_signal_ts"] < SIGNAL_CONFIG["cooldown_sec"]:
        return None

    level = None
    side = None

    if funding_strong and oi_break and price_break:
        level = "STRONG_SHORT"
        side = "SHORT"
    elif pullback_short:
        level = "PULLBACK_SHORT"
        side = "SHORT"
    elif pullback_long:
        level = "PULLBACK_LONG"
        side = "LONG"

    if not level:
        return None

    state["last_signal_ts"] = now

    return {
        "ts": ts_now,
        "level": level,
        "side": side,
        "funding": funding_now,
        "oi_change": oi_change,
        "price": price_now,
        "vwap": vwap,
    }


# ================= Collector =================

def collector(symbol):
    print(f"▶️ collector started for {symbol}")
    state = symbols_state[symbol]

    while True:
        try:
            snapshot = {}
            for name, fn in EXCHANGE_FUNCS.items():
                try:
                    snapshot[name] = fn(symbol)
                except Exception as e:
                    print(f"⚠️ {symbol} {name} failed:", e)

            if snapshot:
                prices = []
                fundings = []
                ois = []

                for ex, v in snapshot.items():
                    prices.append(v["price"])
                    fundings.append(v["funding"])
                    ois.append(v["oi"])

                    state["price_ex"][ex].append(v["price"])
                    state["funding_ex"][ex].append(v["funding"])
                    state["oi_ex"][ex].append(v["oi"])

                ts_now = datetime.utcnow().isoformat()
                state["ts"].append(ts_now)
                state["price_avg"].append(mean(prices))
                state["funding_avg"].append(mean(fundings))
                state["oi_avg"].append(mean(ois))

                persist(symbol, snapshot)

                sig = compute_signal(state)
                if sig:
                    state["signals"].append(sig)
                    print("🚨 SIGNAL:", symbol, sig)

        except Exception as e:
            print(f"collector {symbol} error:", e)

        time.sleep(REFRESH_INTERVAL)


# ================= Web =================

HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Trading Signal Engine</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body { background:#020617; color:#e5e7eb; font-family:Arial; }
    h1 { margin-bottom:4px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:20px; }
    .card { background:#020617; padding:16px; border-radius:10px; border:1px solid #1e293b; }
    .signal-strong { color:#ef4444; font-weight:bold; }
    .signal-warn { color:#f59e0b; font-weight:bold; }
    .signal-long { color:#22c55e; font-weight:bold; }
    table { width:100%; border-collapse:collapse; margin-top:8px; }
    th,td { padding:6px; text-align:right; }
    th { color:#94a3b8; }
    td:first-child, th:first-child { text-align:left; }
    select { background:#020617; color:#e5e7eb; border:1px solid #1e293b; padding:6px; border-radius:6px; }
    .legend { font-size:12px; color:#94a3b8; margin-bottom:4px; }
    .toolbar { display:flex; gap:12px; margin-bottom:10px; }
  </style>
</head>
<body>
<h1>⚡ Signal Engine</h1>

<div class="toolbar">
  <select id="symbolSelect"></select>
  <select id="rangeSelect">
    <option value="1" selected>1h</option>
    <option value="3">3h</option>
    <option value="6">6h</option>
    <option value="12">12h</option>
    <option value="24">24h</option>
    <option value="168">7d</option>
  </select>
</div>

<div class="grid">
  <div class="card">
    <div class="legend">Price (Avg + Exchanges)</div>
    <canvas id="priceChart"></canvas>
  </div>
  <div class="card">
    <div class="legend">Funding % (Avg + Exchanges)</div>
    <canvas id="fundingChart"></canvas>
  </div>
  <div class="card">
    <div class="legend">Open Interest (Avg + Exchanges)</div>
    <canvas id="oiChart"></canvas>
  </div>
  <div class="card">
    <h3>Signals</h3>
    <table id="signalTable">
      <thead>
        <tr><th>Time</th><th>Level</th><th>Side</th><th>Funding</th><th>OI Δ%</th><th>Price</th><th>VWAP</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script>
const symbols = {{ symbols | safe }};
let currentSymbol = symbols[0];
let currentHours = 24;

const COLORS = {
  avg: "#ffffff",
  binance: "#facc15",
  bybit: "#38bdf8",
  okx: "#a855f7",
  bitget: "#22c55e",
};

function makeMultiChart(id, formatter=null) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { labels: [], datasets: [] },
    options: {
      responsive:true,
      animation:false,
      interaction:{mode:"nearest", intersect:false},
      scales:{
        x:{display:false},
        y:{ ticks:{ callback: formatter || (v=>v) } }
      },
      plugins:{
        legend:{ labels:{ color:"#cbd5f5" } }
      }
    }
  });
}

const priceChart = makeMultiChart("priceChart");
const fundingChart = makeMultiChart("fundingChart", v=>(v*100).toFixed(4)+"%");
const oiChart = makeMultiChart("oiChart");

function initControls() {
  const sel = document.getElementById("symbolSelect");
  symbols.forEach(s=>{
    const opt = document.createElement("option");
    opt.value = s;
    opt.text = s;
    sel.appendChild(opt);
  });
  sel.onchange = () => {
    currentSymbol = sel.value;
    refresh();
  };

  const rangeSel = document.getElementById("rangeSelect");
  rangeSel.onchange = () => {
    currentHours = parseInt(rangeSel.value);
    refresh();
  };
}

function buildDatasets(chart, ts, avg, exMap, unitLabel) {
  const datasets = [];

  datasets.push({
    label: unitLabel + " AVG",
    data: avg,
    borderColor: COLORS.avg,
    borderWidth: 2.5,
    tension:0.25,
    pointRadius:0,
  });

  for (const ex in exMap) {
    datasets.push({
      label: unitLabel + " " + ex,
      data: exMap[ex],
      borderColor: COLORS[ex],
      borderWidth: 1,
      tension:0.25,
      pointRadius:0,
    });
  }

  chart.data.labels = ts.map(t=>t.slice(11,19));
  chart.data.datasets = datasets;
  chart.update();
}

function overlaySignals(chart, ts, prices, signals) {
  if (!signals || signals.length === 0) return;

  const sigDataset = {
    label: "Signals",
    data: [],
    showLine: false,
    pointRadius: 6,
    pointHoverRadius: 8,
    pointBackgroundColor: [],
    pointBorderColor: [],
  };

  signals.forEach(sig => {
    const idx = ts.findIndex(t => t >= sig.ts);
    if (idx === -1) return;

    sigDataset.data.push({ x: idx, y: prices[idx] });
    if (sig.side === "SHORT") {
      sigDataset.pointBackgroundColor.push("#ef4444");
      sigDataset.pointBorderColor.push("#ef4444");
    } else {
      sigDataset.pointBackgroundColor.push("#22c55e");
      sigDataset.pointBorderColor.push("#22c55e");
    }
  });

  chart.data.datasets.push(sigDataset);
}

async function refresh() {
  const res = await fetch(`/api/state?symbol=${currentSymbol}&hours=${currentHours}`);
  const data = await res.json();

  buildDatasets(priceChart, data.ts, data.price_avg, data.price_ex, "Price");
  buildDatasets(fundingChart, data.ts, data.funding_avg, data.funding_ex, "Funding");
  buildDatasets(oiChart, data.ts, data.oi_avg, data.oi_ex, "OI");

  overlaySignals(priceChart, data.ts, data.price_avg, data.signals);

  const tbody = document.querySelector("#signalTable tbody");
  tbody.innerHTML = "";
  (data.signals || []).slice().reverse().forEach(s => {
    const cls = s.side === "SHORT" ? "signal-strong" : "signal-long";
    tbody.innerHTML += `
      <tr class="${cls}">
        <td>${s.ts.slice(11,19)}</td>
        <td>${s.level}</td>
        <td>${s.side}</td>
        <td>${(s.funding*100).toFixed(4)}%</td>
        <td>${(s.oi_change*100).toFixed(2)}%</td>
        <td>${s.price.toFixed(3)}</td>
        <td>${s.vwap.toFixed(3)}</td>
      </tr>`;
  });
}

initControls();
setInterval(refresh, 10000);
refresh();
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML, symbols=SYMBOLS)


def serialize_state(state, hours):
    points = int(hours * 3600 / REFRESH_INTERVAL)

    ts = list(state["ts"])[-points:]
    price_avg = list(state["price_avg"])[-points:]
    funding_avg = list(state["funding_avg"])[-points:]
    oi_avg = list(state["oi_avg"])[-points:]

    price_ex = {k: list(v)[-points:] for k, v in state["price_ex"].items()}
    funding_ex = {k: list(v)[-points:] for k, v in state["funding_ex"].items()}
    oi_ex = {k: list(v)[-points:] for k, v in state["oi_ex"].items()}

    signals = [s for s in state["signals"] if s["ts"] >= ts[0]] if ts else []

    return {
        "ts": ts,
        "price_avg": price_avg,
        "funding_avg": funding_avg,
        "oi_avg": oi_avg,
        "price_ex": price_ex,
        "funding_ex": funding_ex,
        "oi_ex": oi_ex,
        "signals": signals,
    }


@app.route("/api/state")
def api_state():
    symbol = request.args.get("symbol", SYMBOLS[0])
    hours = float(request.args.get("hours", 24))
    return jsonify(serialize_state(symbols_state[symbol], hours))


# ================= Bootstrap =================

def start_symbol(symbol):
    symbols_state[symbol] = load_history(symbol)
    threading.Thread(target=collector, args=(symbol,), daemon=True).start()


if __name__ == "__main__":
    for sym in SYMBOLS:
        start_symbol(sym)
    app.run(host="0.0.0.0", port=8081, debug=False)
