from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

FUTURES_STREAM_BASES = (
    "wss://fstream.binancefuture.com/stream?streams=",
    "wss://fstream.binance.com/stream?streams=",
)
SPOT_STREAM_BASES = (
    "wss://stream.binance.com:9443/stream?streams=",
)


@dataclass(frozen=True)
class StreamPrice:
    market: str
    symbol: str
    price: float
    updated_at: float

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.updated_at)


@dataclass(frozen=True)
class StreamQuote:
    market: str
    symbol: str
    best_bid: float
    best_ask: float
    updated_at: float

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.updated_at)


class BinanceWebSocketPriceCache:
    def __init__(self, stale_after_seconds: float = 5.0) -> None:
        self.stale_after_seconds = stale_after_seconds
        self._prices: dict[tuple[str, str], StreamPrice] = {}
        self._quotes: dict[tuple[str, str], StreamQuote] = {}
        self._symbols: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._ws: Any = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.last_error: str | None = None
        self.connected = False
        self.active_url: str | None = None

    def update_symbols(self, targets: list[tuple[str, str]]) -> None:
        normalized = {(market, symbol.upper()) for market, symbol in targets if symbol}
        with self._lock:
            if normalized == self._symbols:
                return
            self._symbols = normalized
        if self._running:
            self.restart()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self) -> None:
        self._running = False
        self.connected = False
        self.active_url = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def latest_price(self, market: str, symbol: str, max_age_seconds: float | None = None) -> float | None:
        max_age = self.stale_after_seconds if max_age_seconds is None else max_age_seconds
        with self._lock:
            item = self._prices.get((market, symbol.upper()))
        if item is None or item.age_seconds() > max_age:
            return None
        return item.price

    def latest_quote(
        self,
        market: str,
        symbol: str,
        max_age_seconds: float | None = None,
    ) -> StreamQuote | None:
        max_age = self.stale_after_seconds if max_age_seconds is None else max_age_seconds
        with self._lock:
            item = self._quotes.get((market, symbol.upper()))
        if item is None or item.age_seconds() > max_age:
            return None
        return item

    def freshness_text(self) -> str:
        with self._lock:
            prices = list(self._prices.values())
            symbol_count = len(self._symbols)
        if not self._running:
            return "WebSocket 未启动"
        if not prices:
            if self.last_error:
                return f"WebSocket 等待行情，最近错误：{self.last_error}"
            return "WebSocket 等待行情"
        newest_age = min(price.age_seconds() for price in prices)
        state = "已连接" if self.connected else "重连中"
        return f"WebSocket {state}，订阅 {symbol_count} 个，最新 {newest_age:.1f}s 前"

    def _run_forever(self) -> None:
        while self._running:
            streams = self._stream_names()
            if not streams:
                time.sleep(0.5)
                continue
            for url in self._stream_urls(streams):
                if not self._running:
                    break
                self.active_url = url
                try:
                    import websocket

                    websocket.setdefaulttimeout(10)
                    self._ws = websocket.WebSocketApp(
                        url,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close,
                        on_open=self._on_open,
                    )
                    self._ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as exc:
                    self.last_error = str(exc)
                    self.connected = False
                if self.connected or self._has_fresh_prices():
                    break
            if self._running:
                time.sleep(2)

    def _stream_names(self) -> list[str]:
        with self._lock:
            symbols = sorted(self._symbols)
        names: list[str] = []
        for market, symbol in symbols:
            names.append(f"{symbol.lower()}@markPrice@1s" if market == "futures" else f"{symbol.lower()}@ticker")
            names.append(f"{symbol.lower()}@bookTicker")
        return names

    def _stream_urls(self, streams: list[str]) -> list[str]:
        with self._lock:
            markets = {market for market, _ in self._symbols}
        bases = SPOT_STREAM_BASES if markets == {"spot"} else FUTURES_STREAM_BASES
        joined = "/".join(streams)
        return [f"{base}{joined}" for base in bases]

    def _has_fresh_prices(self) -> bool:
        with self._lock:
            prices = list(self._prices.values())
        return any(price.age_seconds() <= self.stale_after_seconds for price in prices)

    def _on_open(self, ws: Any) -> None:
        self.connected = True
        self.last_error = None

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        self.connected = False

    def _on_error(self, ws: Any, error: Any) -> None:
        self.last_error = str(error)
        self.connected = False

    def _on_message(self, ws: Any, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            self.last_error = "WebSocket 收到无效行情帧，已忽略"
            return
        if not isinstance(payload, dict):
            return
        stream = str(payload.get("stream", ""))
        data = payload.get("data", {})
        symbol = str(data.get("s", "")).upper()
        if not symbol:
            return
        market = self._market_for_symbol(symbol, stream)
        if market is None:
            return
        if "@bookTicker" in stream:
            try:
                best_bid = float(data.get("b"))
                best_ask = float(data.get("a"))
            except (TypeError, ValueError):
                return
            if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                return
            item = StreamQuote(market, symbol, best_bid, best_ask, time.time())
            with self._lock:
                self._quotes[(market, symbol)] = item
            return
        raw_price = data.get("p") if market == "futures" else data.get("c")
        if raw_price in (None, ""):
            return
        item = StreamPrice(market=market, symbol=symbol, price=float(raw_price), updated_at=time.time())
        with self._lock:
            self._prices[(market, symbol)] = item

    def _market_for_symbol(self, symbol: str, stream: str) -> str | None:
        with self._lock:
            matches = [market for market, target in self._symbols if target == symbol]
        if len(matches) == 1:
            return matches[0]
        if "@markPrice" in stream:
            return "futures"
        return "spot" if matches else None
