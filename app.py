from flask import Flask, request, jsonify
import os
from datetime import datetime

app = Flask(__name__)

ANCHOR_LEVEL = 30
MIN_GAP = 1
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", 500))

state = {
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

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.get_data(as_text=True)
        print("Raw webhook: " + raw)
        
        try:
            data = request.get_json(force=True, silent=True)
            if not data:
                print("ERROR: Could not parse JSON")
                return jsonify({"error": "invalid json"}), 400
        except Exception as e:
            print("JSON parse error: " + str(e))
            return jsonify({"error": "json error"}), 400

        dot = str(data.get("dot", "")).lower().strip()
        raw_value = data.get("value", data.get("p1", 0))
        value = safe_float(raw_value)
        
        if value is None:
            print("ERROR: Could not convert value to float: " + str(raw_value))
            return jsonify({"error": "invalid value"}), 400

        print("Dot: " + dot + " Value: " + str(value))
        
        state["candle_count"] += 1
        candle = state["candle_count"]

        if dot == "green":
            state["last_green_candle"] = candle
            anchor = state["green_anchor"]

            if anchor is None:
                if value <= -ANCHOR_LEVEL:
                    state["green_anchor"] = {"value": value, "candle": candle}
                    print("GREEN anchor stored: " + str(round(value, 2)))
                else:
                    print("Green dot " + str(round(value, 2)) + " not deep enough for anchor")
            else:
                if value > anchor["value"]:
                    gap = candle - state["last_red_candle"]
                    if gap > MIN_GAP and not state["in_trade"]:
                        print("VALID LONG! Anchor: " + str(round(anchor["value"], 2)) + " Trigger: " + str(round(value, 2)))
                        state["in_trade"] = True
                        state["trade_side"] = "LONG"
                        state["green_anchor"] = None
                        state["trades"].append({
                            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                            "side": "LONG",
                            "status": "OPEN"
                        })
                    elif state["in_trade"]:
                        print("Already in trade - ignored")
                    else:
                        print("Gap too small (" + str(gap) + ") - ignored")
                    if value <= -ANCHOR_LEVEL:
                        state["green_anchor"] = {"value": value, "candle": candle}
                        print("GREEN anchor updated: " + str(round(value, 2)))
                else:
                    print("GREEN anchor cancelled - " + str(round(value, 2)) + " lower than " + str(round(anchor["value"], 2)))
                    if value <= -ANCHOR_LEVEL:
                        state["green_anchor"] = {"value": value, "candle": candle}
                        print("NEW GREEN anchor: " + str(round(value, 2)))
                    else:
                        state["green_anchor"] = None

        elif dot == "red":
            state["last_red_candle"] = candle
            anchor = state["red_anchor"]

            if anchor is None:
                if value >= ANCHOR_LEVEL:
                    state["red_anchor"] = {"value": value, "candle": candle}
                    print("RED anchor stored: " + str(round(value, 2)))
                else:
                    print("Red dot " + str(round(value, 2)) + " not high enough for anchor")
            else:
                if value < anchor["value"]:
                    gap = candle - state["last_green_candle"]
                    if gap > MIN_GAP and not state["in_trade"]:
                        print("VALID SHORT! Anchor: " + str(round(anchor["value"], 2)) + " Trigger: " + str(round(value, 2)))
                        state["in_trade"] = True
                        state["trade_side"] = "SHORT"
                        state["red_anchor"] = None
                        state["trades"].append({
                            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                            "side": "SHORT",
                            "status": "OPEN"
                        })
                    elif state["in_trade"]:
                        print("Already in trade - ignored")
                    else:
                        print("Gap too small (" + str(gap) + ") - ignored")
                    if value >= ANCHOR_LEVEL:
                        state["red_anchor"] = {"value": value, "candle": candle}
                        print("RED anchor updated: " + str(round(value, 2)))
                else:
                    print("RED anchor cancelled - " + str(round(value, 2)) + " higher than " + str(round(anchor["value"], 2)))
                    if value >= ANCHOR_LEVEL:
                        state["red_anchor"] = {"value": value, "candle": candle}
                        print("NEW RED anchor: " + str(round(value, 2)))
                    else:
                        state["red_anchor"] = None
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
            color = "green" if status == "WIN" else "red" if status == "LOSS" else "#aaa"
            rows += "<tr><td>" + t["time"] + "</td><td>" + t["side"] + "</td><td style='color:" + color + "'>" + status + "</td></tr>"
        if not rows:
            rows = "<tr><td colspan='3' style='color:#555'>Waiting for signals...</td></tr>"

        green = str(round(state["green_anchor"]["value"], 1)) if state["green_anchor"] else "None"
        red = str(round(state["red_anchor"]["value"], 1)) if state["red_anchor"] else "None"
        trade = "YES - " + str(state["trade_side"]) if state["in_trade"] else "No"

        html = (
            "<!DOCTYPE html><html><head><title>TB-1000</title>"
            "<meta http-equiv='refresh' content='30'>"
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
            "<div class='c'><div class='l'>In Trade</div><div class='v'>" + trade + "</div></div>"
            "<div class='c'><div class='l'>Green Anchor</div><div class='v'>" + green + "</div></div>"
            "<div class='c'><div class='l'>Red Anchor</div><div class='v'>" + red + "</div></div>"
            "<div class='c'><div class='l'>Total Signals</div><div class='v'>" + str(len(state["trades"])) + "</div></div>"
            "<div class='c'><div class='l'>Candles seen</div><div class='v'>" + str(state["candle_count"]) + "</div></div>"
            "</div>"
            "<table><tr><th>Time</th><th>Side</th><th>Status</th></tr>"
            + rows +
            "</table>"
            "<p style='color:#555;font-size:11px;margin-top:1rem'>Auto-refreshes every 30 seconds</p>"
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
    state["in_trade"] = False
    state["trade_side"] = None
    state["green_anchor"] = None
    state["red_anchor"] = None
    print("State reset by user")
    return jsonify({"status": "reset ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
