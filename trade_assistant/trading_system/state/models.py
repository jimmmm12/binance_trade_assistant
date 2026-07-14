from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TradingState:
    account: dict[str, Any] = field(default_factory=dict)
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_status: dict[str, Any] = field(default_factory=lambda: {"status": "normal", "opening_allowed": True})
    market_regime: dict[str, dict[str, Any]] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    automation: dict[str, Any] = field(default_factory=lambda: {"state": "空仓观察", "action": "idle"})
    sync_status: dict[str, Any] = field(default_factory=lambda: {"source": "local", "healthy": True})
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradingState":
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in allowed})
