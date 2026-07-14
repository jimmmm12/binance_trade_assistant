from __future__ import annotations

from trade_assistant.trading_system.research.optimizer import GridSearchOptimizer


def test_grid_optimizer_filters_small_samples_and_ranks_drawdown_adjusted_result() -> None:
    def evaluate(parameters):
        multiplier = parameters["atr_multiplier"]
        if multiplier == 1.0:
            return {"trades": 10, "average_r": 1.0, "max_drawdown": 1}
        if multiplier == 1.5:
            return {"trades": 30, "average_r": 0.35, "max_drawdown": 4}
        return {"trades": 30, "average_r": 0.25, "max_drawdown": 2}

    results = GridSearchOptimizer().optimize(
        {"atr_multiplier": [1.0, 1.5, 2.0]}, evaluate, minimum_trades=20
    )

    assert len(results) == 2
    assert results[0].parameters["atr_multiplier"] == 1.5
