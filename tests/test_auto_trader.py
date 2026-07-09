from __future__ import annotations

from trade_assistant.auto_trader import AutoTradeConfig, run_auto_cycle, select_candidate
from trade_assistant.models import ScoreBreakdown, ScoredSignal, Signal


def _scored(symbol: str, score: int, side: str = "long") -> ScoredSignal:
    signal = Signal(
        market="futures",
        symbol=symbol,
        side=side,
        score=score,
        last=10.0,
        change_24h=2.0,
        quote_volume_m=180,
        rsi_1h=60,
        rsi_4h=58,
        volume_ratio=1.5,
        momentum_24h=2.5,
        momentum_3d=4.0,
        funding_pct=0.01,
        note="偏多观察",
        atr_pct=2.0,
        atr_1h_pct=2.0,
    )
    return ScoredSignal(
        signal=signal,
        mode="intraday",
        breakdown=ScoreBreakdown(
            total=score,
            liquidity=18,
            trend=18,
            volume=15,
            relative_strength=10,
            risk=12,
            funding=7,
            reasons=["流动性充足"],
            warnings=[],
        ),
    )


def test_select_candidate_picks_highest_live_allowed_signal() -> None:
    candidate = select_candidate([_scored("LOWUSDT", 72), _scored("HIGHUSDT", 86)], [])

    assert candidate is not None
    assert candidate.symbol == "HIGHUSDT"


def test_auto_cycle_generates_plan_and_simulates_order_when_enabled(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=tmp_path / "auto.db",
    )

    def fake_scan():
        return [_scored("UNIUSDT", 86)], []

    decision = run_auto_cycle(config, scan_fn=fake_scan)

    assert decision.action == "simulated_order"
    assert decision.plan is not None
    assert decision.review is not None
    assert decision.position is not None
    assert decision.signal.symbol == "UNIUSDT"


def test_auto_cycle_only_plans_when_simulation_disabled(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        portfolio_path=tmp_path / "auto.db",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([_scored("UNIUSDT", 86)], []))

    assert decision.action == "planned"
    assert decision.position is None


def test_auto_cycle_can_simulate_short_futures_signal(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=tmp_path / "auto.db",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([], [_scored("UNIUSDT", 86, side="short")]))

    assert decision.action == "simulated_order"
    assert decision.position is not None
    assert decision.position.side == "short"
