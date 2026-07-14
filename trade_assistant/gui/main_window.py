from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QDateTime, Qt, QThreadPool, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QAbstractItemView,
    QVBoxLayout,
    QWidget,
)

from trade_assistant.auto_trader import (
    AUTO_EXECUTION_LIVE,
    AUTO_EXECUTION_PLAN,
    AUTO_EXECUTION_SIMULATE,
    AutoTradeConfig,
    AutoTradeDecision,
    PendingOrderDecision,
    run_auto_cycle,
)
from trade_assistant.automation_state import compact_state_path
from trade_assistant.binance_client import BinanceNetworkError, MarketDataUnavailable
from trade_assistant.broker import LIVE_CONFIRMATION
from trade_assistant.main import CONFIG_PATH, ROOT, load_settings
from trade_assistant.market_stream import BinanceWebSocketPriceCache
from trade_assistant.models import ScoredSignal, Signal, TradePlan
from trade_assistant.order_brackets import build_exit_order_drafts
from trade_assistant.portfolio import (
    is_flat_quantity,
    SimulatedPortfolio,
    read_futures_account_risk,
    read_real_futures_position,
    read_real_spot_position,
)
from trade_assistant.position_manager import ManagedPosition, PositionManagementDecision
from trade_assistant.realtime_monitor import MonitorResult, MonitorTarget, evaluate_monitor_target
from trade_assistant.report import trade_plan_to_markdown
from trade_assistant.risk_engine import PlanRiskReview, account_read_failed_guard, daily_loss_guard
from trade_assistant.trading_system.runtime import TradingRuntime
from trade_assistant.gui.services import (
    ScanResult,
    auto_plan_prices,
    create_plan_from_form,
    detect_positions,
    evaluate_plan_from_form,
    live_trading_status,
    order_from_form,
    quick_backtest_for_signal,
    run_scan,
    save_trade_plan,
    signal_to_row,
    simulate_order_from_form,
)
from trade_assistant.gui.workers import FunctionWorker


MARKET_CHOICES = [("全部", "both"), ("现货", "spot"), ("合约", "futures")]
TRADE_MARKET_CHOICES = [("现货", "spot"), ("合约", "futures")]
SIDE_CHOICES = [("做多", "long"), ("做空", "short")]
ORDER_SIDE_CHOICES = [("买入 / 开多 / 平空", "BUY"), ("卖出 / 开空 / 平多", "SELL")]
ORDER_TYPE_CHOICES = [("市价", "MARKET"), ("限价", "LIMIT")]
STRATEGY_MODE_CHOICES = [("日内短线", "intraday"), ("1-3天波段", "swing")]
AUTO_EXECUTION_CHOICES = [
    ("只生成计划", AUTO_EXECUTION_PLAN),
    ("自动模拟下单", AUTO_EXECUTION_SIMULATE),
    ("自动真仓下单", AUTO_EXECUTION_LIVE),
]


def _compact_worker_error(text: str, limit: int = 260) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "未知错误"
    for line in reversed(lines):
        if "Binance " in line or "HTTP " in line or "urlopen error" in line or "SSL:" in line:
            return line[-limit:]
    return lines[-1][-limit:]


SIGNAL_COLUMNS = [
    "市场",
    "交易对",
    "方向",
    "分数",
    "等级",
    "市场状态",
    "策略",
    "风险系数",
    "评分明细",
    "最新价",
    "24h涨跌",
    "成交额(百万)",
    "RSI 1h",
    "RSI 4h",
    "成交量倍数",
    "24h动量",
    "3日动量",
    "资金费率",
    "推荐理由",
    "风险点",
    "建议操作",
    "备注",
]

POSITION_COLUMNS = [
    "来源",
    "市场",
    "交易对",
    "方向",
    "数量",
    "入场价",
    "当前价",
    "杠杆",
    "购买保证金(USDT)",
    "当前保证金(USDT)",
    "止损价",
    "止盈价",
    "已实现盈亏",
    "未实现盈亏",
    "状态",
    "更新时间",
]


@dataclass(frozen=True)
class AutoTradeWorkerResult:
    decision: AutoTradeDecision | None = None
    scan_result: ScanResult | None = None
    market_error: str | None = None


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Binance Trade Assistant")
        self.resize(1320, 820)
        self._load_saved_api_credentials_into_environment()
        self.trading_runtime = TradingRuntime()
        self.thread_pool = QThreadPool.globalInstance()
        self.current_plan: TradePlan | None = None
        self.current_signal: ScoredSignal | None = None
        self.current_risk_review: PlanRiskReview | None = None
        self.manual_reduce_only_order = False
        self.selected_position_key: tuple[str, str, str, str] | None = None
        self.selected_position_values: dict[str, str] | None = None
        self.position_source_mode = "simulated"
        self.auto_trading_enabled = False
        self.auto_cycle_running = False
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self.run_auto_trade_cycle)
        self.monitor_cycle_running = False
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.run_realtime_monitor_cycle)
        self.monitor_position_timer = QTimer(self)
        self.monitor_position_timer.timeout.connect(self.refresh_positions_page)
        self.live_reconcile_running = False
        self.live_reconcile_timer = QTimer(self)
        self.live_reconcile_timer.setInterval(300_000)
        self.live_reconcile_timer.timeout.connect(self.run_live_reconciliation)
        self._auto_live_sync_at = 0.0
        self.last_monitor_alerts: dict[str, str] = {}
        self.last_websocket_wait_text = ""
        self.price_stream = BinanceWebSocketPriceCache(stale_after_seconds=20)
        self.runtime_metrics_timer = QTimer(self)
        self.runtime_metrics_timer.timeout.connect(self.refresh_runtime_metrics)
        self.latest_markdown_path: Path | None = None
        self.latest_csv_path: Path | None = None
        self._build_ui()
        self.refresh_status()
        self.load_settings_into_form()
        recovered_automation = self.trading_runtime.state_manager.snapshot().automation
        if recovered_automation.get("state_path"):
            self.auto_state_label.setText(
                self._automation_state_text("恢复状态", str(recovered_automation["state_path"]))
            )
            self.auto_status_label.setText(
                f"上次运行：{recovered_automation.get('message', '')}；自动交易需手动重新启动"
            )
        self.runtime_metrics_timer.start(1000)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_main_area(), 1)
        self.setCentralWidget(root)
        self._build_menu()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        open_project = QAction("打开项目目录", self)
        open_project.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT))))
        file_menu.addAction(open_project)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(172)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 18)
        brand = QLabel("交易助手")
        brand.setObjectName("Brand")
        layout.addWidget(brand)
        layout.addSpacing(12)
        self.nav_buttons: list[QPushButton] = []
        for index, text in enumerate(["扫描", "交易计划", "下单", "仓位", "自动", "设置"]):
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self.show_page(i))
            layout.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_buttons[0].setChecked(True)
        layout.addStretch(1)
        return sidebar

    def _build_main_area(self) -> QWidget:
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)
        layout.addWidget(self._build_top_bar())
        body = QHBoxLayout()
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_scan_page())
        self.pages.addWidget(self._build_plan_page())
        self.pages.addWidget(self._build_order_page())
        self.pages.addWidget(self._build_positions_page())
        self.pages.addWidget(self._build_auto_page())
        self.pages.addWidget(self._build_settings_page())
        body.addWidget(self.pages, 1)
        body.addWidget(self._build_action_panel())
        layout.addLayout(body, 1)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(135)
        layout.addWidget(self.log_box)
        return area

    def _build_top_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("TopBar")
        layout = QHBoxLayout(frame)
        self.public_status_label = QLabel("公共行情：待扫描")
        self.key_status_label = QLabel()
        self.live_status_label = QLabel()
        self.last_scan_label = QLabel("最近扫描：无")
        layout.addWidget(self.public_status_label)
        layout.addWidget(self.key_status_label)
        layout.addWidget(self.live_status_label)
        layout.addStretch(1)
        layout.addWidget(self.last_scan_label)
        return frame

    def _build_scan_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.scan_market_combo = QComboBox()
        self._add_combo_choices(self.scan_market_combo, MARKET_CHOICES)
        self.strategy_mode_combo = QComboBox()
        self._add_combo_choices(self.strategy_mode_combo, STRATEGY_MODE_CHOICES)
        self.scan_top_spin = QSpinBox()
        self.scan_top_spin.setRange(1, 200)
        self.scan_top_spin.setValue(30)
        self.scan_button = QPushButton("开始扫描")
        self.scan_button.setObjectName("PrimaryButton")
        self.scan_button.clicked.connect(self.start_scan)
        self.open_md_button = QPushButton("打开 Markdown 报告")
        self.open_md_button.clicked.connect(lambda: self.open_file(self.latest_markdown_path))
        self.open_csv_button = QPushButton("打开 CSV 报告")
        self.open_csv_button.clicked.connect(lambda: self.open_file(self.latest_csv_path))
        controls.addWidget(QLabel("市场"))
        controls.addWidget(self.scan_market_combo)
        controls.addWidget(QLabel("选币模式"))
        controls.addWidget(self.strategy_mode_combo)
        controls.addWidget(QLabel("Top"))
        controls.addWidget(self.scan_top_spin)
        controls.addWidget(self.scan_button)
        controls.addWidget(self.open_md_button)
        controls.addWidget(self.open_csv_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.long_table = self._make_signal_table()
        self.short_table = self._make_signal_table()
        layout.addWidget(QLabel("偏多观察"))
        layout.addWidget(self.long_table, 1)
        layout.addWidget(QLabel("偏空观察"))
        layout.addWidget(self.short_table, 1)
        return page

    def _build_plan_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.plan_preview = QTextEdit()
        self.plan_preview.setReadOnly(True)
        layout.addWidget(self.plan_preview)
        return page

    def _build_order_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        info = QLabel("下单入口在右侧操作面板。真下单必须满足 API Key、环境开关和确认文字。")
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch(1)
        return page

    def _build_positions_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.sim_positions_button = QPushButton("模拟仓")
        self.sim_positions_button.setCheckable(True)
        self.sim_positions_button.setChecked(True)
        self.sim_positions_button.setObjectName("PositionModeButton")
        self.sim_positions_button.clicked.connect(lambda: self.set_position_source_mode("simulated"))
        self.real_positions_button = QPushButton("真实仓")
        self.real_positions_button.setCheckable(True)
        self.real_positions_button.setObjectName("PositionModeButton")
        self.real_positions_button.clicked.connect(lambda: self.set_position_source_mode("real"))
        refresh_button = QPushButton("刷新仓位")
        refresh_button.setObjectName("PrimaryButton")
        refresh_button.clicked.connect(self.refresh_positions_page)
        close_order_button = QPushButton("选中平仓填单")
        close_order_button.clicked.connect(self.fill_close_order_from_selected_position)
        self.clear_sim_positions_button = QPushButton("模拟仓一键清仓")
        self.clear_sim_positions_button.setObjectName("DangerButton")
        self.clear_sim_positions_button.clicked.connect(self.clear_all_simulated_positions)
        controls.addWidget(self.sim_positions_button)
        controls.addWidget(self.real_positions_button)
        controls.addWidget(refresh_button)
        controls.addWidget(close_order_button)
        controls.addWidget(self.clear_sim_positions_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.sim_db_path_label = QLabel()
        self.sim_db_path_label.setWordWrap(True)
        layout.addWidget(self.sim_db_path_label)
        monitor_controls = QHBoxLayout()
        self.realtime_monitor_checkbox = QCheckBox("实时监控")
        self.realtime_monitor_checkbox.toggled.connect(self.toggle_realtime_monitor)
        self.monitor_price_interval_spin = QSpinBox()
        self.monitor_price_interval_spin.setRange(1, 60)
        self.monitor_price_interval_spin.setValue(1)
        self.monitor_price_interval_spin.setSuffix(" 秒")
        self.monitor_position_interval_spin = QSpinBox()
        self.monitor_position_interval_spin.setRange(1, 300)
        self.monitor_position_interval_spin.setValue(1)
        self.monitor_position_interval_spin.setSuffix(" 秒")
        self.monitor_status_label = QLabel("实时监控未启动")
        self.monitor_status_label.setWordWrap(True)
        monitor_controls.addWidget(self.realtime_monitor_checkbox)
        monitor_controls.addWidget(QLabel("价格"))
        monitor_controls.addWidget(self.monitor_price_interval_spin)
        monitor_controls.addWidget(QLabel("仓位"))
        monitor_controls.addWidget(self.monitor_position_interval_spin)
        monitor_controls.addWidget(self.monitor_status_label, 1)
        layout.addLayout(monitor_controls)
        self.monitor_table = QTableWidget(0, 6)
        self.monitor_table.setHorizontalHeaderLabels(["交易对", "方向", "最新价", "R倍数", "浮盈亏", "提醒"])
        self.monitor_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.monitor_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.monitor_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.monitor_table.setColumnWidth(0, 110)
        self.monitor_table.setColumnWidth(5, 420)
        layout.addWidget(self.monitor_table)
        self.positions_table = QTableWidget(0, len(POSITION_COLUMNS))
        self.positions_table.setHorizontalHeaderLabels(POSITION_COLUMNS)
        self.positions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.positions_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.positions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.positions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.positions_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.positions_table.itemSelectionChanged.connect(self.position_selected)
        for key, width in [
            ("交易对", 110),
            ("杠杆", 80),
            ("购买保证金(USDT)", 140),
            ("当前保证金(USDT)", 140),
            ("止损价", 110),
            ("止盈价", 110),
            ("未实现盈亏", 120),
            ("更新时间", 160),
        ]:
            self.positions_table.setColumnWidth(POSITION_COLUMNS.index(key), width)
        layout.addWidget(self.positions_table, 1)
        return page

    def _build_auto_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        controls_scroll = QScrollArea()
        controls_scroll.setObjectName("AutoControlScroll")
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setMinimumWidth(340)
        controls_scroll.setMaximumWidth(460)
        controls_content = QWidget()
        controls_layout = QVBoxLayout(controls_content)
        controls_layout.setContentsMargins(0, 0, 8, 0)
        controls_layout.setSpacing(12)

        controls_group = QGroupBox("自动交易控制台")
        controls = QFormLayout(controls_group)

        self.auto_market_combo = QComboBox()
        self._add_combo_choices(self.auto_market_combo, MARKET_CHOICES)
        self._set_combo_data(self.auto_market_combo, "futures")
        self.auto_mode_combo = QComboBox()
        self._add_combo_choices(self.auto_mode_combo, STRATEGY_MODE_CHOICES)
        self._set_combo_data(self.auto_mode_combo, "intraday")
        self.auto_risk_line_combo = QComboBox()
        self.auto_risk_line_combo.addItem("稳健线", "conservative")
        self.auto_risk_line_combo.addItem("激进线", "aggressive")
        self._set_combo_data(self.auto_risk_line_combo, "aggressive")
        self.auto_top_spin = QSpinBox()
        self.auto_top_spin.setRange(1, 100)
        self.auto_top_spin.setValue(30)
        self.auto_interval_spin = QSpinBox()
        self.auto_interval_spin.setRange(1, 1440)
        self.auto_interval_spin.setValue(5)
        self.auto_interval_spin.setSuffix(" 分钟")
        self.auto_equity_spin = QDoubleSpinBox()
        self.auto_equity_spin.setRange(10, 1_000_000_000)
        self.auto_equity_spin.setDecimals(2)
        self.auto_equity_spin.setValue(1000)
        self.auto_detect_account_checkbox = QCheckBox("自动识别本金和仓位")
        self.auto_detect_account_checkbox.setChecked(True)
        self.auto_execution_combo = QComboBox()
        self._add_combo_choices(self.auto_execution_combo, AUTO_EXECUTION_CHOICES)
        self._set_combo_data(self.auto_execution_combo, AUTO_EXECUTION_SIMULATE)
        self.auto_live_confirm_input = QLineEdit()
        self.auto_live_confirm_input.setPlaceholderText(f"自动真仓必须输入 {LIVE_CONFIRMATION}")
        self.auto_live_confirm_input.setEchoMode(QLineEdit.EchoMode.Password)

        controls.addRow("扫描市场", self.auto_market_combo)
        controls.addRow("策略模式", self.auto_mode_combo)
        controls.addRow("仓位策略", self.auto_risk_line_combo)
        controls.addRow("候选数量", self.auto_top_spin)
        controls.addRow("循环间隔", self.auto_interval_spin)
        controls.addRow("本轮本金", self.auto_equity_spin)
        controls.addRow("账户识别", self.auto_detect_account_checkbox)
        controls.addRow("执行方式", self.auto_execution_combo)
        controls.addRow("真仓确认", self.auto_live_confirm_input)
        controls_layout.addWidget(controls_group)

        self.auto_start_button = QPushButton("启动自动")
        self.auto_start_button.setObjectName("PrimaryButton")
        self.auto_start_button.clicked.connect(self.start_auto_trading)
        self.auto_stop_button = QPushButton("停止")
        self.auto_stop_button.clicked.connect(lambda: self.stop_auto_trading("自动交易已停止"))
        self.auto_stop_button.setEnabled(False)
        self.auto_emergency_button = QPushButton("急停")
        self.auto_emergency_button.setObjectName("DangerButton")
        self.auto_emergency_button.clicked.connect(self.trigger_emergency_stop)
        self.auto_clear_emergency_button = QPushButton("解除急停")
        self.auto_clear_emergency_button.clicked.connect(self.clear_emergency_stop)
        primary_buttons = QHBoxLayout()
        primary_buttons.addWidget(self.auto_start_button)
        primary_buttons.addWidget(self.auto_stop_button)
        safety_buttons = QHBoxLayout()
        safety_buttons.addWidget(self.auto_emergency_button)
        safety_buttons.addWidget(self.auto_clear_emergency_button)
        controls_layout.addLayout(primary_buttons)
        controls_layout.addLayout(safety_buttons)

        status_group = QGroupBox("运行状态")
        status_layout = QVBoxLayout(status_group)

        self.auto_status_label = QLabel("自动交易未启动：默认自动模拟；自动真仓必须单独选择并输入确认文字。")
        self.auto_status_label.setWordWrap(True)
        status_layout.addWidget(self.auto_status_label)
        self.auto_state_label = QLabel("状态机：空仓观察")
        self.auto_state_label.setWordWrap(True)
        status_layout.addWidget(self.auto_state_label)
        self.auto_account_label = QLabel("账户识别：等待自动启动")
        self.auto_account_label.setWordWrap(True)
        status_layout.addWidget(self.auto_account_label)
        self.runtime_metrics_label = QLabel("系统内核：等待状态同步")
        self.runtime_metrics_label.setWordWrap(True)
        status_layout.addWidget(self.runtime_metrics_label)
        controls_layout.addWidget(status_group)
        controls_layout.addStretch(1)
        controls_scroll.setWidget(controls_content)

        log_panel = QFrame()
        log_panel.setObjectName("Panel")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_title = QLabel("自动运行日志")
        log_title.setObjectName("SectionTitle")
        log_layout.addWidget(log_title)
        self.auto_log_table = QTableWidget(0, 5)
        self.auto_log_table.setHorizontalHeaderLabels(["时间", "状态", "交易对", "动作", "说明"])
        self.auto_log_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.auto_log_table.horizontalHeader().setStretchLastSection(True)
        self.auto_log_table.verticalHeader().setVisible(False)
        self.auto_log_table.setWordWrap(False)
        self.auto_log_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.auto_log_table.setColumnWidth(0, 150)
        self.auto_log_table.setColumnWidth(1, 110)
        self.auto_log_table.setColumnWidth(2, 120)
        self.auto_log_table.setColumnWidth(3, 120)
        self.auto_log_table.setColumnWidth(4, 520)
        self.auto_log_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.auto_log_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.auto_log_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        log_layout.addWidget(self.auto_log_table, 1)

        splitter.addWidget(controls_scroll)
        splitter.addWidget(log_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([390, 760])
        layout.addWidget(splitter, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        group = QGroupBox("本地配置")
        form = QFormLayout(group)
        self.quote_asset_input = QLineEdit()
        self.exclude_symbols_input = QLineEdit()
        self.min_quote_volume_spin = QDoubleSpinBox()
        self.min_quote_volume_spin.setRange(0, 1_000_000_000_000)
        self.min_quote_volume_spin.setDecimals(0)
        self.default_equity_spin = QDoubleSpinBox()
        self.default_equity_spin.setRange(0, 1_000_000_000)
        self.default_equity_spin.setDecimals(2)
        self.default_risk_spin = QDoubleSpinBox()
        self.default_risk_spin.setRange(0, 100)
        self.default_risk_spin.setDecimals(2)
        self.default_leverage_spin = QDoubleSpinBox()
        self.default_leverage_spin.setRange(0.1, 125)
        self.default_leverage_spin.setDecimals(2)
        self.daily_loss_stop_spin = QDoubleSpinBox()
        self.daily_loss_stop_spin.setRange(0.1, 100)
        self.daily_loss_stop_spin.setDecimals(2)
        self.intraday_atr_multiplier_spin = QDoubleSpinBox()
        self.intraday_atr_multiplier_spin.setRange(0.1, 10)
        self.intraday_atr_multiplier_spin.setDecimals(2)
        self.swing_atr_multiplier_spin = QDoubleSpinBox()
        self.swing_atr_multiplier_spin.setRange(0.1, 10)
        self.swing_atr_multiplier_spin.setDecimals(2)
        self.min_live_score_spin = QSpinBox()
        self.min_live_score_spin.setRange(0, 100)
        self.settings_scan_limit_spin = QSpinBox()
        self.settings_scan_limit_spin.setRange(1, 200)
        form.addRow("计价资产", self.quote_asset_input)
        form.addRow("排除交易对", self.exclude_symbols_input)
        form.addRow("最低成交额", self.min_quote_volume_spin)
        form.addRow("默认本金", self.default_equity_spin)
        form.addRow("默认风险%", self.default_risk_spin)
        form.addRow("默认杠杆", self.default_leverage_spin)
        form.addRow("日亏损锁定%", self.daily_loss_stop_spin)
        form.addRow("日内ATR倍数", self.intraday_atr_multiplier_spin)
        form.addRow("波段ATR倍数", self.swing_atr_multiplier_spin)
        form.addRow("真下单最低评分", self.min_live_score_spin)
        form.addRow("扫描数量", self.settings_scan_limit_spin)
        layout.addWidget(group)
        score_group = QGroupBox("信号评分引擎")
        score_form = QFormLayout(score_group)
        self.score_weight_spins: dict[str, QSpinBox] = {}
        for key, label in [
            ("trend", "趋势权重"),
            ("momentum", "动量权重"),
            ("volume", "量能权重"),
            ("position", "位置权重"),
            ("timeframe", "大周期权重"),
            ("regime", "市场环境权重"),
        ]:
            spin = QSpinBox()
            spin.setRange(1, 100)
            self.score_weight_spins[key] = spin
            score_form.addRow(label, spin)
        self.score_grade_a_spin = QSpinBox()
        self.score_grade_a_spin.setRange(1, 100)
        self.score_grade_b_spin = QSpinBox()
        self.score_grade_b_spin.setRange(1, 100)
        self.score_observe_spin = QSpinBox()
        self.score_observe_spin.setRange(1, 100)
        score_form.addRow("A级最低分", self.score_grade_a_spin)
        score_form.addRow("B级最低分", self.score_grade_b_spin)
        score_form.addRow("观察最低分", self.score_observe_spin)
        layout.addWidget(score_group)
        kernel_risk_group = QGroupBox("交易内核风控")
        kernel_risk_form = QFormLayout(kernel_risk_group)
        self.kernel_single_risk_spin = QDoubleSpinBox()
        self.kernel_single_risk_spin.setRange(0.1, 10)
        self.kernel_single_risk_spin.setDecimals(2)
        self.kernel_total_exposure_spin = QDoubleSpinBox()
        self.kernel_total_exposure_spin.setRange(0.1, 20)
        self.kernel_total_exposure_spin.setDecimals(1)
        self.kernel_symbol_exposure_spin = QDoubleSpinBox()
        self.kernel_symbol_exposure_spin.setRange(1, 1000)
        self.kernel_symbol_exposure_spin.setDecimals(1)
        self.kernel_max_leverage_spin = QDoubleSpinBox()
        self.kernel_max_leverage_spin.setRange(1, 125)
        self.kernel_max_leverage_spin.setDecimals(1)
        self.kernel_reduce_losses_spin = QSpinBox()
        self.kernel_reduce_losses_spin.setRange(1, 20)
        self.kernel_stop_losses_spin = QSpinBox()
        self.kernel_stop_losses_spin.setRange(2, 50)
        self.partial_fill_policy_combo = QComboBox()
        self.partial_fill_policy_combo.addItem("等待剩余成交", "wait")
        self.partial_fill_policy_combo.addItem("超时撤销剩余", "cancel")
        self.partial_fill_timeout_spin = QSpinBox()
        self.partial_fill_timeout_spin.setRange(5, 3600)
        self.partial_fill_timeout_spin.setSuffix(" 秒")
        self.auto_protective_orders_checkbox = QCheckBox("成交后提交止损和分批止盈单")
        kernel_risk_form.addRow("单笔最大风险%", self.kernel_single_risk_spin)
        kernel_risk_form.addRow("总敞口/权益倍数", self.kernel_total_exposure_spin)
        kernel_risk_form.addRow("单币最大敞口%", self.kernel_symbol_exposure_spin)
        kernel_risk_form.addRow("系统最高杠杆", self.kernel_max_leverage_spin)
        kernel_risk_form.addRow("连续亏损降仓", self.kernel_reduce_losses_spin)
        kernel_risk_form.addRow("连续亏损停仓", self.kernel_stop_losses_spin)
        kernel_risk_form.addRow("部分成交处理", self.partial_fill_policy_combo)
        kernel_risk_form.addRow("部分成交超时", self.partial_fill_timeout_spin)
        kernel_risk_form.addRow("真实仓保护单", self.auto_protective_orders_checkbox)
        layout.addWidget(kernel_risk_group)
        save_button = QPushButton("保存设置")
        save_button.setObjectName("PrimaryButton")
        save_button.clicked.connect(self.save_settings_from_form)
        layout.addWidget(save_button)
        api_group = QGroupBox("Binance API")
        api_layout = QFormLayout(api_group)
        self.api_config_status_label = QLabel()
        self.api_config_status_label.setWordWrap(True)
        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("留空则保留当前 API Key")
        self.api_secret_input = QLineEdit()
        self.api_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_secret_input.setPlaceholderText("留空则保留当前 API Secret")
        self.api_live_checkbox = QCheckBox("允许真下单环境开关")
        api_layout.addRow("状态", self.api_config_status_label)
        api_layout.addRow("API Key", self.api_key_input)
        api_layout.addRow("API Secret", self.api_secret_input)
        api_layout.addRow("真下单", self.api_live_checkbox)
        api_buttons = QHBoxLayout()
        self.apply_api_button = QPushButton("应用到本次运行")
        self.apply_api_button.setObjectName("PrimaryButton")
        self.apply_api_button.clicked.connect(self.apply_api_credentials_to_process)
        self.save_api_env_button = QPushButton("保存到 Windows")
        self.save_api_env_button.clicked.connect(self.save_api_credentials_to_windows)
        api_buttons.addWidget(self.apply_api_button)
        api_buttons.addWidget(self.save_api_env_button)
        api_buttons.addStretch(1)
        api_layout.addRow(api_buttons)
        layout.addWidget(api_group)
        layout.addStretch(1)
        page.setWidget(content)
        return page

    def _build_action_panel(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("ActionPanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(350)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setMinimumWidth(330)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        plan_group = QGroupBox("快速交易计划")
        plan_form = QFormLayout(plan_group)
        self.symbol_input = QLineEdit()
        self.market_combo = QComboBox()
        self._add_combo_choices(self.market_combo, TRADE_MARKET_CHOICES)
        self.side_combo = QComboBox()
        self._add_combo_choices(self.side_combo, SIDE_CHOICES)
        self.entry_input = QLineEdit()
        self.stop_input = QLineEdit()
        self.target_input = QLineEdit()
        self.equity_input = QLineEdit("1000")
        self.risk_input = QLineEdit("1")
        self.leverage_input = QLineEdit("2")
        for label, widget in [
            ("交易对", self.symbol_input),
            ("市场", self.market_combo),
            ("方向", self.side_combo),
            ("入场", self.entry_input),
            ("止损", self.stop_input),
            ("目标", self.target_input),
            ("本金", self.equity_input),
            ("风险%", self.risk_input),
            ("杠杆", self.leverage_input),
        ]:
            plan_form.addRow(label, widget)
        layout.addWidget(plan_group)
        plan_button = QPushButton("生成计划")
        plan_button.setObjectName("PrimaryButton")
        plan_button.clicked.connect(self.generate_plan)
        layout.addWidget(plan_button)
        order_group = QGroupBox("下单")
        order_form = QFormLayout(order_group)
        self.order_side_combo = QComboBox()
        self._add_combo_choices(self.order_side_combo, ORDER_SIDE_CHOICES)
        self.order_type_combo = QComboBox()
        self._add_combo_choices(self.order_type_combo, ORDER_TYPE_CHOICES)
        self.quantity_input = QLineEdit()
        self.price_input = QLineEdit()
        self.live_confirm_input = QLineEdit()
        self.live_confirm_input.setPlaceholderText(LIVE_CONFIRMATION)
        order_form.addRow("买卖", self.order_side_combo)
        order_form.addRow("类型", self.order_type_combo)
        order_form.addRow("数量", self.quantity_input)
        order_form.addRow("限价", self.price_input)
        order_form.addRow("确认文字", self.live_confirm_input)
        layout.addWidget(order_group)
        dry_button = QPushButton("模拟下单")
        dry_button.clicked.connect(self.submit_simulated_order)
        live_button = QPushButton("真下单")
        live_button.setObjectName("DangerButton")
        live_button.clicked.connect(lambda: self.submit_order(True))
        detect_button = QPushButton("检测仓位")
        detect_button.setObjectName("PrimaryButton")
        detect_button.clicked.connect(self.detect_current_position)
        self.position_status_box = QTextEdit()
        self.position_status_box.setReadOnly(True)
        self.position_status_box.setFixedHeight(150)
        layout.addWidget(dry_button)
        layout.addWidget(live_button)
        layout.addWidget(detect_button)
        layout.addWidget(self.position_status_box)
        scroll.setWidget(panel)
        return scroll

    def _make_signal_table(self) -> QTableWidget:
        table = QTableWidget(0, len(SIGNAL_COLUMNS))
        table.setHorizontalHeaderLabels(SIGNAL_COLUMNS)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(False)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setColumnWidth(SIGNAL_COLUMNS.index("备注"), 320)
        table.setColumnWidth(SIGNAL_COLUMNS.index("推荐理由"), 300)
        table.setColumnWidth(SIGNAL_COLUMNS.index("风险点"), 260)
        table.setColumnWidth(SIGNAL_COLUMNS.index("建议操作"), 150)
        table.setColumnWidth(SIGNAL_COLUMNS.index("评分明细"), 360)
        table.setColumnWidth(SIGNAL_COLUMNS.index("等级"), 70)
        table.setColumnWidth(SIGNAL_COLUMNS.index("市场状态"), 130)
        table.setColumnWidth(SIGNAL_COLUMNS.index("策略"), 140)
        table.setColumnWidth(SIGNAL_COLUMNS.index("风险系数"), 90)
        table.setColumnWidth(SIGNAL_COLUMNS.index("交易对"), 110)
        table.setColumnWidth(SIGNAL_COLUMNS.index("最新价"), 110)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.itemSelectionChanged.connect(lambda t=table: self.signal_selected(t))
        return table

    def _add_combo_choices(self, combo: QComboBox, choices: list[tuple[str, str]]) -> None:
        for label, value in choices:
            combo.addItem(label, value)

    def _set_combo_data(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _api_status_text(self, status=None) -> str:
        status = status or live_trading_status()
        key_text = "API Key 已配置" if status.has_api_key else "API Key 未配置"
        secret_text = "Secret 已配置" if status.has_api_secret else "Secret 未配置"
        live_text = "真下单开关已打开" if status.env_switch_enabled else "真下单开关关闭"
        return f"{key_text}，{secret_text}，{live_text}。{status.reason}"

    def _load_api_credentials_into_form(self) -> None:
        self.api_key_input.clear()
        self.api_secret_input.clear()
        self.api_live_checkbox.setChecked(os.getenv("BINANCE_ENABLE_LIVE_TRADING", "").lower() == "true")
        self.api_key_input.setPlaceholderText(
            "已配置，留空则不修改" if os.getenv("BINANCE_API_KEY") else "粘贴 Binance API Key"
        )
        self.api_secret_input.setPlaceholderText(
            "已配置，留空则不修改" if os.getenv("BINANCE_API_SECRET") else "粘贴 Binance API Secret"
        )
        self.api_config_status_label.setText(self._api_status_text())

    def apply_api_credentials_to_process(self) -> None:
        api_key = self.api_key_input.text().strip()
        api_secret = self.api_secret_input.text().strip()
        if api_key:
            os.environ["BINANCE_API_KEY"] = api_key
        if api_secret:
            os.environ["BINANCE_API_SECRET"] = api_secret
        os.environ["BINANCE_ENABLE_LIVE_TRADING"] = "true" if self.api_live_checkbox.isChecked() else "false"
        self.api_secret_input.clear()
        self.trading_runtime.update_credentials(
            os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")
        )
        self._load_api_credentials_into_form()
        self.refresh_status()
        self.append_log("Binance API 已应用到本次运行。")

    def save_api_credentials_to_windows(self) -> None:
        api_key = self.api_key_input.text().strip() or os.getenv("BINANCE_API_KEY", "")
        api_secret = self.api_secret_input.text().strip() or os.getenv("BINANCE_API_SECRET", "")
        live_value = "true" if self.api_live_checkbox.isChecked() else "false"
        if not api_key or not api_secret:
            QMessageBox.warning(self, "API 未完整", "请填写 API Key 和 API Secret 后再保存到 Windows。")
            return
        try:
            self._set_windows_user_env("BINANCE_API_KEY", api_key)
            self._set_windows_user_env("BINANCE_API_SECRET", api_secret)
            self._set_windows_user_env("BINANCE_ENABLE_LIVE_TRADING", live_value)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            self.append_log(f"保存 API 到 Windows 失败：{exc}")
            return
        self.apply_api_credentials_to_process()
        QMessageBox.information(self, "保存成功", "已保存到 Windows 用户环境变量，重新打开软件后仍会生效。")

    def _set_windows_user_env(self, name: str, value: str) -> None:
        if os.name != "nt":
            raise RuntimeError("保存到 Windows 环境变量只支持 Windows。")
        completed = subprocess.run(
            ["setx", name, value],
            capture_output=True,
            text=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or f"setx {name} failed"
            raise RuntimeError(message)

    def _load_saved_api_credentials_into_environment(self) -> None:
        for name in ["BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_ENABLE_LIVE_TRADING"]:
            if os.getenv(name):
                continue
            value = self._read_windows_user_env(name)
            if value:
                os.environ[name] = value

    @staticmethod
    def _read_windows_user_env(name: str) -> str | None:
        if os.name != "nt":
            return None
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return str(value) if value else None

    def show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)

    def refresh_status(self) -> None:
        status = live_trading_status()
        key_ready = status.has_api_key and status.has_api_secret
        self.key_status_label.setText("API Key：已检测" if key_ready else "API Key：未完整配置")
        self.live_status_label.setText(status.reason)
        self.live_status_label.setObjectName("StatusGood" if status.enabled else "StatusBad")
        self.live_status_label.style().unpolish(self.live_status_label)
        self.live_status_label.style().polish(self.live_status_label)
        if hasattr(self, "api_config_status_label"):
            self.api_config_status_label.setText(self._api_status_text(status))

    def append_log(self, text: str) -> None:
        self.log_box.append(text)

    def start_scan(self) -> None:
        self.scan_button.setEnabled(False)
        self.append_log("开始扫描 Binance 市场...")
        worker = FunctionWorker(
            run_scan,
            self.scan_market_combo.currentData(),
            self.scan_top_spin.value(),
            self.strategy_mode_combo.currentData(),
        )
        worker.signals.result.connect(self.scan_finished)
        worker.signals.error.connect(self.scan_error)
        worker.signals.finished.connect(lambda: self.scan_button.setEnabled(True))
        self.thread_pool.start(worker)

    def scan_finished(self, result: ScanResult) -> None:
        self.scan_button.setEnabled(True)
        self.public_status_label.setText("公共行情：正常")
        self.latest_markdown_path = result.markdown_path
        self.latest_csv_path = result.csv_path
        self.fill_signal_table(self.long_table, result.longs)
        self.fill_signal_table(self.short_table, result.shorts)
        self.last_scan_label.setText("最近扫描：已完成")
        self.append_log(f"扫描完成：{result.markdown_path} / {result.csv_path}")

    def scan_error(self, text: str) -> None:
        self.scan_button.setEnabled(True)
        self.worker_error(text)

    def start_auto_trading(self) -> None:
        if self.auto_trading_enabled:
            return
        if self._auto_execution_mode() == AUTO_EXECUTION_LIVE:
            if self.auto_live_confirm_input.text().strip() != LIVE_CONFIRMATION:
                QMessageBox.warning(self, "自动真仓锁定", f"自动真仓必须输入 {LIVE_CONFIRMATION}")
                self.append_log("自动真仓锁定：确认文字不匹配。")
                return
            status = live_trading_status()
            if not status.enabled:
                QMessageBox.warning(self, "自动真仓锁定", status.reason)
                self.append_log(f"自动真仓锁定：{status.reason}")
                return
        self.auto_trading_enabled = True
        self.auto_start_button.setEnabled(False)
        self.auto_stop_button.setEnabled(True)
        interval_ms = self.auto_interval_spin.value() * 60 * 1000
        self.auto_timer.start(interval_ms)
        mode_label = self.auto_execution_combo.currentText()
        risk_label = self.auto_risk_line_combo.currentText()
        self.auto_status_label.setText(
            f"自动交易已启动：每 {self.auto_interval_spin.value()} 分钟运行一轮，{mode_label}，{risk_label}。"
        )
        self.append_log(f"自动交易已启动：{mode_label}，{risk_label}。")
        self.run_auto_trade_cycle()

    def stop_auto_trading(self, reason: str = "自动交易已停止") -> None:
        self.auto_trading_enabled = False
        self.auto_timer.stop()
        self.auto_start_button.setEnabled(True)
        self.auto_stop_button.setEnabled(False)
        self.auto_status_label.setText(reason)
        self.append_log(reason)

    def trigger_emergency_stop(self) -> None:
        reason = "用户触发急停：禁止所有新开仓，保留减仓和平仓权限"
        self.trading_runtime.risk_manager.emergency_stop(reason)
        self.stop_auto_trading("急停已触发：自动循环停止，新开仓已锁定")

    def clear_emergency_stop(self) -> None:
        answer = QMessageBox.question(self, "解除急停", "确认解除新开仓锁定？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.trading_runtime.risk_manager.clear_emergency_stop()
        self.auto_status_label.setText("急停已解除，自动交易仍需手动重新启动")
        self.append_log("急停已解除。")

    def refresh_runtime_metrics(self) -> None:
        if not hasattr(self, "runtime_metrics_label"):
            return
        metrics = self.trading_runtime.metrics_snapshot(self.price_stream.freshness_text())
        latency = "-" if metrics.api_latency_ms is None else f"{metrics.api_latency_ms:.0f}ms"
        memory = "-" if metrics.memory_mb is None else f"{metrics.memory_mb:.0f}MB"
        performance = metrics.performance
        simulated_fills = SimulatedPortfolio().fill_count()
        self.runtime_metrics_label.setText(
            f"系统内核：状态同步 {'正常' if metrics.state_sync_healthy else '异常'}｜"
            f"CPU {metrics.cpu_percent:.1f}%｜内存 {memory}｜"
            f"行情 {metrics.market_websocket}｜账户流 {metrics.user_websocket}｜"
            f"API {latency}｜持仓 {metrics.position_count}｜活动订单 {metrics.active_order_count}｜"
            f"风控 {metrics.risk_status}｜闭环交易 {performance.get('trades', 0)}｜"
            f"开仓记录 {performance.get('open_trades', 0)}｜模拟成交 {simulated_fills}"
        )

    def run_auto_trade_cycle(self) -> None:
        if not self.auto_trading_enabled:
            return
        if self.auto_cycle_running:
            self._append_auto_log("跳过", "", "等待", "上一轮还没结束，本轮跳过。")
            return
        self.auto_cycle_running = True
        self.auto_status_label.setText("自动交易运行中：正在扫描并生成计划。")
        settings = load_settings()
        execution_mode = self._auto_execution_mode()
        config = AutoTradeConfig(
            market=self.auto_market_combo.currentData(),
            mode=self.auto_mode_combo.currentData(),
            top=self.auto_top_spin.value(),
            auto_simulate=execution_mode == AUTO_EXECUTION_SIMULATE,
            equity=self.auto_equity_spin.value(),
            max_daily_loss_pct=float(settings.get("daily_loss_stop_pct", 2.0)),
            execution_mode=execution_mode,
            live_confirm=self.auto_live_confirm_input.text().strip(),
            auto_detect_account=self.auto_detect_account_checkbox.isChecked(),
            risk_line=self.auto_risk_line_combo.currentData(),
        )
        worker = FunctionWorker(self._run_auto_trade_worker, config)
        worker.signals.result.connect(self.auto_trade_cycle_finished)
        worker.signals.error.connect(self.auto_trade_cycle_error)
        worker.signals.finished.connect(lambda: setattr(self, "auto_cycle_running", False))
        self.thread_pool.start(worker)

    def _run_auto_trade_worker(self, config: AutoTradeConfig) -> AutoTradeWorkerResult:
        scan_result: ScanResult | None = None

        def scan_for_auto() -> tuple[list[ScoredSignal], list[ScoredSignal]]:
            nonlocal scan_result
            scan_result = run_scan(config.market, config.top, config.mode)
            return scan_result.longs, scan_result.shorts

        try:
            decision = run_auto_cycle(
                config,
                scan_fn=scan_for_auto,
                market_fresh_fn=self._auto_market_fresh,
                live_status_fn=self._auto_live_status,
                live_order_fn=lambda plan, side, review, signal: self._auto_live_order(
                    plan, side, review, signal, config.live_confirm, config.risk_line
                ),
                position_order_fn=lambda managed, decision: self._auto_position_order(
                    managed, decision, config.live_confirm
                ),
                managed_positions_fn=lambda: self._auto_managed_positions(config.execution_mode or AUTO_EXECUTION_PLAN),
                account_equity_fn=lambda: self._auto_account_equity(config.execution_mode or AUTO_EXECUTION_PLAN),
                real_position_fn=self._auto_real_position,
                pending_orders_fn=self._auto_pending_orders,
                entry_gate_fn=(
                    (lambda signal: self.trading_runtime.automatic_entry_allowed(signal.symbol))
                    if config.execution_mode == AUTO_EXECUTION_LIVE
                    else None
                ),
            )
        except (MarketDataUnavailable, BinanceNetworkError) as exc:
            return AutoTradeWorkerResult(market_error=str(exc))
        if scan_result is None:
            raise RuntimeError("自动扫描没有返回结果")
        return AutoTradeWorkerResult(decision=decision, scan_result=scan_result)

    def auto_trade_cycle_finished(self, payload: AutoTradeWorkerResult) -> None:
        self.auto_cycle_running = False
        if payload.market_error:
            message = f"{payload.market_error}；自动循环保持开启，等待下一轮。"
            self.public_status_label.setText("公共行情：不可用")
            self.last_scan_label.setText("最近扫描：行情失败")
            self._append_auto_log("行情失败", "", "等待下一轮", payload.market_error)
            self.auto_status_label.setText(message)
            self.append_log(f"自动交易行情失败：{message}")
            return
        if payload.decision is None or payload.scan_result is None:
            raise RuntimeError("自动交易后台任务没有返回有效结果")
        decision = payload.decision
        scan_result = payload.scan_result
        self.trading_runtime.record_automation_decision(decision)
        self.public_status_label.setText("公共行情：正常")
        self.latest_markdown_path = scan_result.markdown_path
        self.latest_csv_path = scan_result.csv_path
        self.fill_signal_table(self.long_table, scan_result.longs)
        self.fill_signal_table(self.short_table, scan_result.shorts)
        self.last_scan_label.setText("最近扫描：自动完成")

        symbol = decision.signal.symbol if decision.signal is not None else ""
        self._append_auto_log(decision.action, symbol, "自动循环", decision.message)
        self.auto_state_label.setText(self._automation_state_text("状态机", decision.state_path))
        self.auto_status_label.setText(decision.message)
        equity = decision.plan.equity if decision.plan is not None else self.auto_equity_spin.value()
        self.auto_account_label.setText(
            f"账户识别：{'自动' if self.auto_detect_account_checkbox.isChecked() else '手动'}，本轮本金 {equity:.2f}"
        )
        self.append_log(f"自动交易：{decision.message}")
        if decision.plan is not None:
            self.current_plan = decision.plan
            self.current_signal = decision.signal
            self.current_risk_review = decision.review
            self._apply_plan_to_forms(decision.plan)
            save_trade_plan(decision.plan)
            preview = trade_plan_to_markdown(decision.plan)
            if decision.review is not None:
                preview += "\n\n" + self._risk_review_text(decision.review)
            self.plan_preview.setPlainText(preview)
        if decision.position is not None and decision.plan is not None:
            is_live_order = decision.action in {"live_order_sent", "live_order_pending"}
            title = "自动真仓订单：" if is_live_order else "自动模拟下单："
            self.position_status_box.setPlainText(title + "\n" + self._position_text(decision.position))
            if is_live_order:
                self._save_detected_position_record(decision.position)
            else:
                self._save_plan_position_record(decision.plan, "自动模拟持仓")
        if decision.action in {"live_order_sent", "live_order_pending"}:
            self.set_position_source_mode("real")
            self.live_reconcile_timer.start()
            if not self.realtime_monitor_checkbox.isChecked():
                self.realtime_monitor_checkbox.setChecked(True)
            self.append_log("自动真仓订单已交给 Order Manager：已切换真实仓并启动实时监控。")
        if decision.plan is not None or decision.position is not None:
            self.refresh_positions_page()

    def auto_trade_cycle_error(self, text: str) -> None:
        self.auto_cycle_running = False
        if self._is_transient_network_text(text):
            message = f"{_compact_worker_error(text)}；自动循环保持开启，等待下一轮重试。"
            self._append_auto_log("网络超时", "", "等待下一轮", message)
            self.auto_status_label.setText(message)
            self.append_log(f"自动交易网络暂不可用：{message}")
            return
        self._append_auto_log("错误", "", "自动循环", _compact_worker_error(text))
        self.auto_status_label.setText("自动交易错误，已停止。")
        self.stop_auto_trading("自动交易错误，已停止。")
        self.worker_error(text)

    def _append_auto_log(self, status: str, symbol: str, action: str, message: str) -> None:
        row = self.auto_log_table.rowCount()
        self.auto_log_table.insertRow(row)
        values = [
            QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"),
            status,
            symbol,
            action,
            message,
        ]
        for column, value in enumerate(values):
            display_value = _compact_worker_error(value, 180) if column == 4 else value
            item = QTableWidgetItem(display_value)
            item.setToolTip(value)
            self.auto_log_table.setItem(row, column, item)
        self.auto_log_table.setRowHeight(row, 30)
        self.auto_log_table.scrollToBottom()

    def _automation_state_text(self, prefix: str, state_path: str) -> str:
        return f"{prefix}：{compact_state_path(state_path)}"

    def _auto_execution_mode(self) -> str:
        return self.auto_execution_combo.currentData()

    def _auto_live_status(self, signal: ScoredSignal) -> tuple[bool, str]:
        status = live_trading_status()
        if not status.enabled:
            return False, status.reason
        runtime_state = self.trading_runtime.state_manager.snapshot()
        stream = self.trading_runtime.user_data.status()
        sync_age = time.monotonic() - self._auto_live_sync_at
        if stream.running and stream.market == signal.market and runtime_state.sync_status.get("healthy") and sync_age < 60:
            return True, "复用本轮账户同步和 User Data Stream"
        try:
            mismatches = self.trading_runtime.start_live_sync(signal.market, signal.symbol)
            self._auto_live_sync_at = time.monotonic()
        except BinanceNetworkError as exc:
            return False, f"Binance 状态同步网络暂不可用，本轮不真仓，下一轮重试：{exc}"
        except Exception as exc:
            return False, f"Binance 状态同步失败，禁止真仓：{exc}"
        mismatch_text = f"，修正 {len(mismatches)} 项本地差异" if mismatches else ""
        return True, f"Binance 账户、仓位和活动订单已同步{mismatch_text}，User Data Stream 已启动"

    def _auto_market_fresh(self, signal: ScoredSignal) -> tuple[bool, str]:
        self.price_stream.update_symbols([(signal.market, signal.symbol)])
        self.price_stream.start()
        stream_settings = load_settings().get("auto_execution", {})
        wait_seconds = float(stream_settings.get("websocket_ready_wait_seconds", 30))
        max_age = float(stream_settings.get("websocket_max_age_seconds", 20))
        deadline = time.monotonic() + wait_seconds
        while True:
            if self.price_stream.latest_price(signal.market, signal.symbol, max_age_seconds=max_age) is not None:
                return True, "WebSocket 行情新鲜"
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        return False, f"自动真仓暂缓：WebSocket 行情 {wait_seconds:.0f} 秒内未就绪或超过 {max_age:.0f} 秒未更新，本轮跳过该币并继续检查其它候选"

    def _auto_live_order(
        self,
        plan: TradePlan,
        order_side: str,
        review: PlanRiskReview,
        signal: ScoredSignal,
        confirm: str,
        risk_line: str = "conservative",
    ) -> dict:
        plan = self._maker_entry_plan(plan, order_side)
        return self.trading_runtime.submit_plan(
            plan,
            order_side,
            review,
            market_fresh=self.price_stream.latest_price(
                plan.market,
                plan.symbol,
                max_age_seconds=float(load_settings().get("auto_execution", {}).get("websocket_max_age_seconds", 20)),
            ) is not None,
            allow_live=True,
            confirm=confirm,
            strategy=f"automatic:{signal.breakdown.selected_strategy}",
            risk_line=risk_line,
        )

    def _maker_entry_plan(self, plan: TradePlan, order_side: str) -> TradePlan:
        """Use book-ticker price for passive automated entries without REST polling."""
        from dataclasses import replace

        from trade_assistant.order_pricing import maker_limit_price

        execution = load_settings().get("auto_execution", {})
        max_age = float(execution.get("websocket_max_age_seconds", 20))
        quote = self.price_stream.latest_quote(plan.market, plan.symbol, max_age_seconds=max_age)
        reference = self.price_stream.latest_price(plan.market, plan.symbol, max_age_seconds=max_age) or plan.entry
        maker_price = maker_limit_price(
            order_side,
            best_bid=quote.best_bid if quote else None,
            best_ask=quote.best_ask if quote else None,
            reference_price=reference,
            fallback_offset_bps=float(execution.get("post_only_fallback_offset_bps", 3.0)),
        )
        if maker_price is None:
            return plan
        if (order_side == "BUY" and maker_price <= plan.stop) or (order_side == "SELL" and maker_price >= plan.stop):
            return plan
        return replace(plan, entry=maker_price)

    def _auto_pending_orders(self, candidates: list[ScoredSignal]) -> PendingOrderDecision | None:
        result = self.trading_runtime.manage_pending_entry_orders(candidates)
        if result is None:
            return None
        action = str(result.get("action") or "")
        if action in {"monitoring", "canceled", "wait", "filled", "blocked"}:
            return PendingOrderDecision(
                action=action,
                symbol=str(result.get("symbol") or ""),
                symbols=tuple(str(symbol) for symbol in result.get("symbols", ()) if symbol),
                message=str(result.get("message") or "挂单状态已更新"),
            )
        return None

    def _auto_position_order(
        self,
        managed: ManagedPosition,
        decision: PositionManagementDecision,
        confirm: str,
    ) -> dict:
        position = managed.position
        return self.trading_runtime.submit_position_decision(
            managed,
            decision,
            allow_live=True,
            confirm=confirm,
        )

    def _auto_account_equity(self, execution_mode: str) -> float:
        if execution_mode == AUTO_EXECUTION_LIVE:
            from trade_assistant.binance_client import BinanceClient

            risk = read_futures_account_risk(BinanceClient())
            equity = risk.wallet_balance + risk.total_unrealized_pnl
            return equity if equity > 0 else risk.wallet_balance
        portfolio = SimulatedPortfolio()
        cash = portfolio.cash_balance()
        unrealized = sum(float(record["unrealized_pnl"]) for record in portfolio.position_records())
        return max(0.0, cash + unrealized)

    def _auto_managed_positions(self, execution_mode: str) -> list[ManagedPosition]:
        records = SimulatedPortfolio().position_records()
        record_map = {(row["source"], row["market"], row["symbol"]): row for row in records}
        if execution_mode != AUTO_EXECUTION_LIVE:
            return [
                ManagedPosition(
                    position=SimulatedPortfolio().get_position(row["market"], row["symbol"], float(row["mark_price"])),
                    stop_price=float(row["stop_price"]),
                    target_price=float(row["target_price"]),
                    status=str(row["status"]),
                )
                for row in records
                if row["source"] == "simulated" and row["side"] in {"long", "short"} and float(row["quantity"]) > 0
            ]
        from trade_assistant.binance_client import BinanceClient

        risk = read_futures_account_risk(BinanceClient())
        managed: list[ManagedPosition] = []
        for position in risk.positions:
            row = record_map.get(("real", position.market, position.symbol)) or record_map.get(
                ("simulated", position.market, position.symbol)
            )
            managed.append(
                ManagedPosition(
                    position=position,
                    stop_price=float(row["stop_price"]) if row else 0.0,
                    target_price=float(row["target_price"]) if row else 0.0,
                    status=str(row["status"]) if row else "真实持仓",
                )
            )
        return managed

    def _auto_real_position(self, signal: ScoredSignal):
        from trade_assistant.binance_client import BinanceClient

        client = BinanceClient()
        if signal.market == "futures":
            return read_real_futures_position(client, signal.symbol)
        return read_real_spot_position(client, signal.symbol, signal.last)

    def fill_signal_table(self, table: QTableWidget, signals: list[Signal]) -> None:
        table.setRowCount(0)
        for signal in signals:
            row_data = signal_to_row(signal)
            row = table.rowCount()
            table.insertRow(row)
            for column, key in enumerate(SIGNAL_COLUMNS):
                item = QTableWidgetItem(row_data[key])
                item.setToolTip(row_data[key])
                item.setData(Qt.ItemDataRole.UserRole, signal)
                table.setItem(row, column, item)

    def signal_selected(self, table: QTableWidget) -> None:
        items = table.selectedItems()
        if not items:
            return
        signal = items[0].data(Qt.ItemDataRole.UserRole)
        if isinstance(signal, ScoredSignal):
            self.current_signal = signal
            base_signal = signal.signal
        elif isinstance(signal, Signal):
            self.current_signal = None
            base_signal = signal
        else:
            return
        self.symbol_input.setText(base_signal.symbol)
        self._set_combo_data(self.market_combo, base_signal.market)
        self._set_combo_data(self.side_combo, base_signal.side)
        auto_prices = auto_plan_prices(signal, self.strategy_mode_combo.currentData())
        self.entry_input.setText(f"{auto_prices.entry:.8f}")
        self.stop_input.setText(f"{auto_prices.stop:.8f}")
        self.target_input.setText(f"{auto_prices.target:.8f}")
        if auto_prices.adaptive is not None:
            self.risk_input.setText(f"{auto_prices.adaptive.risk_pct:.2f}")
            self.leverage_input.setText(f"{auto_prices.adaptive.suggested_leverage:.1f}")
        self.quantity_input.clear()
        log_text = f"已选择 {base_signal.symbol}，已自动填写计划：{auto_prices.risk_note}"
        if auto_prices.warning:
            log_text += f"；{auto_prices.warning}"
        self.append_log(log_text)

    def generate_plan(self) -> None:
        try:
            simulated, _, _ = detect_positions(
                symbol=self.symbol_input.text().strip().upper(),
                market=self.market_combo.currentData(),
                signal=self.current_signal,
            )
            plan, review = evaluate_plan_from_form(
                self.symbol_input.text(),
                self.market_combo.currentData(),
                self.side_combo.currentData(),
                self.entry_input.text(),
                self.stop_input.text(),
                self.target_input.text(),
                self.equity_input.text(),
                self.risk_input.text(),
                self.leverage_input.text(),
                self.current_signal,
                simulated,
                self.strategy_mode_combo.currentData(),
            )
            path = save_trade_plan(plan)
        except Exception as exc:
            QMessageBox.warning(self, "交易计划错误", str(exc))
            self.append_log(f"交易计划错误：{exc}")
            return
        self.current_plan = plan
        self.current_risk_review = review
        self.refresh_positions_page()
        self._fill_order_form_from_plan(plan)
        preview = trade_plan_to_markdown(plan) + "\n\n" + self._risk_review_text(review)
        if self.current_signal is not None:
            backtest = quick_backtest_for_signal(
                self.current_signal,
                self.strategy_mode_combo.currentData(),
                plan.loss_pct_to_stop,
                plan.gain_pct_to_target / plan.loss_pct_to_stop if plan.loss_pct_to_stop else 1.0,
            )
            if backtest is not None:
                preview += "\n\n" + self._backtest_text(backtest)
        self.plan_preview.setPlainText(preview)
        self.append_log(f"交易计划已保存：{path}")
        self.append_log(self._risk_review_text(review))
        self.show_page(1)

    def _apply_plan_to_forms(self, plan: TradePlan) -> None:
        self.symbol_input.setText(plan.symbol)
        self._set_combo_data(self.market_combo, plan.market)
        self._set_combo_data(self.side_combo, plan.side)
        self.entry_input.setText(f"{plan.entry:.8f}")
        self.stop_input.setText(f"{plan.stop:.8f}")
        self.target_input.setText(f"{plan.target:.8f}")
        self.equity_input.setText(f"{plan.equity:.2f}")
        self.risk_input.setText(f"{plan.risk_pct:.2f}")
        self.leverage_input.setText(f"{plan.leverage:.1f}")
        self._fill_order_form_from_plan(plan)

    def _fill_order_form_from_plan(self, plan: TradePlan) -> None:
        self.manual_reduce_only_order = False
        order_side = "BUY" if plan.side == "long" else "SELL"
        self._set_combo_data(self.order_side_combo, order_side)
        self._set_combo_data(self.order_type_combo, "LIMIT")
        self.quantity_input.setText(f"{plan.quantity:.8f}")
        self.price_input.setText(f"{plan.entry:.8f}")
        self.live_confirm_input.clear()

    def submit_order(self, live: bool) -> None:
        manual_reduce_only_order = self.manual_reduce_only_order
        self.refresh_status()
        if live:
            if not manual_reduce_only_order:
                market_guard = self._fresh_market_data_guard()
                if market_guard:
                    QMessageBox.warning(self, "实时行情锁定", market_guard)
                    self.append_log(market_guard)
                    return
                if self.current_risk_review and not self.current_risk_review.live_allowed:
                    QMessageBox.warning(self, "风控锁定", "当前计划风控评分不足或强平安全垫不足，只允许模拟。")
                    self.append_log("风控锁定：当前计划只允许模拟，不发送真实订单。")
                    return
                guard = self._daily_loss_guard()
                if not guard.live_allowed:
                    QMessageBox.warning(self, "日亏损锁定", guard.message)
                    self.append_log(guard.message)
                    return
            status = live_trading_status()
            if not status.enabled:
                QMessageBox.warning(self, "真下单锁定", status.reason)
                self.append_log(status.reason)
                return
            if self.live_confirm_input.text() != LIVE_CONFIRMATION:
                QMessageBox.warning(self, "确认文字错误", f"请输入 {LIVE_CONFIRMATION}")
                return
            answer = QMessageBox.question(self, "确认真下单", "这会向 Binance 发送真实订单。确认继续？")
            if answer != QMessageBox.StandardButton.Yes:
                self.append_log("用户取消真下单。")
                return
        try:
            if live:
                if manual_reduce_only_order:
                    result = order_from_form(
                        self.market_combo.currentData(),
                        self.symbol_input.text(),
                        self.order_side_combo.currentData(),
                        self.quantity_input.text(),
                        self.order_type_combo.currentData(),
                        self.price_input.text(),
                        allow_live=True,
                        confirm=self.live_confirm_input.text(),
                        reduce_only=True,
                    )
                elif self.current_plan is None or self.current_risk_review is None:
                    raise ValueError("真下单前必须先生成并通过交易计划")
                else:
                    self.trading_runtime.start_live_sync(self.current_plan.market, self.current_plan.symbol)
                    result = self.trading_runtime.submit_plan(
                        self.current_plan,
                        self.order_side_combo.currentData(),
                        self.current_risk_review,
                        market_fresh=True,
                        allow_live=True,
                        confirm=self.live_confirm_input.text(),
                        strategy="manual",
                        order_type=self.order_type_combo.currentData(),
                    )
            else:
                result = order_from_form(
                    self.market_combo.currentData(),
                    self.symbol_input.text(),
                    self.order_side_combo.currentData(),
                    self.quantity_input.text(),
                    self.order_type_combo.currentData(),
                    self.price_input.text(),
                    allow_live=False,
                    confirm="",
                )
        except Exception as exc:
            QMessageBox.warning(self, "下单错误", str(exc))
            self.append_log(f"下单错误：{exc}")
            return
        self.append_log(json.dumps(result, indent=2, ensure_ascii=False))
        if live and not result.get("dry_run", False):
            if manual_reduce_only_order:
                try:
                    mismatches = self.trading_runtime.start_live_sync(
                        self.market_combo.currentData(),
                        self.symbol_input.text().strip().upper() or None,
                    )
                    if mismatches:
                        self.append_log("平仓后 Binance 对账已修正：" + "；".join(mismatches))
                except Exception as exc:
                    self.append_log(f"平仓后 Binance 对账失败，等待定期刷新：{exc}")
            self.set_position_source_mode("real")
            self.live_reconcile_timer.start()
            if not self.realtime_monitor_checkbox.isChecked():
                self.realtime_monitor_checkbox.setChecked(True)
            self.append_log("真下单已发送：已切换真实仓，并按 1 秒刷新真实仓位/API。")
        if manual_reduce_only_order:
            self.manual_reduce_only_order = False

    def submit_simulated_order(self) -> None:
        manual_reduce_only_order = self.manual_reduce_only_order
        try:
            result, position = simulate_order_from_form(
                self.market_combo.currentData(),
                self.symbol_input.text(),
                self.order_side_combo.currentData(),
                self.quantity_input.text(),
                self.order_type_combo.currentData(),
                self.price_input.text(),
                self.entry_input.text(),
                self.leverage_input.text(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "模拟下单错误", str(exc))
            self.append_log(f"模拟下单错误：{exc}")
            return
        text = "模拟下单已写入本地模拟仓：\n" + json.dumps(result, indent=2, ensure_ascii=False)
        text += "\n" + self._position_text(position)
        self.position_status_box.setPlainText(text)
        self._sync_position_record_from_snapshot(position)
        if manual_reduce_only_order:
            self.manual_reduce_only_order = False
        if self.current_plan is not None and position.side != "flat" and not manual_reduce_only_order:
            self._save_plan_position_record(self.current_plan, "模拟持仓")
        self.refresh_positions_page()
        self.append_log(text)

    def fill_close_order_from_selected_position(self) -> None:
        row = self.positions_table.currentRow()
        values = self._position_values_from_row(row) if self.positions_table.selectedItems() else None
        if values is None:
            values = self.selected_position_values
        if values is None:
            QMessageBox.information(self, "选择仓位", "请先在仓位表里选中要平仓的币。")
            return
        symbol = values["交易对"].strip()
        if not symbol:
            return
        market = "futures" if values["市场"] == "合约" else "spot"
        position_side = "long" if values["方向"] == "做多" else "short"
        exit_side = "SELL" if position_side == "long" else "BUY"
        quantity = values["数量"].strip()
        try:
            if is_flat_quantity(float(quantity)):
                self.refresh_positions_page()
                QMessageBox.information(self, "空仓", f"{symbol} 当前数量为 0，无需平仓。")
                return
        except ValueError:
            QMessageBox.warning(self, "数量错误", f"{symbol} 仓位数量无效：{quantity}")
            return
        mark_price = values["当前价"].strip()
        self.symbol_input.setText(symbol)
        self._set_combo_data(self.market_combo, market)
        self._set_combo_data(self.side_combo, position_side)
        self.entry_input.setText(mark_price)
        self._set_combo_data(self.order_side_combo, exit_side)
        self._set_combo_data(self.order_type_combo, "MARKET")
        self.quantity_input.setText(quantity)
        self.price_input.clear()
        self.live_confirm_input.clear()
        self.manual_reduce_only_order = True
        self.show_page(2)
        self.append_log(f"已填入 {symbol} 平仓单：{exit_side} {quantity}，真仓会使用 Reduce Only。")

    def clear_all_simulated_positions(self) -> None:
        portfolio = SimulatedPortfolio()
        if portfolio.simulated_residue_count() <= 0:
            message = (
                "当前本地模拟仓没有持仓或残留。\n"
                f"当前模拟库：{portfolio.db_path.resolve()}\n"
                "如果界面上仍看到仓位，请确认你打开的是固定新版 EXE。"
            )
            QMessageBox.information(self, "模拟仓", message)
            self.position_status_box.setPlainText(message)
            self.append_log(message)
            self.set_position_source_mode("simulated")
            return
        result = QMessageBox.question(
            self,
            "确认清空模拟仓",
            "只会清空本地模拟仓，不会发送任何 Binance 真仓订单。确认要按当前记录价平掉所有模拟仓吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        try:
            count = portfolio.clear_all_positions()
        except Exception as exc:
            QMessageBox.warning(self, "模拟仓清仓失败", str(exc))
            self.append_log(f"模拟仓一键清仓失败：{exc}")
            return
        self.manual_reduce_only_order = False
        self.selected_position_key = None
        self.selected_position_values = None
        residue = portfolio.simulated_residue_count()
        status = (
            f"模拟仓已一键清仓：已处理 {count} 个本地模拟仓/残留，不涉及真实 API。\n"
            f"当前模拟库：{portfolio.db_path.resolve()}"
        )
        if residue:
            status += f"\n仍检测到 {residue} 条模拟残留，请把日志截图发我。"
        else:
            status += "\n复查结果：模拟仓已清空。"
        self.position_status_box.setPlainText(status)
        self.set_position_source_mode("simulated")
        self.refresh_positions_page()
        self.append_log(status)

    def detect_current_position(self) -> None:
        symbol = self.symbol_input.text().strip().upper()
        if not symbol:
            QMessageBox.information(self, "缺少交易对", "请先选择或填写交易对。")
            return
        try:
            simulated, real, advice = detect_positions(
                symbol=symbol,
                market=self.market_combo.currentData(),
                signal=self.current_signal,
            )
        except Exception as exc:
            QMessageBox.warning(self, "检测仓位失败", str(exc))
            self.append_log(f"检测仓位失败：{exc}")
            return
        lines = [
            f"当前币种：{symbol}",
            f"模拟仓：{self._position_text(simulated)}",
            f"真实API：{self._position_text(real) if real else '未配置或未读取'}",
            f"策略建议：{advice.summary}",
        ]
        if self.market_combo.currentData() == "futures" and live_trading_status().has_api_key:
            try:
                from trade_assistant.binance_client import BinanceClient

                lines.append(self._account_risk_text(read_futures_account_risk(BinanceClient())))
            except Exception as exc:
                lines.append(f"合约账户看板：读取失败，{exc}")
        if advice.warnings:
            lines.append("风险提示：" + "；".join(advice.warnings))
        text = "\n".join(lines)
        self.position_status_box.setPlainText(text)
        self._sync_position_record_from_snapshot(simulated)
        if real:
            self._sync_position_record_from_snapshot(real)
        self.refresh_positions_page()
        self.append_log(text)

    def _position_text(self, position) -> str:
        if position.side == "flat" or is_flat_quantity(position.quantity):
            return "空仓"
        side = "多仓" if position.side == "long" else "空头"
        return (
            f"{side} 数量 {position.quantity:.8f}，均价 {position.entry_price:.8f}，"
            f"标记价 {position.mark_price:.8f}，浮盈亏 {position.unrealized_pnl:.2f} USDT"
        )

    def _save_plan_position_record(self, plan: TradePlan, status: str) -> None:
        SimulatedPortfolio().upsert_position_record(
            source="simulated",
            market=plan.market,
            symbol=plan.symbol,
            side=plan.side,
            quantity=plan.quantity,
            entry_price=plan.entry,
            mark_price=plan.entry,
            stop_price=plan.stop,
            target_price=plan.target,
            leverage=plan.leverage,
            realized_pnl=0.0,
            status=status,
        )

    def _sync_position_record_from_snapshot(self, position) -> None:
        if position.source == "real":
            self.trading_runtime.state_manager.upsert_position_snapshot(asdict(position))
        if position.side == "flat" or is_flat_quantity(position.quantity):
            SimulatedPortfolio().close_position_record(
                position.source,
                position.market,
                position.symbol,
                position.mark_price,
            )
            return
        self._save_detected_position_record(position)

    def _save_detected_position_record(self, position) -> None:
        stop = float(self.stop_input.text()) if self.stop_input.text().strip() else 0.0
        target = float(self.target_input.text()) if self.target_input.text().strip() else 0.0
        SimulatedPortfolio().upsert_position_record(
            source=position.source,
            market=position.market,
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            mark_price=position.mark_price,
            stop_price=stop,
            target_price=target,
            leverage=position.leverage,
            realized_pnl=position.realized_pnl,
            status="真实持仓" if position.source == "real" else "模拟持仓",
        )

    def set_position_source_mode(self, mode: str) -> None:
        self.position_source_mode = mode
        self.sim_positions_button.setChecked(mode == "simulated")
        self.real_positions_button.setChecked(mode == "real")
        self.refresh_positions_page()
        if self.realtime_monitor_checkbox.isChecked():
            self.run_realtime_monitor_cycle()

    def toggle_realtime_monitor(self, enabled: bool) -> None:
        if enabled:
            targets = self._monitor_targets()
            self.price_stream.update_symbols([(target.market, target.symbol) for target in targets])
            self.price_stream.start()
            self.monitor_timer.start(self.monitor_price_interval_spin.value() * 1000)
            self.monitor_position_timer.start(self.monitor_position_interval_spin.value() * 1000)
            self.monitor_status_label.setText("实时监控已启动：WebSocket 秒级行情，真仓默认只报警。")
            self.append_log("实时监控已启动。")
            self.run_realtime_monitor_cycle()
            self.refresh_positions_page()
        else:
            self.monitor_timer.stop()
            self.monitor_position_timer.stop()
            self.price_stream.stop()
            self.monitor_status_label.setText("实时监控已停止")
            self.append_log("实时监控已停止。")

    def run_realtime_monitor_cycle(self) -> None:
        if self.monitor_cycle_running:
            return
        targets = self._monitor_targets()
        if not targets:
            self.monitor_table.setRowCount(0)
            self.monitor_status_label.setText("实时监控已启动：暂无计划或持仓目标。")
            return
        self.price_stream.update_symbols([(target.market, target.symbol) for target in targets])
        self.price_stream.start()
        self.monitor_cycle_running = True
        worker = FunctionWorker(self._run_realtime_monitor_worker, targets)
        worker.signals.result.connect(self.realtime_monitor_cycle_finished)
        worker.signals.error.connect(self.realtime_monitor_error)
        worker.signals.finished.connect(lambda: setattr(self, "monitor_cycle_running", False))
        self.thread_pool.start(worker)

    def _run_realtime_monitor_worker(self, targets: list[MonitorTarget]) -> list[MonitorResult]:
        prices: dict[tuple[str, str], float] = {}
        results: list[MonitorResult] = []
        for target in targets:
            key = (target.market, target.symbol)
            if key not in prices:
                price = self.price_stream.latest_price(target.market, target.symbol, max_age_seconds=5)
                if price is None:
                    results.append(
                        MonitorResult(
                            target=target,
                            price=0.0,
                            unrealized_pnl=0.0,
                            r_multiple=0.0,
                            alerts=["等待 WebSocket 行情，不使用 REST 轮询"],
                            severity="waiting",
                        )
                    )
                    continue
                prices[key] = price
            results.append(evaluate_monitor_target(target, prices[key]))
        return results

    def realtime_monitor_cycle_finished(self, results: list[MonitorResult]) -> None:
        self.monitor_cycle_running = False
        self.monitor_table.setRowCount(0)
        for result in results:
            waiting = result.severity == "waiting"
            if not waiting:
                self._update_simulated_mark_price_from_monitor(result)
            row = self.monitor_table.rowCount()
            self.monitor_table.insertRow(row)
            values = [
                result.target.symbol,
                "做空" if result.target.side == "short" else "做多",
                "等待" if waiting else f"{result.price:.8f}",
                "-" if waiting else f"{result.r_multiple:.2f}R",
                "-" if waiting else f"{result.unrealized_pnl:.2f}",
                result.alert_text,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.monitor_table.setItem(row, column, item)
            alert_key = f"{result.target.market}:{result.target.symbol}:{result.target.side}"
            if result.alerts and not waiting and self.last_monitor_alerts.get(alert_key) != result.alert_text:
                self.append_log(f"实时监控 {result.target.symbol}：{result.alert_text}")
                self.last_monitor_alerts[alert_key] = result.alert_text
        waiting_count = sum(1 for result in results if result.severity == "waiting")
        prefix = "实时监控等待 WebSocket 行情" if waiting_count else "实时监控已更新"
        self.monitor_status_label.setText(
            f"{prefix}：{len(results)} 个目标，{QDateTime.currentDateTime().toString('HH:mm:ss')}，"
            f"{self.price_stream.freshness_text()}"
        )
        if self.position_source_mode == "simulated":
            self.refresh_positions_page()

    def _update_simulated_mark_price_from_monitor(self, result: MonitorResult) -> None:
        SimulatedPortfolio().update_position_record_mark_price(
            "simulated",
            result.target.market,
            result.target.symbol,
            result.price,
        )

    def realtime_monitor_error(self, text: str) -> None:
        self.monitor_cycle_running = False
        if self._is_websocket_wait_text(text):
            self._handle_websocket_waiting(text)
            return
        if self._is_rate_limit_text(text):
            self._pause_rest_polling_after_rate_limit(text)
            return
        self.monitor_status_label.setText(f"实时监控行情读取失败：{text[-180:]}")
        self.append_log(f"实时监控行情读取失败：{text[-500:]}")

    def _monitor_targets(self) -> list[MonitorTarget]:
        targets: list[MonitorTarget] = []
        if self.position_source_mode == "real":
            targets.extend(self._real_monitor_targets())
        else:
            if self.current_plan is not None:
                targets.append(self._target_from_plan(self.current_plan))
            form_target = self._target_from_form()
            if form_target is not None:
                targets.append(form_target)
            for record in SimulatedPortfolio().position_records(source="simulated"):
                if record["side"] in {"long", "short"} and float(record["quantity"]) > 0:
                    targets.append(
                        MonitorTarget(
                            market=record["market"],
                            symbol=record["symbol"],
                            side=record["side"],
                            quantity=float(record["quantity"]),
                            entry=float(record["entry_price"]),
                            stop=float(record["stop_price"]),
                            target=float(record["target_price"]),
                        )
                    )
        return self._dedupe_monitor_targets(targets)

    def _real_monitor_targets(self) -> list[MonitorTarget]:
        targets: list[MonitorTarget] = []
        local_records = {
            (row["market"], row["symbol"]): row
            for row in SimulatedPortfolio().position_records(active_only=False)
            if row["source"] in {"real", "simulated"}
        }
        for position in self.trading_runtime.real_position_rows():
            market = str(position.get("market", "futures"))
            symbol = str(position.get("symbol", "")).upper()
            side = str(position.get("side", "flat"))
            quantity = float(position.get("quantity", 0))
            if side not in {"long", "short"} or quantity <= 0 or not symbol:
                continue
            local = local_records.get((market, symbol), {})
            targets.append(
                MonitorTarget(
                    market=market,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry=float(position.get("entry_price") or 0),
                    stop=float(local.get("stop_price") or 0),
                    target=float(local.get("target_price") or 0),
                    liquidation_price=self._optional_float(position.get("liquidation_price")),
                )
            )
        return targets

    def _optional_float(self, value) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    def _target_from_plan(self, plan: TradePlan) -> MonitorTarget:
        return MonitorTarget(
            market=plan.market,
            symbol=plan.symbol,
            side=plan.side,
            quantity=plan.quantity,
            entry=plan.entry,
            stop=plan.stop,
            target=plan.target,
        )

    def _target_from_form(self) -> MonitorTarget | None:
        symbol = self.symbol_input.text().strip().upper()
        if not symbol:
            return None
        try:
            entry = float(self.entry_input.text())
            stop = float(self.stop_input.text())
            target = float(self.target_input.text())
            quantity = float(self.quantity_input.text()) if self.quantity_input.text().strip() else 0.0
        except ValueError:
            return None
        return MonitorTarget(
            market=self.market_combo.currentData(),
            symbol=symbol,
            side=self.side_combo.currentData(),
            quantity=quantity,
            entry=entry,
            stop=stop,
            target=target,
        )

    def _fresh_market_data_guard(self) -> str | None:
        symbol = self.symbol_input.text().strip().upper()
        if not symbol:
            return "缺少交易对，禁止真下单。"
        market = self.market_combo.currentData()
        self.price_stream.update_symbols([(market, symbol)])
        self.price_stream.start()
        if self.price_stream.latest_price(market, symbol, max_age_seconds=5) is None:
            return "实时 WebSocket 行情未就绪或超过 5 秒未更新，真下单已锁定。"
        return None

    def closeEvent(self, event) -> None:
        self.price_stream.stop()
        self.trading_runtime.stop()
        self.runtime_metrics_timer.stop()
        self.live_reconcile_timer.stop()
        super().closeEvent(event)

    def _dedupe_monitor_targets(self, targets: list[MonitorTarget]) -> list[MonitorTarget]:
        deduped: dict[tuple[str, str, str, float, float, float], MonitorTarget] = {}
        for target in targets:
            key = (target.market, target.symbol, target.side, target.entry, target.stop, target.target)
            deduped[key] = target
        return list(deduped.values())

    def position_selected(self) -> None:
        if not self.positions_table.selectedItems():
            return
        values = self._position_values_from_row(self.positions_table.currentRow())
        if values is None:
            return
        self.selected_position_values = values
        self.selected_position_key = self._position_key_from_values(values)

    def _position_values_from_row(self, row: int) -> dict[str, str] | None:
        if row < 0 or row >= self.positions_table.rowCount():
            return None
        return {
            key: self.positions_table.item(row, index).text() if self.positions_table.item(row, index) else ""
            for index, key in enumerate(POSITION_COLUMNS)
        }

    def _position_key_from_values(self, values: dict[str, str]) -> tuple[str, str, str, str]:
        return (
            values.get("来源", ""),
            values.get("市场", ""),
            values.get("交易对", ""),
            values.get("方向", ""),
        )

    def _restore_position_selection(self, key: tuple[str, str, str, str] | None) -> bool:
        if key is None:
            return False
        for row in range(self.positions_table.rowCount()):
            values = self._position_values_from_row(row)
            if values is not None and self._position_key_from_values(values) == key:
                self.positions_table.selectRow(row)
                self.selected_position_values = values
                self.selected_position_key = key
                return True
        return False

    def refresh_positions_page(self) -> None:
        portfolio = SimulatedPortfolio()
        if hasattr(self, "sim_db_path_label"):
            self.sim_db_path_label.setText(
                f"当前模拟库：{portfolio.db_path.resolve()}｜模拟残留 {portfolio.simulated_residue_count()} 条"
            )
        rows = (
            self._real_position_rows()
            if self.position_source_mode == "real"
            else portfolio.position_records(source="simulated")
        )
        selected_key = self.selected_position_key
        self.positions_table.blockSignals(True)
        self.positions_table.setRowCount(0)
        for record in rows:
            display = {
                "来源": "模拟/本地" if record["source"] == "simulated" else "真实API",
                "市场": "合约" if record["market"] == "futures" else "现货",
                "交易对": record["symbol"],
                "方向": "做多" if record["side"] == "long" else "做空",
                "数量": f"{record['quantity']:.8f}",
                "入场价": f"{record['entry_price']:.8f}",
                "当前价": f"{record['mark_price']:.8f}",
                "杠杆": f"{self._position_leverage(record):.1f}x",
                "购买保证金(USDT)": f"{self._position_total_usdt(record, 'entry_price'):.2f}",
                "当前保证金(USDT)": f"{self._position_total_usdt(record, 'mark_price'):.2f}",
                "止损价": f"{record['stop_price']:.8f}",
                "止盈价": f"{record['target_price']:.8f}",
                "已实现盈亏": f"{record['realized_pnl']:.2f}",
                "未实现盈亏": f"{record['unrealized_pnl']:.2f}",
                "状态": record["status"],
                "更新时间": record["updated_at"],
            }
            row_index = self.positions_table.rowCount()
            self.positions_table.insertRow(row_index)
            for column, key in enumerate(POSITION_COLUMNS):
                item = QTableWidgetItem(display[key])
                item.setToolTip(display[key])
                self.positions_table.setItem(row_index, column, item)
        self.positions_table.blockSignals(False)
        if not self._restore_position_selection(selected_key):
            self.positions_table.clearSelection()
            self.selected_position_key = None
            self.selected_position_values = None

    def _real_position_rows(self) -> list[dict]:
        if not live_trading_status().has_api_key or not live_trading_status().has_api_secret:
            self.append_log("真实仓需要配置 Binance API Key / Secret。")
            return []
        try:
            self.trading_runtime.update_credentials(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))
            if not self.trading_runtime.user_data.status().running:
                mismatches = self.trading_runtime.start_live_sync("futures")
                self.live_reconcile_timer.start()
                if mismatches:
                    self.append_log("Binance 对账已修正：" + "；".join(mismatches))
            else:
                self.trading_runtime.ensure_user_stream("futures")
        except Exception as exc:
            if self._is_rate_limit_text(str(exc)):
                self._pause_rest_polling_after_rate_limit(str(exc))
                return []
            self.append_log(f"真实仓读取失败：{exc}")
            return []
        positions = self.trading_runtime.real_position_rows()
        self.price_stream.update_symbols(
            [(str(position.get("market", "futures")), str(position.get("symbol", ""))) for position in positions]
        )
        self.price_stream.start()
        records = SimulatedPortfolio().position_records()
        record_map = {(row["source"], row["market"], row["symbol"]): row for row in records}
        rows: list[dict] = []
        for position in positions:
            market = str(position.get("market", "futures"))
            symbol = str(position.get("symbol", ""))
            side = str(position.get("side", "flat"))
            quantity = float(position.get("quantity", 0))
            entry_price = float(position.get("entry_price", 0))
            mark_price = self.price_stream.latest_price(market, symbol, max_age_seconds=5) or float(
                position.get("mark_price", entry_price)
            )
            direction = -1 if side == "short" else 1
            local_plan = record_map.get(("real", market, symbol)) or record_map.get(("simulated", market, symbol))
            rows.append(
                {
                    "source": "real",
                    "market": market,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "notional": quantity * mark_price,
                    "stop_price": float(local_plan["stop_price"]) if local_plan else 0.0,
                    "target_price": float(local_plan["target_price"]) if local_plan else 0.0,
                    "leverage": float(position.get("leverage") or 1.0),
                    "realized_pnl": float(position.get("realized_pnl", 0)),
                    "unrealized_pnl": (mark_price - entry_price) * quantity * direction,
                    "status": "真实持仓/User Stream",
                    "updated_at": str(position.get("updated_at", "")),
                }
            )
        return rows

    def _position_total_usdt(self, record: dict, price_key: str) -> float:
        quantity = abs(float(record.get("quantity") or 0))
        price = float(record.get(price_key) or 0)
        notional = quantity * price
        if record.get("market") != "futures":
            return notional
        return notional / self._position_leverage(record)

    def _position_leverage(self, record: dict) -> float:
        try:
            leverage = float(record.get("leverage") or 1.0)
        except (TypeError, ValueError):
            leverage = 1.0
        return max(1.0, leverage)

    def run_live_reconciliation(self) -> None:
        if self.live_reconcile_running or not self.trading_runtime.user_data.status().running:
            return
        self.live_reconcile_running = True
        worker = FunctionWorker(self.trading_runtime.sync_live_state)
        worker.signals.result.connect(self.live_reconciliation_finished)
        worker.signals.error.connect(self.live_reconciliation_error)
        worker.signals.finished.connect(lambda: setattr(self, "live_reconcile_running", False))
        self.thread_pool.start(worker)

    def live_reconciliation_finished(self, mismatches: list[str]) -> None:
        self.live_reconcile_running = False
        if mismatches:
            self.append_log("定期对账已按 Binance 修正：" + "；".join(mismatches))
        if self.position_source_mode == "real":
            self.refresh_positions_page()

    def live_reconciliation_error(self, text: str) -> None:
        self.live_reconcile_running = False
        if self._is_rate_limit_text(text):
            self._pause_rest_polling_after_rate_limit(text)
            return
        self.append_log(f"Binance 定期对账失败：{_compact_worker_error(text)}")

    def _is_rate_limit_text(self, text: str) -> bool:
        lower_text = text.lower()
        return (
            "http 429" in lower_text
            or "-1003" in lower_text
            or "too many requests" in lower_text
            or "限流" in text
        )

    def _is_transient_network_text(self, text: str) -> bool:
        lower_text = text.lower()
        return (
            "timeout" in lower_text
            or "timed out" in lower_text
            or "urlopen error" in lower_text
            or "handshake operation timed out" in lower_text
            or "网络超时" in text
            or "连接失败" in text
        )

    def _is_websocket_wait_text(self, text: str) -> bool:
        return "WebSocket 行情未就绪" in text or "WebSocket 行情等待中" in text

    def _handle_websocket_waiting(self, text: str) -> None:
        message = (
            "实时监控等待 WebSocket 行情：订阅已保持，不使用 REST 轮询。"
            f"{self.price_stream.freshness_text()}"
        )
        self.monitor_status_label.setText(message)
        if self.last_websocket_wait_text != text:
            self.append_log(f"{message} 原因：{text[-300:]}")
            self.last_websocket_wait_text = text

    def _pause_rest_polling_after_rate_limit(self, text: str) -> None:
        self.monitor_timer.stop()
        self.monitor_position_timer.stop()
        self.live_reconcile_timer.stop()
        if self.realtime_monitor_checkbox.isChecked():
            self.realtime_monitor_checkbox.blockSignals(True)
            self.realtime_monitor_checkbox.setChecked(False)
            self.realtime_monitor_checkbox.blockSignals(False)
        if self.auto_trading_enabled or self.auto_timer.isActive():
            self.stop_auto_trading("Binance 限流：自动交易已暂停，避免继续触发 429。")
        message = (
            "Binance 限流：已暂停实时监控、真实仓对账和自动交易。"
            "请等待 90-120 秒后再手动开启；行情尽量走 WebSocket，REST 只做低频对账。"
        )
        self.monitor_status_label.setText(message)
        self.append_log(f"{message} 原因：{text[-500:]}")

    def _risk_review_text(self, review: PlanRiskReview) -> str:
        lines = [
            "风控评审：",
            f"- 计划质量评分：{review.quality_score}/100，建议：{review.recommended_action}",
            (
                f"- 资金分池：{review.risk_bucket}池 {review.allocation_pct:.0f}%"
                f"，本计划可用 {review.allocation_equity:.2f} / 总本金 {review.total_equity:.2f} USDT"
            ),
            f"- 强平安全垫：{review.liquidation_status}",
            f"- 建议杠杆：不超过 {review.suggested_leverage:.1f}x",
        ]
        if review.liquidation_price is not None:
            lines.append(f"- {review.liquidation_source}：{review.liquidation_price:.8f}")
        if review.liquidation_buffer_pct is not None:
            lines.append(f"- 止损到强平缓冲：{review.liquidation_buffer_pct:.2f}%")
        if review.reasons:
            lines.append("- 推荐理由：" + "；".join(review.reasons))
        if review.warnings:
            lines.append("- 风险点：" + "；".join(review.warnings))
        lines.append("- 移动止损/减仓：" + "；".join(review.management_rules))
        if self.current_plan is not None and self.current_plan.market == "futures":
            drafts = build_exit_order_drafts(self.current_plan)
            lines.append("- 止损/止盈单草稿：" + json.dumps(drafts, ensure_ascii=False))
        return "\n".join(lines)

    def _account_risk_text(self, risk) -> str:
        lines = [
            "合约账户看板：",
            f"钱包余额 {risk.wallet_balance:.2f} USDT，可用 {risk.available_balance:.2f} USDT，浮盈亏 {risk.total_unrealized_pnl:.2f} USDT",
        ]
        for position in risk.positions[:5]:
            lines.append(
                f"{position.symbol} {position.side} {position.quantity:.8f}，"
                f"{position.margin_type or 'unknown'}，杠杆 {position.leverage:.1f}x，"
                f"强平 {position.liquidation_price or 0:.8f}"
            )
        return "\n".join(lines)

    def _backtest_text(self, result) -> str:
        return "\n".join(
            [
                "轻量复盘：",
                f"- 样本交易数：{result.trades}",
                f"- 命中率：{result.win_rate:.2f}%",
                f"- 平均R：{result.average_r:.3f}",
                f"- 最大回撤R：{result.max_drawdown_r:.3f}",
            ]
        )

    def _daily_loss_guard(self):
        try:
            equity = float(self.equity_input.text())
        except ValueError:
            equity = 0.0
        from trade_assistant.binance_client import BinanceClient
        from trade_assistant.portfolio import SimulatedPortfolio, futures_today_realized_pnl

        settings = load_settings()
        stop_pct = float(settings.get("daily_loss_stop_pct", 2.0))
        warning_pct = float(settings.get("daily_loss_warning_pct", stop_pct * 0.75))
        portfolio = SimulatedPortfolio()
        simulated_loss = portfolio.today_realized_pnl()
        if self.market_combo.currentData() != "futures":
            return daily_loss_guard(equity, simulated_loss, 0.0, stop_pct, warning_pct)
        if not live_trading_status().has_api_key or not live_trading_status().has_api_secret:
            return daily_loss_guard(equity, simulated_loss, 0.0, stop_pct, warning_pct)
        try:
            real_loss = futures_today_realized_pnl(BinanceClient())
        except Exception:
            return account_read_failed_guard()
        return daily_loss_guard(equity, simulated_loss + real_loss, 0.0, stop_pct, warning_pct)

    def worker_error(self, text: str) -> None:
        self.public_status_label.setText("公共行情：异常")
        summary = _compact_worker_error(text, 500)
        self.append_log(f"后台任务失败：{summary}")
        QMessageBox.warning(self, "后台任务失败", summary)

    def open_file(self, path: Path | None) -> None:
        if not path or not path.exists():
            QMessageBox.information(self, "文件不存在", "还没有可打开的报告文件。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def load_settings_into_form(self) -> None:
        settings = load_settings()
        self.quote_asset_input.setText(settings["quote_asset"])
        self.exclude_symbols_input.setText(", ".join(settings["exclude_symbols"]))
        self.min_quote_volume_spin.setValue(float(settings["min_quote_volume"]))
        self.default_equity_spin.setValue(float(settings["default_equity"]))
        self.default_risk_spin.setValue(float(settings["default_risk_pct"]))
        self.default_leverage_spin.setValue(float(settings["default_leverage"]))
        self.daily_loss_stop_spin.setValue(float(settings.get("daily_loss_stop_pct", 2.0)))
        self.intraday_atr_multiplier_spin.setValue(float(settings.get("intraday_atr_multiplier", 1.4)))
        self.swing_atr_multiplier_spin.setValue(float(settings.get("swing_atr_multiplier", 1.8)))
        self.min_live_score_spin.setValue(int(settings.get("min_live_score", 75)))
        score_config = settings.get("signal_score", {})
        weights = score_config.get("weights", {})
        for key, fallback in {
            "trend": 30,
            "momentum": 20,
            "volume": 15,
            "position": 15,
            "timeframe": 10,
            "regime": 10,
        }.items():
            self.score_weight_spins[key].setValue(int(weights.get(key, fallback)))
        thresholds = score_config.get("thresholds", {})
        self.score_grade_a_spin.setValue(int(thresholds.get("grade_a", 90)))
        self.score_grade_b_spin.setValue(int(thresholds.get("grade_b", 75)))
        self.score_observe_spin.setValue(int(thresholds.get("observe", 60)))
        kernel_risk = settings.get("system_risk", {})
        self.kernel_single_risk_spin.setValue(float(kernel_risk.get("max_single_risk_pct", 1.0)))
        self.kernel_total_exposure_spin.setValue(float(kernel_risk.get("max_total_exposure_multiple", 3.0)))
        self.kernel_symbol_exposure_spin.setValue(float(kernel_risk.get("max_symbol_exposure_pct", 40.0)))
        self.kernel_max_leverage_spin.setValue(float(kernel_risk.get("max_leverage", 5.0)))
        self.kernel_reduce_losses_spin.setValue(int(kernel_risk.get("reduce_after_consecutive_losses", 3)))
        self.kernel_stop_losses_spin.setValue(int(kernel_risk.get("stop_after_consecutive_losses", 5)))
        order_manager = settings.get("order_manager", {})
        self._set_combo_data(
            self.partial_fill_policy_combo, str(order_manager.get("partial_fill_policy", "wait"))
        )
        self.partial_fill_timeout_spin.setValue(int(order_manager.get("partial_fill_timeout_seconds", 30)))
        self.auto_protective_orders_checkbox.setChecked(
            bool(order_manager.get("auto_place_protective_orders", False))
        )
        self.settings_scan_limit_spin.setValue(int(settings["scan_limit"]))
        self.scan_top_spin.setValue(int(settings["scan_limit"]))
        self.equity_input.setText(str(settings["default_equity"]))
        self.risk_input.setText(str(settings["default_risk_pct"]))
        self.leverage_input.setText(str(settings["default_leverage"]))
        self._load_api_credentials_into_form()

    def save_settings_from_form(self) -> None:
        weights = {key: spin.value() for key, spin in self.score_weight_spins.items()}
        if sum(weights.values()) != 100:
            QMessageBox.warning(self, "评分配置错误", "六项评分权重之和必须等于 100。")
            return
        if not (self.score_observe_spin.value() < self.score_grade_b_spin.value() < self.score_grade_a_spin.value()):
            QMessageBox.warning(self, "评分配置错误", "评分阈值必须满足：观察 < B级 < A级。")
            return
        if self.kernel_reduce_losses_spin.value() >= self.kernel_stop_losses_spin.value():
            QMessageBox.warning(self, "风控配置错误", "连续亏损降仓次数必须小于停止开仓次数。")
            return
        settings = load_settings()
        settings.update({
            "quote_asset": self.quote_asset_input.text().strip().upper() or "USDT",
            "exclude_symbols": [
                item.strip().upper()
                for item in self.exclude_symbols_input.text().split(",")
                if item.strip()
            ],
            "min_quote_volume": self.min_quote_volume_spin.value(),
            "default_equity": self.default_equity_spin.value(),
            "default_risk_pct": self.default_risk_spin.value(),
            "default_leverage": self.default_leverage_spin.value(),
            "daily_loss_stop_pct": self.daily_loss_stop_spin.value(),
            "daily_loss_warning_pct": max(0.1, self.daily_loss_stop_spin.value() * 0.75),
            "intraday_atr_multiplier": self.intraday_atr_multiplier_spin.value(),
            "swing_atr_multiplier": self.swing_atr_multiplier_spin.value(),
            "min_live_score": self.min_live_score_spin.value(),
            "scan_limit": self.settings_scan_limit_spin.value(),
        })
        score_config = settings.setdefault("signal_score", {})
        score_config["weights"] = weights
        thresholds = score_config.setdefault("thresholds", {})
        thresholds.update(
            {
                "grade_a": self.score_grade_a_spin.value(),
                "grade_b": self.score_grade_b_spin.value(),
                "observe": self.score_observe_spin.value(),
            }
        )
        settings["system_risk"] = {
            "max_single_risk_pct": self.kernel_single_risk_spin.value(),
            "max_daily_loss_pct": self.daily_loss_stop_spin.value(),
            "max_total_exposure_multiple": self.kernel_total_exposure_spin.value(),
            "max_symbol_exposure_pct": self.kernel_symbol_exposure_spin.value(),
            "max_leverage": self.kernel_max_leverage_spin.value(),
            "reduce_after_consecutive_losses": self.kernel_reduce_losses_spin.value(),
            "stop_after_consecutive_losses": self.kernel_stop_losses_spin.value(),
        }
        settings["order_manager"] = {
            "partial_fill_policy": self.partial_fill_policy_combo.currentData(),
            "partial_fill_timeout_seconds": self.partial_fill_timeout_spin.value(),
            "auto_place_protective_orders": self.auto_protective_orders_checkbox.isChecked(),
        }
        CONFIG_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
        self.trading_runtime.reload_settings(settings)
        self.append_log(f"设置已保存：{CONFIG_PATH}")
        self.load_settings_into_form()


