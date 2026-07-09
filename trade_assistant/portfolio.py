from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .main import ROOT
from .models import FuturesAccountRisk, PositionSnapshot


DEFAULT_SIM_BALANCE = 10000.0
DEFAULT_DB_PATH = ROOT / "data" / "sim_account.db"
REAL_LOSS_INCOME_TYPES = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}


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
        if amount == 0:
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
    return normalize_futures_account_risk(client.futures_account_balance(), client.futures_positions())


def normalize_futures_account_risk(balance_rows: list[dict], position_rows: list[dict]) -> FuturesAccountRisk:
    usdt = next((row for row in balance_rows if row.get("asset") == "USDT"), {})
    positions = [
        normalize_futures_position([row], row.get("symbol", ""))
        for row in position_rows
        if float(row.get("positionAmt", 0)) != 0
    ]
    return FuturesAccountRisk(
        wallet_balance=float(usdt.get("balance", 0)),
        available_balance=float(usdt.get("availableBalance", 0)),
        total_unrealized_pnl=sum(position.unrealized_pnl for position in positions),
        positions=positions,
    )


def _optional_float(value) -> float | None:
    if value in (None, "", "0", 0):
        return None
    return float(value)


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
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, market, symbol)
                )
                """
            )
            row = conn.execute("SELECT balance FROM sim_account WHERE asset = 'USDT'").fetchone()
            if row is None:
                conn.execute("INSERT INTO sim_account(asset, balance) VALUES('USDT', ?)", (DEFAULT_SIM_BALANCE,))

    def cash_balance(self, asset: str = "USDT") -> float:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute("SELECT balance FROM sim_account WHERE asset = ?", (asset,)).fetchone()
        return float(row["balance"]) if row else 0.0

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
                    stop_price, target_price, realized_pnl, status, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, market, symbol) DO UPDATE SET
                    side = excluded.side,
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    mark_price = excluded.mark_price,
                    stop_price = excluded.stop_price,
                    target_price = excluded.target_price,
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
                    realized_pnl,
                    status,
                    now,
                ),
            )

    def position_records(self) -> list[dict]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM position_records
                ORDER BY updated_at DESC
                """
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
            if new_quantity == 0:
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
        if remaining > 0:
            return old_side, remaining, old_entry, realized
        if remaining < 0:
            return target_side, abs(remaining), price, realized
        return target_side, 0.0, price, realized

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
        if remaining > 0:
            state["quantity"] = remaining
        elif remaining < 0:
            state["side"] = target_side
            state["quantity"] = abs(remaining)
            state["entry"] = price
        else:
            state["side"] = "flat"
            state["quantity"] = 0.0
            state["entry"] = 0.0
        return realized
