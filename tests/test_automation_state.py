from __future__ import annotations

from trade_assistant.automation_state import AutoTradeState, AutoTradeStateMachine, compact_state_path


def test_state_machine_summary_compacts_long_candidate_rotation_path() -> None:
    machine = AutoTradeStateMachine()
    for _ in range(8):
        machine.move(AutoTradeState.OPPORTUNITY_FOUND, "发现候选")
        machine.move(AutoTradeState.PLAN_GENERATED, "生成计划")
        machine.move(AutoTradeState.WAITING_CONFIRMATION, "等待下一候选")

    summary = machine.summary

    assert summary == "空仓观察 -> 发现机会 -> 生成计划 -> 等待确认/自动模拟"
    assert "..." not in summary
    assert "等待确认/自动模拟" in summary


def test_compact_state_path_collapses_old_recovered_path() -> None:
    old_path = " -> ".join(
        [
            "空仓观察",
            *["发现机会", "生成计划", "等待确认/自动模拟", "等待确认/自动模拟"] * 8,
        ]
    )

    compacted = compact_state_path(old_path)

    assert compacted == "空仓观察 -> 发现机会 -> 生成计划 -> 等待确认/自动模拟"
    assert compacted.count("等待确认/自动模拟") <= 1
    assert len(compacted) < len(old_path)
