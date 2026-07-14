from __future__ import annotations

from urllib.error import HTTPError

from trade_assistant.binance_client import BinanceAuthError, BinanceClient, BinanceNetworkError, BinanceRateLimitError
from trade_assistant.binance_client import MarketDataUnavailable
from trade_assistant.binance_client import reset_rate_limit_backoff
from trade_assistant.portfolio import SimulatedPortfolio
from trade_assistant.portfolio import normalize_futures_position, normalize_spot_position
from trade_assistant.portfolio import normalize_futures_account_risk


def test_simulated_portfolio_initializes_with_default_usdt(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()

    assert portfolio.cash_balance() == 1000


def test_simulated_buy_creates_long_position_and_reduces_cash(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()

    position = portfolio.apply_fill("spot", "UNIUSDT", "BUY", quantity=10, price=3.0)

    assert position.side == "long"
    assert position.quantity == 10
    assert position.entry_price == 3.0
    assert portfolio.cash_balance() == 970


def test_simulated_sell_reduces_long_position_and_realizes_pnl(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("spot", "UNIUSDT", "BUY", quantity=10, price=3.0)

    position = portfolio.apply_fill("spot", "UNIUSDT", "SELL", quantity=4, price=3.5)

    assert position.side == "long"
    assert position.quantity == 6
    assert position.realized_pnl == 2.0
    assert portfolio.cash_balance() == 984


def test_simulated_futures_sell_opens_short_position(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()

    position = portfolio.apply_fill("futures", "UNIUSDT", "SELL", quantity=10, price=3.0)

    assert position.side == "short"
    assert position.quantity == 10
    assert position.entry_price == 3.0
    assert portfolio.cash_balance() == 1000


def test_simulated_futures_buy_reduces_short_and_realizes_pnl(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("futures", "UNIUSDT", "SELL", quantity=10, price=3.0)

    position = portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=4, price=2.5)

    assert position.side == "short"
    assert position.quantity == 6
    assert position.realized_pnl == 2.0
    assert portfolio.cash_balance() == 1002


def test_legacy_empty_sim_account_migrates_to_configured_default_equity(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    with portfolio._connect() as conn:
        conn.execute("UPDATE sim_account SET balance = 10000 WHERE asset = 'USDT'")

    migrated = SimulatedPortfolio(tmp_path / "sim.db")

    assert migrated.cash_balance() == 1000


def test_legacy_sim_account_with_open_position_is_not_migrated(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("futures", "UNIUSDT", "SELL", quantity=1, price=3.0)
    with portfolio._connect() as conn:
        conn.execute("UPDATE sim_account SET balance = 10000 WHERE asset = 'USDT'")

    migrated = SimulatedPortfolio(tmp_path / "sim.db")

    assert migrated.cash_balance() == 10000


def test_simulated_futures_tiny_residual_is_treated_as_flat(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("futures", "LABUSDT", "SELL", quantity=1.000000001, price=1.1938)

    position = portfolio.apply_fill("futures", "LABUSDT", "BUY", quantity=1.0, price=1.1929)

    assert position.side == "flat"
    assert position.quantity == 0
    assert portfolio.get_position("futures", "LABUSDT").side == "flat"


def test_today_realized_pnl_counts_futures_short_losses(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("futures", "UNIUSDT", "SELL", quantity=10, price=3.0)
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=10, price=3.5)

    assert portfolio.today_realized_pnl() == -5.0


def test_position_records_store_plan_stop_target_and_pnl(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=3.0,
        mark_price=3.2,
        stop_price=2.8,
        target_price=3.6,
        leverage=5,
        realized_pnl=1.5,
        status="模拟持仓",
    )

    rows = portfolio.position_records()

    assert rows[0]["symbol"] == "UNIUSDT"
    assert rows[0]["stop_price"] == 2.8
    assert rows[0]["target_price"] == 3.6
    assert rows[0]["leverage"] == 5
    assert rows[0]["unrealized_pnl"] == 2.0


def test_planned_position_records_are_hidden_from_active_positions(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=3.0,
        mark_price=3.2,
        stop_price=2.8,
        target_price=3.6,
        status="自动计划",
    )

    assert portfolio.position_records() == []
    rows = portfolio.position_records(active_only=False)
    assert rows[0]["symbol"] == "UNIUSDT"


def test_position_record_mark_price_can_be_updated_for_realtime_pnl(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=3.0,
        mark_price=3.0,
        stop_price=2.8,
        target_price=3.6,
        status="模拟持仓",
    )

    portfolio.update_position_record_mark_price("simulated", "futures", "UNIUSDT", 3.4)

    rows = portfolio.position_records()
    assert rows[0]["mark_price"] == 3.4
    assert rows[0]["unrealized_pnl"] == 4.0


def test_closed_position_records_are_hidden_by_default(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=3.0,
        mark_price=3.2,
        stop_price=2.8,
        target_price=3.6,
        status="模拟持仓",
    )

    portfolio.close_position_record("simulated", "futures", "UNIUSDT", 3.2)

    assert portfolio.position_records() == []
    rows = portfolio.position_records(active_only=False)
    assert rows[0]["side"] == "flat"
    assert rows[0]["quantity"] == 0
    assert rows[0]["status"] == "已平仓/空仓"


def test_tiny_position_records_are_hidden_by_default(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="LABUSDT",
        side="short",
        quantity=0.000000001,
        entry_price=1.1938,
        mark_price=1.1929,
        stop_price=0,
        target_price=0,
        status="模拟持仓",
    )

    assert portfolio.position_records() == []
    assert portfolio.position_records(active_only=False)[0]["quantity"] == 0.000000001


def test_clear_all_simulated_positions_closes_at_recorded_mark_prices(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=10, price=3.0)
    portfolio.apply_fill("futures", "AAVEUSDT", "SELL", quantity=2, price=100.0)
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=3.0,
        mark_price=3.2,
        stop_price=2.8,
        target_price=3.6,
        status="模拟持仓",
    )
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="AAVEUSDT",
        side="short",
        quantity=2,
        entry_price=100.0,
        mark_price=95.0,
        stop_price=110.0,
        target_price=85.0,
        status="模拟持仓",
    )

    count = portfolio.clear_all_positions()

    assert count == 2
    assert portfolio.get_position("futures", "UNIUSDT").side == "flat"
    assert portfolio.get_position("futures", "AAVEUSDT").side == "flat"
    assert portfolio.position_records() == []
    assert portfolio.today_realized_pnl() == 12.0
    assert portfolio.cash_balance() == 1012.0


def test_clear_all_simulated_positions_closes_orphan_sim_positions(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=10, price=3.0)

    assert portfolio.position_records() == []
    assert portfolio.open_position_count() == 1

    count = portfolio.clear_all_positions()

    assert count == 1
    assert portfolio.open_position_count() == 0
    assert portfolio.get_position("futures", "UNIUSDT").side == "flat"


def test_clear_all_simulated_positions_removes_tiny_dust_rows(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    with portfolio._connect() as conn:
        conn.execute(
            """
            INSERT INTO sim_positions(market, symbol, side, quantity, entry_price, realized_pnl, leverage, updated_at)
            VALUES('futures', 'TAGUSDT', 'long', ?, 0.001107, 0, 1, 'now')
            """,
            (0.00000000001,),
        )
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="TAGUSDT",
        side="long",
        quantity=0.0,
        entry_price=0.001107,
        mark_price=0.001107,
        stop_price=0,
        target_price=0,
        status="模拟持仓",
    )

    assert portfolio.open_position_count() == 0
    assert portfolio.simulated_residue_count() == 2

    count = portfolio.clear_all_positions()

    assert count == 1
    assert portfolio.simulated_residue_count() == 0


def test_signed_get_uses_signed_query(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return b'{"ok": true}'

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.time", lambda: 1)
    client = BinanceClient(api_key="key", api_secret="secret")

    result = client.signed_get("spot", "/api/v3/account", {"recvWindow": 5000})

    assert result == {"ok": True}
    assert "timestamp=1000" in captured["url"]
    assert "signature=" in captured["url"]
    assert captured["headers"]["X-mbx-apikey"] == "key"


def test_signed_get_converts_401_to_readable_auth_error(monkeypatch):
    class Response:
        def read(self):
            return b'{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'

        def close(self):
            pass

    def fake_urlopen_with_body(req, timeout):
        error = HTTPError(req.full_url, 401, "Unauthorized", {}, Response())
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen_with_body)
    client = BinanceClient(api_key="key", api_secret="secret")

    try:
        client.signed_get("futures", "/fapi/v3/positionRisk", {"recvWindow": 5000})
    except BinanceAuthError as exc:
        text = str(exc)
        assert "认证失败 HTTP 401" in text
        assert "API Key / Secret" in text
        assert "Invalid API-key" in text
    else:
        raise AssertionError("401 should be converted to BinanceAuthError")


def test_signed_get_converts_timeout_to_network_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = BinanceClient(api_key="key", api_secret="secret")

    try:
        client.signed_get("futures", "/fapi/v3/account", {"recvWindow": 5000})
    except BinanceNetworkError as exc:
        text = str(exc)
        assert "网络超时" in text
        assert "下一轮重试" in text
    else:
        raise AssertionError("timeout should become BinanceNetworkError")


def test_public_get_converts_binance_451_to_readable_market_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 451, "Unavailable For Legal Reasons", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = BinanceClient()

    try:
        client.exchange_symbols("futures")
    except MarketDataUnavailable as exc:
        assert "Binance 公共行情接口返回 451" in str(exc)
        assert "当前网络" in str(exc)
    else:
        raise AssertionError("HTTP 451 should become a readable market data error")


def test_public_get_converts_empty_response_to_market_data_error(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return b""

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: Response())
    client = BinanceClient()

    try:
        client.latest_price("futures", "UNIUSDT")
    except MarketDataUnavailable as exc:
        assert "连接失败" in str(exc)
    else:
        raise AssertionError("empty response should become a recoverable market data error")


def test_public_get_429_enters_rate_limit_backoff(monkeypatch):
    reset_rate_limit_backoff()
    calls = {"count": 0}

    class Response:
        def read(self):
            return b'{"code":-1003,"msg":"Too many requests"}'

        def close(self):
            pass

    def fake_urlopen(req, timeout):
        calls["count"] += 1
        raise HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "12"}, Response())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = BinanceClient()

    try:
        client.latest_price("futures", "UNIUSDT")
    except BinanceRateLimitError as exc:
        assert "HTTP 429" in str(exc)
        assert "12 秒" in str(exc)
    else:
        raise AssertionError("HTTP 429 should enter rate limit backoff")

    try:
        client.latest_price("futures", "UNIUSDT")
    except BinanceRateLimitError as exc:
        assert "REST 请求已暂停" in str(exc)
    else:
        raise AssertionError("cooldown should block the next REST call")
    assert calls["count"] == 1
    reset_rate_limit_backoff()


def test_latest_price_uses_market_specific_ticker_endpoint(monkeypatch):
    reset_rate_limit_backoff()
    captured = {}

    def fake_get_json(url):
        captured["url"] = url
        return {"symbol": "UNIUSDT", "price": "3.25"}

    client = BinanceClient()
    monkeypatch.setattr(client, "get_json", fake_get_json)

    price = client.latest_price("futures", "UNIUSDT")

    assert price == 3.25
    assert "/fapi/v1/ticker/price" in captured["url"]
    assert "symbol=UNIUSDT" in captured["url"]


def test_normalize_spot_position_from_account_payload():
    payload = {
        "balances": [
            {"asset": "UNI", "free": "3", "locked": "2"},
            {"asset": "BTC", "free": "0", "locked": "0"},
        ]
    }

    position = normalize_spot_position(payload, "UNIUSDT", mark_price=3.2)

    assert position.source == "real"
    assert position.side == "long"
    assert position.quantity == 5
    assert position.notional == 16


def test_normalize_futures_position_long_and_short():
    rows = [
        {
            "symbol": "UNIUSDT",
            "positionAmt": "-4",
            "entryPrice": "3.5",
            "markPrice": "3.25",
            "unRealizedProfit": "1.0",
            "leverage": "2",
            "liquidationPrice": "4.8",
            "marginType": "isolated",
            "isolatedMargin": "20",
        }
    ]

    position = normalize_futures_position(rows, "UNIUSDT")

    assert position.source == "real"
    assert position.side == "short"
    assert position.quantity == 4
    assert position.entry_price == 3.5
    assert position.liquidation_price == 4.8
    assert position.margin_type == "isolated"


def test_futures_income_history_calls_income_endpoint(monkeypatch):
    captured = {}

    def fake_signed_get(market, path, params):
        captured["market"] = market
        captured["path"] = path
        captured["params"] = params
        return [{"incomeType": "REALIZED_PNL", "income": "-12.5"}]

    client = BinanceClient(api_key="key", api_secret="secret")
    monkeypatch.setattr(client, "signed_get", fake_signed_get)

    rows = client.futures_income_history(start_time=1000, end_time=2000)

    assert rows[0]["income"] == "-12.5"
    assert captured["market"] == "futures"
    assert captured["path"] == "/fapi/v1/income"
    assert captured["params"]["startTime"] == 1000


def test_futures_account_balance_calls_balance_endpoint(monkeypatch):
    captured = {}

    def fake_signed_get(market, path, params):
        captured["market"] = market
        captured["path"] = path
        return [{"asset": "USDT", "balance": "1200", "availableBalance": "900"}]

    client = BinanceClient(api_key="key", api_secret="secret")
    monkeypatch.setattr(client, "signed_get", fake_signed_get)

    rows = client.futures_account_balance()

    assert rows[0]["availableBalance"] == "900"
    assert captured["market"] == "futures"
    assert captured["path"] == "/fapi/v3/balance"


def test_futures_account_calls_account_endpoint(monkeypatch):
    captured = {}

    def fake_signed_get(market, path, params):
        captured["market"] = market
        captured["path"] = path
        return {"totalWalletBalance": "18", "availableBalance": "17.5"}

    client = BinanceClient(api_key="key", api_secret="secret")
    monkeypatch.setattr(client, "signed_get", fake_signed_get)

    payload = client.futures_account()

    assert payload["totalWalletBalance"] == "18"
    assert captured["market"] == "futures"
    assert captured["path"] == "/fapi/v3/account"


def test_futures_position_mode_calls_dual_side_endpoint(monkeypatch):
    captured = {}

    def fake_signed_get(market, path, params):
        captured["market"] = market
        captured["path"] = path
        return {"dualSidePosition": True}

    client = BinanceClient(api_key="key", api_secret="secret")
    monkeypatch.setattr(client, "signed_get", fake_signed_get)

    payload = client.futures_position_mode()

    assert payload["dualSidePosition"] is True
    assert captured["market"] == "futures"
    assert captured["path"] == "/fapi/v1/positionSide/dual"


def test_normalize_futures_account_risk_summarizes_positions_and_balance():
    risk = normalize_futures_account_risk(
        balance_rows=[{"asset": "USDT", "balance": "1200", "availableBalance": "900"}],
        position_rows=[
            {
                "symbol": "UNIUSDT",
                "positionAmt": "10",
                "entryPrice": "10",
                "markPrice": "10.5",
                "unRealizedProfit": "5",
                "leverage": "3",
                "liquidationPrice": "7.5",
                "marginType": "cross",
            }
        ],
    )

    assert risk.wallet_balance == 1200
    assert risk.available_balance == 900
    assert risk.total_unrealized_pnl == 5
    assert risk.positions[0].liquidation_price == 7.5


def test_normalize_futures_account_risk_prefers_account_total_equity_fields():
    risk = normalize_futures_account_risk(
        account_or_balance={
            "totalWalletBalance": "18",
            "totalUnrealizedProfit": "0.5",
            "availableBalance": "17.2",
        },
        balance_rows=[{"asset": "USDT", "balance": "9.7", "availableBalance": "9.6"}],
        position_rows=[],
    )

    assert risk.wallet_balance == 18
    assert risk.available_balance == 17.2
    assert risk.total_unrealized_pnl == 0.5
