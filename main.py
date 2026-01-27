import requests
import time
import threading
import json
import os
from datetime import datetime, timezone, timedelta
from collections import deque
from flask import Flask, jsonify, render_template_string, request

# ================= Config =================

REFRESH_INTERVAL = 10
MAX_HOURS = 24
MAX_POINTS = int(3600 / REFRESH_INTERVAL * MAX_HOURS)

SIGNAL_CONFIG = {
    "funding_strong": -0.02,
    "funding_warn": -0.012,
    "oi_drop_pct": -0.06,        # 30 min
    "price_break_pct": -0.01,    # below VWAP
    "cooldown_sec": 900,
}

SYMBOLS = ["RIVERUSDT", "HYPEUSDT", "BTCUSDT", "ETHUSDT"]

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# ================= App =================

app = Flask(__name__)
symbols_state = {}
last_access_ts = time.time()

# ================= Persistence =================

def log_file(symbol):
    return os.path.join(DATA_DIR, f"{symbol}.jsonl")

def slice_deque(dq, n):
    return list(dq)[-n:] if dq else []

def load_history(symbol):
    # 初始化 state
    state = {
        "price_avg": deque(maxlen=MAX_POINTS),
        "funding_avg": deque(maxlen=MAX_POINTS),
        "oi_avg": deque(maxlen=MAX_POINTS),

        "price_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},
        "funding_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},
        "oi_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},

        "signals": deque(maxlen=500),
        "last_signal_ts": 0,
        "ts": deque(maxlen=MAX_POINTS),
    }

    path = log_file(symbol)
    if not os.path.exists(path):
        return state

    try:
        with open(path) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except:
        return state

    for row in rows:
        snap = row["data"]
        ts = row.get("ts")
        prices, fundings, ois = [], [], []

        for ex, v in snap.items():
            prices.append(v["price"])
            fundings.append(v["funding"])
            ois.append(v["oi"])

            state["price_ex"][ex].append(v["price"])
            state["funding_ex"][ex].append(v["funding"])
            state["oi_ex"][ex].append(v["oi"])

        state["price_avg"].append(mean(prices))
        state["funding_avg"].append(mean(fundings))
        state["oi_avg"].append(mean(ois))
        state["ts"].append(ts)

        sig = compute_signal_on_series(state)
        if sig:
            state["signals"].append(sig)

    return state

def persist(symbol, snapshot, ts):
    record = {
        "ts": ts,
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
    r = get_json(f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES")
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

def compute_signal_on_series(state):
    prices = list(state["price_avg"])
    fundings = list(state["funding_avg"])
    ois = list(state["oi_avg"])
    if len(prices) < 60:
        return None

    funding_now = fundings[-1]
    price_now = prices[-1]
    oi_now = ois[-1]
    window = int(1800 / REFRESH_INTERVAL)

    funding_strong = funding_now <= SIGNAL_CONFIG["funding_strong"]
    funding_warn = funding_now <= SIGNAL_CONFIG["funding_warn"]
    funding_long_strong = funding_now >= -SIGNAL_CONFIG["funding_strong"]
    funding_long_warn = funding_now >= -SIGNAL_CONFIG["funding_warn"]

    if len(ois) > window:
        oi_prev = ois[-window]
        oi_change = (oi_now - oi_prev) / oi_prev if oi_prev else 0
    else:
        return None

    oi_break = oi_change <= SIGNAL_CONFIG["oi_drop_pct"]
    oi_rise = oi_change >= -SIGNAL_CONFIG["oi_drop_pct"]

    vwap = mean(prices[-window:])
    price_break = (price_now - vwap) / vwap <= SIGNAL_CONFIG["price_break_pct"]
    price_rise = (price_now - vwap) / vwap >= -SIGNAL_CONFIG["price_break_pct"]

    now = now_ts()
    if now - state["last_signal_ts"] < SIGNAL_CONFIG["cooldown_sec"]:
        return None

    # 做空
    if funding_strong and oi_break and price_break:
        level = "STRONG_SHORT"
    elif funding_warn and oi_break:
        level = "PREPARE_SHORT"
    # 做多
    elif funding_long_strong and oi_rise and price_rise:
        level = "STRONG_LONG"
    elif funding_long_warn and oi_rise:
        level = "PREPARE_LONG"
    else:
        return None

    state["last_signal_ts"] = now
    idx = len(prices) - 1
    return {
        "ts": state["ts"][idx],
        "level": level,
        "price": price_now,
        "funding": funding_now,
        "oi_change": oi_change,
        "vwap": vwap,
        "index": idx,
    }

def compute_pullback_on_series(state):
    prices = list(state["price_avg"])
    fundings = list(state["funding_avg"])
    ois = list(state["oi_avg"])
    if len(prices) < 120:
        return None

    price_now = prices[-1]
    funding_now = fundings[-1]
    oi_now = ois[-1]

    long_window = int(1800 / REFRESH_INTERVAL)
    short_window = int(600 / REFRESH_INTERVAL)

    vwap_30 = mean(prices[-long_window:])
    trend_down = price_now < vwap_30
    trend_up = price_now > vwap_30

    rebound = (prices[-1] - prices[-short_window]) / prices[-short_window] >= 0.008
    near_vwap = abs(price_now - vwap_30) / vwap_30 <= 0.003

    funding_rebound = fundings[-short_window] < funding_now < 0
    oi_change = (oi_now - ois[-short_window]) / ois[-short_window]
    oi_hold = oi_change >= -0.01

    now = now_ts()
    if now - state["last_signal_ts"] < SIGNAL_CONFIG["cooldown_sec"]:
        return None

    # 做空回调
    if trend_down and rebound and near_vwap and funding_rebound and oi_hold:
        state["last_signal_ts"] = now
        idx = len(prices) - 1
        return {
            "ts": state["ts"][idx],
            "level": "PULLBACK_SHORT",
            "price": price_now,
            "funding": funding_now,
            "oi_change": oi_change,
            "vwap": vwap_30,
            "index": idx,
        }
    # 做多回调
    if trend_up and rebound and near_vwap and funding_rebound and oi_hold:
        state["last_signal_ts"] = now
        idx = len(prices) - 1
        return {
            "ts": state["ts"][idx],
            "level": "PULLBACK_LONG",
            "price": price_now,
            "funding": funding_now,
            "oi_change": oi_change,
            "vwap": vwap_30,
            "index": idx,
        }

def compute_realtime_signal(state):
    sig1 = compute_signal_on_series(state)
    sig2 = compute_pullback_on_series(state)
    return sig2 or sig1

# ================= Collector =================

def collector(symbol):
    print(f"▶️ collector started for {symbol}")
    state = symbols_state[symbol]

    while True:
        try:
            snapshot_ts = datetime.now(timezone(timedelta(hours=8))).isoformat()
            snapshot = {}
            for name, fn in EXCHANGE_FUNCS.items():
                try:
                    snapshot[name] = fn(symbol)
                except Exception as e:
                    print(f"⚠️ {symbol} {name} failed:", e)

            if snapshot:
                prices, fundings, ois = [], [], []
                for ex, v in snapshot.items():
                    prices.append(v["price"])
                    fundings.append(v["funding"])
                    ois.append(v["oi"])

                    state["price_ex"][ex].append(v["price"])
                    state["funding_ex"][ex].append(v["funding"])
                    state["oi_ex"][ex].append(v["oi"])

                state["price_avg"].append(mean(prices))
                state["funding_avg"].append(mean(fundings))
                state["oi_avg"].append(mean(ois))
                state["ts"].append(snapshot_ts)

                persist(symbol, snapshot, snapshot_ts)

                sig = compute_realtime_signal(state)
                if sig:
                    state["signals"].append(sig)
                    print("🚨 SIGNAL:", symbol, sig)

        except Exception as e:
            print(f"collector {symbol} error:", e)

        time.sleep(60)

# ================= Web =================

HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Signal Engine</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body { background:#020617; color:#e5e7eb; font-family:Arial; }
    h1 { margin-bottom:4px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:20px; }
    .card { background:#020617; padding:16px; border-radius:10px; border:1px solid #1e293b; }
    .signal-strong { color:#ef4444; font-weight:bold; }       /* STRONG_SHORT */
    .signal-warn { color:#f59e0b; font-weight:bold; }         /* PREPARE_SHORT */
    .signal-pullback { color:#38bdf8; font-weight:bold; }     /* PULLBACK_SHORT */
    .signal-strong-long { color:#22c55e; font-weight:bold; }  /* STRONG_LONG */
    .signal-warn-long { color:#10b981; font-weight:bold; }    /* PREPARE_LONG */
    .signal-pullback-long { color:#3b82f6; font-weight:bold; }/* PULLBACK_LONG */
    table { width:100%; border-collapse:collapse; margin-top:8px; }
    th,td { padding:6px; text-align:right; }
    th { color:#94a3b8; }
    td:first-child, th:first-child { text-align:left; }
    select { background:#020617; color:#e5e7eb; border:1px solid #1e293b; padding:6px; border-radius:6px; }
    .legend { font-size:12px; color:#94a3b8; margin-bottom:4px; }
  </style>
</head>
<body>
<h1>⚡ Trading Signal Engine</h1>

<select id="symbolSelect"></select>
<select id="rangeSelect">
  <option value="900" selected>15m</option>
  <option value="3600">1h</option>
  <option value="10800">3h</option>
  <option value="21600">6h</option>
  <option value="43200">12h</option>
  <option value="86400">24h</option>
</select>

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
        <tr><th>Time</th><th>Level</th><th>Funding</th><th>OI Δ%</th><th>Price</th><th>VWAP</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script>
const symbols = {{ symbols | safe }};
let currentSymbol = symbols[0];
let rangeSec = 86400;

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

function initSymbols() {
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
    rangeSec = parseInt(rangeSel.value);
    refresh();
  };
}

function buildDatasets(chart, ts, avg, exMap, unitLabel, markers=[]) {
  const datasets = [];
  avg = avg || [];
  exMap = exMap || {};
  markers = markers || [];
  
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

  if (markers.length) {
    datasets.push({
      type: "scatter",
      label: "Signal",
      data: markers,
      pointRadius: 6,
      pointBackgroundColor: markers.map(m => {
        if(m.level.includes("SHORT")) return "#ef4444";
        if(m.level.includes("PREPARE_SHORT")) return "#f59e0b";
        if(m.level.includes("PULLBACK_SHORT")) return "#38bdf8";
        if(m.level.includes("LONG")) return "#22c55e";
        return "#ffffff";
      }),
      showLine: false,
    });
  }

  chart.data.labels = ts.map(t => t.slice(11,19));
  chart.data.datasets = datasets;
  chart.update();
}

async function refresh() {
  const res = await fetch(`/api/state?symbol=${currentSymbol}&range=${rangeSec}`);
  const data = await res.json();
  const signals = data.signals || [];

  const markers = signals.map(s => ({
    x: s.index,
    y: s.price,
    level: s.level
  }));

  buildDatasets(priceChart, data.ts, data.price_avg, data.price_ex, "Price", markers);
  buildDatasets(fundingChart, data.ts, data.funding_avg, data.funding_ex, "Funding");
  buildDatasets(oiChart, data.ts, data.oi_avg, data.oi_ex, "OI");

  const tbody = document.querySelector("#signalTable tbody");
  tbody.innerHTML = "";
  signals.slice().reverse().forEach(s => {
    let cls = "";
    if (s.level === "STRONG_SHORT") cls = "signal-strong";
    if (s.level === "PREPARE_SHORT") cls = "signal-warn";
    if (s.level === "PULLBACK_SHORT") cls = "signal-pullback";
    if (s.level === "STRONG_LONG") cls = "signal-strong-long";
    if (s.level === "PREPARE_LONG") cls = "signal-warn-long";
    if (s.level === "PULLBACK_LONG") cls = "signal-pullback-long";
    tbody.innerHTML += `
      <tr class="${cls}">
        <td>${s.ts.slice(11,19)}</td>
        <td>${s.level}</td>
        <td>${(s.funding*100).toFixed(4)}%</td>
        <td>${(s.oi_change*100).toFixed(2)}%</td>
        <td>${s.price.toFixed(3)}</td>
        <td>${s.vwap.toFixed(3)}</td>
      </tr>`;
  });
}

initSymbols();
setInterval(refresh, 10000);
refresh();
</script>
</body>
</html>
"""

@app.route("/")
def home():
    global last_access_ts
    last_access_ts = time.time()
    return render_template_string(HTML, symbols=SYMBOLS)

@app.route("/api/state")
def api_state():
    global last_access_ts
    last_access_ts = time.time()
    symbol = request.args.get("symbol", SYMBOLS[0])
    range_sec = int(request.args.get("range", 86400))
    state = symbols_state.get(symbol)
    if not state:
        return jsonify({})
    points = int(range_sec / REFRESH_INTERVAL)
    return jsonify({
        "ts": slice_deque(state["ts"], points),
        "price_avg": slice_deque(state["price_avg"], points),
        "funding_avg": slice_deque(state["funding_avg"], points),
        "oi_avg": slice_deque(state["oi_avg"], points),
        "price_ex": {k: slice_deque(v, points) for k, v in state["price_ex"].items()},
        "funding_ex": {k: slice_deque(v, points) for k, v in state["funding_ex"].items()},
        "oi_ex": {k: slice_deque(v, points) for k, v in state["oi_ex"].items()},
        "signals": list(state["signals"]),
    })

# ================= Bootstrap =================

def start_symbol(symbol):
    symbols_state[symbol] = load_history(symbol)
    threading.Thread(target=collector, args=(symbol,), daemon=True).start()

if __name__ == "__main__":
    for sym in SYMBOLS:
        start_symbol(sym)
    app.run(host="0.0.0.0", port=8081, debug=False)
