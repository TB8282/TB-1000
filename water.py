import os
import time
import requests
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
TP_PCT = 0.0045
SL_PCT = 0.0035
FEE_PCT = 0.0002


def get_db():
    return psycopg2.connect(DATABASE_URL)


def fmt(n):
    try:
        return "${:,.2f}".format(float(n))
    except:
        return "$0.00"


def get_btc_price():
    try:
        r = requests.get("https://api.crypto.com/v2/public/get-ticker?instrument_name=BTC_USDT", timeout=3)
        price = float(r.json()["result"]["data"][0]["a"])
        return price
    except Exception as e:
        print(f"Price error: {str(e)}")
        return None


def get_trade_state():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM bot_state")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        data = {r[0]: r[1] for r in rows}
        in_trade = data.get("in_trade") == "True"
        side = data.get("trade_side") if data.get("trade_side") != "None" else None
        tp = float(data["tp_price"]) if data.get("tp_price") not in [None, "None"] else None
        sl = float(data["sl_price"]) if data.get("sl_price") not in [None, "None"] else None
        entry = float(data["entry_price"]) if data.get("entry_price") not in [None, "None"] else None
        balance = float(data.get("balance", 500))
        return in_trade, side, tp, sl, entry, balance
    except Exception as e:
        print(f"DB read error: {str(e)}")
        return False, None, None, None, None, 500


def close_trade(result, exit_price, entry, side, balance):
    try:
        fee = round(balance * FEE_PCT, 2)
        if result == "WIN":
            pnl = round(balance * TP_PCT - fee, 2)
        else:
            pnl = round(-(balance * SL_PCT) - fee, 2)

        new_balance = round(balance + pnl, 2)

        conn = get_db()
        cur = conn.cursor()

        # Update trade record
        cur.execute("""
            UPDATE trades SET status=%s, exit_price=%s, pnl=%s, balance_after=%s
            WHERE id = (SELECT MAX(id) FROM trades)
        """, (result, exit_price, pnl, new_balance))

        # Update bot state
        fields = {
            "in_trade": "False",
            "trade_side": "None",
            "entry_price": "None",
            "tp_price": "None",
            "sl_price": "None",
            "balance": str(new_balance),
        }
        if result == "WIN":
            cur.execute("SELECT value FROM bot_state WHERE key='wins'")
            row = cur.fetchone()
            wins = int(row[0]) + 1 if row else 1
            fields["wins"] = str(wins)
        else:
            cur.execute("SELECT value FROM bot_state WHERE key='losses'")
            row = cur.fetchone()
            losses = int(row[0]) + 1 if row else 1
            fields["losses"] = str(losses)

        for key, val in fields.items():
            cur.execute("""
                INSERT INTO bot_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, val))

        conn.commit()
        cur.close()
        conn.close()

        print(f"TRADE CLOSED: {result} | Side: {side} | Entry: {entry} | Exit: {exit_price} | PnL: {fmt(pnl)} | Balance: {fmt(new_balance)}")

    except Exception as e:
        print(f"Close trade error: {str(e)}")


check_count = 0
print("Watcher process started")

while True:
    try:
        time.sleep(1)
        check_count += 1

        price = get_btc_price()

        if check_count % 30 == 0:
            print(f"Watcher check #{check_count} | price: {price}")

        if price is None:
            continue

        in_trade, side, tp, sl, entry, balance = get_trade_state()

        if not in_trade:
            continue

        print(f"Watching: {price} | TP: {tp} | SL: {sl}")

        if side == "LONG":
            if price >= tp:
                close_trade("WIN", price, entry, side, balance)
            elif price <= sl:
                close_trade("LOSS", price, entry, side, balance)
        elif side == "SHORT":
            if price <= tp:
                close_trade("WIN", price, entry, side, balance)
            elif price >= sl:
                close_trade("LOSS", price, entry, side, balance)

    except Exception as e:
        print(f"Watcher error: {str(e)} - continuing in 5 seconds")
        time.sleep(5)
