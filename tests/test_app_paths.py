from __future__ import annotations

import json
from pathlib import Path

from trade_assistant import main as app_main
from trade_assistant.gui.app import app_icon_path


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


def test_app_icon_exists_for_window_and_taskbar():
    path = app_icon_path()

    assert path.name == "app_icon.ico"
    assert path.exists()


def test_ensure_config_exists_copies_bundled_default(tmp_path, monkeypatch):
    app_config = tmp_path / "app" / "config" / "settings.json"
    bundled_config = tmp_path / "_internal" / "config" / "settings.json"
    bundled_config.parent.mkdir(parents=True)
    bundled_config.write_text(json.dumps({"quote_asset": "USDT"}), encoding="utf-8")
    monkeypatch.setattr(app_main, "CONFIG_PATH", app_config)

    app_main.ensure_config_exists(default_config_path=bundled_config)

    assert app_config.exists()
    assert json.loads(app_config.read_text(encoding="utf-8")) == {"quote_asset": "USDT"}


def test_load_settings_adds_new_nested_defaults_without_losing_existing_values(tmp_path, monkeypatch):
    app_config = tmp_path / "config" / "settings.json"
    app_config.parent.mkdir(parents=True)
    app_config.write_text(json.dumps({"default_equity": 2500, "signal_score": {"weights": {"trend": 35}}}), encoding="utf-8")
    monkeypatch.setattr(app_main, "CONFIG_PATH", app_config)

    settings = app_main.load_settings()

    assert settings["default_equity"] == 2500
    assert settings["signal_score"]["weights"]["trend"] == 35
    assert settings["signal_score"]["weights"]["momentum"] == 20
    assert settings["signal_score"]["thresholds"]["grade_a"] == 90


def test_load_settings_retries_transient_empty_file(tmp_path, monkeypatch):
    app_config = tmp_path / "config" / "settings.json"
    app_config.parent.mkdir(parents=True)
    app_config.write_text(json.dumps({"default_equity": 1800}), encoding="utf-8")
    monkeypatch.setattr(app_main, "CONFIG_PATH", app_config)
    original_read = Path.read_text
    calls = {"count": 0}

    def transient_empty(path, *args, **kwargs):
        if path == app_config and calls["count"] == 0:
            calls["count"] += 1
            return ""
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient_empty)

    assert app_main.load_settings()["default_equity"] == 1800
