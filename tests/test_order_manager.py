from __future__ import annotations

from types import SimpleNamespace

from trade_assistant.risk import create_trade_plan
from trade_assistant.trading_system.execution.order_manager import OrderManager
from trade_assistant.trading_system.risk.manager import RiskContext, RiskLimits, RiskManager
from trade_assistant.trading_system.state.manager import StateManager
from trade_assistant.trading_system.storage.database import TradingDatabase


class FakeEngine:
    def __init__(self, submit_result=None, submit_error=None, query_result=None) -> None:
        self.submit_result = submit_result or {
            "symbol": "UNIUSDT",
            "clientOrderId": "FIXED",
            "orderId": 1,
            "side": "BUY",
            "type": "LIMIT",
            "origQty": "20",
            "executedQty": "0",
            "price": "10",
            "status": "NEW",
        }
        self.submit_error = submit_error
        self.query_result = query_result or self.submit_result
        self.submit_calls = 0
        self.query_calls = 0
        self.payloads = []
        self.cancel_calls = 0
        self.leverages = []
        self.client = None

    def submit(self, market, payload, allow_live, confirm):
        self.submit_calls += 1
        self.payloads.append(payload)
        if self.submit_error:
            raise self.submit_error
        return {
            **self.submit_result,
            "clientOrderId": payload["newClientOrderId"],
            "type": payload["type"],
            "origQty": str(payload["quantity"]),
            "stopPrice": str(payload.get("stopPrice", 0)),
        }

    def set_futures_leverage(self, symbol, leverage):
        self.leverages.append((symbol, leverage))
        return {"symbol": symbol, "leverage": str(leverage)}

    def query(self, market, symbol, client_order_id, *, algo=False, exchange_order_id=None):
        self.query_calls += 1
        return {**self.query_result, "clientOrderId": client_order_id}

    def cancel(self, market, symbol, client_order_id, *, algo=False, exchange_order_id=None):
        self.cancel_calls += 1
        return {**self.query_result, "clientOrderId": client_order_id, "status": "CANCELED"}


def _manager(tmp_path, engine: FakeEngine) -> tuple[OrderManager, TradingDatabase]:
    store = TradingDatabase(tmp_path / "orders.db")
    state = StateManager(store)
    risk = RiskManager(RiskLimits(), store, state)
    return OrderManager(store, state, risk, engine), store


def _plan():
    return create_trade_plan("UNIUSDT", "futures", "long", 10, 9.5, 11, 1000, 1, 2)


def test_order_manager_persists_lifecycle_and_prevents_duplicate_submit(tmp_path) -> None:
    engine = FakeEngine()
    manager, store = _manager(tmp_path, engine)
    context = RiskContext(equity=1000)

    first = manager.submit_plan(
        _plan(),
        "BUY",
        None,
        context,
        allow_live=True,
        confirm="ABC",
        client_order_id="FIXED",
    )
    second = manager.submit_plan(
        _plan(),
        "BUY",
        None,
        context,
        allow_live=True,
        confirm="ABC",
        client_order_id="FIXED",
    )

    assert first["managed_status"] == "NEW"
    assert second["duplicate"] is True
    assert engine.submit_calls == 1
    assert store.get_order("FIXED")["status"] == "NEW"
    assert engine.leverages == [("UNIUSDT", 2)]


def test_order_manager_rejects_opening_when_exchange_leverage_differs_from_plan(tmp_path) -> None:
    class WrongLeverageEngine(FakeEngine):
        def set_futures_leverage(self, symbol, leverage):
            return {"symbol": symbol, "leverage": "20"}

    manager, store = _manager(tmp_path, WrongLeverageEngine())

    try:
        manager.submit_plan(
            _plan(), "BUY", None, RiskContext(equity=1000), allow_live=True, confirm="ABC", client_order_id="BAD_LEV"
        )
    except Exception as exc:
        assert "杠杆设置/验证失败" in str(exc)
    else:
        raise AssertionError("mismatched Binance leverage must reject the opening order")
    assert store.get_order("BAD_LEV")["status"] == "REJECTED"


def test_order_manager_uses_post_only_for_automatic_limit_entry(tmp_path) -> None:
    engine = FakeEngine()
    manager, _ = _manager(tmp_path, engine)

    manager.submit_plan(
        _plan(), "BUY", None, RiskContext(equity=1000), allow_live=True, confirm="ABC", post_only=True
    )

    assert engine.payloads[0]["timeInForce"] == "GTX"


def test_order_manager_returns_clean_rejection_when_post_only_would_take_liquidity(tmp_path) -> None:
    engine = FakeEngine(submit_error=RuntimeError("Binance 下单接口 HTTP 400：-5022: Due to the order could not be executed as maker"))
    manager, store = _manager(tmp_path, engine)

    result = manager.submit_plan(
        _plan(), "BUY", None, RiskContext(equity=1000), allow_live=True, confirm="ABC", post_only=True
    )

    assert result["rejected"] is True
    assert result["post_only_rejected"] is True
    assert "Post Only" in result["message"]
    assert store.get_order(result["clientOrderId"])["status"] == "REJECTED"


def test_order_manager_queries_exchange_after_submit_timeout(tmp_path) -> None:
    query = {
        "symbol": "UNIUSDT",
        "orderId": 9,
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "20",
        "executedQty": "20",
        "price": "10",
        "status": "FILLED",
    }
    engine = FakeEngine(submit_error=TimeoutError("network timeout"), query_result=query)
    manager, store = _manager(tmp_path, engine)

    result = manager.submit_plan(
        _plan(),
        "BUY",
        None,
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="TIMEOUT",
    )

    assert result["status"] == "FILLED"
    assert engine.query_calls == 1
    assert store.get_order("TIMEOUT")["status"] == "FILLED"


def test_order_manager_submits_stop_and_split_targets_as_reduce_only(tmp_path) -> None:
    engine = FakeEngine()
    manager, _ = _manager(tmp_path, engine)

    results = manager.submit_protective_orders(
        _plan(),
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        parent_client_order_id="BTA_PARENT",
    )

    assert len(results) == 3
    assert engine.submit_calls == 3
    assert engine.payloads[0]["type"] == "STOP_MARKET"
    assert engine.payloads[0]["reduceOnly"] is True
    assert all("newClientOrderId" in payload for payload in engine.payloads)


def test_order_manager_replaces_existing_protective_stop(tmp_path) -> None:
    engine = FakeEngine()
    manager, _ = _manager(tmp_path, engine)
    context = RiskContext(equity=1000)
    manager.submit_protective_orders(
        _plan(),
        context,
        allow_live=True,
        confirm="ABC",
        parent_client_order_id="BTA_PARENT",
    )

    result = manager.replace_protective_stop(
        market="futures",
        symbol="UNIUSDT",
        side="SELL",
        quantity=20,
        stop_price=10,
        context=context,
        allow_live=True,
        confirm="ABC",
    )

    assert engine.cancel_calls == 1
    assert engine.payloads[-1]["type"] == "STOP_MARKET"
    assert engine.payloads[-1]["stopPrice"] == 10
    assert result["managed_status"] == "NEW"


def test_replace_protective_stop_treats_unknown_old_order_as_already_canceled(tmp_path) -> None:
    class UnknownCancelEngine(FakeEngine):
        def cancel(self, market, symbol, client_order_id, **kwargs):
            raise RuntimeError("Binance 撤单接口 HTTP 400：-2011: Unknown order sent.")

    engine = UnknownCancelEngine()
    manager, store = _manager(tmp_path, engine)
    context = RiskContext(equity=1000)
    manager.submit_protective_orders(
        _plan(), context, allow_live=True, confirm="ABC", parent_client_order_id="BTA_PARENT"
    )

    result = manager.replace_protective_stop(
        market="futures", symbol="UNIUSDT", side="SELL", quantity=20, stop_price=10,
        context=context, allow_live=True, confirm="ABC"
    )

    assert result["managed_status"] == "NEW"
    assert any(order["status"] == "CANCELED" for order in store.list_orders({"CANCELED"}))


def test_recover_active_orders_marks_missing_exchange_order_as_canceled(tmp_path) -> None:
    class MissingOrderEngine(FakeEngine):
        def query(self, market, symbol, client_order_id, **kwargs):
            raise RuntimeError("Binance 查询订单接口 HTTP 400：-2013: Order does not exist.")

    manager, store = _manager(tmp_path, MissingOrderEngine())
    manager.submit_raw(
        market="futures",
        symbol="ZECUSDT",
        side="SELL",
        quantity=1,
        order_type="STOP_MARKET",
        price=None,
        stop_price=30,
        reduce_only=True,
        context=RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        strategy="protective_exit",
        risk_checked=True,
    )

    recovered = manager.recover_active_orders()

    assert recovered[0]["status"] == "CANCELED"
    assert store.get_order(recovered[0]["client_order_id"])["status"] == "CANCELED"


def test_protective_algo_order_uses_algo_api_for_recovery_and_cancel(tmp_path) -> None:
    class AlgoEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.query_modes = []
            self.cancel_modes = []

        def query(self, market, symbol, client_order_id, *, algo=False, exchange_order_id=None):
            self.query_modes.append((algo, exchange_order_id))
            return {
                "symbol": symbol,
                "clientAlgoId": client_order_id,
                "algoId": exchange_order_id or "9001",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "20",
                "triggerPrice": "9.5",
                "algoStatus": "NEW",
            }

        def cancel(self, market, symbol, client_order_id, *, algo=False, exchange_order_id=None):
            self.cancel_modes.append((algo, exchange_order_id))
            return {
                "symbol": symbol,
                "clientAlgoId": client_order_id,
                "algoId": exchange_order_id or "9001",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "20",
                "triggerPrice": "9.5",
                "algoStatus": "CANCELED",
            }

    engine = AlgoEngine()
    manager, store = _manager(tmp_path, engine)
    stop = manager.submit_raw(
        market="futures",
        symbol="UNIUSDT",
        side="SELL",
        quantity=20,
        order_type="STOP_MARKET",
        price=None,
        stop_price=9.5,
        reduce_only=True,
        context=RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        strategy="protective_exit",
        client_order_id="ALGO_STOP",
        risk_checked=True,
    )
    stored = store.get_order(stop["clientOrderId"])
    stored["exchange_order_id"] = "9001"
    store.upsert_order(stored)

    recovered = manager.recover_active_orders()
    manager.replace_protective_stop(
        market="futures",
        symbol="UNIUSDT",
        side="SELL",
        quantity=20,
        stop_price=9.7,
        context=RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
    )

    assert recovered[0]["order_type"] == "STOP_MARKET"
    assert recovered[0]["stop_price"] == 9.5
    assert engine.query_modes == [(True, "9001")]
    assert engine.cancel_modes == [(True, "9001")]


def test_order_manager_normalizes_quantity_and_price_before_submit(tmp_path) -> None:
    class PrecisionClient:
        def public_get(self, market, path, params=None):
            return {
                "symbols": [
                    {
                        "symbol": "UNIUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                        ],
                    }
                ]
            }

    engine = FakeEngine()
    engine.client = PrecisionClient()
    manager, _ = _manager(tmp_path, engine)

    result = manager.submit_raw(
        market="futures",
        symbol="UNIUSDT",
        side="BUY",
        quantity=1.23999,
        order_type="LIMIT",
        price=10.12345,
        reduce_only=False,
        context=RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        strategy="test",
        risk_checked=True,
    )

    assert result["managed_status"] == "NEW"
    assert engine.payloads[0]["quantity"] == "1.23"
    assert engine.payloads[0]["price"] == "10.123"


def test_order_manager_rejects_below_min_notional_before_submit(tmp_path) -> None:
    class PrecisionClient:
        def public_get(self, market, path, params=None):
            return {
                "symbols": [
                    {
                        "symbol": "PEPEUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.000001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            }

    engine = FakeEngine()
    engine.client = PrecisionClient()
    manager, _ = _manager(tmp_path, engine)

    try:
        manager.submit_raw(
            market="futures",
            symbol="PEPEUSDT",
            side="BUY",
            quantity=1000,
            order_type="LIMIT",
            price=0.001,
            reduce_only=False,
            context=RiskContext(equity=1000),
            allow_live=True,
            confirm="ABC",
            strategy="test",
            risk_checked=True,
        )
    except ValueError as exc:
        assert "低于交易所最小值 5 USDT" in str(exc)
    else:
        raise AssertionError("below min notional order should fail before submit")
    assert engine.submit_calls == 0


def test_order_manager_adds_position_side_for_hedge_mode_open_order(tmp_path) -> None:
    class HedgeClient:
        def futures_position_mode(self):
            return {"dualSidePosition": True}

        def public_get(self, market, path, params=None):
            return {
                "symbols": [
                    {
                        "symbol": "UNIUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                        ],
                    }
                ]
            }

    engine = FakeEngine()
    engine.client = HedgeClient()
    manager, _ = _manager(tmp_path, engine)

    manager.submit_plan(
        _plan(),
        "BUY",
        None,
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="HEDGE_OPEN",
    )

    assert engine.payloads[0]["positionSide"] == "LONG"


def test_order_manager_uses_position_side_for_hedge_mode_reduce_order(tmp_path) -> None:
    class HedgeClient:
        def futures_position_mode(self):
            return {"dualSidePosition": True}

        def public_get(self, market, path, params=None):
            return {
                "symbols": [
                    {
                        "symbol": "UNIUSDT",
                        "filters": [
                            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        ],
                    }
                ]
            }

    engine = FakeEngine()
    engine.client = HedgeClient()
    manager, _ = _manager(tmp_path, engine)

    manager.submit_reduce(
        market="futures",
        symbol="UNIUSDT",
        side="SELL",
        quantity=0.2,
        context=RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="HEDGE_REDUCE",
    )

    assert engine.payloads[0]["positionSide"] == "LONG"
    assert "reduceOnly" not in engine.payloads[0]


def test_order_manager_bumps_small_account_plan_to_min_notional_after_precision(tmp_path) -> None:
    class PrecisionClient:
        def public_get(self, market, path, params=None):
            return {
                "symbols": [
                    {
                        "symbol": "UNIUSDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            }

    engine = FakeEngine()
    engine.client = PrecisionClient()
    manager, _ = _manager(tmp_path, engine)
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10, 9.72, 11, 14.4, 0.7, 2)

    manager.submit_plan(
        plan,
        "BUY",
        None,
        RiskContext(equity=18),
        allow_live=True,
        confirm="ABC",
        client_order_id="MICRO_BUMP",
    )

    assert float(engine.payloads[0]["quantity"]) * float(engine.payloads[0]["price"]) >= 5


def test_order_manager_cancels_stale_entry_order_before_waiting_forever(tmp_path) -> None:
    engine = FakeEngine()
    manager, store = _manager(tmp_path, engine)
    manager.submit_plan(
        _plan(),
        "BUY",
        None,
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="STALE_ENTRY",
    )

    result = manager.manage_entry_orders(
        [SimpleNamespace(symbol="UNIUSDT", side="long", score=86, last=10.0)],
        max_wait_seconds=0,
    )

    assert result is not None
    assert result["action"] == "monitoring"
    assert "等待成交超过" in result["message"]
    assert result["canceled_symbols"] == ("UNIUSDT",)
    assert engine.cancel_calls == 1
    assert store.get_order("STALE_ENTRY")["status"] == "CANCELED"


def test_order_manager_keeps_fresh_entry_order_when_signal_is_still_valid(tmp_path) -> None:
    engine = FakeEngine()
    manager, _ = _manager(tmp_path, engine)
    manager.submit_plan(
        _plan(),
        "BUY",
        None,
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="FRESH_ENTRY",
    )

    result = manager.manage_entry_orders(
        [SimpleNamespace(symbol="UNIUSDT", side="long", score=86, last=10.0)],
        max_wait_seconds=999,
    )

    assert result is not None
    assert result["action"] == "monitoring"
    assert result["symbols"] == ("UNIUSDT",)
    assert engine.cancel_calls == 0


def test_order_manager_cancels_entry_order_when_signal_score_drops(tmp_path) -> None:
    engine = FakeEngine()
    manager, _ = _manager(tmp_path, engine)
    manager.submit_plan(
        _plan(),
        "BUY",
        None,
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="WEAK_ENTRY",
    )

    result = manager.manage_entry_orders(
        [SimpleNamespace(symbol="UNIUSDT", side="long", score=55, last=10.0)],
        max_wait_seconds=999,
    )

    assert result is not None
    assert result["action"] == "monitoring"
    assert "低于挂单保留线" in result["message"]
    assert result["canceled_symbols"] == ("UNIUSDT",)


def test_order_manager_prevents_same_symbol_same_side_duplicate_even_with_new_id(tmp_path) -> None:
    engine = FakeEngine()
    manager, _ = _manager(tmp_path, engine)
    manager.submit_plan(
        _plan(),
        "BUY",
        None,
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="FIRST_UNI",
    )

    result = manager.submit_plan(
        _plan(),
        "BUY",
        None,
        RiskContext(equity=1000),
        allow_live=True,
        confirm="ABC",
        client_order_id="SECOND_UNI",
    )

    assert result["duplicate"] is True
    assert result["managed_order"]["client_order_id"] == "FIRST_UNI"
    assert engine.submit_calls == 1


def test_order_manager_monitors_all_automatic_entries_without_stopping_at_first(tmp_path) -> None:
    engine = FakeEngine()
    manager, _ = _manager(tmp_path, engine)
    context = RiskContext(equity=1000)
    manager.submit_plan(
        _plan(),
        "BUY",
        None,
        context,
        allow_live=True,
        confirm="ABC",
        client_order_id="UNI_ENTRY",
    )
    second_plan = create_trade_plan("LINKUSDT", "futures", "long", 10, 9.5, 11, 1000, 1, 2)
    manager.submit_plan(
        second_plan,
        "BUY",
        None,
        context,
        allow_live=True,
        confirm="ABC",
        client_order_id="LINK_ENTRY",
    )

    result = manager.manage_entry_orders(
        [
            SimpleNamespace(symbol="UNIUSDT", side="long", score=86, last=10.0),
            SimpleNamespace(symbol="LINKUSDT", side="long", score=86, last=10.0),
        ],
        max_wait_seconds=999,
    )

    assert result is not None
    assert result["action"] == "monitoring"
    assert set(result["symbols"]) == {"UNIUSDT", "LINKUSDT"}
    assert engine.query_calls == 2
