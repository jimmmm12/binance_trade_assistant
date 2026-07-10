from __future__ import annotations

from dataclasses import dataclass

from .models import PositionSnapshot, Signal, TradePlan


MAINTENANCE_BUFFER_PCT = 0.5


@dataclass(frozen=True)
class PlanRiskReview:
    liquidation_price: float | None
    liquidation_buffer_pct: float | None
    liquidation_status: str
    liquidation_source: str
    suggested_leverage: float
    quality_score: int
    recommended_action: str
    live_allowed: bool
    warnings: list[str]
    reasons: list[str]
    management_rules: list[str]


@dataclass(frozen=True)
class DailyLossGuard:
    status: str
    loss_pct: float
    live_allowed: bool
    message: str


def estimate_liquidation_price(side: str, entry: float, leverage: float) -> float | None:
    if entry <= 0 or leverage <= 0:
        return None
    move_pct = max(0.0, 100 / leverage - MAINTENANCE_BUFFER_PCT) / 100
    if side == "short":
        return round(entry * (1 + move_pct), 8)
    return round(entry * (1 - move_pct), 8)


def suggest_leverage(stop_pct: float, mode: str) -> float:
    if stop_pct <= 0:
        return 1.0
    target_loss_at_stop = 8.0 if mode == "intraday" else 6.0
    raw = target_loss_at_stop / stop_pct
    if stop_pct >= (6.0 if mode == "intraday" else 9.0):
        raw = min(raw, 1.0)
    return round(max(1.0, min(5.0, raw)), 1)


def evaluate_plan_risk(
    plan: TradePlan,
    signal: Signal | None,
    position: PositionSnapshot | None,
    mode: str,
    min_live_score: int = 70,
) -> PlanRiskReview:
    warnings: list[str] = []
    reasons: list[str] = []
    score = 100
    suggested = suggest_leverage(plan.loss_pct_to_stop, mode)
    liquidation_price = None
    liquidation_buffer_pct = None
    liquidation_status = "安全"

    if plan.market == "futures":
        liquidation_price, liquidation_source = _liquidation_price_for_review(plan, position)
        if liquidation_price is None:
            liquidation_status = "不建议下单"
            warnings.append("无法估算强平价")
            score -= 40
        else:
            liquidation_buffer_pct = _liquidation_buffer_pct(plan, liquidation_price)
            if liquidation_buffer_pct < plan.loss_pct_to_stop * 0.75:
                liquidation_status = "不建议下单"
                warnings.append("强平安全垫不足")
                score -= 45
            elif liquidation_buffer_pct < plan.loss_pct_to_stop * 1.5:
                liquidation_status = "偏危险"
                warnings.append("强平价离止损较近，建议降低杠杆")
                score -= 20
            else:
                reasons.append("强平价与止损之间有安全垫")
        if plan.leverage > suggested:
            warnings.append(f"建议杠杆不超过 {suggested:.1f}x")
            score -= 10

    if signal is not None:
        score += _score_signal_quality(signal, warnings, reasons, mode)

    if position is not None and position.side == plan.side and position.notional > plan.equity * 1.5:
        warnings.append("当前同向仓位偏重，不建议继续加仓")
        score -= 25

    if plan.loss_pct_to_stop > (6.0 if mode == "intraday" else 14.0):
        warnings.append("ATR止损距离过大，只建议模拟观察")
        score -= 25

    score = max(0, min(100, score))
    hard_signal_block = signal is not None and (
        (signal.atr_pct is not None and signal.atr_pct >= (6.0 if mode == "intraday" else 14.0))
        or (signal.funding_pct is not None and abs(signal.funding_pct) >= 0.12)
    )
    live_allowed = liquidation_status != "不建议下单" and score >= min_live_score and not hard_signal_block
    if hard_signal_block:
        recommended_action = "禁止真仓"
        warnings.append("信号存在硬风险，只允许模拟或观察")
    elif not live_allowed and score < 55:
        recommended_action = "只观察"
    elif not live_allowed:
        recommended_action = "只建议模拟"
    elif score < 82 or liquidation_status == "偏危险":
        recommended_action = "谨慎小仓"
    else:
        recommended_action = "可按计划执行"

    return PlanRiskReview(
        liquidation_price=liquidation_price,
        liquidation_buffer_pct=None if liquidation_buffer_pct is None else round(liquidation_buffer_pct, 2),
        liquidation_status=liquidation_status,
        liquidation_source=liquidation_source if plan.market == "futures" else "不适用",
        suggested_leverage=suggested,
        quality_score=score,
        recommended_action=recommended_action,
        live_allowed=live_allowed,
        warnings=warnings,
        reasons=reasons,
        management_rules=management_rules(plan),
    )


def daily_loss_guard(
    equity: float,
    realized_pnl: float,
    unrealized_pnl: float = 0.0,
    stop_pct: float = 2.0,
    warning_pct: float = 1.5,
) -> DailyLossGuard:
    if equity <= 0:
        return DailyLossGuard("停止交易", 100.0, False, "本金无效，禁止真下单")
    loss = max(0.0, -(realized_pnl + min(0.0, unrealized_pnl)))
    loss_pct = loss / equity * 100
    if loss_pct >= stop_pct:
        return DailyLossGuard("停止交易", round(loss_pct, 2), False, f"今日亏损达到 {stop_pct:.2f}%，真下单已锁定")
    if loss_pct >= warning_pct:
        return DailyLossGuard("警告", round(loss_pct, 2), True, f"今日亏损达到 {warning_pct:.2f}%，只建议模拟或减仓")
    return DailyLossGuard("正常", round(loss_pct, 2), True, "今日亏损未触发限制")


def account_read_failed_guard() -> DailyLossGuard:
    return DailyLossGuard("停止交易", 100.0, False, "账户风控读取失败，真下单已锁定")


def management_rules(plan: TradePlan) -> list[str]:
    return [
        "到 1R 后：止损移动到成本价",
        "到 1.5R 后：减仓 30%",
        "到 2R 后：保留尾仓，剩余仓位用移动止损",
        f"若价格触及止损 {plan.stop:.8f}：退出，不补仓摊平",
    ]


def _liquidation_buffer_pct(plan: TradePlan, liquidation_price: float) -> float:
    if plan.side == "short":
        buffer_distance = liquidation_price - plan.stop
    else:
        buffer_distance = plan.stop - liquidation_price
    return buffer_distance / plan.entry * 100


def _liquidation_price_for_review(
    plan: TradePlan,
    position: PositionSnapshot | None,
) -> tuple[float | None, str]:
    if position and position.source == "real" and position.symbol == plan.symbol and position.liquidation_price:
        return position.liquidation_price, "Binance真实强平价"
    return estimate_liquidation_price(plan.side, plan.entry, plan.leverage), "保守估算强平价"


def _score_signal_quality(signal: Signal, warnings: list[str], reasons: list[str], mode: str) -> int:
    delta = 0
    if signal.quote_volume_m >= 100:
        reasons.append("流动性充足")
        delta += 5
    elif signal.quote_volume_m < 50:
        warnings.append("流动性不足")
        delta -= 15
    atr = signal.atr_4h_pct if mode == "swing" and signal.atr_4h_pct is not None else signal.atr_pct
    atr = atr if atr is not None else 0.0
    if atr > (4.0 if mode == "intraday" else 8.0):
        warnings.append("ATR波动偏大")
        delta -= 15
    if signal.side == "long" and signal.rsi_1h >= 78:
        warnings.append("RSI过热，追多风险高")
        delta -= 12
    if signal.side == "short" and signal.rsi_1h <= 22:
        warnings.append("RSI过冷，追空风险高")
        delta -= 12
    if signal.funding_pct is not None and abs(signal.funding_pct) >= 0.08:
        warnings.append("资金费率拥挤")
        delta -= 12
    return delta
