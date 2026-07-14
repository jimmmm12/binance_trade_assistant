from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .main import ROOT
from .models import ScoredSignal, Signal, TradePlan
from .risk_engine import PlanRiskReview
from .dataset import append_automation_record


DEFAULT_AUTOMATION_LOG_PATH = ROOT / "data" / "automation_events.jsonl"


@dataclass(frozen=True)
class AutomationEvent:
    created_at: str
    state: str
    action: str
    symbol: str
    message: str
    score: int | None = None
    recommended_action: str | None = None
    price: float | None = None
    atr_pct: float | None = None
    rsi_1h: float | None = None
    volume_ratio: float | None = None
    funding_pct: float | None = None
    realized_pnl: float | None = None
    plan_followed: bool | None = None
    grade: str | None = None
    position_multiplier: float | None = None
    score_breakdown: dict[str, int] | None = None
    reasons: list[str] | None = None
    warnings: list[str] | None = None
    selected_strategy: str | None = None
    market_regime: str | None = None
    return_series_1h: list[float] | None = None
    plan: dict[str, float | str] | None = None


def append_automation_event(path: Path | None, event: AutomationEvent) -> None:
    target = path or DEFAULT_AUTOMATION_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
    if path is None:
        try:
            append_automation_record(event)
        except OSError:
            # Dataset capture must never interrupt a trading decision.
            pass


def build_automation_event(
    *,
    state: str,
    action: str,
    message: str,
    signal: Signal | ScoredSignal | None = None,
    plan: TradePlan | None = None,
    review: PlanRiskReview | None = None,
    realized_pnl: float | None = None,
    plan_followed: bool | None = None,
) -> AutomationEvent:
    base = signal.signal if isinstance(signal, ScoredSignal) else signal
    score = signal.score if isinstance(signal, ScoredSignal) else (base.score if base else None)
    breakdown = signal.breakdown if isinstance(signal, ScoredSignal) else None
    return AutomationEvent(
        created_at=datetime.now().isoformat(timespec="seconds"),
        state=state,
        action=action,
        symbol=base.symbol if base else (plan.symbol if plan else ""),
        message=message,
        score=score,
        recommended_action=review.recommended_action if review else None,
        price=base.last if base else (plan.entry if plan else None),
        atr_pct=_best_atr(base) if base else None,
        rsi_1h=base.rsi_1h if base else None,
        volume_ratio=base.volume_ratio if base else None,
        funding_pct=base.funding_pct if base else None,
        realized_pnl=realized_pnl,
        plan_followed=plan_followed,
        grade=breakdown.grade if breakdown else None,
        position_multiplier=breakdown.position_multiplier if breakdown else None,
        score_breakdown=(
            {
                "trend": breakdown.trend,
                "momentum": breakdown.momentum,
                "volume": breakdown.volume,
                "position": breakdown.positioning,
                "timeframe": breakdown.timeframe,
                "regime": breakdown.regime,
            }
            if breakdown
            else None
        ),
        reasons=breakdown.reasons if breakdown else None,
        warnings=breakdown.warnings if breakdown else None,
        selected_strategy=breakdown.selected_strategy if breakdown else None,
        market_regime=breakdown.market_regime if breakdown else None,
        return_series_1h=list(base.returns_1h) if base else None,
        plan=(
            {
                "entry": plan.entry,
                "stop": plan.stop,
                "target": plan.target,
                "risk_pct": plan.risk_pct,
                "leverage": plan.leverage,
                "notional": plan.notional,
            }
            if plan
            else None
        ),
    )


def _best_atr(signal: Signal | None) -> float | None:
    if signal is None:
        return None
    for value in (signal.atr_1h_pct, signal.atr_pct, signal.atr_4h_pct):
        if value is not None:
            return value
    return None
