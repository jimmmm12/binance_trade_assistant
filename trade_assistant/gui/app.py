from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from trade_assistant.gui.main_window import MainWindow
from trade_assistant.gui.styles import APP_STYLE
from trade_assistant.main import ROOT, bundled_root


APP_USER_MODEL_ID = "BinanceTradeAssistant.Desktop"


def app_icon_path() -> Path:
    bundled = bundled_root() / "assets" / "app_icon.ico"
    if bundled.exists():
        return bundled
    return ROOT / "assets" / "app_icon.ico"


def set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        return


def main() -> int:
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("Binance Trade Assistant")
    icon_path = app_icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
