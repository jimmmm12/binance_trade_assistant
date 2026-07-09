from __future__ import annotations

from trade_assistant.strategy import average_true_range_pct


def test_average_true_range_pct_uses_high_low_and_previous_close() -> None:
    klines = [
        [0, "100", "110", "95", "105", "1000"],
        [0, "105", "112", "101", "108", "1000"],
        [0, "108", "118", "104", "116", "1000"],
    ]

    atr_pct = average_true_range_pct(klines, period=2)

    assert round(atr_pct, 4) == 10.7759


def test_average_true_range_pct_returns_none_without_enough_history() -> None:
    klines = [[0, "100", "110", "95", "105", "1000"]]

    assert average_true_range_pct(klines, period=14) is None
