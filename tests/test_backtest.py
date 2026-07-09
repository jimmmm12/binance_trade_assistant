from __future__ import annotations

from trade_assistant.backtest import backtest_atr_plan


def test_backtest_atr_plan_reports_win_rate_average_r_and_drawdown() -> None:
    closes = [10, 10.1, 10.3, 10.5, 10.2, 10.6, 10.9, 11.1, 10.8, 11.2]

    result = backtest_atr_plan(closes, side="long", stop_pct=2.0, reward_risk=1.5, lookahead=3)

    assert result.trades > 0
    assert 0 <= result.win_rate <= 100
    assert result.average_r > 0
    assert result.max_drawdown_r <= 0
