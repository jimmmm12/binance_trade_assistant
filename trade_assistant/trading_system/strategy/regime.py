from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ...models import Signal


class MarketRegime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class RegimeResult:
    regime: MarketRegime
    confidence: float
    reasons: list[str]


def detect_regime(signal: Signal, mode: str) -> RegimeResult:
    atr = signal.atr_4h_pct if mode == "swing" and signal.atr_4h_pct is not None else signal.atr_1h_pct
    atr = atr if atr is not None else signal.atr_pct
    adx = signal.adx_4h if mode == "swing" else signal.adx_1h
    fast = signal.ema50_4h if mode == "swing" else signal.ema20_1h
    slow = signal.ema200_4h if mode == "swing" else signal.ema50_1h
    extreme_atr = 14.0 if mode == "swing" else 6.0

    if atr is not None and atr >= extreme_atr:
        return RegimeResult(MarketRegime.NO_TRADE, 0.95, [f"ATR {atr:.2f}% 超过极端波动限制"])
    if signal.atr_percentile is not None and signal.atr_percentile >= 95:
        return RegimeResult(
            MarketRegime.HIGH_VOLATILITY,
            min(1.0, signal.atr_percentile / 100),
            [f"ATR处于历史 {signal.atr_percentile:.0f}% 分位"],
        )
    if adx is not None and adx < 18:
        return RegimeResult(MarketRegime.RANGE, min(1.0, (25 - adx) / 15), [f"ADX {adx:.1f}，趋势不足"])
    if fast is not None and slow is not None:
        confidence = min(1.0, 0.55 + max(0.0, (adx or 20) - 20) / 40)
        if fast > slow:
            return RegimeResult(MarketRegime.TREND_UP, confidence, ["EMA结构向上", f"ADX {adx or 0:.1f}"])
        if fast < slow:
            return RegimeResult(MarketRegime.TREND_DOWN, confidence, ["EMA结构向下", f"ADX {adx or 0:.1f}"])
    return RegimeResult(MarketRegime.RANGE, 0.4, ["趋势数据不足，按震荡环境处理"])
