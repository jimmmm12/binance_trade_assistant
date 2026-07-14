from __future__ import annotations

from collections.abc import Sequence


def ema(values: list[float], period: int) -> float:
    if not values:
        raise ValueError("values must not be empty")
    k = 2 / (period + 1)
    value = values[0]
    for price in values[1:]:
        value = price * k + value * (1 - k)
    return value


def ema_series(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    result = [float(values[0])]
    for price in values[1:]:
        result.append(float(price) * k + result[-1] * (1 - k))
    return result


def macd_histogram(
    values: Sequence[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> list[float]:
    if not values:
        return []
    fast = ema_series(values, fast_period)
    slow = ema_series(values, slow_period)
    macd_line = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    signal_line = ema_series(macd_line, signal_period)
    return [macd_value - signal_value for macd_value, signal_value in zip(macd_line, signal_line)]


def adx(klines: Sequence[Sequence], period: int = 14) -> float | None:
    """Return Wilder ADX from Binance-style OHLC rows."""
    if len(klines) < period * 2 + 1:
        return None

    true_ranges: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index in range(1, len(klines)):
        high = float(klines[index][2])
        low = float(klines[index][3])
        previous_high = float(klines[index - 1][2])
        previous_low = float(klines[index - 1][3])
        previous_close = float(klines[index - 1][4])
        up_move = high - previous_high
        down_move = previous_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])
    dx_values: list[float] = []
    for index in range(period, len(true_ranges)):
        smoothed_tr = smoothed_tr - smoothed_tr / period + true_ranges[index]
        smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm[index]
        smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm[index]
        if smoothed_tr <= 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * smoothed_plus / smoothed_tr
        minus_di = 100 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di
        dx_values.append(0.0 if denominator <= 0 else 100 * abs(plus_di - minus_di) / denominator)

    if len(dx_values) < period:
        return None
    adx_value = sum(dx_values[:period]) / period
    for dx_value in dx_values[period:]:
        adx_value = (adx_value * (period - 1) + dx_value) / period
    return adx_value


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
