from __future__ import annotations

import json

from trade_assistant.automation_log import AutomationEvent
from trade_assistant import dataset


def test_dataset_appends_structured_automation_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BTA_DATASET_DIR", str(tmp_path / "research_dataset"))
    event = AutomationEvent(
        created_at="2026-07-12T10:30:00",
        state="已开仓",
        action="live_order_sent",
        symbol="UNIUSDT",
        message="submitted",
        score=76,
        price=3.6,
        volume_ratio=1.5,
        warnings=["测试警告"],
    )

    path = dataset.append_automation_record(event)

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["record_type"] == "automation_decision"
    assert row["features"]["symbol"] == "UNIUSDT"
    assert row["decision"]["action"] == "live_order_sent"
    assert (tmp_path / "research_dataset" / "dataset_manifest.json").exists()


def test_dataset_appends_trade_outcome(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BTA_DATASET_DIR", str(tmp_path / "research_dataset"))

    path = dataset.append_trade_outcome_record(
        {"symbol": "UNIUSDT", "side": "long", "entry": 3.5, "exit": 3.7, "pnl": 0.2}
    )

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["record_type"] == "trade_outcome"
    assert row["outcome"]["pnl"] == 0.2
