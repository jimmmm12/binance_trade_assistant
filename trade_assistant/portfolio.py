from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .main import ROOT, load_settings
from .models import FuturesAccountRisk, PositionSnapshot


LEGACY_DEFAULT_SIM_BALANCE = 10000.0
DEFAULT_SIM_BALANCE = 1000.0
DEFAULT_DB_PATH = ROOT / "data" / "sim_account.db"
REAL_LOSS_INCOME_TYPES = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
POSITION_EPSILON = 1e-8


def is_flat_quantity(quantity: float) -> bool:
    return abs(float(quantity)) <= POSITION_EPSILON


def configured_default_sim_balance() -> float:
    try:
        value = float(load_settings().get("default_equity", DEFAULT_SIM_BALANCE))
    except Exception:
        return DEFAULT_SIM_BALANCE
    return value if value > 0 else DEFAULT_SIM_BALANCE


def today_window_ms() -> tuple[int, int]:
    now = datetime.now()
    start = datetime.combine(now.date(), datetime.min.time())
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000)


def futures_today_realized_pnl(client) -> float:
    start, end = today_window_ms()
    rows = client.futures_income_history(start, end)
    total = 0.0
    for row in rows:
        if row.get("incomeType") in REAL_LOSS_INCOME_TYPES:
            total += float(row.get("income", 0))
    return total


def flat_position(source: str, market: str, symbol: str, mark_price: float = 0.0) -> PositionSnapshot:
    return PositionSnapshot(
        source=source,
        market=market,
        symbol=symbol,
        side="flat",
        quantity=0,
        entry_price=0,
        mark_price=mark_price,
        notional=0,
        unrealized_pnl=0,
        realized_pnl=0,
        leverage=1,
        updated_at=datetime.now().isoformat(timespec="seconds"),
        liquidation_price=None,
        margin_type=None,
        isolated_margin=None,
    )


def normalize_spot_position(account_payload: dict, symbol: str, mark_price: float = 0.0) -> PositionSnapshot:
    base_asset = symbol.upper().removesuffix("USDT")
    for row in account_payload.get("balances", []):
        if row.get("asset") != base_asset:
            continue
        quantity = float(row.get("free", 0)) + float(row.get("locked", 0))
        if quantity <= 0:
            return flat_position("real", "spot", symbol, mark_price)
        return PositionSnapshot(
            source="real",
            market="spot",
            symbol=symbol,
            side="long",
            quantity=quantity,
            entry_price=0,
            mark_price=mark_price,
            notional=quantity * mark_price,
            unrealized_pnl=0,
            realized_pnl=0,
            leverage=1,
            updated_at=datetime.now().isoformat(timespec="seconds"),
            liquidation_price=None,
            margin_type=None,
            isolated_margin=None,
        )
    return flat_position("real", "spot", symbol, mark_price)


def normalize_futures_position(rows: list[dict], symbol: str) -> PositionSnapshot:
    for row in rows:
        if row.get("symbol") != symbol.upper():
            continue
        amount = float(row.get("positionAmt", 0))
        if is_flat_quantity(amount):
            return flat_position("real", "futures", symbol, float(row.get("markPrice", 0)))
        quantity = abs(amount)
        mark_price = float(row.get("markPrice", 0))
        return PositionSnapshot(
            source="real",
            market="futures",
            symbol=symbol,
            side="long" if amount > 0 else "short",
            quantity=quantity,
            entry_price=float(row.get("entryPrice", 0)),
            mark_price=mark_price,
            notional=quantity * mark_price,
            unrealized_pnl=float(row.get("unRealizedProfit", 0)),
            realized_pnl=0,
            leverage=float(row.get("leverage", 1)),
            updated_at=datetime.now().isoformat(timespec="seconds"),
            liquidation_price=_optional_float(row.get("liquidationPrice")),
            margin_type=row.get("marginType"),
            isolated_margin=_optional_float(row.get("isolatedMargin")),
        )
    return flat_position("real", "futures", symbol)


def read_real_spot_position(client, symbol: str, mark_price: float = 0.0) -> PositionSnapshot:
    return normalize_spot_position(client.spot_account(), symbol, mark_price)


def read_real_futures_position(client, symbol: str) -> PositionSnapshot:
    return normalize_futures_position(client.futures_positions(symbol), symbol)


def read_futures_account_risk(client) -> FuturesAccountRisk:
    position_rows = client.futures_positions()
    if hasattr(client, "futures_account"):
        try:
            return normalize_futures_account_risk(
                client.futures_account(),
                position_rows,
                balance_rows=client.futures_account_balance(),
            )
        except Exception:
            pass
    return normalize_futures_account_risk(client.futures_account_balance(), position_rows)


def normalize_futures_account_risk(
    account_or_balance: dict | list[dict] | None = None,
    position_rows: list[dict] | None = None,
    balance_rows: list[dict] | None = None,
) -> FuturesAccountRisk:
    position_rows = position_rows or []
    account_payload = account_or_balance if isinstance(account_or_balance, dict) else {}
    balance_payload = balance_rows if balance_rows is not None else (
        account_or_balance if isinstance(account_or_balance, list) else []
    )
    usdt = next((row for row in balance_payload if row.get("asset") == "USDT"), {})
    positions = [
        normalize_futures_position([row], row.get("symbol", ""))
        for row in position_rows
        if float(row.get("positionAmt", 0)) != 0
    ]
    total_wallet = _optional_float(account_payload.get("totalWalletBalance"))
    total_margin = _optional_float(account_payload.get("totalMarginBalance"))
    wallet_balance = _first_positive_float(total_wallet, usdt.get("balance"), total_margin)
    available_balance = _first_positive_float(account_payload.get("availableBalance"), usdt.get("availableBalance"))
    unrealized = _optional_float(account_payload.get("totalUnrealizedProfit"))
    if unrealized is None:
        unrealized = sum(position.unrealized_pnl for position in positions)
    if (total_wallet is None or total_wallet <= 0) and total_margin is not None and total_margin > 0:
        unrealized = 0.0
    return FuturesAccountRisk(
        wallet_balance=wallet_balance,
        available_balance=available_balance,
        total_unrealized_pnl=unrealized,
        positions=positions,
    )


def _optional_float(value) -> float | None:
    if value in (None, "", "0", 0):
        return None
    return float(value)


def _first_positive_float(*values) -> float:
    for value in values:
        parsed = _optional_float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return 0.0


class SimulatedPortfolio:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sim_account (
                    asset TEXT PRIMARY KEY,
                    balance REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sim_positions (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    leverage REAL NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, symbol)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sim_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS position_records (
                    source TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    leverage REAL NOT NULL DEFAULT 1,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, market, symbol)
                )
                """
            )
            self._ensure_position_record_columns(conn)
            row = conn.execute("SELECT balance FROM sim_account WHERE asset = 'USDT'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sim_account(asset, balance) VALUES('USDT', ?)",
                    (configured_default_sim_balance(),),
                )
            else:
                self._migrate_legacy_default_balance(conn, float(row["balance"]))

    def _ensure_position_record_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(position_records)").fetchall()
        }
        if "leverage" not in columns:
            conn.execute("ALTER TABLE position_records ADD COLUMN leverage REAL NOT NULL DEFAULT 1")

    def _migrate_legacy_default_balance(self, conn: sqlite3.Connection, balance: float) -> None:
        if abs(balance - LEGACY_DEFAULT_SIM_BALANCE) > POSITION_EPSILON:
            return
        open_position = conn.execute("SELECT 1 FROM sim_positions LIMIT 1").fetchone()
        if open_position is not None:
            return
        default_balance = configured_default_sim_balance()
        if abs(default_balance - balance) <= POSITION_EPSILON:
            return
        conn.execute("UPDATE sim_account SET balance = ? WHERE asset = 'USDT'", (default_balance,))

    def cash_balance(self, asset: str = "USDT") -> float:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute("SELECT balance FROM sim_account WHERE asset = ?", (asset,)).fetchone()
        return float(row["balance"]) if row else 0.0

    def fill_count(self) -> int:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM sim_fills").fetchone()
        return int(row["count"] or 0)

    def today_realized_pnl(self) -> float:
        self.initialize()
        today = datetime.now().date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT market, symbol, side, quantity, price FROM sim_fills WHERE created_at >= ? ORDER BY id ASC",
                (today,),
            ).fetchall()
        states: dict[tuple[str, str], dict[str, float | str]] = {}
        realized = 0.0
        for row in rows:
            market = row["market"]
            symbol = row["symbol"]
            side = row["side"]
            fill_quantity = float(row["quantity"])
            price = float(row["price"])
            key = (market, symbol)
            state = states.setdefault(key, {"side": "flat", "quantity": 0.0, "entry": 0.0})
            if market == "futures":
                realized += self._apply_futures_fill_to_state(state, side, fill_quantity, price)
            elif side == "BUY":
                old_quantity = float(state["quantity"])
                old_entry = float(state["entry"])
                new_quantity = old_quantity + fill_quantity
                state["side"] = "long"
                state["quantity"] = new_quantity
                state["entry"] = ((old_quantity * old_entry) + (fill_quantity * price)) / new_quantity
            elif side == "SELL" and float(state["quantity"]) > 0:
                closing = min(float(state["quantity"]), fill_quantity)
                realized += (price - float(state["entry"])) * closing
                state["quantity"] = float(state["quantity"]) - closing
        return round(realized, 8)

    def upsert_position_record(
        self,
        source: str,
        market: str,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        mark_price: float,
        stop_price: float,
        target_price: float,
        leverage: float = 1.0,
        realized_pnl: float = 0.0,
        status: str = "计划中",
    ) -> None:
        self.initialize()
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO position_records(
                    source, market, symbol, side, quantity, entry_price, mark_price,
                    stop_price, target_price, leverage, realized_pnl, status, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, market, symbol) DO UPDATE SET
                    side = excluded.side,
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    mark_price = excluded.mark_price,
                    stop_price = excluded.stop_price,
                    target_price = excluded.target_price,
                    leverage = excluded.leverage,
                    realized_pnl = excluded.realized_pnl,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    source,
                    market,
                    symbol,
                    side,
                    quantity,
                    entry_price,
                    mark_price,
                    stop_price,
                    target_price,
                    max(1.0, float(leverage or 1.0)),
                    realized_pnl,
                    status,
                    now,
                ),
            )

    def position_records(self, active_only: bool = True, source: str | None = None) -> list[dict]:
        self.initialize()
        conditions: list[str] = []
        params: list[object] = []
        if active_only:
            conditions.append("quantity > ?")
            conditions.append("side != 'flat'")
            conditions.append("status NOT LIKE ?")
            params.append(POSITION_EPSILON)
            params.append("%计划%")
        if source:
            conditions.append("source = ?")
            params.append(source)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM position_records
                {where}
                ORDER BY updated_at DESC
                """,
                tuple(params),
            ).fetchall()
        records: list[dict] = []
        for row in rows:
            record = dict(row)
            side_multiplier = -1 if record["side"] == "short" else 1
            record["unrealized_pnl"] = round(
                (record["mark_price"] - record["entry_price"]) * record["quantity"] * side_multiplier,
                8,
            )
            records.append(record)
        return records

    def open_position_count(self) -> int:
        self.initialize()
        with self._connect() as conn:
            sim_row = conn.execute(
                "SELECT COUNT(*) AS count FROM sim_positions WHERE quantity > ?",
                (POSITION_EPSILON,),
            ).fetchone()
            record_row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM position_records
                WHERE source = 'simulated' AND quantity > ? AND side != 'flat'
                """,
                (POSITION_EPSILON,),
            ).fetchone()
        return int(sim_row["count"] or 0) + int(record_row["count"] or 0)

    def simulated_residue_count(self) -> int:
        self.initialize()
        with self._connect() as conn:
            sim_row = conn.execute("SELECT COUNT(*) AS count FROM sim_positions").fetchone()
            record_row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM position_records
                WHERE source = 'simulated' AND (ABS(quantity) > 0 OR side != 'flat')
                """
            ).fetchone()
        return int(sim_row["count"] or 0) + int(record_row["count"] or 0)

    def update_position_record_mark_price(self, source: str, market: str, symbol: str, mark_price: float) -> None:
        self.initialize()
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE position_records
                SET mark_price = ?, updated_at = ?
                WHERE source = ? AND market = ? AND symbol = ?
                """,
                (mark_price, now, source, market, symbol.upper()),
            )

    def close_position_record(self, source: str, market: str, symbol: str, mark_price: float = 0.0) -> None:
        self.initialize()
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE position_records
                SET side = 'flat',
                    quantity = 0,
                    mark_price = ?,
                    status = '已平仓/空仓',
                    updated_at = ?
                WHERE source = ? AND market = ? AND symbol = ?
                """,
                (mark_price, now, source, market, symbol.upper()),
            )

    def clear_all_positions(self) -> int:
        """Close every local simulated position at its latest recorded mark price."""
        self.initialize()
        with self._connect() as conn:
            positions = [dict(row) for row in conn.execute("SELECT * FROM sim_positions").fetchall()]
            records = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM position_records
                    WHERE source = 'simulated' AND (ABS(quantity) > 0 OR side != 'flat')
                    """,
                ).fetchall()
            ]

        mark_prices = {
            (record["market"], str(record["symbol"]).upper()): float(record["mark_price"] or record["entry_price"] or 0)
            for record in records
        }
        cleared = 0
        closed_keys: set[tuple[str, str]] = set()
        for row in positions:
            market = row["market"]
            symbol = str(row["symbol"]).upper()
            side = row["side"]
            quantity = float(row["quantity"])
            if is_flat_quantity(quantity):
                self._delete_sim_position(market, symbol)
                self.close_position_record("simulated", market, symbol, mark_prices.get((market, symbol), 0.0))
                closed_keys.add((market, symbol))
                cleared += 1
                continue
            mark_price = mark_prices.get((market, symbol)) or float(row["entry_price"])
            if mark_price <= 0:
                mark_price = float(row["entry_price"])
            exit_side = "SELL" if side == "long" else "BUY"
            self.apply_fill(market, symbol, exit_side, quantity, mark_price, float(row["leverage"]))
            self.close_position_record("simulated", market, symbol, mark_price)
            closed_keys.add((market, symbol))
            cleared += 1

        for record in records:
            key = (record["market"], str(record["symbol"]).upper())
            if key in closed_keys:
                continue
            self.close_position_record(
                "simulated",
                record["market"],
                record["symbol"],
                float(record["mark_price"] or record["entry_price"] or 0),
            )
            cleared += 1
        return cleared

    def _delete_sim_position(self, market: str, symbol: str) -> None:
        self.initialize()
        with self._connect() as conn:
            conn.execute("DELETE FROM sim_positions WHERE market = ? AND symbol = ?", (market, symbol.upper()))

    def apply_fill(
        self,
        market: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        leverage: float = 1.0,
    ) -> PositionSnapshot:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if price <= 0:
            raise ValueError("price must be positive")
        self.initialize()
        side = side.upper()
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sim_fills(market, symbol, side, quantity, price, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (market, symbol, side, quantity, price, now),
            )
            row = conn.execute(
                "SELECT * FROM sim_positions WHERE market = ? AND symbol = ?",
                (market, symbol),
            ).fetchone()
            cash = self.cash_balance()
            old_realized = float(row["realized_pnl"]) if row is not None else 0.0
            if market == "futures":
                new_side, new_quantity, new_entry, realized = self._apply_futures_fill(row, side, quantity, price)
                cash += realized - old_realized
            elif side == "BUY":
                new_side = "long"
                new_quantity, new_entry, realized = self._apply_buy(row, quantity, price)
                cash -= quantity * price
            elif side == "SELL":
                new_side = "long"
                new_quantity, new_entry, realized = self._apply_sell(row, quantity, price)
                cash += quantity * price
            else:
                raise ValueError("side must be BUY or SELL")
            conn.execute("UPDATE sim_account SET balance = ? WHERE asset = 'USDT'", (cash,))
            if is_flat_quantity(new_quantity):
                conn.execute("DELETE FROM sim_positions WHERE market = ? AND symbol = ?", (market, symbol))
            else:
                conn.execute(
                    """
                    INSERT INTO sim_positions(market, symbol, side, quantity, entry_price, realized_pnl, leverage, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market, symbol) DO UPDATE SET
                        side = excluded.side,
                        quantity = excluded.quantity,
                        entry_price = excluded.entry_price,
                        realized_pnl = excluded.realized_pnl,
                        leverage = excluded.leverage,
                        updated_at = excluded.updated_at
                    """,
                    (market, symbol, new_side, new_quantity, new_entry, realized, leverage, now),
                )
        return self.get_position(market, symbol, mark_price=price)

    def get_position(self, market: str, symbol: str, mark_price: float = 0.0) -> PositionSnapshot:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sim_positions WHERE market = ? AND symbol = ?",
                (market, symbol),
            ).fetchone()
        if row is None:
            return PositionSnapshot(
                source="simulated",
                market=market,
                symbol=symbol,
                side="flat",
                quantity=0,
                entry_price=0,
                mark_price=mark_price,
                notional=0,
                unrealized_pnl=0,
                realized_pnl=0,
                leverage=1,
                updated_at=datetime.now().isoformat(timespec="seconds"),
                liquidation_price=None,
                margin_type=None,
                isolated_margin=None,
            )
        mark = mark_price or float(row["entry_price"])
        quantity = float(row["quantity"])
        if is_flat_quantity(quantity):
            return flat_position("simulated", market, symbol, mark)
        entry = float(row["entry_price"])
        side = row["side"]
        side_multiplier = -1 if side == "short" else 1
        return PositionSnapshot(
            source="simulated",
            market=market,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry,
            mark_price=mark,
            notional=quantity * mark,
            unrealized_pnl=(mark - entry) * quantity * side_multiplier,
            realized_pnl=float(row["realized_pnl"]),
            leverage=float(row["leverage"]),
            updated_at=row["updated_at"],
            liquidation_price=None,
            margin_type=None,
            isolated_margin=None,
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _apply_buy(row: sqlite3.Row | None, quantity: float, price: float) -> tuple[float, float, float]:
        if row is None:
            return quantity, price, 0.0
        old_quantity = float(row["quantity"])
        old_entry = float(row["entry_price"])
        new_quantity = old_quantity + quantity
        new_entry = ((old_quantity * old_entry) + (quantity * price)) / new_quantity
        return new_quantity, new_entry, float(row["realized_pnl"])

    @staticmethod
    def _apply_sell(row: sqlite3.Row | None, quantity: float, price: float) -> tuple[float, float, float]:
        if row is None or float(row["quantity"]) < quantity:
            raise ValueError("simulated long position is not enough to sell")
        old_quantity = float(row["quantity"])
        old_entry = float(row["entry_price"])
        realized = float(row["realized_pnl"]) + (price - old_entry) * quantity
        new_quantity = old_quantity - quantity
        return new_quantity, old_entry, realized

    @staticmethod
    def _apply_futures_fill(
        row: sqlite3.Row | None,
        side: str,
        quantity: float,
        price: float,
    ) -> tuple[str, float, float, float]:
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        target_side = "long" if side == "BUY" else "short"
        if row is None:
            return target_side, quantity, price, 0.0

        old_side = row["side"]
        old_quantity = float(row["quantity"])
        old_entry = float(row["entry_price"])
        realized = float(row["realized_pnl"])

        if old_side == target_side:
            new_quantity = old_quantity + quantity
            new_entry = ((old_quantity * old_entry) + (quantity * price)) / new_quantity
            return old_side, new_quantity, new_entry, realized

        closing = min(old_quantity, quantity)
        if old_side == "long":
            realized += (price - old_entry) * closing
        else:
            realized += (old_entry - price) * closing

        remaining = old_quantity - quantity
        if is_flat_quantity(remaining):
            return "flat", 0.0, 0.0, realized
        if remaining > 0:
            return old_side, remaining, old_entry, realized
        return target_side, abs(remaining), price, realized

    @staticmethod
    def _apply_futures_fill_to_state(
        state: dict[str, float | str],
        side: str,
        quantity: float,
        price: float,
    ) -> float:
        target_side = "long" if side == "BUY" else "short"
        old_side = str(state["side"])
        old_quantity = float(state["quantity"])
        old_entry = float(state["entry"])
        if old_side == "flat" or old_quantity == 0:
            state["side"] = target_side
            state["quantity"] = quantity
            state["entry"] = price
            return 0.0
        if old_side == target_side:
            new_quantity = old_quantity + quantity
            state["quantity"] = new_quantity
            state["entry"] = ((old_quantity * old_entry) + (quantity * price)) / new_quantity
            return 0.0

        closing = min(old_quantity, quantity)
        realized = (price - old_entry) * closing if old_side == "long" else (old_entry - price) * closing
        remaining = old_quantity - quantity
        if is_flat_quantity(remaining):
            state["side"] = "flat"
            state["quantity"] = 0.0
            state["entry"] = 0.0
        elif remaining > 0:
            state["quantity"] = remaining
        elif remaining < 0:
            state["side"] = target_side
            state["quantity"] = abs(remaining)
            state["entry"] = price
        return realized
