from __future__ import annotations

import itertools
import math
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ...broker import build_order_payload
from ...models import TradePlan
from ...order_precision import normalize_order_payload, symbol_filters
from ...order_brackets import build_exit_order_drafts
from ...risk_engine import PlanRiskReview
from ..core.events import EventBus, EventType, TradingEvent
from ..risk.manager import RiskContext, RiskManager
from ..state.manager import StateManager
from ..storage.database import TradingDatabase
from .engine import BinanceExecutionEngine


ACTIVE_STATUSES = {"CREATED", "SUBMITTING", "NEW", "PARTIALLY_FILLED", "UNKNOWN"}
TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "DRY_RUN", "REJECTED_BY_RISK"}


class OrderRejectedError(RuntimeError):
    pass


@dataclass(frozen=True)
class PartialFillPolicy:
    action: str = "wait"
    timeout_seconds: int = 30


class OrderManager:
    def __init__(
        self,
        store: TradingDatabase,
        state_manager: StateManager,
        risk_manager: RiskManager,
        engine: BinanceExecutionEngine,
        event_bus: EventBus | None = None,
        partial_fill_policy: PartialFillPolicy | None = None,
    ) -> None:
        self.store = store
        self.state_manager = state_manager
        self.risk_manager = risk_manager
        self.engine = engine
        self.event_bus = event_bus or EventBus()
        self.partial_fill_policy = partial_fill_policy or PartialFillPolicy()
        self._counter = itertools.count(1)

    def submit_plan(
        self,
        plan: TradePlan,
        side: str,
        review: PlanRiskReview | None,
        context: RiskContext,
        *,
        allow_live: bool,
        confirm: str,
        strategy: str = "automatic",
        order_type: str = "LIMIT",
        client_order_id: str | None = None,
        post_only: bool = False,
    ) -> dict[str, Any]:
        decision = self.risk_manager.authorize_plan(plan, review, context)
        order_id = client_order_id or self.new_client_order_id(plan.symbol, side)
        if not decision.allowed:
            order = self._base_order(
                order_id,
                plan.market,
                plan.symbol,
                side,
                order_type,
                plan.quantity,
                plan.entry,
                strategy,
                False,
                "REJECTED_BY_RISK",
            )
            order["last_error"] = decision.message
            self.store.upsert_order(order)
            self.store.append_order_event(order_id, "risk_rejected", {"code": decision.code, "message": decision.message})
            raise OrderRejectedError(decision.message)
        existing = self.store.get_order(order_id)
        if existing is not None:
            return {"duplicate": True, "managed_order": existing}
        active_same_side = self.store.find_active_opening_order(
            plan.market,
            plan.symbol,
            side,
            ACTIVE_STATUSES,
        )
        if active_same_side is not None:
            return {
                "duplicate": True,
                "managed_order": active_same_side,
                "message": (
                    f"{plan.symbol.upper()} 同方向活动开仓单已存在 "
                    f"({active_same_side['client_order_id']})，本次未重复下单"
                ),
            }
        if plan.market == "futures":
            try:
                self._set_and_verify_futures_leverage(plan)
            except Exception as exc:
                order = self._base_order(
                    order_id,
                    plan.market,
                    plan.symbol,
                    side,
                    order_type,
                    plan.quantity,
                    plan.entry,
                    strategy,
                    False,
                    "REJECTED",
                )
                order["last_error"] = f"杠杆设置失败：{exc}"
                self.store.upsert_order(order)
                self.store.append_order_event(order_id, "leverage_rejected", {"error": str(exc)})
                raise OrderRejectedError(f"{plan.symbol} 杠杆设置/验证失败，已拒绝下单：{exc}") from exc
        allow_min_notional_bump = self._can_bump_to_min_notional(plan, context)
        quantity = round(plan.quantity * decision.quantity_multiplier, 8)
        return self.submit_raw(
            market=plan.market,
            symbol=plan.symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=plan.entry if order_type == "LIMIT" else None,
            reduce_only=False,
            context=context,
            allow_live=allow_live,
            confirm=confirm,
            strategy=strategy,
            client_order_id=order_id,
            risk_checked=True,
            allow_min_notional_bump=allow_min_notional_bump,
            post_only=post_only,
        )

    def _set_and_verify_futures_leverage(self, plan: TradePlan) -> None:
        desired = max(1, int(math.floor(plan.leverage)))
        response = self.engine.set_futures_leverage(plan.symbol, desired)
        actual = response.get("leverage") if isinstance(response, dict) else None
        if actual in (None, ""):
            return
        if int(float(actual)) != desired:
            raise RuntimeError(f"Binance 返回 {actual}x，计划要求 {desired}x")

    def submit_reduce(
        self,
        *,
        market: str,
        symbol: str,
        side: str,
        quantity: float,
        context: RiskContext,
        allow_live: bool,
        confirm: str,
        strategy: str = "position_manager",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        decision = self.risk_manager.authorize_reduce(context)
        if not decision.allowed:
            raise OrderRejectedError(decision.message)
        return self.submit_raw(
            market=market,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="MARKET",
            price=None,
            reduce_only=market == "futures",
            context=context,
            allow_live=allow_live,
            confirm=confirm,
            strategy=strategy,
            client_order_id=client_order_id,
            risk_checked=True,
        )

    def submit_raw(
        self,
        *,
        market: str,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None,
        reduce_only: bool,
        context: RiskContext,
        allow_live: bool,
        confirm: str,
        strategy: str,
        client_order_id: str | None = None,
        risk_checked: bool = False,
        stop_price: float | None = None,
        post_only: bool = False,
        allow_min_notional_bump: bool = False,
    ) -> dict[str, Any]:
        if quantity <= 0:
            raise ValueError("order quantity must be positive")
        if not risk_checked and not reduce_only:
            raise OrderRejectedError("开仓订单必须先通过 RiskManager")
        order_id = client_order_id or self.new_client_order_id(symbol, side)
        existing = self.store.get_order(order_id)
        if existing is not None:
            return {"duplicate": True, "managed_order": existing}
        if not reduce_only:
            active_same_side = self.store.find_active_opening_order(
                market,
                symbol,
                side,
                ACTIVE_STATUSES,
            )
            if active_same_side is not None:
                return {
                    "duplicate": True,
                    "managed_order": active_same_side,
                    "message": (
                        f"{symbol.upper()} 同方向活动开仓单已存在 "
                        f"({active_same_side['client_order_id']})，本次未重复下单"
                    ),
                }

        payload = build_order_payload(
            symbol,
            side,
            quantity,
            order_type,
            price,
            reduce_only,
            stop_price=stop_price,
            post_only=post_only,
            client_order_id=order_id,
            market=market,
        )
        if reduce_only:
            payload["_btaReduceOnlyIntent"] = True
        client = getattr(self.engine, "client", None)
        payload = self._apply_futures_position_side(client, market, payload, reduce_only)
        payload = normalize_order_payload(
            client,
            market,
            payload,
            allow_min_notional_bump=allow_min_notional_bump,
        )
        payload.pop("_btaReduceOnlyIntent", None)
        quantity = float(payload["quantity"])
        if payload.get("price") not in (None, ""):
            price = float(payload["price"])
        if payload.get("stopPrice") not in (None, ""):
            stop_price = float(payload["stopPrice"])
        order = self._base_order(
            order_id,
            market,
            symbol,
            side,
            order_type,
            quantity,
            price,
            strategy,
            reduce_only,
            "SUBMITTING",
        )
        order["attempts"] = 1
        order["stop_price"] = stop_price
        order["raw_payload"] = payload
        self.store.upsert_order(order)
        self.store.append_order_event(order_id, "submit_attempt", payload)
        try:
            response = self.engine.submit(market, payload, allow_live, confirm)
        except (TimeoutError, urllib.error.URLError) as exc:
            return self._resolve_uncertain(order, str(exc))
        except Exception as exc:
            order["status"] = "REJECTED"
            order["last_error"] = str(exc)
            order["updated_at"] = _now()
            self.store.upsert_order(order)
            self.store.append_order_event(order_id, "submit_error", {"error": str(exc)})
            if post_only and _is_post_only_rejection(exc):
                message = (
                    f"{symbol.upper()} Post Only 未成交：盘口变化会导致立即吃单，"
                    "Binance 已拒绝且未记录该订单；本轮将继续检查下一候选"
                )
                self.store.append_order_event(order_id, "post_only_rejected", {"error": str(exc), "message": message})
                return {
                    "rejected": True,
                    "post_only_rejected": True,
                    "message": message,
                    "last_error": str(exc),
                    "clientOrderId": order_id,
                    "managed_status": "REJECTED",
                }
            raise

        if response.get("dry_run"):
            order["status"] = "DRY_RUN"
            order["raw_payload"] = response
            order["updated_at"] = _now()
            self.store.upsert_order(order)
            return {**response, "clientOrderId": order_id, "managed_status": "DRY_RUN"}
        managed = self._apply_exchange_response(order, response)
        return {**response, "managed_status": managed["status"], "clientOrderId": order_id}
    def _can_bump_to_min_notional(self, plan: TradePlan, context: RiskContext) -> bool:
        if plan.market != "futures" or context.equity <= 0 or plan.entry <= 0:
            return False
        rules = symbol_filters(getattr(self.engine, "client", None), plan.market, plan.symbol)
        if rules is None:
            return False
        try:
            min_notional = float(rules.get("min_notional") or 0)
        except ValueError:
            return False
        if min_notional <= 0:
            return False
        required_notional = min_notional * 1.04
        account_risk_pct = required_notional * plan.loss_pct_to_stop / 100 / context.equity * 100
        projected_symbol = context.symbol_exposure + required_notional
        if context.risk_line == "aggressive":
            risk_limit = self.risk_manager.limits.aggressive_max_single_risk_pct
            symbol_limit = self.risk_manager.limits.aggressive_max_symbol_exposure_pct
        else:
            risk_limit = self.risk_manager.limits.max_single_risk_pct
            symbol_limit = self.risk_manager.limits.max_symbol_exposure_pct
        return (
            account_risk_pct <= risk_limit + 1e-9
            and projected_symbol <= context.equity * symbol_limit / 100
        )

    def _apply_futures_position_side(
        self,
        client: Any,
        market: str,
        payload: dict[str, Any],
        reduce_only: bool,
    ) -> dict[str, Any]:
        if market != "futures" or not hasattr(client, "futures_position_mode"):
            return payload
        try:
            mode = client.futures_position_mode()
        except Exception:
            return payload
        dual = bool(mode.get("dualSidePosition")) if isinstance(mode, dict) else False
        normalized = dict(payload)
        if not dual:
            normalized.pop("positionSide", None)
            return normalized
        side = str(normalized.get("side", "")).upper()
        if reduce_only:
            normalized["positionSide"] = "LONG" if side == "SELL" else "SHORT"
            normalized.pop("reduceOnly", None)
        else:
            normalized["positionSide"] = "LONG" if side == "BUY" else "SHORT"
        return normalized

    def submit_protective_orders(
        self,
        plan: TradePlan,
        context: RiskContext,
        *,
        allow_live: bool,
        confirm: str,
        parent_client_order_id: str,
    ) -> list[dict[str, Any]]:
        decision = self.risk_manager.authorize_reduce(context)
        if not decision.allowed:
            raise OrderRejectedError(decision.message)
        results: list[dict[str, Any]] = []
        for index, draft in enumerate(build_exit_order_drafts(plan)):
            client_id = f"{parent_client_order_id[:31]}P{index}"[:36]
            results.append(
                self.submit_raw(
                    market=plan.market,
                    symbol=plan.symbol,
                    side=str(draft["side"]),
                    quantity=float(draft["quantity"]),
                    order_type=str(draft["type"]),
                    price=None,
                    stop_price=float(draft["stopPrice"]),
                    reduce_only=True,
                    context=context,
                    allow_live=allow_live,
                    confirm=confirm,
                    strategy="protective_exit",
                    client_order_id=client_id,
                    risk_checked=True,
                )
            )
        return results

    def replace_protective_stop(
        self,
        *,
        market: str,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        context: RiskContext,
        allow_live: bool,
        confirm: str,
    ) -> dict[str, Any]:
        decision = self.risk_manager.authorize_reduce(context)
        if not decision.allowed:
            raise OrderRejectedError(decision.message)
        for order in self.store.list_orders(ACTIVE_STATUSES):
            raw = order.get("raw_payload") or {}
            if (
                order["market"] == market
                and order["symbol"] == symbol.upper()
                and order.get("strategy") == "protective_exit"
                and str(raw.get("type") or order.get("order_type")) == "STOP_MARKET"
            ):
                try:
                    response = self._cancel_exchange_order(order)
                    self._apply_exchange_response(order, response)
                except Exception as exc:
                    if not self._is_unknown_order_error(exc):
                        raise
                    self._mark_canceled_locally(order, "交易所未找到旧止损单，按已撤销处理")
        return self.submit_raw(
            market=market,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="STOP_MARKET",
            price=None,
            stop_price=stop_price,
            reduce_only=True,
            context=context,
            allow_live=allow_live,
            confirm=confirm,
            strategy="protective_exit",
            risk_checked=True,
        )

    def recover_active_orders(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        for order in self.store.list_orders(ACTIVE_STATUSES):
            try:
                payload = self._query_exchange_order(order)
                recovered.append(self._apply_exchange_response(order, payload))
            except Exception as exc:
                if self._is_unknown_order_error(exc):
                    self._mark_canceled_locally(order, "交易所未找到活动订单，按已撤销处理")
                    recovered.append(order)
                    continue
                order["status"] = "UNKNOWN"
                order["last_error"] = str(exc)
                order["updated_at"] = _now()
                self.store.upsert_order(order)
                recovered.append(order)
        return recovered

    def monitor_active_orders(self) -> list[dict[str, Any]]:
        monitored = self.recover_active_orders()
        if self.partial_fill_policy.action != "cancel":
            return monitored
        now = datetime.now()
        for order in monitored:
            if order["status"] != "PARTIALLY_FILLED":
                continue
            updated = datetime.fromisoformat(order["updated_at"])
            if (now - updated).total_seconds() < self.partial_fill_policy.timeout_seconds:
                continue
            try:
                response = self._cancel_exchange_order(order)
                self._apply_exchange_response(order, response)
            except Exception as exc:
                if not self._is_unknown_order_error(exc):
                    raise
                self._mark_canceled_locally(order, "交易所未找到部分成交订单，按已撤销处理")
        return monitored

    def manage_entry_orders(
        self,
        candidates: list[Any],
        *,
        max_wait_seconds: float = 45.0,
        min_score: int = 70,
        max_chase_pct: float = 0.8,
    ) -> dict[str, Any] | None:
        active_entries = [
            order
            for order in self.store.list_orders(ACTIVE_STATUSES)
            if not order.get("reduce_only")
            and self._is_automatic_entry(order)
            and str(order.get("order_type") or "").upper() == "LIMIT"
        ]
        if not active_entries:
            return None

        by_symbol = {candidate.symbol.upper(): candidate for candidate in candidates}
        now = datetime.now()
        pending_symbols: list[str] = []
        messages: list[str] = []
        canceled_symbols: list[str] = []
        filled_symbols: list[str] = []
        monitor_errors: list[str] = []
        for order in active_entries:
            symbol = str(order["symbol"]).upper()
            try:
                response = self._query_exchange_order(order)
                current = self._apply_exchange_response(order, response)
            except Exception as exc:
                order["last_error"] = f"挂单查询失败：{exc}"
                order["updated_at"] = _now()
                self.store.upsert_order(order)
                self.store.append_order_event(order["client_order_id"], "entry_monitor_error", {"error": str(exc)})
                pending_symbols.append(symbol)
                monitor_errors.append(symbol)
                messages.append(f"{symbol} 挂单查询暂时失败，已冻结该币避免重复下单")
                continue

            status = str(current.get("status") or order.get("status") or "").upper()
            if status == "FILLED":
                filled_symbols.append(symbol)
                messages.append(f"{symbol} 挂单已成交，进入持仓管理")
                continue
            if status not in {"NEW", "PARTIALLY_FILLED", "SUBMITTING"}:
                continue

            reason = self._entry_cancel_reason(order, by_symbol, now, max_wait_seconds, min_score, max_chase_pct)
            if reason:
                try:
                    response = self._cancel_exchange_order(order)
                    self._apply_exchange_response(order, response)
                    self.store.append_order_event(order["client_order_id"], "entry_auto_cancel", {"reason": reason})
                    canceled_symbols.append(symbol)
                    messages.append(f"{symbol} 挂单已撤销：{reason}")
                except Exception as exc:
                    if self._is_unknown_order_error(exc):
                        self._mark_canceled_locally(order, "交易所未找到挂单，按已撤销处理")
                        canceled_symbols.append(symbol)
                        messages.append(f"{symbol} 挂单已在交易所消失，按已撤销处理")
                    else:
                        pending_symbols.append(symbol)
                        monitor_errors.append(symbol)
                        messages.append(f"{symbol} 挂单撤销失败，已冻结该币避免重复下单：{exc}")
                continue
            pending_symbols.append(symbol)
            messages.append(f"{symbol} 挂单仍有效，后台继续监控")
        active_symbols = tuple(dict.fromkeys(pending_symbols))
        return {
            "action": "monitoring",
            "symbol": active_symbols[0] if active_symbols else (filled_symbols[0] if filled_symbols else ""),
            "symbols": active_symbols,
            "message": "；".join(messages),
            "canceled_symbols": tuple(dict.fromkeys(canceled_symbols)),
            "filled_symbols": tuple(dict.fromkeys(filled_symbols)),
            "monitor_errors": tuple(dict.fromkeys(monitor_errors)),
        }

    @staticmethod
    def _is_automatic_entry(order: dict[str, Any]) -> bool:
        strategy = str(order.get("strategy") or "").strip().lower()
        return strategy not in {"", "manual", "protective_exit", "position_manager"}

    @staticmethod
    def _is_futures_algo_order(order: dict[str, Any]) -> bool:
        if order.get("market") != "futures":
            return False
        raw = order.get("raw_payload") or {}
        order_type = str(raw.get("type") or raw.get("orderType") or order.get("order_type") or "").upper()
        return bool(raw.get("algoId") or raw.get("clientAlgoId")) or order_type in {
            "STOP",
            "STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
            "TRAILING_STOP_MARKET",
        }

    def _query_exchange_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return self.engine.query(
            order["market"],
            order["symbol"],
            order["client_order_id"],
            algo=self._is_futures_algo_order(order),
            exchange_order_id=str(order.get("exchange_order_id") or "") or None,
        )

    def _cancel_exchange_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return self.engine.cancel(
            order["market"],
            order["symbol"],
            order["client_order_id"],
            algo=self._is_futures_algo_order(order),
            exchange_order_id=str(order.get("exchange_order_id") or "") or None,
        )

    @staticmethod
    def _is_unknown_order_error(error: Exception) -> bool:
        text = str(error).lower()
        return (
            "-2011" in text
            or "-2013" in text
            or "unknown order" in text
            or "order does not exist" in text
        )

    def _mark_canceled_locally(self, order: dict[str, Any], reason: str) -> None:
        order["status"] = "CANCELED"
        order["last_error"] = reason
        order["updated_at"] = _now()
        self.store.upsert_order(order)
        self.store.append_order_event(order["client_order_id"], "cancel_already_gone", {"reason": reason})
        return None

    def _entry_cancel_reason(
        self,
        order: dict[str, Any],
        by_symbol: dict[str, Any],
        now: datetime,
        max_wait_seconds: float,
        min_score: int,
        max_chase_pct: float,
    ) -> str:
        created_at = datetime.fromisoformat(str(order.get("created_at") or order.get("updated_at")))
        age_seconds = (now - created_at).total_seconds()
        if age_seconds >= max_wait_seconds:
            return f"等待成交超过 {max_wait_seconds:.0f} 秒"

        candidate = by_symbol.get(str(order["symbol"]).upper())
        if candidate is None:
            return "最新扫描已不再推荐该交易对"
        expected_side = "BUY" if candidate.side == "long" else "SELL"
        if str(order.get("side", "")).upper() != expected_side:
            return f"最新方向已变为 {candidate.side}"
        if int(candidate.score) < min_score:
            return f"最新评分 {candidate.score} 低于挂单保留线 {min_score}"

        order_price = float(order.get("price") or 0)
        last = float(getattr(candidate, "last", 0) or 0)
        if order_price > 0 and last > 0 and max_chase_pct > 0:
            drift_pct = (last - order_price) / order_price * 100
            if expected_side == "BUY" and drift_pct > max_chase_pct:
                return f"现价已高于挂单价 {drift_pct:.2f}%，不追单"
            if expected_side == "SELL" and drift_pct < -max_chase_pct:
                return f"现价已低于挂单价 {abs(drift_pct):.2f}%，不追空"
        return ""

    def apply_exchange_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        managed = self.state_manager.apply_order_update(payload, "binance_user_stream")
        if managed.get("status") == "FILLED" and managed.get("client_order_id"):
            stored = self.store.get_order(managed["client_order_id"])
            if stored is not None:
                details = payload.get("o") if isinstance(payload.get("o"), dict) else payload
                fill = {
                    "executedQty": details.get("z") or details.get("executedQty"),
                    "avgPrice": details.get("ap") or details.get("avgPrice"),
                    "price": details.get("p") or details.get("price"),
                }
                self.store.record_filled_order({**stored, **managed}, fill)
        return managed

    def new_client_order_id(self, symbol: str, side: str) -> str:
        suffix = next(self._counter) % 1000
        millis = int(time.time() * 1000) % 10_000_000_000
        return f"BTA_{symbol.upper()[:10]}_{side.upper()[:1]}_{millis}_{suffix:03d}"[:36]

    def _resolve_uncertain(self, order: dict[str, Any], error: str) -> dict[str, Any]:
        try:
            response = self._query_exchange_order(order)
        except Exception as query_exc:
            order["status"] = "UNKNOWN"
            order["last_error"] = f"submit={error}; query={query_exc}"
            order["updated_at"] = _now()
            self.store.upsert_order(order)
            self.store.append_order_event(order["client_order_id"], "uncertain", {"error": order["last_error"]})
            return {"uncertain": True, "clientOrderId": order["client_order_id"], "managed_status": "UNKNOWN"}
        return self._apply_exchange_response(order, response)

    def _apply_exchange_response(self, order: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **response,
            "market": order["market"],
            "strategy": order["strategy"],
            "clientOrderId": response.get("clientOrderId") or response.get("clientAlgoId") or order["client_order_id"],
            "orderId": response.get("orderId") or response.get("algoId") or order.get("exchange_order_id") or "",
            "symbol": response.get("symbol") or order["symbol"],
            "side": response.get("side") or order["side"],
            "type": response.get("type") or response.get("orderType") or order["order_type"],
            "origQty": response.get("origQty") or response.get("quantity") or order["quantity"],
            "stopPrice": response.get("stopPrice") or response.get("triggerPrice") or order.get("stop_price"),
            "status": response.get("status") or response.get("algoStatus") or order.get("status"),
            "reduceOnly": response.get("reduceOnly", order["reduce_only"]),
            "attempts": order.get("attempts", 1),
            "created_at": order.get("created_at"),
        }
        managed = self.state_manager.apply_order_update(payload, "binance_execution")
        self.store.append_order_event(order["client_order_id"], "exchange_response", response)
        if managed["status"] == "FILLED":
            self.store.record_filled_order({**order, **managed}, response)
        return managed

    @staticmethod
    def _base_order(
        client_order_id: str,
        market: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None,
        strategy: str,
        reduce_only: bool,
        status: str,
    ) -> dict[str, Any]:
        now = _now()
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": None,
            "market": market,
            "symbol": symbol.upper(),
            "side": side.upper(),
            "order_type": order_type.upper(),
            "quantity": quantity,
            "price": price,
            "stop_price": None,
            "filled_quantity": 0.0,
            "status": status,
            "strategy": strategy,
            "reduce_only": reduce_only,
            "attempts": 0,
            "last_error": None,
            "raw_payload": {},
            "created_at": now,
            "updated_at": now,
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _is_post_only_rejection(error: Exception) -> bool:
    message = str(error).lower()
    return "-5022" in message or "could not be executed as maker" in message or "post only order will be rejected" in message
