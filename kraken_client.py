"""
Kraken API Client
==================
Handles authenticated requests to Kraken's REST API: placing orders,
cancelling orders, checking balance, and querying order status.

IMPORTANT: Kraken's API only supports ONE conditional close order per
entry (confirmed via live test tonight - close2 parameter is silently
ignored). This means TP and SL must be placed as TWO SEPARATE orders
after entry, and whichever doesn't fill must be cancelled manually.
"""

import time
import base64
import hashlib
import hmac
import urllib.parse
import requests

API_URL = "https://api.kraken.com"


class KrakenClient:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret

    def _get_signature(self, urlpath, data):
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data['nonce']) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _request(self, uri_path, data):
        data['nonce'] = str(int(1000 * time.time()))
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._get_signature(uri_path, data),
        }
        resp = requests.post(API_URL + uri_path, headers=headers, data=data, timeout=15)
        return resp.json()

    def get_balance(self):
        return self._request("/0/private/Balance", {})

    def place_entry_order(self, pair, side, volume, leverage, validate=False):
        """
        Places a market entry order with leverage. Returns Kraken's response,
        including the order's txid if successful.
        """
        data = {
            "ordertype": "market",
            "type": side,  # "buy" or "sell"
            "volume": str(volume),
            "pair": pair,
            "leverage": str(leverage),
        }
        if validate:
            data["validate"] = "true"
        return self._request("/0/private/AddOrder", data)

    def place_close_order(self, pair, side, volume, ordertype, price, leverage, validate=False):
        """
        Places a standalone conditional order (stop-loss OR take-profit),
        used as the SECOND of the two separate close orders.
        side should be the OPPOSITE of the entry side (e.g. entry was "buy",
        this close order should be "sell").
        ordertype: "stop-loss" or "take-profit"
        price: the trigger price (absolute price, not a percentage)
        """
        data = {
            "ordertype": ordertype,
            "type": side,
            "volume": str(volume),
            "pair": pair,
            "price": str(price),
            "leverage": str(leverage),
            "reduce_only": "true",
        }
        if validate:
            data["validate"] = "true"
        return self._request("/0/private/AddOrder", data)

    def cancel_order(self, txid):
        return self._request("/0/private/CancelOrder", {"txid": txid})

    def query_orders(self, txids):
        """txids: list of order IDs to check status on"""
        return self._request("/0/private/QueryOrders", {"txid": ",".join(txids)})

    def get_ticker(self, pair):
        """Public endpoint, no auth needed - current market price"""
        resp = requests.get(f"{API_URL}/0/public/Ticker", params={"pair": pair}, timeout=10)
        return resp.json()
