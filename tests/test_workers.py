from __future__ import annotations

from trade_assistant.binance_client import MarketDataUnavailable
from trade_assistant.gui.workers import FunctionWorker


def test_function_worker_emits_user_facing_error_without_traceback() -> None:
    errors: list[str] = []

    def fail() -> None:
        raise MarketDataUnavailable("行情接口暂时不可用")

    worker = FunctionWorker(fail)
    worker.signals.error.connect(errors.append)
    worker.run()

    assert errors == ["行情接口暂时不可用"]
