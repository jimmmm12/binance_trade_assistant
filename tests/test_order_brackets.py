from __future__ import annotations

from trade_assistant.order_brackets import build_exit_order_drafts
from trade_assistant.broker import build_order_payload
from trade_assistant.risk import create_trade_plan


def test_build_exit_order_drafts_creates_stop_and_split_take_profit_orders() -> None:
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10.0, 9.5, 11.0, 1000, 1, 2)

    drafts = build_exit_order_drafts(plan)

    assert drafts[0]["type"] == "STOP_MARKET"
    assert drafts[0]["side"] == "SELL"
    assert drafts[0]["stopPrice"] == 9.5
    assert drafts[1]["type"] == "TAKE_PROFIT_MARKET"
    assert drafts[1]["stopPrice"] == 10.75
    assert drafts[1]["quantity"] == round(plan.quantity * 0.3, 8)
    assert drafts[-1]["reduceOnly"] is True


def test_build_order_payload_supports_futures_stop_and_post_only() -> None:
    stop = build_order_payload(
        "UNIUSDT",
        "SELL",
        2,
        "STOP_MARKET",
        reduce_only=True,
        stop_price=9.5,
        client_order_id="BTA_STOP",
        market="futures",
    )
    post_only = build_order_payload(
        "UNIUSDT", "BUY", 2, "LIMIT", 10, post_only=True, market="futures"
    )

    assert stop["stopPrice"] == 9.5
    assert stop["workingType"] == "MARK_PRICE"
    assert stop["newClientOrderId"] == "BTA_STOP"
    assert stop["quantity"] == "2"
    assert post_only["timeInForce"] == "GTX"
