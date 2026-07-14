from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from .automation_policy import automation_sizing_config, exposure_allowed, initial_sizing_decision
from .automation_log import append_automation_event, build_automation_event
from .automation_state import AutoTradeState, AutoTradeStateMachine
from .gui.services import auto_plan_prices, evaluate_plan_from_form, simulate_order_from_form
from .main import load_settings
from .models import PositionSnapshot, ScoredSignal, TradePlan
from .opportunity_selector import assess_opportunity, rank_candidates
from .portfolio import SimulatedPortfolio
from .portfolio_correlation import correlation_gate
from .position_manager import ManagedPosition, PositionManagementDecision, manage_position
from .risk_engine import PlanRiskReview, daily_loss_guard, evaluate_plan_risk


AUTO_EXECUTION_PLAN = "plan"
AUTO_EXECUTION_SIMULATE = "simulate"
AUTO_EXECUTION_LIVE = "live"
AUTO_EXECUTION_MODES = {AUTO_EXECUTION_PLAN, AUTO_EXECUTION_SIMULATE, AUTO_EXECUTION_LIVE}
DEFAULT_MICRO_MIN_NOTIONAL = 5.2


@dataclass(frozen=True)
class AutoTradeConfig:
    market: str
    mode: str
    top: int
    auto_simulate: bool
    equity: float = 1000.0
    portfolio_path: Path | None = None
    automation_log_path: Path | None = None
    max_daily_loss_pct: float = 2.0
    execution_mode: str | None = None
    live_confirm: str = ""
    auto_detect_account: bool = True
    risk_line: str = "conservative"


@dataclass(frozen=True)
class AutoTradeDecision:
    action: str
    message: str
    signal: ScoredSignal | None
    plan: TradePlan | None
    review: PlanRiskReview | None = None
    position: PositionSnapshot | None = None
    state: AutoTradeState = AutoTradeState.EMPTY_OBSERVING
    state_path: str = AutoTradeState.EMPTY_OBSERVING.value


@dataclass(frozen=True)
class PendingOrderDecision:
    action: str
    message: str
    symbol: str = ""
    symbols: tuple[str, ...] = ()


def select_candidate(longs: list[ScoredSignal], shorts: list[ScoredSignal]) -> ScoredSignal | None:
    candidates = [item for item in [*longs, *shorts] if item.breakdown.action_level not in {"block_live", "avoid"}]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.score, item.quote_volume_m), reverse=True)[0]


def run_auto_cycle(
    config: AutoTradeConfig,
    *,
    scan_fn: Callable[[], tuple[list[ScoredSignal], list[ScoredSignal]]],
    market_fresh_fn: Callable[[ScoredSignal], tuple[bool, str]] | None = None,
    live_status_fn: Callable[[ScoredSignal], tuple[bool, str]] | None = None,
    live_order_fn: Callable[[TradePlan, str, PlanRiskReview, ScoredSignal], dict] | None = None,
    position_order_fn: Callable[[ManagedPosition, PositionManagementDecision], dict] | None = None,
    managed_positions_fn: Callable[[], list[ManagedPosition]] | None = None,
    account_equity_fn: Callable[[], float] | None = None,
    real_position_fn: Callable[[ScoredSignal], PositionSnapshot | None] | None = None,
    consecutive_losses_fn: Callable[[], int] | None = None,
    pending_orders_fn: Callable[[list[ScoredSignal]], PendingOrderDecision | None] | None = None,
    entry_gate_fn: Callable[[ScoredSignal], tuple[bool, str]] | None = None,
) -> AutoTradeDecision:
    machine = AutoTradeStateMachine()
    execution_mode = _execution_mode(config)
    equity = _cycle_equity(config, account_equity_fn)
    settings = _risk_line_settings(load_settings(), config.risk_line)
    consecutive_losses = _cycle_consecutive_losses(consecutive_losses_fn)
    longs, shorts = scan_fn()
    candidates = rank_candidates([*longs, *shorts], settings, execution_mode=execution_mode)
    pending_note = ""
    pending_symbols: set[str] = set()
    if execution_mode == AUTO_EXECUTION_LIVE and pending_orders_fn is not None:
        pending = pending_orders_fn(candidates)
        if pending is not None:
            pending_note = pending.message
            if pending.action != "canceled":
                pending_symbols = {
                    symbol.upper()
                    for symbol in (pending.symbols or ((pending.symbol,) if pending.symbol else ()))
                    if symbol
                }
            if pending.action == "blocked":
                machine.move(AutoTradeState.BLOCKED, pending.message, pending.symbol)
                return _decision(config, machine, "blocked", pending.message, None, None)
    portfolio = SimulatedPortfolio(config.portfolio_path) if config.portfolio_path else SimulatedPortfolio()
    guard = daily_loss_guard(
        equity=equity,
        realized_pnl=portfolio.today_realized_pnl(),
        stop_pct=config.max_daily_loss_pct,
    )
    if not guard.live_allowed:
        if execution_mode == AUTO_EXECUTION_LIVE:
            machine.move(AutoTradeState.BLOCKED, guard.message)
            return _decision(config, machine, "blocked", guard.message, None, None)
        pending_note = _prepend_pending_note(
            pending_note,
            f"模拟风险提醒：{guard.message.replace('真下单已锁定', '模拟继续运行')}",
        )

    managed_positions = managed_positions_fn() if managed_positions_fn is not None else _simulated_managed_positions(portfolio)
    add_stage_multiplier: float | None = None
    management_note = ""
    management = _best_position_management(managed_positions, candidates, config.mode, settings)
    if management is not None:
        managed, position_decision, signal_for_position = management
        if position_decision.action in {"close", "reduce", "move_stop"}:
            management_result = _execute_position_management(
                config,
                machine,
                portfolio,
                managed,
                position_decision,
                signal_for_position,
                execution_mode,
                position_order_fn,
            )
            if management_result.action in {"blocked", "order_uncertain"}:
                return management_result
            management_note = management_result.message
        elif position_decision.action == "hold" and position_decision.status != managed.status:
            if execution_mode == AUTO_EXECUTION_LIVE:
                _persist_live_position_management(portfolio, managed, position_decision)
            else:
                _update_simulated_position_record(portfolio, managed, position_decision)
            management_note = position_decision.message
        if position_decision.action == "add" and signal_for_position is not None:
            signal = signal_for_position
            add_stage_multiplier = position_decision.quantity if position_decision.quantity > 0 else None
        else:
            signal = _first_new_opportunity(candidates, managed_positions, pending_symbols)
    else:
        signal = _first_new_opportunity(candidates, managed_positions, pending_symbols)

    candidate_queue = (
        [signal]
        if signal is not None and add_stage_multiplier is not None and signal.symbol.upper() not in pending_symbols
        else _new_opportunities(candidates, managed_positions, pending_symbols)
    )
    if not candidate_queue:
        if management_note:
            return _decision(config, machine, "position_management", management_note, None, None)
        if pending_symbols:
            message = _prepend_pending_note(pending_note, "挂单正在后台管理；本轮没有其它可开仓候选")
            machine.move(AutoTradeState.WAITING_CONFIRMATION, message, next(iter(pending_symbols)))
            return _decision(config, machine, "live_order_pending", message, None, None)
        message = _prepend_pending_note(pending_note, "本轮没有可用信号")
        return _decision(config, machine, "no_signal", message, None, None)

    skipped: list[str] = []
    last_skipped: AutoTradeDecision | None = None
    executed: list[AutoTradeDecision] = []
    accepted_signals: list[ScoredSignal] = []
    max_new_positions = max(1, int(settings.get("auto_execution", {}).get("max_new_positions_per_cycle", 2)))
    for candidate in candidate_queue:
        candidate_settings = _secondary_slot_settings(settings) if executed and add_stage_multiplier is None else settings
        if add_stage_multiplier is None:
            correlation_allowed, correlation_message = correlation_gate(
                candidate,
                _correlation_references(candidates, managed_positions, accepted_signals),
                max_signed_correlation=float(
                    settings.get("auto_execution", {}).get("portfolio_max_signed_correlation", 0.75)
                ),
                max_correlated_positions=int(
                    settings.get("auto_execution", {}).get("portfolio_max_correlated_positions", 1)
                ),
                lookback=int(settings.get("auto_execution", {}).get("portfolio_correlation_lookback", 48)),
            )
            if not correlation_allowed:
                machine.move(AutoTradeState.WAITING_CONFIRMATION, "组合相关性风险限制", candidate.symbol)
                decision = _decision(
                    config,
                    machine,
                    "candidate_skipped",
                    f"组合风控跳过候选：{correlation_message}，继续检查下一候选",
                    candidate,
                    None,
                )
                skipped.append(f"{candidate.symbol}: {decision.message}")
                last_skipped = decision
                continue
        decision = _attempt_open_signal(
            config,
            machine,
            portfolio,
            candidate,
            execution_mode,
            equity,
            candidate_settings,
            consecutive_losses,
            add_stage_multiplier if candidate == signal else None,
            market_fresh_fn=market_fresh_fn,
            live_status_fn=live_status_fn,
            live_order_fn=live_order_fn,
            real_position_fn=real_position_fn,
            entry_gate_fn=entry_gate_fn,
        )
        if _should_try_next_candidate(decision):
            skipped.append(f"{candidate.symbol}: {decision.message}")
            last_skipped = decision
            continue
        if add_stage_multiplier is None and decision.action in {
            "live_order_sent",
            "live_order_pending",
            "simulated_order",
        }:
            executed.append(decision)
            accepted_signals.append(candidate)
            if len(executed) < max_new_positions:
                continue
            symbols = "、".join(item.signal.symbol for item in executed if item.signal is not None)
            decision = replace(decision, action="multi_position_opened", message=f"本轮已处理 {len(executed)} 个候选：{symbols}；{decision.message}")
        if pending_note:
            decision = replace(decision, message=_prepend_pending_note(pending_note, decision.message))
        if management_note:
            decision = replace(decision, message=f"{management_note}；继续扫描后：{decision.message}")
        return decision

    if executed:
        last = executed[-1]
        if len(executed) == 1:
            message = last.message
            if management_note:
                message = f"{management_note}；继续扫描后：{message}"
            return replace(last, message=_prepend_pending_note(pending_note, message))
        symbols = "、".join(item.signal.symbol for item in executed if item.signal is not None)
        message = f"本轮已处理 {len(executed)} 个候选：{symbols}；{last.message}"
        if management_note:
            message = f"{management_note}；继续扫描后：{message}"
        return replace(last, action="multi_position_opened", message=_prepend_pending_note(pending_note, message))

    if last_skipped is not None and last_skipped.plan is not None:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选轮换后保留最后一份计划")
        return _decision(
            config,
            machine,
            "planned",
            _prepend_pending_note(pending_note, "本轮候选都无法自动下单，已保留最后一份交易计划：" + last_skipped.message),
            last_skipped.signal,
            last_skipped.plan,
            last_skipped.review,
            position=last_skipped.position,
        )
    message = "本轮候选都未通过自动执行：" + "；".join(skipped[:5])
    message = _prepend_pending_note(pending_note, message)
    if management_note:
        message = f"{management_note}；继续扫描后：{message}"
    machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选轮换后仍无可执行机会")
    return _decision(config, machine, "no_executable_signal", message, None, None)


def _prepend_pending_note(note: str, message: str) -> str:
    return f"{note}；{message}" if note else message


def _secondary_slot_settings(settings: dict) -> dict:
    """Use a smaller diversified second slot instead of forcing a second full-risk trade."""
    result = deepcopy(settings)
    execution = result.get("auto_execution", {})
    positioning = result.get("automation_positioning", {})
    threshold = int(execution.get("secondary_min_open_score", 65))
    multiplier = float(execution.get("secondary_risk_multiplier", 0.5))
    positioning["min_open_score"] = threshold
    tiers = [dict(item) for item in positioning.get("score_tiers", [])]
    tiers.append({"min_score": threshold, "multiplier": max(0.05, multiplier * 0.4)})
    positioning["score_tiers"] = sorted(tiers, key=lambda item: float(item.get("min_score", 0)), reverse=True)
    result["automation_positioning"] = positioning
    return result


def _execution_mode(config: AutoTradeConfig) -> str:
    if config.execution_mode is None:
        return AUTO_EXECUTION_SIMULATE if config.auto_simulate else AUTO_EXECUTION_PLAN
    if config.execution_mode not in AUTO_EXECUTION_MODES:
        raise ValueError("execution_mode must be plan, simulate, or live")
    return config.execution_mode


def _risk_line_settings(settings: dict, risk_line: str) -> dict:
    if risk_line != "aggressive":
        return settings
    result = deepcopy(settings)
    aggressive = result.get("aggressive_line", {})
    positioning = result.setdefault("automation_positioning", {})
    system = result.setdefault("system_risk", {})
    risk_pct = float(aggressive.get("max_single_risk_pct", 2.5))
    positioning["first_entry_pct"] = float(aggressive.get("first_entry_pct", 1.0))
    positioning["max_single_risk_pct"] = risk_pct
    positioning["min_open_score"] = int(aggressive.get("min_open_score", positioning.get("min_open_score", 70)))
    positioning["max_symbol_exposure_pct"] = float(aggressive.get("max_symbol_exposure_pct", 200.0))
    positioning["max_total_exposure_pct"] = float(aggressive.get("max_total_exposure_pct", 350.0))
    tiers = aggressive.get("score_tiers")
    if isinstance(tiers, list):
        positioning["score_tiers"] = tiers
    for key in (
        "add_stage_pcts",
        "max_add_count",
        "min_profit_r_for_add",
        "min_add_score",
        "risk_reduce_pct",
        "profit_take_rules",
        "trailing_atr_multiplier",
        "time_stop_hours",
        "time_stop_min_r",
        "time_stop_min_score",
        "max_margin_drawdown_reduce_pct",
        "max_margin_drawdown_close_pct",
        "max_position_leverage",
        "loss_streak_reduce_after",
        "loss_streak_stop_after",
        "loss_streak_reduction_multiplier",
    ):
        if key in aggressive:
            positioning[key] = aggressive[key]
    result["default_risk_pct"] = risk_pct
    system["max_single_risk_pct"] = risk_pct
    system["max_symbol_exposure_pct"] = float(aggressive.get("max_symbol_exposure_pct", 200.0))
    system["max_total_exposure_multiple"] = float(aggressive.get("max_total_exposure_pct", 350.0)) / 100
    system["max_leverage"] = float(aggressive.get("max_leverage", 5.0))
    micro = result.setdefault("small_account_live", {})
    micro["min_notional_usdt"] = float(aggressive.get("min_notional_usdt", micro.get("min_notional_usdt", DEFAULT_MICRO_MIN_NOTIONAL)))
    micro["max_account_risk_pct"] = risk_pct
    micro["max_leverage"] = float(aggressive.get("max_leverage", 5.0))
    execution = result.setdefault("auto_execution", {})
    execution["risk_line"] = "aggressive"
    for key in ("live_min_score", "live_min_volume_ratio"):
        if key in aggressive:
            execution[key] = aggressive[key]
    if "live_block_warning_markers" in aggressive:
        execution["live_block_warning_markers"] = aggressive["live_block_warning_markers"]
    if "live_allowed_strategies" in aggressive:
        execution["live_allowed_strategies"] = aggressive["live_allowed_strategies"]
    for key in ("min_expected_net_gain_pct", "min_net_to_cost_multiple", "min_net_reward_r"):
        if key in aggressive:
            execution[key] = aggressive[key]
    return result


def _cycle_equity(config: AutoTradeConfig, account_equity_fn: Callable[[], float] | None) -> float:
    if _execution_mode(config) != AUTO_EXECUTION_LIVE:
        return config.equity
    if not config.auto_detect_account or account_equity_fn is None:
        return config.equity
    try:
        equity = float(account_equity_fn())
    except Exception:
        return config.equity
    return equity if equity > 0 else config.equity


def _cycle_consecutive_losses(consecutive_losses_fn: Callable[[], int] | None) -> int:
    if consecutive_losses_fn is None:
        return 0
    try:
        return max(0, int(consecutive_losses_fn()))
    except Exception:
        return 0


def _attempt_open_signal(
    config: AutoTradeConfig,
    machine: AutoTradeStateMachine,
    portfolio: SimulatedPortfolio,
    signal: ScoredSignal,
    execution_mode: str,
    equity: float,
    settings: dict,
    consecutive_losses: int,
    stage_multiplier: float | None,
    *,
    market_fresh_fn: Callable[[ScoredSignal], tuple[bool, str]] | None,
    live_status_fn: Callable[[ScoredSignal], tuple[bool, str]] | None,
    live_order_fn: Callable[[TradePlan, str, PlanRiskReview, ScoredSignal], dict] | None,
    real_position_fn: Callable[[ScoredSignal], PositionSnapshot | None] | None,
    entry_gate_fn: Callable[[ScoredSignal], tuple[bool, str]] | None,
) -> AutoTradeDecision:
    machine.move(AutoTradeState.OPPORTUNITY_FOUND, "扫描发现候选信号", signal.symbol)
    if signal.breakdown.action_level in {"block_live", "avoid"}:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选评分低于交易线或触发硬风险", signal.symbol)
        return _decision(config, machine, "candidate_skipped", "评分系统判定不交易，继续检查下一候选", signal, None)
    if signal.market == "spot" and signal.side == "short" and execution_mode != AUTO_EXECUTION_PLAN:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "现货不允许自动做空，继续轮换", signal.symbol)
        return _decision(config, machine, "candidate_skipped", "现货做空信号只观察，继续检查下一候选", signal, None)
    if execution_mode == AUTO_EXECUTION_LIVE:
        opportunity = assess_opportunity(signal, settings, execution_mode="live")
        if not opportunity.allowed:
            machine.move(AutoTradeState.WAITING_CONFIRMATION, "研究型选币过滤", signal.symbol)
            return _decision(
                config,
                machine,
                "candidate_skipped",
                "研究型选币过滤："
                + "；".join([*opportunity.warnings, f"研究评分 {opportunity.score:.0f}/{opportunity.tier}"])[:220],
                signal,
                None,
            )
        quality_allowed, quality_message = _live_entry_quality_allowed(signal, settings)
        if not quality_allowed:
            machine.move(AutoTradeState.WAITING_CONFIRMATION, "自动真仓质量过滤", signal.symbol)
            return _decision(config, machine, "candidate_skipped", quality_message, signal, None)
        recovery_allowed, recovery_message = _aggressive_recovery_allowed(
            config, signal, settings, consecutive_losses
        )
        if not recovery_allowed:
            machine.move(AutoTradeState.WAITING_CONFIRMATION, "连续亏损恢复模式", signal.symbol)
            return _decision(config, machine, "candidate_skipped", recovery_message, signal, None)

    prices = auto_plan_prices(signal, config.mode)
    sizing_stage_multiplier = stage_multiplier
    recovery_probe = False
    if config.risk_line == "aggressive" and stage_multiplier is None:
        recovery_after = int(settings.get("aggressive_line", {}).get("recovery_after_consecutive_losses", 5))
        if consecutive_losses >= recovery_after:
            base_stage = automation_sizing_config(settings).first_entry_pct
            sizing_stage_multiplier = base_stage * float(
                settings.get("aggressive_line", {}).get("recovery_risk_multiplier", 0.25)
            )
            recovery_probe = True
    sizing = initial_sizing_decision(
        signal,
        config.mode,
        settings,
        base_risk_pct=float(settings.get("default_risk_pct", 1.0)),
        stage_multiplier=sizing_stage_multiplier,
        consecutive_losses=consecutive_losses,
    )
    if not sizing.allowed:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "动态仓位系统跳过候选", signal.symbol)
        return _decision(
            config,
            machine,
            "candidate_skipped",
            "动态仓位系统不允许开仓/加仓：" + "；".join(sizing.warnings or ["风险比例为 0"]),
            signal,
            None,
        )

    existing = _existing_position_for_mode(execution_mode, signal, portfolio, real_position_fn)
    if existing.side == "flat" and entry_gate_fn is not None:
        allowed, message = entry_gate_fn(signal)
        if not allowed:
            machine.move(AutoTradeState.WAITING_CONFIRMATION, "自动入场频率保护", signal.symbol)
            return _decision(config, machine, "candidate_skipped", message, signal, None)
    leverage = prices.adaptive.suggested_leverage if prices.adaptive else 1.0
    if config.risk_line == "aggressive":
        leverage = _aggressive_leverage(leverage, signal, settings, config.mode)
    plan, review = evaluate_plan_from_form(
        symbol=signal.symbol,
        market=signal.market,
        side=signal.side,
        entry=str(prices.entry),
        stop=str(prices.stop),
        target=str(prices.target),
        equity=str(equity),
        risk_pct=str(sizing.risk_pct),
        leverage=str(leverage),
        signal=signal,
        position=existing if existing.side != "flat" else None,
        mode=config.mode,
        allocation_pct_override=(
            float(settings.get("aggressive_line", {}).get("risk_allocation_pct", 100.0))
            if config.risk_line == "aggressive"
            else None
        ),
    )
    if config.risk_line == "aggressive" and not recovery_probe:
        plan, review = _apply_aggressive_margin_target(
            plan,
            review,
            signal,
            existing if existing.side != "flat" else None,
            settings,
            config.mode,
        )
    else:
        plan, review = _cap_plan_to_margin_budget(
            plan,
            review,
            signal,
            existing if existing.side != "flat" else None,
            settings,
            config.mode,
        )
    if execution_mode == AUTO_EXECUTION_LIVE:
        plan, review = _apply_live_micro_notional_floor(plan, review, signal, settings)
    economics_allowed, economics_message = _entry_economics_allowed(plan, settings)
    if not economics_allowed:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "预期收益不足以覆盖成本", signal.symbol)
        return _decision(config, machine, "candidate_skipped", economics_message, signal, plan, review)
    if execution_mode == AUTO_EXECUTION_LIVE and not review.live_allowed and _live_can_use_small_risk(review):
        review = replace(
            review,
            live_allowed=True,
            recommended_action="谨慎小仓",
            warnings=[
                *review.warnings,
                f"原自适应建议为 {review.recommended_action}，本轮按小仓位 {plan.risk_pct:.2f}% 和低杠杆 {plan.leverage:.1f}x 执行",
            ],
        )
    machine.move(AutoTradeState.PLAN_GENERATED, "已生成交易计划", signal.symbol)

    positioning_config = automation_sizing_config(settings)
    if existing.side != "flat" and not exposure_allowed(
        equity=equity,
        current_symbol_notional=existing.notional,
        add_notional=plan.notional,
        config=positioning_config,
    ):
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选单币仓位达到上限", signal.symbol)
        return _decision(
            config,
            machine,
            "candidate_skipped",
            f"单币敞口将超过权益 {positioning_config.max_symbol_exposure_pct:.0f}%，继续检查下一候选",
            signal,
            plan,
            review,
            position=existing,
        )
    if not review.live_allowed and execution_mode == AUTO_EXECUTION_LIVE:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选未通过真仓评审", signal.symbol)
        return _decision(
            config,
            machine,
            "candidate_skipped",
            f"风控评审不允许自动执行：{review.recommended_action}",
            signal,
            plan,
            review,
        )
    if execution_mode == AUTO_EXECUTION_PLAN:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "等待人工确认", signal.symbol)
        return _decision(config, machine, "planned", "已自动生成计划，等待人工确认", signal, plan, review)
    if execution_mode == AUTO_EXECUTION_LIVE:
        return _run_live_order(
            config,
            machine,
            signal,
            plan,
            review,
            market_fresh_fn=market_fresh_fn,
            live_status_fn=live_status_fn,
            live_order_fn=live_order_fn,
            real_position_fn=real_position_fn,
        )

    order_side = "BUY" if signal.side == "long" else "SELL"
    try:
        _, position = simulate_order_from_form(
            market=signal.market,
            symbol=signal.symbol,
            side=order_side,
            quantity=f"{plan.quantity:.8f}",
            order_type="LIMIT",
            price=f"{plan.entry:.8f}",
            fallback_price=f"{plan.entry:.8f}",
            leverage=f"{plan.leverage:.8f}",
            portfolio_path=config.portfolio_path,
        )
    except ValueError as exc:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选下单数量过小", signal.symbol)
        return _decision(
            config,
            machine,
            "candidate_skipped",
            f"资金池限制后下单数量过小，继续检查下一候选：{exc}",
            signal,
            plan,
            review,
        )
    machine.move(AutoTradeState.OPENED, "已模拟加仓" if existing.side != "flat" else "已模拟开仓", signal.symbol)
    machine.move(AutoTradeState.MANAGING, "进入持仓管理", signal.symbol)
    action = "simulated_add" if existing.side != "flat" else "simulated_order"
    portfolio.upsert_position_record(
        source=position.source,
        market=position.market,
        symbol=position.symbol,
        side=position.side,
        quantity=position.quantity,
        entry_price=position.entry_price,
        mark_price=position.mark_price,
        stop_price=plan.stop,
        target_price=plan.target,
        leverage=plan.leverage,
        realized_pnl=position.realized_pnl,
        status=_position_status_after_entry(existing, stage_multiplier),
    )
    message = (
        f"已按动态仓位规则模拟加仓：{sizing.stage_label}，风险 {sizing.risk_pct:.2f}%"
        if existing.side != "flat"
        else f"已按动态仓位规则模拟初始建仓：{sizing.stage_label}，风险 {sizing.risk_pct:.2f}%"
    )
    if recovery_probe:
        message += "；连续亏损恢复模式，仅使用试探仓"
    return _decision(config, machine, action, message, signal, plan, review, position)


def _apply_live_micro_notional_floor(
    plan: TradePlan,
    review: PlanRiskReview,
    signal: ScoredSignal,
    settings: dict,
) -> tuple[TradePlan, PlanRiskReview]:
    micro = settings.get("small_account_live", {})
    if not bool(micro.get("enabled", True)):
        return plan, review
    if plan.market != "futures":
        return plan, review
    max_equity = float(micro.get("max_equity", 100.0))
    total_equity = review.total_equity if review.total_equity > 0 else plan.equity
    if total_equity <= 0 or total_equity > max_equity:
        return plan, review
    min_notional = float(micro.get("min_notional_usdt", DEFAULT_MICRO_MIN_NOTIONAL))
    if plan.notional >= min_notional:
        return plan, review
    max_account_risk_pct = float(micro.get("max_account_risk_pct", 1.0))
    loss_pct = plan.loss_pct_to_stop
    boosted_risk_amount = min_notional * loss_pct / 100
    boosted_account_risk_pct = boosted_risk_amount / total_equity * 100
    if boosted_account_risk_pct > max_account_risk_pct + 1e-9:
        warning = (
            f"小账户微仓需要 {min_notional:.2f}U 名义价值，但止损风险 "
            f"{boosted_account_risk_pct:.2f}% 超过账户上限 {max_account_risk_pct:.2f}%"
        )
        return plan, replace(review, warnings=[*review.warnings, warning])

    quantity = min_notional / plan.entry
    leverage = min(plan.leverage, float(micro.get("max_leverage", 2.0)))
    leverage = max(1.0, leverage)
    boosted_plan = replace(
        plan,
        leverage=leverage,
        risk_amount=boosted_risk_amount,
        risk_pct=boosted_risk_amount / plan.equity * 100,
        quantity=quantity,
        notional=min_notional,
        margin_required=min_notional / leverage,
        leveraged_loss_pct=loss_pct * leverage,
        leveraged_gain_pct=plan.gain_pct_to_target * leverage,
    )
    note = (
        f"小账户微仓模式：原计划名义价值 {plan.notional:.2f}U 低于交易所最小额，"
        f"已抬到 {min_notional:.2f}U；账户级止损风险约 {boosted_account_risk_pct:.2f}%"
    )
    return boosted_plan, replace(
        review,
        recommended_action="微仓真仓",
        live_allowed=True,
        suggested_leverage=min(review.suggested_leverage, leverage),
        warnings=[*review.warnings, note],
    )


def _aggressive_leverage(base_leverage: float, signal: ScoredSignal, settings: dict, mode: str) -> float:
    aggressive = settings.get("aggressive_line", {})
    min_leverage = float(aggressive.get("min_leverage", 4.0))
    max_leverage = float(aggressive.get("max_leverage", 8.0))
    if max_leverage < min_leverage:
        max_leverage = min_leverage
    if signal.score >= 90:
        target = max_leverage
    elif signal.score >= 80:
        target = max(min_leverage, max_leverage - 1.0)
    elif signal.score >= 72:
        target = max(min_leverage, min(max_leverage, min_leverage + 1.0))
    else:
        target = min_leverage

    base = signal.signal
    atr = base.atr_4h_pct if mode == "swing" and base.atr_4h_pct is not None else base.atr_1h_pct
    atr = atr if atr is not None else base.atr_pct
    if atr is not None:
        if atr >= float(aggressive.get("leverage_cut_atr_pct", 4.5)):
            target = min(target, min_leverage + 1.0)
        if atr >= float(aggressive.get("leverage_floor_atr_pct", 6.0)):
            target = min_leverage
    return round(max(1.0, min(max_leverage, max(min_leverage, target, base_leverage))), 1)


def _apply_aggressive_margin_target(
    plan: TradePlan,
    review: PlanRiskReview,
    signal: ScoredSignal,
    position: PositionSnapshot | None,
    settings: dict,
    mode: str,
) -> tuple[TradePlan, PlanRiskReview]:
    if plan.market != "futures" or plan.entry <= 0 or plan.loss_pct_to_stop <= 0:
        return plan, review
    aggressive = settings.get("aggressive_line", {})
    total_equity = review.total_equity if review.total_equity > 0 else plan.equity
    if total_equity <= 0:
        return plan, review

    target_margin_pct = _aggressive_margin_pct(signal.score, aggressive)
    target_notional = total_equity * target_margin_pct / 100 * plan.leverage
    risk_cap_notional = total_equity * float(aggressive.get("max_single_risk_pct", 5.0)) / 100 / (plan.loss_pct_to_stop / 100)
    exposure_cap_notional = total_equity * float(aggressive.get("max_symbol_exposure_pct", 250.0)) / 100
    capped_notional = min(target_notional, risk_cap_notional, exposure_cap_notional)
    if capped_notional <= 0 or abs(capped_notional - plan.notional) <= 1e-9:
        return plan, review

    adjusted = _plan_with_notional(plan, capped_notional)
    refreshed = evaluate_plan_risk(
        adjusted,
        signal.signal,
        position,
        mode,
        min_live_score=int(settings.get("min_live_score", 75)),
    )
    action = "抬升" if capped_notional > plan.notional else "压低"
    note = (
        f"激进线按本金预算{action}仓位：保证金约 {adjusted.margin_required:.2f}U "
        f"({adjusted.margin_required / total_equity * 100:.0f}% 权益，上限 {target_margin_pct:.0f}%)，"
        f"名义价值 {adjusted.notional:.2f}U，账户级止损风险 {adjusted.risk_amount / total_equity * 100:.2f}%"
    )
    refreshed = replace(
        refreshed,
        risk_bucket=review.risk_bucket,
        allocation_pct=review.allocation_pct,
        allocation_equity=review.allocation_equity,
        total_equity=review.total_equity,
        reasons=[*refreshed.reasons, note, *review.reasons],
    )
    return adjusted, refreshed


def _cap_plan_to_margin_budget(
    plan: TradePlan,
    review: PlanRiskReview,
    signal: ScoredSignal,
    position: PositionSnapshot | None,
    settings: dict,
    mode: str,
) -> tuple[TradePlan, PlanRiskReview]:
    if plan.market != "futures" or plan.entry <= 0 or plan.leverage <= 0:
        return plan, review
    total_equity = review.total_equity if review.total_equity > 0 else plan.equity
    if total_equity <= 0:
        return plan, review
    positioning = settings.get("automation_positioning", {})
    max_margin_pct = float(
        positioning.get(
            "max_initial_margin_pct",
            min(20.0, float(positioning.get("max_symbol_exposure_pct", 40.0))),
        )
    )
    max_margin = total_equity * max(0.0, max_margin_pct) / 100
    max_notional = max_margin * plan.leverage
    if max_notional <= 0 or plan.notional <= max_notional + 1e-9:
        return plan, review

    adjusted = _plan_with_notional(plan, max_notional)
    refreshed = evaluate_plan_risk(
        adjusted,
        signal.signal,
        position,
        mode,
        min_live_score=int(settings.get("min_live_score", 75)),
    )
    note = (
        f"自动仓位已按本金预算封顶：保证金 {adjusted.margin_required:.2f}U "
        f"({adjusted.margin_required / total_equity * 100:.0f}% 权益)，"
        f"名义价值 {adjusted.notional:.2f}U，避免近止损把数量放大"
    )
    refreshed = replace(
        refreshed,
        risk_bucket=review.risk_bucket,
        allocation_pct=review.allocation_pct,
        allocation_equity=review.allocation_equity,
        total_equity=review.total_equity,
        reasons=[*refreshed.reasons, note, *review.reasons],
    )
    return adjusted, refreshed


def _plan_with_notional(plan: TradePlan, notional: float) -> TradePlan:
    quantity = notional / plan.entry
    risk_amount = notional * plan.loss_pct_to_stop / 100
    return replace(
        plan,
        risk_amount=risk_amount,
        risk_pct=risk_amount / plan.equity * 100,
        quantity=quantity,
        notional=notional,
        margin_required=notional / plan.leverage,
        leveraged_loss_pct=plan.loss_pct_to_stop * plan.leverage,
        leveraged_gain_pct=plan.gain_pct_to_target * plan.leverage,
    )


def _aggressive_margin_pct(score: int, aggressive: dict) -> float:
    tiers = aggressive.get("margin_tiers", [])
    if isinstance(tiers, list):
        for row in sorted(
            (item for item in tiers if isinstance(item, dict)),
            key=lambda item: float(item.get("min_score", 0)),
            reverse=True,
        ):
            if score >= int(row.get("min_score", 0)):
                return max(0.0, float(row.get("margin_pct", 0.0)))
    if score >= 90:
        return float(aggressive.get("target_margin_pct_a", 45.0))
    if score >= 80:
        return float(aggressive.get("target_margin_pct_b", 35.0))
    if score >= 70:
        return float(aggressive.get("target_margin_pct_c", 28.0))
    return float(aggressive.get("target_margin_pct_probe", 18.0))


def _entry_economics_allowed(plan: TradePlan, settings: dict) -> tuple[bool, str]:
    execution = settings.get("auto_execution", {})
    round_trip_cost_pct = max(0.0, float(execution.get("estimated_round_trip_cost_bps", 16.0))) / 100
    min_net_gain_pct = max(0.0, float(execution.get("min_expected_net_gain_pct", 0.45)))
    min_cost_multiple = max(1.0, float(execution.get("min_net_to_cost_multiple", 2.5)))
    min_net_r = max(0.0, float(execution.get("min_net_reward_r", 0.0)))
    gross_gain_pct = plan.gain_pct_to_target
    net_gain_pct = gross_gain_pct - round_trip_cost_pct
    required_gain_pct = max(min_net_gain_pct, round_trip_cost_pct * min_cost_multiple)
    if net_gain_pct + 1e-9 < required_gain_pct:
        return (
            False,
            f"预期毛收益 {gross_gain_pct:.2f}% 扣除手续费/滑点估算 {round_trip_cost_pct:.2f}% 后仅 {net_gain_pct:.2f}%，低于自动开仓线 {required_gain_pct:.2f}%",
        )
    if min_net_r > 0 and plan.loss_pct_to_stop > 0:
        net_r = net_gain_pct / plan.loss_pct_to_stop
        if net_r + 1e-9 < min_net_r:
            return (
                False,
                f"扣费后目标只有 {net_r:.2f}R，低于自动开仓线 {min_net_r:.2f}R，避免被手续费和噪音消耗",
            )
    return True, "预期净收益覆盖手续费和滑点"


def _live_entry_quality_allowed(signal: ScoredSignal, settings: dict) -> tuple[bool, str]:
    execution = settings.get("auto_execution", {})
    aggressive = execution.get("risk_line") == "aggressive"
    min_score = int(execution.get("live_min_score", 72))
    if signal.score < min_score:
        return False, f"自动真仓质量过滤：评分 {signal.score} 低于真仓线 {min_score}"

    min_volume_ratio = float(execution.get("live_min_volume_ratio", 1.2))
    volume_ratio = float(signal.signal.volume_ratio or 0.0)
    if volume_ratio < min_volume_ratio:
        return False, f"自动真仓质量过滤：量能仅 {volume_ratio:.2f} 倍，低于 {min_volume_ratio:.2f} 倍"

    allowed_strategies = {
        str(item).strip().lower()
        for item in execution.get("live_allowed_strategies", ["trend_following", "breakout"])
        if str(item).strip()
    }
    selected_strategy = str(signal.breakdown.selected_strategy or "").lower()
    if not aggressive and allowed_strategies and selected_strategy not in allowed_strategies:
        return False, f"自动真仓质量过滤：{selected_strategy or '未识别'} 策略不在真仓白名单"

    warning_markers = tuple(
        str(item).strip()
        for item in execution.get(
            "live_block_warning_markers",
            [
                "多周期方向冲突",
                "BTC/ETH 大盘环境相反",
                "识别出的市场趋势相反",
                "主动成交方向与信号背离",
                "价格离保护结构较远",
            ],
        )
        if str(item).strip()
    )
    matched = [warning for warning in signal.breakdown.warnings if any(marker in warning for marker in warning_markers)]
    if matched:
        return False, f"自动真仓质量过滤：{'；'.join(matched[:2])}"
    return True, "真仓质量过滤通过"


def _aggressive_recovery_allowed(
    config: AutoTradeConfig,
    signal: ScoredSignal,
    settings: dict,
    consecutive_losses: int,
) -> tuple[bool, str]:
    if config.risk_line != "aggressive":
        return True, "非激进线无需恢复模式"
    aggressive = settings.get("aggressive_line", {})
    trigger = int(aggressive.get("recovery_after_consecutive_losses", 5))
    if consecutive_losses < trigger:
        return True, "未触发连续亏损恢复模式"
    min_score = int(aggressive.get("recovery_min_score", 82))
    min_volume = float(aggressive.get("recovery_min_volume_ratio", 1.05))
    if signal.score < min_score:
        return False, f"连续亏损 {consecutive_losses} 次进入恢复模式：仅允许评分 {min_score}+ 的试探仓"
    if float(signal.signal.volume_ratio or 0.0) < min_volume:
        return False, f"连续亏损恢复模式：量能需达到 {min_volume:.2f} 倍以上"
    return True, "连续亏损恢复模式：高质量试探仓通过"


def _live_can_use_small_risk(review: PlanRiskReview) -> bool:
    if review.live_allowed:
        return True
    if review.liquidation_status == "不建议下单":
        return False
    return review.recommended_action in {"只建议模拟", "谨慎小仓"}


def _should_try_next_candidate(decision: AutoTradeDecision) -> bool:
    if decision.action != "candidate_skipped":
        return False
    text = decision.message
    return not _is_global_execution_block(text)


def _is_global_execution_block(text: str) -> bool:
    global_blocks = (
        "确认文字",
        "实时行情",
        "API",
        "账户状态",
        "账户权益无效",
        "急停",
        "日亏损",
        "订单状态不确定",
        "保护单提交失败",
    )
    return any(item in text for item in global_blocks)


def _existing_position_for_mode(
    execution_mode: str,
    signal: ScoredSignal,
    portfolio: SimulatedPortfolio,
    real_position_fn: Callable[[ScoredSignal], PositionSnapshot | None] | None,
) -> PositionSnapshot:
    if execution_mode == AUTO_EXECUTION_LIVE and real_position_fn is not None:
        real = real_position_fn(signal)
        if real is not None:
            return real
    return portfolio.get_position(signal.market, signal.symbol, mark_price=signal.last)


def _simulated_managed_positions(portfolio: SimulatedPortfolio) -> list[ManagedPosition]:
    managed: list[ManagedPosition] = []
    for record in portfolio.position_records():
        if record["side"] not in {"long", "short"} or float(record["quantity"]) <= 0:
            continue
        position = portfolio.get_position(record["market"], record["symbol"], mark_price=float(record["mark_price"]))
        managed.append(
            ManagedPosition(
                position=position,
                stop_price=float(record["stop_price"]),
                target_price=float(record["target_price"]),
                status=str(record["status"]),
            )
        )
    return managed


def _best_position_management(
    managed_positions: list[ManagedPosition],
    candidates: list[ScoredSignal],
    mode: str = "intraday",
    settings: dict | None = None,
) -> tuple[ManagedPosition, PositionManagementDecision, ScoredSignal | None] | None:
    priority = {"close": 0, "reduce": 1, "move_stop": 2, "add": 3, "hold": 4, "ignore": 5}
    decisions: list[tuple[int, ManagedPosition, PositionManagementDecision, ScoredSignal | None]] = []
    for managed in managed_positions:
        same = _matching_signal(candidates, managed.position, same_side=True)
        opposite = _matching_signal(candidates, managed.position, same_side=False)
        decision = manage_position(
            managed,
            same_side_signal=same,
            opposite_signal=opposite,
            mode=mode,
            settings=settings,
        )
        decisions.append((priority.get(decision.action, 9), managed, decision, same))
    if not decisions:
        return None
    decisions.sort(key=lambda item: item[0])
    _, managed, decision, signal = decisions[0]
    return managed, decision, signal


def _matching_signal(
    candidates: list[ScoredSignal],
    position: PositionSnapshot,
    *,
    same_side: bool,
) -> ScoredSignal | None:
    matches = [
        candidate
        for candidate in candidates
        if candidate.market == position.market
        and candidate.symbol == position.symbol
        and ((candidate.side == position.side) == same_side)
    ]
    return matches[0] if matches else None


def _first_new_opportunity(
    candidates: list[ScoredSignal],
    managed_positions: list[ManagedPosition],
    blocked_symbols: set[str] | None = None,
) -> ScoredSignal | None:
    opportunities = _new_opportunities(candidates, managed_positions, blocked_symbols)
    return opportunities[0] if opportunities else None


def _new_opportunities(
    candidates: list[ScoredSignal],
    managed_positions: list[ManagedPosition],
    blocked_symbols: set[str] | None = None,
) -> list[ScoredSignal]:
    held = {(item.position.market, item.position.symbol) for item in managed_positions if item.position.side != "flat"}
    blocked_symbols = {symbol.upper() for symbol in (blocked_symbols or set())}
    blocked_fallbacks: list[ScoredSignal] = []
    opportunities: list[ScoredSignal] = []
    for candidate in candidates:
        if candidate.symbol.upper() in blocked_symbols:
            continue
        if (candidate.market, candidate.symbol) in held:
            continue
        if candidate.breakdown.action_level in {"block_live", "avoid"}:
            blocked_fallbacks.append(candidate)
            continue
        opportunities.append(candidate)
    return opportunities or blocked_fallbacks


def _correlation_references(
    candidates: list[ScoredSignal],
    managed_positions: list[ManagedPosition],
    accepted_signals: list[ScoredSignal],
) -> list[ScoredSignal]:
    """Return directional return-series references for portfolio risk clustering.

    A live position only joins the cluster if this scan contains a same-direction
    signal for it. That gives the position a current, comparable return series;
    missing history deliberately leaves the candidate eligible.
    """
    references = list(accepted_signals)
    for managed in managed_positions:
        position = managed.position
        if position.side not in {"long", "short"} or position.quantity <= 0:
            continue
        signal = _matching_signal(candidates, position, same_side=True)
        if signal is not None:
            references.append(signal)
    return references


def _execute_position_management(
    config: AutoTradeConfig,
    machine: AutoTradeStateMachine,
    portfolio: SimulatedPortfolio,
    managed: ManagedPosition,
    position_decision: PositionManagementDecision,
    signal: ScoredSignal | None,
    execution_mode: str,
    position_order_fn: Callable[[ManagedPosition, PositionManagementDecision], dict] | None,
) -> AutoTradeDecision:
    position = managed.position
    machine.move(AutoTradeState.MANAGING, position_decision.message, position.symbol)
    if execution_mode == AUTO_EXECUTION_PLAN:
        return _decision(
            config,
            machine,
            "position_management",
            position_decision.message,
            signal,
            None,
            position=position,
        )
    if position_decision.action == "move_stop":
        if execution_mode == AUTO_EXECUTION_LIVE:
            if position_order_fn is None:
                machine.move(AutoTradeState.BLOCKED, "真实仓移动止损通道未配置", position.symbol)
                return _decision(config, machine, "blocked", "真实仓移动止损通道未配置", signal, None, position=position)
            from .broker import LIVE_CONFIRMATION

            if config.live_confirm != LIVE_CONFIRMATION:
                machine.move(AutoTradeState.BLOCKED, "真实仓移动止损确认文字不匹配", position.symbol)
                return _decision(config, machine, "blocked", "真实仓移动止损确认文字不匹配", signal, None, position=position)
            try:
                result = position_order_fn(managed, position_decision)
            except (ValueError, RuntimeError) as exc:
                return _skip_too_small_live_management(
                    config, machine, portfolio, managed, position_decision, signal, exc
                )
            if result.get("dry_run") or result.get("uncertain"):
                machine.move(AutoTradeState.BLOCKED, "真实仓止损单更新未确认", position.symbol)
                return _decision(config, machine, "blocked", "真实仓止损单更新未确认", signal, None, position=position)
            return _decision(
                config,
                machine,
                "live_stop_moved",
                position_decision.message,
                signal,
                None,
                position=position,
            )
        _update_simulated_position_record(portfolio, managed, position_decision)
        return _decision(
            config,
            machine,
            "stop_moved",
            position_decision.message,
            signal,
            None,
            position=position,
        )
    if execution_mode == AUTO_EXECUTION_SIMULATE:
        try:
            _, updated = simulate_order_from_form(
                market=position.market,
                symbol=position.symbol,
                side=position_decision.exit_side,
                quantity=f"{position_decision.quantity:.8f}",
                order_type="MARKET",
                price=f"{position.mark_price:.8f}",
                fallback_price=f"{position.mark_price:.8f}",
                portfolio_path=config.portfolio_path,
            )
        except ValueError as exc:
            machine.move(AutoTradeState.WAITING_CONFIRMATION, "减仓数量低于交易所最小下单量", position.symbol)
            return _decision(
                config,
                machine,
                "position_management",
                f"{position_decision.message}；但减仓数量过小，未自动提交：{exc}",
                signal,
                None,
                position=position,
            )
        _update_simulated_position_record(portfolio, managed, position_decision)
        return _decision(
            config,
            machine,
            "position_reduced" if position_decision.action == "reduce" else "position_closed",
            position_decision.message,
            signal,
            None,
            position=updated,
        )
    if position_order_fn is None:
        machine.move(AutoTradeState.BLOCKED, "自动真仓仓位管理通道未配置", position.symbol)
        return _decision(config, machine, "blocked", "自动真仓仓位管理通道未配置", signal, None, position=position)
    from .broker import LIVE_CONFIRMATION

    if config.live_confirm != LIVE_CONFIRMATION:
        machine.move(AutoTradeState.BLOCKED, "自动真仓仓位管理确认文字不匹配", position.symbol)
        return _decision(config, machine, "blocked", "自动真仓仓位管理确认文字不匹配", signal, None, position=position)
    try:
        result = position_order_fn(managed, position_decision)
    except (ValueError, RuntimeError) as exc:
        return _skip_too_small_live_management(
            config, machine, portfolio, managed, position_decision, signal, exc
        )
    if result.get("dry_run"):
        machine.move(AutoTradeState.BLOCKED, "自动真仓仓位管理未真正发送订单", position.symbol)
        return _decision(config, machine, "blocked", "自动真仓仓位管理未真正发送订单", signal, None, position=position)
    _persist_live_position_management(portfolio, managed, position_decision)
    return _decision(
        config,
        machine,
        "live_position_reduced" if position_decision.action == "reduce" else "live_position_closed",
        position_decision.message,
        signal,
        None,
        position=position,
    )


def _update_simulated_position_record(
    portfolio: SimulatedPortfolio,
    managed: ManagedPosition,
    position_decision: PositionManagementDecision,
) -> None:
    position = managed.position
    stop = position_decision.new_stop if position_decision.new_stop is not None else managed.stop_price
    status = position_decision.status or managed.status
    quantity = position.quantity
    if position_decision.action in {"reduce", "close"}:
        quantity = max(0.0, position.quantity - position_decision.quantity)
    if quantity <= 0:
        status = position_decision.status or "已平仓"
    portfolio.upsert_position_record(
        source=position.source,
        market=position.market,
        symbol=position.symbol,
        side=position.side,
        quantity=quantity,
        entry_price=position.entry_price,
        mark_price=position.mark_price,
        stop_price=stop,
        target_price=managed.target_price,
        leverage=position.leverage,
        realized_pnl=position.realized_pnl,
        status=status,
    )


def _persist_live_position_management(
    portfolio: SimulatedPortfolio,
    managed: ManagedPosition,
    position_decision: PositionManagementDecision,
) -> None:
    """Keep one-shot management markers until Binance confirms the next position snapshot."""
    position = managed.position
    portfolio.upsert_position_record(
        source="real",
        market=position.market,
        symbol=position.symbol,
        side=position.side,
        quantity=position.quantity,
        entry_price=position.entry_price,
        mark_price=position.mark_price,
        stop_price=position_decision.new_stop if position_decision.new_stop is not None else managed.stop_price,
        target_price=managed.target_price,
        leverage=position.leverage,
        realized_pnl=position.realized_pnl,
        status=position_decision.status or managed.status,
    )


def _skip_too_small_live_management(
    config: AutoTradeConfig,
    machine: AutoTradeStateMachine,
    portfolio: SimulatedPortfolio,
    managed: ManagedPosition,
    decision: PositionManagementDecision,
    signal: ScoredSignal | None,
    error: Exception,
) -> AutoTradeDecision:
    """A fractional reduce can be below Binance's lot step; do not kill the cycle."""
    text = str(error)
    if "-4120" in text or "Algo Order API" in text:
        machine.move(AutoTradeState.MANAGING, "条件单接口暂未确认，保留仓位并继续扫描", managed.position.symbol)
        return _decision(
            config,
            machine,
            "position_management",
            f"{decision.message}；Binance 条件单暂未确认，未改变仓位，下一轮会重试：{text}",
            signal,
            None,
            position=managed.position,
        )
    if "数量" not in text and "规整后为 0" not in text:
        raise error
    skipped = replace(decision, status=_append_management_marker(decision.status or managed.status, "数量过小已跳过"))
    _persist_live_position_management(portfolio, managed, skipped)
    machine.move(AutoTradeState.MANAGING, "减仓数量低于交易所最小值，已跳过", managed.position.symbol)
    return _decision(
        config,
        machine,
        "position_management",
        f"{decision.message}；减仓数量低于交易所最小下单量，已跳过且继续扫描其它候选：{text}",
        signal,
        None,
        position=managed.position,
    )


def _append_management_marker(status: str, marker: str) -> str:
    return status if marker in status else f"{status}/{marker}".strip("/")


def _position_status_after_entry(existing: PositionSnapshot, add_stage_multiplier: float | None) -> str:
    if existing.side != "flat":
        return "ADD_POSITION/顺势加仓" if add_stage_multiplier else "ADD_POSITION/加仓"
    return "INITIAL/初始试探仓"


def _run_live_order(
    config: AutoTradeConfig,
    machine: AutoTradeStateMachine,
    signal: ScoredSignal,
    plan: TradePlan,
    review: PlanRiskReview,
    *,
    market_fresh_fn: Callable[[ScoredSignal], tuple[bool, str]] | None,
    live_status_fn: Callable[[ScoredSignal], tuple[bool, str]] | None,
    live_order_fn: Callable[[TradePlan, str, PlanRiskReview, ScoredSignal], dict] | None,
    real_position_fn: Callable[[ScoredSignal], PositionSnapshot | None] | None,
) -> AutoTradeDecision:
    from .broker import LIVE_CONFIRMATION

    machine.move(AutoTradeState.WAITING_CONFIRMATION, "自动真仓执行前检查", signal.symbol)
    if config.live_confirm != LIVE_CONFIRMATION:
        machine.move(AutoTradeState.BLOCKED, "自动真仓确认文字不匹配", signal.symbol)
        return _decision(config, machine, "blocked", "自动真仓确认文字不匹配，未下单", signal, plan, review)
    if live_status_fn is not None:
        live_ready, live_message = live_status_fn(signal)
        if not live_ready:
            transient = any(token in live_message.lower() for token in ("429", "-1003", "限流", "网络暂不可用", "timeout"))
            machine.move(AutoTradeState.WAITING_CONFIRMATION if transient else AutoTradeState.BLOCKED, live_message, signal.symbol)
            return _decision(
                config,
                machine,
                "candidate_skipped" if transient else "blocked",
                f"{live_message}，继续检查下一候选" if transient else live_message,
                signal,
                plan,
                review,
            )
    if market_fresh_fn is not None:
        fresh, fresh_message = market_fresh_fn(signal)
        if not fresh:
            machine.move(AutoTradeState.WAITING_CONFIRMATION, fresh_message, signal.symbol)
            return _decision(
                config,
                machine,
                "candidate_skipped",
                f"{fresh_message}，继续检查下一候选",
                signal,
                plan,
                review,
            )
    if live_order_fn is None:
        machine.move(AutoTradeState.BLOCKED, "自动真仓下单通道未配置", signal.symbol)
        return _decision(config, machine, "blocked", "自动真仓下单通道未配置", signal, plan, review)

    order_side = "BUY" if signal.side == "long" else "SELL"
    try:
        result = live_order_fn(plan, order_side, review, signal)
    except Exception as exc:
        message = str(exc)
        if _is_global_execution_block(message):
            machine.move(AutoTradeState.BLOCKED, "自动真仓全局风控锁定", signal.symbol)
            return _decision(
                config,
                machine,
                "blocked",
                f"自动真仓已停止：{message}",
                signal,
                plan,
                review,
            )
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选真实下单失败，继续轮换", signal.symbol)
        return _decision(
            config,
            machine,
            "candidate_skipped",
            f"真实下单失败，继续检查下一候选：{message}",
            signal,
            plan,
            review,
        )
    if result.get("dry_run") or result.get("rejected"):
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "候选订单管理器拒绝，继续轮换", signal.symbol)
        reason = str(result.get("message") or result.get("last_error") or "订单管理器拒绝或未发送真实订单")
        return _decision(config, machine, "candidate_skipped", reason, signal, plan, review)
    if result.get("duplicate"):
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "同币活动订单已存在，继续轮换", signal.symbol)
        return _decision(
            config,
            machine,
            "candidate_skipped",
            str(result.get("message") or f"{signal.symbol} 已有同方向活动开仓单，继续检查下一候选"),
            signal,
            plan,
            review,
        )
    if result.get("uncertain") or result.get("managed_status") == "UNKNOWN":
        machine.move(AutoTradeState.BLOCKED, "订单状态不确定，禁止继续开仓", signal.symbol)
        return _decision(config, machine, "order_uncertain", "订单状态不确定，已进入对账保护", signal, plan, review)
    order_status = str(result.get("managed_status") or result.get("status") or "NEW").upper()
    position = real_position_fn(signal) if real_position_fn is not None else None
    if order_status not in {"FILLED"}:
        machine.move(AutoTradeState.WAITING_CONFIRMATION, f"订单已提交，等待成交：{order_status}", signal.symbol)
        return _decision(
            config,
            machine,
            "live_order_pending",
            f"真实订单已提交，当前状态 {order_status}，等待 Binance 成交回报",
            signal,
            plan,
            review,
            position=position,
        )
    machine.move(AutoTradeState.OPENED, "已发送真实订单", signal.symbol)
    machine.move(AutoTradeState.MANAGING, "进入真实仓位管理", signal.symbol)
    return _decision(
        config,
        machine,
        "live_order_sent",
        "已通过自动真仓检查并发送真实订单",
        signal,
        plan,
        review,
        position=position,
    )


def _decision(
    config: AutoTradeConfig,
    machine: AutoTradeStateMachine,
    action: str,
    message: str,
    signal: ScoredSignal | None,
    plan: TradePlan | None,
    review: PlanRiskReview | None = None,
    position: PositionSnapshot | None = None,
) -> AutoTradeDecision:
    decision = AutoTradeDecision(
        action=action,
        message=message,
        signal=signal,
        plan=plan,
        review=review,
        position=position,
        state=machine.state,
        state_path=machine.summary,
    )
    append_automation_event(
        config.automation_log_path,
        build_automation_event(
            state=decision.state.value,
            action=decision.action,
            message=decision.message,
            signal=signal,
            plan=plan,
            review=review,
            realized_pnl=position.realized_pnl if position else None,
            plan_followed=decision.action in {"simulated_order", "planned"},
        ),
    )
    return decision
