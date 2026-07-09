from __future__ import annotations

from trade_assistant.adaptive_parameters import adapt_parameters
from trade_assistant.backtest import BacktestResult
from trade_assistant.models import Signal


def _signal(**overrides) -> Signal:
    data = {
        "market": "futures",
        "symbol": "UNIUSDT",
        "side": "long",
        "score": 8,
        "last": 10.0,
        "change_24h": 2.0,
        "quote_volume_m": 180,
        "rsi_1h": 60,
        "rsi_4h": 58,
        "volume_ratio": 1.5,
        "momentum_24h": 2.5,
        "momentum_3d": 4.0,
        "funding_pct": 0.01,
        "note": "偏多观察",
        "atr_pct": 2.0,
        "atr_1h_pct": 2.0,
    }
    data.update(overrides)
    return Signal(**data)


def test_adapt_parameters_keeps_good_liquid_setup_tradeable() -> None:
    params = adapt_parameters(_signal(), mode="intraday")

    assert params.atr_multiplier == 1.4
    assert params.reward_risk == 1.8
    assert params.risk_pct == 1.0
    assert params.allow_live is True
    assert "流动性充足" in params.reasons


def test_adapt_parameters_reduces_risk_for_high_volatility_and_crowded_funding() -> None:
    params = adapt_parameters(
        _signal(quote_volume_m=35, atr_pct=6.0, atr_1h_pct=6.0, funding_pct=0.12),
        mode="intraday",
    )

    assert params.atr_multiplier > 1.4
    assert params.risk_pct <= 0.4
    assert params.suggested_leverage == 1.0
    assert params.allow_live is False
    assert any("波动" in warning for warning in params.warnings)


def test_adapt_parameters_uses_backtest_to_reduce_reward_and_risk() -> None:
    params = adapt_parameters(
        _signal(),
        mode="intraday",
        backtest=BacktestResult(trades=30, win_rate=32.0, average_r=-0.12, max_drawdown_r=-8.0),
    )

    assert params.reward_risk == 1.3
    assert params.risk_pct == 0.5
    assert params.allow_live is False
    assert any("回测" in warning for warning in params.warnings)
