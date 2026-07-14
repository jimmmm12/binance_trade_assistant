from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from .binance_client import BinanceClient


LIVE_CONFIRMATION = "ABC"


def build_order_payload(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str,
    price: float | None = None,
    reduce_only: bool = False,
    stop_price: float | None = None,
    post_only: bool = False,
    client_order_id: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": symbol,
        "side": side.upper(),
        "type": order_type.upper(),
        "quantity": _decimal_text(quantity),
    }
    if payload["type"] == "LIMIT":
        if price is None:
            raise ValueError("LIMIT order requires price")
        payload["price"] = price
        if post_only and market == "spot":
            payload["type"] = "LIMIT_MAKER"
        else:
            payload["timeInForce"] = "GTX" if post_only else "GTC"
    if payload["type"] in {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"}:
        if stop_price is None or stop_price <= 0:
            raise ValueError(f"{payload['type']} order requires stop_price")
        payload["stopPrice"] = stop_price
        if market == "futures":
            payload["workingType"] = "MARK_PRICE"
    if reduce_only:
        payload["reduceOnly"] = True
    if client_order_id:
        payload["newClientOrderId"] = client_order_id
    return payload


def _decimal_text(value: float | str | Decimal) -> str:
    decimal = Decimal(str(value)).normalize()
    if decimal == decimal.to_integral_value():
        return str(decimal.quantize(Decimal("1")))
    return format(decimal, "f")


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
    if market == "futures" and _is_algo_order(payload):
        request = _build_futures_algo_payload(payload)
        response = client.signed_post("futures", "/fapi/v1/algoOrder", request)
        if isinstance(response, dict):
            response.setdefault("clientOrderId", payload.get("newClientOrderId", ""))
            response.setdefault("orderId", response.get("algoId", ""))
            response.setdefault("status", "NEW")
        return response
    path = "/fapi/v1/order" if market == "futures" else "/api/v3/order"
    return client.signed_post(market, path, payload)


def _is_algo_order(payload: dict[str, Any]) -> bool:
    return str(payload.get("type", "")).upper() in {
        "STOP",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }


def _build_futures_algo_payload(payload: dict[str, Any]) -> dict[str, Any]:
    request = dict(payload)
    request["algoType"] = "CONDITIONAL"
    if request.get("stopPrice") not in (None, ""):
        request["triggerPrice"] = request.pop("stopPrice")
    client_id = request.pop("newClientOrderId", "")
    if client_id:
        request["newClientAlgoId"] = client_id
    return request
