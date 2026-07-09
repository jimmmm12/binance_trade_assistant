from __future__ import annotations

from .models import PositionAdvice, PositionSnapshot, ScoredSignal


OPEN_THRESHOLD = 70
ADD_THRESHOLD = 78
REDUCE_THRESHOLD = 60


def advise_position(
    signal: ScoredSignal,
    simulated: PositionSnapshot,
    real: PositionSnapshot | None,
) -> PositionAdvice:
    warnings: list[str] = []
    signal_side = signal.side

    if real and real.side != "flat" and real.side != signal_side:
        warnings.append("真实仓位反向，禁止自动反手")
        return PositionAdvice(action="block", summary="真实账户已有反向仓位，优先人工确认", warnings=warnings)

    if simulated.side != "flat" and simulated.side != signal_side:
        warnings.append("模拟仓位反向，禁止直接加仓")
        return PositionAdvice(action="block", summary="模拟仓已有反向仓位，先处理原仓位", warnings=warnings)

    warnings.extend(signal.warnings)
    if any("资金费率过热" in item or "资金费率过冷" in item for item in signal.warnings):
        return PositionAdvice(action="block", summary="资金费率拥挤，禁止追仓", warnings=warnings)

    has_position = simulated.side == signal_side or (real is not None and real.side == signal_side)
    if not has_position:
        if signal.score >= OPEN_THRESHOLD:
            return PositionAdvice(action="open", summary="信号强且当前无同市场仓位，允许开仓", warnings=warnings)
        return PositionAdvice(action="wait", summary="信号分不足，等待更清晰机会", warnings=warnings)

    pnl = simulated.unrealized_pnl
    if real and real.side == signal_side:
        pnl += real.unrealized_pnl

    if signal.score >= ADD_THRESHOLD and pnl >= 0:
        return PositionAdvice(action="add", summary="已有同向仓位且信号仍强，可考虑小幅加仓", warnings=warnings)
    if signal.score < REDUCE_THRESHOLD or pnl < 0:
        return PositionAdvice(action="reduce", summary="同向仓位信号转弱或浮亏，建议减仓", warnings=warnings)
    return PositionAdvice(action="hold", summary="已有同向仓位，暂不加仓，继续观察", warnings=warnings)
