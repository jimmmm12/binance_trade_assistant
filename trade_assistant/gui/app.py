from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from trade_assistant.gui.main_window import MainWindow
from trade_assistant.gui.styles import APP_STYLE


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Binance Trade Assistant")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
