from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import PositionSnapshot, ScoredSignal, Signal


@dataclass(frozen=True)
class ScoreRiskTier:
    min_score: int
    multiplier: float


@dataclass(frozen=True)
class AutomationSizingConfig:
    max_single_risk_pct: float = 1.0
    score_tiers: tuple[ScoreRiskTier, ...] = (
        ScoreRiskTier(90, 1.0),
        ScoreRiskTier(80, 0.7),
        ScoreRiskTier(70, 0.4),
    )
    min_open_score: int = 70
    first_entry_pct: float = 0.4
    add_stage_pcts: tuple[float, ...] = (0.3, 0.3)
    max_add_count: int = 2
    min_profit_r_for_add: float = 1.0
    min_add_score: int = 85
    add_order_pct_of_initial: float = 0.3
    allow_loss_add: bool = False
    loss_add_min_score: int = 92
    max_loss_add_r: float = -0.35
    profit_take_rules: tuple[tuple[float, float, str], ...] = (
        (1.0, 0.3, "1R减仓"),
        (2.0, 0.3, "2R减仓"),
    )
    risk_reduce_pct: float = 0.5
    atr_stop_multiplier: float = 2.0
    trailing_atr_multiplier: float = 2.0
    time_stop_hours: float = 48.0
    time_stop_min_r: float = 0.5
    time_stop_min_score: int = 78
    reduce_score_threshold: int = 60
    max_margin_drawdown_reduce_pct: float = 15.0
    max_margin_drawdown_close_pct: float = 28.0
    max_position_leverage: float = 8.0
    max_symbol_exposure_pct: float = 40.0
    max_total_exposure_pct: float = 180.0
    loss_streak_reduce_after: int = 3
    loss_streak_stop_after: int = 5
    loss_streak_reduction_multiplier: float = 0.5


@dataclass(frozen=True)
class SizingDecision:
    allowed: bool
    risk_pct: float
    score_multiplier: float
    volatility_multiplier: float
    stage_multiplier: float
    account_multiplier: float
    stage_label: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def automation_sizing_config(settings: dict[str, Any] | None) -> AutomationSizingConfig:
    source = (settings or {}).get("automation_positioning", {})
    tiers = source.get("score_tiers")
    parsed_tiers: tuple[ScoreRiskTier, ...]
    if isinstance(tiers, list):
        parsed: list[ScoreRiskTier] = []
        for row in tiers:
            if not isinstance(row, dict):
                continue
            parsed.append(ScoreRiskTier(int(row.get("min_score", 0)), float(row.get("multiplier", 0))))
        parsed_tiers = tuple(sorted(parsed, key=lambda item: item.min_score, reverse=True)) or AutomationSizingConfig().score_tiers
    else:
        parsed_tiers = AutomationSizingConfig().score_tiers

    profit_rules = _tuple_rules(
        source.get("profit_take_rules"),
        AutomationSizingConfig().profit_take_rules,
        label_key="marker",
    )
    return AutomationSizingConfig(
        max_single_risk_pct=float(source.get("max_single_risk_pct", (settings or {}).get("default_risk_pct", 1.0))),
        score_tiers=parsed_tiers,
        min_open_score=int(source.get("min_open_score", 70)),
        first_entry_pct=float(source.get("first_entry_pct", 0.4)),
        add_stage_pcts=tuple(float(item) for item in source.get("add_stage_pcts", [0.3, 0.3])),
        max_add_count=int(source.get("max_add_count", 2)),
        min_profit_r_for_add=float(source.get("min_profit_r_for_add", 1.0)),
        min_add_score=int(source.get("min_add_score", 85)),
        add_order_pct_of_initial=float(source.get("add_order_pct_of_initial", 0.3)),
        allow_loss_add=bool(source.get("allow_loss_add", False)),
        loss_add_min_score=int(source.get("loss_add_min_score", 92)),
        max_loss_add_r=float(source.get("max_loss_add_r", -0.35)),
        profit_take_rules=profit_rules,
        risk_reduce_pct=float(source.get("risk_reduce_pct", 0.5)),
        atr_stop_multiplier=float(source.get("atr_stop_multiplier", 2.0)),
        trailing_atr_multiplier=float(source.get("trailing_atr_multiplier", 2.0)),
        time_stop_hours=float(source.get("time_stop_hours", 48.0)),
        time_stop_min_r=float(source.get("time_stop_min_r", 0.5)),
        time_stop_min_score=int(source.get("time_stop_min_score", 78)),
        reduce_score_threshold=int(source.get("reduce_score_threshold", 60)),
        max_margin_drawdown_reduce_pct=float(source.get("max_margin_drawdown_reduce_pct", 15.0)),
        max_margin_drawdown_close_pct=float(source.get("max_margin_drawdown_close_pct", 28.0)),
        max_position_leverage=float(source.get("max_position_leverage", (settings or {}).get("default_leverage", 8.0))),
        max_symbol_exposure_pct=float(source.get("max_symbol_exposure_pct", 40.0)),
        max_total_exposure_pct=float(source.get("max_total_exposure_pct", 180.0)),
        loss_streak_reduce_after=int(source.get("loss_streak_reduce_after", 3)),
        loss_streak_stop_after=int(source.get("loss_streak_stop_after", 5)),
        loss_streak_reduction_multiplier=float(source.get("loss_streak_reduction_multiplier", 0.5)),
    )


def initial_sizing_decision(
    signal: Signal | ScoredSignal | None,
    mode: str,
    settings: dict[str, Any] | None,
    *,
    base_risk_pct: float | None = None,
    stage_multiplier: float | None = None,
    consecutive_losses: int = 0,
) -> SizingDecision:
    config = automation_sizing_config(settings)
    score = _score(signal)
    reasons: list[str] = []
    warnings: list[str] = []
    if score < config.min_open_score:
        return SizingDecision(
            False,
            0.0,
            0.0,
            1.0,
            0.0,
            1.0,
            "禁止开仓",
            warnings=[f"评分 {score} 低于开仓线 {config.min_open_score}"],
        )

    score_multiplier = score_risk_multiplier(score, config)
    volatility_multiplier = volatility_risk_multiplier(signal, mode)
    actual_stage = config.first_entry_pct if stage_multiplier is None else stage_multiplier
    account_multiplier = loss_streak_multiplier(consecutive_losses, config)
    if account_multiplier <= 0:
        return SizingDecision(
            False,
            0.0,
            score_multiplier,
            volatility_multiplier,
            actual_stage,
            account_multiplier,
            "连续亏损保护",
            warnings=[f"连续亏损 {consecutive_losses} 次，暂停新开仓"],
        )

    raw_risk = config.max_single_risk_pct if base_risk_pct is None else min(base_risk_pct, config.max_single_risk_pct)
    risk_pct = round(raw_risk * score_multiplier * volatility_multiplier * actual_stage * account_multiplier, 4)
    reasons.append(f"评分 {score} 风险系数 {score_multiplier:.0%}")
    reasons.append(f"波动调整 {volatility_multiplier:.0%}")
    reasons.append(f"阶段仓位 {actual_stage:.0%}")
    if account_multiplier < 1:
        warnings.append(f"连续亏损保护，风险降至 {account_multiplier:.0%}")
    return SizingDecision(
        allowed=risk_pct > 0,
        risk_pct=risk_pct,
        score_multiplier=score_multiplier,
        volatility_multiplier=volatility_multiplier,
        stage_multiplier=actual_stage,
        account_multiplier=account_multiplier,
        stage_label="初始试探仓" if actual_stage <= config.first_entry_pct + 1e-9 else "阶段加仓",
        reasons=reasons,
        warnings=warnings,
    )


def score_risk_multiplier(score: int, config: AutomationSizingConfig) -> float:
    for tier in config.score_tiers:
        if score >= tier.min_score:
            return max(0.0, float(tier.multiplier))
    return 0.0


def volatility_risk_multiplier(signal: Signal | ScoredSignal | None, mode: str) -> float:
    base = signal.signal if isinstance(signal, ScoredSignal) else signal
    if base is None:
        return 0.85
    atr = base.atr_4h_pct if mode == "swing" and base.atr_4h_pct is not None else base.atr_1h_pct
    atr = atr if atr is not None else base.atr_pct
    if atr is None or atr <= 0:
        return 0.85
    healthy = 4.0 if mode == "intraday" else 8.0
    extreme = 6.0 if mode == "intraday" else 14.0
    if atr >= extreme:
        return 0.25
    if atr > healthy:
        return 0.5
    if atr < (0.45 if mode == "intraday" else 0.9):
        return 0.7
    return 1.0


def loss_streak_multiplier(consecutive_losses: int, config: AutomationSizingConfig) -> float:
    if consecutive_losses >= config.loss_streak_stop_after:
        return 0.0
    if consecutive_losses >= config.loss_streak_reduce_after:
        return max(0.0, config.loss_streak_reduction_multiplier)
    return 1.0


def next_add_stage_pct(status: str, config: AutomationSizingConfig) -> float:
    add_count = status.count("加仓")
    if add_count >= config.max_add_count or add_count >= len(config.add_stage_pcts):
        return 0.0
    return max(0.0, config.add_stage_pcts[add_count])


def exposure_allowed(
    *,
    equity: float,
    current_symbol_notional: float,
    add_notional: float,
    config: AutomationSizingConfig,
) -> bool:
    if equity <= 0:
        return False
    return current_symbol_notional + add_notional <= equity * config.max_symbol_exposure_pct / 100


def atr_price_distance(signal: Signal | ScoredSignal | None, price: float, mode: str, multiplier: float) -> float:
    base = signal.signal if isinstance(signal, ScoredSignal) else signal
    if base is None or price <= 0:
        return 0.0
    atr = base.atr_4h_pct if mode == "swing" and base.atr_4h_pct is not None else base.atr_1h_pct
    atr = atr if atr is not None else base.atr_pct
    if atr is None or atr <= 0:
        return 0.0
    return price * atr / 100 * multiplier


def holding_hours(position: PositionSnapshot) -> float:
    try:
        started = datetime.fromisoformat(position.updated_at)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now() - started).total_seconds() / 3600)


def lifecycle_state(status: str, r_multiple: float, quantity: float) -> str:
    if quantity <= 0:
        return "EXIT"
    if "退出" in status or "已平仓" in status:
        return "EXIT"
    if "减仓" in status:
        return "REDUCE_POSITION"
    if "移动止损" in status or "保本" in status or "TRAILING" in status:
        return "TRAILING"
    if "加仓" in status:
        return "ADD_POSITION"
    if r_multiple >= 1:
        return "PROFIT_HOLD"
    return "INITIAL"


def _score(signal: Signal | ScoredSignal | None) -> int:
    if signal is None:
        return 0
    if isinstance(signal, ScoredSignal):
        return signal.score
    score = int(signal.score)
    return score * 10 if 0 < score <= 10 else score


def _tuple_rules(value: Any, default: tuple[tuple[float, float, str], ...], label_key: str) -> tuple[tuple[float, float, str], ...]:
    if not isinstance(value, list):
        return default
    result: list[tuple[float, float, str]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        result.append((float(row.get("r", 0)), float(row.get("reduce_pct", 0)), str(row.get(label_key, ""))))
    return tuple(item for item in result if item[0] > 0 and item[1] > 0 and item[2]) or default
