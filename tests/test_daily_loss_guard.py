from __future__ import annotations

from trade_assistant.portfolio import SimulatedPortfolio
from trade_assistant.portfolio import futures_today_realized_pnl


def test_simulated_portfolio_tracks_today_realized_pnl(tmp_path) -> None:
    portfolio = SimulatedPortfolio(tmp_path / "sim.db")
    portfolio.apply_fill("spot", "UNIUSDT", "BUY", quantity=10, price=3.0)
    portfolio.apply_fill("spot", "UNIUSDT", "SELL", quantity=4, price=2.5)

    assert portfolio.today_realized_pnl() == -2.0


def test_futures_today_realized_pnl_sums_realized_commission_and_funding(monkeypatch) -> None:
    class FakeClient:
        def futures_income_history(self, start_time, end_time):
            return [
                {"incomeType": "REALIZED_PNL", "income": "-12"},
                {"incomeType": "COMMISSION", "income": "-1.5"},
                {"incomeType": "FUNDING_FEE", "income": "0.5"},
                {"incomeType": "TRANSFER", "income": "100"},
            ]

    pnl = futures_today_realized_pnl(FakeClient())

    assert pnl == -13.0
