from flask import Flask, request, jsonify
import os
import threading
import time
import psycopg2
from datetime import datetime
from coinbase_client import CoinbaseClient, PRODUCT_ID

app = Flask(__name__)

# ============ CONFIRMED RULES ============
ANCHOR_LEVEL = 35
TRIGGER_MAX_GREEN = 15
TRIGGER_MIN_RED = -15
TP_PCT = 0.0075            # 0.75%
SL_PCT = 0.0075            # 0.75%
LEVERAGE = 10              # requested leverage for preview/order calls; ACTUAL
                           # leverage Coinbase grants varies by intraday/overnight
                           # margin window - real margin is now pulled live via
                           # preview_order, not assumed from this constant.
BALANCE_SAFETY_PCT = 0.95  # same safety buffer concept as Kraken version
CONTRACT_SIZE_BTC = 0.01   # nano BTC perp = 0.01 BTC per contract

CDP_API_KEY_NAME = os.environ.get("CDP_API_KEY_NAME")
CDP_API_KEY_PRIVATE_KEY = os.environ.get("CDP_API_KEY_PRIVATE_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

coinbase = CoinbaseClient(CDP_API_KEY_NAME, CDP_API_KEY_PRIVATE_KEY)

state = {
    "in_trade": False,
    "trade_side": None,
    "entry_price": None,
    "entry_order_id": None,
    "tp_price": None,
    "sl_price": None,
    "tp_order_id": None,
    "sl_order_id": None,
    "entry_time": None,
    "contracts": None,
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
            CREATE TABLE IF NOT EXISTS coinbase_bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS coinbase_trades (
                id SERIAL PRIMARY KEY,
                time TEXT,
                side TEXT,
                entry_price REAL,
                tp_price REAL,
                sl_price REAL,
                tp_order_id TEXT,
                sl_order_id TEXT,
                status TEXT,
                exit_price REAL,
                closed_time TEXT
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Coinbase DB tables initialized")
    except Exception as e:
        print(f"DB init error: {e}")


def load_state():
    """
    Loads persisted bot state from the DB on startup. Without this,
    app.py's in-memory state dict resets to defaults (in_trade=False)
    on every redeploy/restart, even if a real trade is still open on
    Coinbase and being tracked by worker.py via the same DB table.
    """
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM coinbase_bot_state")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        db_values = {k: v for k, v in rows}
        if not db_values:
            print("No saved state found in DB - starting fresh.")
            return

        with state_lock:
            if "in_trade" in db_values:
                state["in_trade"] = db_values["in_trade"] == "True"
            if "trade_side" in db_values:
                state["trade_side"] = None if db_values["trade_side"] == "None" else db_values["trade_side"]
            if "entry_price" in db_values:
                state["entry_price"] = safe_float(db_values["entry_price"])
            if "entry_order_id" in db_values:
                state["entry_order_id"] = None if db_values["entry_order_id"] == "None" else db_values["entry_order_id"]
            if "tp_price" in db_values:
                state["tp_price"] = safe_float(db_values["tp_price"])
            if "sl_price" in db_values:
                state["sl_price"] = safe_float(db_values["sl_price"])
            if "tp_order_id" in db_values:
                state["tp_order_id"] = None if db_values["tp_order_id"] == "None" else db_values["tp_order_id"]
            if "sl_order_id" in db_values:
                state["sl_order_id"] = None if db_values["sl_order_id"] == "None" else db_values["sl_order_id"]
            if "entry_time" in db_values:
                state["entry_time"] = None if db_values["entry_time"] == "None" else db_values["entry_time"]
            if "contracts" in db_values:
                state["contracts"] = None if db_values["contracts"] == "None" else db_values["contracts"]
            if "wins" in db_values:
                state["wins"] = int(db_values["wins"])
            if "losses" in db_values:
                state["losses"] = int(db_values["losses"])

        print(f"State loaded from DB: in_trade={state['in_trade']} | "
              f"trade_side={state['trade_side']} | wins={state['wins']} | losses={state['losses']}")
    except Exception as e:
        print(f"State load error: {e}")


def save_state():
    try:
        conn = get_db()
        cur = conn.cursor()
        for key in ["in_trade", "trade_side", "entry_price", "entry_order_id", "tp_price",
                    "sl_price", "tp_order_id", "sl_order_id", "entry_time",
                    "contracts", "wins", "losses"]:
            cur.execute("""
                INSERT INTO coinbase_bot_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, str(state.get(key))))
        conn.commit()
        cur.close()
        conn.close()
        print(f"State saved to DB OK: in_trade={state.get('in_trade')}", flush=True)
    except Exception as e:
        print(f"DB save error: {e}", flush=True)


def save_trade(t):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO coinbase_trades
            (time, side, entry_price, tp_price, sl_price, tp_order_id, sl_order_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (t["time"], t["side"], t["entry_price"], t["tp_price"], t["sl_price"],
              t["tp_order_id"], t["sl_order_id"], "OPEN"))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB save trade error: {e}")


def calculate_contracts(entry_side):
    """
    Calculates position size in WHOLE CONTRACTS using the REAL live margin
    requirement from Coinbase's own preview_order endpoint - NOT a hardcoded
    leverage assumption.

    TIMING INSTRUMENTATION ADDED (Sep 15 2026): every Coinbase API call in
    this function is now wrapped with elapsed-time logging, to find which
    specific call is stalling and causing the ongoing SystemExit/worker
    timeout crashes even after raising Gunicorn's --timeout to 120s.
    """
    t0 = time.time()
    bal_result = coinbase.get_balance()
    print(f"TIMING: get_balance() took {time.time() - t0:.2f}s", flush=True)
    if bal_result.get("error"):
        print(f"BALANCE CHECK FAILED: {bal_result['error']}")
        return None
    balance_data = bal_result.get("result", {}).get("balance_summary", {})
    # FIX (Sep 15 2026): cbi_usd_balance only reflects the SPOT account and
    # misses cash already sitting in the Derivatives/CFM account (confirmed
    # via Coinbase's own API docs and the order form's "Available (USD +
    # USDC)" figure). futures_buying_power is Coinbase's own documented
    # "amount of cash balance available to trade CFM futures" - the correct
    # combined number, matching what the order form itself shows.
    usd_balance = float(balance_data.get("futures_buying_power", {}).get("value", 0))
    if usd_balance <= 0:
        print(f"WARNING: futures_buying_power is {usd_balance} - nothing to trade with.")
        return None

    t1 = time.time()
    preview_result = coinbase.place_entry_order(entry_side, 1, LEVERAGE, validate=True)
    print(f"TIMING: preview_order() (margin check) took {time.time() - t1:.2f}s", flush=True)
    if preview_result.get("error"):
        print(f"MARGIN PREVIEW FAILED: {preview_result['error']}")
        return None

    preview_data = preview_result.get("result", {})
    margin_per_contract = safe_float(preview_data.get("order_margin_total"))
    if not margin_per_contract or margin_per_contract <= 0:
        print(f"PREVIEW RETURNED NO USABLE MARGIN VALUE: {preview_data}")
        return None

    available_for_trading = usd_balance * BALANCE_SAFETY_PCT
    contracts = int(available_for_trading // margin_per_contract)
    print(f"Contract calc: balance=${usd_balance:.2f} | safety={BALANCE_SAFETY_PCT} | "
          f"margin_per_contract=${margin_per_contract:.2f} (LIVE preview) | "
          f"available_for_trading=${available_for_trading:.2f} | contracts={contracts}")
    print(f"TIMING: calculate_contracts() TOTAL took {time.time() - t0:.2f}s", flush=True)
    return contracts if contracts > 0 else None


def open_trade(side, webhook_close_price, candle_time):
    """
    Places the real entry order on Coinbase, then queries for the ACTUAL
    fill price. Same HA-distortion protection as the Kraken version -
    signal comes from the chart, price math comes from the exchange.

    FIX (Sep 14 2026): every order ID is saved to the DB immediately
    after Coinbase returns it - BEFORE any further slow/blocking code
    runs (fill-price polling, the next order call, etc).

    TIMING INSTRUMENTATION ADDED (Sep 15 2026): wraps every remaining
    Coinbase API call (entry order, fill-price poll, TP order, SL order)
    with elapsed-time logging, plus a running total from function start,
    to find where the SystemExit/worker-timeout crash is actually
    happening - it is still occurring even with Gunicorn's timeout
    raised to 120s, so something is stalling longer than expected.
    """
    func_start = time.time()
    entry_side = "buy" if side == "LONG" else "sell"
    exit_side = "sell" if side == "LONG" else "buy"

    contracts = calculate_contracts(entry_side)
    print(f"TIMING: [elapsed {time.time() - func_start:.2f}s] after calculate_contracts()", flush=True)
    if not contracts:
        print("TRADE ABORTED: could not calculate contract size from live balance/margin.")
        return

    t_entry = time.time()
    entry_result = coinbase.place_entry_order(entry_side, contracts, LEVERAGE)
    print(f"TIMING: place_entry_order() took {time.time() - t_entry:.2f}s "
          f"| [elapsed {time.time() - func_start:.2f}s total]", flush=True)
    if entry_result.get("error"):
        print(f"ENTRY ORDER FAILED: {entry_result['error']}")
        return
    print(f"Entry order placed: {entry_result}")

    entry_order_id = entry_result.get("result", {}).get("order_id")
    if not entry_order_id:
        print("ENTRY FAILED: no order_id returned, cannot proceed")
        return

    # SAVE IMMEDIATELY - before the fill-price polling loop below.
    t_save1 = time.time()
    with state_lock:
        state["in_trade"] = True
        state["trade_side"] = side
        state["entry_price"] = webhook_close_price
        state["entry_order_id"] = entry_order_id
        state["tp_price"] = None
        state["sl_price"] = None
        state["tp_order_id"] = None
        state["sl_order_id"] = None
        state["entry_time"] = candle_time
        state["contracts"] = contracts
    save_state()
    print(f"TIMING: entry save_state() took {time.time() - t_save1:.2f}s "
          f"| [elapsed {time.time() - func_start:.2f}s total]", flush=True)
    print(f"Entry order_id saved to DB immediately: {entry_order_id}")

    entry_price = None
    t_poll = time.time()
    for attempt in range(5):
        time.sleep(1)
        order_info = coinbase.query_orders([entry_order_id])
        order_data = order_info.get("result", {}).get(entry_order_id, {})
        if order_data.get("status") == "closed":
            entry_price = float(order_data.get("price", 0))
            break
    print(f"TIMING: fill-price poll loop took {time.time() - t_poll:.2f}s "
          f"| [elapsed {time.time() - func_start:.2f}s total]", flush=True)

    if not entry_price:
        print("WARNING: Could not confirm real fill price after 5 attempts. "
              f"Falling back to webhook close price ({webhook_close_price}).")
        entry_price = webhook_close_price
    else:
        print(f"Confirmed REAL fill price from Coinbase: {entry_price} "
              f"(webhook sent: {webhook_close_price})")
        with state_lock:
            state["entry_price"] = entry_price
        save_state()

    if side == "LONG":
        tp = round(entry_price * (1 + TP_PCT) / 5) * 5
        sl = round(entry_price * (1 - SL_PCT) / 5) * 5
    else:
        tp = round(entry_price * (1 - TP_PCT) / 5) * 5
        sl = round(entry_price * (1 + SL_PCT) / 5) * 5

    t_tp = time.time()
    tp_result = coinbase.place_close_order(exit_side, contracts, "take-profit", tp, LEVERAGE)
    print(f"TIMING: place_close_order(TP) took {time.time() - t_tp:.2f}s "
          f"| [elapsed {time.time() - func_start:.2f}s total]", flush=True)
    tp_order_id = None
    if tp_result.get("error"):
        print(f"TP ORDER FAILED: {tp_result['error']}")
    else:
        tp_order_id = tp_result.get("result", {}).get("order_id")
        with state_lock:
            state["tp_price"] = tp
            state["tp_order_id"] = tp_order_id
        save_state()
        print(f"TP order_id saved to DB immediately: {tp_order_id}")

    t_sl = time.time()
    sl_result = coinbase.place_close_order(exit_side, contracts, "stop-loss", sl, LEVERAGE)
    print(f"TIMING: place_close_order(SL) took {time.time() - t_sl:.2f}s "
          f"| [elapsed {time.time() - func_start:.2f}s total]", flush=True)
    sl_order_id = None
    if sl_result.get("error"):
        print(f"SL ORDER FAILED: {sl_result['error']}")
    else:
        sl_order_id = sl_result.get("result", {}).get("order_id")
        with state_lock:
            state["sl_price"] = sl
            state["sl_order_id"] = sl_order_id
        save_state()
        print(f"SL order_id saved to DB immediately: {sl_order_id}")

    save_trade({
        "time": candle_time, "side": side, "entry_price": entry_price,
        "tp_price": tp, "sl_price": sl, "tp_order_id": tp_order_id, "sl_order_id": sl_order_id,
    })
    print(f"TRADE OPENED: {side} | Entry: {entry_price} | TP: {tp} ({tp_order_id}) | SL: {sl} ({sl_order_id})")
    print(f"TIMING: open_trade() TOTAL took {time.time() - func_start:.2f}s", flush=True)
    print("NOTE: worker.py must poll tp_order_id/sl_order_id and cancel whichever doesn't fill.")


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

        # FIX (Sep 16 2026): open_trade() must NEVER be called while
        # state_lock is held. state_lock is a plain threading.Lock (not
        # reentrant) - open_trade() itself acquires state_lock internally
        # to save entry/TP/SL state. Calling open_trade() from inside this
        # same "with state_lock:" block caused every single trade to hang
        # forever at open_trade()'s "with state_lock:" line, waiting on a
        # lock this same thread already held. Gunicorn's worker-timeout
        # (30s originally, 120s after that change) was the only thing that
        # ever ended it - explaining every SystemExit/WORKER TIMEOUT crash
        # traced back to that exact line across every incident this week.
        # Fix: decide inside the lock (cheap, in-memory), release the lock,
        # THEN call open_trade() if a valid signal fired.
        trade_side_to_open = None

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
                        state["green_anchor"] = {"value": value}
                        trade_side_to_open = "LONG"
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
                        state["red_anchor"] = {"value": value}
                        trade_side_to_open = "SHORT"
                elif value >= ANCHOR_LEVEL:
                    state["red_anchor"] = {"value": value}
                    print(f"NEW RED anchor: {round(value, 2)}")

        # state_lock is now RELEASED. Safe for open_trade() to acquire it.
        if trade_side_to_open:
            open_trade(trade_side_to_open, close_price, now)

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
            FROM coinbase_trades ORDER BY id DESC LIMIT 10
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
    bal_result = coinbase.get_balance()
    balance_data = (bal_result.get("result") or {}).get("balance_summary", {})
    cbi_balance = float(balance_data.get("futures_buying_power", {}).get("value", 0))

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
        "<!DOCTYPE html><html><head><title>TB-1000 Coinbase</title>"
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
        "<h1>TB-1000 Coinbase Bot (10x Leverage)</h1>"
        "<div class='g'>"
        f"<div class='c'><div class='l'>Available Balance (USD)</div><div class='v'>{fmt(cbi_balance)}</div></div>"
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
        "<p style='color:#555;font-size:11px;margin-top:1rem'>Auto-refreshes every 10 seconds | Live on Coinbase</p>"
        "</body></html>"
    )
    return html


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


def close_trade_record(status, exit_price):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE coinbase_trades SET status=%s, exit_price=%s, closed_time=%s
        WHERE id = (SELECT id FROM coinbase_trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1)
    """, (status, exit_price, datetime.utcnow().isoformat()))
    conn.commit()
    cur.close()
    conn.close()


@app.route("/close_manual", methods=["GET"])
def close_manual():
    """
    Records a trade that was closed manually on the exchange (bracket
    order filled) but which the bot's automated worker never saw
    because it lacked real order IDs to monitor. Recovery tool.
    Visit as a URL, e.g.:
    /close_manual?status=LOSS&exit_price=76615
    status must be WIN, LOSS, or TIE.
    """
    status = request.args.get("status", "").upper()
    exit_price = safe_float(request.args.get("exit_price"))
    if status not in ("WIN", "LOSS", "TIE") or exit_price is None:
        return jsonify({"error": "required params: status (WIN/LOSS/TIE), exit_price"}), 400

    close_trade_record(status, exit_price)

    with state_lock:
        if status == "WIN":
            state["wins"] = state.get("wins", 0) + 1
        elif status == "LOSS":
            state["losses"] = state.get("losses", 0) + 1
        state["in_trade"] = False
        state["trade_side"] = None
        state["entry_price"] = None
        state["entry_order_id"] = None
        state["tp_price"] = None
        state["sl_price"] = None
        state["tp_order_id"] = None
        state["sl_order_id"] = None
        state["entry_time"] = None
        state["contracts"] = None

    save_state()
    return jsonify({"status": "closed", "state": {k: v for k, v in state.items() if k not in ("green_anchor", "red_anchor")}})


@app.route("/resync", methods=["GET"])
def resync():
    """
    Manually re-syncs state to reflect a real trade already open on Coinbase
    but missing from the DB/in-memory state (recovery tool, not normal flow).
    Visit as a URL with query params, e.g.:
    /resync?side=LONG&entry_price=77205&tp_price=77785&sl_price=76625&contracts=1&entry_time=2026-09-12 20:15
    """
    side = request.args.get("side")
    entry_price = safe_float(request.args.get("entry_price"))
    tp_price = safe_float(request.args.get("tp_price"))
    sl_price = safe_float(request.args.get("sl_price"))
    contracts = request.args.get("contracts")
    entry_time = request.args.get("entry_time")

    if not side or entry_price is None:
        return jsonify({"error": "missing required params: side, entry_price"}), 400

    with state_lock:
        state["in_trade"] = True
        state["trade_side"] = side
        state["entry_price"] = entry_price
        state["tp_price"] = tp_price
        state["sl_price"] = sl_price
        state["contracts"] = contracts
        state["entry_time"] = entry_time

    save_state()
    return jsonify({"status": "resynced", "state": {k: v for k, v in state.items() if k not in ("green_anchor", "red_anchor")}})


@app.route("/debug", methods=["GET"])
def debug():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM coinbase_bot_state ORDER BY key")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        db_state = {r[0]: r[1] for r in rows}
    except Exception as e:
        db_state = {"error": str(e)}

    return jsonify({
        "db_state": db_state,
        "in_memory_state": {k: v for k, v in state.items() if k not in ("green_anchor", "red_anchor")}
    })


init_db()
load_state()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
