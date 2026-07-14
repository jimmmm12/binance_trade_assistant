from __future__ import annotations

import json
from dataclasses import replace

from tests.test_strategy_scoring import make_signal
from trade_assistant.binance_client import BinanceClient
from trade_assistant.risk import create_trade_plan
from trade_assistant.trading_system.data.user_data import BinanceUserDataService
from trade_assistant.trading_system.runtime import TradingRuntime
from trade_assistant.trading_system.strategy.regime import MarketRegime, detect_regime


class FakeClient:
    pass


def test_user_data_service_forwards_decoded_event() -> None:
    events = []
    service = BinanceUserDataService(FakeClient(), events.append)

    service._on_message(None, json.dumps({"e": "ORDER_TRADE_UPDATE", "o": {"c": "BTA_1"}}))

    assert events[0]["e"] == "ORDER_TRADE_UPDATE"
    assert service.last_event_at is not None


def test_runtime_keeps_user_stream_when_credentials_are_unchanged(tmp_path) -> None:
    runtime = TradingRuntime(
        client=BinanceClient(api_key="key", api_secret="secret"),
        database_path=tmp_path / "trading.db",
    )

    class UserDataMustNotRestart:
        def stop(self):
            raise AssertionError("same credentials should not restart user stream")

    runtime.user_data = UserDataMustNotRestart()

    runtime.update_credentials("key", "secret")


def test_protective_order_failure_emergency_stops_new_entries(tmp_path) -> None:
    class ProtectionClient:
        api_key = "key"
        api_secret = "secret"

        def futures_income_history(self, start_time, end_time=None):
            return []

    runtime = TradingRuntime(client=ProtectionClient(), database_path=tmp_path / "trading.db")
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10, 9.5, 11, 1000, 1, 2)
    runtime._pending_protection["BTA_PARENT"] = (plan, "ABC")

    def fail_protection(*args, **kwargs):
        raise RuntimeError("algo endpoint unavailable")

    runtime.order_manager.submit_protective_orders = fail_protection
    runtime._place_pending_protection("BTA_PARENT")

    state = runtime.state_manager.snapshot()
    assert state.risk_status["status"] == "stopped"
    assert "保护单提交失败" in state.risk_status["message"]
    assert runtime.store.load_state("protection:BTA_PARENT")["status"] == "failed"


def test_regime_detector_identifies_trend_range_and_no_trade() -> None:
    base = make_signal()
    up = replace(base, ema20_1h=11, ema50_1h=10, adx_1h=32, atr_1h_pct=2)
    ranging = replace(base, ema20_1h=10.1, ema50_1h=10, adx_1h=12, atr_1h_pct=1)
    extreme = replace(base, atr_1h_pct=7)

    assert detect_regime(up, "intraday").regime == MarketRegime.TREND_UP
    assert detect_regime(ranging, "intraday").regime == MarketRegime.RANGE
    assert detect_regime(extreme, "intraday").regime == MarketRegime.NO_TRADE
