from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


def normalize_order_payload(
    client: Any,
    market: str,
    payload: dict[str, Any],
    *,
    allow_min_notional_bump: bool = False,
) -> dict[str, Any]:
    rules = symbol_filters(client, market, str(payload.get("symbol", "")))
    if rules is None:
        return payload
    order_type = str(payload.get("type", "")).upper()
    normalized = dict(payload)
    quantity = normalize_quantity(
        normalized["quantity"],
        rules.get("market_step_size") if order_type == "MARKET" else rules.get("step_size"),
        rules.get("market_min_qty") if order_type == "MARKET" else rules.get("min_qty"),
        normalized.get("symbol", ""),
    )
    normalized["quantity"] = quantity
    if normalized.get("price") not in (None, ""):
        normalized["price"] = normalize_price(normalized["price"], rules.get("tick_size"))
    if normalized.get("stopPrice") not in (None, ""):
        normalized["stopPrice"] = normalize_price(normalized["stopPrice"], rules.get("tick_size"))
    if (
        allow_min_notional_bump
        and not normalized.get("reduceOnly")
        and not normalized.get("_btaReduceOnlyIntent")
        and normalized.get("price") not in (None, "")
    ):
        normalized["quantity"] = bump_quantity_to_min_notional(
            normalized["quantity"],
            normalized["price"],
            rules.get("min_notional"),
            rules.get("market_step_size") if order_type == "MARKET" else rules.get("step_size"),
            rules.get("market_min_qty") if order_type == "MARKET" else rules.get("min_qty"),
        )
    if (
        not normalized.get("reduceOnly")
        and not normalized.get("_btaReduceOnlyIntent")
        and normalized.get("price") not in (None, "")
    ):
        validate_min_notional(
            normalized["quantity"],
            normalized["price"],
            rules.get("min_notional"),
            normalized.get("symbol", ""),
        )
    return normalized


def normalize_quantity(value: Any, step_size: Any, min_qty: Any, symbol: str = "") -> str:
    parsed = Decimal(str(value))
    step = Decimal(str(step_size or "0"))
    minimum = Decimal(str(min_qty or "0"))
    if step > 0:
        parsed = (parsed / step).to_integral_value(rounding=ROUND_DOWN) * step
    if parsed <= 0:
        prefix = f"{symbol} " if symbol else ""
        raise ValueError(f"{prefix}下单数量按交易所精度规整后为 0")
    if minimum > 0 and parsed < minimum:
        prefix = f"{symbol} " if symbol else ""
        raise ValueError(f"{prefix}下单数量低于最小数量 {decimal_text(minimum)}")
    return decimal_text(parsed)


def validate_min_notional(quantity: Any, price: Any, min_notional: Any, symbol: str = "") -> None:
    minimum = Decimal(str(min_notional or "0"))
    if minimum <= 0:
        return
    notional = Decimal(str(quantity)) * Decimal(str(price))
    if notional < minimum:
        prefix = f"{symbol} " if symbol else ""
        raise ValueError(
            f"{prefix}订单名义价值 {decimal_text(notional)} USDT 低于交易所最小值 {decimal_text(minimum)} USDT"
        )


def bump_quantity_to_min_notional(
    quantity: Any,
    price: Any,
    min_notional: Any,
    step_size: Any,
    min_qty: Any,
) -> str:
    minimum = Decimal(str(min_notional or "0"))
    parsed = Decimal(str(quantity))
    parsed_price = Decimal(str(price))
    if minimum <= 0 or parsed_price <= 0 or parsed * parsed_price >= minimum:
        return decimal_text(parsed)
    step = Decimal(str(step_size or "0"))
    min_quantity = Decimal(str(min_qty or "0"))
    target = minimum / parsed_price
    if min_quantity > 0:
        target = max(target, min_quantity)
    if step > 0:
        target = (target / step).to_integral_value(rounding=ROUND_UP) * step
    if target <= parsed:
        target = parsed
    return decimal_text(target)


def normalize_price(value: Any, tick_size: Any) -> str:
    parsed = Decimal(str(value))
    tick = Decimal(str(tick_size or "0"))
    if tick > 0:
        parsed = (parsed / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    if parsed <= 0:
        raise ValueError("下单价格必须大于 0")
    return decimal_text(parsed)


def symbol_filters(client: Any, market: str, symbol: str) -> dict[str, str] | None:
    if not hasattr(client, "public_get") or not symbol:
        return None
    try:
        data = client.public_get(
            market,
            "/fapi/v1/exchangeInfo" if market == "futures" else "/api/v3/exchangeInfo",
            {"symbol": symbol.upper()},
        )
    except Exception:
        return None
    symbols = data.get("symbols", []) if isinstance(data, dict) else []
    row = next((item for item in symbols if item.get("symbol") == symbol.upper()), None)
    if not row:
        return None
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    lot = filters.get("LOT_SIZE") or {}
    market_lot = filters.get("MARKET_LOT_SIZE") or lot
    price_filter = filters.get("PRICE_FILTER") or {}
    min_notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    return {
        "step_size": str(lot.get("stepSize", "0")),
        "min_qty": str(lot.get("minQty", "0")),
        "market_step_size": str(market_lot.get("stepSize", lot.get("stepSize", "0"))),
        "market_min_qty": str(market_lot.get("minQty", lot.get("minQty", "0"))),
        "tick_size": str(price_filter.get("tickSize", "0")),
        "min_notional": str(
            min_notional_filter.get("notional", min_notional_filter.get("minNotional", "0"))
        ),
    }


def decimal_text(value: Any) -> str:
    decimal = Decimal(str(value)).normalize()
    if decimal == decimal.to_integral_value():
        return str(decimal.quantize(Decimal("1")))
    return format(decimal, "f")
