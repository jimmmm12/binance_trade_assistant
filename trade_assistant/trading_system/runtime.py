from __future__ import annotations

import time
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..automation_state import compact_state_path
from ..binance_client import BinanceClient
from ..main import load_settings
from ..models import ScoredSignal, TradePlan
from ..portfolio import futures_today_realized_pnl, read_futures_account_risk
from ..position_manager import ManagedPosition, PositionManagementDecision
from ..risk_engine import PlanRiskReview
from .core.events import EventBus
from .data.user_data import BinanceUserDataService
from .execution.engine import BinanceExecutionEngine
from .execution.order_manager import OrderManager, PartialFillPolicy
from .monitoring.metrics import MetricsCollector, RuntimeMetrics
from .risk.manager import RiskContext, RiskLimits, RiskManager
from .research.optimizer import GridSearchOptimizer
from .state.manager import StateManager
from .storage.database import DEFAULT_TRADING_DB_PATH, TradingDatabase
from .strategy.base import StrategyRegistry
from .strategy.builtin import BreakoutStrategy, MeanReversionStrategy, TrendFollowingStrategy
from .strategy.regime import detect_regime


class TradingRuntime:
    def __init__(
        self,
        *,
        client: BinanceClient | None = None,
        database_path: str | Path = DEFAULT_TRADING_DB_PATH,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.client = client or BinanceClient()
        self.event_bus = EventBus()
        self.store = TradingDatabase(database_path)
        self.state_manager = StateManager(self.store, self.event_bus)
        self.risk_manager = RiskManager(
            RiskLimits.from_settings(self.settings), self.store, self.state_manager
        )
        execution = BinanceExecutionEngine(self.client)
        partial = self.settings.get("order_manager", {})
        self.order_manager = OrderManager(
            self.store,
            self.state_manager,
            self.risk_manager,
            execution,
            self.event_bus,
            PartialFillPolicy(
                action=str(partial.get("partial_fill_policy", "wait")),
                timeout_seconds=int(partial.get("partial_fill_timeout_seconds", 30)),
            ),
        )
        self.user_data = BinanceUserDataService(self.client, self._handle_user_event)
        self.metrics = MetricsCollector(self.store, self.state_manager)
        self.strategies = StrategyRegistry()
        self.optimizer = GridSearchOptimizer()
        for strategy in (TrendFollowingStrategy(), BreakoutStrategy(), MeanReversionStrategy()):
            self.strategies.register(strategy)
        self.last_reconciliation: list[str] = []
        self._pending_protection: dict[str, tuple[TradePlan, str]] = {}
        self._live_market = "futures"
        self._spot_symbol: str | None = None

    def sync_live_state(self, market: str | None = None, symbol: str | None = None) -> list[str]:
        market = market or self._live_market
        symbol = symbol or self._spot_symbol
        started = time.perf_counter()
        try:
            if market == "futures":
                account = read_futures_account_risk(self.client)
                mismatches = self.state_manager.reconcile_futures_account(account)
            else:
                if not symbol:
                    raise ValueError("现货状态同步需要交易对")
                account_payload = self.client.spot_account()
                mark_price = self.client.latest_price("spot", symbol)
                mismatches = self.state_manager.reconcile_spot_account(account_payload, symbol, mark_price)
            for payload in self.client.open_orders(market, symbol if market == "spot" else None):
                self.state_manager.apply_order_update({**payload, "market": market}, "binance_rest")
            self.order_manager.recover_active_orders()
            self.last_reconciliation = mismatches
            self._live_market = market
            if market == "spot":
                self._spot_symbol = symbol
            return mismatches
        except Exception as exc:
            self.state_manager.set_sync_error(str(exc), "binance_rest")
            raise
        finally:
            self.metrics.record_api_latency(started)

    def start_live_sync(self, market: str = "futures", symbol: str | None = None) -> list[str]:
        mismatches = self.sync_live_state(market, symbol)
        status = self.user_data.status()
        if status.running and status.market != market:
            self.user_data.stop()
            self.user_data = BinanceUserDataService(self.client, self._handle_user_event)
        self.user_data.start(market)
        return mismatches

    def ensure_user_stream(self, market: str = "futures") -> None:
        status = self.user_data.status()
        if status.running and status.market == market:
            return
        if status.running:
            self.user_data.stop()
            self.user_data = BinanceUserDataService(self.client, self._handle_user_event)
        self._live_market = market
        self.user_data.start(market)

    def stop(self) -> None:
        self.user_data.stop()

    def update_credentials(self, api_key: str | None, api_secret: str | None) -> None:
        new_key = api_key or os.getenv("BINANCE_API_KEY")
        new_secret = api_secret or os.getenv("BINANCE_API_SECRET")
        if self.client.api_key == new_key and self.client.api_secret == new_secret:
            return
        was_running = self.user_data.status().running
        self.user_data.stop()
        self.client = BinanceClient(api_key=new_key, api_secret=new_secret)
        self.order_manager.engine = BinanceExecutionEngine(self.client)
        self.user_data = BinanceUserDataService(self.client, self._handle_user_event)
        if was_running and new_key and new_secret:
            self.start_live_sync(self._live_market, self._spot_symbol)

    def reload_settings(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.risk_manager.limits = RiskLimits.from_settings(settings)
        partial = settings.get("order_manager", {})
        self.order_manager.partial_fill_policy = PartialFillPolicy(
            action=str(partial.get("partial_fill_policy", "wait")),
            timeout_seconds=int(partial.get("partial_fill_timeout_seconds", 30)),
        )

    def evaluate_regime(self, signal: ScoredSignal) -> dict[str, Any]:
        result = detect_regime(signal.signal, signal.mode)
        payload = {
            "regime": result.regime.value,
            "confidence": result.confidence,
            "reasons": result.reasons,
        }
        self.state_manager.set_market_regime(signal.symbol, payload)
        return payload

    def record_automation_decision(self, decision) -> None:
        self.state_manager.set_automation_state(
            {
                "state": decision.state.value,
                "state_path": compact_state_path(decision.state_path),
                "action": decision.action,
                "message": decision.message,
                "symbol": decision.signal.symbol if decision.signal is not None else "",
            }
        )

    def manage_pending_entry_orders(self, candidates: list[ScoredSignal]) -> dict[str, Any] | None:
        order_settings = self.settings.get("order_manager", {})
        return self.order_manager.manage_entry_orders(
            candidates,
            max_wait_seconds=float(order_settings.get("entry_order_timeout_seconds", 45)),
            min_score=int(order_settings.get("entry_order_min_score", 70)),
            max_chase_pct=float(order_settings.get("entry_order_max_chase_pct", 0.8)),
        )

    def submit_plan(
        self,
        plan: TradePlan,
        side: str,
        review: PlanRiskReview,
        *,
        market_fresh: bool,
        allow_live: bool,
        confirm: str,
        strategy: str = "automatic",
        order_type: str = "LIMIT",
        risk_line: str = "conservative",
    ) -> dict[str, Any]:
        context = self.build_risk_context(plan.symbol, market=plan.market, market_fresh=market_fresh, risk_line=risk_line)
        result = self.order_manager.submit_plan(
            plan,
            side,
            review,
            context,
            allow_live=allow_live,
            confirm=confirm,
            strategy=strategy,
            order_type=order_type,
            post_only=(
                order_type == "LIMIT"
                and strategy.startswith("automatic:")
                and bool(self.settings.get("auto_execution", {}).get("post_only_entries", True))
            ),
        )
        client_order_id = str(result.get("clientOrderId") or "")
        order_status = str(result.get("managed_status") or result.get("status") or "").upper()
        if (
            client_order_id
            and plan.market == "futures"
            and order_status in {"NEW", "PARTIALLY_FILLED", "FILLED"}
            and bool(self.settings.get("order_manager", {}).get("auto_place_protective_orders", False))
        ):
            self._pending_protection[client_order_id] = (plan, confirm)
            self.store.save_state(
                f"protection:{client_order_id}",
                {"status": "waiting_entry_fill", "plan": asdict(plan)},
            )
            if order_status == "FILLED":
                self._place_pending_protection(client_order_id)
        return result

    def automatic_entry_allowed(self, symbol: str) -> tuple[bool, str]:
        config = self.settings.get("auto_execution", {})
        latest = self.store.latest_automatic_entry(symbol)
        cooldown_minutes = max(0.0, float(config.get("symbol_reentry_cooldown_minutes", 30.0)))
        if latest is not None and cooldown_minutes > 0:
            try:
                created = datetime.fromisoformat(str(latest["created_at"]))
            except ValueError:
                created = datetime.min
            elapsed = max(0.0, (datetime.now() - created).total_seconds() / 60)
            if elapsed < cooldown_minutes:
                return False, f"{symbol.upper()} 距离上次自动开仓仅 {elapsed:.0f} 分钟，冷却 {cooldown_minutes:.0f} 分钟"
        daily_limit = max(1, int(config.get("max_entries_per_symbol_per_day", 3)))
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        count = self.store.automatic_entry_count_since(symbol, since)
        if count >= daily_limit:
            return False, f"{symbol.upper()} 今日自动开仓已达 {count}/{daily_limit} 次上限"
        return True, "交易频率通过"

    def submit_position_decision(
        self,
        managed: ManagedPosition,
        decision: PositionManagementDecision,
        *,
        allow_live: bool,
        confirm: str,
    ) -> dict[str, Any]:
        context = self.build_risk_context(
            managed.position.symbol, market=managed.position.market, market_fresh=True
        )
        if decision.action == "move_stop":
            if decision.new_stop is None or decision.new_stop <= 0:
                raise ValueError("移动止损缺少有效的新止损价")
            return self.order_manager.replace_protective_stop(
                market=managed.position.market,
                symbol=managed.position.symbol,
                side=decision.exit_side,
                quantity=managed.position.quantity,
                stop_price=decision.new_stop,
                context=context,
                allow_live=allow_live,
                confirm=confirm,
            )
        return self.order_manager.submit_reduce(
            market=managed.position.market,
            symbol=managed.position.symbol,
            side=decision.exit_side,
            quantity=decision.quantity,
            context=context,
            allow_live=allow_live,
            confirm=confirm,
        )

    def build_risk_context(self, symbol: str, *, market: str = "futures", market_fresh: bool, risk_line: str = "conservative") -> RiskContext:
        state = self.state_manager.snapshot()
        account = state.account
        equity = float(account.get("equity") or account.get("wallet_balance") or 0)
        real_positions = [position for position in state.positions.values() if position.get("source") == "real"]
        total_exposure = sum(abs(float(position.get("notional", 0))) for position in real_positions)
        symbol_exposure = sum(
            abs(float(position.get("notional", 0)))
            for position in real_positions
            if position.get("symbol") == symbol.upper()
        )
        if market == "futures":
            try:
                today_pnl = futures_today_realized_pnl(self.client)
                risk_data_healthy = True
            except Exception:
                today_pnl = 0.0
                risk_data_healthy = False
        else:
            today_pnl = self.store.today_trade_pnl()
            risk_data_healthy = True
        return RiskContext(
            equity=equity,
            today_pnl=today_pnl,
            total_exposure=total_exposure,
            symbol_exposure=symbol_exposure,
            consecutive_losses=self.store.consecutive_losses(),
            market_fresh=market_fresh,
            account_state_fresh=bool(state.sync_status.get("healthy", False)),
            api_healthy=bool(self.client.api_key and self.client.api_secret),
            risk_data_healthy=risk_data_healthy,
            emergency_stop=state.risk_status.get("status") == "stopped",
            open_order_count=len(self.store.list_orders({"NEW", "PARTIALLY_FILLED", "UNKNOWN"})),
            uncertain_order_count=len(self.store.list_orders({"UNKNOWN"})),
            risk_line=risk_line,
        )

    def metrics_snapshot(self, market_websocket: str = "unknown") -> RuntimeMetrics:
        user_status = self.user_data.status()
        user_text = "connected" if user_status.connected else ("reconnecting" if user_status.running else "stopped")
        return self.metrics.snapshot(market_websocket, user_text)

    def real_position_rows(self) -> list[dict[str, Any]]:
        state = self.state_manager.snapshot()
        return [
            position
            for position in state.positions.values()
            if position.get("source") == "real" and abs(float(position.get("quantity", 0))) > 1e-8
        ]

    def _handle_user_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("e", ""))
        if event_type in {"ORDER_TRADE_UPDATE", "executionReport"}:
            managed = self.order_manager.apply_exchange_event(payload)
            if managed.get("status") == "FILLED":
                self._place_pending_protection(str(managed.get("client_order_id") or ""))
        elif event_type in {"ACCOUNT_UPDATE", "outboundAccountPosition"}:
            if event_type == "ACCOUNT_UPDATE":
                self.state_manager.apply_futures_account_event(payload)
            else:
                self.state_manager.apply_spot_account_event(payload)

    def _place_pending_protection(self, client_order_id: str) -> None:
        pending = self._pending_protection.get(client_order_id)
        if pending is None:
            return
        plan, confirm = pending
        try:
            results = self.order_manager.submit_protective_orders(
                plan,
                self.build_risk_context(plan.symbol, market=plan.market, market_fresh=True),
                allow_live=True,
                confirm=confirm,
                parent_client_order_id=client_order_id,
            )
        except Exception as exc:
            message = f"{plan.symbol} 成交后保护单提交失败：{exc}"
            self.store.save_state(
                f"protection:{client_order_id}",
                {"status": "failed", "plan": asdict(plan), "error": str(exc)},
            )
            self.store.append_risk_event(
                "danger",
                "protective_order_failed",
                message,
                {"client_order_id": client_order_id},
            )
            self.risk_manager.emergency_stop(message)
            return
        self.store.save_state(
            f"protection:{client_order_id}",
            {"status": "placed", "plan": asdict(plan), "orders": results},
        )
        self._pending_protection.pop(client_order_id, None)
