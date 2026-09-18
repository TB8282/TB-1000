"""
Coinbase Worker - Order Monitoring Service
=============================================
Same role as the old Kraken worker: the ONLY process that monitors and
closes trades. app.py does NOT run a parallel price-watcher thread.
This worker polls Coinbase's own order status via the API.

FIX (Sep 17 2026): app.py now attaches TP/SL to the entry order as a
single native Coinbase bracket (trigger_bracket_gtc) instead of placing
two separate follow-up orders. tp_order_id and sl_order_id in the DB
are now the SAME id (the bracket's own order id) - there is only one
order to poll, not two.

Because of that, this worker can no longer tell WIN from LOSS by
"which of the two order IDs closed" (there's only one ID now covering
both outcomes). Instead: once that single order shows closed/filled,
WIN/LOSS is determined by comparing the account's current
futures_buying_power to balance_before_trade (saved by app.py the
moment the trade opened). Balance up = WIN, balance down = LOSS.
This is a simpler, more certain signal than trying to infer which leg
of a merged bracket triggered, at the cost of being slightly less
precise about the exact fill price recorded (uses the bracket order's
own reported price for the trade record, balance comparison only
decides WIN/LOSS).

FIX (Sep 18 2026): Coinbase reports the bracket ORDER's own status as
CANCELLED once one leg fills (the bracket wrapper itself gets
cancelled, not marked FILLED) - not "closed" as expected. This left
trades stuck in_trade=True forever, since the old code only acted on
status=="closed". Now CANCELLED is treated the same as closed: a
cancelled bracket means one side filled (real trades don't get
cancelled with no fill mid-flight), so WIN/LOSS is determined the
same way as before (balance before vs after).

SCRATCH RULE (unchanged):
Once a trade's unrealized profit reaches SCRATCH_ARM_PCT (0.5%), the
trade is "armed." If price then retraces back to the entry price while
armed, the trade is force-closed flat (a scratch) - cancels the open
bracket order and closes at market.

TIE RULE (unchanged):
If a trade has been open longer than PROFIT_TIMEOUT_HOURS, it is
force-closed at market UNCONDITIONALLY - win, loss, or flat - and
logged as a TIE.
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


def get_current_balance():
    """
    Returns current futures_buying_power as a float, or None on error.
    Used to compare against balance_before_trade to determine WIN/LOSS.
    """
    bal_result = coinbase.get_balance()
    if bal_result.get("error"):
        print(f"Balance check error: {bal_result['error']}")
        return None
    balance_data = bal_result.get("result", {}).get("balance_summary", {})
    return float(balance_data.get("futures_buying_power", {}).get("value", 0))


def force_close_at_market(data, bracket_order_id, side, trade_contracts, status_label):
    """
    Shared close path for both SCRATCH and TIE outcomes: cancels the open
    bracket order, closes the position at market, records the result
    (SCRATCH/TIE are their own labels, not WIN/LOSS, so balance comparison
    is not needed here), and resets bot state.
    """
    close_side = "sell" if side == "LONG" else "buy"

    if bracket_order_id and bracket_order_id != "None":
        cancel_result = coinbase.cancel_order(bracket_order_id)
        print(f"Cancel bracket order result: {cancel_result}")

    if not trade_contracts or trade_contracts == "None":
        print(f"WARNING: no saved contract count, cannot {status_label}-close safely.")
        return

    close_result = coinbase.place_entry_order(close_side, int(trade_contracts), LEVERAGE)
    print(f"{status_label} close result: {close_result}")

    ticker_result = coinbase.get_ticker()
    exit_price = ticker_result["result"]["price"] if not ticker_result.get("error") else 0

    close_trade_record(status_label, exit_price)
    update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                      sl_order_id=None, entry_order_id=None,
                      balance_before_trade=None, scratch_armed=False)
    print(f"TRADE CLOSED: {status_label}")


def check_current_trade():
    data = load_bot_state()
    if data.get("in_trade") != "True":
        return

    # tp_order_id and sl_order_id are now the SAME value (the single
    # bracket order's own id) - only one order to poll.
    bracket_order_id = data.get("tp_order_id")
    side = data.get("trade_side")
    entry_time_str = data.get("entry_time")
    entry_price = float(data.get("entry_price", 0))
    trade_contracts = data.get("contracts")
    scratch_armed = data.get("scratch_armed") == "True"
    balance_before_trade = data.get("balance_before_trade")
    balance_before_trade = float(balance_before_trade) if balance_before_trade not in (None, "None") else None

    if not bracket_order_id or bracket_order_id == "None":
        print("WARNING: Missing bracket order id, cannot monitor this trade properly.")
        return

    result = coinbase.query_orders([bracket_order_id])
    if result.get("error"):
        print(f"Query orders error: {result['error']}")
        return

    orders = result.get("result", {})
    bracket_status = orders.get(bracket_order_id, {}).get("status")
    print(f"Order check | Bracket ({bracket_order_id}): {bracket_status}")

    # FIX (Sep 18 2026): Coinbase reports the bracket's own status as
    # CANCELLED once one leg fills (not FILLED/closed) - a real trade's
    # bracket does not get cancelled with no fill mid-flight, so treat
    # CANCELLED the same as closed/filled here.
    if bracket_status in ("closed", "CANCELLED"):
        exit_price = orders.get(bracket_order_id, {}).get("price", 0)

        if balance_before_trade is None:
            print("WARNING: no balance_before_trade saved - cannot determine WIN/LOSS. "
                  "Recording as TIE and clearing state so the bot doesn't get stuck.")
            close_trade_record("TIE", exit_price)
            update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                              sl_order_id=None, entry_order_id=None,
                              balance_before_trade=None, scratch_armed=False)
            return

        current_balance = get_current_balance()
        if current_balance is None:
            print("WARNING: could not fetch current balance to determine WIN/LOSS - will retry next poll.")
            return

        if current_balance > balance_before_trade:
            status_label = "WIN"
            wins = int(data.get("wins", 0)) + 1
            close_trade_record("WIN", exit_price)
            update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                              sl_order_id=None, entry_order_id=None,
                              balance_before_trade=None, wins=wins, scratch_armed=False)
        else:
            status_label = "LOSS"
            losses = int(data.get("losses", 0)) + 1
            close_trade_record("LOSS", exit_price)
            update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                              sl_order_id=None, entry_order_id=None,
                              balance_before_trade=None, losses=losses, scratch_armed=False)

        print(f"TRADE CLOSED: {status_label} | balance_before=${balance_before_trade:.2f} "
              f"-> current=${current_balance:.2f}")
        return

    # Bracket not filled yet - check scratch and timeout rules
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
                force_close_at_market(data, bracket_order_id, side,
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
            force_close_at_market(data, bracket_order_id, side,
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
