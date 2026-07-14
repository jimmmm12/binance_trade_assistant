from __future__ import annotations

from trade_assistant.trading_system.data.market_data import MarketDataValidator, MarketEvent
from trade_assistant.trading_system.research.performance import PerformanceAnalyzer
from trade_assistant.trading_system.storage.database import TradingDatabase


def test_market_data_validator_rejects_stale_or_incomplete_event() -> None:
    event = MarketEvent(
        market="futures",
        symbol="BTCUSDT",
        price=60000,
        volume=10,
        timestamp=100,
        candles=[[1, 2, 3]],
        order_book=None,
        source="websocket",
    )

    errors = MarketDataValidator.validate(event, now=110, max_age_seconds=5)

    assert "行情过期" in errors
    assert "K线字段不完整" in errors


def test_performance_analyzer_reports_profit_factor_and_drawdown(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "performance.db")
    with store._connect() as conn:
        for index, pnl in enumerate([10, -4, -3, 8]):
            conn.execute(
                """
                INSERT INTO trades(
                    market, symbol, side, entry, exit, quantity, remaining_quantity,
                    pnl, strategy, opened_at, closed_at
                ) VALUES('futures', 'UNIUSDT', 'long', 10, 11, 1, 0, ?, 'test', ?, ?)
                """,
                (pnl, f"2026-07-10T00:00:0{index}", f"2026-07-10T00:01:0{index}"),
            )

    report = PerformanceAnalyzer(store).analyze()

    assert report.trades == 4
    assert report.total_pnl == 11
    assert report.profit_factor == round(18 / 7, 4)
    assert report.max_drawdown == 7


def test_database_tracks_recent_automatic_entries_for_frequency_limits(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "orders.db")
    store.upsert_order(
        {
            "client_order_id": "AUTO_1",
            "exchange_order_id": "1",
            "market": "futures",
            "symbol": "UNIUSDT",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 1,
            "price": 10,
            "status": "FILLED",
            "strategy": "automatic:trend",
            "reduce_only": False,
            "created_at": "2026-07-12T08:00:00",
        }
    )

    latest = store.latest_automatic_entry("uniusdt")

    assert latest is not None
    assert latest["client_order_id"] == "AUTO_1"
    assert store.automatic_entry_count_since("UNIUSDT", "2026-07-12T00:00:00") == 1


def test_database_can_reset_consecutive_losses_without_deleting_trades(tmp_path) -> None:
    store = TradingDatabase(tmp_path / "losses.db")
    with store._connect() as conn:
        for index, pnl in enumerate([-1, -2, -3]):
            conn.execute(
                """
                INSERT INTO trades(
                    market, symbol, side, entry, exit, quantity, remaining_quantity,
                    pnl, strategy, opened_at, closed_at
                ) VALUES('futures', 'UNIUSDT', 'long', 10, 9, 1, 0, ?, 'test', ?, ?)
                """,
                (pnl, f"2026-07-12T00:00:0{index}", f"2026-07-12T00:01:0{index}"),
            )

    assert store.consecutive_losses() == 3

    reset_at = store.reset_consecutive_losses("test-reset")

    assert store.loss_streak_reset_at() == reset_at
    assert store.consecutive_losses() == 0
    assert store.closed_trade_pnls() == [-1, -2, -3]

    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO trades(
                market, symbol, side, entry, exit, quantity, remaining_quantity,
                pnl, strategy, opened_at, closed_at
            ) VALUES('futures', 'DOTUSDT', 'long', 10, 9, 1, 0, -4, 'test', ?, ?)
            """,
            ("2099-01-01T00:00:00", "2099-01-01T00:01:00"),
        )

    assert store.consecutive_losses() == 1
