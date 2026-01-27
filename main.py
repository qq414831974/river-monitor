import requests
import time
import json
from datetime import datetime

SYMBOL = "RIVERUSDT"
OKX_SYMBOL = "RIVER-USDT-SWAP"
BITGET_SYMBOL = "RIVERUSDT"

ALERT_THRESHOLDS = {
    "funding_rate": -0.02,
    "oi_change": -0.3,
    "price_levels": [65, 50, 30],
}

prev_oi = None
price_alerted = set()


# ================= Utils =================

def get_json(url):
    r = requests.get(url, timeout=6)
    r.raise_for_status()
    return r.json()


def fmt_price(x):
    return f"{x:,.2f}"


def fmt_pct(x):
    return f"{x*100:,.3f}%"


def fmt_int(x):
    return f"{int(float(x)):,}"


def line():
    print("━" * 54)


# ================= Binance =================

def binance_data():
    data = get_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=RIVERUSDT")
    oi = get_json("https://fapi.binance.com/fapi/v1/openInterest?symbol=RIVERUSDT")
    return {
        "funding": float(data["lastFundingRate"]),
        "price": float(data["markPrice"]),
        "oi": float(oi["openInterest"]),
    }


# ================= Bybit =================

def bybit_data():
    data = get_json("https://api.bybit.com/v5/market/tickers?category=linear&symbol=RIVERUSDT")
    item = data["result"]["list"][0]
    return {
        "funding": float(item["fundingRate"]),
        "price": float(item["markPrice"]),
        "oi": float(item["openInterest"]),
    }


# ================= OKX =================

def okx_data():
    funding = get_json("https://www.okx.com/api/v5/public/funding-rate?instId=RIVER-USDT-SWAP")
    oi = get_json("https://www.okx.com/api/v5/public/open-interest?instId=RIVER-USDT-SWAP")
    price = get_json("https://www.okx.com/api/v5/market/ticker?instId=RIVER-USDT-SWAP")

    return {
        "funding": float(funding["data"][0]["fundingRate"]),
        "oi": float(oi["data"][0]["oi"]),
        "price": float(price["data"][0]["last"]),
    }


# ================= Bitget =================

def bitget_data():
    data = get_json(
        "https://api.bitget.com/api/v2/mix/market/ticker"
        "?symbol=RIVERUSDT&productType=USDT-FUTURES"
    )
    item = data["data"][0]
    return {
        "funding": float(item["fundingRate"]),
        "price": float(item["markPrice"]),
        "oi": float(item["holdingAmount"]),
    }


# ================= Alert =================

def send_alert(msg):
    print(f"🚨 ALERT: {msg}")


# ================= Pretty Print =================

def render_dashboard(data, prev_total_oi=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line()
    print(f"⏰ {ts}   SYMBOL: {SYMBOL}")
    line()
    print(f"{'Exchange':<10} {'Price':<10} {'Funding':<12} {'OI'}")
    print("-" * 54)

    prices, fundings, ois = [], [], []

    for ex, v in data.items():
        prices.append(v["price"])
        fundings.append(v["funding"])
        ois.append(v["oi"])

        fr = fmt_pct(v["funding"])
        if v["funding"] < ALERT_THRESHOLDS["funding_rate"]:
            fr = f"🔥 {fr}"

        print(
            f"{ex:<10} "
            f"{fmt_price(v['price']):<10} "
            f"{fr:<12} "
            f"{fmt_int(v['oi'])}"
        )

    print("-" * 54)
    avg_price = sum(prices) / len(prices)
    avg_funding = sum(fundings) / len(fundings)
    total_oi = sum(ois)

    print(
        f"{'AVG':<10} "
        f"{fmt_price(avg_price):<10} "
        f"{fmt_pct(avg_funding):<12} "
        f"{fmt_int(total_oi)}"
    )

    if prev_total_oi:
        change = (total_oi - prev_total_oi) / prev_total_oi
        emoji = "📉" if change < 0 else "📈"
        print(f"\n{emoji} OI Change: {fmt_pct(change)}")

    line()

    return avg_price, avg_funding, total_oi


# ================= Main Loop =================

def monitor():
    global prev_oi

    while True:
        try:
            data = {}

            for name, fn in [
                ("binance", binance_data),
                ("bybit", bybit_data),
                ("okx", okx_data),
                ("bitget", bitget_data),
            ]:
                try:
                    data[name] = fn()
                except Exception as e:
                    print(f"⚠️ {name} 获取失败:", e)

            if not data:
                time.sleep(60)
                continue

            avg_price, avg_funding, total_oi = render_dashboard(data, prev_oi)

            # ---------- Funding Alert ----------
            if avg_funding < ALERT_THRESHOLDS["funding_rate"]:
                send_alert(f"🧨 极端资金费率: {fmt_pct(avg_funding)}")

            # ---------- OI Alert ----------
            if prev_oi:
                change = (total_oi - prev_oi) / prev_oi
                if change < ALERT_THRESHOLDS["oi_change"]:
                    send_alert(f"📉 持仓量骤降: {fmt_pct(change)}")

            prev_oi = total_oi

            # ---------- Price Levels ----------
            for level in ALERT_THRESHOLDS["price_levels"]:
                if avg_price < level and level not in price_alerted:
                    send_alert(f"📌 跌破关键价位 ${level}, 当前 ${fmt_price(avg_price)}")
                    price_alerted.add(level)

            # ---------- Log ----------
            log = {
                "ts": time.time(),
                "data": data,
                "avg_price": avg_price,
                "avg_funding": avg_funding,
                "total_oi": total_oi,
            }

            with open("river_monitor.log", "a") as f:
                f.write(json.dumps(log) + "\n")

            time.sleep(300)

        except Exception as e:
            print("❌ monitor error:", e)
            time.sleep(60)


if __name__ == "__main__":
    monitor()
