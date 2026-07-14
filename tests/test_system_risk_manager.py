from __future__ import annotations

from trade_assistant.risk import create_trade_plan
from trade_assistant.trading_system.risk.manager import RiskContext, RiskLimits, RiskManager
from trade_assistant.trading_system.state.manager import StateManager
from trade_assistant.trading_system.storage.database import TradingDatabase


def _manager(tmp_path, **limits) -> RiskManager:
    store = TradingDatabase(tmp_path / "risk.db")
    state = StateManager(store)
    return RiskManager(RiskLimits(**limits), store, state)


def _plan():
    return create_trade_plan("UNIUSDT", "futures", "long", 10, 9.5, 11, 1000, 1, 2)


def test_risk_manager_blocks_opening_when_state_is_stale(tmp_path) -> None:
    manager = _manager(tmp_path)
    context = RiskContext(equity=1000, account_state_fresh=False)

    decision = manager.authorize_plan(_plan(), None, context)

    assert decision.allowed is False
    assert decision.code == "state_stale"


def test_risk_manager_reduces_size_after_loss_streak(tmp_path) -> None:
    manager = _manager(tmp_path, reduce_after_consecutive_losses=3, stop_after_consecutive_losses=5)
    context = RiskContext(equity=1000, consecutive_losses=3)

    decision = manager.authorize_plan(_plan(), None, context)

    assert decision.allowed is True
    assert decision.quantity_multiplier == 0.5


def test_aggressive_risk_manager_uses_recovery_reduction_before_its_higher_stop(tmp_path) -> None:
    manager = _manager(
        tmp_path,
        aggressive_reduce_after_consecutive_losses=3,
        aggressive_stop_after_consecutive_losses=8,
        aggressive_loss_streak_reduction_multiplier=0.35,
    )
    context = RiskContext(equity=1000, consecutive_losses=5, risk_line="aggressive")

    decision = manager.authorize_plan(_plan(), None, context)

    assert decision.allowed is True
    assert decision.quantity_multiplier == 0.35


def test_risk_manager_blocks_daily_loss_but_allows_reduce(tmp_path) -> None:
    manager = _manager(tmp_path, max_daily_loss_pct=2.0)
    context = RiskContext(equity=1000, today_pnl=-25)

    opening = manager.authorize_plan(_plan(), None, context)
    reducing = manager.authorize_reduce(context)

    assert opening.allowed is False
    assert opening.code == "daily_loss_limit"
    assert reducing.allowed is True


def test_risk_manager_checks_single_risk_against_total_account_equity(tmp_path) -> None:
    manager = _manager(tmp_path, max_single_risk_pct=1.0)
    plan = create_trade_plan("UNIUSDT", "futures", "long", 10, 9.72, 11, 14.4, 1.011111, 2)
    context = RiskContext(equity=18)

    decision = manager.authorize_plan(plan, None, context)

    assert decision.allowed is True


def test_risk_manager_allows_tiny_rounding_difference_at_risk_boundary(tmp_path) -> None:
    manager = _manager(tmp_path, aggressive_max_single_risk_pct=2.5)
    context = RiskContext(equity=1000, risk_line="aggressive")
    within_rounding = create_trade_plan("UNIUSDT", "futures", "long", 10, 9.5, 11, 1000, 2.504, 2)
    above_limit = create_trade_plan("UNIUSDT", "futures", "long", 10, 9.5, 11, 1000, 2.506, 2)

    assert manager.authorize_plan(within_rounding, None, context).allowed is True
    rejected = manager.authorize_plan(above_limit, None, context)
    assert rejected.allowed is False
    assert rejected.code == "single_risk_limit"


def test_risk_manager_blocks_uncertain_order(tmp_path) -> None:
    manager = _manager(tmp_path)

    decision = manager.authorize_plan(_plan(), None, RiskContext(equity=1000, uncertain_order_count=1))

    assert decision.allowed is False
    assert decision.code == "order_uncertain"


def test_risk_manager_blocks_when_daily_pnl_data_is_unavailable(tmp_path) -> None:
    manager = _manager(tmp_path)

    decision = manager.authorize_plan(
        _plan(), None, RiskContext(equity=1000, risk_data_healthy=False)
    )

    assert decision.allowed is False
    assert decision.code == "risk_data_unavailable"
