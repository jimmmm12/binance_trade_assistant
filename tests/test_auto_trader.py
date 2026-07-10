from __future__ import annotations

from trade_assistant.auto_trader import AUTO_EXECUTION_LIVE, AutoTradeConfig, run_auto_cycle, select_candidate
from trade_assistant.automation_state import AutoTradeState
from trade_assistant.models import ScoreBreakdown, ScoredSignal, Signal
from trade_assistant.portfolio import SimulatedPortfolio


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
            volatility=10,
            position=10,
            recommendation="可交易，≤3x",
            action_level="tradeable",
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
    assert decision.state == AutoTradeState.MANAGING
    assert "已开仓" in decision.state_path
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


def test_auto_cycle_does_not_repeat_order_when_position_exists(tmp_path) -> None:
    db_path = tmp_path / "auto.db"
    SimulatedPortfolio(db_path).apply_fill("futures", "UNIUSDT", "BUY", 1, 10)
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=db_path,
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([_scored("UNIUSDT", 86)], []))

    assert decision.action == "manage_position"
    assert decision.position is not None
    assert decision.position.quantity == 1


def test_auto_cycle_blocks_low_score_live_plan_when_not_simulating(tmp_path) -> None:
    low = _scored("RISKUSDT", 45)
    low = ScoredSignal(
        signal=low.signal,
        mode=low.mode,
        breakdown=ScoreBreakdown(
            total=45,
            liquidity=8,
            trend=6,
            volume=6,
            relative_strength=4,
            risk=5,
            funding=3,
            reasons=[],
            warnings=["ATR波动过大，禁止真仓自动下单"],
            volatility=1,
            position=10,
            recommendation="禁止真仓，仅观察",
            action_level="block_live",
        ),
    )
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([low], []))

    assert decision.action == "blocked"
    assert decision.state == AutoTradeState.BLOCKED
    assert "只观察" in decision.message


def test_auto_cycle_live_mode_requires_confirmation(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="WRONG",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda: (True, "ready"),
        live_order_fn=lambda plan, side: {"ok": True},
    )

    assert decision.action == "blocked"
    assert "确认文字" in decision.message


def test_auto_cycle_live_mode_sends_order_when_all_guards_pass(tmp_path) -> None:
    sent: list[tuple[str, str]] = []
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="LIVE_TRADING_CONFIRMED",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda: (True, "ready"),
        live_order_fn=lambda plan, side: sent.append((plan.symbol, side)) or {"orderId": 1},
    )

    assert decision.action == "live_order_sent"
    assert sent == [("UNIUSDT", "BUY")]
