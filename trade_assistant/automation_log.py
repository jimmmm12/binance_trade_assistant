from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .main import ROOT
from .models import ScoredSignal, Signal, TradePlan
from .risk_engine import PlanRiskReview


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


def append_automation_event(path: Path | None, event: AutomationEvent) -> None:
    target = path or DEFAULT_AUTOMATION_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")


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
    )


def _best_atr(signal: Signal | None) -> float | None:
    if signal is None:
        return None
    for value in (signal.atr_1h_pct, signal.atr_pct, signal.atr_4h_pct):
        if value is not None:
            return value
    return None

