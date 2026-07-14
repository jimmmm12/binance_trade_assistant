from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...models import TradePlan
from ...risk_engine import PlanRiskReview
from ..state.manager import StateManager
from ..storage.database import TradingDatabase


RISK_COMPARISON_TOLERANCE_PCT = 0.005


@dataclass(frozen=True)
class RiskLimits:
    max_single_risk_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_total_exposure_multiple: float = 3.0
    max_symbol_exposure_pct: float = 40.0
    max_leverage: float = 5.0
    reduce_after_consecutive_losses: int = 3
    stop_after_consecutive_losses: int = 5
    aggressive_max_single_risk_pct: float = 2.5
    aggressive_max_total_exposure_multiple: float = 3.5
    aggressive_max_symbol_exposure_pct: float = 200.0
    aggressive_max_leverage: float = 5.0
    aggressive_reduce_after_consecutive_losses: int = 3
    aggressive_stop_after_consecutive_losses: int = 8
    aggressive_loss_streak_reduction_multiplier: float = 0.35

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "RiskLimits":
        configured = settings.get("system_risk", {})
        aggressive = settings.get("aggressive_line", {})
        return cls(
            max_single_risk_pct=float(configured.get("max_single_risk_pct", settings.get("default_risk_pct", 1.0))),
            max_daily_loss_pct=float(configured.get("max_daily_loss_pct", settings.get("daily_loss_stop_pct", 2.0))),
            max_total_exposure_multiple=float(configured.get("max_total_exposure_multiple", 3.0)),
            max_symbol_exposure_pct=float(configured.get("max_symbol_exposure_pct", 40.0)),
            max_leverage=float(configured.get("max_leverage", 5.0)),
            reduce_after_consecutive_losses=int(configured.get("reduce_after_consecutive_losses", 3)),
            stop_after_consecutive_losses=int(configured.get("stop_after_consecutive_losses", 5)),
            aggressive_max_single_risk_pct=float(aggressive.get("max_single_risk_pct", 2.5)),
            aggressive_max_total_exposure_multiple=float(aggressive.get("max_total_exposure_pct", 350.0)) / 100,
            aggressive_max_symbol_exposure_pct=float(aggressive.get("max_symbol_exposure_pct", 200.0)),
            aggressive_max_leverage=float(aggressive.get("max_leverage", 5.0)),
            aggressive_reduce_after_consecutive_losses=int(aggressive.get("loss_streak_reduce_after", 3)),
            aggressive_stop_after_consecutive_losses=int(aggressive.get("loss_streak_stop_after", 8)),
            aggressive_loss_streak_reduction_multiplier=float(
                aggressive.get("loss_streak_reduction_multiplier", 0.35)
            ),
        )


@dataclass(frozen=True)
class RiskContext:
    equity: float
    today_pnl: float = 0.0
    total_exposure: float = 0.0
    symbol_exposure: float = 0.0
    consecutive_losses: int = 0
    market_fresh: bool = True
    account_state_fresh: bool = True
    api_healthy: bool = True
    risk_data_healthy: bool = True
    emergency_stop: bool = False
    open_order_count: int = 0
    uncertain_order_count: int = 0
    risk_line: str = "conservative"


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: str
    message: str
    quantity_multiplier: float = 1.0
    warnings: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        store: TradingDatabase,
        state_manager: StateManager,
    ) -> None:
        self.limits = limits
        self.store = store
        self.state_manager = state_manager
        self._emergency_stop = False
        self._emergency_reason = ""

    def authorize_plan(
        self,
        plan: TradePlan,
        review: PlanRiskReview | None,
        context: RiskContext,
    ) -> RiskDecision:
        aggressive = context.risk_line == "aggressive"
        loss_stop = (
            self.limits.aggressive_stop_after_consecutive_losses
            if aggressive
            else self.limits.stop_after_consecutive_losses
        )
        loss_reduce = (
            self.limits.aggressive_reduce_after_consecutive_losses
            if aggressive
            else self.limits.reduce_after_consecutive_losses
        )
        loss_multiplier = (
            self.limits.aggressive_loss_streak_reduction_multiplier if aggressive else 0.5
        )
        checks = [
            (self._emergency_stop or context.emergency_stop, "emergency_stop", self._emergency_reason or "系统急停已启用"),
            (not context.api_healthy, "api_unhealthy", "Binance API 状态异常，禁止开仓"),
            (not context.risk_data_healthy, "risk_data_unavailable", "当日盈亏读取失败，禁止开仓"),
            (not context.market_fresh, "market_stale", "实时行情过期，禁止开仓"),
            (not context.account_state_fresh, "state_stale", "账户状态未与 Binance 同步，禁止开仓"),
            (context.uncertain_order_count > 0, "order_uncertain", "存在状态不确定的订单，禁止继续开仓"),
            (context.equity <= 0, "invalid_equity", "账户权益无效，禁止开仓"),
            (
                context.consecutive_losses >= loss_stop,
                "loss_streak_stop",
                f"连续亏损达到 {loss_stop} 次，停止开仓",
            ),
        ]
        for blocked, code, message in checks:
            if blocked:
                return self._reject(code, message, context)

        daily_loss_pct = max(0.0, -context.today_pnl / context.equity * 100)
        if daily_loss_pct >= self.limits.max_daily_loss_pct:
            return self._reject(
                "daily_loss_limit",
                f"今日亏损 {daily_loss_pct:.2f}% 达到限制 {self.limits.max_daily_loss_pct:.2f}%",
                context,
            )
        risk_limit = self.limits.max_single_risk_pct if not aggressive else self.limits.aggressive_max_single_risk_pct
        symbol_limit = self.limits.max_symbol_exposure_pct if not aggressive else self.limits.aggressive_max_symbol_exposure_pct
        total_limit = self.limits.max_total_exposure_multiple if not aggressive else self.limits.aggressive_max_total_exposure_multiple
        leverage_limit = self.limits.max_leverage if not aggressive else self.limits.aggressive_max_leverage
        account_risk_pct = plan.risk_amount / context.equity * 100 if context.equity > 0 else plan.risk_pct
        if account_risk_pct > risk_limit + RISK_COMPARISON_TOLERANCE_PCT:
            return self._reject(
                "single_risk_limit",
                f"账户级单笔风险 {account_risk_pct:.2f}% 超过限制 {risk_limit:.2f}%",
                context,
            )
        if plan.leverage > leverage_limit + 1e-9:
            return self._reject(
                "leverage_limit",
                f"杠杆 {plan.leverage:.1f}x 超过系统限制 {leverage_limit:.1f}x",
                context,
            )
        if review is not None and not review.live_allowed:
            return self._reject("plan_review_block", "交易计划未通过强平安全垫和质量评审", context)

        projected_total = context.total_exposure + plan.notional
        if projected_total > context.equity * total_limit:
            return self._reject("total_exposure_limit", "下单后总风险敞口超过账户限制", context)
        projected_symbol = context.symbol_exposure + plan.notional
        if projected_symbol > context.equity * symbol_limit / 100:
            return self._reject(
                "symbol_exposure_limit",
                f"下单后单币敞口超过权益的 {symbol_limit:.0f}%",
                context,
            )

        multiplier = 1.0
        warnings: list[str] = []
        if context.consecutive_losses >= loss_reduce:
            multiplier = loss_multiplier
            warnings.append(f"连续亏损保护：本单数量降低至 {loss_multiplier:.0%}")
        decision = RiskDecision(True, "allowed", "全部前置风控通过", multiplier, warnings)
        self._record(decision, context)
        self.state_manager.set_risk_status(
            {
                "status": "warning" if warnings else "normal",
                "opening_allowed": True,
                "message": decision.message,
                "warnings": warnings,
            }
        )
        return decision

    def authorize_reduce(self, context: RiskContext) -> RiskDecision:
        if not context.api_healthy:
            return self._reject("api_unhealthy", "API 不可用，无法发送平仓单", context)
        decision = RiskDecision(True, "reduce_allowed", "减仓/平仓不受开仓风控限制")
        self._record(decision, context)
        return decision

    def emergency_stop(self, reason: str) -> None:
        self._emergency_stop = True
        self._emergency_reason = reason
        self.state_manager.set_risk_status(
            {"status": "stopped", "opening_allowed": False, "message": reason}
        )
        self.store.append_risk_event("danger", "emergency_stop", reason)

    def clear_emergency_stop(self) -> None:
        self._emergency_stop = False
        self._emergency_reason = ""
        self.state_manager.set_risk_status(
            {"status": "normal", "opening_allowed": True, "message": "急停已解除"}
        )

    def _reject(self, code: str, message: str, context: RiskContext) -> RiskDecision:
        decision = RiskDecision(False, code, message, 0.0)
        self._record(decision, context)
        self.state_manager.set_risk_status(
            {"status": "blocked", "opening_allowed": False, "code": code, "message": message}
        )
        return decision

    def _record(self, decision: RiskDecision, context: RiskContext) -> None:
        self.store.append_risk_event(
            "info" if decision.allowed else "warning",
            decision.code,
            decision.message,
            {"context": context.__dict__, "quantity_multiplier": decision.quantity_multiplier},
        )
