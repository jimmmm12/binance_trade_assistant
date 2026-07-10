from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
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
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QAbstractItemView,
    QVBoxLayout,
    QWidget,
)

from trade_assistant.auto_trader import AutoTradeConfig, AutoTradeDecision, run_auto_cycle
from trade_assistant.binance_client import MarketDataUnavailable
from trade_assistant.broker import LIVE_CONFIRMATION
from trade_assistant.main import CONFIG_PATH, ROOT, load_settings
from trade_assistant.market_stream import BinanceWebSocketPriceCache
from trade_assistant.models import ScoredSignal, Signal, TradePlan
from trade_assistant.order_brackets import build_exit_order_drafts
from trade_assistant.portfolio import SimulatedPortfolio, read_futures_account_risk
from trade_assistant.realtime_monitor import MonitorResult, MonitorTarget, evaluate_monitor_target
from trade_assistant.report import trade_plan_to_markdown
from trade_assistant.risk_engine import PlanRiskReview, account_read_failed_guard, daily_loss_guard
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


SIGNAL_COLUMNS = [
    "市场",
    "交易对",
    "方向",
    "分数",
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
        self.thread_pool = QThreadPool.globalInstance()
        self.current_plan: TradePlan | None = None
        self.current_signal: ScoredSignal | None = None
        self.current_risk_review: PlanRiskReview | None = None
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
        self.last_monitor_alerts: dict[str, str] = {}
        self.price_stream = BinanceWebSocketPriceCache(stale_after_seconds=5)
        self.latest_markdown_path: Path | None = None
        self.latest_csv_path: Path | None = None
        self._build_ui()
        self.refresh_status()
        self.load_settings_into_form()

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
        self.sim_positions_button.setObjectName("PrimaryButton")
        self.sim_positions_button.clicked.connect(lambda: self.set_position_source_mode("simulated"))
        self.real_positions_button = QPushButton("真实仓")
        self.real_positions_button.setCheckable(True)
        self.real_positions_button.clicked.connect(lambda: self.set_position_source_mode("real"))
        refresh_button = QPushButton("刷新仓位")
        refresh_button.setObjectName("PrimaryButton")
        refresh_button.clicked.connect(self.refresh_positions_page)
        controls.addWidget(self.sim_positions_button)
        controls.addWidget(self.real_positions_button)
        controls.addWidget(refresh_button)
        controls.addStretch(1)
        layout.addLayout(controls)
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
        for key, width in [
            ("交易对", 110),
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
        layout = QVBoxLayout(page)
        controls_group = QGroupBox("自动交易控制台")
        controls = QFormLayout(controls_group)

        self.auto_market_combo = QComboBox()
        self._add_combo_choices(self.auto_market_combo, MARKET_CHOICES)
        self._set_combo_data(self.auto_market_combo, "futures")
        self.auto_mode_combo = QComboBox()
        self._add_combo_choices(self.auto_mode_combo, STRATEGY_MODE_CHOICES)
        self._set_combo_data(self.auto_mode_combo, "intraday")
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
        self.auto_simulate_checkbox = QCheckBox("自动模拟下单")
        self.auto_simulate_checkbox.setChecked(True)

        controls.addRow("扫描市场", self.auto_market_combo)
        controls.addRow("策略模式", self.auto_mode_combo)
        controls.addRow("候选数量", self.auto_top_spin)
        controls.addRow("循环间隔", self.auto_interval_spin)
        controls.addRow("本轮本金", self.auto_equity_spin)
        controls.addRow("执行方式", self.auto_simulate_checkbox)
        layout.addWidget(controls_group)

        buttons = QHBoxLayout()
        self.auto_start_button = QPushButton("启动自动")
        self.auto_start_button.setObjectName("PrimaryButton")
        self.auto_start_button.clicked.connect(self.start_auto_trading)
        self.auto_stop_button = QPushButton("停止")
        self.auto_stop_button.clicked.connect(lambda: self.stop_auto_trading("自动交易已停止"))
        self.auto_stop_button.setEnabled(False)
        self.auto_emergency_button = QPushButton("急停")
        self.auto_emergency_button.setObjectName("DangerButton")
        self.auto_emergency_button.clicked.connect(lambda: self.stop_auto_trading("急停已触发：自动循环已停止"))
        buttons.addWidget(self.auto_start_button)
        buttons.addWidget(self.auto_stop_button)
        buttons.addWidget(self.auto_emergency_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.auto_status_label = QLabel("自动交易未启动：当前版本不会自动真下单，只会自动计划和可选本地模拟下单。")
        self.auto_status_label.setWordWrap(True)
        layout.addWidget(self.auto_status_label)
        self.auto_state_label = QLabel("状态机：空仓观察")
        self.auto_state_label.setWordWrap(True)
        layout.addWidget(self.auto_state_label)

        self.auto_log_table = QTableWidget(0, 5)
        self.auto_log_table.setHorizontalHeaderLabels(["时间", "状态", "交易对", "动作", "说明"])
        self.auto_log_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.auto_log_table.setColumnWidth(0, 150)
        self.auto_log_table.setColumnWidth(1, 110)
        self.auto_log_table.setColumnWidth(2, 120)
        self.auto_log_table.setColumnWidth(3, 120)
        self.auto_log_table.setColumnWidth(4, 420)
        self.auto_log_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.auto_log_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.auto_log_table, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
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
        self.auto_trading_enabled = True
        self.auto_start_button.setEnabled(False)
        self.auto_stop_button.setEnabled(True)
        interval_ms = self.auto_interval_spin.value() * 60 * 1000
        self.auto_timer.start(interval_ms)
        self.auto_status_label.setText(
            f"自动交易已启动：每 {self.auto_interval_spin.value()} 分钟运行一轮，"
            "不会自动真下单。"
        )
        self.append_log("自动交易已启动。")
        self.run_auto_trade_cycle()

    def stop_auto_trading(self, reason: str = "自动交易已停止") -> None:
        self.auto_trading_enabled = False
        self.auto_timer.stop()
        self.auto_start_button.setEnabled(True)
        self.auto_stop_button.setEnabled(False)
        self.auto_status_label.setText(reason)
        self.append_log(reason)

    def run_auto_trade_cycle(self) -> None:
        if not self.auto_trading_enabled:
            return
        if self.auto_cycle_running:
            self._append_auto_log("跳过", "", "等待", "上一轮还没结束，本轮跳过。")
            return
        self.auto_cycle_running = True
        self.auto_status_label.setText("自动交易运行中：正在扫描并生成计划。")
        settings = load_settings()
        config = AutoTradeConfig(
            market=self.auto_market_combo.currentData(),
            mode=self.auto_mode_combo.currentData(),
            top=self.auto_top_spin.value(),
            auto_simulate=self.auto_simulate_checkbox.isChecked(),
            equity=self.auto_equity_spin.value(),
            max_daily_loss_pct=float(settings.get("daily_loss_stop_pct", 2.0)),
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
            decision = run_auto_cycle(config, scan_fn=scan_for_auto)
        except MarketDataUnavailable as exc:
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
        self.public_status_label.setText("公共行情：正常")
        self.latest_markdown_path = scan_result.markdown_path
        self.latest_csv_path = scan_result.csv_path
        self.fill_signal_table(self.long_table, scan_result.longs)
        self.fill_signal_table(self.short_table, scan_result.shorts)
        self.last_scan_label.setText("最近扫描：自动完成")

        symbol = decision.signal.symbol if decision.signal is not None else ""
        self._append_auto_log(decision.action, symbol, "自动循环", decision.message)
        self.auto_state_label.setText(f"状态机：{decision.state_path}")
        self.auto_status_label.setText(decision.message)
        self.append_log(f"自动交易：{decision.message}")
        if decision.plan is not None:
            self.current_plan = decision.plan
            self.current_signal = decision.signal
            self.current_risk_review = decision.review
            self._apply_plan_to_forms(decision.plan)
            self._save_plan_position_record(decision.plan, "自动计划")
            save_trade_plan(decision.plan)
            preview = trade_plan_to_markdown(decision.plan)
            if decision.review is not None:
                preview += "\n\n" + self._risk_review_text(decision.review)
            self.plan_preview.setPlainText(preview)
        if decision.position is not None and decision.plan is not None:
            self.position_status_box.setPlainText("自动模拟下单：\n" + self._position_text(decision.position))
            self._save_plan_position_record(decision.plan, "自动模拟持仓")
        if decision.plan is not None or decision.position is not None:
            self.refresh_positions_page()

    def auto_trade_cycle_error(self, text: str) -> None:
        self.auto_cycle_running = False
        self._append_auto_log("错误", "", "自动循环", text[-500:])
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
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            self.auto_log_table.setItem(row, column, item)
        self.auto_log_table.scrollToBottom()

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
        self._save_plan_position_record(plan, "计划中")
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
        order_side = "BUY" if plan.side == "long" else "SELL"
        self._set_combo_data(self.order_side_combo, order_side)
        self._set_combo_data(self.order_type_combo, "LIMIT")
        self.quantity_input.setText(f"{plan.quantity:.8f}")
        self.price_input.setText(f"{plan.entry:.8f}")
        self.live_confirm_input.clear()

    def submit_order(self, live: bool) -> None:
        self.refresh_status()
        if live:
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
            result = order_from_form(
                self.market_combo.currentData(),
                self.symbol_input.text(),
                self.order_side_combo.currentData(),
                self.quantity_input.text(),
                self.order_type_combo.currentData(),
                self.price_input.text(),
                allow_live=live,
                confirm=self.live_confirm_input.text(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "下单错误", str(exc))
            self.append_log(f"下单错误：{exc}")
            return
        self.append_log(json.dumps(result, indent=2, ensure_ascii=False))
        if live and not result.get("dry_run", False):
            self.set_position_source_mode("real")
            if not self.realtime_monitor_checkbox.isChecked():
                self.realtime_monitor_checkbox.setChecked(True)
            self.append_log("真下单已发送：已切换真实仓，并按 1 秒刷新真实仓位/API。")

    def submit_simulated_order(self) -> None:
        try:
            result, position = simulate_order_from_form(
                self.market_combo.currentData(),
                self.symbol_input.text(),
                self.order_side_combo.currentData(),
                self.quantity_input.text(),
                self.order_type_combo.currentData(),
                self.price_input.text(),
                self.entry_input.text(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "模拟下单错误", str(exc))
            self.append_log(f"模拟下单错误：{exc}")
            return
        text = "模拟下单已写入本地模拟仓：\n" + json.dumps(result, indent=2, ensure_ascii=False)
        text += "\n" + self._position_text(position)
        self.position_status_box.setPlainText(text)
        if self.current_plan is not None:
            self._save_plan_position_record(self.current_plan, "模拟持仓")
            self.refresh_positions_page()
        self.append_log(text)

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
        if real and real.side != "flat":
            self._save_detected_position_record(real)
            self.refresh_positions_page()
        self.append_log(text)

    def _position_text(self, position) -> str:
        if position.side == "flat":
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
            realized_pnl=0.0,
            status=status,
        )

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
            realized_pnl=position.realized_pnl,
            status="真实持仓" if position.source == "real" else "模拟持仓",
        )

    def set_position_source_mode(self, mode: str) -> None:
        self.position_source_mode = mode
        self.sim_positions_button.setChecked(mode == "simulated")
        self.real_positions_button.setChecked(mode == "real")
        self.refresh_positions_page()

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
        from trade_assistant.binance_client import BinanceClient

        client = BinanceClient()
        prices: dict[tuple[str, str], float] = {}
        results: list[MonitorResult] = []
        for target in targets:
            key = (target.market, target.symbol)
            if key not in prices:
                prices[key] = self.price_stream.latest_price(target.market, target.symbol) or client.latest_price(
                    target.market, target.symbol
                )
            results.append(evaluate_monitor_target(target, prices[key]))
        return results

    def realtime_monitor_cycle_finished(self, results: list[MonitorResult]) -> None:
        self.monitor_cycle_running = False
        self.monitor_table.setRowCount(0)
        for result in results:
            self._update_simulated_mark_price_from_monitor(result)
            row = self.monitor_table.rowCount()
            self.monitor_table.insertRow(row)
            values = [
                result.target.symbol,
                "做空" if result.target.side == "short" else "做多",
                f"{result.price:.8f}",
                f"{result.r_multiple:.2f}R",
                f"{result.unrealized_pnl:.2f}",
                result.alert_text,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.monitor_table.setItem(row, column, item)
            alert_key = f"{result.target.market}:{result.target.symbol}:{result.target.side}"
            if result.alerts and self.last_monitor_alerts.get(alert_key) != result.alert_text:
                self.append_log(f"实时监控 {result.target.symbol}：{result.alert_text}")
                self.last_monitor_alerts[alert_key] = result.alert_text
        self.monitor_status_label.setText(
            f"实时监控已更新：{len(results)} 个目标，{QDateTime.currentDateTime().toString('HH:mm:ss')}，"
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
        self.monitor_status_label.setText(f"实时监控行情读取失败：{text[-180:]}")
        self.append_log(f"实时监控行情读取失败：{text[-500:]}")

    def _monitor_targets(self) -> list[MonitorTarget]:
        targets: list[MonitorTarget] = []
        if self.current_plan is not None:
            targets.append(self._target_from_plan(self.current_plan))
        form_target = self._target_from_form()
        if form_target is not None:
            targets.append(form_target)
        for record in SimulatedPortfolio().position_records():
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
        super().closeEvent(event)

    def _dedupe_monitor_targets(self, targets: list[MonitorTarget]) -> list[MonitorTarget]:
        deduped: dict[tuple[str, str, str, float, float, float], MonitorTarget] = {}
        for target in targets:
            key = (target.market, target.symbol, target.side, target.entry, target.stop, target.target)
            deduped[key] = target
        return list(deduped.values())

    def refresh_positions_page(self) -> None:
        rows = self._real_position_rows() if self.position_source_mode == "real" else SimulatedPortfolio().position_records()
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

    def _real_position_rows(self) -> list[dict]:
        if not live_trading_status().has_api_key or not live_trading_status().has_api_secret:
            self.append_log("真实仓需要配置 Binance API Key / Secret。")
            return []
        try:
            from trade_assistant.binance_client import BinanceClient

            risk = read_futures_account_risk(BinanceClient())
        except Exception as exc:
            self.append_log(f"真实仓读取失败：{exc}")
            return []
        rows: list[dict] = []
        for position in risk.positions:
            rows.append(
                {
                    "source": "real",
                    "market": position.market,
                    "symbol": position.symbol,
                    "side": position.side,
                    "quantity": position.quantity,
                    "entry_price": position.entry_price,
                    "mark_price": position.mark_price,
                    "stop_price": 0.0,
                    "target_price": 0.0,
                    "realized_pnl": position.realized_pnl,
                    "unrealized_pnl": position.unrealized_pnl,
                    "status": "真实持仓",
                    "updated_at": position.updated_at,
                }
            )
        return rows

    def _risk_review_text(self, review: PlanRiskReview) -> str:
        lines = [
            "风控评审：",
            f"- 计划质量评分：{review.quality_score}/100，建议：{review.recommended_action}",
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
        self.append_log(text)
        QMessageBox.warning(self, "后台任务失败", text[-1200:])

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
        self.min_live_score_spin.setValue(int(settings.get("min_live_score", 70)))
        self.settings_scan_limit_spin.setValue(int(settings["scan_limit"]))
        self.scan_top_spin.setValue(int(settings["scan_limit"]))
        self.equity_input.setText(str(settings["default_equity"]))
        self.risk_input.setText(str(settings["default_risk_pct"]))
        self.leverage_input.setText(str(settings["default_leverage"]))
        self._load_api_credentials_into_form()

    def save_settings_from_form(self) -> None:
        settings = {
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
        }
        CONFIG_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
        self.append_log(f"设置已保存：{CONFIG_PATH}")
        self.load_settings_into_form()


