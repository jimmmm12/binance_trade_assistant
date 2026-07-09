from __future__ import annotations

import json
from pathlib import Path

from trade_assistant import main as app_main


def test_resolve_root_source_uses_project_root():
    root = app_main.resolve_root(
        frozen=False,
        executable=Path("D:/ignored/BinanceTradeAssistant.exe"),
        file_path=Path("D:/document/work/binance_trade_assistant/trade_assistant/main.py"),
    )

    assert root == Path("D:/document/work/binance_trade_assistant")


def test_resolve_root_frozen_uses_executable_parent():
    root = app_main.resolve_root(
        frozen=True,
        executable=Path("D:/document/work/binance_trade_assistant/dist/BinanceTradeAssistant/BinanceTradeAssistant.exe"),
        file_path=Path("D:/ignored/_internal/trade_assistant/main.py"),
    )

    assert root == Path("D:/document/work/binance_trade_assistant/dist/BinanceTradeAssistant")


def test_ensure_config_exists_copies_bundled_default(tmp_path, monkeypatch):
    app_config = tmp_path / "app" / "config" / "settings.json"
    bundled_config = tmp_path / "_internal" / "config" / "settings.json"
    bundled_config.parent.mkdir(parents=True)
    bundled_config.write_text(json.dumps({"quote_asset": "USDT"}), encoding="utf-8")
    monkeypatch.setattr(app_main, "CONFIG_PATH", app_config)

    app_main.ensure_config_exists(default_config_path=bundled_config)

    assert app_config.exists()
    assert json.loads(app_config.read_text(encoding="utf-8")) == {"quote_asset": "USDT"}
