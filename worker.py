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

FIX (Sep 20 2026): balance_before/current comparison was reading
current balance IMMEDIATELY upon detecting the bracket close, before
Coinbase finished settling the freed margin + P&L back into
futures_buying_power. This produced a transient low balance snapshot
(margin still mostly locked) that could look like a big loss even on
a real TP win - confirmed on a real trade that exited at TP ($80,950,
+$4.64 actual gain per Coinbase transaction history) but got logged
as LOSS because balance read $96.24 mid-settlement instead of the
real post-settle ~$296. Fixed: after detecting bracket close, wait 2s
and poll up to 3 times before comparing, giving Coinbase's settlement
time to complete.

SCRATCH RULE (unchanged):
Once a trade's unrealized profit reaches SCRATCH_ARM_PCT (0.5%), the
trade is "armed." If price then retraces back to the entry price while
armed, the trade is force-closed flat (a scratch) - cancels the open
bracket order and closes at market.

TIE RULE (unchanged):
If a trade has been open longer than PROFIT_TIMEOUT_HOURS, it is
force-closed at market UNCONDITIONALLY - win, loss, or flat - and
logged as a TIE.

EOD CUTOFF RULE (added Sep 22 2026):
Coinbase's overnight margin requirements are much stricter than
intraday (confirmed real near-liquidation incident: a SHORT's overnight
liquidation estimate had already been breached by just a 0.72% adverse
move while using the same position sizing that was safe intraday).
Any trade opened during today's daytime session (entry time < 4pm ET)
that is still open at or after 3:15pm ET is force-closed at market
UNCONDITIONALLY, regardless of P&L - same closing mechanism as the TIE
rule, logged under its own status so how often this fires is visible
separately. This check runs on every 5s poll cycle (not a one-shot
timer) and fails toward closing: any error just means it gets retried
next cycle rather than silently skipped. Uses zoneinfo (America/New_York)
rather than a fixed UTC offset, so DST transitions are handled correctly
- Render's underlying clock is UTC, and a fixed-offset approach was the
same class of bug behind the earlier 4-hour dashboard timestamp issue.
Bounded to only trades entered TODAY before 4pm ET, so a legitimate
trade opened last night under the (lower-leverage) overnight regime is
never mistakenly force-closed by this rule.
"""

import os
import time
import psycopg2
from datetime import datetime
from zoneinfo import ZoneInfo
from coinbase_client import CoinbaseClient, PRODUCT_ID

ET = ZoneInfo("America/New_York")
EOD_FORCE_CLOSE_HOUR_ET = 15
EOD_FORCE_CLOSE_MINUTE_ET = 15  # 3:15 PM ET
DAYTIME_SESSION_END_HOUR_ET = 16  # 4:00 PM ET - trades opened at/after this are already under the overnight regime

LEVERAGE = 10
PROFIT_TIMEOUT_HOURS = float(os.environ.get("PROFIT_TIMEOUT_HOURS", 24))
SCRATCH_ARM_PCT = 0.005  # 0.5% - once profit reaches this, arm the scratch watcher
BALANCE_SETTLE_WAIT_SECONDS = 2  # FIX (Sep 20 2026): give Coinbase time to settle before reading balance
BALANCE_SETTLE_MAX_ATTEMPTS = 3

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
    # FIX (Sep 19 2026): full state hygiene - clear every trade-specific
    # field on close, not just the ones that happened to matter for one
    # bug at a time. entry_price/entry_time/contracts were staying stale
    # here even though tp_price/sl_price were already fixed.
    update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                      sl_order_id=None, entry_order_id=None,
                      tp_price=None, sl_price=None, entry_price=None,
                      entry_time=None, contracts=None,
                      balance_before_trade=None, scratch_armed=False, highest_profit_pct=0)
    print(f"TRADE CLOSED: {status_label}")


def is_eod_force_close_due(entry_time_str):
    """
    Returns True if it is currently within the 3:15pm-4:00pm ET window,
    meaning ANY currently open trade must be force-closed now, regardless
    of P&L, before Coinbase's overnight margin shift.

    FIX (Sep 25 2026): removed the old same-calendar-day + entered-before-4pm
    restriction. That version only caught a trade if it opened THAT SAME
    day before 4pm - a trade opened at, say, 9pm and still open through
    the next day's 4pm was never caught by this check at all, since its
    entry date no longer matched "today." Confirmed real gap: a trade
    left open overnight would ride through the very margin shift this
    function exists to protect against, day after day, with only the
    24hr TIE rule as a backstop (and only once 24 hours had passed).

    Now this checks ONLY the current time-of-day, with no entry-time
    condition at all - any trade still open when the clock enters
    3:15pm-4:00pm ET gets closed, whether it opened five minutes ago or
    five days ago. Bounded to the 3:15-4:00 window itself (not "any time
    after 3:15") so a trade opened at, say, 8pm the same evening isn't
    immediately caught the instant this function runs later that night -
    it only fires within the actual daily window, checked every 5s poll
    cycle. This also means no trade can ever stay open longer than
    roughly 24 hours (worst case: opens right after one window closes,
    caught by the very next one) - the 24hr TIE rule becomes a backstop
    that should now never actually have a chance to fire.
    """
    now_et = datetime.now(ET)
    return (now_et.hour, now_et.minute) >= (EOD_FORCE_CLOSE_HOUR_ET, EOD_FORCE_CLOSE_MINUTE_ET) \
        and now_et.hour < DAYTIME_SESSION_END_HOUR_ET


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
    highest_profit_pct = float(data.get("highest_profit_pct", 0) or 0)
    balance_before_trade = data.get("balance_before_trade")
    balance_before_trade = float(balance_before_trade) if balance_before_trade not in (None, "None") else None

    if not bracket_order_id or bracket_order_id == "None":
        print("WARNING: Missing bracket order id, cannot monitor this trade properly.")
        return

    # EOD CUTOFF: checked first, every poll cycle, before anything else -
    # this must fire regardless of bracket status, scratch state, or the
    # 24hr TIE timer. Deliberately placed ahead of the order-status query
    # below so it never gets skipped by an unrelated API error further down.
    if is_eod_force_close_due(entry_time_str):
        print(f"EOD CUTOFF TRIGGERED - trade entered {entry_time_str} UTC still open at/after "
              f"3:15pm ET - force-closing before overnight margin shift")
        force_close_at_market(data, bracket_order_id, side, trade_contracts, "EOD_CUTOFF")
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
                              tp_price=None, sl_price=None, entry_price=None,
                              entry_time=None, contracts=None,
                              balance_before_trade=None, scratch_armed=False, highest_profit_pct=0)
            return

        # FIX (Sep 20 2026): do not read balance immediately - Coinbase
        # needs a moment to settle freed margin + P&L back into
        # futures_buying_power after a bracket closes. Reading too early
        # produced a transient low snapshot that misclassified a real
        # win as a LOSS. Wait, then poll up to BALANCE_SETTLE_MAX_ATTEMPTS
        # times, using the last successfully fetched value.
        current_balance = None
        for attempt in range(1, BALANCE_SETTLE_MAX_ATTEMPTS + 1):
            time.sleep(BALANCE_SETTLE_WAIT_SECONDS)
            current_balance = get_current_balance()
            print(f"Balance settle check {attempt}/{BALANCE_SETTLE_MAX_ATTEMPTS}: {current_balance}")
            if current_balance is not None:
                break

        if current_balance is None:
            print("WARNING: could not fetch current balance to determine WIN/LOSS after retries - will retry next poll.")
            return

        if current_balance > balance_before_trade:
            status_label = "WIN"
            wins = int(data.get("wins", 0)) + 1
            close_trade_record("WIN", exit_price)
            update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                              sl_order_id=None, entry_order_id=None,
                              tp_price=None, sl_price=None, entry_price=None,
                              entry_time=None, contracts=None,
                              balance_before_trade=None, wins=wins, scratch_armed=False, highest_profit_pct=0)
        else:
            status_label = "LOSS"
            losses = int(data.get("losses", 0)) + 1
            close_trade_record("LOSS", exit_price)
            update_bot_state(in_trade=False, trade_side=None, tp_order_id=None,
                              sl_order_id=None, entry_order_id=None,
                              tp_price=None, sl_price=None, entry_price=None,
                              entry_time=None, contracts=None,
                              balance_before_trade=None, losses=losses, scratch_armed=False, highest_profit_pct=0)

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

        # FIX (Sep 25 2026): replaced the single breakeven-only retrace
        # with tiered profit-locking floors. Real backtest evidence found
        # roughly half of all TIE/scratch closes were near-misses that
        # ran to 0.68-0.72% (right at the edge of the 0.75% TP) before
        # giving the whole move back for a flat/fee-only close. Tracks
        # the HIGHEST profit % actually reached (not sequential arming
        # steps), so a fast price move that jumps straight from 0.4% to
        # 0.72% between polls still gets the correct floor immediately,
        # not whatever the last-checked step happened to be.
        if profit_pct > highest_profit_pct:
            highest_profit_pct = profit_pct
            update_bot_state(highest_profit_pct=highest_profit_pct)

        floor_pct = None
        if highest_profit_pct >= 0.007:
            floor_pct = 0.0030
        elif highest_profit_pct >= 0.006:
            floor_pct = 0.0015
        elif highest_profit_pct >= SCRATCH_ARM_PCT:
            floor_pct = 0.0

        if floor_pct is not None and profit_pct <= floor_pct:
            print(f"RETRACE FLOOR HIT - peaked at {round(highest_profit_pct*100,3)}%, "
                  f"floor was {round(floor_pct*100,3)}%, now at {round(profit_pct*100,3)}% - closing")
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
