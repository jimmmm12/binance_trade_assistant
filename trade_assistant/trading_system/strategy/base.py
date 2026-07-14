from __future__ import annotations

from typing import Any, Protocol

from .regime import MarketRegime


class StrategyPlugin(Protocol):
    name: str
    supported_regimes: set[MarketRegime]

    def generate_signal(self, market_event: dict[str, Any], trading_state: dict[str, Any]) -> Any:
        ...


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, StrategyPlugin] = {}

    def register(self, strategy: StrategyPlugin) -> None:
        if strategy.name in self._strategies:
            raise ValueError(f"strategy already registered: {strategy.name}")
        self._strategies[strategy.name] = strategy

    def get(self, name: str) -> StrategyPlugin:
        return self._strategies[name]

    def for_regime(self, regime: MarketRegime) -> list[StrategyPlugin]:
        return [strategy for strategy in self._strategies.values() if regime in strategy.supported_regimes]

    def names(self) -> list[str]:
        return sorted(self._strategies)
