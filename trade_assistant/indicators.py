from __future__ import annotations


def ema(values: list[float], period: int) -> float:
    if not values:
        raise ValueError("values must not be empty")
    k = 2 / (period + 1)
    value = values[0]
    for price in values[1:]:
        value = price * k + value * (1 - k)
    return value


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    start = len(closes) - period
    for index in range(start, len(closes)):
        diff = closes[index] - closes[index - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return (current / previous - 1) * 100


def average(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)

