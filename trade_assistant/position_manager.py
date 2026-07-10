from __future__ import annotations

from dataclasses import dataclass

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
) -> PositionManagementDecision:
    position = managed.position
    if position.side == "flat" or position.quantity <= 0:
        return PositionManagementDecision("ignore", "空仓无需管理")

    exit_side = "SELL" if position.side == "long" else "BUY"
    r_multiple = _r_multiple(position, managed.stop_price)
    status = managed.status or ""

    if _stop_reached(position, managed.stop_price):
        return PositionManagementDecision(
            "close",
            "价格触发止损，自动退出该仓位",
            position.quantity,
            exit_side,
            status="止损退出",
            severity="danger",
        )

    liquidation_buffer = _liquidation_buffer_pct(position)
    if liquidation_buffer is not None:
        if liquidation_buffer <= 2:
            return PositionManagementDecision(
                "close",
                f"强平安全垫仅 {liquidation_buffer:.2f}%，自动退出",
                position.quantity,
                exit_side,
                status="强平安全垫不足退出",
                severity="danger",
            )
        if liquidation_buffer <= 3.5:
            return PositionManagementDecision(
                "reduce",
                f"强平安全垫偏低 {liquidation_buffer:.2f}%，自动减仓 50%",
                _partial_quantity(position.quantity, 0.5),
                exit_side,
                status="强平安全垫减仓",
                severity="danger",
            )

    if opposite_signal is not None and opposite_signal.score >= 72:
        return PositionManagementDecision(
            "reduce",
            f"出现反向高分信号 {opposite_signal.score}，自动减仓 50%",
            _partial_quantity(position.quantity, 0.5),
            exit_side,
            status="反向信号减仓",
            severity="warning",
        )

    if same_side_signal is not None:
        if same_side_signal.score >= 82 and position.unrealized_pnl >= 0:
            return PositionManagementDecision(
                "add",
                f"同向信号仍强 {same_side_signal.score} 且仓位未浮亏，可自动小幅加仓",
                0.0,
                exit_side,
                status="同向强势加仓候选",
                severity="warning",
            )
        if same_side_signal.score < 58 and position.unrealized_pnl <= 0:
            return PositionManagementDecision(
                "reduce",
                f"同向信号转弱 {same_side_signal.score} 且浮亏，自动减仓 50%",
                _partial_quantity(position.quantity, 0.5),
                exit_side,
                status="弱信号减仓",
                severity="warning",
            )
        if any("ATR波动过大" in item or "禁止真仓" in item for item in same_side_signal.warnings):
            return PositionManagementDecision(
                "reduce",
                "波动异常放大，自动减仓 30%",
                _partial_quantity(position.quantity, 0.3),
                exit_side,
                status="波动放大减仓",
                severity="warning",
            )

    if r_multiple >= 2 and "2R尾仓" not in status:
        return PositionManagementDecision(
            "reduce",
            "达到 2R，自动减仓至尾仓并启用移动止损",
            _partial_quantity(position.quantity, 0.4),
            exit_side,
            new_stop=_break_even_stop(position),
            status=_append_status(status, "2R尾仓"),
            severity="warning",
        )
    if r_multiple >= 1.5 and "1.5R减仓" not in status:
        return PositionManagementDecision(
            "reduce",
            "达到 1.5R，自动减仓 30%",
            _partial_quantity(position.quantity, 0.3),
            exit_side,
            new_stop=_break_even_stop(position),
            status=_append_status(status, "1.5R减仓"),
            severity="warning",
        )
    if r_multiple >= 1 and "1R保本" not in status:
        return PositionManagementDecision(
            "move_stop",
            "达到 1R，止损移动到成本价",
            0.0,
            exit_side,
            new_stop=_break_even_stop(position),
            status=_append_status(status, "1R保本"),
            severity="warning",
        )

    return PositionManagementDecision("hold", "仓位正常，继续持有", status=status)


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


def _partial_quantity(quantity: float, ratio: float) -> float:
    return round(max(0.0, quantity * ratio), 8)


def _break_even_stop(position: PositionSnapshot) -> float:
    return round(position.entry_price, 8)


def _append_status(status: str, marker: str) -> str:
    if not status:
        return marker
    if marker in status:
        return status
    return f"{status}/{marker}"
