from __future__ import annotations

from .models import TradePlan


def create_trade_plan(
    symbol: str,
    market: str,
    side: str,
    entry: float,
    stop: float,
    target: float,
    equity: float,
    risk_pct: float,
    leverage: float,
) -> TradePlan:
    if entry <= 0 or stop <= 0 or target <= 0:
        raise ValueError("entry, stop, and target must be positive")
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    if side == "long" and not (stop < entry < target):
        raise ValueError("long plan requires stop < entry < target")
    if side == "short" and not (target < entry < stop):
        raise ValueError("short plan requires target < entry < stop")
    risk_amount = equity * risk_pct / 100
    stop_distance = abs(entry - stop)
    quantity = risk_amount / stop_distance
    notional = quantity * entry
    margin_required = notional / leverage if leverage else notional
    loss_pct_to_stop = stop_distance / entry * 100
    gain_pct_to_target = abs(target - entry) / entry * 100
    return TradePlan(
        symbol=symbol,
        market=market,
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        equity=equity,
        risk_pct=risk_pct,
        leverage=leverage,
        risk_amount=risk_amount,
        quantity=quantity,
        notional=notional,
        margin_required=margin_required,
        loss_pct_to_stop=loss_pct_to_stop,
        gain_pct_to_target=gain_pct_to_target,
        leveraged_loss_pct=loss_pct_to_stop * leverage,
        leveraged_gain_pct=gain_pct_to_target * leverage,
    )

