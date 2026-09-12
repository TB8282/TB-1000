"""
Coinbase Worker - Order Monitoring Service
=============================================
Same role as the old Kraken worker: the ONLY process that monitors and
closes trades. app.py does NOT run a parallel price-watcher thread.
This worker polls Coinbase's own order status via the API, since TP/SL
orders are placed directly on the exchange (two separate orders, same
constraint as Kraken - see coinbase_client.py).

PROFIT-TIMEOUT RULE (unchanged from Kraken version):
If a trade has been open longer than PROFIT_TIMEOUT_HOURS AND is
currently sitting in profit, force-close it at market. If it's
underwater at that point, let it keep running toward SL or eventual
recovery - do NOT force a loss just because time ran out.
"""

import os
import time
import psycopg2
from datetime import datetime
from coinbase_client import CoinbaseClient, PRODUCT_ID

LEVERAGE = 10
PROFIT_TIMEOUT_HOURS = float(os.environ.get("PROFIT_TIMEOUT_HOURS", 12))

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


def check_current_trade():
    data = load_bot_state()
    if data.get("in_trade") != "True":
        return

    tp_order_id = data.get("tp_order_id")
    sl_order_id = data.get("sl_order_id")
    side = data.get("trade_side")
    entry_time_str = data.get("entry_time")

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
                          sl_order_id=None, wins=wins)
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
                          sl_order_id=None, losses=losses)
        print("TRADE CLOSED: LOSS")
        return

    # Neither filled yet - check profit-timeout rule
    if entry_time_str and entry_time_str != "None":
        try:
            entry_time = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M")
            hours_open = (datetime.utcnow() - entry_time).total_seconds() / 3600
        except Exception:
            hours_open = 0

        if hours_open >= PROFIT_TIMEOUT_HOURS:
            ticker_result = coinbase.get_ticker()
            if ticker_result.get("error"):
                print(f"Could not fetch current price for timeout check: {ticker_result['error']}")
                return
            current_price = ticker_result["result"]["price"]

            entry_price = float(data.get("entry_price", 0))
            in_profit = (
                (side == "LONG" and current_price > entry_price) or
                (side == "SHORT" and current_price < entry_price)
            )

            if in_profit:
                print(f"PROFIT-TIMEOUT triggered after {hours_open:.1f}hrs - closing at market")
                close_side = "sell" if side == "LONG" else "buy"
                trade_contracts = data.get("contracts")
                if not trade_contracts or trade_contracts == "None":
                    print("WARNING: no saved contract count for this trade, cannot timeout-close safely.")
                    return
                coinbase.cancel_order(tp_order_id)
                coinbase.cancel_order(sl_order_id)
                close_result = coinbase.place_entry_order(close_side, int(trade_contracts), LEVERAGE)
                print(f"Timeout close result: {close_result}")
                close_trade_record("WIN", current_price)
                wins = int(data.get("wins", 0)) + 1
                update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                                  sl_order_id=None, wins=wins)
                print("TRADE CLOSED: WIN (profit-timeout)")
            else:
                print(f"Trade open {hours_open:.1f}hrs, underwater - letting it ride toward SL")


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
