from __future__ import annotations

from trade_assistant.models import PositionSnapshot, ScoreBreakdown, ScoredSignal, Signal
from trade_assistant.position_manager import ManagedPosition, manage_position


def _signal() -> ScoredSignal:
    base = Signal(
        market="futures", symbol="UNIUSDT", side="long", score=84, last=105.5,
        change_24h=2, quote_volume_m=200, rsi_1h=60, rsi_4h=58, volume_ratio=1.3,
        momentum_24h=2, momentum_3d=3, funding_pct=0.01, note="test", atr_pct=2, atr_1h_pct=2,
    )
    return ScoredSignal(
        signal=base,
        mode="intraday",
        breakdown=ScoreBreakdown(
            total=84, liquidity=15, trend=24, volume=12, relative_strength=10, risk=12, funding=7,
            reasons=[], warnings=[], action_level="tradeable", selected_strategy="trend_following",
        ),
    )


def _managed(mark_price: float, status: str = "") -> ManagedPosition:
    return ManagedPosition(
        PositionSnapshot(
            source="simulated", market="futures", symbol="UNIUSDT", side="long", quantity=10,
            entry_price=100, mark_price=mark_price, notional=mark_price * 10, unrealized_pnl=(mark_price - 100) * 10,
            realized_pnl=0, leverage=3, updated_at="2026-07-12T10:00:00",
        ),
        stop_price=95,
        target_price=110,
        status=status,
    )


def _leveraged_loss_managed(*, leverage: float, unrealized_pnl: float, status: str = "") -> ManagedPosition:
    return ManagedPosition(
        PositionSnapshot(
            source="real", market="futures", symbol="BNBUSDT", side="long", quantity=0.1,
            entry_price=100, mark_price=98, notional=90, unrealized_pnl=unrealized_pnl,
            realized_pnl=0, leverage=leverage, updated_at="2026-07-12T19:00:00",
        ),
        stop_price=95,
        target_price=110,
        status=status,
    )


def _aggressive_settings() -> dict:
    return {
        "automation_positioning": {
            "min_add_score": 78,
            "min_profit_r_for_add": 1.1,
            "add_stage_pcts": [0.25, 0.15],
            "profit_take_rules": [
                {"r": 1.75, "reduce_pct": 0.2, "marker": "1.75R减仓"},
                {"r": 3.0, "reduce_pct": 0.25, "marker": "3R减仓"},
            ],
            "trailing_atr_multiplier": 2.5,
        }
    }


def test_position_manager_moves_stop_to_break_even_before_aggressive_add() -> None:
    decision = manage_position(_managed(105.5), same_side_signal=_signal(), settings=_aggressive_settings())

    assert decision.action == "move_stop"
    assert "1R" in decision.message
    assert decision.new_stop is not None and decision.new_stop >= 100


def test_position_manager_scales_out_before_second_aggressive_add_at_profit_target() -> None:
    decision = manage_position(
        _managed(108.75, "INITIAL/1R保本"), same_side_signal=_signal(), settings=_aggressive_settings()
    )

    assert decision.action == "reduce"
    assert decision.quantity == 2.0
    assert "1.75R" in decision.message


def test_position_manager_reduces_when_margin_drawdown_is_too_large() -> None:
    decision = manage_position(
        _leveraged_loss_managed(leverage=20, unrealized_pnl=-1.0),
        settings={
            "automation_positioning": {
                "risk_reduce_pct": 0.5,
                "max_margin_drawdown_reduce_pct": 15.0,
                "max_margin_drawdown_close_pct": 28.0,
            }
        },
    )

    assert decision.action == "reduce"
    assert "保证金回撤" in decision.message


def test_position_manager_closes_when_margin_drawdown_hits_hard_line() -> None:
    decision = manage_position(
        _leveraged_loss_managed(leverage=20, unrealized_pnl=-1.5),
        settings={
            "automation_positioning": {
                "max_margin_drawdown_reduce_pct": 15.0,
                "max_margin_drawdown_close_pct": 28.0,
            }
        },
    )

    assert decision.action == "close"
    assert "硬退出线" in decision.message


def test_position_manager_reduces_when_actual_leverage_exceeds_policy_limit() -> None:
    decision = manage_position(
        _leveraged_loss_managed(leverage=20, unrealized_pnl=-0.1),
        settings={
            "automation_positioning": {
                "risk_reduce_pct": 0.5,
                "max_position_leverage": 8.0,
            }
        },
    )

    assert decision.action == "reduce"
    assert "实际杠杆 20.0x" in decision.message
