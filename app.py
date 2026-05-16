from flask import Flask, request, jsonify
import os
from datetime import datetime

app = Flask(__name__)

TP_PCT = 0.004
SL_PCT = 0.0025
FEE_PCT = 0.00019
ANCHOR_LEVEL = -30
MIN_GAP = 1
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", 1000))

state = {
    "balance": STARTING_BALANCE,
    "in_trade": False,
    "trade_side": None,
    "last_green_dot": None,
    "last_red_dot": None,
    "last_spacer_red_candle": None,
    "last_spacer_green_candle": None,
    "candle_count": 0,
    "trades": [],
    "wins": 0,
    "losses": 0,
    "ties": 0,
}

def fmt(n):
    return f"${n:,.2f}"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print(f"\n📡 Alert: {data}")
        dot = data.get("dot")
        value = float(data.get("value", 0))
        state["candle_count"] += 1
        candle = state["candle_count"]

        if dot == "green":
            prev_green = state["last_green_dot"]
            spacer_candle = state["last_spacer_red_candle"]

            if prev_green and spacer_candle:
                gap = candle - spacer_candle
                if (prev_green["value"] <= ANCHOR_LEVEL and
                        value > prev_green["value"] and
                        gap > MIN_GAP and
                        not state["in_trade"]):
                    print(f"🟢 VALID LONG! Anchor: {prev_green['value']:.2f} Trigger: {value:.2f}")
                    state["in_trade"] = True
                    state["trade_side"] = "LONG"
                else:
                    print(f"⚠️ Green dot {value:.2f} — conditions not met")
            else:
                print(f"⚓ Storing green dot: {value:.2f}")

            state["last_green_dot"] = {"value": value, "candle": candle}

        elif dot == "red":
            prev_red = state["last_red_dot"]
            spacer_candle = state["last_spacer_green_candle"]

            state["last_spacer_red_candle"] = candle

            if prev_red and spacer_candle:
                gap = candle - spacer_candle
                if (prev_red["value"] >= abs(ANCHOR_LEVEL) and
                        value < prev_red["value"] and
                        gap > MIN_GAP and
                        not state["in_trade"]):
                    print(f"🔴 VALID SHORT! Anchor: {prev_red['value']:.2f} Trigger: {value:.2f}")
                    state["in_trade"] = True
                    state["trade_side"] = "SHORT"
                else:
                    print(f"⚠️ Red dot {value:.2f} — conditions not met")
            else:
                print(f"⚓ Storing red dot: {value:.2f}")

            state["last_spacer_green_candle"] = candle
            state["last_red_dot"] = {"value": value, "candle": candle}

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def dashboard():
    total = state["balance"]
    html = f"""<!DOCTYPE html>
<html><head><title>TB-1000 Dashboard</title>
<style>
body{{font-family:sans-serif;background:#0d0d0d;color:#eee;padding:2rem;}}
h1{{color:#00ff88;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:1.5rem 0;}}
.card{{background:#1a1a1a;border-radius:8px;padding:1rem;}}
.label{{font-size:12px;color:#888;margin-bottom:4px;}}
.value{{font-size:22px;font-weight:bold;color:#00ff88;}}
</style></head><body>
<h1>TB-1000 Trading Bot</h1>
<div class='grid'>
<div class='card'><div class='label'>Balance</div><div class='value'>{fmt(state['balance'])}</div></div>
<div class='card'><div class='label'>Wins</div><div class='value'>{state['wins']}</div></div>
<div class='card'><div class='label'>Losses</div><div class='value'>{state['losses']}</div></div>
<div class='card'><div class='label'>Ties</div><div class='value'>{state['ties']}</div></div>
<div class='card'><div class='label'>In Trade</div><div class='value'>{'YES - ' + str(state['trade_side']) if state['in_trade'] else 'No'}</div></div>
<div class='card'><div class='label'>Total Trades</div><div class='value'>{len(state['trades'])}</div></div>
</div>
</body></html>"""
    return html

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
