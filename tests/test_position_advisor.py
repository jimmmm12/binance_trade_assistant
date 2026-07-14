from __future__ import annotations

from dataclasses import replace

from tests.test_strategy_scoring import make_signal
from trade_assistant.models import PositionSnapshot
from trade_assistant.position_advisor import advise_position
from trade_assistant.strategy_scoring import score_signal


def make_position(source: str, side: str, pnl: float = 0.0) -> PositionSnapshot:
    return PositionSnapshot(
        source=source,
        market="futures",
        symbol="UNIUSDT",
        side=side,
        quantity=10 if side != "flat" else 0,
        entry_price=3.2,
        mark_price=3.3,
        notional=33,
        unrealized_pnl=pnl,
        realized_pnl=0,
        leverage=2,
        updated_at="2026-07-09T00:00:00",
    )


def test_strong_signal_and_flat_positions_allows_open():
    signal = score_signal(make_signal(), "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    advice = advise_position(signal, make_position("simulated", "flat"), None)

    assert advice.action == "open"
    assert "允许开仓" in advice.summary


def test_b_grade_profitable_position_holds_without_automatic_add():
    signal = score_signal(make_signal(), "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    advice = advise_position(signal, make_position("simulated", "long", pnl=5), None)

    assert signal.breakdown.grade == "B"
    assert advice.action == "hold"


def test_a_grade_profitable_position_allows_add():
    strong = replace(
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
    signal = score_signal(strong, "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    advice = advise_position(signal, make_position("simulated", "long", pnl=5), None)

    assert signal.breakdown.grade == "A"
    assert advice.action == "add"
    assert "加仓" in advice.summary


def test_weak_signal_same_direction_suggests_reduce():
    weak = score_signal(make_signal(), "intraday", btc_momentum_24h=12, eth_momentum_24h=12)

    advice = advise_position(weak, make_position("simulated", "long", pnl=-1), None)

    assert advice.action == "reduce"
    assert "减仓" in advice.summary


def test_opposite_real_position_blocks_action():
    signal = score_signal(make_signal(), "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    advice = advise_position(signal, make_position("simulated", "flat"), make_position("real", "short"))

    assert advice.action == "block"
    assert any("真实仓位反向" in warning for warning in advice.warnings)
