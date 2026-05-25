import psycopg2
import requests
import time
import os

DATABASE_URL = os.environ.get("DATABASE_URL")
FEE_PCT = 0.0002
TP_PCT = 0.0045
SL_PCT = 0.0035

def get_db():
    return psycopg2.connect(DATABASE_URL)

def check_and_close():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM bot_state")
    data = dict(cur.fetchall())
    cur.close()
    conn.close()

    if data.get("in_trade") != "True":
        return

    tp = float(data["tp_price"])
    sl = float(data["sl_price"])
    side = data["trade_side"]
    balance = float(data["balance"])

    r = requests.get("https://api.crypto.com/v2/public/get-ticker?instrument_name=BTC_USDT", timeout=10)
    price = float(r.json()["result"]["data"][0]["a"])

    print(f"Price: {price} | TP: {tp} | SL: {sl} | Side: {side}")

    hit = None
    if side == "LONG":
        if price >= tp:
            hit = "WIN"
        elif price <= sl:
            hit = "LOSS"
    elif side == "SHORT":
        if price <= tp:
            hit = "WIN"
        elif price >= sl:
            hit = "LOSS"

    if hit:
        fee = round(balance * FEE_PCT, 2)
        pnl = round(balance * TP_PCT - fee, 2) if hit == "WIN" else round(-(balance * SL_PCT) - fee, 2)
        new_balance = round(balance + pnl, 2)
        wins = int(data["wins"]) + (1 if hit == "WIN" else 0)
        losses = int(data["losses"]) + (0 if hit == "WIN" else 1)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE trades SET status=%s, exit_price=%s, pnl=%s, balance_after=%s WHERE id=(SELECT id FROM trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1)", (hit, price, pnl, new_balance))
        cur.execute("UPDATE bot_state SET value=%s WHERE key='balance'", (str(new_balance),))
        cur.execute("UPDATE bot_state SET value='False' WHERE key='in_trade'")
        cur.execute("UPDATE bot_state SET value='None' WHERE key='trade_side'")
        cur.execute("UPDATE bot_state SET value='None' WHERE key='tp_price'")
        cur.execute("UPDATE bot_state SET value='None' WHERE key='sl_price'")
        cur.execute("UPDATE bot_state SET value='None' WHERE key='entry_price'")
        cur.execute("UPDATE bot_state SET value=%s WHERE key='wins'", (str(wins),))
        cur.execute("UPDATE bot_state SET value=%s WHERE key='losses'", (str(losses),))
        conn.commit()
        cur.close()
        conn.close()
        print(f"TRADE CLOSED: {hit} | Exit: {price} | PnL: {pnl} | New Balance: {new_balance}")

while True:
    try:
        check_and_close()
    except Exception as e:
        print(f"Error: {e}")
    time.sleep(30)
