from __future__ import annotations

from trade_assistant.order_brackets import build_exit_order_drafts
from trade_assistant.risk import create_trade_plan


def test_build_exit_order_drafts_creates_stop_and_split_take_profit_orders() -> None:
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10.0, 9.5, 11.0, 1000, 1, 2)

    drafts = build_exit_order_drafts(plan)

    assert drafts[0]["type"] == "STOP_MARKET"
    assert drafts[0]["side"] == "SELL"
    assert drafts[0]["stopPrice"] == 9.5
    assert drafts[1]["type"] == "TAKE_PROFIT_MARKET"
    assert drafts[1]["stopPrice"] == 10.5
    assert drafts[1]["quantity"] == round(plan.quantity * 0.3, 8)
    assert drafts[-1]["reduceOnly"] is True
