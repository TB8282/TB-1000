from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime

app = Flask(__name__)

ANCHOR_LEVEL = 30
MIN_GAP = 1
TP_PCT = 0.0045
SL_PCT = 0.0035
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", 500))
STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "balance": STARTING_BALANCE,
        "in_trade": False,
        "trade_side": None,
        "green_anchor": None,
        "red_anchor": None,
        "last_red_candle": 0,
        "last_green_candle": 0,
        "candle_count": 0,
        "trades": [],
        "wins": 0,
        "losses": 0,
        "ties": 0,
    }

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def get_candle_close():
    try:
        url = "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=1"
        response = requests.get(url, timeout=5)
        data = response.json()
        close_price = float(data[0][4])
        return close_price
    except Exception as e:
        print(f"Price fetch error: {e}")
        return None

def check_trade_close(current_price):
    if not state["in_trade"]:
        return
    trade = state["trades"][-1]
    if trade["status"] != "OPEN":
        return
    tp = trade["tp"]
    sl = trade["sl"]
    side = trade["side"]
    entry = trade["entry"]
    result = None
    if side == "LONG":
        if current_price >= tp:
            result = "WIN"
        elif current_price <= sl:
            result = "LOSS"
    elif side == "SHORT":
        if current_price <= tp:
            result = "WIN"
        elif current_price >= sl:
            result = "LOSS"
    if result:
        trade["status"] = result
        trade["close_price"] = current_price
        trade["close_time"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        if side == "LONG":
            if result == "WIN":
                pnl = state["balance"] * TP_PCT
                state["wins"] += 1
            else:
                pnl = -state["balance"] * SL_PCT
                state["losses"] += 1
        elif side == "SHORT":
            if result == "WIN":
                pnl = state["balance"] * TP_PCT
                state["wins"] += 1
            else:
                pnl = -state["balance"] * SL_PCT
                state["losses"] += 1
        state["balance"] += pnl
        trade["pnl"] = round(pnl, 2)
        state["in_trade"] = False
        state["trade_side"] = None
        save_state()

state = load_state()

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        dot = data.get("dot")
        value = float(data.get("value", 0))
        state["candle_count"] += 1
        candle = state["candle_count"]

        # Check if open trade has hit TP or SL
        if state["in_trade"]:
            price = get_candle_close()
            if price:
                check_trade_close(price)

        if dot == "green":
            state["last_green_candle"] = candle
            anchor = state["green_anchor"]
            if anchor is None:
                if value <= -ANCHOR_LEVEL:
                    state["green_anchor"] = {"value": value, "candle": candle}
            else:
                if value <= anchor["value"]:
                    state["green_anchor"] = {"value": value, "candle": candle}
                else:
                    gap = candle - state["last_red_candle"]
                    if gap > MIN_GAP and not state["in_trade"]:
                        price = get_candle_close()
                        if price:
                            tp = round(price * (1 + TP_PCT), 2)
                            sl = round(price * (1 - SL_PCT), 2)
                            state["in_trade"] = True
                            state["trade_side"] = "LONG"
                            state["green_anchor"] = None
                            state["trades"].append({
                                "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                                "side": "LONG",
                                "status": "OPEN",
                                "entry": price,
                                "tp": tp,
                                "sl": sl,
                                "pnl": None,
                            })

        elif dot == "red":
            state["last_red_candle"] = candle
            anchor = state["red_anchor"]
            if anchor is None:
                if value >= ANCHOR_LEVEL:
                    state["red_anchor"] = {"value": value, "candle": candle}
            else:
                if value >= anchor["value"]:
                    state["red_anchor"] = {"value": value, "candle": candle}
                else:
                    gap = candle - state["last_green_candle"]
                    if gap > MIN_GAP and not state["in_trade"]:
                        price = get_candle_close()
                        if price:
                            tp = round(price * (1 - TP_PCT), 2)
                            sl = round(price * (1 + SL_PCT), 2)
                            state["in_trade"] = True
                            state["trade_side"] = "SHORT"
                            state["red_anchor"] = None
                            state["trades"].append({
                                "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                                "side": "SHORT",
                                "status": "OPEN",
                                "entry": price,
                                "tp": tp,
                                "sl": sl,
                                "pnl": None,
                            })

        save_state()
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def dashboard():
    bal = "${:,.2f}".format(state["balance"])
    green = str(round(state["green_anchor"]["value"], 1)) if state["green_anchor"] else "None"
    red = str(round(state["red_anchor"]["value"], 1)) if state["red_anchor"] else "None"
    trade = "YES - " + str(state["trade_side"]) if state["in_trade"] else "No"
    rows = ""
    for t in reversed(state["trades"][-10:]):
        entry = "${:,.2f}".format(t["entry"]) if t.get("entry") else "-"
        tp = "${:,.2f}".format(t["tp"]) if t.get("tp") else "-"
        sl = "${:,.2f}".format(t["sl"]) if t.get("sl") else "-"
        pnl = "${:,.2f}".format(t["pnl"]) if t.get("pnl") is not None else "-"
        rows += "<tr><td>" + t["time"] + "</td><td>" + t["side"] + "</td><td>" + t["status"] + "</td><td>" + entry + "</td><td>" + tp + "</td><td>" + sl + "</td><td>" + pnl + "</td></tr>"
    if not rows:
        rows = "<tr><td colspan='7'>Waiting for signals...</td></tr>"
    html = "<html><head><title>TB-1000</title><style>body{background:#0d0d0d;color:#eee;font-family:sans-serif;padding:2rem;}h1{color:#00ff88;}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1rem 0;}.c{background:#1a1a1a;border-radius:8px;padding:1rem;}.l{font-size:12px;color:#888;}.v{font-size:20px;font-weight:bold;color:#00ff88;}table{width:100%;border-collapse:collapse;margin-top:1rem;}th,td{padding:8px;border-bottom:1px solid #333;text-align:left;}</style></head><body>"
    html += "<h1>TB-1000 Trading Bot</h1><div class='g'>"
    html += "<div class='c'><div class='l'>Balance</div><div class='v'>" + bal + "</div></div>"
    html += "<div class='c'><div class='l'>Wins</div><div class='v'>" + str(state["wins"]) + "</div></div>"
    html += "<div class='c'><div class='l'>Losses</div><div class='v'>" + str(state["losses"]) + "</div></div>"
    html += "<div class='c'><div class='l'>Ties</div><div class='v'>" + str(state["ties"]) + "</div></div>"
    html += "<div class='c'><div class='l'>In Trade</div><div class='v'>" + trade + "</div></div>"
    html += "<div class='c'><div class='l'>Green Anchor</div><div class='v'>" + green + "</div></div>"
    html += "<div class='c'><div class='l'>Red Anchor</div><div class='v'>" + red + "</div></div>"
    html += "<div class='c'><div class='l'>Total Signals</div><div class='v'>" + str(len(state["trades"])) + "</div></div>"
    html += "</div><table><tr><th>Time</th><th>Side</th><th>Status</th><th>Entry</th><th>TP</th><th>SL</th><th>PnL</th></tr>" + rows + "</table></body></html>"
    return html

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
