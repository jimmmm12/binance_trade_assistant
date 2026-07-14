from __future__ import annotations

from typing import Any

from ...binance_client import BinanceClient
from ...broker import place_order


class BinanceExecutionEngine:
    def __init__(self, client: BinanceClient) -> None:
        self.client = client

    def submit(
        self,
        market: str,
        payload: dict[str, Any],
        allow_live: bool,
        confirm: str,
    ) -> dict[str, Any]:
        return place_order(self.client, market, payload, allow_live, confirm)

    def set_futures_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self.client.set_futures_leverage(symbol, leverage)

    def query(
        self,
        market: str,
        symbol: str,
        client_order_id: str,
        *,
        algo: bool = False,
        exchange_order_id: str | None = None,
    ) -> dict[str, Any]:
        if market == "futures" and algo:
            return self.client.query_algo_order(symbol, client_order_id, exchange_order_id)
        return self.client.query_order(market, symbol, client_order_id)

    def cancel(
        self,
        market: str,
        symbol: str,
        client_order_id: str,
        *,
        algo: bool = False,
        exchange_order_id: str | None = None,
    ) -> dict[str, Any]:
        if market == "futures" and algo:
            return self.client.cancel_algo_order(symbol, client_order_id, exchange_order_id)
        return self.client.cancel_order(market, symbol, client_order_id)

    def open_orders(self, market: str, symbol: str | None = None) -> list[dict[str, Any]]:
        return self.client.open_orders(market, symbol)
