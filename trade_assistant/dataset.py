from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .main import ROOT


DATASET_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.Lock()


def dataset_root() -> Path:
    """Return a persistent dataset directory outside disposable frozen releases."""
    configured = os.getenv("BTA_DATASET_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # <project>/release/BinanceTradeAssistant -> <project>/research_dataset
        return Path(sys.executable).resolve().parents[2] / "research_dataset"
    return ROOT / "research_dataset"


def append_automation_record(event: Any) -> Path:
    payload = asdict(event) if is_dataclass(event) else dict(event)
    record = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "record_type": "automation_decision",
        "event_id": _event_id(payload),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "features": {
            "symbol": payload.get("symbol", ""),
            "score": payload.get("score"),
            "price": payload.get("price"),
            "atr_pct": payload.get("atr_pct"),
            "rsi_1h": payload.get("rsi_1h"),
            "volume_ratio": payload.get("volume_ratio"),
            "funding_pct": payload.get("funding_pct"),
            "grade": payload.get("grade"),
            "position_multiplier": payload.get("position_multiplier"),
            "score_breakdown": payload.get("score_breakdown"),
            "reasons": payload.get("reasons"),
            "warnings": payload.get("warnings"),
            "selected_strategy": payload.get("selected_strategy"),
            "market_regime": payload.get("market_regime"),
            "return_series_1h": payload.get("return_series_1h"),
        },
        "decision": {
            "timestamp": payload.get("created_at"),
            "state": payload.get("state"),
            "action": payload.get("action"),
            "recommended_action": payload.get("recommended_action"),
            "message": payload.get("message"),
            "plan_followed": payload.get("plan_followed"),
            "realized_pnl": payload.get("realized_pnl"),
            "plan": payload.get("plan"),
        },
    }
    return _append_record("decisions", record)


def migrate_automation_events(events: Iterable[dict[str, Any]]) -> int:
    count = 0
    for event in events:
        append_automation_record(event)
        count += 1
    return count


def append_trade_outcome_record(outcome: dict[str, Any]) -> Path:
    record = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "record_type": "trade_outcome",
        "event_id": _event_id(outcome),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "outcome": outcome,
    }
    return _append_record("outcomes", record)


def migrate_trade_outcomes(outcomes: Iterable[dict[str, Any]]) -> int:
    count = 0
    for outcome in outcomes:
        append_trade_outcome_record(outcome)
        count += 1
    return count


def _append_record(category: str, record: dict[str, Any]) -> Path:
    root = dataset_root()
    _ensure_manifest(root)
    partition = datetime.now().strftime("%Y-%m")
    target = root / category / f"{partition}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        with target.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return target


def _ensure_manifest(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "dataset_manifest.json"
    if manifest.exists():
        return
    payload = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "name": "binance_trade_assistant_research_dataset",
        "retention": "append_only",
        "protected": True,
        "cleanup_policy": "Never delete this directory when cleaning builds, releases, reports, or old executables.",
        "contains": ["automation decisions", "market features", "risk reasons", "future outcome labels"],
    }
    manifest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _event_id(payload: dict[str, Any]) -> str:
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
