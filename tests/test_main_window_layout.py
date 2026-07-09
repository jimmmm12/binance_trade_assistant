from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QLineEdit, QScrollArea

from trade_assistant.gui.services import ScanResult
from trade_assistant.gui.main_window import AutoTradeWorkerResult, MainWindow
from trade_assistant.models import Signal
from trade_assistant.portfolio import SimulatedPortfolio
from trade_assistant.realtime_monitor import MonitorTarget, evaluate_monitor_target


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_action_panel_uses_resizable_scroll_area_to_prevent_overlap() -> None:
    _app()
    window = MainWindow()

    scroll = window.findChild(QScrollArea, "ActionPanelScroll")

    assert scroll is not None
    assert scroll.widgetResizable() is True
    assert scroll.horizontalScrollBarPolicy().name == "ScrollBarAlwaysOff"

    window.close()


def test_selecting_signal_auto_fills_atr_based_plan_prices() -> None:
    _app()
    window = MainWindow()
    signal = Signal(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        score=8,
        last=10.0,
        change_24h=2.0,
        quote_volume_m=180,
        rsi_1h=61,
        rsi_4h=58,
        volume_ratio=1.5,
        momentum_24h=2.4,
        momentum_3d=5.5,
        funding_pct=0.01,
        note="偏多观察",
        atr_1h_pct=2.0,
    )

    window.fill_signal_table(window.long_table, [signal])
    window.long_table.selectRow(0)

    assert window.symbol_input.text() == "UNIUSDT"
    assert window.entry_input.text() == "10.00000000"
    assert window.stop_input.text() == "9.72000000"
    assert window.target_input.text() == "10.50400000"
    assert window.risk_input.text() == "1.00"
    assert window.leverage_input.text() == "2.9"
    assert "ATR 2.00%" in window.log_box.toPlainText()

    window.close()


def test_generate_plan_auto_fills_order_form_without_live_confirmation() -> None:
    _app()
    window = MainWindow()
    window.symbol_input.setText("UNIUSDT")
    window._set_combo_data(window.market_combo, "futures")
    window._set_combo_data(window.side_combo, "long")
    window.entry_input.setText("10")
    window.stop_input.setText("9.5")
    window.target_input.setText("11")
    window.equity_input.setText("1000")
    window.risk_input.setText("1")
    window.leverage_input.setText("2")

    window.generate_plan()

    assert window.order_side_combo.currentData() == "BUY"
    assert window.order_type_combo.currentData() == "LIMIT"
    assert window.quantity_input.text() == f"{window.current_plan.quantity:.8f}"
    assert window.price_input.text() == "10.00000000"
    assert window.live_confirm_input.text() == ""
    assert window.positions_table.rowCount() >= 1

    window.close()


def test_positions_page_exists_with_required_columns() -> None:
    _app()
    window = MainWindow()

    headers = [
        window.positions_table.horizontalHeaderItem(index).text()
        for index in range(window.positions_table.columnCount())
    ]

    assert "止损价" in headers
    assert "止盈价" in headers
    assert "未实现盈亏" in headers
    assert any(button.text() == "仓位" for button in window.nav_buttons)
    assert window.sim_positions_button.isChecked() is True
    assert window.real_positions_button.text() == "真实仓"

    window.close()


def test_position_mode_buttons_switch_position_source() -> None:
    _app()
    window = MainWindow()

    window.real_positions_button.click()

    assert window.position_source_mode == "real"
    assert window.real_positions_button.isChecked() is True
    assert window.sim_positions_button.isChecked() is False

    window.sim_positions_button.click()

    assert window.position_source_mode == "simulated"
    assert window.sim_positions_button.isChecked() is True

    window.close()


def test_auto_trade_page_exposes_safe_cycle_controls() -> None:
    _app()
    window = MainWindow()

    nav_labels = [button.text() for button in window.nav_buttons]

    assert "自动" in nav_labels
    assert window.auto_interval_spin.value() == 5
    assert window.auto_simulate_checkbox.isChecked() is True
    assert window.auto_timer.isActive() is False
    assert "不会自动真下单" in window.auto_status_label.text()

    cycles: list[str] = []
    window.run_auto_trade_cycle = lambda: cycles.append("started")
    window.start_auto_trading()

    assert window.auto_trading_enabled is True
    assert window.auto_timer.isActive() is True
    assert cycles == ["started"]
    assert window.auto_start_button.isEnabled() is False
    assert window.auto_stop_button.isEnabled() is True

    window.stop_auto_trading("测试急停")

    assert window.auto_trading_enabled is False
    assert window.auto_timer.isActive() is False
    assert window.auto_start_button.isEnabled() is True
    assert "测试急停" in window.auto_status_label.text()

    window.close()


def test_manual_scan_result_reenables_scan_button(tmp_path) -> None:
    _app()
    window = MainWindow()
    window.scan_button.setEnabled(False)

    window.scan_finished(ScanResult([], [], tmp_path / "scan.md", tmp_path / "scan.csv"))

    assert window.scan_button.isEnabled() is True

    window.close()


def test_manual_scan_error_reenables_scan_button(monkeypatch) -> None:
    _app()
    window = MainWindow()
    monkeypatch.setattr("trade_assistant.gui.main_window.QMessageBox.warning", lambda *args: None)
    window.scan_button.setEnabled(False)

    window.scan_error("扫描失败")

    assert window.scan_button.isEnabled() is True
    assert "扫描失败" in window.log_box.toPlainText()

    window.close()


def test_auto_trade_market_data_error_waits_for_next_cycle() -> None:
    _app()
    window = MainWindow()
    window.auto_trading_enabled = True
    window.auto_cycle_running = True
    window.auto_timer.start(60_000)
    window.auto_start_button.setEnabled(False)
    window.auto_stop_button.setEnabled(True)

    window.auto_trade_cycle_finished(
        AutoTradeWorkerResult(market_error="Binance 公共行情接口返回 451：当前网络/IP 可能被限制。")
    )

    assert window.auto_trading_enabled is True
    assert window.auto_cycle_running is False
    assert window.auto_timer.isActive() is True
    assert window.auto_start_button.isEnabled() is False
    assert "等待下一轮" in window.auto_status_label.text()
    assert "Traceback" not in window.log_box.toPlainText()

    window.close()


def test_settings_page_can_apply_api_credentials_to_current_process(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_ENABLE_LIVE_TRADING", raising=False)
    _app()
    window = MainWindow()

    assert window.api_secret_input.echoMode() == QLineEdit.EchoMode.Password
    assert "API Key" in window.api_config_status_label.text()
    assert "Secret" in window.api_config_status_label.text()

    window.api_key_input.setText("key-123")
    window.api_secret_input.setText("secret-456")
    window.api_live_checkbox.setChecked(True)
    window.apply_api_credentials_to_process()

    assert window.api_secret_input.text() == ""
    assert window.key_status_label.text() == "API Key：已检测"
    assert "真下单环境已就绪" in window.api_config_status_label.text()

    window.close()


def test_settings_page_loads_saved_windows_api_credentials(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_ENABLE_LIVE_TRADING", raising=False)
    saved = {
        "BINANCE_API_KEY": "saved-key",
        "BINANCE_API_SECRET": "saved-secret",
        "BINANCE_ENABLE_LIVE_TRADING": "true",
    }
    monkeypatch.setattr(MainWindow, "_read_windows_user_env", staticmethod(lambda name: saved.get(name)))
    _app()

    window = MainWindow()

    assert window.key_status_label.text() == "API Key：已检测"
    assert "Secret 已配置" in window.api_config_status_label.text()
    assert window.api_secret_input.text() == ""
    assert "已配置" in window.api_secret_input.placeholderText()
    assert window.api_live_checkbox.isChecked() is True

    window.close()


def test_positions_page_has_realtime_monitor_controls() -> None:
    _app()
    window = MainWindow()

    assert window.monitor_price_interval_spin.value() == 1
    assert window.monitor_position_interval_spin.value() == 1
    assert window.monitor_position_interval_spin.minimum() == 1
    assert window.monitor_timer.isActive() is False
    assert window.monitor_position_timer.isActive() is False

    cycles: list[str] = []
    window.run_realtime_monitor_cycle = lambda: cycles.append("started")
    window.realtime_monitor_checkbox.setChecked(True)

    assert window.monitor_timer.isActive() is True
    assert window.monitor_position_timer.isActive() is True
    assert window.monitor_position_timer.interval() == 1000
    assert cycles == ["started"]
    assert "实时监控已启动" in window.monitor_status_label.text()

    window.realtime_monitor_checkbox.setChecked(False)

    assert window.monitor_timer.isActive() is False
    assert window.monitor_position_timer.isActive() is False
    assert "实时监控已停止" in window.monitor_status_label.text()

    window.close()


def test_realtime_monitor_results_update_monitor_table() -> None:
    _app()
    window = MainWindow()
    target = MonitorTarget("futures", "UNIUSDT", "long", 10, 10, 9, 12)
    result = evaluate_monitor_target(target, 11.5)
    window.monitor_cycle_running = True

    window.realtime_monitor_cycle_finished([result])

    assert window.monitor_cycle_running is False
    assert window.monitor_table.rowCount() == 1
    assert window.monitor_table.item(0, 0).text() == "UNIUSDT"
    assert window.monitor_table.item(0, 3).text() == "1.50R"
    assert "1.5R" in window.monitor_table.item(0, 5).text()
    assert "1 个目标" in window.monitor_status_label.text()

    window.close()


def test_realtime_monitor_updates_simulated_position_record_mark_price(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=10,
        mark_price=10,
        stop_price=9,
        target_price=12,
        status="模拟持仓",
    )
    _app()
    window = MainWindow()
    result = evaluate_monitor_target(MonitorTarget("futures", "UNIUSDT", "long", 10, 10, 9, 12), 11)

    window.realtime_monitor_cycle_finished([result])

    rows = portfolio.position_records()
    assert rows[0]["mark_price"] == 11
    assert rows[0]["unrealized_pnl"] == 10

    window.close()


def test_realtime_monitor_error_resets_running_flag() -> None:
    _app()
    window = MainWindow()
    window.monitor_cycle_running = True

    window.realtime_monitor_error("行情失败")

    assert window.monitor_cycle_running is False
    assert "行情失败" in window.monitor_status_label.text()

    window.close()
