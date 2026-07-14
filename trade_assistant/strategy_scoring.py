from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .models import ScoreBreakdown, ScoredSignal, Signal
from .trading_system.strategy.regime import MarketRegime, detect_regime


DEFAULT_SIGNAL_SCORE_CONFIG: dict[str, Any] = {
    "weights": {
        "trend": 30,
        "momentum": 20,
        "volume": 15,
        "position": 15,
        "timeframe": 10,
        "regime": 10,
    },
    "thresholds": {
        "grade_a": 90,
        "grade_b": 70,
        "observe": 50,
        "add": 85,
        "reduce": 60,
    },
    "position_multipliers": {
        "grade_a": 1.0,
        "grade_b": 0.6,
        "observe": 0.3,
        "blocked": 0.0,
    },
    "hard_limits": {
        "intraday_atr_pct": 6.0,
        "swing_atr_pct": 14.0,
        "directional_funding_pct": 0.12,
        "min_quote_volume_m": 20.0,
    },
}


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))


class SignalScorer:
    """Six-dimension signal quality scorer with orthogonal supporting indicators."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = _merged_config(config)
        self.weights = self.config["weights"]
        self.thresholds = self.config["thresholds"]
        self.multipliers = self.config["position_multipliers"]
        self.hard_limits = self.config["hard_limits"]

    def calculate(
        self,
        signal: Signal,
        mode: str,
        btc_momentum_24h: float = 0.0,
        eth_momentum_24h: float = 0.0,
    ) -> ScoredSignal:
        if mode not in {"intraday", "swing"}:
            raise ValueError("mode must be intraday or swing")

        reasons: list[str] = []
        warnings: list[str] = []
        ratios = {
            "trend": self.trend_score(signal, mode, reasons),
            "momentum": self.momentum_score(signal, mode, reasons, warnings),
            "volume": self.volume_score(signal, reasons, warnings),
            "position": self.position_score(signal, reasons, warnings),
            "timeframe": self.timeframe_score(signal, mode, reasons, warnings),
            "regime": self.regime_score(
                signal,
                mode,
                btc_momentum_24h,
                eth_momentum_24h,
                reasons,
                warnings,
            ),
        }
        components = {
            name: clamp(ratio * float(self.weights[name]), high=int(self.weights[name]))
            for name, ratio in ratios.items()
        }
        weight_total = sum(float(value) for value in self.weights.values()) or 100.0
        total = clamp(sum(components.values()) / weight_total * 100)
        hard_block = self._apply_hard_limits(signal, mode, warnings)
        regime_result = detect_regime(signal, mode)
        if regime_result.regime == MarketRegime.NO_TRADE:
            hard_block = True
            warnings.extend(regime_result.reasons)
        elif regime_result.regime == MarketRegime.HIGH_VOLATILITY:
            warnings.extend(regime_result.reasons)
        elif (
            (regime_result.regime == MarketRegime.TREND_UP and signal.side == "short")
            or (regime_result.regime == MarketRegime.TREND_DOWN and signal.side == "long")
        ):
            warnings.append("信号方向与识别出的市场趋势相反")
        grade, multiplier, recommendation, action_level = self._decision(total, hard_block, warnings)

        funding_quality = _funding_quality(signal)
        liquidity_quality = _liquidity_quality(signal.quote_volume_m)
        breakdown = ScoreBreakdown(
            total=total,
            liquidity=clamp(liquidity_quality * 10, high=10),
            trend=components["trend"],
            volume=components["volume"],
            relative_strength=components["timeframe"],
            risk=components["regime"],
            funding=clamp(funding_quality * 10, high=10),
            reasons=_dedupe(reasons),
            warnings=_dedupe(warnings),
            volatility=clamp(_volatility_quality(signal, mode) * 10, high=10),
            position=components["position"],
            recommendation=recommendation,
            action_level=action_level,
            momentum=components["momentum"],
            positioning=components["position"],
            timeframe=components["timeframe"],
            regime=components["regime"],
            grade=grade,
            confidence=round(total / 100, 2),
            position_multiplier=multiplier,
            add_allowed=(
                not hard_block
                and total >= int(self.thresholds["add"])
                and action_level in {"tradeable", "small_trade"}
            ),
            reduce_recommended=total < int(self.thresholds["reduce"]),
            market_regime=regime_result.regime.value,
            regime_confidence=round(regime_result.confidence, 2),
            selected_strategy=_strategy_for_regime(regime_result.regime),
        )
        return ScoredSignal(signal=signal, mode=mode, breakdown=breakdown)

    def trend_score(self, signal: Signal, mode: str, reasons: list[str]) -> float:
        direction = 1 if signal.side == "long" else -1
        if mode == "swing":
            fast = signal.ema50_4h
            slow = signal.ema200_4h
            adx_value = signal.adx_4h
            rsi_value = signal.rsi_4h
            momentum = signal.momentum_3d
            label = "4h波段"
        else:
            fast = signal.ema20_1h
            slow = signal.ema50_1h
            adx_value = signal.adx_1h
            rsi_value = signal.rsi_1h
            momentum = signal.momentum_24h
            label = "1h日内"

        score = 0.0
        aligned = _directional_relation(fast, slow, direction)
        if aligned is None:
            aligned = _directional_value(momentum, direction)
        if aligned:
            score += 0.4
            reasons.append(f"{label} EMA结构与方向一致")

        if adx_value is None:
            score += 0.15
        elif adx_value >= 30:
            score += 0.3
            reasons.append(f"ADX {adx_value:.1f}，趋势强度较高")
        elif adx_value >= 25:
            score += 0.25
        elif adx_value >= 18:
            score += 0.13

        price_reference = slow if mode == "intraday" else fast
        price_aligned = _directional_relation(signal.last, price_reference, direction)
        if price_aligned is None:
            price_aligned = rsi_value >= 50 if direction > 0 else rsi_value <= 50
        if price_aligned:
            score += 0.15

        separation = _directional_separation(fast, slow, direction)
        if separation is None:
            separation = 1.0 if _directional_value(momentum, direction) else 0.0
        if separation >= 0.01:
            score += 0.15
        elif separation > 0:
            score += 0.08
        return min(1.0, score)

    def momentum_score(
        self,
        signal: Signal,
        mode: str,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        direction = 1 if signal.side == "long" else -1
        rsi_value = signal.rsi_4h if mode == "swing" else signal.rsi_1h
        score = 0.0
        healthy = 50 <= rsi_value <= 70 if direction > 0 else 30 <= rsi_value <= 50
        stretched = rsi_value >= 78 if direction > 0 else rsi_value <= 22
        if healthy:
            score += 0.45
            reasons.append(f"RSI {rsi_value:.1f} 处于顺势健康区")
        elif stretched:
            warnings.append("RSI处于追涨追跌高风险区")
        elif (45 <= rsi_value < 78 and direction > 0) or (22 < rsi_value <= 55 and direction < 0):
            score += 0.22

        histogram = signal.macd_hist_1h
        histogram_delta = signal.macd_hist_delta_1h
        if histogram is None:
            momentum = signal.momentum_3d if mode == "swing" else signal.momentum_24h
            if _directional_value(momentum, direction):
                score += 0.3
                reasons.append("价格动量与信号方向一致")
            score += 0.12
        else:
            if _directional_value(histogram, direction):
                score += 0.3
                reasons.append("MACD柱体方向一致")
            if histogram_delta is not None and _directional_value(histogram_delta, direction):
                score += 0.25
                reasons.append("MACD动量正在扩张")
        return min(1.0, score)

    def volume_score(self, signal: Signal, reasons: list[str], warnings: list[str]) -> float:
        ratio = signal.volume_ratio
        if ratio >= 1.5:
            score = 0.67
            reasons.append(f"短期放量至均量 {ratio:.2f} 倍")
        elif ratio >= 1.2:
            score = 0.5
        elif ratio >= 1.0:
            score = 0.3
        else:
            score = 0.05
            warnings.append("成交量低于近期均量，信号延续性不足")

        direction = 1 if signal.side == "long" else -1
        taker = signal.taker_buy_ratio
        if taker is None:
            score += 0.1
        elif (direction > 0 and taker >= 0.54) or (direction < 0 and taker <= 0.46):
            score += 0.2
            reasons.append("主动成交方向支持当前信号")
        elif (direction > 0 and taker < 0.46) or (direction < 0 and taker > 0.54):
            warnings.append("主动成交方向与信号背离")

        obv_slope = signal.obv_slope_pct
        if obv_slope is None:
            score += 0.065
        elif _directional_value(obv_slope, direction):
            score += 0.13
            reasons.append("OBV资金流方向一致")
        return min(1.0, score)

    def position_score(self, signal: Signal, reasons: list[str], warnings: list[str]) -> float:
        direction = 1 if signal.side == "long" else -1
        near_structure = signal.support_distance_atr if direction > 0 else signal.resistance_distance_atr
        room = signal.resistance_distance_atr if direction > 0 else signal.support_distance_atr
        breakout = (signal.breakout_atr or 0.0) * direction

        if near_structure is None or room is None:
            return 0.55
        if breakout > 0 and signal.volume_ratio >= 1.5:
            score = 0.67
            reasons.append("放量突破近期结构，位置得到量能确认")
        elif near_structure <= 0.75:
            score = 0.67
            reasons.append("入场价靠近顺向支撑/压力结构")
        elif near_structure <= 1.5:
            score = 0.53
        elif near_structure <= 2.5:
            score = 0.27
        else:
            score = 0.07
            warnings.append("价格离保护结构较远，追单风险偏高")

        if room >= 3.0:
            score += 0.33
            reasons.append("距离反向结构有至少 3 ATR 空间")
        elif room >= 1.8:
            score += 0.22
        elif room >= 1.0:
            score += 0.1
        elif breakout <= 0:
            warnings.append("临近支撑/阻力，潜在盈利空间不足")
        return min(1.0, score)

    def timeframe_score(
        self,
        signal: Signal,
        mode: str,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        direction = 1 if signal.side == "long" else -1
        one_hour = _directional_relation(signal.ema20_1h, signal.ema50_1h, direction)
        four_hour = _directional_relation(signal.ema50_4h, signal.ema200_4h, direction)
        daily = _directional_relation(signal.ema20_1d, signal.ema50_1d, direction)
        if one_hour is None:
            one_hour = _directional_value(signal.momentum_24h, direction)
        if four_hour is None:
            four_hour = _directional_value(signal.momentum_3d, direction)
        if daily is None:
            daily = signal.rsi_4h >= 50 if direction > 0 else signal.rsi_4h <= 50

        score = (0.35 if one_hour else 0.0) + (0.35 if four_hour else 0.0) + (0.3 if daily else 0.0)
        aligned_count = sum((one_hour, four_hour, daily))
        if aligned_count == 3:
            reasons.append("1h、4h、1D方向一致")
        elif aligned_count <= 1:
            warnings.append("多周期方向冲突，逆势风险较高")
        elif mode == "swing" and not daily:
            warnings.append("日线方向未确认，波段信号降级")
        return score

    def regime_score(
        self,
        signal: Signal,
        mode: str,
        btc_momentum_24h: float,
        eth_momentum_24h: float,
        reasons: list[str],
        warnings: list[str],
    ) -> float:
        score = 0.0
        volatility = _volatility_quality(signal, mode)
        score += volatility * 0.35
        if volatility >= 0.8:
            reasons.append("ATR处于可交易波动区间")
        elif volatility <= 0.25:
            warnings.append("ATR波动环境异常")

        percentile = signal.atr_percentile
        if percentile is None:
            score += 0.075
        elif 20 <= percentile <= 85:
            score += 0.15
        elif percentile >= 95:
            warnings.append(f"ATR位于历史 {percentile:.0f}% 分位，波动突然放大")
        else:
            score += 0.05

        liquidity = _liquidity_quality(signal.quote_volume_m)
        score += liquidity * 0.2
        if liquidity >= 0.8:
            reasons.append("成交额充足，滑点风险较低")
        elif liquidity <= 0.3:
            warnings.append("流动性偏低，滑点和成交风险上升")

        funding = _funding_quality(signal)
        score += funding * 0.15
        if funding <= 0.3:
            warnings.append("资金费率与信号方向过度拥挤")

        direction = 1 if signal.side == "long" else -1
        benchmark = (btc_momentum_24h + eth_momentum_24h) / 2
        if btc_momentum_24h == 0 and eth_momentum_24h == 0:
            score += 0.075
        elif _directional_value(benchmark, direction):
            score += 0.15
            reasons.append("BTC/ETH市场环境支持当前方向")
        else:
            warnings.append("信号方向与 BTC/ETH 大盘环境相反")
        return min(1.0, score)

    def _apply_hard_limits(self, signal: Signal, mode: str, warnings: list[str]) -> bool:
        hard_block = False
        atr = signal.atr_4h_pct if mode == "swing" and signal.atr_4h_pct is not None else signal.atr_1h_pct
        atr = atr if atr is not None else signal.atr_pct
        atr_limit = float(
            self.hard_limits["swing_atr_pct"] if mode == "swing" else self.hard_limits["intraday_atr_pct"]
        )
        if atr is not None and atr >= atr_limit:
            warnings.append(f"ATR {atr:.2f}% 超过硬限制，禁止真仓")
            hard_block = True

        funding_limit = float(self.hard_limits["directional_funding_pct"])
        funding = signal.funding_pct
        crowded = funding is not None and (
            (signal.side == "long" and funding >= funding_limit)
            or (signal.side == "short" and funding <= -funding_limit)
        )
        if crowded:
            warnings.append("资金费率达到单边拥挤硬限制，禁止真仓")
            hard_block = True

        if signal.quote_volume_m < float(self.hard_limits["min_quote_volume_m"]):
            warnings.append("流动性低于硬限制，禁止真仓")
            hard_block = True
        return hard_block

    def _decision(self, score: int, hard_block: bool, warnings: list[str]) -> tuple[str, float, str, str]:
        if hard_block:
            return "禁止", 0.0, "禁止真仓，仅允许模拟观察", "block_live"
        if score >= int(self.thresholds["grade_a"]):
            multiplier = float(self.multipliers["grade_a"])
            return "A", multiplier, f"A级机会，使用基础风险的 {multiplier:.0%}", "tradeable"
        if score >= int(self.thresholds["grade_b"]):
            multiplier = float(self.multipliers["grade_b"])
            action = "simulate_only" if _has_soft_block(warnings) else "small_trade"
            text = "B级机会，但风险拥挤，只允许模拟" if action == "simulate_only" else f"B级机会，使用基础风险的 {multiplier:.0%}"
            return "B", multiplier, text, action
        if score >= int(self.thresholds["observe"]):
            multiplier = float(self.multipliers["observe"])
            return "观察", multiplier, f"观察级机会，模拟风险 {multiplier:.0%}", "simulate_only"
        return "放弃", float(self.multipliers["blocked"]), "质量不足，暂不交易", "avoid"


def score_signal(
    signal: Signal,
    mode: str,
    btc_momentum_24h: float = 0.0,
    eth_momentum_24h: float = 0.0,
    config: Mapping[str, Any] | None = None,
) -> ScoredSignal:
    return SignalScorer(config).calculate(signal, mode, btc_momentum_24h, eth_momentum_24h)


def _merged_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    result = deepcopy(DEFAULT_SIGNAL_SCORE_CONFIG)
    if not config:
        return result
    source = config.get("signal_score", config) if isinstance(config, Mapping) else {}
    for section in result:
        custom = source.get(section) if isinstance(source, Mapping) else None
        if isinstance(custom, Mapping):
            result[section].update(custom)
    weights = result["weights"]
    if any(float(value) <= 0 for value in weights.values()):
        raise ValueError("signal score weights must be positive")
    return result


def _directional_value(value: float, direction: int) -> bool:
    return value * direction > 0


def _directional_relation(first: float | None, second: float | None, direction: int) -> bool | None:
    if first is None or second is None or second == 0:
        return None
    return (first - second) * direction > 0


def _directional_separation(first: float | None, second: float | None, direction: int) -> float | None:
    if first is None or second is None or second == 0:
        return None
    return (first - second) / abs(second) * direction


def _funding_quality(signal: Signal) -> float:
    if signal.funding_pct is None or signal.market == "spot":
        return 1.0
    directional = signal.funding_pct if signal.side == "long" else -signal.funding_pct
    if directional >= 0.12:
        return 0.0
    if directional >= 0.08:
        return 0.2
    if directional >= 0.05:
        return 0.55
    return 1.0


def _liquidity_quality(quote_volume_m: float) -> float:
    if quote_volume_m >= 500:
        return 1.0
    if quote_volume_m >= 200:
        return 0.9
    if quote_volume_m >= 100:
        return 0.8
    if quote_volume_m >= 50:
        return 0.55
    if quote_volume_m >= 20:
        return 0.3
    return 0.0


def _volatility_quality(signal: Signal, mode: str) -> float:
    atr = signal.atr_4h_pct if mode == "swing" and signal.atr_4h_pct is not None else signal.atr_1h_pct
    atr = atr if atr is not None else signal.atr_pct
    if atr is None or atr <= 0:
        return 0.45
    low = 0.35 if mode == "intraday" else 0.8
    healthy = 4.0 if mode == "intraday" else 8.0
    extreme = 6.0 if mode == "intraday" else 14.0
    if atr < low:
        return 0.45
    if atr <= healthy:
        return 1.0
    if atr < extreme:
        return 0.45
    return 0.0


def _has_soft_block(warnings: list[str]) -> bool:
    phrases = (
        "过度拥挤",
        "波动突然放大",
        "多周期方向冲突",
        "主动成交方向与信号背离",
        "市场趋势相反",
    )
    return any(any(phrase in warning for phrase in phrases) for warning in warnings)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _strategy_for_regime(regime: MarketRegime) -> str:
    if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
        return "trend_following"
    if regime == MarketRegime.RANGE:
        return "mean_reversion"
    if regime == MarketRegime.HIGH_VOLATILITY:
        return "breakout"
    return "none"
