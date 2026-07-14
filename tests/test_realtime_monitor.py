from __future__ import annotations

from trade_assistant.realtime_monitor import MonitorTarget, evaluate_monitor_target
from trade_assistant.market_stream import BinanceWebSocketPriceCache, StreamPrice
import time


def test_monitor_long_target_reports_r_milestones_and_target() -> None:
    target = MonitorTarget(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry=10,
        stop=9,
        target=12,
    )

    result = evaluate_monitor_target(target, price=11.5)

    assert result.r_multiple == 1.5
    assert result.unrealized_pnl == 15
    assert "1R" in result.alert_text
    assert "1.5R" in result.alert_text
    assert "减仓" in result.alert_text


def test_monitor_short_target_reports_stop_loss() -> None:
    target = MonitorTarget(
        market="futures",
        symbol="UNIUSDT",
        side="short",
        quantity=10,
        entry=10,
        stop=10.5,
        target=9,
    )

    result = evaluate_monitor_target(target, price=10.6)

    assert result.r_multiple < 0
    assert "触发止损" in result.alert_text
    assert result.severity == "danger"


def test_monitor_warns_when_price_is_close_to_liquidation() -> None:
    target = MonitorTarget(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry=10,
        stop=9,
        target=12,
        liquidation_price=9.7,
    )

    result = evaluate_monitor_target(target, price=10)

    assert "接近强平" in result.alert_text
    assert result.severity == "danger"


def test_websocket_price_cache_rejects_stale_prices() -> None:
    cache = BinanceWebSocketPriceCache(stale_after_seconds=1)
    cache._prices[("futures", "UNIUSDT")] = StreamPrice("futures", "UNIUSDT", 10.0, time.time() - 2)

    assert cache.latest_price("futures", "UNIUSDT") is None

    cache._prices[("futures", "UNIUSDT")] = StreamPrice("futures", "UNIUSDT", 11.0, time.time())

    assert cache.latest_price("futures", "UNIUSDT") == 11.0


def test_futures_websocket_uses_binancefuture_endpoint_first() -> None:
    cache = BinanceWebSocketPriceCache()
    cache.update_symbols([("futures", "UNIUSDT")])

    urls = cache._stream_urls(cache._stream_names())

    assert urls[0].startswith("wss://fstream.binancefuture.com/")
    assert "uniusdt@markPrice@1s" in urls[0]
    assert urls[1].startswith("wss://fstream.binance.com/")


def test_websocket_freshness_reports_last_error_while_waiting() -> None:
    cache = BinanceWebSocketPriceCache()
    cache.update_symbols([("futures", "UNIUSDT")])
    cache._running = True
    cache.last_error = "timed out"

    assert "最近错误：timed out" in cache.freshness_text()


def test_websocket_price_cache_ignores_invalid_json_frame() -> None:
    cache = BinanceWebSocketPriceCache()

    cache._on_message(None, "")

    assert cache.last_error is not None
    assert "无效行情帧" in cache.last_error
