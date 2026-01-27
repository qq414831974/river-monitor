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
    "funding_strong": -0.02,
    "funding_warn": -0.015,
    "oi_drop_pct": -0.08,       # 30 min
    "price_break_pct": -0.015, # below VWAP
    "cooldown_sec": 1800,
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
        "price_avg": deque(maxlen=MAX_POINTS),
        "funding_avg": deque(maxlen=MAX_POINTS),
        "oi_avg": deque(maxlen=MAX_POINTS),

        "price_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},
        "funding_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},
        "oi_ex": {ex: deque(maxlen=MAX_POINTS) for ex in EXCHANGE_FUNCS},

        "signals": deque(maxlen=200),
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

    funding_now = fundings[-1]
    price_now = prices[-1]
    oi_now = ois[-1]

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

    now = now_ts()
    if now - state["last_signal_ts"] < SIGNAL_CONFIG["cooldown_sec"]:
        return None

    level = None
    if funding_strong and oi_break and price_break:
        level = "STRONG_SHORT"
    elif funding_warn and oi_break:
        level = "PREPARE_SHORT"

    if not level:
        return None

    state["last_signal_ts"] = now

    return {
        "ts": datetime.utcnow().isoformat(),
        "level": level,
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
    h1 { margin-bottom:6px; }
    .topbar { display:flex; gap:12px; align-items:center; margin-bottom:10px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:20px; }
    .card { background:#020617; padding:16px; border-radius:10px; border:1px solid #1e293b; }
    .signal-strong { color:#ef4444; font-weight:bold; }
    .signal-warn { color:#f59e0b; font-weight:bold; }
    table { width:100%; border-collapse:collapse; margin-top:8px; }
    th,td { padding:6px; text-align:right; }
    th { color:#94a3b8; }
    td:first-child, th:first-child { text-align:left; }
    select { background:#020617; color:#e5e7eb; border:1px solid #1e293b; padding:6px; border-radius:6px; }
    .legend { font-size:12px; color:#94a3b8; margin-bottom:4px; }
  </style>
</head>
<body>
<h1>⚡ Signal Engine</h1>

<div class="topbar">
  <select id="symbolSelect"></select>
  <select id="rangeSelect">
    <option value="1800">30m</option>
    <option value="3600">1h</option>
    <option value="14400">4h</option>
    <option value="43200">12h</option>
    <option value="86400" selected>24h</option>
    <option value="all">All</option>
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
        <tr><th>Time</th><th>Level</th><th>Funding</th><th>OI Δ%</th><th>Price</th><th>VWAP</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script>
const symbols = {{ symbols | safe }};
let currentSymbol = symbols[0];
let currentRange = "86400";

const COLORS = {
  avg: "#ffffff",
  binance: "#facc15",
  bybit: "#38bdf8",
  okx: "#a855f7",
  bitget: "#22c55e",
};

function makeMultiChart(id, label, formatter=null) {
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

const priceChart = makeMultiChart("priceChart", "Price");
const fundingChart = makeMultiChart("fundingChart", "Funding", v=>(v*100).toFixed(4)+"%");
const oiChart = makeMultiChart("oiChart", "OI");

function initControls() {
  const symSel = document.getElementById("symbolSelect");
  symbols.forEach(s=>{
    const opt = document.createElement("option");
    opt.value = s;
    opt.text = s;
    symSel.appendChild(opt);
  });
  symSel.onchange = () => {
    currentSymbol = symSel.value;
    refresh();
  };

  const rangeSel = document.getElementById("rangeSelect");
  rangeSel.onchange = () => {
    currentRange = rangeSel.value;
    refresh();
  };
}

function buildDatasets(chart, avg, exMap, unitLabel) {
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

  chart.data.labels = avg.map((_,i)=>i);
  chart.data.datasets = datasets;
  chart.update();
}

async function refresh() {
  const res = await fetch(`/api/state?symbol=${currentSymbol}&range=${currentRange}`);
  const data = await res.json();

  buildDatasets(priceChart, data.price_avg, data.price_ex, "Price");
  buildDatasets(fundingChart, data.funding_avg, data.funding_ex, "Funding");
  buildDatasets(oiChart, data.oi_avg, data.oi_ex, "OI");

  const tbody = document.querySelector("#signalTable tbody");
  tbody.innerHTML = "";
  (data.signals || []).slice().reverse().forEach(s => {
    const cls = s.level === "STRONG_SHORT" ? "signal-strong" : "signal-warn";
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


def slice_by_range(arr, seconds):
    if seconds == "all":
        return arr
    try:
        sec = int(seconds)
    except:
        return arr
    points = int(sec / REFRESH_INTERVAL)
    return arr[-points:] if points > 0 else arr


def serialize_state(state, range_sec):
    return {
        "price_avg": slice_by_range(list(state["price_avg"]), range_sec),
        "funding_avg": slice_by_range(list(state["funding_avg"]), range_sec),
        "oi_avg": slice_by_range(list(state["oi_avg"]), range_sec),
        "price_ex": {k: slice_by_range(list(v), range_sec) for k, v in state["price_ex"].items()},
        "funding_ex": {k: slice_by_range(list(v), range_sec) for k, v in state["funding_ex"].items()},
        "oi_ex": {k: slice_by_range(list(v), range_sec) for k, v in state["oi_ex"].items()},
        "signals": list(state["signals"]),
    }


@app.route("/api/state")
def api_state():
    symbol = request.args.get("symbol", SYMBOLS[0])
    range_sec = request.args.get("range", "86400")
    return jsonify(serialize_state(symbols_state[symbol], range_sec))


# ================= Bootstrap =================

def start_symbol(symbol):
    symbols_state[symbol] = load_history(symbol)
    threading.Thread(target=collector, args=(symbol,), daemon=True).start()


if __name__ == "__main__":
    for sym in SYMBOLS:
        start_symbol(sym)
    app.run(host="0.0.0.0", port=8081, debug=False)
