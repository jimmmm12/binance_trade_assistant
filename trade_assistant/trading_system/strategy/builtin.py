from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...models import ScoredSignal
from .regime import MarketRegime


@dataclass(frozen=True)
class StrategySignal:
    strategy: str
    signal: ScoredSignal
    reason: str


class TrendFollowingStrategy:
    name = "trend_following"
    supported_regimes = {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}

    def generate_signal(self, market_event: dict[str, Any], trading_state: dict[str, Any]) -> StrategySignal | None:
        signal = _scored_signal(market_event)
        regime = MarketRegime(str(market_event["regime"]))
        aligned = (regime == MarketRegime.TREND_UP and signal.side == "long") or (
            regime == MarketRegime.TREND_DOWN and signal.side == "short"
        )
        if not aligned or signal.breakdown.action_level == "block_live":
            return None
        return StrategySignal(self.name, signal, "趋势方向、评分和市场状态一致")


class BreakoutStrategy:
    name = "breakout"
    supported_regimes = {
        MarketRegime.TREND_UP,
        MarketRegime.TREND_DOWN,
        MarketRegime.HIGH_VOLATILITY,
    }

    def generate_signal(self, market_event: dict[str, Any], trading_state: dict[str, Any]) -> StrategySignal | None:
        signal = _scored_signal(market_event)
        breakout = (signal.signal.breakout_atr or 0) * (1 if signal.side == "long" else -1)
        if breakout <= 0 or signal.signal.volume_ratio < 1.5:
            return None
        return StrategySignal(self.name, signal, "放量突破近期结构")


class MeanReversionStrategy:
    name = "mean_reversion"
    supported_regimes = {MarketRegime.RANGE}

    def generate_signal(self, market_event: dict[str, Any], trading_state: dict[str, Any]) -> StrategySignal | None:
        signal = _scored_signal(market_event)
        rsi = signal.signal.rsi_1h
        stretched = (signal.side == "long" and rsi <= 35) or (signal.side == "short" and rsi >= 65)
        if not stretched or signal.breakdown.action_level == "block_live":
            return None
        return StrategySignal(self.name, signal, "震荡环境中 RSI 偏离均值")


def _scored_signal(market_event: dict[str, Any]) -> ScoredSignal:
    signal = market_event.get("scored_signal")
    if not isinstance(signal, ScoredSignal):
        raise ValueError("market_event requires scored_signal")
    return signal
