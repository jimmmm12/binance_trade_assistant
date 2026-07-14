from __future__ import annotations

from trade_assistant.order_pricing import maker_limit_price


def test_maker_price_uses_best_bid_for_buy_and_best_ask_for_sell() -> None:
    assert maker_limit_price("BUY", best_bid=99.9, best_ask=100.1, reference_price=100) == 99.9
    assert maker_limit_price("SELL", best_bid=99.9, best_ask=100.1, reference_price=100) == 100.1


def test_maker_price_uses_passive_offset_without_a_quote() -> None:
    assert maker_limit_price("BUY", reference_price=100, fallback_offset_bps=5) == 99.95
    assert maker_limit_price("SELL", reference_price=100, fallback_offset_bps=5) == 100.05
