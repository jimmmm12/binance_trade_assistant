from __future__ import annotations

import threading
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventType(str, Enum):
    MARKET = "market"
    ACCOUNT = "account"
    POSITION = "position"
    ORDER = "order"
    RISK = "risk"
    SYSTEM = "system"


@dataclass(frozen=True)
class TradingEvent:
    event_type: EventType
    source: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))


class EventBus:
    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: dict[EventType, list[Callable[[TradingEvent], None]]] = defaultdict(list)
        self._history: deque[TradingEvent] = deque(maxlen=history_size)
        self._lock = threading.RLock()

    def subscribe(self, event_type: EventType, handler: Callable[[TradingEvent], None]) -> None:
        with self._lock:
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: Callable[[TradingEvent], None]) -> None:
        with self._lock:
            if handler in self._subscribers[event_type]:
                self._subscribers[event_type].remove(handler)

    def publish(self, event: TradingEvent) -> None:
        with self._lock:
            self._history.append(event)
            handlers = list(self._subscribers[event.event_type])
        for handler in handlers:
            handler(event)

    def recent(self, limit: int = 100) -> list[TradingEvent]:
        with self._lock:
            return list(self._history)[-max(0, limit) :]
