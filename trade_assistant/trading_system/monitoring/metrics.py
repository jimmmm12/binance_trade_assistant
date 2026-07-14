from __future__ import annotations

import ctypes
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from ..state.manager import StateManager
from ..storage.database import TradingDatabase
from ..research.performance import PerformanceAnalyzer


@dataclass(frozen=True)
class RuntimeMetrics:
    uptime_seconds: float
    cpu_percent: float
    memory_mb: float | None
    api_latency_ms: float | None
    market_websocket: str
    user_websocket: str
    state_sync_healthy: bool
    position_count: int
    active_order_count: int
    order_status_counts: dict[str, int]
    risk_status: str
    performance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetricsCollector:
    def __init__(self, store: TradingDatabase, state_manager: StateManager) -> None:
        self.store = store
        self.state_manager = state_manager
        self.started_at = time.time()
        self.api_latency_ms: float | None = None
        self.performance = PerformanceAnalyzer(store)
        self._last_wall = time.perf_counter()
        self._last_cpu = time.process_time()

    def record_api_latency(self, started_at: float) -> None:
        self.api_latency_ms = round((time.perf_counter() - started_at) * 1000, 2)

    def snapshot(self, market_websocket: str, user_websocket: str) -> RuntimeMetrics:
        state = self.state_manager.snapshot()
        status_counts = self.store.order_status_counts()
        active = sum(status_counts.get(status, 0) for status in {"CREATED", "SUBMITTING", "NEW", "PARTIALLY_FILLED", "UNKNOWN"})
        performance = self.performance.analyze().to_dict()
        now_wall = time.perf_counter()
        now_cpu = time.process_time()
        wall_delta = max(1e-9, now_wall - self._last_wall)
        cpu_percent = max(0.0, (now_cpu - self._last_cpu) / wall_delta * 100)
        self._last_wall = now_wall
        self._last_cpu = now_cpu
        return RuntimeMetrics(
            uptime_seconds=round(time.time() - self.started_at, 1),
            cpu_percent=round(cpu_percent, 1),
            memory_mb=_process_memory_mb(),
            api_latency_ms=self.api_latency_ms,
            market_websocket=market_websocket,
            user_websocket=user_websocket,
            state_sync_healthy=bool(state.sync_status.get("healthy", False)),
            position_count=len(state.positions),
            active_order_count=active,
            order_status_counts=status_counts,
            risk_status=str(state.risk_status.get("status", "unknown")),
            performance=performance,
        )


def _process_memory_mb() -> float | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return None
    return round(counters.WorkingSetSize / 1024 / 1024, 1)
