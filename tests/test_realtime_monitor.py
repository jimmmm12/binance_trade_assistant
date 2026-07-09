from __future__ import annotations

from trade_assistant.realtime_monitor import MonitorTarget, evaluate_monitor_target


def test_monitor_long_target_reports_r_milestones_and_target() -> None:
    target = MonitorTarget(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry=10,
        stop=9,
        target=12,
    )

    result = evaluate_monitor_target(target, price=11.5)

    assert result.r_multiple == 1.5
    assert result.unrealized_pnl == 15
    assert "1R" in result.alert_text
    assert "1.5R" in result.alert_text
    assert "减仓" in result.alert_text


def test_monitor_short_target_reports_stop_loss() -> None:
    target = MonitorTarget(
        market="futures",
        symbol="UNIUSDT",
        side="short",
        quantity=10,
        entry=10,
        stop=10.5,
        target=9,
    )

    result = evaluate_monitor_target(target, price=10.6)

    assert result.r_multiple < 0
    assert "触发止损" in result.alert_text
    assert result.severity == "danger"


def test_monitor_warns_when_price_is_close_to_liquidation() -> None:
    target = MonitorTarget(
        market="futures",
        symbol="UNIUSDT",
        side="long",
        quantity=10,
        entry=10,
        stop=9,
        target=12,
        liquidation_price=9.7,
    )

    result = evaluate_monitor_target(target, price=10)

    assert "接近强平" in result.alert_text
    assert result.severity == "danger"
