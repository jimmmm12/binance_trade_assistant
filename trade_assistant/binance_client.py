from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Any


class MarketDataUnavailable(RuntimeError):
    user_facing = True


class BinanceClient:
    SPOT_BASE = "https://api.binance.com"
    FUTURES_BASE = "https://fapi.binance.com"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")

    def get_json(self, url: str, timeout: int = 20) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 451:
                raise MarketDataUnavailable(
                    "Binance 公共行情接口返回 451：当前网络/IP 可能被 Binance 限制，"
                    "请切换到可访问 Binance API 的网络后再扫描。"
                ) from exc
            raise MarketDataUnavailable(f"Binance 公共行情接口 HTTP {exc.code}：{exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MarketDataUnavailable(f"Binance 公共行情接口连接失败：{exc.reason}") from exc

    def public_get(self, market: str, path: str, params: dict[str, Any] | None = None) -> Any:
        base = self.FUTURES_BASE if market == "futures" else self.SPOT_BASE
        query = urllib.parse.urlencode(params or {})
        url = f"{base}{path}"
        if query:
            url = f"{url}?{query}"
        return self.get_json(url)

    def exchange_symbols(self, market: str, quote_asset: str = "USDT") -> set[str]:
        data = self.public_get(market, "/fapi/v1/exchangeInfo" if market == "futures" else "/api/v3/exchangeInfo")
        symbols: set[str] = set()
        for item in data["symbols"]:
            if item.get("quoteAsset") != quote_asset or item.get("status") != "TRADING":
                continue
            if market == "futures" and item.get("contractType") != "PERPETUAL":
                continue
            symbols.add(item["symbol"])
        return symbols

    def ticker_24h(self, market: str) -> list[dict[str, Any]]:
        path = "/fapi/v1/ticker/24hr" if market == "futures" else "/api/v3/ticker/24hr"
        return self.public_get(market, path)

    def latest_price(self, market: str, symbol: str) -> float:
        path = "/fapi/v1/ticker/price" if market == "futures" else "/api/v3/ticker/price"
        row = self.public_get(market, path, {"symbol": symbol.upper()})
        return float(row["price"])

    def premium_index(self) -> dict[str, dict[str, Any]]:
        rows = self.public_get("futures", "/fapi/v1/premiumIndex")
        return {row["symbol"]: row for row in rows}

    def klines(self, market: str, symbol: str, interval: str, limit: int = 120) -> list[list[Any]]:
        path = "/fapi/v1/klines" if market == "futures" else "/api/v3/klines"
        return self.public_get(market, path, {"symbol": symbol, "interval": interval, "limit": limit})

    def signed_post(self, market: str, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET are required for live orders.")
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        body = f"{query}&signature={signature}".encode()
        base = self.FUTURES_BASE if market == "futures" else self.SPOT_BASE
        req = urllib.request.Request(
            f"{base}{path}",
            data=body,
            headers={"X-MBX-APIKEY": self.api_key, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def signed_get(self, market: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET are required for signed account reads.")
        query_params = dict(params or {})
        query_params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(query_params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        base = self.FUTURES_BASE if market == "futures" else self.SPOT_BASE
        req = urllib.request.Request(
            f"{base}{path}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": self.api_key},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def spot_account(self) -> Any:
        return self.signed_get("spot", "/api/v3/account", {"recvWindow": 5000})

    def futures_positions(self, symbol: str | None = None) -> Any:
        params = {"recvWindow": 5000}
        if symbol:
            params["symbol"] = symbol.upper()
        return self.signed_get("futures", "/fapi/v3/positionRisk", params)

    def futures_income_history(self, start_time: int, end_time: int | None = None) -> Any:
        params: dict[str, Any] = {"startTime": start_time, "recvWindow": 5000, "limit": 1000}
        if end_time is not None:
            params["endTime"] = end_time
        return self.signed_get("futures", "/fapi/v1/income", params)

    def futures_account_balance(self) -> Any:
        return self.signed_get("futures", "/fapi/v3/balance", {"recvWindow": 5000})
