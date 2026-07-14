from __future__ import annotations

from dataclasses import replace

from trade_assistant.auto_trader import (
    AUTO_EXECUTION_LIVE,
    AutoTradeConfig,
    PendingOrderDecision,
    run_auto_cycle,
    select_candidate,
    _apply_aggressive_margin_target,
    _cap_plan_to_margin_budget,
    _entry_economics_allowed,
    _is_global_execution_block,
    _live_entry_quality_allowed,
)
from trade_assistant.automation_state import AutoTradeState
from trade_assistant.models import ScoreBreakdown, ScoredSignal, Signal
from trade_assistant.opportunity_selector import assess_opportunity, rank_candidates
from trade_assistant.portfolio import SimulatedPortfolio
from trade_assistant.risk import create_trade_plan
from trade_assistant.risk_engine import evaluate_plan_risk


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
            selected_strategy="trend_following",
        ),
    )


def test_select_candidate_picks_highest_live_allowed_signal() -> None:
    candidate = select_candidate([_scored("LOWUSDT", 72), _scored("HIGHUSDT", 86)], [])

    assert candidate is not None
    assert candidate.symbol == "HIGHUSDT"


def test_select_candidate_skips_higher_scoring_hard_block() -> None:
    blocked = _scored("BLOCKEDUSDT", 95)
    blocked = replace(blocked, breakdown=replace(blocked.breakdown, action_level="block_live"))
    allowed = _scored("ALLOWEDUSDT", 82)

    candidate = select_candidate([blocked, allowed], [])

    assert candidate is not None
    assert candidate.symbol == "ALLOWEDUSDT"


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


def test_auto_cycle_rotates_past_blocked_top_candidate(tmp_path) -> None:
    blocked = _scored("BLOCKEDUSDT", 95)
    blocked = replace(blocked, breakdown=replace(blocked.breakdown, action_level="block_live"))
    allowed = _scored("ALLOWEDUSDT", 82)
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        portfolio_path=tmp_path / "auto.db",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([blocked, allowed], []))

    assert decision.action == "planned"
    assert decision.signal is not None
    assert decision.signal.symbol == "ALLOWEDUSDT"


def test_auto_cycle_rotates_past_symbol_in_entry_cooldown(tmp_path) -> None:
    first = _scored("FIRSTUSDT", 90)
    second = _scored("SECONDUSDT", 86)
    config = AutoTradeConfig(market="futures", mode="intraday", top=5, auto_simulate=True, portfolio_path=tmp_path / "auto.db")

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([first, second], []),
        entry_gate_fn=lambda signal: (signal.symbol != "FIRSTUSDT", "FIRSTUSDT 自动开仓冷却中"),
    )

    assert decision.action == "simulated_order"
    assert decision.signal is not None
    assert decision.signal.symbol == "SECONDUSDT"


def test_auto_cycle_skips_correlated_second_candidate_and_opens_next_independent_symbol(tmp_path) -> None:
    same_returns = tuple(float(index) for index in range(48))
    independent_returns = tuple(1.0 if index % 2 else -1.0 for index in range(48))
    first = replace(_scored("FIRSTUSDT", 90), signal=replace(_scored("FIRSTUSDT", 90).signal, returns_1h=same_returns))
    correlated = replace(_scored("SECONDUSDT", 88), signal=replace(_scored("SECONDUSDT", 88).signal, returns_1h=same_returns))
    independent = replace(_scored("THIRDUSDT", 86), signal=replace(_scored("THIRDUSDT", 86).signal, returns_1h=independent_returns))
    config = AutoTradeConfig(
        market="futures", mode="intraday", top=5, auto_simulate=True, portfolio_path=tmp_path / "auto.db"
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([first, correlated, independent], []))

    assert decision.action == "multi_position_opened"
    assert SimulatedPortfolio(config.portfolio_path).get_position("futures", "FIRSTUSDT").quantity > 0
    assert SimulatedPortfolio(config.portfolio_path).get_position("futures", "SECONDUSDT").quantity == 0
    assert SimulatedPortfolio(config.portfolio_path).get_position("futures", "THIRDUSDT").quantity > 0


def test_auto_cycle_limits_new_risk_cluster_against_an_existing_position(tmp_path) -> None:
    same_returns = tuple(float(index) for index in range(48))
    independent_returns = tuple(1.0 if index % 2 else -1.0 for index in range(48))
    db_path = tmp_path / "auto.db"
    portfolio = SimulatedPortfolio(db_path)
    portfolio.apply_fill("futures", "HELDUSDT", "BUY", 1, 10)
    portfolio.upsert_position_record(
        source="simulated", market="futures", symbol="HELDUSDT", side="long", quantity=1,
        entry_price=10, mark_price=10, stop_price=9, target_price=12, status="INITIAL/初始试探仓",
    )
    held = replace(_scored("HELDUSDT", 90), signal=replace(_scored("HELDUSDT", 90).signal, returns_1h=same_returns))
    correlated = replace(_scored("CORRELATEDUSDT", 88), signal=replace(_scored("CORRELATEDUSDT", 88).signal, returns_1h=same_returns))
    independent = replace(_scored("INDEPENDENTUSDT", 86), signal=replace(_scored("INDEPENDENTUSDT", 86).signal, returns_1h=independent_returns))
    config = AutoTradeConfig(market="futures", mode="intraday", top=5, auto_simulate=True, portfolio_path=db_path)

    decision = run_auto_cycle(config, scan_fn=lambda: ([held, correlated, independent], []))

    assert decision.signal is not None
    assert decision.signal.symbol == "INDEPENDENTUSDT"
    assert portfolio.get_position("futures", "CORRELATEDUSDT").quantity == 0
    assert portfolio.get_position("futures", "INDEPENDENTUSDT").quantity > 0


def test_entry_economics_rejects_signal_that_cannot_cover_costs() -> None:
    thin = create_trade_plan("UNIUSDT", "futures", "long", 10, 9.9, 10.05, 1000, 1, 2)
    strong = create_trade_plan("UNIUSDT", "futures", "long", 10, 9.9, 10.8, 1000, 1, 2)

    allowed, message = _entry_economics_allowed(thin, {})
    strong_allowed, _ = _entry_economics_allowed(strong, {})

    assert allowed is False
    assert "手续费/滑点" in message
    assert strong_allowed is True


def test_candidate_risk_rejection_is_not_treated_as_global_automation_failure() -> None:
    assert _is_global_execution_block("下单后总风险敞口超过账户限制") is False
    assert _is_global_execution_block("账户级单笔风险 2.50% 超过限制 2.50%") is False
    assert _is_global_execution_block("账户状态未与 Binance 同步") is True


def test_live_entry_quality_rejects_structural_conflict_despite_passing_score() -> None:
    conflicting = _scored("AAVEUSDT", 86)
    conflicting = replace(
        conflicting,
        breakdown=replace(conflicting.breakdown, warnings=["主动成交方向与信号背离"]),
    )

    allowed, message = _live_entry_quality_allowed(conflicting, {})

    assert allowed is False
    assert "质量过滤" in message


def test_live_entry_quality_requires_score_volume_and_trend_strategy() -> None:
    quality = _scored("UNIUSDT", 86)

    allowed, _ = _live_entry_quality_allowed(quality, {})
    low_score, score_message = _live_entry_quality_allowed(_scored("LOWUSDT", 71), {})
    low_volume = replace(quality, signal=replace(quality.signal, volume_ratio=1.1))
    volume_allowed, volume_message = _live_entry_quality_allowed(low_volume, {})

    assert allowed is True
    assert low_score is False
    assert "评分" in score_message
    assert volume_allowed is False
    assert "量能" in volume_message


def test_aggressive_line_treats_benchmark_divergence_as_size_signal_not_hard_block() -> None:
    divergent = _scored("ALTUSDT", 70)
    divergent = replace(
        divergent,
        breakdown=replace(divergent.breakdown, warnings=["BTC/ETH 大盘环境相反"]),
        signal=replace(divergent.signal, volume_ratio=0.75),
    )

    allowed, message = _live_entry_quality_allowed(
        divergent,
        {
            "auto_execution": {
                "risk_line": "aggressive",
                "live_min_score": 66,
                "live_min_volume_ratio": 0.7,
                "live_block_warning_markers": ["多周期方向冲突", "主动成交方向与信号背离"],
            }
        },
    )

    assert allowed is True
    assert message == "真仓质量过滤通过"


def test_benchmark_divergence_is_not_a_hard_block_for_default_live_filter() -> None:
    divergent = _scored("ALTUSDT", 86)
    divergent = replace(
        divergent,
        breakdown=replace(divergent.breakdown, warnings=["BTC/ETH 大盘环境相反"]),
    )

    allowed, message = _live_entry_quality_allowed(
        divergent,
        {
            "auto_execution": {
                "live_min_score": 72,
                "live_min_volume_ratio": 1.2,
                "live_allowed_strategies": ["trend_following", "breakout"],
                "live_block_warning_markers": ["多周期方向冲突", "主动成交方向与信号背离"],
            }
        },
    )

    assert allowed is True
    assert message == "真仓质量过滤通过"


def test_opportunity_selector_prefers_liquid_relative_strength_over_raw_score() -> None:
    raw_high = replace(
        _scored("RAWUSDT", 88),
        signal=replace(_scored("RAWUSDT", 88).signal, quote_volume_m=20, volume_ratio=0.8, momentum_24h=-1.0),
    )
    stronger = replace(
        _scored("STRONGUSDT", 82),
        signal=replace(_scored("STRONGUSDT", 82).signal, quote_volume_m=500, volume_ratio=1.8, momentum_24h=3.0),
    )

    ranked = rank_candidates([raw_high, stronger], {}, execution_mode="live")

    assert ranked[0].symbol == "STRONGUSDT"


def test_opportunity_selector_blocks_structural_conflict_unless_quality_is_exceptional() -> None:
    conflicted = replace(
        _scored("CONFLICTUSDT", 74),
        breakdown=replace(_scored("CONFLICTUSDT", 74).breakdown, warnings=["多周期方向冲突，逆势风险较高"]),
    )

    assessment = assess_opportunity(conflicted, {}, execution_mode="live")

    assert assessment.allowed is False
    assert any("结构冲突" in warning for warning in assessment.warnings)


def test_auto_cycle_rotates_after_research_filter_rejects_top_candidate(tmp_path) -> None:
    conflicted = replace(
        _scored("CONFLICTUSDT", 90),
        breakdown=replace(_scored("CONFLICTUSDT", 90).breakdown, warnings=["多周期方向冲突，逆势风险较高"]),
    )
    clean = _scored("CLEANUSDT", 84)
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
    )
    sent: list[str] = []

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([conflicted, clean], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: sent.append(signal.symbol) or {"status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert sent == ["CLEANUSDT"]


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

    assert decision.action == "simulated_add"
    assert decision.position is not None
    assert decision.position.quantity > 1


def test_auto_cycle_holds_existing_position_without_rebuying_when_signal_is_not_add(tmp_path) -> None:
    db_path = tmp_path / "auto.db"
    portfolio = SimulatedPortfolio(db_path)
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", 1, 10)
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=1,
        entry_price=10,
        mark_price=10.1,
        stop_price=9,
        target_price=12,
        status="模拟持仓",
    )
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=db_path,
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([_scored("UNIUSDT", 72)], []))

    assert decision.action == "no_signal"
    assert portfolio.get_position("futures", "UNIUSDT").quantity == 1


def test_auto_cycle_reduces_weak_existing_position_without_crashing_on_tiny_quantity(tmp_path) -> None:
    db_path = tmp_path / "auto.db"
    portfolio = SimulatedPortfolio(db_path)
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", 1, 10)
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=1,
        entry_price=10,
        mark_price=9.8,
        stop_price=9,
        target_price=12,
        status="模拟持仓",
    )
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=db_path,
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([_scored("UNIUSDT", 52)], []))

    assert decision.action == "position_management"
    assert "减仓数量过小" in decision.message
    assert portfolio.get_position("futures", "UNIUSDT").quantity == 1


def test_auto_cycle_reduces_atr_warning_existing_position_without_crashing_on_tiny_quantity(tmp_path) -> None:
    db_path = tmp_path / "auto.db"
    portfolio = SimulatedPortfolio(db_path)
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", 1, 10)
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=1,
        entry_price=10,
        mark_price=10.2,
        stop_price=9,
        target_price=12,
        status="模拟持仓",
    )
    warned = _scored("UNIUSDT", 78)
    warned = replace(warned, breakdown=replace(warned.breakdown, warnings=["ATR波动过大"]))
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=db_path,
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([warned], []))

    assert decision.action == "position_management"
    assert "减仓数量过小" in decision.message
    assert portfolio.get_position("futures", "UNIUSDT").quantity == 1


def test_auto_cycle_opens_new_opportunity_when_existing_position_only_needs_hold(tmp_path) -> None:
    db_path = tmp_path / "auto.db"
    portfolio = SimulatedPortfolio(db_path)
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", 1, 10)
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=1,
        entry_price=10,
        mark_price=10.1,
        stop_price=9,
        target_price=12,
        status="模拟持仓",
    )
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=db_path,
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([_scored("UNIUSDT", 72), _scored("AAVEUSDT", 76)], []))

    assert decision.action == "simulated_order"
    assert decision.signal is not None
    assert decision.signal.symbol == "AAVEUSDT"
    assert portfolio.get_position("futures", "AAVEUSDT").quantity > 0


def test_auto_cycle_plan_mode_respects_manual_equity_even_when_auto_detect_is_checked(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        auto_detect_account=True,
        equity=30.0,
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        account_equity_fn=lambda: 2500.0,
    )

    assert decision.plan is not None
    assert decision.plan.equity == 24.0
    assert decision.review is not None
    assert decision.review.risk_bucket == "低风险"
    assert decision.review.allocation_pct == 80.0
    assert round(decision.plan.risk_pct, 4) == 0.28
    assert decision.plan.margin_required < 30.0


def test_auto_cycle_simulation_mode_respects_30u_manual_equity_with_auto_detect_checked(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        auto_detect_account=True,
        equity=30.0,
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86, side="short")], []),
        account_equity_fn=lambda: 2500.0,
    )

    assert decision.plan is not None
    assert decision.plan.equity == 24.0
    assert decision.plan.margin_required < 30.0
    if decision.position is not None:
        assert decision.position.quantity * decision.position.entry_price / max(1.0, decision.plan.leverage) < 30.0


def test_auto_cycle_uses_full_target_risk_only_for_a_score_then_first_stage(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        auto_detect_account=True,
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 92)], []),
        account_equity_fn=lambda: 2500.0,
    )

    assert decision.plan is not None
    assert decision.plan.equity == 800.0
    assert round(decision.plan.risk_pct, 4) == 0.4


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

    assert decision.action == "no_executable_signal"
    assert decision.state == AutoTradeState.WAITING_CONFIRMATION
    assert "未通过" in decision.message


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
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: {"ok": True},
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
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: sent.append((plan.symbol, side))
        or {"orderId": 1, "status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert sent == [("UNIUSDT", "BUY")]


def test_auto_cycle_live_mode_uses_small_risk_for_adaptive_simulate_only_warning(tmp_path) -> None:
    live_orders: list[str] = []
    simulate_only = _scored("UNIUSDT", 86)
    simulate_only = replace(
        simulate_only,
        signal=replace(simulate_only.signal, funding_pct=0.09),
    )
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([simulate_only], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: live_orders.append(plan.symbol)
        or {"orderId": 1, "status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert live_orders == ["UNIUSDT"]
    assert decision.plan is not None
    assert decision.plan.risk_pct < 0.4


def test_auto_cycle_live_mode_boosts_small_account_order_to_min_notional(tmp_path) -> None:
    sent = []
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        auto_detect_account=True,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 92)], []),
        account_equity_fn=lambda: 18.0,
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: sent.append((plan, side, review))
        or {"orderId": 1, "status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert decision.plan is not None
    assert round(decision.plan.notional, 2) == 5.20
    assert decision.plan.leverage <= 2.0
    assert decision.review is not None
    assert decision.review.recommended_action == "微仓真仓"
    assert sent[0][0].notional == decision.plan.notional


def test_auto_cycle_aggressive_line_uses_full_equity_without_stage_discount(tmp_path) -> None:
    sent = []
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        auto_detect_account=True,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        risk_line="aggressive",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        account_equity_fn=lambda: 18.0,
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: sent.append((plan, review))
        or {"orderId": 1, "status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert decision.plan is not None
    assert decision.plan.risk_pct >= 3.0
    assert decision.plan.notional > 20.0
    assert 5.0 <= decision.plan.leverage <= 8.0
    assert decision.plan.margin_required >= 3.0
    assert decision.review is not None
    assert decision.review.allocation_pct == 100.0
    assert decision.review.allocation_equity == 18.0
    assert sent[0][0].notional == decision.plan.notional


def test_aggressive_margin_target_caps_tight_stop_position_to_equity_budget() -> None:
    signal = _scored("ADAUSDT", 86, side="short")
    plan = create_trade_plan(
        "ADAUSDT",
        "futures",
        "short",
        entry=0.1573,
        stop=0.15735,
        target=0.155,
        equity=1000,
        risk_pct=5,
        leverage=6,
    )
    review = evaluate_plan_risk(plan, signal.signal, None, "intraday", min_live_score=66)
    review = replace(review, total_equity=1000, allocation_equity=1000, allocation_pct=100)
    settings = {
        "min_live_score": 66,
        "aggressive_line": {
            "margin_tiers": [{"min_score": 80, "margin_pct": 42.0}],
            "max_single_risk_pct": 5.0,
            "max_symbol_exposure_pct": 300.0,
        },
    }

    adjusted, adjusted_review = _apply_aggressive_margin_target(
        plan,
        review,
        signal,
        None,
        settings,
        "intraday",
    )

    assert plan.margin_required > 20_000
    assert adjusted.margin_required <= 420.0 + 1e-6
    assert adjusted.quantity < plan.quantity
    assert "压低" in "；".join(adjusted_review.reasons)


def test_auto_margin_budget_caps_conservative_tight_stop_position() -> None:
    signal = _scored("ADAUSDT", 86, side="short")
    plan = create_trade_plan(
        "ADAUSDT",
        "futures",
        "short",
        entry=0.1573,
        stop=0.15735,
        target=0.155,
        equity=1000,
        risk_pct=1,
        leverage=5,
    )
    review = evaluate_plan_risk(plan, signal.signal, None, "intraday", min_live_score=66)
    review = replace(review, total_equity=1000, allocation_equity=800, allocation_pct=80)
    settings = {
        "min_live_score": 66,
        "automation_positioning": {"max_initial_margin_pct": 20.0},
    }

    adjusted, adjusted_review = _cap_plan_to_margin_budget(
        plan,
        review,
        signal,
        None,
        settings,
        "intraday",
    )

    assert plan.margin_required > 6_000
    assert adjusted.margin_required <= 200.0 + 1e-6
    assert adjusted.quantity < plan.quantity
    assert "本金预算封顶" in "；".join(adjusted_review.reasons)


def test_auto_cycle_aggressive_line_allows_clean_66_score_with_discounted_size(tmp_path) -> None:
    sent: list[str] = []
    candidate = _scored("UNIUSDT", 66)
    candidate = replace(candidate, signal=replace(candidate.signal, volume_ratio=0.75))
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        auto_detect_account=True,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        risk_line="aggressive",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([candidate], []),
        account_equity_fn=lambda: 18.0,
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: sent.append(signal.symbol)
        or {"orderId": 1, "status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert sent == ["UNIUSDT"]
    assert decision.plan is not None
    assert 1.0 <= decision.plan.risk_pct <= 5.0


def test_aggressive_recovery_mode_blocks_ordinary_candidate_after_five_losses(tmp_path) -> None:
    sent: list[str] = []
    candidate = _scored("UNIUSDT", 74)
    config = AutoTradeConfig(
        market="futures", mode="intraday", top=5, auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE, live_confirm="ABC", risk_line="aggressive",
        portfolio_path=tmp_path / "auto.db",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([candidate], []),
        consecutive_losses_fn=lambda: 5,
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: sent.append(signal.symbol) or {"status": "FILLED"},
    )

    assert decision.action == "no_executable_signal"
    assert "恢复模式" in decision.message
    assert sent == []


def test_aggressive_recovery_mode_allows_high_quality_probe_with_reduced_risk(tmp_path) -> None:
    sent: list = []
    candidate = _scored("UNIUSDT", 84)
    candidate = replace(candidate, signal=replace(candidate.signal, volume_ratio=1.2))
    config = AutoTradeConfig(
        market="futures", mode="intraday", top=5, auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE, live_confirm="ABC", risk_line="aggressive",
        portfolio_path=tmp_path / "auto.db",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([candidate], []),
        consecutive_losses_fn=lambda: 5,
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: sent.append(plan) or {"status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert decision.plan is not None
    assert decision.plan.risk_pct < 1.5
    assert len(sent) == 1


def test_auto_cycle_live_mode_tries_next_candidate_after_order_error(tmp_path) -> None:
    sent: list[str] = []
    bad = _scored("BADUSDT", 90)
    good = _scored("GOODUSDT", 88)
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    def fake_live_order(plan, side, review, signal):
        sent.append(plan.symbol)
        if plan.symbol == "BADUSDT":
            raise RuntimeError("Precision is over the maximum defined for this asset.")
        return {"orderId": 2, "status": "FILLED"}

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([bad, good], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=fake_live_order,
    )

    assert decision.action == "live_order_sent"
    assert decision.signal is not None
    assert decision.signal.symbol == "GOODUSDT"
    assert sent == ["BADUSDT", "GOODUSDT"]


def test_auto_cycle_live_mode_stops_on_global_emergency_order_error(tmp_path) -> None:
    sent: list[str] = []
    first = _scored("BADUSDT", 90)
    second = _scored("GOODUSDT", 88)
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    def fake_live_order(plan, side, review, signal):
        sent.append(plan.symbol)
        raise RuntimeError("系统急停已启用")

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([first, second], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=fake_live_order,
    )

    assert decision.action == "blocked"
    assert "自动真仓已停止" in decision.message
    assert sent == ["BADUSDT"]


def test_auto_cycle_keeps_plan_when_risk_pool_quantity_is_below_exchange_minimum(tmp_path) -> None:
    simulate_only = _scored("UNIUSDT", 86)
    simulate_only = replace(simulate_only, signal=replace(simulate_only.signal, atr_1h_pct=7.0, atr_pct=7.0))
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([simulate_only], []))

    assert decision.action == "planned"
    assert "下单数量过小" in decision.message
    assert decision.plan is not None
    assert decision.plan.equity == 200


def test_auto_cycle_simulation_mode_is_not_blocked_by_daily_loss_guard(tmp_path) -> None:
    db_path = tmp_path / "auto.db"
    portfolio = SimulatedPortfolio(db_path)
    portfolio.apply_fill("futures", "LOSSUSDT", "SELL", quantity=10, price=3.0)
    portfolio.apply_fill("futures", "LOSSUSDT", "BUY", quantity=10, price=3.5)
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=True,
        equity=1000.0,
        max_daily_loss_pct=0.1,
        portfolio_path=db_path,
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(config, scan_fn=lambda: ([_scored("UNIUSDT", 86)], []))

    assert decision.action != "blocked"
    assert "模拟风险提醒" in decision.message
    assert "模拟继续运行" in decision.message


def test_auto_cycle_live_mode_is_blocked_by_daily_loss_guard(tmp_path) -> None:
    db_path = tmp_path / "auto.db"
    portfolio = SimulatedPortfolio(db_path)
    portfolio.apply_fill("futures", "LOSSUSDT", "SELL", quantity=10, price=3.0)
    portfolio.apply_fill("futures", "LOSSUSDT", "BUY", quantity=10, price=3.5)
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        equity=1000.0,
        max_daily_loss_pct=0.1,
        portfolio_path=db_path,
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: {"status": "FILLED"},
    )

    assert decision.action == "blocked"
    assert "真下单已锁定" in decision.message


def test_auto_cycle_waits_for_binance_fill_before_marking_position_open(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: {"orderId": 1, "status": "NEW"},
    )

    assert decision.action == "live_order_pending"
    assert decision.state == AutoTradeState.WAITING_CONFIRMATION
    assert "等待 Binance 成交回报" in decision.message


def test_auto_cycle_waits_when_existing_entry_order_is_still_valid(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )
    live_orders = []

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        pending_orders_fn=lambda candidates: PendingOrderDecision("wait", "UNIUSDT 挂单仍有效，继续等待成交", "UNIUSDT"),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: live_orders.append(plan.symbol)
        or {"orderId": 1, "status": "FILLED"},
    )

    assert decision.action == "live_order_pending"
    assert "挂单仍有效" in decision.message
    assert live_orders == []


def test_auto_cycle_manages_pending_order_but_opens_next_symbol(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
    )
    live_orders: list[str] = []

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 90), _scored("LINKUSDT", 86)], []),
        pending_orders_fn=lambda candidates: PendingOrderDecision(
            "monitoring",
            "UNIUSDT 挂单仍有效，后台继续监控",
            "UNIUSDT",
            ("UNIUSDT",),
        ),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: live_orders.append(plan.symbol)
        or {"orderId": 2, "status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert decision.signal is not None
    assert decision.signal.symbol == "LINKUSDT"
    assert live_orders == ["LINKUSDT"]


def test_auto_cycle_continues_after_canceling_stale_entry_order(tmp_path) -> None:
    config = AutoTradeConfig(
        market="futures",
        mode="intraday",
        top=5,
        auto_simulate=False,
        execution_mode=AUTO_EXECUTION_LIVE,
        live_confirm="ABC",
        portfolio_path=tmp_path / "auto.db",
        automation_log_path=tmp_path / "events.jsonl",
    )
    live_orders = []

    decision = run_auto_cycle(
        config,
        scan_fn=lambda: ([_scored("UNIUSDT", 86)], []),
        pending_orders_fn=lambda candidates: PendingOrderDecision("canceled", "UNIUSDT 挂单已撤销：等待成交超过 45 秒", "UNIUSDT"),
        market_fresh_fn=lambda signal: (True, "fresh"),
        live_status_fn=lambda signal: (True, "ready"),
        live_order_fn=lambda plan, side, review, signal: live_orders.append(plan.symbol)
        or {"orderId": 2, "status": "FILLED"},
    )

    assert decision.action == "live_order_sent"
    assert "挂单已撤销" in decision.message
    assert live_orders == ["UNIUSDT"]
