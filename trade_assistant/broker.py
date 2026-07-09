from __future__ import annotations

import os
from typing import Any

from .binance_client import BinanceClient


LIVE_CONFIRMATION = "LIVE_TRADING_CONFIRMED"


def build_order_payload(symbol: str, side: str, quantity: float, order_type: str, price: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": symbol,
        "side": side.upper(),
        "type": order_type.upper(),
        "quantity": quantity,
    }
    if payload["type"] == "LIMIT":
        if price is None:
            raise ValueError("LIMIT order requires price")
        payload["price"] = price
        payload["timeInForce"] = "GTC"
    return payload


def place_order(
    client: BinanceClient,
    market: str,
    payload: dict[str, Any],
    allow_live: bool,
    confirm: str | None,
) -> dict[str, Any]:
    live_switch = os.getenv("BINANCE_ENABLE_LIVE_TRADING", "").lower() == "true"
    if not allow_live or not live_switch or confirm != LIVE_CONFIRMATION:
        return {"dry_run": True, "market": market, "payload": payload}
    path = "/fapi/v1/order" if market == "futures" else "/api/v3/order"
    return client.signed_post(market, path, payload)

