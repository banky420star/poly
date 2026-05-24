"""Direct Polymarket CLOB REST API client with API key/secret/passphrase auth."""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("clob")

CLOB_BASE = "https://clob.polymarket.com"


class CLOBExecutor:
    """Live Polymarket CLOB order execution using L2 API credentials."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        base_url: str = CLOB_BASE,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(30.0))
        return self._client

    def _sign(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time()))
        message = f"{timestamp}{method}{path}{body}"
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.passphrase,
            "POLY_TIMESTAMP": timestamp,
            "POLY_SIGNATURE": signature,
        }

    def _request(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body_str = json.dumps(body) if body else ""
        headers = self._sign(method, path, body_str)
        headers["Content-Type"] = "application/json"
        try:
            resp = self.client.request(method, url, headers=headers, content=body_str)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"CLOB {method} {path} error: {e}")
            return {"error": str(e)}

    def get_orders(self) -> Dict:
        return self._request("GET", "/orders")

    def get_trades(self) -> Dict:
        return self._request("GET", "/trades")

    def get_orderbook(self, token_id: str) -> Dict:
        return self._request("GET", f"/book?token_id={token_id}")

    def place_limit_order(
        self, token_id: str, price: float, size: float,
        side: str = "BUY", tick_size: str = "0.01",
    ) -> Dict:
        return self._request("POST", "/order", {
            "tokenID": token_id,
            "price": str(price),
            "size": str(size),
            "side": side.upper(),
            "tickSize": tick_size,
            "orderType": "GTC",
        })

    def place_market_order(
        self, token_id: str, amount: float,
        side: str = "BUY", tick_size: str = "0.01",
    ) -> Dict:
        endpoint = "/market-buy" if side.upper() == "BUY" else "/market-sell"
        return self._request("POST", endpoint, {
            "tokenID": token_id,
            "amount": str(amount),
            "tickSize": tick_size,
        })

    def cancel_order(self, order_id: str) -> Dict:
        return self._request("DELETE", f"/order/{order_id}")

    def cancel_all(self) -> Dict:
        return self._request("DELETE", "/orders")

    def close(self):
        if self._client:
            self._client.close()
            self._client = None
