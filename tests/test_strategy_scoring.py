from __future__ import annotations

from trade_assistant.models import PositionAdvice, PositionSnapshot, ScoreBreakdown, ScoredSignal, Signal
from trade_assistant.strategy_scoring import score_signal


def make_signal() -> Signal:
    return Signal(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        score=0,
        last=3.25,
        change_24h=4.0,
        quote_volume_m=150,
        rsi_1h=61,
        rsi_4h=58,
        volume_ratio=1.8,
        momentum_24h=3.2,
        momentum_3d=6.5,
        funding_pct=0.01,
        note="偏多观察",
    )


def test_scored_signal_keeps_base_signal_and_breakdown():
    breakdown = ScoreBreakdown(
        total=82,
        liquidity=18,
        trend=17,
        volume=18,
        relative_strength=9,
        risk=13,
        funding=7,
        reasons=["1h趋势向上"],
        warnings=[],
    )
    scored = ScoredSignal(signal=make_signal(), mode="intraday", breakdown=breakdown)

    assert scored.symbol == "UNIUSDT"
    assert scored.score == 82
    assert scored.reasons == ["1h趋势向上"]
    assert scored.breakdown.recommendation == "等待确认"


def test_position_models_hold_normalized_state():
    position = PositionSnapshot(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=3.2,
        mark_price=3.3,
        notional=33,
        unrealized_pnl=1,
        realized_pnl=0,
        leverage=2,
        updated_at="2026-07-09T00:00:00",
    )
    advice = PositionAdvice(action="add", summary="可小幅加仓", warnings=[])

    assert position.side == "long"
    assert advice.action == "add"


def test_intraday_rewards_volume_surge_and_liquidity():
    signal = make_signal()
    scored = score_signal(signal, mode="intraday", btc_momentum_24h=1.0, eth_momentum_24h=1.0)

    assert scored.mode == "intraday"
    assert scored.score >= 70
    assert scored.breakdown.volatility >= 0
    assert scored.breakdown.recommendation
    assert any("短期放量" in reason for reason in scored.reasons)


def test_swing_rewards_multiday_trend_more_than_intraday():
    signal = make_signal()
    swing = score_signal(signal, mode="swing", btc_momentum_24h=1.0, eth_momentum_24h=1.0)

    assert swing.mode == "swing"
    assert swing.breakdown.trend >= 15
    assert any("波段" in reason or "4h" in reason for reason in swing.reasons)


def test_funding_warning_penalizes_crowded_long():
    base = make_signal()
    signal = Signal(
        market=base.market,
        symbol=base.symbol,
        side=base.side,
        score=base.score,
        last=base.last,
        change_24h=base.change_24h,
        quote_volume_m=base.quote_volume_m,
        rsi_1h=base.rsi_1h,
        rsi_4h=base.rsi_4h,
        volume_ratio=base.volume_ratio,
        momentum_24h=base.momentum_24h,
        momentum_3d=base.momentum_3d,
        funding_pct=0.12,
        note=base.note,
    )
    scored = score_signal(signal, mode="intraday", btc_momentum_24h=1.0, eth_momentum_24h=1.0)

    assert scored.breakdown.funding < 4
    assert any("资金费率" in warning for warning in scored.warnings)
