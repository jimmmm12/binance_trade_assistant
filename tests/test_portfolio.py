from __future__ import annotations

from urllib.error import HTTPError

from trade_assistant.binance_client import BinanceClient
from trade_assistant.binance_client import MarketDataUnavailable
from trade_assistant.portfolio import SimulatedPortfolio
from trade_assistant.portfolio import normalize_futures_position, normalize_spot_position
from trade_assistant.portfolio import normalize_futures_account_risk


def test_simulated_portfolio_initializes_with_default_usdt(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()

    assert portfolio.cash_balance() == 10000


def test_simulated_buy_creates_long_position_and_reduces_cash(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()

    position = portfolio.apply_fill("spot", "UNIUSDT", "BUY", quantity=10, price=3.0)

    assert position.side == "long"
    assert position.quantity == 10
    assert position.entry_price == 3.0
    assert portfolio.cash_balance() == 9970


def test_simulated_sell_reduces_long_position_and_realizes_pnl(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("spot", "UNIUSDT", "BUY", quantity=10, price=3.0)

    position = portfolio.apply_fill("spot", "UNIUSDT", "SELL", quantity=4, price=3.5)

    assert position.side == "long"
    assert position.quantity == 6
    assert position.realized_pnl == 2.0
    assert portfolio.cash_balance() == 9984


def test_simulated_futures_sell_opens_short_position(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()

    position = portfolio.apply_fill("futures", "UNIUSDT", "SELL", quantity=10, price=3.0)

    assert position.side == "short"
    assert position.quantity == 10
    assert position.entry_price == 3.0
    assert portfolio.cash_balance() == 10000


def test_simulated_futures_buy_reduces_short_and_realizes_pnl(tmp_path):
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    portfolio.apply_fill("futures", "UNIUSDT", "SELL", quantity=10, price=3.0)

    position = portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=4, price=2.5)

    assert position.side == "short"
    assert position.quantity == 6
    assert position.realized_pnl == 2.0
    assert portfolio.cash_balance() == 10002


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
        realized_pnl=1.5,
        status="计划中",
    )

    rows = portfolio.position_records()

    assert rows[0]["symbol"] == "UNIUSDT"
    assert rows[0]["stop_price"] == 2.8
    assert rows[0]["target_price"] == 3.6
    assert rows[0]["unrealized_pnl"] == 2.0


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


def test_latest_price_uses_market_specific_ticker_endpoint(monkeypatch):
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
