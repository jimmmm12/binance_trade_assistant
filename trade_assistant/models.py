from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSnapshot:
    market: str
    symbol: str
    last: float
    change_24h: float
    quote_volume: float
    funding_pct: float | None = None


@dataclass(frozen=True)
class Signal:
    market: str
    symbol: str
    side: str
    score: int
    last: float
    change_24h: float
    quote_volume_m: float
    rsi_1h: float
    rsi_4h: float
    volume_ratio: float
    momentum_24h: float
    momentum_3d: float
    funding_pct: float | None
    note: str
    atr_pct: float | None = None
    atr_1h_pct: float | None = None
    atr_4h_pct: float | None = None


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    market: str
    side: str
    entry: float
    stop: float
    target: float
    equity: float
    risk_pct: float
    leverage: float
    risk_amount: float
    quantity: float
    notional: float
    margin_required: float
    loss_pct_to_stop: float
    gain_pct_to_target: float
    leveraged_loss_pct: float
    leveraged_gain_pct: float


@dataclass(frozen=True)
class ScoreBreakdown:
    total: int
    liquidity: int
    trend: int
    volume: int
    relative_strength: int
    risk: int
    funding: int
    reasons: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class ScoredSignal:
    signal: Signal
    mode: str
    breakdown: ScoreBreakdown

    @property
    def market(self) -> str:
        return self.signal.market

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def side(self) -> str:
        return self.signal.side

    @property
    def score(self) -> int:
        return self.breakdown.total

    @property
    def last(self) -> float:
        return self.signal.last

    @property
    def quote_volume_m(self) -> float:
        return self.signal.quote_volume_m

    @property
    def reasons(self) -> list[str]:
        return self.breakdown.reasons

    @property
    def warnings(self) -> list[str]:
        return self.breakdown.warnings


@dataclass(frozen=True)
class PositionSnapshot:
    source: str
    market: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    mark_price: float
    notional: float
    unrealized_pnl: float
    realized_pnl: float
    leverage: float
    updated_at: str
    liquidation_price: float | None = None
    margin_type: str | None = None
    isolated_margin: float | None = None


@dataclass(frozen=True)
class FuturesAccountRisk:
    wallet_balance: float
    available_balance: float
    total_unrealized_pnl: float
    positions: list[PositionSnapshot]


@dataclass(frozen=True)
class PositionAdvice:
    action: str
    summary: str
    warnings: list[str]
