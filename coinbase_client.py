"""
Coinbase Futures (CFM) API Client
===================================
Drop-in replacement for kraken_client.py. Same method interface, so
app.py and worker.py need minimal changes: get_balance, place_entry_order,
place_close_order, cancel_order, query_orders, get_ticker.

FIX (Sep 17 2026): place_entry_order() now supports attaching a native
Coinbase bracket (trigger_bracket_gtc) directly to the entry order via
tp_price/sl_price args. This replaces placing TWO separate follow-up
orders (the old approach in place_close_order, still kept below for
backward compatibility / manual use) with ONE atomic order: Coinbase's
own docs confirm "As soon as a fill occurs for the order at one of the
specified price levels, the other side is automatically disabled" -
true exchange-enforced OCO, not dependent on worker.py polling in time.

PRODUCT ID: nano BTC Perp Futures is NOT just "BIP" - it includes a
dated suffix (confirmed live): "BIP-20DEC30-CDE". This is set as
PRODUCT_ID below and used for all calls.

CONFIRMED FEES (live API, tested tonight):
- Commission: $1.08192 per contract per side (flat, not %)
- Funding: $0.10/hour per $1,000 notional (confirmed via Coinbase docs)

CONTRACT SIZE: 0.01 BTC per contract. "volume" in this client's method
signatures is CONTRACTS (whole numbers), not BTC amount - this differs
from Kraken, where volume was in BTC. app.py's calculate_volume() must
be updated to calculate contract count, not BTC volume.
"""

import os
from coinbase.rest import RESTClient

PRODUCT_ID = "BIP-20DEC30-CDE"


class CoinbaseClient:
    def __init__(self, api_key_name, api_key_private_key):
        self.client = RESTClient(api_key=api_key_name, api_secret=api_key_private_key)

    def get_balance(self):
        """
        Returns futures balance summary. Callers should read
        result['balance_summary']['cfm_usd_balance']['value'] for
        available USD balance in the futures (CFM) account.
        """
        try:
            summary = self.client.get_futures_balance_summary()
            return {"result": summary.to_dict(), "error": None}
        except Exception as e:
            return {"result": None, "error": str(e)}

    def get_ticker(self):
        """
        Public-ish call using the authenticated client to get current
        best bid/ask for PRODUCT_ID. Returns a dict shaped so callers
        can pull a current price the same way get_ticker was used
        with Kraken.
        """
        try:
            product = self.client.get_product(product_id=PRODUCT_ID)
            price = float(product.price)
            return {"result": {"price": price}, "error": None}
        except Exception as e:
            return {"result": None, "error": str(e)}

    def place_entry_order(self, side, contracts, leverage, validate=False, tp_price=None, sl_price=None):
        """
        Places a market entry order. side: "buy" or "sell".
        contracts: whole number of BIP contracts (NOT BTC volume).

        FIX (Sep 17 2026): if tp_price and sl_price are both given, a
        native Coinbase bracket (trigger_bracket_gtc) is attached to
        this SAME order via attached_order_configuration - TP/SL exist
        from the moment the entry fills, no follow-up calls needed.
        When validate=True (margin preview), the same attached config
        is included so the previewed margin reflects the real bracketed
        order, not a bare entry.

        Returns dict with 'result' containing 'order_id' on success
        (and 'bracket_order_id' when a bracket was attached - see the
        "BRACKET DEBUG" raw-response log in app.py if this ever comes
        back None, since Coinbase's exact field name for the attached
        order's own ID was not 100% confirmed from documentation alone
        and may need a one-line adjustment after seeing a real response).
        """
        cb_side = "BUY" if side.lower() == "buy" else "SELL"
        attached_config = None
        if tp_price is not None and sl_price is not None:
            attached_config = {
                "trigger_bracket_gtc": {
                    "limit_price": str(tp_price),
                    "stop_trigger_price": str(sl_price),
                }
            }
        try:
            if validate:
                kwargs = dict(
                    product_id=PRODUCT_ID,
                    side=cb_side,
                    order_configuration={
                        "market_market_ioc": {"base_size": str(contracts)}
                    },
                    leverage=str(leverage),
                )
                if attached_config:
                    kwargs["attached_order_configuration"] = attached_config
                resp = self.client.preview_order(**kwargs)
                return {"result": resp.to_dict(), "error": None}

            kwargs = dict(
                client_order_id=os.urandom(8).hex(),
                product_id=PRODUCT_ID,
                side=cb_side,
                base_size=str(contracts),
                leverage=str(leverage),
            )
            if attached_config:
                kwargs["attached_order_configuration"] = attached_config
            resp = self.client.market_order(**kwargs)
            resp_dict = resp.to_dict()
            if not resp_dict.get("success"):
                return {"result": None, "error": resp_dict.get("error_response")}
            order_id = resp_dict.get("success_response", {}).get("order_id")
            # Best-effort extraction of the attached bracket's own order ID.
            # Not 100% confirmed which key Coinbase uses here - checking a
            # few plausible locations. app.py logs the full raw response
            # so this can be corrected in one line if it comes back None.
            bracket_order_id = (
                resp_dict.get("success_response", {}).get("attached_order_id")
                or resp_dict.get("attached_order_id")
                or resp_dict.get("success_response", {}).get("attached_order", {}).get("order_id")
            )
            return {
                "result": {
                    "order_id": order_id,
                    "bracket_order_id": bracket_order_id,
                    "raw": resp_dict,
                },
                "error": None,
            }
        except Exception as e:
            return {"result": None, "error": str(e)}

    def place_close_order(self, side, contracts, ordertype, price, leverage, validate=False):
        """
        LEGACY / MANUAL USE ONLY as of Sep 17 2026: places ONE of the two
        standalone close orders (either TP or SL) as independent orders.
        open_trade() in app.py no longer calls this for new trades - TP/SL
        are now attached directly to the entry order in place_entry_order().
        Kept here only for manual/recovery use if ever needed.
        side: opposite of entry side ("buy" or "sell").
        ordertype: "take-profit" or "stop-loss".
        price: trigger/limit price (absolute, not percentage).
        """
        cb_side = "BUY" if side.lower() == "buy" else "SELL"
        try:
            if ordertype == "take-profit":
                order_config = {
                    "limit_limit_gtc": {
                        "base_size": str(contracts),
                        "limit_price": str(price),
                        "post_only": False,
                    }
                }
            elif ordertype == "stop-loss":
                # stop_direction depends on which way triggers the stop:
                # closing a LONG means SELLing when price drops -> STOP_DOWN
                # closing a SHORT means BUYing when price rises -> STOP_UP
                stop_direction = (
                    "STOP_DIRECTION_STOP_DOWN" if cb_side == "SELL"
                    else "STOP_DIRECTION_STOP_UP"
                )
                order_config = {
                    "stop_limit_stop_limit_gtc": {
                        "base_size": str(contracts),
                        "limit_price": str(price),
                        "stop_price": str(price),
                        "stop_direction": stop_direction,
                    }
                }
            else:
                return {"result": None, "error": f"Unknown ordertype: {ordertype}"}

            if validate:
                resp = self.client.preview_order(
                    product_id=PRODUCT_ID,
                    side=cb_side,
                    order_configuration=order_config,
                    leverage=str(leverage),
                )
                return {"result": resp.to_dict(), "error": None}

            resp = self.client.create_order(
                client_order_id=os.urandom(8).hex(),
                product_id=PRODUCT_ID,
                side=cb_side,
                order_configuration=order_config,
                leverage=str(leverage),
            )
            resp_dict = resp.to_dict()
            if not resp_dict.get("success"):
                return {"result": None, "error": resp_dict.get("error_response")}
            order_id = resp_dict.get("success_response", {}).get("order_id")
            return {"result": {"order_id": order_id, "raw": resp_dict}, "error": None}
        except Exception as e:
            return {"result": None, "error": str(e)}

    def get_open_bracket_order(self, exclude_order_id=None):
        """
        FIX (Sep 17 2026): Coinbase's entry-order response has an
        attached_order_id field, but on the first real test it came back
        as an empty string, not the real bracket order's ID - the bracket
        order likely isn't registered on Coinbase's side yet at the exact
        instant the entry order responds. This works around that by
        directly listing open orders for PRODUCT_ID and returning the
        first one that isn't the just-filled entry order - that's the
        live TP/SL bracket order, confirmed to exist (seen directly in
        the Coinbase UI as a single linked "TP/SL" order).
        """
        try:
            resp = self.client.list_orders(product_id=PRODUCT_ID, order_status="OPEN")
            resp_dict = resp.to_dict()
            orders = resp_dict.get("orders", [])
            for o in orders:
                oid = o.get("order_id")
                if oid and oid != exclude_order_id:
                    return {"result": {"order_id": oid, "raw": o}, "error": None}
            return {"result": None, "error": f"No open orders found besides entry. Raw: {resp_dict}"}
        except Exception as e:
            return {"result": None, "error": str(e)}

    def cancel_order(self, order_id):
        try:
            resp = self.client.cancel_orders(order_ids=[order_id])
            return {"result": resp.to_dict(), "error": None}
        except Exception as e:
            return {"result": None, "error": str(e)}

    def query_orders(self, order_ids):
        """
        order_ids: list of order IDs to check status on.
        Returns dict shaped as {"result": {order_id: {"status": ..., "price": ...}}}
        to match the shape worker.py/app.py already expect from Kraken.
        """
        try:
            result = {}
            for oid in order_ids:
                order = self.client.get_order(order_id=oid)
                order_dict = order.to_dict().get("order", {})
                status = order_dict.get("status")
                # Map Coinbase's FILLED status to Kraken's "closed" so
                # existing worker.py logic (checking for "closed") works
                # unchanged.
                mapped_status = "closed" if status == "FILLED" else status
                avg_price = order_dict.get("average_filled_price", 0)
                result[oid] = {"status": mapped_status, "price": avg_price}
            return {"result": result, "error": None}
        except Exception as e:
            return {"result": None, "error": str(e)}
