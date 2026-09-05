"""
Kraken Worker - Order Monitoring Service
==========================================
This is the ONLY process that monitors and closes trades. app.py does
NOT run a parallel price-watcher thread - that was the source of the
race condition in the old Binance bot. This worker polls Kraken's own
order status via the API instead of manually checking price, since
TP/SL orders are placed directly on the exchange.

PROFIT-TIMEOUT RULE (confirmed tonight):
If a trade has been open longer than PROFIT_TIMEOUT_HOURS AND is
currently sitting in profit, force-close it at market. If it's
underwater at that point, let it keep running toward SL or eventual
recovery - do NOT force a loss just because time ran out.
"""

import os
import time
import psycopg2
from datetime import datetime
from kraken_client import KrakenClient

PAIR = "XBTUSD"
LEVERAGE = 10
# NOTE: No fixed VOLUME here anymore - each trade's actual volume is
# saved to the database when app.py opens it, and read back below.
PROFIT_TIMEOUT_HOURS = float(os.environ.get("PROFIT_TIMEOUT_HOURS", 12))  # TUNE THIS - not yet finalized

KRAKEN_API_KEY = os.environ.get("KRAKEN_API_KEY")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")

kraken = KrakenClient(KRAKEN_API_KEY, KRAKEN_API_SECRET)


def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    Creates the tables if they don't exist. Runs on every worker startup.
    This is needed here because only worker.py is actually running as a
    deployed service right now - app.py's init_db() never executes.
    """
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
        print("Kraken DB tables initialized (from worker.py)")
    except Exception as e:
        print(f"DB init error: {e}")


def load_bot_state():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM kraken_bot_state")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r[0]: r[1] for r in rows}


def update_bot_state(**kwargs):
    conn = get_db()
    cur = conn.cursor()
    for key, val in kwargs.items():
        cur.execute("""
            INSERT INTO kraken_bot_state (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, str(val)))
    conn.commit()
    cur.close()
    conn.close()


def close_trade_record(status, exit_price):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE kraken_trades SET status=%s, exit_price=%s, closed_time=%s
        WHERE id = (SELECT id FROM kraken_trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1)
    """, (status, exit_price, datetime.utcnow().isoformat()))
    conn.commit()
    cur.close()
    conn.close()


def check_current_trade():
    data = load_bot_state()
    if data.get("in_trade") != "True":
        return

    tp_txid = data.get("tp_txid")
    sl_txid = data.get("sl_txid")
    side = data.get("trade_side")
    entry_time_str = data.get("entry_time")

    if not tp_txid or not sl_txid or tp_txid == "None" or sl_txid == "None":
        print("WARNING: Missing order IDs, cannot monitor this trade properly.")
        return

    # Check both order statuses
    result = kraken.query_orders([tp_txid, sl_txid])
    if result.get("error"):
        print(f"Query orders error: {result['error']}")
        return

    orders = result.get("result", {})
    tp_status = orders.get(tp_txid, {}).get("status")
    sl_status = orders.get(sl_txid, {}).get("status")

    print(f"Order check | TP ({tp_txid}): {tp_status} | SL ({sl_txid}): {sl_status}")

    if tp_status == "closed":
        print("TP FILLED - cancelling SL order")
        cancel_result = kraken.cancel_order(sl_txid)
        print(f"Cancel SL result: {cancel_result}")
        exit_price = orders.get(tp_txid, {}).get("price", 0)
        close_trade_record("WIN", exit_price)
        wins = int(data.get("wins", 0)) + 1
        update_bot_state(in_trade=False, trade_side=None, tp_txid=None,
                          sl_txid=None, wins=wins)
        print("TRADE CLOSED: WIN")
        return

    if sl_status == "closed":
        print("SL FILLED - cancelling TP order")
        cancel_result = kraken.cancel_order(tp_txid)
        print(f"Cancel TP result: {cancel_result}")
        exit_price = orders.get(sl_txid, {}).get("price", 0)
        close_trade_record("LOSS", exit_price)
        losses = int(data.get("losses", 0)) + 1
        update_bot_state(in_trade=False, trade_side=None, tp_txid=None,
                          sl_txid=None, losses=losses)
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
            ticker = kraken.get_ticker(PAIR)
            try:
                current_price = float(list(ticker["result"].values())[0]["c"][0])
            except Exception as e:
                print(f"Could not fetch current price for timeout check: {e}")
                return

            entry_price = float(data.get("entry_price", 0))
            in_profit = (
                (side == "LONG" and current_price > entry_price) or
                (side == "SHORT" and current_price < entry_price)
            )

            if in_profit:
                print(f"PROFIT-TIMEOUT triggered after {hours_open:.1f}hrs - closing at market")
                close_side = "sell" if side == "LONG" else "buy"
                trade_volume = data.get("volume")
                if not trade_volume or trade_volume == "None":
                    print("WARNING: no saved volume for this trade, cannot timeout-close safely.")
                    return
                # Cancel both pending orders first
                kraken.cancel_order(tp_txid)
                kraken.cancel_order(sl_txid)
                # Market close using the SAME volume as the original entry
                close_result = kraken.place_entry_order(PAIR, close_side, trade_volume, LEVERAGE)
                print(f"Timeout close result: {close_result}")
                close_trade_record("WIN", current_price)
                wins = int(data.get("wins", 0)) + 1
                update_bot_state(in_trade=False, trade_side=None, tp_txid=None,
                                  sl_txid=None, wins=wins)
                print("TRADE CLOSED: WIN (profit-timeout)")
            else:
                print(f"Trade open {hours_open:.1f}hrs, underwater - letting it ride toward SL")


def worker_loop():
    print("Kraken worker started - polling every 5s")
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
