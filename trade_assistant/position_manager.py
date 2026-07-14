from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .automation_policy import (
    atr_price_distance,
    automation_sizing_config,
    holding_hours,
    lifecycle_state,
    next_add_stage_pct,
)
from .models import PositionSnapshot, ScoredSignal


@dataclass(frozen=True)
class ManagedPosition:
    position: PositionSnapshot
    stop_price: float = 0.0
    target_price: float = 0.0
    status: str = ""


@dataclass(frozen=True)
class PositionManagementDecision:
    action: str
    message: str
    quantity: float = 0.0
    exit_side: str = ""
    new_stop: float | None = None
    status: str = ""
    severity: str = "normal"


def manage_position(
    managed: ManagedPosition,
    *,
    same_side_signal: ScoredSignal | None = None,
    opposite_signal: ScoredSignal | None = None,
    mode: str = "intraday",
    settings: dict[str, Any] | None = None,
) -> PositionManagementDecision:
    config = automation_sizing_config(settings)
    position = managed.position
    if position.side == "flat" or position.quantity <= 0:
        return PositionManagementDecision("ignore", "空仓无需管理")

    exit_side = "SELL" if position.side == "long" else "BUY"
    r_multiple = _r_multiple(position, managed.stop_price)
    status = managed.status or ""
    trade_state = lifecycle_state(status, r_multiple, position.quantity)

    if _stop_reached(position, managed.stop_price):
        if _can_wait_for_stop_confirmation(status, same_side_signal, opposite_signal, settings):
            return PositionManagementDecision(
                "hold",
                f"{trade_state}：首次刺穿止损，但同向K线结构仍健康，进入一次止损确认观察",
                status=_append_status(status, "止损确认观察"),
                severity="warning",
            )
        return PositionManagementDecision(
            "close",
            f"{trade_state}：价格触发止损，自动退出该仓位",
            position.quantity,
            exit_side,
            status="止损退出",
            severity="danger",
        )

    margin_drawdown = _margin_drawdown_pct(position)
    if margin_drawdown is not None:
        if margin_drawdown >= config.max_margin_drawdown_close_pct:
            return PositionManagementDecision(
                "close",
                f"{trade_state}：保证金回撤 {margin_drawdown:.1f}% 超过硬退出线，自动退出",
                position.quantity,
                exit_side,
                status="保证金回撤退出",
                severity="danger",
            )
        if margin_drawdown >= config.max_margin_drawdown_reduce_pct and "保证金回撤减仓" not in status:
            return PositionManagementDecision(
                "reduce",
                f"{trade_state}：保证金回撤 {margin_drawdown:.1f}% 偏大，先降低 {config.risk_reduce_pct:.0%} 风险暴露",
                _partial_quantity(position.quantity, config.risk_reduce_pct),
                exit_side,
                status=_append_status(status, "保证金回撤减仓"),
                severity="danger",
            )

    if position.leverage > config.max_position_leverage and "杠杆过高减仓" not in status:
        return PositionManagementDecision(
            "reduce",
            f"{trade_state}：实际杠杆 {position.leverage:.1f}x 超过系统上限 {config.max_position_leverage:.1f}x，先降低 {config.risk_reduce_pct:.0%} 风险暴露",
            _partial_quantity(position.quantity, config.risk_reduce_pct),
            exit_side,
            status=_append_status(status, "杠杆过高减仓"),
            severity="danger",
        )

    liquidation_buffer = _liquidation_buffer_pct(position)
    if liquidation_buffer is not None:
        if liquidation_buffer <= 2:
            return PositionManagementDecision(
                "close",
                f"{trade_state}：强平安全垫仅 {liquidation_buffer:.2f}%，自动退出",
                position.quantity,
                exit_side,
                status="强平安全垫不足退出",
                severity="danger",
            )
        if liquidation_buffer <= 3.5:
            return PositionManagementDecision(
                "reduce",
                f"{trade_state}：强平安全垫偏低 {liquidation_buffer:.2f}%，自动减仓 {config.risk_reduce_pct:.0%}",
                _partial_quantity(position.quantity, config.risk_reduce_pct),
                exit_side,
                status="强平安全垫减仓",
                severity="danger",
            )

    if opposite_signal is not None and opposite_signal.breakdown.action_level in {"tradeable", "small_trade"}:
        return PositionManagementDecision(
            "reduce",
            f"{trade_state}：出现反向高分信号 {opposite_signal.score}，先降低 {config.risk_reduce_pct:.0%} 风险暴露",
            _partial_quantity(position.quantity, config.risk_reduce_pct),
            exit_side,
            status=_append_status(status, "反向信号减仓"),
            severity="warning",
        )

    if r_multiple >= 1 and "1R保本" not in status:
        return PositionManagementDecision(
            "move_stop",
            f"{trade_state}：达到 1R，止损移动到成本价",
            0.0,
            exit_side,
            new_stop=_best_profit_stop(position, same_side_signal, mode, config.trailing_atr_multiplier),
            status=_append_status(status, "1R保本"),
            severity="warning",
        )

    for target_r, reduce_pct, marker in sorted(config.profit_take_rules, key=lambda item: item[0], reverse=True):
        if r_multiple >= target_r and marker not in status:
            return PositionManagementDecision(
                "reduce",
                f"{trade_state}：达到 {target_r:g}R，自动减仓 {reduce_pct:.0%}，剩余仓位移动止损跟踪",
                _partial_quantity(position.quantity, reduce_pct),
                exit_side,
                new_stop=_best_profit_stop(position, same_side_signal, mode, config.trailing_atr_multiplier),
                status=_append_status(status, marker),
                severity="warning",
            )

    if same_side_signal is not None:
        signal_score = same_side_signal.score
        new_trailing_stop = _trailing_stop(position, same_side_signal, mode, config.trailing_atr_multiplier)
        add_allowed = same_side_signal.breakdown.add_allowed or (
            same_side_signal.breakdown.grade == "未分级"
            and signal_score >= config.min_add_score
            and same_side_signal.breakdown.action_level in {"tradeable", "small_trade"}
        )
        add_stage_pct = next_add_stage_pct(status, config)
        if (
            add_allowed
            and signal_score >= config.min_add_score
            and add_stage_pct > 0
            and r_multiple >= config.min_profit_r_for_add
            and position.unrealized_pnl > 0
        ):
            return PositionManagementDecision(
                "add",
                f"{trade_state}：已有浮盈 {r_multiple:.2f}R，同向评分 {signal_score}，允许顺势加仓 {add_stage_pct:.0%}",
                add_stage_pct,
                exit_side,
                new_stop=new_trailing_stop,
                status=_append_status(status, f"加仓{status.count('加仓') + 1}"),
                severity="warning",
            )
        if (
            config.allow_loss_add
            and position.unrealized_pnl < 0
            and r_multiple >= config.max_loss_add_r
            and signal_score >= config.loss_add_min_score
            and same_side_signal.breakdown.action_level == "tradeable"
            and add_stage_pct > 0
        ):
            return PositionManagementDecision(
                "add",
                f"{trade_state}：浮亏未超过 {abs(config.max_loss_add_r):.2f}R 且评分 {signal_score} 极强，允许严格小比例补仓",
                min(add_stage_pct, config.add_order_pct_of_initial),
                exit_side,
                new_stop=new_trailing_stop,
                status=_append_status(status, f"严格补仓{status.count('加仓') + 1}"),
                severity="warning",
            )
        reduce_recommended = same_side_signal.breakdown.reduce_recommended or (
            same_side_signal.breakdown.grade == "未分级" and signal_score < config.reduce_score_threshold
        )
        if reduce_recommended and "评分下降减仓" not in status:
            return PositionManagementDecision(
                "reduce",
                f"{trade_state}：同向评分降至 {signal_score}，降低 {config.risk_reduce_pct:.0%} 风险暴露",
                _partial_quantity(position.quantity, config.risk_reduce_pct),
                exit_side,
                new_stop=new_trailing_stop,
                status=_append_status(status, "评分下降减仓"),
                severity="warning",
            )
        if any("ATR波动过大" in item or "禁止真仓" in item for item in same_side_signal.warnings):
            return PositionManagementDecision(
                "reduce",
                f"{trade_state}：波动异常放大，先降低 {config.risk_reduce_pct:.0%} 风险暴露",
                _partial_quantity(position.quantity, config.risk_reduce_pct),
                exit_side,
                new_stop=new_trailing_stop,
                status=_append_status(status, "波动放大减仓"),
                severity="warning",
            )
        if r_multiple >= 1 and new_trailing_stop is not None and _is_better_stop(position, new_trailing_stop, managed.stop_price):
            return PositionManagementDecision(
                "move_stop",
                f"{trade_state}：趋势盈利中，按 ATR 移动止损保护利润",
                0.0,
                exit_side,
                new_stop=new_trailing_stop,
                status=_append_status(status, "移动止损"),
                severity="warning",
            )

    if holding_hours(position) >= config.time_stop_hours and r_multiple < config.time_stop_min_r:
        weak_or_missing = same_side_signal is None or same_side_signal.score < config.time_stop_min_score
        if weak_or_missing:
            return PositionManagementDecision(
                "close",
                f"{trade_state}：持仓超过 {config.time_stop_hours:.0f} 小时仍未达到 {config.time_stop_min_r:.1f}R，时间止损退出",
                position.quantity,
                exit_side,
                status="时间止损退出",
                severity="warning",
            )

    return PositionManagementDecision("hold", f"{trade_state}：仓位正常，继续持有", status=status)


def _r_multiple(position: PositionSnapshot, stop_price: float) -> float:
    risk = abs(position.entry_price - stop_price)
    if risk <= 0:
        return 0.0
    direction = -1 if position.side == "short" else 1
    return round((position.mark_price - position.entry_price) * direction / risk, 4)


def _stop_reached(position: PositionSnapshot, stop_price: float) -> bool:
    if stop_price <= 0:
        return False
    return position.mark_price >= stop_price if position.side == "short" else position.mark_price <= stop_price


def _liquidation_buffer_pct(position: PositionSnapshot) -> float | None:
    if not position.liquidation_price or position.liquidation_price <= 0 or position.mark_price <= 0:
        return None
    if position.side == "short":
        buffer = position.liquidation_price - position.mark_price
    else:
        buffer = position.mark_price - position.liquidation_price
    return max(0.0, buffer / position.mark_price * 100)


def _margin_drawdown_pct(position: PositionSnapshot) -> float | None:
    if position.unrealized_pnl >= 0 or position.notional <= 0 or position.leverage <= 0:
        return None
    margin = abs(position.notional) / position.leverage
    if margin <= 0:
        return None
    return max(0.0, -position.unrealized_pnl / margin * 100)


def _partial_quantity(quantity: float, ratio: float) -> float:
    return round(max(0.0, quantity * ratio), 8)


def _can_wait_for_stop_confirmation(
    status: str,
    same_side_signal: ScoredSignal | None,
    opposite_signal: ScoredSignal | None,
    settings: dict[str, Any] | None,
) -> bool:
    source = (settings or {}).get("automation_positioning", {})
    if not bool(source.get("liquidity_sweep_protection", True)) or "止损确认观察" in status:
        return False
    if same_side_signal is None or opposite_signal is not None:
        return False
    minimum = int(source.get("stop_confirmation_min_score", 70))
    if same_side_signal.score < minimum or same_side_signal.breakdown.action_level not in {"tradeable", "small_trade"}:
        return False
    danger_terms = ("ATR波动", "多周期方向冲突", "主动成交方向与信号背离", "禁止真仓")
    return not any(any(term in warning for term in danger_terms) for warning in same_side_signal.warnings)


def _break_even_stop(position: PositionSnapshot) -> float:
    return round(position.entry_price, 8)


def _trailing_stop(
    position: PositionSnapshot,
    signal: ScoredSignal | None,
    mode: str,
    multiplier: float,
) -> float | None:
    distance = atr_price_distance(signal, position.mark_price, mode, multiplier)
    if distance <= 0:
        return _break_even_stop(position)
    if position.side == "short":
        return round(min(position.entry_price, position.mark_price + distance), 8)
    return round(max(position.entry_price, position.mark_price - distance), 8)


def _best_profit_stop(
    position: PositionSnapshot,
    signal: ScoredSignal | None,
    mode: str,
    multiplier: float,
) -> float:
    stop = _trailing_stop(position, signal, mode, multiplier)
    return stop if stop is not None else _break_even_stop(position)


def _is_better_stop(position: PositionSnapshot, proposed: float, current: float) -> bool:
    if current <= 0:
        return True
    if position.side == "short":
        return proposed < current
    return proposed > current


def _append_status(status: str, marker: str) -> str:
    if not status:
        return marker
    if marker in status:
        return status
    return f"{status}/{marker}"
