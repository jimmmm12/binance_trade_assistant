from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorTarget:
    market: str
    symbol: str
    side: str
    quantity: float
    entry: float
    stop: float
    target: float
    liquidation_price: float | None = None


@dataclass(frozen=True)
class MonitorResult:
    target: MonitorTarget
    price: float
    unrealized_pnl: float
    r_multiple: float
    alerts: list[str]
    severity: str

    @property
    def alert_text(self) -> str:
        return "；".join(self.alerts) if self.alerts else "正常监控"


def evaluate_monitor_target(target: MonitorTarget, price: float) -> MonitorResult:
    side_multiplier = -1 if target.side == "short" else 1
    unrealized_pnl = round((price - target.entry) * target.quantity * side_multiplier, 8)
    risk_distance = abs(target.entry - target.stop)
    r_multiple = 0.0
    if risk_distance > 0:
        r_multiple = round((price - target.entry) * side_multiplier / risk_distance, 4)

    alerts: list[str] = []
    severity = "normal"
    if _stop_reached(target, price):
        alerts.append("触发止损，立即处理")
        severity = "danger"
    if _target_reached(target, price):
        alerts.append("触及目标价，考虑止盈")
        severity = _max_severity(severity, "warning")
    if r_multiple >= 1:
        alerts.append("到 1R，止损移动到成本价")
        severity = _max_severity(severity, "warning")
    if r_multiple >= 1.5:
        alerts.append("到 1.5R，减仓 30%")
        severity = _max_severity(severity, "warning")
    if r_multiple >= 2:
        alerts.append("到 2R，保留尾仓")
        severity = _max_severity(severity, "warning")

    liquidation_buffer = _liquidation_buffer_pct(target, price)
    if liquidation_buffer is not None:
        if liquidation_buffer <= 3.1:
            alerts.append(f"接近强平，安全垫 {liquidation_buffer:.2f}%")
            severity = "danger"
        elif liquidation_buffer <= 5:
            alerts.append(f"强平安全垫偏低，剩余 {liquidation_buffer:.2f}%")
            severity = _max_severity(severity, "warning")

    return MonitorResult(
        target=target,
        price=price,
        unrealized_pnl=unrealized_pnl,
        r_multiple=r_multiple,
        alerts=alerts,
        severity=severity,
    )


def _stop_reached(target: MonitorTarget, price: float) -> bool:
    if target.stop <= 0:
        return False
    return price >= target.stop if target.side == "short" else price <= target.stop


def _target_reached(target: MonitorTarget, price: float) -> bool:
    if target.target <= 0:
        return False
    return price <= target.target if target.side == "short" else price >= target.target


def _liquidation_buffer_pct(target: MonitorTarget, price: float) -> float | None:
    if not target.liquidation_price or target.liquidation_price <= 0 or price <= 0:
        return None
    if target.side == "short":
        buffer = target.liquidation_price - price
    else:
        buffer = price - target.liquidation_price
    return max(0.0, buffer / price * 100)


def _max_severity(left: str, right: str) -> str:
    order = {"normal": 0, "warning": 1, "danger": 2}
    return left if order[left] >= order[right] else right
