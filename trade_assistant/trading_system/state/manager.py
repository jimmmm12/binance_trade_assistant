from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ...models import FuturesAccountRisk
from ..core.events import EventBus, EventType, TradingEvent
from ..storage.database import TradingDatabase
from .models import TradingState

POSITION_EPSILON = 1e-8


class StateManager:
    STATE_KEY = "trading_state"

    def __init__(self, store: TradingDatabase, event_bus: EventBus | None = None) -> None:
        self.store = store
        self.event_bus = event_bus or EventBus()
        self._lock = threading.RLock()
        saved = self.store.load_state(self.STATE_KEY)
        self._state = TradingState.from_dict(saved) if saved else TradingState()

    def snapshot(self) -> TradingState:
        with self._lock:
            return TradingState.from_dict(json.loads(json.dumps(self._state.to_dict())))

    def reconcile_futures_account(self, account: FuturesAccountRisk, source: str = "binance_rest") -> list[str]:
        exchange_positions = {
            f"{position.market}:{position.symbol}": asdict(position)
            for position in account.positions
            if abs(position.quantity) > POSITION_EPSILON
        }
        mismatches: list[str] = []
        with self._lock:
            previous_real = {
                key: value for key, value in self._state.positions.items() if value.get("source") == "real"
            }
            for key in sorted(set(previous_real) | set(exchange_positions)):
                old = previous_real.get(key)
                new = exchange_positions.get(key)
                if not _same_position(old, new):
                    mismatches.append(_mismatch_text(key, old, new))
            retained = {
                key: value for key, value in self._state.positions.items() if value.get("source") != "real"
            }
            self._state.positions = {**retained, **exchange_positions}
            self._state.account = {
                "wallet_balance": account.wallet_balance,
                "available_balance": account.available_balance,
                "total_unrealized_pnl": account.total_unrealized_pnl,
                "equity": account.wallet_balance + account.total_unrealized_pnl,
                "source": source,
                "updated_at": _now(),
            }
            self._state.sync_status = {
                "source": source,
                "healthy": True,
                "last_success": _now(),
                "mismatches": mismatches,
            }
            self._persist_locked()
        self.store.save_snapshot(source, self._state.to_dict())
        self.event_bus.publish(
            TradingEvent(EventType.ACCOUNT, source, {"mismatches": mismatches, "account": self._state.account})
        )
        return mismatches

    def reconcile_spot_account(
        self,
        account_payload: dict[str, Any],
        symbol: str,
        mark_price: float,
        source: str = "binance_rest",
    ) -> list[str]:
        symbol = symbol.upper()
        base_asset = symbol.removesuffix("USDT")
        balances = {row.get("asset"): row for row in account_payload.get("balances", [])}
        usdt = balances.get("USDT", {})
        base = balances.get(base_asset, {})
        usdt_balance = float(usdt.get("free", 0)) + float(usdt.get("locked", 0))
        quantity = float(base.get("free", 0)) + float(base.get("locked", 0))
        key = f"spot:{symbol}"
        with self._lock:
            previous = self._state.positions.get(key)
            current = None
            if abs(quantity) > POSITION_EPSILON:
                current = {
                    "source": "real",
                    "market": "spot",
                    "symbol": symbol,
                    "side": "long",
                    "quantity": quantity,
                    "entry_price": float(previous.get("entry_price", 0)) if previous else 0.0,
                    "mark_price": mark_price,
                    "notional": quantity * mark_price,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "leverage": 1.0,
                    "updated_at": _now(),
                }
            mismatches = [] if _same_position(previous, current) else [_mismatch_text(key, previous, current)]
            if current is None:
                self._state.positions.pop(key, None)
            else:
                self._state.positions[key] = current
            self._state.account = {
                "wallet_balance": usdt_balance,
                "available_balance": float(usdt.get("free", 0)),
                "total_unrealized_pnl": 0.0,
                "equity": usdt_balance + quantity * mark_price,
                "source": source,
                "updated_at": _now(),
            }
            self._state.sync_status = {
                "source": source,
                "healthy": True,
                "last_success": _now(),
                "mismatches": mismatches,
            }
            self._persist_locked()
        self.store.save_snapshot(source, self._state.to_dict())
        return mismatches

    def upsert_position_snapshot(self, position: dict[str, Any], source: str = "manual_sync") -> None:
        market = str(position.get("market") or "futures")
        symbol = str(position.get("symbol") or "").upper()
        if not symbol:
            return
        key = f"{market}:{symbol}"
        quantity = float(position.get("quantity") or 0)
        side = str(position.get("side") or "flat")
        with self._lock:
            if abs(quantity) <= POSITION_EPSILON or side == "flat":
                self._state.positions.pop(key, None)
            else:
                self._state.positions[key] = {**position, "symbol": symbol, "market": market, "updated_at": _now()}
            self._state.sync_status = {
                **self._state.sync_status,
                "source": source,
                "healthy": True,
                "last_success": _now(),
            }
            self._persist_locked()
        self.event_bus.publish(TradingEvent(EventType.POSITION, source, {"position": position}))

    def apply_order_update(self, payload: dict[str, Any], source: str = "binance") -> dict[str, Any]:
        order = _normalize_order(payload)
        if not order["client_order_id"]:
            return order
        with self._lock:
            self._state.orders[order["client_order_id"]] = order
            self._persist_locked()
        self.store.upsert_order(order)
        self.store.append_order_event(order["client_order_id"], "exchange_update", payload)
        self.event_bus.publish(TradingEvent(EventType.ORDER, source, order))
        return order

    def apply_futures_account_event(self, payload: dict[str, Any]) -> None:
        account_update = payload.get("a") if isinstance(payload.get("a"), dict) else {}
        balances = account_update.get("B", [])
        positions = account_update.get("P", [])
        with self._lock:
            usdt = next((item for item in balances if item.get("a") == "USDT"), None)
            if usdt is not None:
                wallet_balance = float(usdt.get("wb", self._state.account.get("wallet_balance", 0)))
                cross_wallet = float(usdt.get("cw", wallet_balance))
                self._state.account = {
                    **self._state.account,
                    "wallet_balance": wallet_balance,
                    "available_balance": cross_wallet,
                    "source": "binance_user_stream",
                    "updated_at": _now(),
                }
            for item in positions:
                symbol = str(item.get("s", "")).upper()
                if not symbol:
                    continue
                key = f"futures:{symbol}"
                quantity_signed = float(item.get("pa", 0))
                if abs(quantity_signed) <= POSITION_EPSILON:
                    self._state.positions.pop(key, None)
                    continue
                previous = self._state.positions.get(key, {})
                quantity = abs(quantity_signed)
                entry = float(item.get("ep", 0))
                mark = float(previous.get("mark_price") or entry)
                self._state.positions[key] = {
                    **previous,
                    "source": "real",
                    "market": "futures",
                    "symbol": symbol,
                    "side": "long" if quantity_signed > 0 else "short",
                    "quantity": quantity,
                    "entry_price": entry,
                    "mark_price": mark,
                    "notional": quantity * mark,
                    "unrealized_pnl": float(item.get("up", previous.get("unrealized_pnl", 0))),
                    "realized_pnl": float(item.get("cr", previous.get("realized_pnl", 0))),
                    "margin_type": item.get("mt", previous.get("margin_type")),
                    "isolated_margin": _float_or_none(item.get("iw")),
                    "updated_at": _now(),
                }
            self._state.sync_status = {
                **self._state.sync_status,
                "source": "binance_user_stream",
                "healthy": True,
                "last_success": _now(),
            }
            self._persist_locked()
        self.event_bus.publish(
            TradingEvent(EventType.POSITION, "binance_user_stream", {"positions": positions})
        )

    def apply_spot_account_event(self, payload: dict[str, Any]) -> None:
        updates = payload.get("B", [])
        if not updates:
            return
        with self._lock:
            for item in updates:
                asset = str(item.get("a", ""))
                quantity = float(item.get("f", 0)) + float(item.get("l", 0))
                if asset == "USDT":
                    self._state.account = {
                        **self._state.account,
                        "wallet_balance": quantity,
                        "available_balance": float(item.get("f", 0)),
                        "source": "binance_user_stream",
                        "updated_at": _now(),
                    }
                    continue
                for key, position in list(self._state.positions.items()):
                    if position.get("market") != "spot" or position.get("symbol") != f"{asset}USDT":
                        continue
                    if abs(quantity) <= POSITION_EPSILON:
                        self._state.positions.pop(key, None)
                    else:
                        mark = float(position.get("mark_price", 0))
                        self._state.positions[key] = {
                            **position,
                            "quantity": quantity,
                            "notional": quantity * mark,
                            "updated_at": _now(),
                        }
            self._state.sync_status = {
                **self._state.sync_status,
                "source": "binance_user_stream",
                "healthy": True,
                "last_success": _now(),
            }
            self._persist_locked()

    def set_risk_status(self, status: dict[str, Any], source: str = "risk_manager") -> None:
        with self._lock:
            self._state.risk_status = {**status, "updated_at": _now()}
            self._persist_locked()
        self.event_bus.publish(TradingEvent(EventType.RISK, source, self._state.risk_status))

    def set_market_regime(self, symbol: str, regime: dict[str, Any]) -> None:
        with self._lock:
            self._state.market_regime[symbol.upper()] = {**regime, "updated_at": _now()}
            self._persist_locked()

    def set_sync_error(self, message: str, source: str) -> None:
        with self._lock:
            self._state.sync_status = {
                **self._state.sync_status,
                "source": source,
                "healthy": False,
                "last_error": message,
                "updated_at": _now(),
            }
            self._persist_locked()
        self.event_bus.publish(TradingEvent(EventType.SYSTEM, source, {"error": message}))

    def set_performance(self, performance: dict[str, Any]) -> None:
        with self._lock:
            self._state.performance = performance
            self._persist_locked()

    def set_automation_state(self, automation: dict[str, Any]) -> None:
        with self._lock:
            self._state.automation = {**automation, "updated_at": _now()}
            self._persist_locked()

    def _persist_locked(self) -> None:
        self._state.updated_at = _now()
        self.store.save_state(self.STATE_KEY, self._state.to_dict())


def _normalize_order(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("o") if isinstance(payload.get("o"), dict) else payload
    client_order_id = str(
        nested.get("clientOrderId")
        or nested.get("clientAlgoId")
        or nested.get("c")
        or nested.get("client_order_id")
        or ""
    )
    return {
        "client_order_id": client_order_id,
        "exchange_order_id": str(nested.get("orderId") or nested.get("algoId") or nested.get("i") or nested.get("exchange_order_id") or ""),
        "market": str(nested.get("market") or payload.get("market") or "futures"),
        "symbol": str(nested.get("symbol") or nested.get("s") or "").upper(),
        "side": str(nested.get("side") or nested.get("S") or "").upper(),
        "order_type": str(nested.get("type") or nested.get("orderType") or nested.get("o") or nested.get("order_type") or "MARKET").upper(),
        "quantity": float(nested.get("origQty") or nested.get("q") or nested.get("quantity") or 0),
        "price": _float_or_none(nested.get("price") or nested.get("p")),
        "stop_price": _float_or_none(nested.get("stopPrice") or nested.get("triggerPrice") or nested.get("sp")),
        "filled_quantity": float(nested.get("executedQty") or nested.get("z") or nested.get("filled_quantity") or 0),
        "status": str(nested.get("status") or nested.get("algoStatus") or nested.get("X") or "UNKNOWN").upper(),
        "strategy": str(nested.get("strategy") or "automatic"),
        "reduce_only": bool(nested.get("reduceOnly") or nested.get("R") or False),
        "attempts": int(nested.get("attempts") or 1),
        "last_error": nested.get("last_error"),
        "raw_payload": payload,
        "created_at": str(nested.get("created_at") or _now()),
        "updated_at": _now(),
    }


def _same_position(old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    if old is None or new is None:
        return old is new
    return (
        old.get("side") == new.get("side")
        and abs(float(old.get("quantity", 0)) - float(new.get("quantity", 0))) < 1e-10
        and abs(float(old.get("entry_price", 0)) - float(new.get("entry_price", 0))) < 1e-8
    )


def _mismatch_text(key: str, old: dict[str, Any] | None, new: dict[str, Any] | None) -> str:
    old_text = "无" if old is None else f"{old.get('side')} {old.get('quantity')}"
    new_text = "无" if new is None else f"{new.get('side')} {new.get('quantity')}"
    return f"{key} 本地={old_text} Binance={new_text}，已以 Binance 为准"


def _float_or_none(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")
