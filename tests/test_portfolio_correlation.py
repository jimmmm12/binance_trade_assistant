from __future__ import annotations

from dataclasses import replace

from trade_assistant.models import ScoreBreakdown, ScoredSignal, Signal
from trade_assistant.portfolio_correlation import correlation_gate, signed_return_correlation


def _signal(symbol: str, side: str, returns: tuple[float, ...]) -> ScoredSignal:
    signal = Signal(
        market="futures",
        symbol=symbol,
        side=side,
        score=80,
        last=10.0,
        change_24h=1.0,
        quote_volume_m=100.0,
        rsi_1h=58.0,
        rsi_4h=56.0,
        volume_ratio=1.5,
        momentum_24h=2.0,
        momentum_3d=4.0,
        funding_pct=0.01,
        note="test",
        returns_1h=returns,
    )
    return ScoredSignal(
        signal=signal,
        mode="intraday",
        breakdown=ScoreBreakdown(80, 15, 18, 12, 10, 12, 6, [], []),
    )


def test_same_direction_highly_correlated_signals_are_blocked() -> None:
    returns = tuple(float(index) for index in range(48))
    allowed, message = correlation_gate(_signal("ETHUSDT", "long", returns), [_signal("SOLUSDT", "long", returns)])

    assert allowed is False
    assert "相关性风险簇已满" in message


def test_opposite_direction_same_market_returns_offset_portfolio_risk() -> None:
    returns = tuple(float(index) for index in range(48))
    candidate = _signal("ETHUSDT", "short", returns)
    reference = _signal("SOLUSDT", "long", returns)

    assert signed_return_correlation(candidate, reference) == -1.0
    assert correlation_gate(candidate, [reference])[0] is True


def test_insufficient_return_history_never_blocks_a_candidate() -> None:
    candidate = _signal("ETHUSDT", "long", (0.1, 0.2, 0.3))
    reference = _signal("SOLUSDT", "long", (0.1, 0.2, 0.3))

    assert correlation_gate(candidate, [reference])[0] is True
