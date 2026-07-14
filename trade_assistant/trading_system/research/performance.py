from __future__ import annotations

from dataclasses import asdict, dataclass

from ..storage.database import TradingDatabase


@dataclass(frozen=True)
class PerformanceReport:
    trades: int
    open_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    average_pnl: float
    profit_factor: float | None
    max_drawdown: float

    def to_dict(self) -> dict:
        return asdict(self)


class PerformanceAnalyzer:
    def __init__(self, store: TradingDatabase) -> None:
        self.store = store

    def analyze(self) -> PerformanceReport:
        pnls = self.store.closed_trade_pnls()
        open_trades = self.store.open_trade_count()
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        return PerformanceReport(
            trades=len(pnls),
            open_trades=open_trades,
            wins=len(wins),
            losses=len(losses),
            win_rate=round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
            total_pnl=round(sum(pnls), 8),
            average_pnl=round(sum(pnls) / len(pnls), 8) if pnls else 0.0,
            profit_factor=round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
            max_drawdown=round(max_drawdown, 8),
        )
