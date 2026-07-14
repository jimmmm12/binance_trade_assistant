from __future__ import annotations

from trade_assistant.models import FuturesAccountRisk, PositionSnapshot
from trade_assistant.trading_system.core.events import EventBus, EventType
from trade_assistant.trading_system.state.manager import StateManager
from trade_assistant.trading_system.storage.database import TradingDatabase


def _position(quantity: float) -> PositionSnapshot:
    return PositionSnapshot(
        source="real",
        market="futures",
        symbol="BTCUSDT",
        side="long",
        quantity=quantity,
        entry_price=60000,
        mark_price=61000,
        notional=quantity * 61000,
        unrealized_pnl=quantity * 1000,
        realized_pnl=0,
        leverage=2,
        updated_at="2026-07-10T00:00:00",
    )


def test_state_manager_persists_and_recovers_binance_authoritative_state(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "state.db")
    manager = StateManager(store)
    first = FuturesAccountRisk(1000, 800, 5, [_position(0.5)])
    manager.reconcile_futures_account(first)

    recovered = StateManager(TradingDatabase(tmp_path / "state.db"))
    snapshot = recovered.snapshot()

    assert snapshot.account["wallet_balance"] == 1000
    assert snapshot.positions["futures:BTCUSDT"]["quantity"] == 0.5


def test_state_manager_reports_mismatch_and_uses_binance_quantity(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "state.db")
    manager = StateManager(store)
    manager.reconcile_futures_account(FuturesAccountRisk(1000, 800, 5, [_position(0.5)]))

    mismatches = manager.reconcile_futures_account(FuturesAccountRisk(1000, 700, 8, [_position(0.8)]))

    assert len(mismatches) == 1
    assert "已以 Binance 为准" in mismatches[0]
    assert manager.snapshot().positions["futures:BTCUSDT"]["quantity"] == 0.8


def test_state_manager_publishes_order_event_and_persists_order(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "state.db")
    bus = EventBus()
    events = []
    bus.subscribe(EventType.ORDER, events.append)
    manager = StateManager(store, bus)

    order = manager.apply_order_update(
        {
            "market": "futures",
            "symbol": "BTCUSDT",
            "clientOrderId": "BTA_TEST",
            "orderId": 123,
            "side": "BUY",
            "type": "LIMIT",
            "origQty": "0.01",
            "executedQty": "0.004",
            "status": "PARTIALLY_FILLED",
            "price": "60000",
        }
    )

    assert order["filled_quantity"] == 0.004
    assert store.get_order("BTA_TEST")["status"] == "PARTIALLY_FILLED"
    assert events[0].payload["client_order_id"] == "BTA_TEST"


def test_state_manager_normalizes_futures_algo_order_payload(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "state.db")
    manager = StateManager(store)

    order = manager.apply_order_update(
        {
            "market": "futures",
            "symbol": "BTCUSDT",
            "clientAlgoId": "BTA_STOP",
            "algoId": 12345,
            "side": "SELL",
            "orderType": "STOP_MARKET",
            "quantity": "0.01",
            "triggerPrice": "60000",
            "algoStatus": "NEW",
        }
    )

    assert order["client_order_id"] == "BTA_STOP"
    assert order["exchange_order_id"] == "12345"
    assert order["order_type"] == "STOP_MARKET"
    assert order["stop_price"] == 60000
    assert order["status"] == "NEW"


def test_state_manager_reconciles_spot_balance_and_symbol_position(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "state.db")
    manager = StateManager(store)
    account = {
        "balances": [
            {"asset": "USDT", "free": "800", "locked": "0"},
            {"asset": "SOL", "free": "2", "locked": "1"},
        ]
    }

    mismatches = manager.reconcile_spot_account(account, "SOLUSDT", 150)
    state = manager.snapshot()

    assert len(mismatches) == 1
    assert state.positions["spot:SOLUSDT"]["quantity"] == 3
    assert state.account["equity"] == 1250
