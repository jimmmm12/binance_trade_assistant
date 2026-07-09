from __future__ import annotations

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


def test_same_direction_profitable_position_allows_add():
    signal = score_signal(make_signal(), "intraday", btc_momentum_24h=1, eth_momentum_24h=1)

    advice = advise_position(signal, make_position("simulated", "long", pnl=5), None)

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
