from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AutoTradeState(str, Enum):
    EMPTY_OBSERVING = "空仓观察"
    OPPORTUNITY_FOUND = "发现机会"
    PLAN_GENERATED = "生成计划"
    WAITING_CONFIRMATION = "等待确认/自动模拟"
    OPENED = "已开仓"
    MANAGING = "持仓管理"
    REDUCING_OR_TRAILING = "减仓/移动止损"
    REVIEWING = "平仓复盘"
    BLOCKED = "风控阻断"
    ERROR = "异常"


@dataclass(frozen=True)
class StateTransition:
    created_at: str
    from_state: AutoTradeState
    to_state: AutoTradeState
    symbol: str
    reason: str


@dataclass
class AutoTradeStateMachine:
    state: AutoTradeState = AutoTradeState.EMPTY_OBSERVING
    transitions: list[StateTransition] = field(default_factory=list)

    def move(self, to_state: AutoTradeState, reason: str, symbol: str = "") -> AutoTradeState:
        self.transitions.append(
            StateTransition(
                created_at=datetime.now().isoformat(timespec="seconds"),
                from_state=self.state,
                to_state=to_state,
                symbol=symbol,
                reason=reason,
            )
        )
        self.state = to_state
        return self.state

    @property
    def summary(self) -> str:
        if not self.transitions:
            return self.state.value
        path = " -> ".join([self.transitions[0].from_state.value, *[item.to_state.value for item in self.transitions]])
        return compact_state_path(path)


def compact_state_path(path: str, max_steps: int = 6) -> str:
    parts = [part.strip() for part in str(path).split("->") if part.strip()]
    if not parts:
        return AutoTradeState.EMPTY_OBSERVING.value

    collapsed: list[str] = []
    for part in parts:
        if not collapsed or collapsed[-1] != part:
            collapsed.append(part)
    deduped: list[str] = []
    seen: set[str] = set()
    for part in collapsed:
        if part in seen:
            continue
        deduped.append(part)
        seen.add(part)
    collapsed = deduped

    if len(collapsed) <= max_steps:
        return " -> ".join(collapsed)
    return " -> ".join([collapsed[0], "...", *collapsed[-(max_steps - 2) :]])
