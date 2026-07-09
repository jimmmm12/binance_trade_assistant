from __future__ import annotations

from trade_assistant.gui.services import auto_plan_prices
from trade_assistant.models import Signal


def _signal(side: str = "long", atr_pct: float | None = 2.0) -> Signal:
    return Signal(
        market="futures",
        symbol="UNIUSDT",
        side=side,
        score=8,
        last=10.0,
        change_24h=3.0,
        quote_volume_m=200,
        rsi_1h=61,
        rsi_4h=58,
        volume_ratio=1.6,
        momentum_24h=2.5,
        momentum_3d=5.0,
        funding_pct=0.01,
        note="偏多观察",
        atr_pct=atr_pct,
    )


def test_auto_plan_prices_uses_atr_for_intraday_long_stop_and_target() -> None:
    plan = auto_plan_prices(_signal("long", atr_pct=2.0), "intraday")

    assert plan.entry == 10.0
    assert plan.stop == 9.72
    assert plan.target == 10.504
    assert plan.stop_pct == 2.8
    assert plan.reward_risk == 1.8
    assert "ATR 2.00%" in plan.risk_note
    assert "自适应风险" in plan.risk_note
    assert plan.warning is None
    assert plan.adaptive is not None


def test_auto_plan_prices_places_short_stop_above_entry_and_target_below() -> None:
    plan = auto_plan_prices(_signal("short", atr_pct=2.0), "swing")

    assert plan.stop == 10.36
    assert plan.target == 9.208
    assert plan.stop > plan.entry
    assert plan.target < plan.entry


def test_auto_plan_prices_warns_when_volatility_is_too_high() -> None:
    plan = auto_plan_prices(_signal("long", atr_pct=5.0), "intraday")

    assert plan.stop_pct == 9.0
    assert "波动" in plan.warning
