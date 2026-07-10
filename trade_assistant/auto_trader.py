from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .automation_log import append_automation_event, build_automation_event
from .automation_state import AutoTradeState, AutoTradeStateMachine
from .gui.services import auto_plan_prices, evaluate_plan_from_form, simulate_order_from_form
from .models import PositionSnapshot, ScoredSignal, TradePlan
from .portfolio import SimulatedPortfolio
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
    real_position_fn: Callable[[ScoredSignal], PositionSnapshot | None] | None = None,
) -> AutoTradeDecision:
    machine = AutoTradeStateMachine()
    execution_mode = _execution_mode(config)
    longs, shorts = scan_fn()
    signal = select_candidate(longs, shorts)
    if signal is None:
        return _decision(config, machine, "no_signal", "本轮没有可用信号", None, None)
    machine.move(AutoTradeState.OPPORTUNITY_FOUND, "扫描发现候选信号", signal.symbol)
    if signal.breakdown.action_level == "block_live" and execution_mode != AUTO_EXECUTION_SIMULATE:
        machine.move(AutoTradeState.BLOCKED, "机会评分禁止真仓，且未启用自动模拟", signal.symbol)
        return _decision(config, machine, "blocked", "机会评分禁止真仓，本轮只观察", signal, None)
    if signal.market == "spot" and signal.side == "short" and execution_mode != AUTO_EXECUTION_PLAN:
        machine.move(AutoTradeState.BLOCKED, "现货不允许自动模拟做空", signal.symbol)
        return _decision(config, machine, "blocked", "现货做空信号只观察，不自动卖出", signal, None)
    portfolio = SimulatedPortfolio(config.portfolio_path) if config.portfolio_path else SimulatedPortfolio()
    guard = daily_loss_guard(
        equity=config.equity,
        realized_pnl=portfolio.today_realized_pnl(),
        stop_pct=config.max_daily_loss_pct,
    )
    if not guard.live_allowed:
        machine.move(AutoTradeState.BLOCKED, guard.message, signal.symbol)
        return _decision(config, machine, "blocked", guard.message, signal, None)
    existing = _existing_position_for_mode(execution_mode, signal, portfolio, real_position_fn)
    if existing.side != "flat" and existing.quantity > 0:
        machine.move(AutoTradeState.MANAGING, "已有仓位，进入持仓管理，不重复开仓", signal.symbol)
        return _decision(
            config,
            machine,
            "manage_position",
            "已有仓位，本轮不重复下单，进入持仓管理",
            signal,
            None,
            position=existing,
        )
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
        equity=str(config.equity),
        risk_pct=str(prices.adaptive.risk_pct if prices.adaptive else 1.0),
        leverage=str(prices.adaptive.suggested_leverage if prices.adaptive else 1.0),
        signal=signal,
        position=None,
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
    machine.move(AutoTradeState.OPENED, "已模拟开仓", signal.symbol)
    machine.move(AutoTradeState.MANAGING, "进入持仓管理", signal.symbol)
    return _decision(config, machine, "simulated_order", "已自动生成计划并模拟下单", signal, plan, review, position)


def _execution_mode(config: AutoTradeConfig) -> str:
    if config.execution_mode is None:
        return AUTO_EXECUTION_SIMULATE if config.auto_simulate else AUTO_EXECUTION_PLAN
    if config.execution_mode not in AUTO_EXECUTION_MODES:
        raise ValueError("execution_mode must be plan, simulate, or live")
    return config.execution_mode


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
