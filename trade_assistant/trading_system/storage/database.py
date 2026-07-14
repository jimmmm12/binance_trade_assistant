from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ...main import ROOT


DEFAULT_TRADING_DB_PATH = ROOT / "data" / "trading_system.db"
LOSS_STREAK_RESET_STATE_KEY = "loss_streak_reset"


class TradingDatabase:
    def __init__(self, path: str | Path = DEFAULT_TRADING_DB_PATH, *, capture_dataset: bool | None = None) -> None:
        self.path = Path(path)
        self.capture_dataset = (
            self.path.resolve() == DEFAULT_TRADING_DB_PATH.resolve()
            if capture_dataset is None
            else capture_dataset
        )
        self.initialize()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS system_state (
                    state_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_orders (
                    client_order_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL,
                    stop_price REAL,
                    filled_quantity REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    raw_payload TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry REAL NOT NULL,
                    exit REAL,
                    quantity REAL NOT NULL,
                    remaining_quantity REAL NOT NULL DEFAULT 0,
                    pnl REAL,
                    holding_seconds REAL,
                    strategy TEXT NOT NULL,
                    score REAL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_managed_orders_status ON managed_orders(status);
                CREATE INDEX IF NOT EXISTS idx_order_events_client ON order_events(client_order_id);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, closed_at);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
            if "remaining_quantity" not in columns:
                conn.execute("ALTER TABLE trades ADD COLUMN remaining_quantity REAL NOT NULL DEFAULT 0")

    def save_state(self, key: str, payload: dict[str, Any]) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO system_state(state_key, payload, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (key, _json(payload), now),
            )

    def load_state(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM system_state WHERE state_key = ?", (key,)).fetchone()
        if row is None or not str(row["payload"] or "").strip():
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def upsert_order(self, order: dict[str, Any]) -> None:
        now = _now()
        created_at = str(order.get("created_at") or now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO managed_orders(
                    client_order_id, exchange_order_id, market, symbol, side, order_type,
                    quantity, price, stop_price, filled_quantity, status, strategy,
                    reduce_only, attempts, last_error, raw_payload, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id=excluded.exchange_order_id,
                    filled_quantity=excluded.filled_quantity,
                    status=excluded.status,
                    attempts=excluded.attempts,
                    last_error=excluded.last_error,
                    raw_payload=excluded.raw_payload,
                    updated_at=excluded.updated_at
                """,
                (
                    order["client_order_id"],
                    _optional_text(order.get("exchange_order_id")),
                    order["market"],
                    order["symbol"],
                    order["side"],
                    order["order_type"],
                    float(order["quantity"]),
                    _optional_float(order.get("price")),
                    _optional_float(order.get("stop_price")),
                    float(order.get("filled_quantity", 0)),
                    order["status"],
                    order.get("strategy", "manual"),
                    1 if order.get("reduce_only") else 0,
                    int(order.get("attempts", 0)),
                    _optional_text(order.get("last_error")),
                    _json(order.get("raw_payload") or {}),
                    created_at,
                    str(order.get("updated_at") or now),
                ),
            )

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM managed_orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return _order_row(row) if row else None

    def list_orders(self, statuses: set[str] | None = None, limit: int = 500) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            params.extend(sorted(statuses))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM managed_orders {where} ORDER BY updated_at DESC LIMIT ?", params
            ).fetchall()
        return [_order_row(row) for row in rows]

    def find_active_opening_order(
        self,
        market: str,
        symbol: str,
        side: str,
        statuses: set[str],
    ) -> dict[str, Any] | None:
        """Return an unresolved opening order for the exact directional exposure."""
        if not statuses:
            return None
        placeholders = ",".join("?" for _ in statuses)
        params = [market, symbol.upper(), side.upper(), *sorted(statuses)]
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM managed_orders
                WHERE market = ? AND UPPER(symbol) = ? AND UPPER(side) = ?
                  AND reduce_only = 0 AND status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return _order_row(row) if row else None

    def latest_automatic_entry(self, symbol: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM managed_orders
                WHERE UPPER(symbol) = ? AND reduce_only = 0 AND strategy LIKE 'automatic:%'
                  AND status NOT IN ('REJECTED', 'REJECTED_BY_RISK', 'CANCELED', 'EXPIRED', 'DRY_RUN')
                ORDER BY created_at DESC LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return _order_row(row) if row else None

    def automatic_entry_count_since(self, symbol: str, since: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM managed_orders
                WHERE UPPER(symbol) = ? AND reduce_only = 0 AND strategy LIKE 'automatic:%'
                  AND status NOT IN ('REJECTED', 'REJECTED_BY_RISK', 'CANCELED', 'EXPIRED', 'DRY_RUN')
                  AND created_at >= ?
                """,
                (symbol.upper(), since),
            ).fetchone()
        return int(row["count"] or 0)

    def append_order_event(self, client_order_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO order_events(client_order_id, event_type, payload, created_at) VALUES(?, ?, ?, ?)",
                (client_order_id, event_type, _json(payload), _now()),
            )

    def has_order_event(self, client_order_id: str, event_type: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM order_events WHERE client_order_id = ? AND event_type = ? LIMIT 1",
                (client_order_id, event_type),
            ).fetchone()
        return row is not None

    def record_filled_order(self, order: dict[str, Any], response: dict[str, Any]) -> None:
        client_order_id = order["client_order_id"]
        if self.has_order_event(client_order_id, "trade_recorded"):
            return
        quantity = float(response.get("executedQty") or order.get("filled_quantity") or order["quantity"])
        price = float(
            response.get("avgPrice")
            or response.get("price")
            or order.get("price")
            or 0
        )
        if quantity <= 0 or price <= 0:
            return
        now = _now()
        closed_outcomes: list[dict[str, Any]] = []
        if not order.get("reduce_only"):
            position_side = "long" if order["side"] == "BUY" else "short"
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO trades(
                        client_order_id, market, symbol, side, entry, quantity,
                        remaining_quantity, strategy, score, opened_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        client_order_id,
                        order["market"],
                        order["symbol"],
                        position_side,
                        price,
                        quantity,
                        quantity,
                        order.get("strategy", "automatic"),
                        response.get("score"),
                        now,
                    ),
                )
        else:
            close_side = "long" if order["side"] == "SELL" else "short"
            remaining_to_close = quantity
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM trades
                    WHERE market = ? AND symbol = ? AND side = ? AND closed_at IS NULL
                    ORDER BY opened_at ASC
                    """,
                    (order["market"], order["symbol"], close_side),
                ).fetchall()
                for row in rows:
                    if remaining_to_close <= 0:
                        break
                    current_remaining = float(row["remaining_quantity"] or row["quantity"])
                    closing = min(current_remaining, remaining_to_close)
                    direction = 1 if close_side == "long" else -1
                    realized = (price - float(row["entry"])) * closing * direction
                    new_remaining = current_remaining - closing
                    cumulative_pnl = float(row["pnl"] or 0) + realized
                    opened_at = datetime.fromisoformat(row["opened_at"])
                    holding = (datetime.fromisoformat(now) - opened_at).total_seconds()
                    conn.execute(
                        """
                        UPDATE trades SET exit = ?, pnl = ?, remaining_quantity = ?,
                            holding_seconds = ?, closed_at = ?
                        WHERE id = ?
                        """,
                        (
                            price,
                            cumulative_pnl,
                            new_remaining,
                            holding if new_remaining <= 1e-12 else None,
                            now if new_remaining <= 1e-12 else None,
                            row["id"],
                        ),
                    )
                    if new_remaining <= 1e-12:
                        closed_outcomes.append(
                            {
                                "trade_id": int(row["id"]),
                                "client_order_id": row["client_order_id"],
                                "market": row["market"],
                                "symbol": row["symbol"],
                                "side": row["side"],
                                "entry": float(row["entry"]),
                                "exit": price,
                                "quantity": float(row["quantity"]),
                                "pnl": cumulative_pnl,
                                "holding_seconds": holding,
                                "strategy": row["strategy"],
                                "opened_at": row["opened_at"],
                                "closed_at": now,
                            }
                        )
                    remaining_to_close -= closing
        self.append_order_event(client_order_id, "trade_recorded", {"quantity": quantity, "price": price})
        if self.capture_dataset and closed_outcomes:
            try:
                from ...dataset import append_trade_outcome_record

                for outcome in closed_outcomes:
                    append_trade_outcome_record(outcome)
            except OSError:
                pass

    def closed_trade_records(self, limit: int = 10000) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, client_order_id, market, symbol, side, entry, exit, quantity,
                       pnl, holding_seconds, strategy, score, opened_at, closed_at
                FROM trades WHERE closed_at IS NOT NULL
                ORDER BY closed_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_snapshot(self, source: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO snapshots(source, payload, created_at) VALUES(?, ?, ?)",
                (source, _json(payload), _now()),
            )

    def append_risk_event(
        self,
        level: str,
        code: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO risk_events(level, code, message, payload, created_at) VALUES(?, ?, ?, ?, ?)",
                (level, code, message, _json(payload or {}), _now()),
            )

    def performance_summary(self) -> dict[str, float | int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    COALESCE(SUM(pnl), 0) AS total_pnl,
                    COALESCE(AVG(pnl), 0) AS average_pnl
                FROM trades WHERE closed_at IS NOT NULL
                """
            ).fetchone()
        trades = int(row["trades"] or 0)
        wins = int(row["wins"] or 0)
        return {
            "trades": trades,
            "wins": wins,
            "win_rate": round(wins / trades * 100, 2) if trades else 0.0,
            "total_pnl": round(float(row["total_pnl"] or 0), 8),
            "average_pnl": round(float(row["average_pnl"] or 0), 8),
        }

    def consecutive_losses(self) -> int:
        reset_at = self.loss_streak_reset_at()
        params: tuple[str, ...] = ()
        where = "closed_at IS NOT NULL"
        if reset_at:
            where += " AND closed_at > ?"
            params = (reset_at,)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT pnl FROM trades WHERE {where} ORDER BY closed_at DESC LIMIT 50",
                params,
            ).fetchall()
        count = 0
        for row in rows:
            if float(row["pnl"] or 0) >= 0:
                break
            count += 1
        return count

    def loss_streak_reset_at(self) -> str | None:
        state = self.load_state(LOSS_STREAK_RESET_STATE_KEY)
        if not state:
            return None
        reset_at = str(state.get("reset_at") or "").strip()
        return reset_at or None

    def reset_consecutive_losses(self, reason: str = "manual") -> str:
        reset_at = _now()
        self.save_state(
            LOSS_STREAK_RESET_STATE_KEY,
            {
                "reset_at": reset_at,
                "reason": reason,
            },
        )
        self.append_risk_event(
            "info",
            "loss_streak_reset",
            "连续亏损计数已人工清零，历史交易记录保留。",
            {"reset_at": reset_at, "reason": reason},
        )
        return reset_at

    def order_status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM managed_orders GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def closed_trade_pnls(self, limit: int = 5000) -> list[float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [float(row["pnl"] or 0) for row in rows]

    def open_trade_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM trades WHERE closed_at IS NULL").fetchone()
        return int(row["count"] or 0)

    def today_trade_pnl(self) -> float:
        today = datetime.now().date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades WHERE closed_at >= ?",
                (today,),
            ).fetchone()
        return float(row["pnl"] or 0)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn


def _order_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["reduce_only"] = bool(result["reduce_only"])
    try:
        result["raw_payload"] = json.loads(result["raw_payload"] or "{}")
    except json.JSONDecodeError:
        result["raw_payload"] = {}
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _optional_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _optional_text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")
