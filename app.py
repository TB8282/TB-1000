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

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        dot = data.get("dot")
        value = float(data.get("value", 0))
        state["candle_count"] += 1
        candle = state["candle_count"]

        if dot == "green":
            state["last_green_candle"] = candle
            anchor = state["green_anchor"]
            if anchor is None:
                if value <= -ANCHOR_LEVEL:
                    state["green_anchor"] = {"value": value, "candle": candle}
            else:
                if value <= anchor["value"]:
                    # New green dot is lower — update anchor
                    state["green_anchor"] = {"value": value, "candle": candle}
                else:
                    # New green dot is higher — check for LONG entry
                    gap = candle - state["last_red_candle"]
                    if gap > MIN_GAP and not state["in_trade"]:
                        state["in_trade"] = True
                        state["trade_side"] = "LONG"
                        state["green_anchor"] = None
                        state["trades"].append({"time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "side": "LONG", "status": "OPEN"})

        elif dot == "red":
            state["last_red_candle"] = candle
            anchor = state["red_anchor"]
            if anchor is None:
                if value >= ANCHOR_LEVEL:
                    state["red_anchor"] = {"value": value, "candle": candle}
            else:
                if value >= anchor["value"]:
                    # New red dot is higher — update anchor
                    state["red_anchor"] = {"value": value, "candle": candle}
                else:
                    # New red dot is lower — check for SHORT entry
                    gap = candle - state["last_green_candle"]
                    if gap > MIN_GAP and not state["in_trade"]:
                        state["in_trade"] = True
                        state["trade_side"] = "SHORT"
                        state["red_anchor"] = None
                        state["trades"].append({"time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "side": "SHORT", "status": "OPEN"})

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
        rows += "<tr><td>" + t["time"] + "</td><td>" + t["side"] + "</td><td>" + t["status"] + "</td></tr>"
    if not rows:
        rows = "<tr><td colspan='3'>Waiting for signals...</td></tr>"
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
    html += "</div><table><tr><th>Time</th><th>Side</th><th>Status</th></tr>" + rows + "</table></body></html>"
    return html

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
