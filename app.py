from flask import Flask, request, jsonify
import os
import threading
import time
import psycopg2
from datetime import datetime
from kraken_client import KrakenClient

app = Flask(__name__)

# ============ CONFIRMED RULES (tonight's session) ============
ANCHOR_LEVEL = 35          # was 30 - green anchor <= -35, red anchor >= +35
TRIGGER_MAX_GREEN = 15     # was 5 - green trigger must be <= +15
TRIGGER_MIN_RED = -15      # was -5 - red trigger must be >= -15
TP_PCT = 0.0050            # 0.50%
SL_PCT = 0.0050            # 0.50%
LEVERAGE = 10              # confirmed max via Kraken API tonight
BALANCE_SAFETY_PCT = 0.95  # use 95% of balance*leverage to avoid Kraken's
                           # buying-power rejection ("Cannot be greater than X USD")
PAIR = "XBTUSD"
# NOTE: VOLUME is no longer fixed - it's now calculated fresh before every
# trade based on your ACTUAL current Kraken balance. See calculate_volume().

KRAKEN_API_KEY = os.environ.get("KRAKEN_API_KEY")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")

kraken = KrakenClient(KRAKEN_API_KEY, KRAKEN_API_SECRET)

state = {
    "in_trade": False,
    "trade_side": None,
    "entry_price": None,
    "tp_price": None,
    "sl_price": None,
    "tp_txid": None,
    "sl_txid": None,
    "entry_time": None,
    "volume": None,
    "green_anchor": None,
    "red_anchor": None,
    "candle_count": 0,
    "wins": 0,
    "losses": 0,
}
state_lock = threading.Lock()


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kraken_bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kraken_trades (
                id SERIAL PRIMARY KEY,
                time TEXT,
                side TEXT,
                entry_price REAL,
                tp_price REAL,
                sl_price REAL,
                tp_txid TEXT,
                sl_txid TEXT,
                status TEXT,
                exit_price REAL,
                closed_time TEXT
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Kraken DB tables initialized")
    except Exception as e:
        print(f"DB init error: {e}")


def save_state():
    try:
        conn = get_db()
        cur = conn.cursor()
        for key in ["in_trade", "trade_side", "entry_price", "tp_price",
                    "sl_price", "tp_txid", "sl_txid", "entry_time", "volume",
                    "wins", "losses"]:
            cur.execute("""
                INSERT INTO kraken_bot_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, str(state.get(key))))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB save error: {e}")


def save_trade(t):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kraken_trades
            (time, side, entry_price, tp_price, sl_price, tp_txid, sl_txid, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (t["time"], t["side"], t["entry_price"], t["tp_price"], t["sl_price"],
              t["tp_txid"], t["sl_txid"], "OPEN"))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB save trade error: {e}")


def calculate_volume():
    """
    Calculates position size using 95% of your FULL current Kraken
    balance, at the confirmed 10x leverage. This is what makes the bot
    actually use "full balance per trade, compounding" like every
    backtest tonight assumed - it queries your REAL balance right
    before each trade, not a fixed number.

    The 5% haircut (BALANCE_SAFETY_PCT) exists because Kraken rejects
    orders that exceed your true buying power (confirmed live: a $100
    balance at 10x showed a hard cap of $980 available to trade, not
    a full $1,000) - this buffer keeps every order safely under that
    ceiling instead of risking an ENTRY ORDER FAILED abort.

    volume (in BTC) = (balance_usd * BALANCE_SAFETY_PCT * leverage) / current_btc_price
    """
    bal_result = kraken.get_balance()
    if bal_result.get("error"):
        print(f"BALANCE CHECK FAILED: {bal_result['error']}")
        return None
    balance_data = bal_result.get("result", {})
    # ZUSD is Kraken's code for USD balance
    usd_balance = float(balance_data.get("ZUSD", 0))
    if usd_balance <= 0:
        print(f"WARNING: USD balance is {usd_balance} - nothing to trade with.")
        return None

    ticker = kraken.get_ticker(PAIR)
    try:
        current_price = float(list(ticker["result"].values())[0]["c"][0])
    except Exception as e:
        print(f"Could not fetch current price for volume calc: {e}")
        return None

    notional = usd_balance * BALANCE_SAFETY_PCT * LEVERAGE
    volume = round(notional / current_price, 8)
    print(f"Volume calc: balance=${usd_balance:.2f} | safety={BALANCE_SAFETY_PCT} | "
          f"price=${current_price:.1f} | notional=${notional:.2f} | volume={volume} BTC")
    return volume


def open_trade(side, webhook_close_price, candle_time):
    """
    Places the real entry order on Kraken, then queries Kraken for the
    ACTUAL fill price (not the webhook's close price, which may be HA-
    distorted if the chart uses Heikin Ashi candles). TP/SL are
    calculated from the real fill price, so HA vs regular candle
    differences don't affect trade accuracy - only the DOT SIGNAL
    itself comes from the chart; the price math comes from Kraken.
    """
    entry_side = "buy" if side == "LONG" else "sell"
    exit_side = "sell" if side == "LONG" else "buy"

    VOLUME = calculate_volume()
    if not VOLUME:
        print("TRADE ABORTED: could not calculate volume from live balance.")
        return

    # STEP 1: Place entry order (market order, fills near-instantly)
    entry_result = kraken.place_entry_order(PAIR, entry_side, VOLUME, LEVERAGE)
    if entry_result.get("error"):
        print(f"ENTRY ORDER FAILED: {entry_result['error']}")
        return
    print(f"Entry order placed: {entry_result}")

    entry_txid = entry_result.get("result", {}).get("txid", [None])[0]
    if not entry_txid:
        print("ENTRY FAILED: no txid returned, cannot proceed")
        return

    # STEP 2: Poll briefly for the real fill price (market orders fill fast,
    # but not always instantly - retry a few times before giving up)
    entry_price = None
    for attempt in range(5):
        time.sleep(1)
        order_info = kraken.query_orders([entry_txid])
        order_data = order_info.get("result", {}).get(entry_txid, {})
        if order_data.get("status") == "closed":
            entry_price = float(order_data.get("price", 0))
            break

    if not entry_price:
        print("WARNING: Could not confirm real fill price after 5 attempts. "
              f"Falling back to webhook close price ({webhook_close_price}) - "
              "this may be HA-distorted if chart uses Heikin Ashi.")
        entry_price = webhook_close_price
    else:
        print(f"Confirmed REAL fill price from Kraken: {entry_price} "
              f"(webhook sent: {webhook_close_price})")

    if side == "LONG":
        tp = round(entry_price * (1 + TP_PCT), 1)
        sl = round(entry_price * (1 - SL_PCT), 1)
    else:
        tp = round(entry_price * (1 - TP_PCT), 1)
        sl = round(entry_price * (1 + SL_PCT), 1)

    # STEP 3: Place TP order (standalone conditional)
    tp_result = kraken.place_close_order(PAIR, exit_side, VOLUME, "take-profit", tp, LEVERAGE)
    tp_txid = None
    if tp_result.get("error"):
        print(f"TP ORDER FAILED: {tp_result['error']}")
    else:
        tp_txid = tp_result.get("result", {}).get("txid", [None])[0]

    # STEP 3: Place SL order (standalone conditional)
    sl_result = kraken.place_close_order(PAIR, exit_side, VOLUME, "stop-loss", sl, LEVERAGE)
    sl_txid = None
    if sl_result.get("error"):
        print(f"SL ORDER FAILED: {sl_result['error']}")
    else:
        sl_txid = sl_result.get("result", {}).get("txid", [None])[0]

    with state_lock:
        state["in_trade"] = True
        state["trade_side"] = side
        state["entry_price"] = entry_price
        state["tp_price"] = tp
        state["sl_price"] = sl
        state["tp_txid"] = tp_txid
        state["sl_txid"] = sl_txid
        state["entry_time"] = candle_time
        state["volume"] = VOLUME

    save_trade({
        "time": candle_time, "side": side, "entry_price": entry_price,
        "tp_price": tp, "sl_price": sl, "tp_txid": tp_txid, "sl_txid": sl_txid,
    })
    save_state()
    print(f"TRADE OPENED: {side} | Entry: {entry_price} | TP: {tp} ({tp_txid}) | SL: {sl} ({sl_txid})")
    print("NOTE: worker.py must poll tp_txid/sl_txid and cancel whichever doesn't fill.")


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "invalid json"}), 400
        dot = str(data.get("dot", "")).lower().strip()
        value = safe_float(data.get("value", 0))
        close_price = safe_float(data.get("close", None))
        if value is None or close_price is None:
            return jsonify({"error": "invalid payload"}), 400

        print(f"Dot: {dot} | Value: {round(value, 2)} | Close: {close_price}")

        with state_lock:
            state["candle_count"] += 1
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

            if dot == "green":
                anchor = state["green_anchor"]
                if anchor is None:
                    if value <= -ANCHOR_LEVEL:
                        state["green_anchor"] = {"value": value}
                        print(f"GREEN anchor stored: {round(value, 2)}")
                elif value > anchor["value"]:
                    if value > TRIGGER_MAX_GREEN:
                        print(f"GREEN trigger too high ({round(value,2)}) - anchor kept")
                    elif state["in_trade"]:
                        print("Already in trade - ignored")
                    else:
                        print(f"VALID LONG! Anchor: {round(anchor['value'],2)} Trigger: {round(value,2)}")
                        open_trade("LONG", close_price, now)
                        # NOTE: anchor is NOT cleared here - confirmed rule tonight
                elif value <= -ANCHOR_LEVEL:
                    state["green_anchor"] = {"value": value}
                    print(f"NEW GREEN anchor: {round(value, 2)}")

            elif dot == "red":
                anchor = state["red_anchor"]
                if anchor is None:
                    if value >= ANCHOR_LEVEL:
                        state["red_anchor"] = {"value": value}
                        print(f"RED anchor stored: {round(value, 2)}")
                elif value < anchor["value"]:
                    if value < TRIGGER_MIN_RED:
                        print(f"RED trigger too low ({round(value,2)}) - anchor kept")
                    elif state["in_trade"]:
                        print("Already in trade - ignored")
                    else:
                        print(f"VALID SHORT! Anchor: {round(anchor['value'],2)} Trigger: {round(value,2)}")
                        open_trade("SHORT", close_price, now)
                elif value >= ANCHOR_LEVEL:
                    state["red_anchor"] = {"value": value}
                    print(f"NEW RED anchor: {round(value, 2)}")

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def fmt(n):
    try:
        return "${:,.2f}".format(float(n))
    except (TypeError, ValueError):
        return "$0.00"


def load_trades():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT time, side, entry_price, tp_price, sl_price, status, exit_price
            FROM kraken_trades ORDER BY id DESC LIMIT 10
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"Load trades error: {e}")
        return []


@app.route("/", methods=["GET"])
def dashboard():
    bal_result = kraken.get_balance()
    balance_data = bal_result.get("result", {})
    usd_balance = float(balance_data.get("ZUSD", 0))

    rows_html = ""
    for t in load_trades():
        time_, side, entry, tp, sl, status, exit_price = t
        color = "#00ff88" if status == "WIN" else "red" if status == "LOSS" else "#aaa"
        exit_display = fmt(exit_price) if exit_price else "-"
        rows_html += (
            "<tr>"
            f"<td>{time_}</td><td>{side}</td>"
            f"<td>{fmt(entry)}</td>"
            f"<td style='color:#00ff88'>{fmt(tp)}</td>"
            f"<td style='color:red'>{fmt(sl)}</td>"
            f"<td style='color:{color}'>{status}</td>"
            f"<td>{exit_display}</td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='7' style='color:#555'>Waiting for signals...</td></tr>"

    green = str(round(state["green_anchor"]["value"], 1)) if state["green_anchor"] else "None"
    red = str(round(state["red_anchor"]["value"], 1)) if state["red_anchor"] else "None"
    trade = f"YES - {state['trade_side']}" if state["in_trade"] else "No"
    tp_display = fmt(state["tp_price"]) if state["tp_price"] else "-"
    sl_display = fmt(state["sl_price"]) if state["sl_price"] else "-"
    total = state["wins"] + state["losses"]
    win_rate = f"{round(state['wins']/total*100)}%" if total > 0 else "-"

    html = (
        "<!DOCTYPE html><html><head><title>TB-1000 Kraken</title>"
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
        "<h1>TB-1000 Kraken Bot (10x Leverage)</h1>"
        "<div class='g'>"
        f"<div class='c'><div class='l'>Kraken Balance (USD)</div><div class='v'>{fmt(usd_balance)}</div></div>"
        f"<div class='c'><div class='l'>Wins</div><div class='v'>{state['wins']}</div></div>"
        f"<div class='c'><div class='l'>Losses</div><div class='v'>{state['losses']}</div></div>"
        f"<div class='c'><div class='l'>Win Rate</div><div class='v'>{win_rate}</div></div>"
        f"<div class='c'><div class='l'>In Trade</div><div class='v'>{trade}</div></div>"
        f"<div class='c'><div class='l'>Live TP</div><div class='v'>{tp_display}</div></div>"
        f"<div class='c'><div class='l'>Live SL</div><div class='v'>{sl_display}</div></div>"
        f"<div class='c'><div class='l'>Green Anchor</div><div class='v'>{green}</div></div>"
        f"<div class='c'><div class='l'>Red Anchor</div><div class='v'>{red}</div></div>"
        f"<div class='c'><div class='l'>Leverage</div><div class='v'>{LEVERAGE}x</div></div>"
        "</div>"
        "<table><tr><th>Time</th><th>Side</th><th>Entry</th><th>TP</th><th>SL</th><th>Status</th><th>Exit</th></tr>"
        + rows_html +
        "</table>"
        "<p style='color:#555;font-size:11px;margin-top:1rem'>Auto-refreshes every 10 seconds | Live on Kraken</p>"
        "</body></html>"
    )
    return html


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


# NOTE: NO price_watcher_loop or thread started here.
# This is the race condition fix - ALL order monitoring/closing
# happens in worker.py only, using Kraken's own order status
# (via query_orders), not manual price polling.

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
