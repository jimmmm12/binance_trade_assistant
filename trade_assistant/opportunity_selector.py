from __future__ import annotations

from dataclasses import dataclass
from math import log10
from typing import Any

from .models import ScoredSignal


BENCHMARK_DIVERGENCE_MARKERS = ("BTC/ETH 大盘环境相反",)
STRUCTURAL_RISK_MARKERS = (
    "多周期方向冲突",
    "识别出的市场趋势相反",
    "主动成交方向与信号背离",
    "价格离保护结构较远",
    "RSI处于追涨追跌高风险区",
)


@dataclass(frozen=True)
class OpportunityAssessment:
    allowed: bool
    score: float
    tier: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def assess_opportunity(
    signal: ScoredSignal,
    settings: dict[str, Any],
    *,
    execution_mode: str = "simulate",
) -> OpportunityAssessment:
    """Research-style candidate filter inspired by mature bot frameworks.

    The regular signal score says "the setup has indicators". This layer asks a
    different question: is this candidate worth spending an automated order slot
    on after accounting for liquidity, strategy fit, trend confirmation, and
    enough movement room to pay fees/slippage?
    """

    selector = settings.get("opportunity_selection", {})
    if not bool(selector.get("enabled", True)):
        return OpportunityAssessment(True, float(signal.score), "legacy", ("旧评分排序",), ())

    aggressive = settings.get("auto_execution", {}).get("risk_line") == "aggressive"
    min_live_score = float(
        selector.get(
            "min_research_score_aggressive" if aggressive else "min_research_score_live",
            55.0 if aggressive else 62.0,
        )
    )
    weights = selector.get("weights", {})
    base_weight = float(weights.get("base_signal", 0.35))
    liquidity_weight = float(weights.get("liquidity", 0.18))
    relative_weight = float(weights.get("relative_strength", 0.18))
    confirmation_weight = float(weights.get("trend_confirmation", 0.19))
    cost_edge_weight = float(weights.get("cost_edge", 0.10))

    base = _clamp(signal.score)
    liquidity = _liquidity_score(signal, selector)
    relative_strength = _relative_strength_score(signal)
    confirmation = _trend_confirmation_score(signal)
    cost_edge = _cost_edge_score(signal, selector)
    weighted = (
        base * base_weight
        + liquidity * liquidity_weight
        + relative_strength * relative_weight
        + confirmation * confirmation_weight
        + cost_edge * cost_edge_weight
    )

    reasons: list[str] = []
    warnings: list[str] = []
    reasons.append(f"研究评分 {weighted:.0f}，原始评分 {signal.score}")
    if liquidity >= 70:
        reasons.append("流动性和量能通过")
    if relative_strength >= 65:
        reasons.append("相对强度方向有效")
    if confirmation >= 65:
        reasons.append("趋势/周期确认较好")
    if cost_edge >= 60:
        reasons.append("波动空间足以覆盖交易成本")

    warning_penalty = _warning_penalty(signal, selector, aggressive)
    if warning_penalty:
        weighted -= warning_penalty
        warnings.append(f"结构风险扣分 {warning_penalty:.0f}")

    strategy_penalty, strategy_warning = _strategy_fit_penalty(signal, selector, aggressive)
    if strategy_penalty:
        weighted -= strategy_penalty
        warnings.append(strategy_warning)

    atr_percentile = signal.signal.atr_percentile
    if atr_percentile is not None and atr_percentile >= float(selector.get("high_volatility_percentile", 92.0)):
        weighted -= float(selector.get("high_volatility_penalty", 12.0))
        warnings.append(f"ATR处于历史 {atr_percentile:.0f}% 分位，容易假突破")

    if _is_chasing(signal, selector):
        weighted -= float(selector.get("chase_penalty", 10.0))
        warnings.append("离保护结构太远，属于追单")

    weighted = max(0.0, min(100.0, weighted))
    tier = _tier(weighted)
    live_mode = execution_mode == "live"
    hard_structural = _has_structural_warning(signal, selector, aggressive)
    allowed = True
    if live_mode and weighted < min_live_score:
        allowed = False
        warnings.append(f"研究评分 {weighted:.0f} 低于真仓线 {min_live_score:.0f}")
    if live_mode and hard_structural and weighted < float(selector.get("structural_override_score", 82.0)):
        allowed = False
        warnings.append("结构冲突未达到高分覆盖线")

    return OpportunityAssessment(allowed, round(weighted, 2), tier, tuple(reasons), tuple(warnings))


def rank_candidates(
    candidates: list[ScoredSignal],
    settings: dict[str, Any],
    *,
    execution_mode: str = "simulate",
) -> list[ScoredSignal]:
    assessed = [
        (
            assess_opportunity(candidate, settings, execution_mode=execution_mode),
            candidate,
        )
        for candidate in candidates
    ]
    assessed.sort(
        key=lambda item: (
            item[0].allowed,
            item[0].score,
            item[1].score,
            item[1].quote_volume_m,
        ),
        reverse=True,
    )
    return [candidate for _, candidate in assessed]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _direction(signal: ScoredSignal) -> int:
    return 1 if signal.side == "long" else -1


def _directional_value(value: float | None, direction: int) -> bool:
    if value is None:
        return False
    return value * direction > 0


def _liquidity_score(signal: ScoredSignal, selector: dict[str, Any]) -> float:
    min_quote_volume_m = float(selector.get("min_quote_volume_m", 30.0))
    quote_volume_m = max(0.0, signal.quote_volume_m)
    if quote_volume_m <= 0:
        quote_component = 0.0
    else:
        quote_component = min(100.0, 45.0 + 22.0 * log10(max(1.0, quote_volume_m / min_quote_volume_m)))

    volume_ratio = max(0.0, float(signal.signal.volume_ratio or 0.0))
    if volume_ratio >= 1.8:
        volume_component = 100.0
    elif volume_ratio >= 1.2:
        volume_component = 78.0
    elif volume_ratio >= 0.85:
        volume_component = 55.0
    else:
        volume_component = 25.0
    return _clamp(quote_component * 0.55 + volume_component * 0.45)


def _relative_strength_score(signal: ScoredSignal) -> float:
    direction = _direction(signal)
    score = 20.0
    if _directional_value(signal.signal.momentum_24h, direction):
        score += 28.0
    if _directional_value(signal.signal.momentum_3d, direction):
        score += 28.0
    if _directional_value(signal.signal.obv_slope_pct, direction):
        score += 14.0
    taker = signal.signal.taker_buy_ratio
    if taker is not None:
        if (direction > 0 and taker >= 0.52) or (direction < 0 and taker <= 0.48):
            score += 10.0
        elif (direction > 0 and taker <= 0.45) or (direction < 0 and taker >= 0.55):
            score -= 12.0
    rsi = signal.signal.rsi_1h
    if direction > 0 and rsi >= 78:
        score -= 18.0
    if direction < 0 and rsi <= 22:
        score -= 18.0
    return _clamp(score)


def _trend_confirmation_score(signal: ScoredSignal) -> float:
    breakdown = signal.breakdown
    component_total = max(1.0, 30.0 + 10.0 + 10.0)
    component_score = (
        max(0.0, float(breakdown.trend))
        + max(0.0, float(breakdown.timeframe))
        + max(0.0, float(breakdown.regime))
    )
    score = component_score / component_total * 100.0
    selected_strategy = str(breakdown.selected_strategy or "").lower()
    if selected_strategy == "breakout":
        breakout_atr = signal.signal.breakout_atr
        if breakout_atr is not None and 0.0 <= breakout_atr <= 0.75:
            score += 10.0
    if selected_strategy == "mean_reversion" and breakdown.market_regime == "RANGE":
        score += 5.0
    return _clamp(score)


def _cost_edge_score(signal: ScoredSignal, selector: dict[str, Any]) -> float:
    atr = signal.signal.atr_1h_pct or signal.signal.atr_pct or 0.0
    min_move = float(selector.get("min_intraday_move_pct", 0.8))
    if atr <= 0:
        return 35.0
    if atr < min_move:
        return _clamp(25.0 + atr / min_move * 35.0)
    if atr <= float(selector.get("ideal_intraday_move_pct", 3.5)):
        return 80.0
    if atr <= float(selector.get("max_usable_intraday_move_pct", 5.5)):
        return 65.0
    return 38.0


def _warning_penalty(signal: ScoredSignal, selector: dict[str, Any], aggressive: bool) -> float:
    penalty = 0.0
    soft = float(selector.get("soft_warning_penalty", 5.0))
    structural = float(selector.get("structural_warning_penalty", 18.0))
    for warning in signal.warnings:
        if any(marker in warning for marker in BENCHMARK_DIVERGENCE_MARKERS):
            penalty += soft if aggressive else soft * 1.5
        elif any(marker in warning for marker in STRUCTURAL_RISK_MARKERS):
            penalty += structural
        else:
            penalty += soft
    return penalty


def _strategy_fit_penalty(
    signal: ScoredSignal,
    selector: dict[str, Any],
    aggressive: bool,
) -> tuple[float, str]:
    strategy = str(signal.breakdown.selected_strategy or "").lower()
    live_strategies = {
        str(item).lower()
        for item in selector.get("preferred_live_strategies", ["trend_following", "breakout"])
    }
    if strategy in live_strategies:
        return 0.0, ""
    if aggressive and strategy == "mean_reversion" and signal.breakdown.market_regime == "RANGE" and signal.score >= 78:
        return 4.0, "震荡均值回归仅按小仓处理"
    return float(selector.get("non_preferred_strategy_penalty", 16.0)), f"{strategy or '未识别'} 不是优先真仓策略"


def _has_structural_warning(signal: ScoredSignal, selector: dict[str, Any], aggressive: bool) -> bool:
    if aggressive and bool(selector.get("allow_benchmark_divergence_aggressive", True)):
        warnings = [
            warning
            for warning in signal.warnings
            if not any(marker in warning for marker in BENCHMARK_DIVERGENCE_MARKERS)
        ]
    else:
        warnings = signal.warnings
    return any(any(marker in warning for marker in STRUCTURAL_RISK_MARKERS) for warning in warnings)


def _is_chasing(signal: ScoredSignal, selector: dict[str, Any]) -> bool:
    direction = _direction(signal)
    max_distance = float(selector.get("max_protection_distance_atr", 2.8))
    if direction > 0:
        distance = signal.signal.support_distance_atr
    else:
        distance = signal.signal.resistance_distance_atr
    return distance is not None and distance > max_distance


def _tier(score: float) -> str:
    if score >= 82:
        return "A"
    if score >= 68:
        return "B"
    if score >= 55:
        return "C"
    return "D"
