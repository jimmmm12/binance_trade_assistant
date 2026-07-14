from __future__ import annotations

from dataclasses import dataclass

from .backtest import BacktestResult
from .models import Signal
from .risk_engine import suggest_leverage


@dataclass(frozen=True)
class AdaptiveParameters:
    atr_multiplier: float
    reward_risk: float
    risk_pct: float
    suggested_leverage: float
    allow_live: bool
    reasons: list[str]
    warnings: list[str]


def adapt_parameters(
    signal: Signal,
    mode: str,
    backtest: BacktestResult | None = None,
    base_risk_pct: float = 1.0,
) -> AdaptiveParameters:
    atr = _mode_atr(signal, mode)
    base_risk_pct = max(0.0, float(base_risk_pct))
    atr_multiplier = 1.8 if mode == "swing" else 1.4
    reward_risk = 2.2 if mode == "swing" else 1.8
    risk_pct = base_risk_pct
    allow_live = True
    reasons: list[str] = []
    warnings: list[str] = []

    if signal.quote_volume_m >= 100:
        reasons.append("流动性充足")
    elif signal.quote_volume_m < 50:
        warnings.append("流动性不足，降低仓位")
        risk_pct = min(risk_pct, base_risk_pct * 0.5)
        allow_live = False

    high_atr = 8.0 if mode == "swing" else 4.0
    extreme_atr = 12.0 if mode == "swing" else 6.0
    if atr >= high_atr:
        warnings.append("波动偏大，放宽止损并降低仓位")
        atr_multiplier += 0.4
        risk_pct = min(risk_pct, base_risk_pct * 0.5)
    if atr >= extreme_atr:
        warnings.append("波动极端，只建议模拟观察")
        risk_pct = min(risk_pct, base_risk_pct * 0.3)
        allow_live = False

    if signal.funding_pct is not None and abs(signal.funding_pct) >= 0.08:
        warnings.append("资金费率拥挤，降低追单风险")
        risk_pct = min(risk_pct, base_risk_pct * 0.4)
        allow_live = False

    if signal.side == "long" and signal.rsi_1h >= 78:
        warnings.append("RSI过热，降低做多风险")
        risk_pct = min(risk_pct, base_risk_pct * 0.5)
    if signal.side == "short" and signal.rsi_1h <= 22:
        warnings.append("RSI过冷，降低做空风险")
        risk_pct = min(risk_pct, base_risk_pct * 0.5)

    if backtest is not None:
        if backtest.trades >= 20 and (backtest.win_rate < 40 or backtest.average_r <= 0):
            warnings.append("近期回测表现差，降低目标和仓位")
            reward_risk = 1.5 if mode == "swing" else 1.3
            risk_pct = min(risk_pct, base_risk_pct * 0.5)
            allow_live = False
        elif backtest.trades >= 20 and backtest.win_rate >= 55 and backtest.average_r > 0.2:
            reasons.append("近期回测表现较好")

    stop_pct = max(3.0 if mode == "swing" else 1.2, atr * atr_multiplier)
    leverage = suggest_leverage(stop_pct, mode)
    return AdaptiveParameters(
        atr_multiplier=round(atr_multiplier, 2),
        reward_risk=round(reward_risk, 2),
        risk_pct=round(risk_pct, 2),
        suggested_leverage=leverage,
        allow_live=allow_live,
        reasons=reasons,
        warnings=warnings,
    )


def _mode_atr(signal: Signal, mode: str) -> float:
    if mode == "swing":
        value = signal.atr_4h_pct if signal.atr_4h_pct is not None else signal.atr_pct
    else:
        value = signal.atr_1h_pct if signal.atr_1h_pct is not None else signal.atr_pct
    return value if value is not None and value > 0 else 0.0
