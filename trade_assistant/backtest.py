from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestResult:
    trades: int
    win_rate: float
    average_r: float
    max_drawdown_r: float


def backtest_atr_plan(
    closes: list[float],
    side: str,
    stop_pct: float,
    reward_risk: float,
    lookahead: int = 6,
) -> BacktestResult:
    results: list[float] = []
    for index, entry in enumerate(closes[:-lookahead]):
        stop_distance = entry * stop_pct / 100
        future = closes[index + 1 : index + 1 + lookahead]
        result = _trade_r(entry, future, side, stop_distance, reward_risk)
        results.append(result)
    if not results:
        return BacktestResult(0, 0.0, 0.0, 0.0)
    wins = sum(1 for item in results if item > 0)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for item in results:
        equity += item
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return BacktestResult(
        trades=len(results),
        win_rate=round(wins / len(results) * 100, 2),
        average_r=round(sum(results) / len(results), 3),
        max_drawdown_r=round(max_drawdown, 3),
    )


def _trade_r(entry: float, future: list[float], side: str, stop_distance: float, reward_risk: float) -> float:
    stop = entry + stop_distance if side == "short" else entry - stop_distance
    target = entry - stop_distance * reward_risk if side == "short" else entry + stop_distance * reward_risk
    for price in future:
        if side == "short":
            if price >= stop:
                return -1.0
            if price <= target:
                return reward_risk
        else:
            if price <= stop:
                return -1.0
            if price >= target:
                return reward_risk
    last = future[-1]
    raw = (entry - last) / stop_distance if side == "short" else (last - entry) / stop_distance
    return max(-1.0, min(reward_risk, raw))
