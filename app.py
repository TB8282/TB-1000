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
            state["last_green_candle"] = candle
            anchor = state["green_anchor"]

            if anchor is None:
                # No anchor yet
                if value <= -ANCHOR_LEVEL:
                    state["green_anchor"] = {"value": value, "candle": candle}
                    print(f"⚓ GREEN anchor stored: {value:.2f}")
                else:
                    print(f"⚠️ Green dot {value:.2f} too high for anchor")
            else:
                if value > anchor["value"]:
                    # Check gap rule
                    gap = candle - state.get("last_red_candle", 0)
                    if gap > MIN_GAP and not state["in_trade"]:
                        print(f"🟢 VALID LONG! Anchor: {anchor['value']:.2f} Trigger: {value:.2f}")
                        state["in_trade"] = True
                        state["trade_side"] = "LONG"
                        state["green_anchor"] = None
                        trade = {
                            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                            "side": "LONG",
                            "entry": value,
                            "status": "OPEN"
                        }
                        state["trades"].append(trade)
                    elif state["in_trade"]:
                        print(f"⚠️ Already in trade — signal ignored")
                    else:
                        print(f"⚠️ Gap too small — signal ignored")
                    # Update anchor regardless
                    if value <= -ANCHOR_LEVEL:
                        state["green_anchor"] = {"value": value, "candle": candle}
                        print(f"⚓ GREEN anchor updated: {value:.2f}")
                else:
                    # New dot lower than anchor — cancel and restart
                    print(f"🔄 GREEN anchor cancelled — new dot {value:.2f} lower than anchor {anchor['value']:.2f}")
                    if value <= -ANCHOR_LEVEL:
                        state["green_anchor"] = {"value": value, "candle": candle}
                        print(f"⚓ NEW GREEN anchor stored: {value:.2f}")
                    else:
                        state["green_anchor"] = None

        elif dot == "red":
            state["last_red_candle"] = candle
            anchor = state["red_anchor"]

            if anchor is None:
                if value >= ANCHOR_LEVEL:
                    state["red_anchor"] = {"value": value, "candle": candle}
                    print(f"⚓ RED anchor stored: {value:.2f}")
                else:
                    print(f"⚠️ Red dot {value:.2f} too low for anchor")
            else:
                if value < anchor["value"]:
                    gap = candle - state.get("last_green_candle", 0)
                    if gap > MIN_GAP and not state["in_trade"]:
                        print(f"🔴 VALID SHORT! Anchor: {anchor['value']:.2f} Trigger: {value:.2f}")
                        state["in_trade"] = True
                        state["trade_side"] = "SHORT"
                        state["red_anchor"] = None
                        trade = {
                            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                            "side": "SHORT",
                            "entry": value,
                            "status": "OPEN"
                        }
                        state["trades"].append(trade)
                    elif state["in_trade"]:
                        print(f"⚠️ Already in trade — signal ignored")
                    else:
                        print(f"⚠️ Gap too small — signal ignored")
                    if value >= ANCHOR_LEVEL:
                        state["red_anchor"] = {"value": value, "candle": candle}
                        print(f"⚓ RED anchor updated: {value:.2f}")
                else:
                    print(f"🔄 RED anchor cancelled — new dot {value:.2f} higher than anchor {anchor['value']:.2f}")
                    if value >= ANCHOR_LEVEL:
                        state["red_anchor"] = {"value": value, "candle": candle}
                        print(f"⚓ NEW RED anchor stored: {value:.2f}")
                    else:
                        state["red_anchor"] = None

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def dashboard():
    recent = state["trades"][-10:][::-1]
    rows = ""
    for t in recent:
        color = "green" if t.get("status") == "WIN" else "red" if t.get("status") == "LOSS" else "#aaa"
        rows += f"<tr><td>{t['time']}</td><td>{t['side']}</td><td style='color:{color}'>{t.get('status','OPEN')}</td></tr>"

    html = f"""<!DOCTYPE html>
<html><head><title>TB-1000 Dashboard</title>
<style>
body{{font-family:sans-serif;background:#0d0d0d;color:#eee;padding:2rem;}}
h1{{color:#00ff88;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:1.5rem 0;}}
.card{{background:#1a1a1a;border-radius:8px;padding:1rem;}}
.label{{font-size:12px;color:#888;margin-bottom:4px;}}
.value{{font-size:22px;font-
