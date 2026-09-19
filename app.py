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
    "balance_before_trade": None,
    "green_anchor": None,
    "red_anchor": None,
    "candle_count": 0,
    "wins": 0,
    "losses": 0,
    "scratches": 0,
    "ties": 0,
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

    FIX (Sep 18 2026): wins/losses are now recalculated directly from
    the coinbase_trades table (source of truth - one row per real
    trade) instead of trusting the separately-stored counter in
    coinbase_bot_state, which was going stale/out of sync with the
    actual trade history (dashboard showed 1W/0L while the trade
    table had 2 LOSS rows).
    """
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM coinbase_bot_state")
        rows = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='WIN'")
        real_wins = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='LOSS'")
        real_losses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='SCRATCH'")
        real_scratches = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='TIE'")
        real_ties = cur.fetchone()[0]

        cur.close()
        conn.close()

        db_values = {k: v for k, v in rows}
        if not db_values:
            print("No saved state found in DB - starting fresh.")
            with state_lock:
                state["wins"] = real_wins
                state["losses"] = real_losses
                state["scratches"] = real_scratches
                state["ties"] = real_ties
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
            if "balance_before_trade" in db_values:
                state["balance_before_trade"] = safe_float(db_values["balance_before_trade"])

            state["wins"] = real_wins
            state["losses"] = real_losses
            state["scratches"] = real_scratches
            state["ties"] = real_ties

        print(f"State loaded from DB: in_trade={state['in_trade']} | "
              f"trade_side={state['trade_side']} | wins={state['wins']} (from trade table) | "
              f"losses={state['losses']} (from trade table)")
    except Exception as e:
        print(f"State load error: {e}")


def save_state():
    try:
        conn = get_db()
        cur = conn.cursor()
        for key in ["in_trade", "trade_side", "entry_price", "entry_order_id", "tp_price",
                    "sl_price", "tp_order_id", "sl_order_id", "entry_time",
                    "contracts", "balance_before_trade", "wins", "losses"]:
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


def calculate_contracts(entry_side, tp_price=None, sl_price=None):
    """
    Calculates position size in WHOLE CONTRACTS using the REAL live margin
    requirement from Coinbase's own preview_order endpoint - NOT a hardcoded
    leverage assumption.

    FIX (Sep 17 2026): tp_price/sl_price are now passed into the margin
    PREVIEW too (not just the real order), since attaching a bracket to
    the entry order can affect the real margin Coinbase requires - this
    keeps the preview accurate to what will actually be submitted.
    """
    t0 = time.time()
    bal_result = coinbase.get_balance()
    print(f"TIMING: get_balance() took {time.time() - t0:.2f}s", flush=True)
    if bal_result.get("error"):
        print(f"BALANCE CHECK FAILED: {bal_result['error']}")
        return None, None
    balance_data = bal_result.get("result", {}).get("balance_summary", {})
    # futures_buying_power is Coinbase's own documented "amount of cash
    # balance available to trade CFM futures" - the correct combined
    # (spot + derivatives) number, matching what the order form itself shows.
    usd_balance = float(balance_data.get("futures_buying_power", {}).get("value", 0))
    if usd_balance <= 0:
        print(f"WARNING: futures_buying_power is {usd_balance} - nothing to trade with.")
        return None, None

    t1 = time.time()
    preview_result = coinbase.place_entry_order(entry_side, 1, LEVERAGE, validate=True,
                                                  tp_price=tp_price, sl_price=sl_price)
    print(f"TIMING: preview_order() (margin check) took {time.time() - t1:.2f}s", flush=True)
    if preview_result.get("error"):
        print(f"MARGIN PREVIEW FAILED: {preview_result['error']}")
        return None, None

    preview_data = preview_result.get("result", {})
    margin_per_contract = safe_float(preview_data.get("order_margin_total"))
    if not margin_per_contract or margin_per_contract <= 0:
        print(f"PREVIEW RETURNED NO USABLE MARGIN VALUE: {preview_data}")
        return None, None

    available_for_trading = usd_balance * BALANCE_SAFETY_PCT
    contracts = int(available_for_trading // margin_per_contract)
    print(f"Contract calc: balance=${usd_balance:.2f} | safety={BALANCE_SAFETY_PCT} | "
          f"margin_per_contract=${margin_per_contract:.2f} (LIVE preview) | "
          f"available_for_trading=${available_for_trading:.2f} | contracts={contracts}")
    print(f"TIMING: calculate_contracts() TOTAL took {time.time() - t0:.2f}s", flush=True)
    return (contracts, usd_balance) if contracts > 0 else (None, usd_balance)


def open_trade(side, webhook_close_price, candle_time):
    """
    Places the real entry order on Coinbase WITH a native TP/SL bracket
    attached (trigger_bracket_gtc) - one atomic order, not two follow-up
    orders. Coinbase's own docs confirm the untriggered side auto-cancels
    the instant the other fills - true exchange-enforced OCO.

    FIX (Sep 18 2026): TP/SL are now calculated from Coinbase's own live
    real-time price (via get_ticker()), NOT the webhook's close value.
    The webhook's close is the Heikin-Ashi candle price (used correctly
    for signal/trigger logic, but not representative of real market
    price), and by the time this function runs it can also be stale by
    up to ~120 seconds if webhook delivery/processing was slow. Pulling
    a fresh live price right before submitting removes both problems.
    Falls back to webhook_close_price only if the live price fetch
    itself errors out, so a trade is never aborted over this.

    Also records balance_before_trade so worker.py can determine WIN/LOSS
    by comparing balance after the trade closes to balance before it
    opened - simpler and more certain than trying to determine which
    specific leg (TP or SL) of a merged bracket order triggered.
    """
    func_start = time.time()
    entry_side = "buy" if side == "LONG" else "sell"
    exit_side = "sell" if side == "LONG" else "buy"

    # TP/SL calculated up front - required so they can be attached to the
    # entry order itself, before any fill is confirmed.
    # FIX (real price, not HA/stale): pull Coinbase's real live price right
    # now instead of using the webhook's close value (HA candle price,
    # and can also be stale by the time this runs). Falls back to
    # webhook_close_price only if the live price fetch fails.
    ticker_result = coinbase.get_ticker()
    if not ticker_result.get("error"):
        base_price = ticker_result["result"]["price"]
        print(f"Using live real price for TP/SL calc: {base_price} (webhook close was: {webhook_close_price})", flush=True)
    else:
        base_price = webhook_close_price
        print(f"WARNING: live price fetch failed ({ticker_result['error']}) - falling back to webhook close price", flush=True)

    if side == "LONG":
        tp = round(base_price * (1 + TP_PCT) / 5) * 5
        sl = round(base_price * (1 - SL_PCT) / 5) * 5
    else:
        tp = round(base_price * (1 - TP_PCT) / 5) * 5
        sl = round(base_price * (1 + SL_PCT) / 5) * 5

    contracts, usd_balance = calculate_contracts(entry_side, tp_price=tp, sl_price=sl)
    print(f"TIMING: [elapsed {time.time() - func_start:.2f}s] after calculate_contracts()", flush=True)
    if not contracts:
        print("TRADE ABORTED: could not calculate contract size from live balance/margin.")
        return

    t_entry = time.time()
    entry_result = coinbase.place_entry_order(entry_side, contracts, LEVERAGE, tp_price=tp, sl_price=sl)
    print(f"TIMING: place_entry_order() (with attached bracket) took {time.time() - t_entry:.2f}s "
          f"| [elapsed {time.time() - func_start:.2f}s total]", flush=True)
    if entry_result.get("error"):
        print(f"ENTRY ORDER FAILED: {entry_result['error']}")
        return

    # BRACKET DEBUG: full raw response, so if bracket_order_id below ever
    # comes back None, this shows exactly where Coinbase actually put the
    # attached order's ID, for a one-line fix in coinbase_client.py.
    print(f"BRACKET DEBUG - full raw entry response: {entry_result}", flush=True)

    entry_order_id = entry_result.get("result", {}).get("order_id")
    bracket_order_id = entry_result.get("result", {}).get("bracket_order_id")
    if not entry_order_id:
        print("ENTRY FAILED: no order_id returned, cannot proceed")
        return
    if not bracket_order_id:
        # FIX (Sep 17 2026): the entry response's attached_order_id came
        # back as an empty string on the first real trade - the bracket
        # order likely wasn't registered on Coinbase's side yet at that
        # exact instant. Wait 1s, then look it up directly via open orders
        # instead of trusting an empty/missing field.
        print("bracket_order_id not in entry response - looking it up via open orders in 1s...", flush=True)
        time.sleep(1)
        lookup = coinbase.get_open_bracket_order(exclude_order_id=entry_order_id)
        print(f"BRACKET LOOKUP - result: {lookup}", flush=True)
        found_id = lookup.get("result", {}).get("order_id") if lookup.get("result") else None
        if found_id:
            bracket_order_id = found_id
            print(f"BRACKET LOOKUP SUCCESS - real bracket order id: {bracket_order_id}")
        else:
            print(f"BRACKET LOOKUP FAILED ({lookup.get('error')}) - falling back to entry_order_id. "
                  "worker.py may not detect closure correctly until this is fixed.")
            bracket_order_id = entry_order_id

    # SAVE IMMEDIATELY - before the fill-price polling loop below.
    with state_lock:
        state["in_trade"] = True
        state["trade_side"] = side
        state["entry_price"] = webhook_close_price
        state["entry_order_id"] = entry_order_id
        state["tp_price"] = tp
        state["sl_price"] = sl
        state["tp_order_id"] = bracket_order_id
        state["sl_order_id"] = bracket_order_id
        state["entry_time"] = candle_time
        state["contracts"] = contracts
        state["balance_before_trade"] = usd_balance
    save_state()
    # FIX (Sep 19 2026): defensively reset scratch_armed at the start of
    # every new trade, regardless of how the previous trade closed. This
    # is belt-and-suspenders on top of the close_manual/resync fixes -
    # a new trade should NEVER inherit an armed scratch flag from
    # whatever came before it.
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO coinbase_bot_state (key, value) VALUES ('scratch_armed', 'False')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed to reset scratch_armed in open_trade: {e}")
    print(f"Entry + bracket saved to DB immediately: entry={entry_order_id} "
          f"bracket={bracket_order_id} balance_before_trade=${usd_balance:.2f}")

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
              f"Falling back to webhook close price ({webhook_close_price}) for the trade record.")
        entry_price = webhook_close_price
    else:
        print(f"Confirmed REAL fill price from Coinbase: {entry_price} "
              f"(webhook sent: {webhook_close_price})")
        with state_lock:
            state["entry_price"] = entry_price
        save_state()

    save_trade({
        "time": candle_time, "side": side, "entry_price": entry_price,
        "tp_price": tp, "sl_price": sl, "tp_order_id": bracket_order_id, "sl_order_id": bracket_order_id,
    })
    print(f"TRADE OPENED: {side} | Entry: {entry_price} | TP: {tp} | SL: {sl} | Bracket: {bracket_order_id}")
    print(f"TIMING: open_trade() TOTAL took {time.time() - func_start:.2f}s", flush=True)
    print("NOTE: worker.py now determines WIN/LOSS by comparing balance before/after, "
          "not by which leg of the bracket filled.")


def sync_state_from_db():
    """
    FIX (Sep 19 2026): app.py's in-memory `state` dict only ever loaded
    from the DB once, at startup (load_state()). worker.py runs as a
    SEPARATE process and updates the DB directly when it closes a trade
    (WIN/LOSS/TIE/SCRATCH) - but app.py's own in-memory copy never heard
    about it. Confirmed real symptom: a trade scratch-closed correctly in
    the DB (in_trade=False), but the dashboard and webhook() kept reading
    in-memory state showing in_trade=true for that same dead trade -
    risking new valid signals being wrongly ignored as "already in trade."
    Called at the top of both webhook() and dashboard() so the tracker
    always reflects the real DB state before anything reads it.
    """
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM coinbase_bot_state")
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='WIN'")
        real_wins = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='LOSS'")
        real_losses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='SCRATCH'")
        real_scratches = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coinbase_trades WHERE status='TIE'")
        real_ties = cur.fetchone()[0]
        cur.close()
        conn.close()
        db_values = {k: v for k, v in rows}
        if not db_values:
            return
        with state_lock:
            state["wins"] = real_wins
            state["losses"] = real_losses
            state["scratches"] = real_scratches
            state["ties"] = real_ties
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
            if "balance_before_trade" in db_values:
                state["balance_before_trade"] = safe_float(db_values["balance_before_trade"])
    except Exception as e:
        print(f"sync_state_from_db error: {e}", flush=True)


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        sync_state_from_db()
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "invalid json"}), 400
        dot = str(data.get("dot", "")).lower().strip()
        value = safe_float(data.get("value", 0))
        close_price = safe_float(data.get("close", None))
        if value is None or close_price is None:
            return jsonify({"error": "invalid payload"}), 400

        print(f"Dot: {dot} | Value: {round(value, 2)} | Close: {close_price}")

        # open_trade() must NEVER be called while state_lock is held - it
        # acquires state_lock itself internally, and Python's threading.Lock
        # is not reentrant. Decide inside the lock (cheap, in-memory),
        # release the lock, THEN call open_trade() if a valid signal fired.
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
                        # FIX (Sep 19 2026): anchor resets to None after a
                        # valid trigger, instead of carrying the trigger's
                        # value forward. An anchor must sit outside the
                        # -35/+35 band; the trigger value itself is always
                        # inside that band, so it is never a valid anchor.
                        state["green_anchor"] = None
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
                        # FIX (Sep 19 2026): same as GREEN above - reset to
                        # None instead of carrying the trigger value forward.
                        state["red_anchor"] = None
                        trade_side_to_open = "SHORT"
                elif value >= ANCHOR_LEVEL:
                    state["red_anchor"] = {"value": value}
                    print(f"NEW RED anchor: {round(value, 2)}")

        # state_lock is now RELEASED. Safe for open_trade() to acquire it.
        # FIX (webhook timeout): run open_trade() in a background thread so
        # Flask can respond to TradingView immediately instead of waiting
        # for the full trade-opening process (balance check, order, fill
        # poll) to finish. Nothing inside open_trade() itself changes.
        if trade_side_to_open:
            threading.Thread(target=open_trade, args=(trade_side_to_open, close_price, now)).start()

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
    sync_state_from_db()
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
        f"<div class='c'><div class='l'>Scratches</div><div class='v'>{state['scratches']}</div></div>"
        f"<div class='c'><div class='l'>Ties</div><div class='v'>{state['ties']}</div></div>"
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


@app.route("/set_anchor", methods=["GET"])
def set_anchor():
    """
    Manually sets green_anchor or red_anchor (recovery tool - anchors
    live in memory only and reset to None on every redeploy).
    Visit as a URL, e.g.:
    /set_anchor?color=red&value=84.44
    color must be 'green' or 'red'.
    """
    color = request.args.get("color", "").lower()
    value = safe_float(request.args.get("value"))
    if color not in ("green", "red") or value is None:
        return jsonify({"error": "required params: color (green/red), value"}), 400

    with state_lock:
        state[f"{color}_anchor"] = {"value": value}

    return jsonify({"status": "ok", "color": color, "value": value})


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


@app.route("/admin_delete_losses", methods=["GET"])
def admin_delete_losses():
    """
    ONE-TIME recovery tool: deletes all LOSS rows from coinbase_trades.
    Used to clear stale/pre-fix loss records so wins/losses (recalculated
    from this table by load_state()) reflect only real post-fix trades.
    Visit as a URL once, then this can be removed.
    """
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM coinbase_trades WHERE status = 'LOSS'")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok", "deleted_rows": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        state["balance_before_trade"] = None

    save_state()
    # FIX (Sep 19 2026): scratch_armed lives in coinbase_bot_state but is
    # only managed by worker.py's update_bot_state() - this recovery route
    # never touched it, so a True flag from a trade that got stuck OPEN
    # and was closed here manually carried straight into the NEXT trade,
    # skipping the 0.5% arm requirement entirely (confirmed: a SHORT
    # scratched 2 seconds after opening because scratch_armed was still
    # True from the previous trade). Explicitly reset it here too.
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO coinbase_bot_state (key, value) VALUES ('scratch_armed', 'False')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed to reset scratch_armed in close_manual: {e}")

    return jsonify({"status": "closed", "state": {k: v for k, v in state.items() if k not in ("green_anchor", "red_anchor")}})


@app.route("/resync", methods=["GET"])
def resync():
    """
    Manually re-syncs state to reflect a real trade already open on Coinbase
    but missing from the DB/in-memory state (recovery tool, not normal flow).
    Visit as a URL with query params, e.g.:
    /resync?side=LONG&entry_price=77205&tp_price=77785&sl_price=76625&contracts=1&entry_time=2026-09-12 20:15&bracket_order_id=abc123

    FIX (Sep 19 2026): this route used to leave entry_order_id, tp_order_id,
    sl_order_id, and balance_before_trade untouched - meaning a resync
    could silently carry over a stale order ID from whatever trade came
    before it, causing worker.py to poll the WRONG order for this "new"
    trade. bracket_order_id is now an optional param; if not given, all
    order-ID fields are explicitly cleared to None rather than left stale.
    balance_before_trade is also explicitly cleared (worker.py needs a
    real value here to determine WIN/LOSS - the same-named recovery gap
    close_manual/check_current_trade already print a WARNING for when
    it's missing, so clearing it rather than leaving a wrong stale number
    is the safer failure mode).
    """
    side = request.args.get("side")
    entry_price = safe_float(request.args.get("entry_price"))
    tp_price = safe_float(request.args.get("tp_price"))
    sl_price = safe_float(request.args.get("sl_price"))
    contracts = request.args.get("contracts")
    entry_time = request.args.get("entry_time")
    bracket_order_id = request.args.get("bracket_order_id")

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
        state["entry_order_id"] = bracket_order_id
        state["tp_order_id"] = bracket_order_id
        state["sl_order_id"] = bracket_order_id
        state["balance_before_trade"] = None

    save_state()
    # FIX (Sep 19 2026): same gap as close_manual - reset scratch_armed
    # here too, since this route also starts tracking a "new" trade in
    # the state, and a stale True would skip the 0.5% arm requirement.
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO coinbase_bot_state (key, value) VALUES ('scratch_armed', 'False')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed to reset scratch_armed in resync: {e}")

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
