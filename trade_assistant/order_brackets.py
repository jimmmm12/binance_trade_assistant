from __future__ import annotations

from typing import Any

from .models import TradePlan


def build_exit_order_drafts(plan: TradePlan) -> list[dict[str, Any]]:
    close_side = "SELL" if plan.side == "long" else "BUY"
    quantity = round(plan.quantity, 8)
    first_tp_qty = round(quantity * 0.3, 8)
    second_tp_qty = round(quantity * 0.3, 8)
    tail_qty = round(max(0.0, quantity - first_tp_qty - second_tp_qty), 8)
    one_r = abs(plan.entry - plan.stop)
    if plan.side == "long":
        one_r_price = plan.entry + one_r
        one_half_r_price = plan.entry + one_r * 1.5
        two_r_price = plan.entry + one_r * 2
    else:
        one_r_price = plan.entry - one_r
        one_half_r_price = plan.entry - one_r * 1.5
        two_r_price = plan.entry - one_r * 2
    return [
        _draft(plan.symbol, close_side, "STOP_MARKET", quantity, plan.stop),
        _draft(plan.symbol, close_side, "TAKE_PROFIT_MARKET", first_tp_qty, one_r_price),
        _draft(plan.symbol, close_side, "TAKE_PROFIT_MARKET", second_tp_qty, one_half_r_price),
        _draft(plan.symbol, close_side, "TAKE_PROFIT_MARKET", tail_qty, two_r_price),
    ]


def _draft(symbol: str, side: str, order_type: str, quantity: float, stop_price: float) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
        "stopPrice": round(stop_price, 8),
        "reduceOnly": True,
        "workingType": "MARK_PRICE",
    }
