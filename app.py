from flask import Flask, request, jsonify
import os
from datetime import datetime

app = Flask(__name__)

TP_PCT = 0.004
SL_PCT = 0.0025
FEE_PCT = 0.00019
ANCHOR_LEVEL = 30
MIN_GAP = 1
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", 500))

state = {
    "balance": STARTING_BALANCE,
    "in_trade": False,
    "trade_side": None,
    "green_anchor": None,
    "red_anchor": None,
    "last_red_candle": None,
    "last_green_candle": None,
    "candle_count": 0,
    "trades": [],
    "wins": 0,
    "losses": 0,
    "ties": 0,
}

def fmt(n):
    return "${:,.2f}".format(n)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print("Alert: {}".format(data))
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
                    print("GREEN anchor stored: {:.2f}".format(value))
                else:
                    print("Green dot {:.2f} too high for anchor".format(value))
            e
