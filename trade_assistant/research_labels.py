from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class TripleBarrierLabel:
    outcome: str
    return_r: float
    mfe_r: float
    mae_r: float
    bars_held: int


def label_triple_barrier(
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    future_bars: Iterable[tuple[float, float, float]],
    max_bars: int,
) -> TripleBarrierLabel:
    """Label future OHLC bars by first stop, target, or time barrier event."""
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0 or target <= 0:
        raise ValueError("entry, stop, and target must define positive risk")
    direction = 1 if side == "long" else -1
    mfe_r = 0.0
    mae_r = 0.0
    for index, (high, low, close) in enumerate(future_bars, start=1):
        favorable = ((high - entry) if direction > 0 else (entry - low)) / risk
        adverse = ((low - entry) if direction > 0 else (entry - high)) / risk
        mfe_r = max(mfe_r, favorable)
        mae_r = min(mae_r, adverse)
        hit_stop = low <= stop if direction > 0 else high >= stop
        hit_target = high >= target if direction > 0 else low <= target
        # Conservative label when both barriers occur in one candle.
        if hit_stop:
            return TripleBarrierLabel("stop", -1.0, round(mfe_r, 4), round(mae_r, 4), index)
        if hit_target:
            return TripleBarrierLabel("target", round(abs(target - entry) / risk, 4), round(mfe_r, 4), round(mae_r, 4), index)
        if index >= max_bars:
            return TripleBarrierLabel("time", round((close - entry) * direction / risk, 4), round(mfe_r, 4), round(mae_r, 4), index)
    return TripleBarrierLabel("insufficient_future_bars", 0.0, round(mfe_r, 4), round(mae_r, 4), 0)


def label_to_dict(label: TripleBarrierLabel) -> dict[str, float | int | str]:
    return asdict(label)
