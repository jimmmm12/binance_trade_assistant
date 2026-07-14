from __future__ import annotations

from trade_assistant.indicators import adx, macd_histogram


def test_adx_detects_a_persistent_trend() -> None:
    rows = []
    for index in range(80):
        close = 100 + index * 1.2
        rows.append([index, close - 0.5, close + 1.0, close - 1.0, close, 1000])

    value = adx(rows, period=14)

    assert value is not None
    assert value >= 25


def test_macd_histogram_reports_expanding_positive_momentum() -> None:
    closes = [100 + index * 0.1 for index in range(40)] + [104 + 0.05 * index * index for index in range(20)]

    histogram = macd_histogram(closes)

    assert histogram[-1] > 0
    assert histogram[-1] > histogram[-10]
