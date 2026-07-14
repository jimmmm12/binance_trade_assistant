from __future__ import annotations


APP_STYLE = """
QMainWindow {
    background: #eef3f5;
}
QWidget {
    font-family: "Microsoft YaHei", "Segoe UI", Arial;
    font-size: 13px;
    color: #17202a;
}
QFrame#Sidebar {
    background: #1e2930;
    border: 0;
}
QLabel#Brand {
    color: #ffffff;
    font-size: 18px;
    font-weight: 700;
}
QPushButton#NavButton {
    background: transparent;
    color: #dbe7eb;
    text-align: left;
    border: 0;
    padding: 10px 12px;
    border-radius: 5px;
}
QPushButton#NavButton:checked {
    background: #0f9f8f;
    color: #ffffff;
}
QFrame#TopBar,
QFrame#Panel,
QGroupBox {
    background: #ffffff;
    border: 1px solid #dce5ea;
    border-radius: 8px;
}
QGroupBox {
    margin-top: 16px;
    padding: 12px;
    font-weight: 700;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
}
QLineEdit,
QComboBox,
QSpinBox,
QDoubleSpinBox,
QTextEdit {
    background: #ffffff;
    border: 1px solid #cfd9df;
    border-radius: 5px;
    padding: 6px;
}
QTableWidget {
    background: #ffffff;
    border: 1px solid #dce5ea;
    gridline-color: #edf2f4;
    selection-background-color: #d9f2ef;
    selection-color: #17202a;
}
QHeaderView::section {
    background: #f4f7f8;
    color: #65727c;
    border: 0;
    border-bottom: 1px solid #dce5ea;
    padding: 7px;
    font-weight: 700;
}
QPushButton {
    background: #f7fafb;
    border: 1px solid #cfd9df;
    border-radius: 5px;
    padding: 8px 12px;
}
QPushButton:hover {
    background: #edf5f6;
}
QPushButton#PrimaryButton {
    background: #0f9f8f;
    color: #ffffff;
    border-color: #0f9f8f;
    font-weight: 700;
}
QPushButton#PositionModeButton {
    background: #f7fafb;
    color: #42515a;
    border: 1px solid #b8c8cf;
    font-weight: 700;
}
QPushButton#PositionModeButton:hover {
    background: #edf5f6;
}
QPushButton#PositionModeButton:checked {
    background: #1e2930;
    color: #ffffff;
    border-color: #1e2930;
}
QPushButton#DangerButton {
    background: #fff4f1;
    color: #b83222;
    border-color: #f3c8bd;
    font-weight: 700;
}
QLabel#StatusGood {
    color: #078f6b;
    font-weight: 700;
}
QLabel#StatusBad {
    color: #b83222;
    font-weight: 700;
}
"""
