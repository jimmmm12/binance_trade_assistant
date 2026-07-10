from __future__ import annotations

from .models import ScoreBreakdown, ScoredSignal, Signal


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))


def score_signal(
    signal: Signal,
    mode: str,
    btc_momentum_24h: float = 0.0,
    eth_momentum_24h: float = 0.0,
) -> ScoredSignal:
    if mode not in {"intraday", "swing"}:
        raise ValueError("mode must be intraday or swing")
    if mode == "intraday":
        return _score_intraday(signal, btc_momentum_24h, eth_momentum_24h)
    return _score_swing(signal, btc_momentum_24h, eth_momentum_24h)


def _score_intraday(signal: Signal, btc_momentum_24h: float, eth_momentum_24h: float) -> ScoredSignal:
    reasons: list[str] = []
    warnings: list[str] = []

    liquidity = _liquidity_score(signal.quote_volume_m, max_score=20)
    if liquidity >= 16:
        reasons.append("流动性充足，适合日内进出")

    volume = clamp(min(signal.volume_ratio / 2.0, 1.0) * 20)
    if signal.volume_ratio >= 1.5:
        reasons.append("短期放量明显")

    trend = 0
    if signal.side == "long":
        trend += 10 if signal.rsi_1h >= 50 else 4
        trend += 8 if signal.momentum_24h > 0 else 0
    else:
        trend += 10 if signal.rsi_1h <= 50 else 4
        trend += 8 if signal.momentum_24h < 0 else 0
    trend = clamp(trend, high=18)
    if trend >= 14:
        reasons.append("1h趋势结构配合信号方向")

    momentum = abs(signal.momentum_24h)
    relative_base = (btc_momentum_24h + eth_momentum_24h) / 2
    relative_delta = signal.momentum_24h - relative_base if signal.side == "long" else relative_base - signal.momentum_24h
    relative_strength = clamp(5 + relative_delta, high=10)
    if relative_strength >= 7:
        reasons.append("相对 BTC/ETH 更强")

    risk = _risk_score(signal.rsi_1h, momentum, max_score=10, warnings=warnings)
    funding = _funding_score(signal, max_score=7, warnings=warnings)
    volatility = _volatility_score(signal.atr_1h_pct or signal.atr_pct, max_score=10, warnings=warnings, mode="intraday")
    position = 10

    total = liquidity + volume + trend + clamp(momentum * 2.0, high=15) + relative_strength + risk + funding + volatility
    recommendation, action_level = _recommendation(total, warnings, signal, mode="intraday")
    breakdown = ScoreBreakdown(
        total=clamp(total),
        liquidity=liquidity,
        trend=trend,
        volume=volume,
        relative_strength=relative_strength,
        risk=risk,
        funding=funding,
        reasons=reasons,
        warnings=warnings,
        volatility=volatility,
        position=position,
        recommendation=recommendation,
        action_level=action_level,
    )
    return ScoredSignal(signal=signal, mode="intraday", breakdown=breakdown)


def _score_swing(signal: Signal, btc_momentum_24h: float, eth_momentum_24h: float) -> ScoredSignal:
    reasons: list[str] = []
    warnings: list[str] = []

    liquidity = _liquidity_score(signal.quote_volume_m, max_score=10)
    volume = clamp(min(signal.volume_ratio / 1.6, 1.0) * 15)
    if signal.volume_ratio >= 1.1:
        reasons.append("成交量具备延续性")

    trend = 0
    if signal.side == "long":
        trend += 12 if signal.rsi_4h >= 50 else 5
        trend += 13 if signal.momentum_3d > 0 else 0
    else:
        trend += 12 if signal.rsi_4h <= 50 else 5
        trend += 13 if signal.momentum_3d < 0 else 0
    trend = clamp(trend, high=25)
    if trend >= 15:
        reasons.append("4h波段趋势配合信号方向")

    relative_base = (btc_momentum_24h + eth_momentum_24h) / 2
    relative_delta = signal.momentum_24h - relative_base if signal.side == "long" else relative_base - signal.momentum_24h
    relative_strength = clamp(7 + relative_delta, high=15)
    if relative_strength >= 10:
        reasons.append("波段相对强弱占优")

    risk = _risk_score(signal.rsi_4h, abs(signal.momentum_3d), max_score=10, warnings=warnings)
    funding = _funding_score(signal, max_score=5, warnings=warnings)
    volatility = _volatility_score(signal.atr_4h_pct or signal.atr_pct, max_score=10, warnings=warnings, mode="swing")
    position = 10
    momentum_score = clamp(abs(signal.momentum_3d) * 2.2, high=20)
    if momentum_score >= 12:
        reasons.append("3日动量延续")

    total = liquidity + volume + trend + momentum_score + relative_strength + risk + funding + volatility
    recommendation, action_level = _recommendation(total, warnings, signal, mode="swing")
    breakdown = ScoreBreakdown(
        total=clamp(total),
        liquidity=liquidity,
        trend=trend,
        volume=volume,
        relative_strength=relative_strength,
        risk=risk,
        funding=funding,
        reasons=reasons,
        warnings=warnings,
        volatility=volatility,
        position=position,
        recommendation=recommendation,
        action_level=action_level,
    )
    return ScoredSignal(signal=signal, mode="swing", breakdown=breakdown)


def _liquidity_score(quote_volume_m: float, max_score: int) -> int:
    if quote_volume_m >= 500:
        return max_score
    if quote_volume_m >= 200:
        return clamp(max_score * 0.85, high=max_score)
    if quote_volume_m >= 80:
        return clamp(max_score * 0.65, high=max_score)
    if quote_volume_m >= 50:
        return clamp(max_score * 0.45, high=max_score)
    return clamp(max_score * 0.2, high=max_score)


def _risk_score(rsi_value: float, momentum_abs: float, max_score: int, warnings: list[str]) -> int:
    score = max_score
    if rsi_value >= 78:
        score -= 5
        warnings.append("RSI过热，追高风险上升")
    elif rsi_value <= 22:
        score -= 5
        warnings.append("RSI过冷，追空风险上升")
    if momentum_abs >= 12:
        score -= 4
        warnings.append("短期涨跌幅过大，可能存在插针或衰竭")
    return clamp(score, high=max_score)


def _funding_score(signal: Signal, max_score: int, warnings: list[str]) -> int:
    if signal.funding_pct is None:
        return max_score
    funding = signal.funding_pct
    if signal.side == "long" and funding >= 0.08:
        warnings.append("资金费率过热，多头拥挤")
        return clamp(max_score * 0.3, high=max_score)
    if signal.side == "short" and funding <= -0.05:
        warnings.append("资金费率过冷，空头拥挤")
        return clamp(max_score * 0.3, high=max_score)
    if abs(funding) >= 0.04:
        return clamp(max_score * 0.7, high=max_score)
    return max_score


def _volatility_score(atr_pct: float | None, max_score: int, warnings: list[str], mode: str) -> int:
    if atr_pct is None or atr_pct <= 0:
        warnings.append("ATR数据不足，计划质量降级")
        return clamp(max_score * 0.5, high=max_score)
    high = 6.0 if mode == "intraday" else 14.0
    warn = 4.0 if mode == "intraday" else 8.0
    if atr_pct >= high:
        warnings.append("ATR波动过大，禁止真仓自动下单")
        return clamp(max_score * 0.1, high=max_score)
    if atr_pct >= warn:
        warnings.append("ATR波动偏大，只建议小仓或模拟")
        return clamp(max_score * 0.45, high=max_score)
    if atr_pct <= (0.35 if mode == "intraday" else 0.8):
        warnings.append("波动过低，盈亏比可能不足")
        return clamp(max_score * 0.6, high=max_score)
    return max_score


def _recommendation(total: float, warnings: list[str], signal: Signal, mode: str) -> tuple[str, str]:
    score = clamp(total)
    hard_risk = any("禁止真仓" in warning or "过大" in warning for warning in warnings)
    if hard_risk or score < 55:
        return "禁止真仓，仅观察", "block_live"
    if warnings or score < 70:
        return "只模拟", "simulate_only"
    if score < 82:
        return "可交易，小仓确认", "small_trade"
    leverage_hint = "≤3x" if mode == "intraday" else "≤2x"
    if signal.market == "spot":
        leverage_hint = "现货"
    return f"可交易，{leverage_hint}", "tradeable"
