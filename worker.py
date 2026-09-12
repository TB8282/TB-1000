"""
Coinbase Worker - Order Monitoring Service
=============================================
Same role as the old Kraken worker: the ONLY process that monitors and
closes trades. app.py does NOT run a parallel price-watcher thread.
This worker polls Coinbase's own order status via the API, since TP/SL
orders are placed directly on the exchange (two separate orders, same
constraint as Kraken - see coinbase_client.py).

SCRATCH RULE (new):
Once a trade's unrealized profit reaches SCRATCH_ARM_PCT (0.5%), the
trade is "armed." If price then retraces back to the entry price while
armed, the trade is force-closed flat (a scratch) - cancels both TP/SL
orders and closes at market. The real -0.75% SL stays live on the
exchange the entire time as a hard floor in case of a sudden wick;
this scratch logic is an independent watcher on top of it, not a
replacement for it.

TIE RULE (changed from old PROFIT-TIMEOUT rule):
If a trade has been open longer than PROFIT_TIMEOUT_HOURS, it is
force-closed at market UNCONDITIONALLY - win, loss, or flat - and
logged as a TIE. This replaces the old behavior of only closing if
in profit and letting losers ride toward SL.
"""

import os
import time
import psycopg2
from datetime import datetime
from coinbase_client import CoinbaseClient, PRODUCT_ID

LEVERAGE = 10
PROFIT_TIMEOUT_HOURS = float(os.environ.get("PROFIT_TIMEOUT_HOURS", 24))
SCRATCH_ARM_PCT = 0.005  # 0.5% - once profit reaches this, arm the scratch watcher

CDP_API_KEY_NAME = os.environ.get("CDP_API_KEY_NAME")
CDP_API_KEY_PRIVATE_KEY = os.environ.get("CDP_API_KEY_PRIVATE_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

coinbase = CoinbaseClient(CDP_API_KEY_NAME, CDP_API_KEY_PRIVATE_KEY)


def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    Creates the tables if they don't exist. Runs on every worker startup.
    Only worker.py is deployed as a running service - app.py's init_db()
    never executes on its own.
    """
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
        print("Coinbase DB tables initialized (from worker.py)")
    except Exception as e:
        print(f"DB init error: {e}")


def load_bot_state():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM coinbase_bot_state")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r[0]: r[1] for r in rows}


def update_bot_state(**kwargs):
    conn = get_db()
    cur = conn.cursor()
    for key, val in kwargs.items():
        cur.execute("""
            INSERT INTO coinbase_bot_state (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, str(val)))
    conn.commit()
    cur.close()
    conn.close()


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


def force_close_at_market(data, tp_order_id, sl_order_id, side, trade_contracts, status_label):
    """
    Shared close path for both SCRATCH and TIE outcomes: cancels both
    open TP/SL orders, closes the position at market, records the
    result, and resets bot state. status_label is "SCRATCH" or "TIE".
    """
    close_side = "sell" if side == "LONG" else "buy"

    if tp_order_id and tp_order_id != "None":
        cancel_tp = coinbase.cancel_order(tp_order_id)
        print(f"Cancel TP result: {cancel_tp}")
    if sl_order_id and sl_order_id != "None":
        cancel_sl = coinbase.cancel_order(sl_order_id)
        print(f"Cancel SL result: {cancel_sl}")

    if not trade_contracts or trade_contracts == "None":
        print(f"WARNING: no saved contract count, cannot {status_label}-close safely.")
        return

    close_result = coinbase.place_entry_order(close_side, int(trade_contracts), LEVERAGE)
    print(f"{status_label} close result: {close_result}")

    ticker_result = coinbase.get_ticker()
    exit_price = ticker_result["result"]["price"] if not ticker_result.get("error") else 0

    close_trade_record(status_label, exit_price)
    update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                      sl_order_id=None, scratch_armed=False)
    print(f"TRADE CLOSED: {status_label}")


def check_current_trade():
    data = load_bot_state()
    if data.get("in_trade") != "True":
        return

    tp_order_id = data.get("tp_order_id")
    sl_order_id = data.get("sl_order_id")
    side = data.get("trade_side")
    entry_time_str = data.get("entry_time")
    entry_price = float(data.get("entry_price", 0))
    trade_contracts = data.get("contracts")
    scratch_armed = data.get("scratch_armed") == "True"

    if not tp_order_id or not sl_order_id or tp_order_id == "None" or sl_order_id == "None":
        print("WARNING: Missing order IDs, cannot monitor this trade properly.")
        return

    result = coinbase.query_orders([tp_order_id, sl_order_id])
    if result.get("error"):
        print(f"Query orders error: {result['error']}")
        return

    orders = result.get("result", {})
    tp_status = orders.get(tp_order_id, {}).get("status")
    sl_status = orders.get(sl_order_id, {}).get("status")

    print(f"Order check | TP ({tp_order_id}): {tp_status} | SL ({sl_order_id}): {sl_status}")

    if tp_status == "closed":
        print("TP FILLED - cancelling SL order")
        cancel_result = coinbase.cancel_order(sl_order_id)
        print(f"Cancel SL result: {cancel_result}")
        exit_price = orders.get(tp_order_id, {}).get("price", 0)
        close_trade_record("WIN", exit_price)
        wins = int(data.get("wins", 0)) + 1
        update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                          sl_order_id=None, wins=wins, scratch_armed=False)
        print("TRADE CLOSED: WIN")
        return

    if sl_status == "closed":
        print("SL FILLED - cancelling TP order")
        cancel_result = coinbase.cancel_order(tp_order_id)
        print(f"Cancel TP result: {cancel_result}")
        exit_price = orders.get(sl_order_id, {}).get("price", 0)
        close_trade_record("LOSS", exit_price)
        losses = int(data.get("losses", 0)) + 1
        update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                          sl_order_id=None, losses=losses, scratch_armed=False)
        print("TRADE CLOSED: LOSS")
        return

    # Neither TP nor SL filled yet - check scratch and timeout rules
    ticker_result = coinbase.get_ticker()
    if ticker_result.get("error"):
        print(f"Could not fetch current price: {ticker_result['error']}")
        return
    current_price = ticker_result["result"]["price"]

    if entry_price > 0:
        profit_pct = ((current_price - entry_price) / entry_price if side == "LONG"
                      else (entry_price - current_price) / entry_price)

        if not scratch_armed and profit_pct >= SCRATCH_ARM_PCT:
            scratch_armed = True
            update_bot_state(scratch_armed=True)
            print(f"SCRATCH ARMED at {round(profit_pct*100, 3)}% profit")

        if scratch_armed:
            retraced_to_entry = (
                (side == "LONG" and current_price <= entry_price) or
                (side == "SHORT" and current_price >= entry_price)
            )
            if retraced_to_entry:
                print(f"SCRATCH TRIGGERED - price retraced to entry ({entry_price})")
                force_close_at_market(data, tp_order_id, sl_order_id, side,
                                       trade_contracts, "SCRATCH")
                return

    # TIE rule - unconditional close after PROFIT_TIMEOUT_HOURS, regardless of P&L
    if entry_time_str and entry_time_str != "None":
        try:
            entry_time = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M")
            hours_open = (datetime.utcnow() - entry_time).total_seconds() / 3600
        except Exception:
            hours_open = 0

        if hours_open >= PROFIT_TIMEOUT_HOURS:
            print(f"TIE TRIGGERED after {hours_open:.1f}hrs - closing unconditionally")
            force_close_at_market(data, tp_order_id, sl_order_id, side,
                                   trade_contracts, "TIE")


def worker_loop():
    print("Coinbase worker started - polling every 5s")
    while True:
        try:
            check_current_trade()
        except Exception as e:
            import traceback
            traceback.print_exc()
        time.sleep(5)


if __name__ == "__main__":
    init_db()
    worker_loop()
