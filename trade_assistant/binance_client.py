from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.error
import urllib.request
from math import ceil
from typing import Any


class MarketDataUnavailable(RuntimeError):
    user_facing = True


class BinanceAuthError(RuntimeError):
    user_facing = True


class BinanceRateLimitError(RuntimeError):
    user_facing = True


class BinanceNetworkError(RuntimeError):
    user_facing = True


_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 90
_rate_limit_until = 0.0


class BinanceClient:
    SPOT_BASE = "https://api.binance.com"
    FUTURES_BASE = "https://fapi.binance.com"
    SPOT_PUBLIC_BASES = (SPOT_BASE, "https://api1.binance.com", "https://api2.binance.com")
    FUTURES_PUBLIC_BASES = (FUTURES_BASE, "https://fapi1.binance.com", "https://fapi2.binance.com")

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")

    def get_json(self, url: str, timeout: int = 8) -> Any:
        _raise_if_rate_limited("公共行情")
        last_error: Exception | None = None
        for attempt in range(2):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    if not raw.strip():
                        raise json.JSONDecodeError("empty Binance response", raw, 0)
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                message = _http_error_body(exc)
                if exc.code == 451:
                    raise MarketDataUnavailable("Binance 公共行情接口返回 451：当前网络/IP 可能被 Binance 限制，请切换到可访问 Binance API 的网络后再扫描。") from exc
                if exc.code == 429:
                    raise _rate_limit_error(exc, "公共行情", message) from exc
                raise MarketDataUnavailable(f"Binance 公共行情接口 HTTP {exc.code}：{message or exc.reason}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.25)
        raise MarketDataUnavailable(f"Binance 公共行情接口连接失败（已重试）：{last_error}") from last_error

    def public_get(self, market: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        bases = self.FUTURES_PUBLIC_BASES if market == "futures" else self.SPOT_PUBLIC_BASES
        last_error: MarketDataUnavailable | None = None
        for base in bases:
            url = f"{base}{path}" + (f"?{query}" if query else "")
            try:
                return self.get_json(url)
            except MarketDataUnavailable as exc:
                last_error = exc
                if "连接失败" not in str(exc) and "连接超时" not in str(exc):
                    raise
        raise last_error or MarketDataUnavailable("Binance 公共行情接口不可用")

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

    def order_book(self, market: str, symbol: str, limit: int = 20) -> Any:
        path = "/fapi/v1/depth" if market == "futures" else "/api/v3/depth"
        return self.public_get(market, path, {"symbol": symbol.upper(), "limit": limit})

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
        return self._signed_urlopen(req, "下单")

    def signed_delete(self, market: str, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET are required for signed requests.")
        query_params = dict(params)
        query_params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(query_params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        base = self.FUTURES_BASE if market == "futures" else self.SPOT_BASE
        req = urllib.request.Request(
            f"{base}{path}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": self.api_key},
            method="DELETE",
        )
        return self._signed_urlopen(req, "撤单")

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
        return self._signed_urlopen(req, "读取账户")

    def _signed_urlopen(self, req: urllib.request.Request, action: str) -> Any:
        _raise_if_rate_limited(action)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    raise BinanceNetworkError(f"Binance {action}接口返回空响应，本轮不执行并将在下一轮重试")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise BinanceNetworkError(f"Binance {action}接口返回无效数据，本轮不执行并将在下一轮重试") from exc
        except urllib.error.HTTPError as exc:
            message = _http_error_body(exc)
            if exc.code in {401, 403}:
                raise BinanceAuthError(_auth_error_text(exc.code, action, message)) from exc
            if exc.code == 429:
                raise _rate_limit_error(exc, action, message) from exc
            raise RuntimeError(f"Binance {action}接口 HTTP {exc.code}：{message or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BinanceNetworkError(_network_error_text(action, exc)) from exc

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

    def futures_account(self) -> Any:
        return self.signed_get("futures", "/fapi/v3/account", {"recvWindow": 5000})

    def futures_position_mode(self) -> Any:
        return self.signed_get("futures", "/fapi/v1/positionSide/dual", {"recvWindow": 5000})

    def set_futures_leverage(self, symbol: str, leverage: int) -> Any:
        if leverage < 1:
            raise ValueError("futures leverage must be at least 1")
        return self.signed_post(
            "futures",
            "/fapi/v1/leverage",
            {"symbol": symbol.upper(), "leverage": int(leverage), "recvWindow": 5000},
        )

    def query_order(self, market: str, symbol: str, client_order_id: str) -> Any:
        path = "/fapi/v1/order" if market == "futures" else "/api/v3/order"
        return self.signed_get(
            market,
            path,
            {"symbol": symbol.upper(), "origClientOrderId": client_order_id, "recvWindow": 5000},
        )

    def query_algo_order(
        self,
        symbol: str,
        client_order_id: str,
        algo_id: str | None = None,
    ) -> Any:
        """Query a Futures conditional/algo order using its actual API namespace."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "recvWindow": 5000}
        if algo_id:
            params["algoId"] = str(algo_id)
        else:
            params["clientAlgoId"] = client_order_id
        return self.signed_get("futures", "/fapi/v1/algoOrder", params)

    def cancel_order(self, market: str, symbol: str, client_order_id: str) -> Any:
        path = "/fapi/v1/order" if market == "futures" else "/api/v3/order"
        return self.signed_delete(
            market,
            path,
            {"symbol": symbol.upper(), "origClientOrderId": client_order_id, "recvWindow": 5000},
        )

    def cancel_algo_order(
        self,
        symbol: str,
        client_order_id: str,
        algo_id: str | None = None,
    ) -> Any:
        """Cancel a Futures conditional/algo order without falling back to normal orders."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "recvWindow": 5000}
        if algo_id:
            params["algoId"] = str(algo_id)
        else:
            params["clientAlgoId"] = client_order_id
        return self.signed_delete("futures", "/fapi/v1/algoOrder", params)

    def open_orders(self, market: str, symbol: str | None = None) -> Any:
        path = "/fapi/v1/openOrders" if market == "futures" else "/api/v3/openOrders"
        params: dict[str, Any] = {"recvWindow": 5000}
        if symbol:
            params["symbol"] = symbol.upper()
        return self.signed_get(market, path, params)

    def create_listen_key(self, market: str) -> str:
        path = "/fapi/v1/listenKey" if market == "futures" else "/api/v3/userDataStream"
        payload = self._api_key_request(market, path, "POST")
        return str(payload["listenKey"])

    def keepalive_listen_key(self, market: str, listen_key: str) -> Any:
        path = "/fapi/v1/listenKey" if market == "futures" else "/api/v3/userDataStream"
        return self._api_key_request(market, path, "PUT", {"listenKey": listen_key})

    def close_listen_key(self, market: str, listen_key: str) -> Any:
        path = "/fapi/v1/listenKey" if market == "futures" else "/api/v3/userDataStream"
        return self._api_key_request(market, path, "DELETE", {"listenKey": listen_key})

    def _api_key_request(
        self,
        market: str,
        path: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.api_key:
            raise RuntimeError("BINANCE_API_KEY is required for user data streams.")
        base = self.FUTURES_BASE if market == "futures" else self.SPOT_BASE
        query = urllib.parse.urlencode(params or {})
        url = f"{base}{path}"
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(
            url,
            data=b"" if method in {"POST", "PUT"} else None,
            headers={"X-MBX-APIKEY": self.api_key},
            method=method,
        )
        _raise_if_rate_limited("启动账户实时流")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            message = _http_error_body(exc)
            if exc.code in {401, 403}:
                raise BinanceAuthError(_auth_error_text(exc.code, "启动账户实时流", message)) from exc
            if exc.code == 429:
                raise _rate_limit_error(exc, "启动账户实时流", message) from exc
            raise RuntimeError(f"Binance 启动账户实时流接口 HTTP {exc.code}：{message or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BinanceNetworkError(_network_error_text("启动账户实时流", exc)) from exc


def reset_rate_limit_backoff() -> None:
    global _rate_limit_until
    _rate_limit_until = 0.0


def _raise_if_rate_limited(action: str) -> None:
    remaining = _rate_limit_remaining_seconds()
    if remaining <= 0:
        return
    raise BinanceRateLimitError(
        f"Binance {action} REST 请求已暂停，约 {ceil(remaining)} 秒后再试。"
        "原因：上一轮触发 HTTP 429 限流。实时行情请使用 WebSocket，冷却结束前不要继续轮询。"
    )


def _rate_limit_remaining_seconds() -> float:
    return max(0.0, _rate_limit_until - time.time())


def _rate_limit_error(exc: urllib.error.HTTPError, action: str, message: str) -> BinanceRateLimitError:
    global _rate_limit_until
    backoff_seconds = _retry_after_seconds(exc) or _DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
    _rate_limit_until = max(_rate_limit_until, time.time() + backoff_seconds)
    detail = f"；Binance 返回：{message}" if message else ""
    return BinanceRateLimitError(
        f"Binance {action}接口触发限流 HTTP 429{detail}。"
        f"软件已自动暂停 REST 请求约 {backoff_seconds} 秒；请先停止实时监控/自动扫描，"
        "行情更新优先走 WebSocket，真实仓用 User Data Stream，REST 只做低频对账。"
    )


def _retry_after_seconds(exc: urllib.error.HTTPError) -> int | None:
    headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
    if not headers:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        value = None
    if not value:
        return None
    try:
        return max(1, int(float(value)))
    except ValueError:
        return None


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8")
    except Exception:
        return str(exc.reason)
    if not raw:
        return str(exc.reason)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    code = payload.get("code")
    message = payload.get("msg") or raw
    return f"{code}: {message}" if code is not None else str(message)


def _auth_error_text(status_code: int, action: str, message: str) -> str:
    reasons = (
        "请检查：1）API Key / Secret 是否复制完整；2）是否用了 Testnet Key；"
        "3）是否开启了合约读取/交易权限；4）如果设置了 IP 白名单，当前网络出口 IP 是否在白名单；"
        "5）电脑时间是否准确。"
    )
    detail = f"；Binance 返回：{message}" if message else ""
    return f"Binance {action}认证失败 HTTP {status_code}{detail}。{reasons}"


def _network_error_text(action: str, exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    return (
        f"Binance {action}接口网络超时或连接失败：{reason}。"
        "软件会保留状态并等待下一轮重试；如果连续出现，请检查网络/VPN/代理和 Binance API 可访问性。"
    )
