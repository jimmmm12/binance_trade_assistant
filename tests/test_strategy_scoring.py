from __future__ import annotations

from dataclasses import replace

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


def test_independent_indicators_can_produce_a_grade_signal():
    signal = replace(
        make_signal(),
        quote_volume_m=500,
        atr_pct=2.0,
        atr_1h_pct=2.0,
        ema20_1h=3.2,
        ema50_1h=3.0,
        ema50_4h=3.1,
        ema200_4h=2.8,
        ema20_1d=3.0,
        ema50_1d=2.7,
        adx_1h=35,
        macd_hist_1h=0.2,
        macd_hist_delta_1h=0.04,
        taker_buy_ratio=0.58,
        obv_slope_pct=12,
        support_distance_atr=0.5,
        resistance_distance_atr=3.5,
        atr_percentile=60,
    )

    scored = score_signal(signal, "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    assert scored.score >= 90
    assert scored.breakdown.grade == "A"
    assert scored.breakdown.position_multiplier == 1.0
    assert scored.breakdown.total == sum(
        [
            scored.breakdown.trend,
            scored.breakdown.momentum,
            scored.breakdown.volume,
            scored.breakdown.positioning,
            scored.breakdown.timeframe,
            scored.breakdown.regime,
        ]
    )


def test_thresholds_are_configurable_without_code_changes():
    scored = score_signal(
        make_signal(),
        "intraday",
        btc_momentum_24h=1,
        eth_momentum_24h=1,
        config={"thresholds": {"grade_a": 80}},
    )

    assert scored.score >= 80
    assert scored.breakdown.grade == "A"
    assert scored.breakdown.position_multiplier == 1.0


def test_configured_add_threshold_is_carried_into_position_decision_data():
    scored = score_signal(
        make_signal(),
        "intraday",
        btc_momentum_24h=1,
        eth_momentum_24h=1,
        config={"thresholds": {"add": 80}},
    )

    assert scored.score >= 80
    assert scored.breakdown.add_allowed is True


def test_default_b_threshold_allows_medium_quality_intraday_setup():
    signal = replace(make_signal(), volume_ratio=1.0, quote_volume_m=60, momentum_24h=1.0)

    scored = score_signal(signal, "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    assert 70 <= scored.score < 75
    assert scored.breakdown.grade == "B"
    assert scored.breakdown.action_level == "small_trade"


def test_low_quality_without_hard_limit_is_observe_not_live_block():
    signal = replace(
        make_signal(),
        rsi_1h=40,
        rsi_4h=40,
        volume_ratio=0.5,
        momentum_24h=0.5,
        momentum_3d=-5,
        quote_volume_m=100,
        funding_pct=0.01,
        atr_pct=2.0,
        atr_1h_pct=2.0,
    )

    scored = score_signal(signal, "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    assert 50 <= scored.score < 70
    assert scored.breakdown.grade == "观察"
    assert scored.breakdown.action_level == "simulate_only"


def test_directional_funding_hard_limit_blocks_live_trading():
    signal = replace(make_signal(), funding_pct=0.12, atr_pct=2.0, atr_1h_pct=2.0)

    scored = score_signal(signal, "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    assert scored.breakdown.action_level == "block_live"
    assert scored.breakdown.position_multiplier == 0.0
    assert any("禁止真仓" in warning for warning in scored.warnings)
