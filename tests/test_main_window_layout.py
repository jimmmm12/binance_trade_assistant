from __future__ import annotations

import sys
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QScrollArea

from trade_assistant.gui.services import ScanResult
from trade_assistant.gui.main_window import AutoTradeWorkerResult, MainWindow
from trade_assistant.models import PositionAdvice, Signal
from trade_assistant.portfolio import SimulatedPortfolio, flat_position
from trade_assistant.realtime_monitor import MonitorResult, MonitorTarget, evaluate_monitor_target
from trade_assistant.trading_system.runtime import TradingRuntime


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
    assert window.risk_input.text() == "0.28"
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
    assert window.positions_table.rowCount() == 0

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
    assert "杠杆" in headers
    assert "购买保证金(USDT)" in headers
    assert "当前保证金(USDT)" in headers
    assert "未实现盈亏" in headers
    assert any(button.text() == "仓位" for button in window.nav_buttons)
    assert window.sim_positions_button.isChecked() is True
    assert window.real_positions_button.text() == "真实仓"

    window.close()


def test_positions_page_displays_entry_and_current_totals_in_usdt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
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
        leverage=2,
        status="模拟持仓",
    )
    _app()
    window = MainWindow()

    window.refresh_positions_page()

    headers = [
        window.positions_table.horizontalHeaderItem(index).text()
        for index in range(window.positions_table.columnCount())
    ]
    leverage_column = headers.index("杠杆")
    entry_total_column = headers.index("购买保证金(USDT)")
    current_total_column = headers.index("当前保证金(USDT)")
    assert window.positions_table.item(0, leverage_column).text() == "2.0x"
    assert window.positions_table.item(0, entry_total_column).text() == "15.00"
    assert window.positions_table.item(0, current_total_column).text() == "16.00"

    window.close()


def test_selected_position_can_fill_close_order_form(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
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
    _app()
    window = MainWindow()

    window.refresh_positions_page()
    window.positions_table.selectRow(0)
    window.fill_close_order_from_selected_position()

    assert window.symbol_input.text() == "UNIUSDT"
    assert window.order_side_combo.currentData() == "SELL"
    assert window.order_type_combo.currentData() == "MARKET"
    assert window.quantity_input.text() == "10.00000000"
    assert window.price_input.text() == ""
    assert window.manual_reduce_only_order is True

    window.close()


def test_position_selection_survives_refresh_for_close_order_form(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
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
    _app()
    window = MainWindow()

    window.refresh_positions_page()
    window.positions_table.selectRow(0)
    portfolio.upsert_position_record(
        source="simulated",
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry_price=3.0,
        mark_price=3.3,
        stop_price=2.8,
        target_price=3.6,
        status="模拟持仓",
    )
    window.refresh_positions_page()
    window.fill_close_order_from_selected_position()

    assert window.positions_table.selectedItems()
    assert window.symbol_input.text() == "UNIUSDT"
    assert window.entry_input.text() == "3.30000000"
    assert window.pages.currentIndex() == 2

    window.close()


def test_recent_selected_position_can_fill_close_form_after_selection_clears(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
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
    _app()
    window = MainWindow()

    window.refresh_positions_page()
    window.positions_table.selectRow(0)
    window.positions_table.clearSelection()
    window.fill_close_order_from_selected_position()

    assert window.symbol_input.text() == "UNIUSDT"
    assert window.order_side_combo.currentData() == "SELL"
    assert window.manual_reduce_only_order is True

    window.close()


def test_detected_flat_position_hides_stale_position_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
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
    monkeypatch.setattr(
        "trade_assistant.gui.main_window.detect_positions",
        lambda **kwargs: (
            flat_position("simulated", "futures", "UNIUSDT", 3.2),
            None,
            PositionAdvice("wait", "空仓", []),
        ),
    )
    _app()
    window = MainWindow()
    window._set_combo_data(window.market_combo, "futures")
    window.symbol_input.setText("UNIUSDT")

    window.refresh_positions_page()
    assert window.positions_table.rowCount() == 1
    window.detect_current_position()

    assert "模拟仓：空仓" in window.position_status_box.toPlainText()
    assert window.positions_table.rowCount() == 0
    assert portfolio.position_records() == []

    window.close()


def test_detected_flat_real_position_clears_runtime_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    monkeypatch.setattr(
        "trade_assistant.gui.main_window.TradingRuntime",
        lambda: TradingRuntime(database_path=tmp_path / "runtime.db"),
    )
    monkeypatch.setattr(
        "trade_assistant.gui.main_window.detect_positions",
        lambda **kwargs: (
            flat_position("simulated", "futures", "UNIUSDT", 3.2),
            flat_position("real", "futures", "UNIUSDT", 3.2),
            PositionAdvice("wait", "空仓", []),
        ),
    )
    _app()
    window = MainWindow()
    window.trading_runtime.state_manager.upsert_position_snapshot(
        {
            "source": "real",
            "market": "futures",
            "symbol": "UNIUSDT",
            "side": "long",
            "quantity": 10,
            "entry_price": 3.0,
            "mark_price": 3.2,
            "notional": 32,
            "unrealized_pnl": 2,
            "realized_pnl": 0,
            "leverage": 1,
            "updated_at": "now",
        }
    )
    window._set_combo_data(window.market_combo, "futures")
    window.symbol_input.setText("UNIUSDT")

    assert window.trading_runtime.real_position_rows()
    window.detect_current_position()

    assert "真实API：空仓" in window.position_status_box.toPlainText()
    assert window.trading_runtime.real_position_rows() == []

    window.close()


def test_manual_simulated_close_hides_position_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    monkeypatch.setattr("trade_assistant.gui.services.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=10, price=3.0)
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
    _app()
    window = MainWindow()

    window.refresh_positions_page()
    window.positions_table.selectRow(0)
    window.fill_close_order_from_selected_position()
    window.submit_simulated_order()

    assert "空仓" in window.position_status_box.toPlainText()
    assert window.positions_table.rowCount() == 0
    assert portfolio.position_records() == []

    window.close()


def test_clear_all_simulated_positions_button_closes_local_sim_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=10, price=3.0)
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
    _app()
    window = MainWindow()

    window.refresh_positions_page()
    assert window.positions_table.rowCount() == 1
    window.clear_all_simulated_positions()

    assert window.position_source_mode == "simulated"
    assert window.positions_table.rowCount() == 0
    assert portfolio.position_records() == []
    assert portfolio.get_position("futures", "UNIUSDT").side == "flat"
    assert "不涉及真实 API" in window.position_status_box.toPlainText()

    window.close()


def test_clear_all_simulated_positions_button_clears_orphan_sim_position(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=10, price=3.0)
    _app()
    window = MainWindow()

    window.refresh_positions_page()
    assert window.positions_table.rowCount() == 0
    window.clear_all_simulated_positions()

    assert portfolio.open_position_count() == 0
    assert portfolio.get_position("futures", "UNIUSDT").side == "flat"
    assert "不涉及真实 API" in window.position_status_box.toPlainText()

    window.close()


def test_clear_all_simulated_positions_button_clears_dust_when_no_open_position(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.initialize()
    with portfolio._connect() as conn:
        conn.execute(
            """
            INSERT INTO sim_positions(market, symbol, side, quantity, entry_price, realized_pnl, leverage, updated_at)
            VALUES('futures', 'LABUSDT', 'short', ?, 1.1938, 0, 1, 'now')
            """,
            (0.000000000001,),
        )
    _app()
    window = MainWindow()

    window.refresh_positions_page()
    assert window.positions_table.rowCount() == 0
    window.clear_all_simulated_positions()

    assert portfolio.simulated_residue_count() == 0
    assert "不涉及真实 API" in window.position_status_box.toPlainText()

    window.close()


def test_simulated_position_mode_hides_real_api_local_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="real",
        market="futures",
        symbol="BNBUSDT",
        side="short",
        quantity=0.15,
        entry_price=574.14,
        mark_price=581.27,
        stop_price=580.85,
        target_price=561.57,
        status="真实持仓",
    )
    _app()
    window = MainWindow()

    window.set_position_source_mode("simulated")

    assert window.sim_positions_button.isChecked() is True
    assert window.positions_table.rowCount() == 0
    assert "模拟残留 0 条" in window.sim_db_path_label.text()

    window.close()


def test_simulated_monitor_targets_exclude_real_api_local_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.upsert_position_record(
        source="real",
        market="futures",
        symbol="BNBUSDT",
        side="short",
        quantity=0.15,
        entry_price=574.14,
        mark_price=581.27,
        stop_price=580.85,
        target_price=561.57,
        status="真实持仓",
    )
    _app()
    window = MainWindow()

    window.position_source_mode = "simulated"
    targets = window._monitor_targets()

    assert all(target.symbol != "BNBUSDT" for target in targets)

    window.close()


def test_manual_close_order_submits_reduce_only_without_trade_plan(monkeypatch) -> None:
    _app()
    window = MainWindow()
    captured = {}
    sync_calls = []
    monkeypatch.setattr(
        "trade_assistant.gui.main_window.live_trading_status",
        lambda: SimpleNamespace(
            enabled=True,
            reason="",
            has_api_key=True,
            has_api_secret=True,
            env_switch_enabled=True,
        ),
    )
    monkeypatch.setattr(
        "trade_assistant.gui.main_window.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    def fake_order_from_form(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"dry_run": False, "ok": True}

    monkeypatch.setattr("trade_assistant.gui.main_window.order_from_form", fake_order_from_form)
    window.trading_runtime.start_live_sync = lambda market, symbol=None: sync_calls.append((market, symbol)) or []
    window.current_plan = None
    window.current_risk_review = None
    window.manual_reduce_only_order = True
    window._set_combo_data(window.market_combo, "futures")
    window.symbol_input.setText("UNIUSDT")
    window._set_combo_data(window.order_side_combo, "SELL")
    window._set_combo_data(window.order_type_combo, "MARKET")
    window.quantity_input.setText("10")
    window.price_input.clear()
    window.live_confirm_input.setText("ABC")

    window.submit_order(True)

    assert captured["args"][:6] == ("futures", "UNIUSDT", "SELL", "10", "MARKET", "")
    assert captured["kwargs"]["allow_live"] is True
    assert captured["kwargs"]["reduce_only"] is True
    assert sync_calls[0] == ("futures", "UNIUSDT")
    assert window.manual_reduce_only_order is False

    window.close()


def test_position_mode_buttons_switch_position_source() -> None:
    _app()
    window = MainWindow()

    window.real_positions_button.click()

    assert window.position_source_mode == "real"
    assert window.real_positions_button.isChecked() is True
    assert window.sim_positions_button.isChecked() is False
    assert window.real_positions_button.objectName() == "PositionModeButton"
    assert window.sim_positions_button.objectName() == "PositionModeButton"

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
    assert window.auto_risk_line_combo.currentData() == "aggressive"
    assert window.auto_execution_combo.currentText() == "自动模拟下单"
    assert window.auto_live_confirm_input.placeholderText()
    assert window.auto_timer.isActive() is False
    assert "自动真仓" in window.auto_status_label.text()

    cycles: list[str] = []
    window.run_auto_trade_cycle = lambda: cycles.append("started")
    window.start_auto_trading()

    assert window.auto_trading_enabled is True
    assert window.auto_timer.isActive() is True
    assert cycles == ["started"]
    assert window.auto_start_button.isEnabled() is False
    assert window.auto_stop_button.isEnabled() is True
    assert "激进线" in window.auto_status_label.text()

    window.stop_auto_trading("测试急停")

    assert window.auto_trading_enabled is False
    assert window.auto_timer.isActive() is False
    assert window.auto_start_button.isEnabled() is True
    assert "测试急停" in window.auto_status_label.text()

    window.close()


def test_auto_trade_page_uses_split_scroll_layout_to_prevent_overlap() -> None:
    _app()
    window = MainWindow()

    scroll = window.findChild(QScrollArea, "AutoControlScroll")

    assert scroll is not None
    assert scroll.widgetResizable() is True
    assert scroll.horizontalScrollBarPolicy().name == "ScrollBarAlwaysOff"
    assert window.auto_log_table.wordWrap() is False
    assert window.auto_log_table.horizontalHeader().stretchLastSection() is True

    window.close()


def test_auto_state_label_compacts_recovered_long_path() -> None:
    _app()
    window = MainWindow()
    long_path = " -> ".join(
        [
            "空仓观察",
            *["发现机会", "生成计划", "等待确认/自动模拟"] * 10,
        ]
    )

    text = window._automation_state_text("恢复状态", long_path)

    assert text == "恢复状态：空仓观察 -> 发现机会 -> 生成计划 -> 等待确认/自动模拟"
    assert len(text) < 80

    window.close()


def test_auto_trade_error_log_compacts_traceback(monkeypatch) -> None:
    _app()
    window = MainWindow()
    monkeypatch.setattr("trade_assistant.gui.main_window.QMessageBox.warning", lambda *args: None)
    long_traceback = "\n".join(
        [
            "Traceback (most recent call last):",
            "  File \"trade_assistant/gui/workers.py\", line 27, in run",
            "RuntimeError: Binance 下单接口 HTTP 400：-1111: Precision is over the maximum defined for this asset.",
        ]
    )

    window.auto_trade_cycle_error(long_traceback)

    assert window.auto_log_table.rowCount() == 1
    assert "Traceback" not in window.auto_log_table.item(0, 4).text()
    assert "HTTP 400" in window.auto_log_table.item(0, 4).text()
    assert len(window.auto_log_table.item(0, 4).text()) < len(long_traceback)

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


def test_auto_trade_network_timeout_keeps_auto_cycle_enabled() -> None:
    _app()
    window = MainWindow()
    window.auto_trading_enabled = True
    window.auto_cycle_running = True
    window.auto_timer.start(60_000)
    window.auto_start_button.setEnabled(False)
    window.auto_stop_button.setEnabled(True)

    window.auto_trade_cycle_error("TimeoutError: The read operation timed out")

    assert window.auto_trading_enabled is True
    assert window.auto_cycle_running is False
    assert window.auto_timer.isActive() is True
    assert window.auto_start_button.isEnabled() is False
    assert "等待下一轮" in window.auto_status_label.text()
    assert "自动交易错误，已停止" not in window.auto_status_label.text()

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


def test_runtime_metrics_separates_closed_open_and_simulated_trades(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.apply_fill("futures", "UNIUSDT", "BUY", quantity=1, price=10)
    _app()
    window = MainWindow()
    metrics = SimpleNamespace(
        api_latency_ms=None,
        memory_mb=None,
        state_sync_healthy=True,
        cpu_percent=0.0,
        market_websocket="WebSocket ok",
        user_websocket="stopped",
        position_count=1,
        active_order_count=0,
        risk_status="normal",
        performance={"trades": 0, "open_trades": 2},
    )
    window.trading_runtime.metrics_snapshot = lambda _: metrics

    window.refresh_runtime_metrics()

    text = window.runtime_metrics_label.text()
    assert "闭环交易 0" in text
    assert "开仓记录 2" in text
    assert "模拟成交 1" in text

    window.close()


def test_positions_page_has_realtime_monitor_controls() -> None:
    _app()
    window = MainWindow()

    assert window.monitor_price_interval_spin.value() == 1
    assert window.monitor_position_interval_spin.value() == 1
    assert window.monitor_position_interval_spin.minimum() == 1
    assert window.monitor_timer.isActive() is False
    assert window.monitor_position_timer.isActive() is False
    assert window.live_reconcile_timer.interval() == 300_000
    assert window.live_reconcile_timer.isActive() is False

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


def test_realtime_monitor_keeps_waiting_row_without_rest_fallback() -> None:
    _app()
    window = MainWindow()
    target = MonitorTarget("futures", "UNIUSDT", "long", 10, 10, 9, 12)
    window.price_stream.latest_price = lambda *args, **kwargs: None

    results = window._run_realtime_monitor_worker([target])

    assert len(results) == 1
    assert results[0].severity == "waiting"
    assert "等待 WebSocket 行情" in results[0].alert_text

    window.close()


def test_realtime_monitor_waiting_result_stays_visible(tmp_path, monkeypatch) -> None:
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
    result = MonitorResult(
        MonitorTarget("futures", "UNIUSDT", "long", 10, 10, 9, 12),
        price=0,
        unrealized_pnl=0,
        r_multiple=0,
        alerts=["等待 WebSocket 行情，不使用 REST 轮询"],
        severity="waiting",
    )

    window.realtime_monitor_cycle_finished([result])

    assert window.monitor_table.rowCount() == 1
    assert window.monitor_table.item(0, 0).text() == "UNIUSDT"
    assert window.monitor_table.item(0, 2).text() == "等待"
    assert "等待 WebSocket 行情" in window.monitor_table.item(0, 5).text()
    assert "等待 WebSocket 行情" in window.monitor_status_label.text()
    assert portfolio.position_records()[0]["mark_price"] == 10

    window.close()


def test_real_position_mode_monitor_targets_exclude_simulated_positions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trade_assistant.gui.main_window.SimulatedPortfolio", lambda: SimulatedPortfolio(tmp_path / "sim.db"))
    monkeypatch.setattr(
        "trade_assistant.gui.main_window.TradingRuntime",
        lambda: TradingRuntime(database_path=tmp_path / "runtime.db"),
    )
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
    window.trading_runtime.state_manager.upsert_position_snapshot(
        {
            "source": "real",
            "market": "futures",
            "symbol": "BTCUSDT",
            "side": "short",
            "quantity": 0.01,
            "entry_price": 60000,
            "mark_price": 59900,
            "notional": 599,
            "unrealized_pnl": 1,
            "realized_pnl": 0,
            "leverage": 2,
            "updated_at": "now",
        }
    )
    window.position_source_mode = "real"

    targets = window._monitor_targets()

    assert [(target.symbol, target.side) for target in targets] == [("BTCUSDT", "short")]

    window.close()


def test_websocket_not_ready_keeps_monitor_waiting_without_rest_error() -> None:
    _app()
    window = MainWindow()
    window.monitor_cycle_running = True
    window.monitor_timer.start(60_000)
    window.monitor_position_timer.start(60_000)

    window.realtime_monitor_error(
        "RuntimeError: UNIUSDT WebSocket 行情未就绪或已超过 5 秒未更新；"
        "为避免 Binance 429，实时监控不会回退 REST 轮询。"
    )

    assert window.monitor_cycle_running is False
    assert window.monitor_timer.isActive() is True
    assert window.monitor_position_timer.isActive() is True
    assert "等待 WebSocket 行情" in window.monitor_status_label.text()
    assert "读取失败" not in window.monitor_status_label.text()

    window.close()


def test_auto_market_fresh_waits_for_websocket_price(monkeypatch) -> None:
    _app()
    window = MainWindow()
    calls = {"latest": 0}

    class FakePriceStream:
        def update_symbols(self, symbols):
            self.symbols = symbols

        def start(self):
            self.started = True

        def latest_price(self, *args, **kwargs):
            calls["latest"] += 1
            return 10.0 if calls["latest"] >= 3 else None

        def stop(self):
            self.stopped = True

    window.price_stream = FakePriceStream()
    monkeypatch.setattr("trade_assistant.gui.main_window.time.sleep", lambda _: None)

    ok, message = window._auto_market_fresh(SimpleNamespace(market="futures", symbol="UNIUSDT"))

    assert ok is True
    assert "新鲜" in message
    assert calls["latest"] == 3

    window.close()


def test_rate_limit_error_pauses_rest_polling_timers() -> None:
    _app()
    window = MainWindow()
    window.monitor_cycle_running = True
    window.realtime_monitor_checkbox.blockSignals(True)
    window.realtime_monitor_checkbox.setChecked(True)
    window.realtime_monitor_checkbox.blockSignals(False)
    window.monitor_timer.start(60_000)
    window.monitor_position_timer.start(60_000)
    window.live_reconcile_timer.start(60_000)
    window.auto_trading_enabled = True
    window.auto_timer.start(60_000)
    window.auto_start_button.setEnabled(False)
    window.auto_stop_button.setEnabled(True)

    window.realtime_monitor_error("Binance 公共行情接口触发限流 HTTP 429：Too many requests")

    assert window.monitor_cycle_running is False
    assert window.monitor_timer.isActive() is False
    assert window.monitor_position_timer.isActive() is False
    assert window.live_reconcile_timer.isActive() is False
    assert window.auto_timer.isActive() is False
    assert window.auto_trading_enabled is False
    assert window.realtime_monitor_checkbox.isChecked() is False
    assert "已暂停实时监控" in window.monitor_status_label.text()

    window.close()
