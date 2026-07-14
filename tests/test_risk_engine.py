from __future__ import annotations

from trade_assistant.models import PositionSnapshot, Signal
from trade_assistant.risk_engine import (
    daily_loss_guard,
    estimate_liquidation_price,
    evaluate_plan_risk,
    suggest_leverage,
)
from trade_assistant.risk import create_trade_plan


def _signal(**overrides) -> Signal:
    data = {
        "market": "futures",
        "symbol": "UNIUSDT",
        "side": "long",
        "score": 8,
        "last": 10.0,
        "change_24h": 2.0,
        "quote_volume_m": 120,
        "rsi_1h": 61,
        "rsi_4h": 58,
        "volume_ratio": 1.5,
        "momentum_24h": 2.5,
        "momentum_3d": 4.0,
        "funding_pct": 0.01,
        "note": "偏多观察",
        "atr_pct": 2.0,
    }
    data.update(overrides)
    return Signal(**data)


def test_estimate_liquidation_price_keeps_long_below_entry_and_short_above_entry() -> None:
    assert estimate_liquidation_price("long", 10.0, 5.0) == 8.05
    assert estimate_liquidation_price("short", 10.0, 5.0) == 11.95


def test_suggest_leverage_reduces_as_stop_distance_grows() -> None:
    assert suggest_leverage(1.5, "intraday") > suggest_leverage(5.0, "intraday")
    assert suggest_leverage(9.0, "swing") == 1.0


def test_evaluate_plan_risk_blocks_when_liquidation_is_too_close_to_stop() -> None:
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10.0, 9.0, 11.8, 1000, 1, 20)

    risk = evaluate_plan_risk(plan, _signal(), position=None, mode="intraday")

    assert risk.liquidation_status == "不建议下单"
    assert risk.live_allowed is False
    assert "强平安全垫不足" in "；".join(risk.warnings)


def test_evaluate_plan_risk_scores_liquid_safe_plan_and_adds_management_rules() -> None:
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10.0, 9.72, 10.504, 1000, 1, 3)

    risk = evaluate_plan_risk(plan, _signal(), position=None, mode="intraday")

    assert risk.liquidation_status == "安全"
    assert risk.quality_score >= 75
    assert risk.live_allowed is True
    assert risk.management_rules[0] == "首次只建目标仓位 40%，确认后再分批加仓"


def test_evaluate_plan_risk_penalizes_existing_heavy_position() -> None:
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10.0, 9.72, 10.504, 1000, 1, 3)
    position = PositionSnapshot(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=200,
        entry_price=9.8,
        mark_price=10,
        notional=2000,
        unrealized_pnl=40,
        realized_pnl=0,
        leverage=3,
        updated_at="2026-07-09T00:00:00",
    )

    risk = evaluate_plan_risk(plan, _signal(), position=position, mode="intraday")

    assert risk.quality_score < 75
    assert risk.recommended_action in {"只建议模拟", "谨慎小仓"}


def test_evaluate_plan_risk_prefers_real_liquidation_price_from_position() -> None:
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10.0, 9.72, 10.504, 1000, 1, 20)
    position = PositionSnapshot(
        source="real",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=10,
        mark_price=10,
        notional=100,
        unrealized_pnl=0,
        realized_pnl=0,
        leverage=20,
        updated_at="2026-07-09T00:00:00",
        liquidation_price=7.5,
        margin_type="isolated",
    )

    risk = evaluate_plan_risk(plan, _signal(), position=position, mode="intraday")

    assert risk.liquidation_price == 7.5
    assert risk.liquidation_source == "Binance真实强平价"
    assert risk.liquidation_status == "安全"


def test_daily_loss_guard_blocks_at_two_percent_and_warns_at_one_point_five_percent() -> None:
    warning = daily_loss_guard(equity=1000, realized_pnl=-15, unrealized_pnl=0)
    blocked = daily_loss_guard(equity=1000, realized_pnl=-20, unrealized_pnl=0)

    assert warning.status == "警告"
    assert warning.live_allowed is True
    assert blocked.status == "停止交易"
    assert blocked.live_allowed is False


def test_daily_loss_guard_accepts_configurable_thresholds() -> None:
    guard = daily_loss_guard(equity=1000, realized_pnl=-10, stop_pct=1.0, warning_pct=0.75)

    assert guard.status == "停止交易"
