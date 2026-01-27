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

# ================= Access Control =================

last_access_ts = 0
ACCESS_TIMEOUT = 300  # 超过300秒无人访问，则认为无人访问

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

    # ===== replay history =====
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

    if len(ois) > window:
        oi_prev = ois[-window]
        oi_change = (oi_now - oi_prev) / oi_prev if oi_prev else 0
    else:
        return None

    oi_break = oi_change <= SIGNAL_CONFIG["oi_drop_pct"]

    vwap = mean(prices[-window:])
    price_break = (price_now - vwap) / vwap <= SIGNAL_CONFIG["price_break_pct"]

    now = now_ts()
    if now - state["last_signal_ts"] < SIGNAL_CONFIG["cooldown_sec"]:
        return None

    if funding_strong and oi_break and price_break:
        level = "STRONG_SHORT"
    elif funding_warn and oi_break:
        level = "PREPARE_SHORT"
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
        "index": len(prices) - 1,
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

    rebound = (prices[-1] - prices[-short_window]) / prices[-short_window] >= 0.008
    near_vwap = abs(price_now - vwap_30) / vwap_30 <= 0.003

    funding_rebound = fundings[-short_window] < funding_now < 0

    oi_change = (oi_now - ois[-short_window]) / ois[-short_window]
    oi_hold = oi_change >= -0.01

    now = now_ts()
    if now - state["last_signal_ts"] < SIGNAL_CONFIG["cooldown_sec"]:
        return None

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


def compute_realtime_signal(state):
    sig1 = compute_signal_on_series(state)
    sig2 = compute_pullback_on_series(state)
    return sig2 or sig1

# ================= Collector =================

def collector(symbol):
    print(f"▶️ collector started for {symbol}")
    state = symbols_state[symbol]

    while True:
        now = time.time()
        # 有人访问，直接读取缓存，不刷新
        if now - last_access_ts < ACCESS_TIMEOUT:
            time.sleep(5)
            continue

        # 无人访问，刷新数据
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

HTML = """ ... 保留你原来的 HTML 不变 ... """

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
    return jsonify(serialize_state(symbols_state[symbol], range_sec))


def slice_deque(dq, n):
    return list(dq)[-n:] if n else list(dq)


def serialize_state(state, range_sec):
    points = int(range_sec / REFRESH_INTERVAL)
    return {
        "price_avg": slice_deque(state["price_avg"], points),
        "funding_avg": slice_deque(state["funding_avg"], points),
        "oi_avg": slice_deque(state["oi_avg"], points),
        "price_ex": {k: slice_deque(v, points) for k, v in state["price_ex"].items()},
        "funding_ex": {k: slice_deque(v, points) for k, v in state["funding_ex"].items()},
        "oi_ex": {k: slice_deque(v, points) for k, v in state["oi_ex"].items()},
        "signals": list(state["signals"]),
    }

# ================= Bootstrap =================

def start_symbol(symbol):
    symbols_state[symbol] = load_history(symbol)
    threading.Thread(target=collector, args=(symbol,), daemon=True).start()


if __name__ == "__main__":
    for sym in SYMBOLS:
        start_symbol(sym)
    app.run(host="0.0.0.0", port=8081, debug=False)
