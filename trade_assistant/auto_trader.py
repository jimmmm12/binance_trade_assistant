from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .gui.services import auto_plan_prices, evaluate_plan_from_form, simulate_order_from_form
from .models import PositionSnapshot, ScoredSignal, TradePlan
from .risk_engine import PlanRiskReview


@dataclass(frozen=True)
class AutoTradeConfig:
    market: str
    mode: str
    top: int
    auto_simulate: bool
    equity: float = 1000.0
    portfolio_path: Path | None = None


@dataclass(frozen=True)
class AutoTradeDecision:
    action: str
    message: str
    signal: ScoredSignal | None
    plan: TradePlan | None
    review: PlanRiskReview | None = None
    position: PositionSnapshot | None = None


def select_candidate(longs: list[ScoredSignal], shorts: list[ScoredSignal]) -> ScoredSignal | None:
    candidates = [*longs, *shorts]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.score, item.quote_volume_m), reverse=True)[0]


def run_auto_cycle(
    config: AutoTradeConfig,
    *,
    scan_fn: Callable[[], tuple[list[ScoredSignal], list[ScoredSignal]]],
) -> AutoTradeDecision:
    longs, shorts = scan_fn()
    signal = select_candidate(longs, shorts)
    if signal is None:
        return AutoTradeDecision("no_signal", "本轮没有可用信号", None, None)
    prices = auto_plan_prices(signal, config.mode)
    if prices.adaptive is not None and not prices.adaptive.allow_live and not config.auto_simulate:
        return AutoTradeDecision("blocked", "自适应参数只建议模拟，未启用自动模拟", signal, None)
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
    if not review.live_allowed and not config.auto_simulate:
        return AutoTradeDecision("blocked", "风控评审不允许自动执行", signal, plan, review)
    if not config.auto_simulate:
        return AutoTradeDecision("planned", "已自动生成计划，等待人工确认", signal, plan, review)
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
    return AutoTradeDecision("simulated_order", "已自动生成计划并模拟下单", signal, plan, review, position)
