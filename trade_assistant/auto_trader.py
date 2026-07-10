from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .automation_log import append_automation_event, build_automation_event
from .automation_state import AutoTradeState, AutoTradeStateMachine
from .gui.services import auto_plan_prices, evaluate_plan_from_form, simulate_order_from_form
from .models import PositionSnapshot, ScoredSignal, TradePlan
from .portfolio import SimulatedPortfolio
from .position_manager import ManagedPosition, PositionManagementDecision, manage_position
from .risk_engine import PlanRiskReview, daily_loss_guard


AUTO_EXECUTION_PLAN = "plan"
AUTO_EXECUTION_SIMULATE = "simulate"
AUTO_EXECUTION_LIVE = "live"
AUTO_EXECUTION_MODES = {AUTO_EXECUTION_PLAN, AUTO_EXECUTION_SIMULATE, AUTO_EXECUTION_LIVE}


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


def select_candidate(longs: list[ScoredSignal], shorts: list[ScoredSignal]) -> ScoredSignal | None:
    candidates = [*longs, *shorts]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.score, item.quote_volume_m), reverse=True)[0]


def run_auto_cycle(
    config: AutoTradeConfig,
    *,
    scan_fn: Callable[[], tuple[list[ScoredSignal], list[ScoredSignal]]],
    market_fresh_fn: Callable[[ScoredSignal], tuple[bool, str]] | None = None,
    live_status_fn: Callable[[], tuple[bool, str]] | None = None,
    live_order_fn: Callable[[TradePlan, str], dict] | None = None,
    position_order_fn: Callable[[ManagedPosition, PositionManagementDecision], dict] | None = None,
    managed_positions_fn: Callable[[], list[ManagedPosition]] | None = None,
    account_equity_fn: Callable[[], float] | None = None,
    real_position_fn: Callable[[ScoredSignal], PositionSnapshot | None] | None = None,
) -> AutoTradeDecision:
    machine = AutoTradeStateMachine()
    execution_mode = _execution_mode(config)
    equity = _cycle_equity(config, account_equity_fn)
    longs, shorts = scan_fn()
    candidates = sorted([*longs, *shorts], key=lambda item: (item.score, item.quote_volume_m), reverse=True)
    portfolio = SimulatedPortfolio(config.portfolio_path) if config.portfolio_path else SimulatedPortfolio()
    guard = daily_loss_guard(
        equity=equity,
        realized_pnl=portfolio.today_realized_pnl(),
        stop_pct=config.max_daily_loss_pct,
    )
    if not guard.live_allowed:
        machine.move(AutoTradeState.BLOCKED, guard.message)
        return _decision(config, machine, "blocked", guard.message, None, None)

    managed_positions = managed_positions_fn() if managed_positions_fn is not None else _simulated_managed_positions(portfolio)
    management = _best_position_management(managed_positions, candidates)
    if management is not None:
        managed, position_decision, signal_for_position = management
        if position_decision.action in {"close", "reduce", "move_stop"}:
            return _execute_position_management(
                config,
                machine,
                portfolio,
                managed,
                position_decision,
                signal_for_position,
                execution_mode,
                position_order_fn,
            )
        if position_decision.action == "add" and signal_for_position is not None:
            signal = signal_for_position
        else:
            signal = _first_new_opportunity(candidates, managed_positions)
    else:
        signal = _first_new_opportunity(candidates, managed_positions)

    if signal is None:
        return _decision(config, machine, "no_signal", "本轮没有可用信号", None, None)
    machine.move(AutoTradeState.OPPORTUNITY_FOUND, "扫描发现候选信号", signal.symbol)
    if signal.breakdown.action_level == "block_live" and execution_mode != AUTO_EXECUTION_SIMULATE:
        machine.move(AutoTradeState.BLOCKED, "机会评分禁止真仓，且未启用自动模拟", signal.symbol)
        return _decision(config, machine, "blocked", "机会评分禁止真仓，本轮只观察", signal, None)
    if signal.market == "spot" and signal.side == "short" and execution_mode != AUTO_EXECUTION_PLAN:
        machine.move(AutoTradeState.BLOCKED, "现货不允许自动模拟做空", signal.symbol)
        return _decision(config, machine, "blocked", "现货做空信号只观察，不自动卖出", signal, None)
    existing = _existing_position_for_mode(execution_mode, signal, portfolio, real_position_fn)
    prices = auto_plan_prices(signal, config.mode)
    if prices.adaptive is not None and not prices.adaptive.allow_live and execution_mode != AUTO_EXECUTION_SIMULATE:
        machine.move(AutoTradeState.BLOCKED, "自适应参数只建议模拟", signal.symbol)
        return _decision(config, machine, "blocked", "自适应参数只建议模拟，未启用自动模拟", signal, None)
    plan, review = evaluate_plan_from_form(
        symbol=signal.symbol,
        market=signal.market,
        side=signal.side,
        entry=str(prices.entry),
        stop=str(prices.stop),
        target=str(prices.target),
        equity=str(equity),
        risk_pct=str(prices.adaptive.risk_pct if prices.adaptive else 1.0),
        leverage=str(prices.adaptive.suggested_leverage if prices.adaptive else 1.0),
        signal=signal,
        position=existing if existing.side != "flat" else None,
        mode=config.mode,
    )
    machine.move(AutoTradeState.PLAN_GENERATED, "已生成交易计划", signal.symbol)
    if not review.live_allowed and execution_mode == AUTO_EXECUTION_LIVE:
        machine.move(AutoTradeState.BLOCKED, "风控评审不允许自动执行", signal.symbol)
        return _decision(config, machine, "blocked", "风控评审不允许自动执行", signal, plan, review)
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
    if review.recommended_action in {"禁止真仓", "只观察"} and signal.breakdown.action_level == "block_live":
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "低分计划只允许模拟观察", signal.symbol)
    order_side = "BUY" if signal.side == "long" else "SELL"
    _, position = simulate_order_from_form(
        market=signal.market,
        symbol=signal.symbol,
        side=order_side,
        quantity=f"{plan.quantity:.8f}",
        order_type="LIMIT",
        price=f"{plan.entry:.8f}",
        fallback_price=f"{plan.entry:.8f}",
        portfolio_path=config.portfolio_path,
    )
    machine.move(AutoTradeState.OPENED, "已模拟加仓" if existing.side != "flat" else "已模拟开仓", signal.symbol)
    machine.move(AutoTradeState.MANAGING, "进入持仓管理", signal.symbol)
    action = "simulated_add" if existing.side != "flat" else "simulated_order"
    message = "已自动评估仓位并模拟加仓" if existing.side != "flat" else "已自动生成计划并模拟下单"
    return _decision(config, machine, action, message, signal, plan, review, position)


def _execution_mode(config: AutoTradeConfig) -> str:
    if config.execution_mode is None:
        return AUTO_EXECUTION_SIMULATE if config.auto_simulate else AUTO_EXECUTION_PLAN
    if config.execution_mode not in AUTO_EXECUTION_MODES:
        raise ValueError("execution_mode must be plan, simulate, or live")
    return config.execution_mode


def _cycle_equity(config: AutoTradeConfig, account_equity_fn: Callable[[], float] | None) -> float:
    if not config.auto_detect_account or account_equity_fn is None:
        return config.equity
    try:
        equity = float(account_equity_fn())
    except Exception:
        return config.equity
    return equity if equity > 0 else config.equity


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
) -> tuple[ManagedPosition, PositionManagementDecision, ScoredSignal | None] | None:
    priority = {"close": 0, "reduce": 1, "move_stop": 2, "add": 3, "hold": 4, "ignore": 5}
    decisions: list[tuple[int, ManagedPosition, PositionManagementDecision, ScoredSignal | None]] = []
    for managed in managed_positions:
        same = _matching_signal(candidates, managed.position, same_side=True)
        opposite = _matching_signal(candidates, managed.position, same_side=False)
        decision = manage_position(managed, same_side_signal=same, opposite_signal=opposite)
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
) -> ScoredSignal | None:
    held = {(item.position.market, item.position.symbol) for item in managed_positions if item.position.side != "flat"}
    for candidate in candidates:
        if (candidate.market, candidate.symbol) not in held:
            return candidate
    return None


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
    result = position_order_fn(managed, position_decision)
    if result.get("dry_run"):
        machine.move(AutoTradeState.BLOCKED, "自动真仓仓位管理未真正发送订单", position.symbol)
        return _decision(config, machine, "blocked", "自动真仓仓位管理未真正发送订单", signal, None, position=position)
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
        realized_pnl=position.realized_pnl,
        status=status,
    )


def _run_live_order(
    config: AutoTradeConfig,
    machine: AutoTradeStateMachine,
    signal: ScoredSignal,
    plan: TradePlan,
    review: PlanRiskReview,
    *,
    market_fresh_fn: Callable[[ScoredSignal], tuple[bool, str]] | None,
    live_status_fn: Callable[[], tuple[bool, str]] | None,
    live_order_fn: Callable[[TradePlan, str], dict] | None,
    real_position_fn: Callable[[ScoredSignal], PositionSnapshot | None] | None,
) -> AutoTradeDecision:
    from .broker import LIVE_CONFIRMATION

    machine.move(AutoTradeState.WAITING_CONFIRMATION, "自动真仓执行前检查", signal.symbol)
    if config.live_confirm != LIVE_CONFIRMATION:
        machine.move(AutoTradeState.BLOCKED, "自动真仓确认文字不匹配", signal.symbol)
        return _decision(config, machine, "blocked", "自动真仓确认文字不匹配，未下单", signal, plan, review)
    if live_status_fn is not None:
        live_ready, live_message = live_status_fn()
        if not live_ready:
            machine.move(AutoTradeState.BLOCKED, live_message, signal.symbol)
            return _decision(config, machine, "blocked", live_message, signal, plan, review)
    if market_fresh_fn is not None:
        fresh, fresh_message = market_fresh_fn(signal)
        if not fresh:
            machine.move(AutoTradeState.BLOCKED, fresh_message, signal.symbol)
            return _decision(config, machine, "blocked", fresh_message, signal, plan, review)
    if live_order_fn is None:
        machine.move(AutoTradeState.BLOCKED, "自动真仓下单通道未配置", signal.symbol)
        return _decision(config, machine, "blocked", "自动真仓下单通道未配置", signal, plan, review)

    order_side = "BUY" if signal.side == "long" else "SELL"
    live_order_fn(plan, order_side)
    position = real_position_fn(signal) if real_position_fn is not None else None
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
