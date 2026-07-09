from __future__ import annotations

from trade_assistant.broker import LIVE_CONFIRMATION
from trade_assistant.gui.services import (
    create_plan_from_form,
    detect_positions,
    evaluate_plan_from_form,
    format_float,
    live_trading_status,
    order_from_form,
    quick_backtest_for_signal,
    run_scan,
    save_trade_plan,
    signal_to_row,
    simulate_order_from_form,
)
from trade_assistant.models import Signal


def test_live_trading_status_locked_without_environment(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_ENABLE_LIVE_TRADING", raising=False)

    status = live_trading_status()

    assert status.enabled is False
    assert status.has_api_key is False
    assert status.has_api_secret is False
    assert status.env_switch_enabled is False
    assert "API Key" in status.reason


def test_live_trading_status_environment_ready_still_requires_confirmation(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_ENABLE_LIVE_TRADING", "true")

    status = live_trading_status()

    assert status.enabled is True
    assert "仍需订单确认" in status.reason


def test_format_float_handles_none_and_precision():
    assert format_float(None) == "-"
    assert format_float(3.123456789, 4) == "3.1235"


def test_signal_to_row_uses_chinese_labels():
    signal = Signal(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        score=9,
        last=3.25,
        change_24h=4.5,
        quote_volume_m=148.2,
        rsi_1h=62.3,
        rsi_4h=58.1,
        volume_ratio=1.42,
        momentum_24h=2.1,
        momentum_3d=5.4,
        funding_pct=0.0123,
        note="偏多观察",
    )

    row = signal_to_row(signal)

    assert row["市场"] == "合约"
    assert row["方向"] == "做多"
    assert row["交易对"] == "UNIUSDT"
    assert row["分数"] == "9"
    assert row["推荐理由"]
    assert row["风险点"]
    assert row["建议操作"]


def test_create_plan_from_form_calculates_quantity():
    plan = create_plan_from_form(
        symbol="UNIUSDT",
        market="futures",
        side="long",
        entry="3.25",
        stop="3.12",
        target="3.38",
        equity="1000",
        risk_pct="1",
        leverage="2",
    )

    assert plan.symbol == "UNIUSDT"
    assert round(plan.quantity, 6) == round(10 / 0.13, 6)


def test_evaluate_plan_from_form_returns_risk_review_and_plan():
    signal = Signal(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        score=9,
        last=10,
        change_24h=2,
        quote_volume_m=180,
        rsi_1h=61,
        rsi_4h=58,
        volume_ratio=1.5,
        momentum_24h=2,
        momentum_3d=5,
        funding_pct=0.01,
        note="偏多观察",
        atr_pct=2,
    )

    plan, review = evaluate_plan_from_form(
        symbol="UNIUSDT",
        market="futures",
        side="long",
        entry="10",
        stop="9.72",
        target="10.504",
        equity="1000",
        risk_pct="1",
        leverage="3",
        signal=signal,
        position=None,
        mode="intraday",
    )

    assert plan.symbol == "UNIUSDT"
    assert review.liquidation_status == "安全"
    assert review.management_rules[0] == "到 1R 后：止损移动到成本价"


def test_create_plan_from_form_rejects_empty_required_prices_with_chinese_message():
    try:
        create_plan_from_form(
            symbol="UNIUSDT",
            market="futures",
            side="long",
            entry="3.25",
            stop="",
            target="3.38",
            equity="1000",
            risk_pct="1",
            leverage="2",
        )
    except ValueError as exc:
        assert "请填写止损价" in str(exc)
    else:
        raise AssertionError("empty stop should fail with a Chinese validation message")


def test_create_plan_from_form_rejects_non_numeric_entry_with_chinese_message():
    try:
        create_plan_from_form(
            symbol="UNIUSDT",
            market="futures",
            side="long",
            entry="abc",
            stop="3.12",
            target="3.38",
            equity="1000",
            risk_pct="1",
            leverage="2",
        )
    except ValueError as exc:
        assert "入场价必须是数字" in str(exc)
    else:
        raise AssertionError("non-numeric entry should fail with a Chinese validation message")


def test_order_from_form_dry_run_without_live_confirmation():
    result = order_from_form(
        market="spot",
        symbol="UNIUSDT",
        side="BUY",
        quantity="1",
        order_type="MARKET",
        price="",
        allow_live=False,
        confirm="",
    )

    assert result["dry_run"] is True
    assert result["payload"]["symbol"] == "UNIUSDT"


def test_order_from_form_refuses_fake_live_when_environment_locked(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_ENABLE_LIVE_TRADING", raising=False)

    result = order_from_form(
        market="spot",
        symbol="UNIUSDT",
        side="BUY",
        quantity="1",
        order_type="MARKET",
        price="",
        allow_live=True,
        confirm=LIVE_CONFIRMATION,
    )

    assert result["dry_run"] is True


def test_order_from_form_live_path_accepts_injected_client(monkeypatch):
    monkeypatch.setenv("BINANCE_ENABLE_LIVE_TRADING", "true")

    calls = []

    def fake_place_order(client, market, payload, allow_live, confirm):
        calls.append((client, market, payload, allow_live, confirm))
        return {"orderId": 123, "payload": payload}

    result = order_from_form(
        market="spot",
        symbol="uniusdt",
        side="BUY",
        quantity="1",
        order_type="LIMIT",
        price="3.2",
        allow_live=True,
        confirm=LIVE_CONFIRMATION,
        client=object(),
        place_order_fn=fake_place_order,
    )

    assert result["orderId"] == 123
    assert result["payload"]["price"] == 3.2
    assert calls[0][1] == "spot"
    assert calls[0][3] is True


def test_order_from_form_limit_requires_price():
    try:
        order_from_form(
            market="spot",
            symbol="UNIUSDT",
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            price="",
            allow_live=False,
            confirm="",
        )
    except ValueError as exc:
        assert "LIMIT order requires price" in str(exc)
    else:
        raise AssertionError("LIMIT order without price should fail")


def test_simulate_order_from_form_writes_local_position(tmp_path):
    result, position = simulate_order_from_form(
        market="spot",
        symbol="UNIUSDT",
        side="BUY",
        quantity="2",
        order_type="MARKET",
        price="",
        fallback_price="3.5",
        portfolio_path=tmp_path / "sim.db",
    )

    assert result["dry_run"] is True
    assert position.side == "long"
    assert position.quantity == 2
    assert position.entry_price == 3.5


def test_run_scan_accepts_injected_dependencies(tmp_path):
    long_signal = Signal(
        market="spot",
        symbol="UNIUSDT",
        side="long",
        score=7,
        last=3.25,
        change_24h=1.5,
        quote_volume_m=100,
        rsi_1h=60,
        rsi_4h=58,
        volume_ratio=1.2,
        momentum_24h=1,
        momentum_3d=2,
        funding_pct=None,
        note="偏多观察",
    )
    short_signal = Signal(
        market="spot",
        symbol="WIFUSDT",
        side="short",
        score=6,
        last=1.25,
        change_24h=-1.5,
        quote_volume_m=90,
        rsi_1h=40,
        rsi_4h=42,
        volume_ratio=1.3,
        momentum_24h=-1,
        momentum_3d=-2,
        funding_pct=None,
        note="偏空观察",
    )

    def fake_scan(client, market, settings, top):
        assert client == "client"
        assert settings["quote_asset"] == "USDT"
        assert top == 5
        return [long_signal], [short_signal]

    def fake_report(longs, shorts, output_dir):
        assert longs[0].signal == long_signal
        assert longs[0].mode == "intraday"
        assert shorts[0].signal == short_signal
        assert output_dir == tmp_path
        return tmp_path / "latest.md", tmp_path / "latest.csv"

    result = run_scan(
        "spot",
        5,
        mode="intraday",
        settings_loader=lambda: {"quote_asset": "USDT"},
        client_factory=lambda: "client",
        scan_fn=fake_scan,
        report_writer=fake_report,
        output_dir=tmp_path,
    )

    assert result.longs[0].signal == long_signal
    assert result.shorts[0].signal == short_signal
    assert result.markdown_path == tmp_path / "latest.md"


def test_quick_backtest_for_signal_uses_client_klines():
    signal = Signal(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        score=7,
        last=10,
        change_24h=1,
        quote_volume_m=100,
        rsi_1h=60,
        rsi_4h=58,
        volume_ratio=1.2,
        momentum_24h=1,
        momentum_3d=2,
        funding_pct=None,
        note="偏多观察",
    )

    class FakeClient:
        def klines(self, market, symbol, interval, limit):
            return [[0, "0", "0", "0", str(10 + index * 0.1), "1"] for index in range(40)]

    result = quick_backtest_for_signal(signal, "intraday", stop_pct=2.0, reward_risk=1.8, client=FakeClient())

    assert result is not None
    assert result.trades > 0


def test_detect_positions_without_api_keys_returns_simulated_only(tmp_path, monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    simulated, real, advice = detect_positions(
        symbol="UNIUSDT",
        market="spot",
        signal=None,
        portfolio_path=tmp_path / "sim.db",
    )

    assert simulated.source == "simulated"
    assert simulated.side == "flat"
    assert real is None
    assert advice.action == "wait"


def test_save_trade_plan_sanitizes_filename(tmp_path):
    plan = create_plan_from_form(
        symbol="UNI/USDT",
        market="futures",
        side="long",
        entry="3.25",
        stop="3.12",
        target="3.38",
        equity="1000",
        risk_pct="1",
        leverage="2",
    )

    path = save_trade_plan(plan, output_dir=tmp_path)

    assert path.parent == tmp_path
    assert path.name == "plan_UNI_USDT_long.md"
