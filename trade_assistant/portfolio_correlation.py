from __future__ import annotations

from math import sqrt
from typing import Iterable

from .models import ScoredSignal


def signed_return_correlation(left: ScoredSignal, right: ScoredSignal, lookback: int = 48) -> float | None:
    """Correlation of directional PnL, not merely raw token return correlation."""
    left_returns = left.signal.returns_1h[-lookback:]
    right_returns = right.signal.returns_1h[-lookback:]
    length = min(len(left_returns), len(right_returns))
    if length < 12:
        return None
    raw = _pearson(left_returns[-length:], right_returns[-length:])
    if raw is None:
        return None
    left_sign = 1 if left.side == "long" else -1
    right_sign = 1 if right.side == "long" else -1
    return round(raw * left_sign * right_sign, 4)


def correlation_gate(
    candidate: ScoredSignal,
    references: Iterable[ScoredSignal],
    *,
    max_signed_correlation: float = 0.75,
    max_correlated_positions: int = 1,
    lookback: int = 48,
) -> tuple[bool, str]:
    matches: list[tuple[str, float]] = []
    for reference in references:
        if reference.symbol == candidate.symbol:
            continue
        correlation = signed_return_correlation(candidate, reference, lookback)
        if correlation is not None and correlation >= max_signed_correlation:
            matches.append((reference.symbol, correlation))
    if len(matches) >= max(1, max_correlated_positions):
        symbols = "、".join(f"{symbol}({value:.2f})" for symbol, value in matches[:3])
        return False, f"相关性风险簇已满：与 {symbols} 同向风险高度相关"
    return True, "相关性风险簇通过"


def _pearson(left: tuple[float, ...], right: tuple[float, ...]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_scale = sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = sqrt(sum((y - right_mean) ** 2 for y in right))
    if left_scale <= 1e-12 or right_scale <= 1e-12:
        return None
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))
