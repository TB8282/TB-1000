from flask import Flask, request, jsonify
import os
import threading
import time
import requests
from datetime import datetime

app = Flask(__name__)

ANCHOR_LEVEL = 30
TRIGGER_MAX_GREEN = 5
TRIGGER_MIN_RED = -5
TP_PCT = 0.0045
SL_PCT = 0.0035
FEE = 0.20
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", 500))

state = {
    "balance": STARTING_BALANCE,
    "in_trade": False,
    "trade_side": None,
    "entry_price": None,
    "tp_price": None,
    "sl_price": None,
    "green_anchor": None,
    "red_anchor": None,
    "candle_count": 0,
    "trades": [],
    "wins": 0,
    "losses": 0,
}

state_lock = threading.Lock()


def fmt(n):
    try:
        return "${:,.2f}".format(float(n))
    except:
        return "$0.00"


def safe_float(val):
    try:
        return float(val)
    except:
        return None


def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3)
        return float(r.json()["price"])
    except:
        return None


def close_trade(result, exit_price):
    with state_lock:
        if not state["in_trade"]:
            return
        side = state["trade_side"]
        entry = state["entry_price"]
        balance = state["balance"]

        if result == "WIN":
            pnl = round(balance * TP_PCT - FEE, 2)
        else:
            pnl = round(-(balance * SL_PCT) - FEE, 2)

        state["balance"] = round(balance + pnl, 2)

        if result == "WIN":
            state["wins"] += 1
        else:
            state["losses"] += 1

        print(f"TRADE CLOSED: {result} | Side: {side} | Entry: {entry} | Exit: {exit_price} | PnL: {fmt(pnl)} | Balance: {fmt(state['balance'])}")

        for t in reversed(state["trades"]):
            if t["status"] == "OPEN":
                t["status"] = result
                t["exit_price"] = exit_price
                t["pnl"] = pnl
                t["balance_after"] = state["balance"]
                break

        state["in_trade"] = False
        state["trade_side"] = None
        state["entry_price"] = None
        state["tp_price"] = None
        state["sl_price"] = None


def price_watcher():
    while True:
        time.sleep(1)
        with state_lock:
            if not state["in_trade"]:
                continue
            tp = state["tp_price"]
            sl = state["sl_price"]
            side = state["trade_side"]

        price = get_btc_price()
        if price is None:
            continue

        if side == "LONG":
            if price >= tp:
                close_trade("WIN", price)
            elif price <= sl:
                close_trade("LOSS", price)
        elif side == "SHORT":
            if price <= tp:
                close_trade("WIN", price)
            elif price >= sl:
                close_trade("LOSS", price)


def open_trade(side, entry_price, candle_time):
    balance = state["balance"]

    if side == "LONG":
        tp = round(entry_price * (1 + TP_PCT), 2)
        sl = round(entry_price * (1 - SL_PCT), 2)
    else:
        tp = round(entry_price * (1 - TP_PCT), 2)
        sl = round(entry_price * (1 + SL_PCT), 2)

    state["in_trade"] = True
    state["trade_side"] = side
    state["entry_price"] = entry_price
    state["tp_price"] = tp
    state["sl_price"] = sl

    potential_win = round(balance * TP_PCT - FEE, 2)
    potential_loss = round(-(balance * SL_PCT) - FEE, 2)

    state["trades"].append({
        "time": candle_time,
        "side": side,
        "entry_price": entry_price,
        "tp_price": tp,
        "sl_price": sl,
        "potential_win": potential_win,
        "potential_loss": potential_loss,
        "status": "OPEN",
        "exit_price": None,
        "pnl": None,
        "balance_after": None,
    })
    print(f"TRADE OPENED: {side} | Entry: {entry_price} | TP: {tp} | SL: {sl} | Win: {fmt(potential_win)} | Loss: {fmt(potential_loss)}")


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.get_data(as_text=True)
        print("Raw webhook: " + raw)

        data = request.get_json(force=True, silent=True)
        if not data:
            print("ERROR: Could not parse JSON")
            return jsonify({"error": "invalid json"}), 400

        dot = str(data.get("dot", "")).lower().strip()
        value = safe_float(data.get("value", 0))
        close_price = safe_float(data.get("close", None))

        if value is None:
            print("ERROR: invalid value")
            return jsonify({"error": "invalid value"}), 400

        if close_price is None:
            print("ERROR: missing close price")
            return jsonify({"error": "invalid close"}), 400

        print(f"Dot: {dot} | Value: {round(value, 2)} | Close: {close_price}")

        with state_lock:
            state["candle_count"] += 1
            candle = state["candle_count"]
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

            if dot == "green":
                anchor = state["green_anchor"]

                if anchor is None:
                    if value <= -ANCHOR_LEVEL:
                        state["green_anchor"] = {"value": value, "candle": candle}
                        print(f"GREEN anchor stored: {round(value, 2)}")
                    else:
                        print(f"Green dot {round(value, 2)} ignored - not deep enough")
                else:
                    if value > anchor["value"]:
                        if value > TRIGGER_MAX_GREEN:
                            print(f"GREEN trigger too high ({round(value, 2)}) - anchor kept")
                        elif state["in_trade"]:
                            print("Already in trade - ignored")
                        else:
                            print(f"VALID LONG! Anchor: {round(anchor['value'], 2)} Trigger: {round(value, 2)}")
                            open_trade("LONG", close_price, now)
                            state["green_anchor"] = None
                            state["red_anchor"] = None
                    else:
                        if value <= -ANCHOR_LEVEL:
                            state["green_anchor"] = {"value": value, "candle": candle}
                            print(f"NEW GREEN anchor: {round(value, 2)}")
                        else:
                            print(f"Green dot {round(value, 2)} ignored - anchor kept at {round(anchor['value'], 2)}")

            elif dot == "red":
                anchor = state["red_anchor"]

                if anchor is None:
                    if value >= ANCHOR_LEVEL:
                        state["red_anchor"] = {"value": value, "candle": candle}
                        print(f"RED anchor stored: {round(value, 2)}")
                    else:
                        print(f"Red dot {round(value, 2)} ignored - not high enough")
                else:
                    if value < anchor["value"]:
                        if value < TRIGGER_MIN_RED:
                            print(f"RED trigger too low ({round(value, 2)}) - anchor kept")
                        elif state["in_trade"]:
                            print("Already in trade - ignored")
                        else:
                            print(f"VALID SHORT! Anchor: {round(anchor['value'], 2)} Trigger: {round(value, 2)}")
                            open_trade("SHORT", close_price, now)
                            state["red_anchor"] = None
                            state["green_anchor"] = None
                    else:
                        if value >= ANCHOR_LEVEL:
                            state["red_anchor"] = {"value": value, "candle": candle}
                            print(f"NEW RED anchor: {round(value, 2)}")
                        else:
                            print(f"Red dot {round(value, 2)} ignored - anchor kept at {round(anchor['value'], 2)}")

            else:
                print("Unknown dot type: " + dot)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("CRITICAL ERROR: " + str(e))
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def dashboard():
    try:
        recent = list(reversed(state["trades"][-10:]))
        rows = ""
        for t in recent:
            status = t.get("status", "OPEN")
            color = "#00ff88" if status == "WIN" else "red" if status == "LOSS" else "#aaa"
            entry = fmt(t.get("entry_price", 0))
            tp = fmt(t.get("tp_price", 0))
            sl = fmt(t.get("sl_price", 0))
            pnl = fmt(t.get("pnl", 0)) if t.get("pnl") is not None else "-"
            bal = fmt(t.get("balance_after", 0)) if t.get("balance_after") is not None else "-"
            rows += (
                "<tr>"
                "<td>" + t["time"] + "</td>"
                "<td>" + t["side"] + "</td>"
                "<td>" + entry + "</td>"
                "<td style='color:#00ff88'>" + tp + "</td>"
                "<td style='color:red'>" + sl + "</td>"
                "<td style='color:" + color + "'>" + status + "</td>"
                "<td>" + pnl + "</td>"
                "<td>" + bal + "</td>"
                "</tr>"
            )
        if not rows:
            rows = "<tr><td colspan='8' style='color:#555'>Waiting for signals...</td></tr>"

        green = str(round(state["green_anchor"]["value"], 1)) if state["green_anchor"] else "None"
        red = str(round(state["red_anchor"]["value"], 1)) if state["red_anchor"] else "None"
        trade = "YES - " + str(state["trade_side"]) if state["in_trade"] else "No"
        tp_display = fmt(state["tp_price"]) if state["tp_price"] else "-"
        sl_display = fmt(state["sl_price"]) if state["sl_price"] else "-"

        total_trades = state["wins"] + state["losses"]
        win_rate = str(round(state["wins"] / total_trades * 100)) + "%" if total_trades > 0 else "-"

        html = (
            "<!DOCTYPE html><html><head><title>TB-1000</title>"
            "<meta http-equiv='refresh' content='10'>"
            "<style>"
            "body{background:#0d0d0d;color:#eee;font-family:sans-serif;padding:2rem;}"
            "h1{color:#00ff88;}"
            ".g{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1rem 0;}"
            ".c{background:#1a1a1a;border-radius:8px;padding:1rem;}"
            ".l{font-size:12px;color:#888;margin-bottom:4px;}"
            ".v{font-size:20px;font-weight:bold;color:#00ff88;}"
            "table{width:100%;border-collapse:collapse;margin-top:1rem;}"
            "th,td{padding:8px;border-bottom:1px solid #333;text-align:left;font-size:13px;}"
            "th{color:#888;font-size:12px;}"
            "</style></head><body>"
            "<h1>TB-1000 Trading Bot</h1>"
            "<div class='g'>"
            "<div class='c'><div class='l'>Balance</div><div class='v'>" + fmt(state["balance"]) + "</div></div>"
            "<div class='c'><div class='l'>Wins</div><div class='v'>" + str(state["wins"]) + "</div></div>"
            "<div class='c'><div class='l'>Losses</div><div class='v'>" + str(state["losses"]) + "</div></div>"
            "<div class='c'><div class='l'>Win Rate</div><div class='v'>" + win_rate + "</div></div>"
            "<div class='c'><div class='l'>In Trade</div><div class='v'>" + trade + "</div></div>"
            "<div class='c'><div class='l'>Live TP</div><div class='v'>" + tp_display + "</div></div>"
            "<div class='c'><div class='l'>Live SL</div><div class='v'>" + sl_display + "</div></div>"
            "<div class='c'><div class='l'>Green Anchor</div><div class='v'>" + green + "</div></div>"
            "<div class='c'><div class='l'>Red Anchor</div><div class='v'>" + red + "</div></div>"
            "<div class='c'><div class='l'>Candles Seen</div><div class='v'>" + str(state["candle_count"]) + "</div></div>"
            "</div>"
            "<table><tr><th>Time</th><th>Side</th><th>Entry</th><th>TP</th><th>SL</th><th>Status</th><th>PnL</th><th>Balance</th></tr>"
            + rows +
            "</table>"
            "<p style='color:#555;font-size:11px;margin-top:1rem'>Auto-refreshes every 10 seconds</p>"
            "</body></html>"
        )
        return html
    except Exception as e:
        return "Dashboard error: " + str(e), 500


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


@app.route("/state", methods=["GET"])
def get_state():
    return jsonify(state), 200


@app.route("/reset", methods=["POST"])
def reset():
    with state_lock:
        state["in_trade"] = False
        state["trade_side"] = None
        state["entry_price"] = None
        state["tp_price"] = None
        state["sl_price"] = None
        state["green_anchor"] = None
        state["red_anchor"] = None
    print("State reset by user")
    return jsonify({"status": "reset ok"}), 200


if __name__ == "__main__":
    watcher = threading.Thread(target=price_watcher, daemon=True)
    watcher.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
