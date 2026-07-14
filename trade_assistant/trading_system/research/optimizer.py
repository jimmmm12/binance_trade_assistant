from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OptimizationResult:
    parameters: dict[str, Any]
    objective: float
    metrics: dict[str, float | int]


class GridSearchOptimizer:
    """Offline-only parameter search; never mutates live strategy settings."""

    def optimize(
        self,
        parameter_grid: dict[str, list[Any]],
        evaluate,
        *,
        minimum_trades: int = 20,
        limit: int = 20,
    ) -> list[OptimizationResult]:
        if not parameter_grid:
            return []
        names = list(parameter_grid)
        results: list[OptimizationResult] = []
        for values in itertools.product(*(parameter_grid[name] for name in names)):
            parameters = dict(zip(names, values))
            metrics = dict(evaluate(parameters))
            trades = int(metrics.get("trades", 0))
            if trades < minimum_trades:
                continue
            average_r = float(metrics.get("average_r", 0))
            max_drawdown = max(0.0, float(metrics.get("max_drawdown", 0)))
            objective = average_r * (trades**0.5) - max_drawdown * 0.1
            results.append(OptimizationResult(parameters, round(objective, 8), metrics))
        results.sort(key=lambda item: item.objective, reverse=True)
        return results[: max(0, limit)]
