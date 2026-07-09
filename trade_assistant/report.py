from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .models import ScoredSignal, Signal, TradePlan


def fmt(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def write_scan_report(longs: list[Signal | ScoredSignal], shorts: list[Signal | ScoredSignal], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "latest.md"
    csv_path = output_dir / "latest.csv"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    lines.append("# Binance 交易助手报告")
    lines.append("")
    lines.append(f"生成时间：{now}")
    lines.append("")
    lines.append("这是信号观察报告，不是投资建议。默认不会自动下单。")
    lines.append("")
    lines.append("阅读方法：")
    lines.append("")
    lines.append("- 偏多观察：只代表可以观察做多条件，不代表立刻买。")
    lines.append("- 偏空观察：只代表可以观察做空条件，不代表立刻空。")
    lines.append("- 分数越高，越值得观察，但仍然必须等入场条件。")
    lines.append("- RSI过高可能过热，RSI过低可能超跌。")
    lines.append("- 成交量倍数越高，说明最近放量越明显。")
    lines.append("- 资金费率过高时，多头可能拥挤；资金费率过低时，空头可能拥挤。")
    lines.append("")
    lines.extend(_signal_section("偏多观察名单", longs[:10]))
    lines.append("")
    lines.extend(_signal_section("偏空观察名单", shorts[:10]))
    md_path.write_text("\n".join(lines), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = [
            "市场",
            "交易对",
            "方向",
            "分数",
            "最新价",
            "24h涨跌幅",
            "成交额_百万",
            "RSI_1小时",
            "RSI_4小时",
            "成交量倍数",
            "24h动量",
            "3日动量",
            "资金费率",
            "流动性分",
            "趋势分",
            "量能分",
            "强弱分",
            "风险分",
            "资金费率分",
            "入选原因",
            "风险提示",
            "备注",
        ]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for signal in [*longs, *shorts]:
            writer.writerow(_signal_csv_row(signal))
    return md_path, csv_path


def _signal_section(title: str, signals: list[Signal | ScoredSignal]) -> list[str]:
    lines = [f"## {title}", ""]
    lines.append("| 市场 | 交易对 | 方向 | 分数 | 最新价 | 24h涨跌 | 成交额(百万) | RSI 1h | RSI 4h | 成交量倍数 | 24h动量 | 3日动量 | 资金费率 | 入选原因 | 风险提示 | 备注 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|")
    for item in signals:
        signal = _base_signal(item)
        side = "做多" if signal.side == "long" else "做空"
        market = "合约" if signal.market == "futures" else "现货"
        reasons = "；".join(item.reasons) if isinstance(item, ScoredSignal) else ""
        warnings = "；".join(item.warnings) if isinstance(item, ScoredSignal) else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    market,
                    signal.symbol,
                    side,
                    str(item.score),
                    fmt(signal.last, 8),
                    fmt(signal.change_24h, 2),
                    fmt(signal.quote_volume_m, 0),
                    fmt(signal.rsi_1h, 1),
                    fmt(signal.rsi_4h, 1),
                    fmt(signal.volume_ratio, 2),
                    fmt(signal.momentum_24h, 2),
                    fmt(signal.momentum_3d, 2),
                    fmt(signal.funding_pct, 4),
                    reasons,
                    warnings,
                    signal.note,
                ]
            )
            + " |"
        )
    return lines


def _signal_csv_row(signal: Signal | ScoredSignal) -> dict[str, str | float | int]:
    base = _base_signal(signal)
    breakdown = signal.breakdown if isinstance(signal, ScoredSignal) else None
    return {
        "市场": "合约" if base.market == "futures" else "现货",
        "交易对": base.symbol,
        "方向": "做多" if base.side == "long" else "做空",
        "分数": signal.score,
        "最新价": base.last,
        "24h涨跌幅": base.change_24h,
        "成交额_百万": base.quote_volume_m,
        "RSI_1小时": base.rsi_1h,
        "RSI_4小时": base.rsi_4h,
        "成交量倍数": base.volume_ratio,
        "24h动量": base.momentum_24h,
        "3日动量": base.momentum_3d,
        "资金费率": "" if base.funding_pct is None else base.funding_pct,
        "流动性分": "" if breakdown is None else breakdown.liquidity,
        "趋势分": "" if breakdown is None else breakdown.trend,
        "量能分": "" if breakdown is None else breakdown.volume,
        "强弱分": "" if breakdown is None else breakdown.relative_strength,
        "风险分": "" if breakdown is None else breakdown.risk,
        "资金费率分": "" if breakdown is None else breakdown.funding,
        "入选原因": "" if breakdown is None else "；".join(breakdown.reasons),
        "风险提示": "" if breakdown is None else "；".join(breakdown.warnings),
        "备注": base.note,
    }


def _base_signal(signal: Signal | ScoredSignal) -> Signal:
    return signal.signal if isinstance(signal, ScoredSignal) else signal


def trade_plan_to_markdown(plan: TradePlan) -> str:
    lines = [
        "# 交易方案",
        "",
        f"- 交易对：{plan.symbol}",
        f"- 市场：{'合约' if plan.market == 'futures' else '现货'}",
        f"- 方向：{'做多' if plan.side == 'long' else '做空'}",
        f"- 入场价：{fmt(plan.entry, 8)}",
        f"- 止损价：{fmt(plan.stop, 8)}",
        f"- 目标价：{fmt(plan.target, 8)}",
        f"- 本金：{fmt(plan.equity, 2)}",
        f"- 单笔风险：{fmt(plan.risk_pct, 2)}%",
        f"- 杠杆：{fmt(plan.leverage, 2)}x",
        f"- 最多亏损金额：{fmt(plan.risk_amount, 2)}",
        f"- 建议数量：{fmt(plan.quantity, 8)}",
        f"- 名义仓位：{fmt(plan.notional, 2)}",
        f"- 需要保证金：{fmt(plan.margin_required, 2)}",
        f"- 到止损亏损：{fmt(plan.loss_pct_to_stop, 2)}%",
        f"- 到目标盈利：{fmt(plan.gain_pct_to_target, 2)}%",
        f"- 加杠杆后到止损约亏：{fmt(plan.leveraged_loss_pct, 2)}%",
        f"- 加杠杆后到目标约赚：{fmt(plan.leveraged_gain_pct, 2)}%",
    ]
    return "\n".join(lines)
