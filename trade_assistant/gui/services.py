from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from trade_assistant.binance_client import BinanceAuthError, BinanceClient
from trade_assistant.backtest import BacktestResult, backtest_atr_plan
from trade_assistant.adaptive_parameters import AdaptiveParameters, adapt_parameters
from trade_assistant.automation_policy import initial_sizing_decision
from trade_assistant.broker import build_order_payload, place_order
from trade_assistant.main import REPORT_DIR, load_settings
from trade_assistant.models import PositionAdvice, PositionSnapshot, ScoredSignal, Signal, TradePlan
from trade_assistant.portfolio import SimulatedPortfolio, read_real_futures_position, read_real_spot_position
from trade_assistant.position_advisor import advise_position
from trade_assistant.report import trade_plan_to_markdown, write_scan_report
from trade_assistant.risk import create_trade_plan
from trade_assistant.risk_engine import (
    PlanRiskReview,
    allocation_pct_for_bucket,
    classify_risk_bucket,
    evaluate_plan_risk,
    suggest_leverage,
)
from trade_assistant.strategy import scan_market
from trade_assistant.strategy_scoring import score_signal


@dataclass(frozen=True)
class LiveTradingStatus:
    enabled: bool
    has_api_key: bool
    has_api_secret: bool
    env_switch_enabled: bool
    reason: str


@dataclass(frozen=True)
class ScanResult:
    longs: list[Signal | ScoredSignal]
    shorts: list[Signal | ScoredSignal]
    markdown_path: Path
    csv_path: Path


@dataclass(frozen=True)
class AutoPlanPrices:
    entry: float
    stop: float
    target: float
    stop_pct: float
    reward_risk: float
    risk_note: str
    warning: str | None = None
    adaptive: AdaptiveParameters | None = None


def format_float(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def live_trading_status() -> LiveTradingStatus:
    has_api_key = bool(os.getenv("BINANCE_API_KEY"))
    has_api_secret = bool(os.getenv("BINANCE_API_SECRET"))
    env_switch_enabled = os.getenv("BINANCE_ENABLE_LIVE_TRADING", "").lower() == "true"
    missing: list[str] = []
    if not has_api_key:
        missing.append("API Key")
    if not has_api_secret:
        missing.append("API Secret")
    if not env_switch_enabled:
        missing.append("BINANCE_ENABLE_LIVE_TRADING=true")
    enabled = not missing
    reason = "真下单环境已就绪，仍需订单确认" if enabled else "真下单锁定：" + "、".join(missing)
    return LiveTradingStatus(
        enabled=enabled,
        has_api_key=has_api_key,
        has_api_secret=has_api_secret,
        env_switch_enabled=env_switch_enabled,
        reason=reason,
    )


def signal_to_row(signal: Signal | ScoredSignal) -> dict[str, str]:
    base = signal.signal if isinstance(signal, ScoredSignal) else signal
    score = signal.score if isinstance(signal, ScoredSignal) else base.score
    reasons = "；".join(signal.reasons) if isinstance(signal, ScoredSignal) else base.note
    warnings = "；".join(signal.warnings) if isinstance(signal, ScoredSignal) else ""
    recommendation = (
        signal.breakdown.recommendation if isinstance(signal, ScoredSignal) else _signal_recommendation(base, warnings)
    )
    grade = signal.breakdown.grade if isinstance(signal, ScoredSignal) else "旧版"
    market_regime = signal.breakdown.market_regime if isinstance(signal, ScoredSignal) else "-"
    selected_strategy = signal.breakdown.selected_strategy if isinstance(signal, ScoredSignal) else "-"
    multiplier = f"{signal.breakdown.position_multiplier:.0%}" if isinstance(signal, ScoredSignal) else "-"
    score_detail = "-"
    if isinstance(signal, ScoredSignal):
        detail = signal.breakdown
        score_detail = (
            f"趋势 {detail.trend}｜动量 {detail.momentum}｜量能 {detail.volume}｜"
            f"位置 {detail.positioning}｜周期 {detail.timeframe}｜环境 {detail.regime}"
        )
    return {
        "市场": "合约" if base.market == "futures" else "现货",
        "交易对": base.symbol,
        "方向": "做多" if base.side == "long" else "做空",
        "分数": str(score),
        "等级": grade,
        "市场状态": market_regime,
        "策略": selected_strategy,
        "风险系数": multiplier,
        "评分明细": score_detail,
        "最新价": format_float(base.last, 8),
        "24h涨跌": format_float(base.change_24h, 2),
        "成交额(百万)": format_float(base.quote_volume_m, 0),
        "RSI 1h": format_float(base.rsi_1h, 1),
        "RSI 4h": format_float(base.rsi_4h, 1),
        "成交量倍数": format_float(base.volume_ratio, 2),
        "24h动量": format_float(base.momentum_24h, 2),
        "3日动量": format_float(base.momentum_3d, 2),
        "资金费率": format_float(base.funding_pct, 4),
        "推荐理由": reasons,
        "风险点": warnings or _signal_risk_points(base),
        "建议操作": recommendation,
        "备注": f"{reasons} {warnings}".strip(),
    }


def _signal_risk_points(signal: Signal) -> str:
    risks: list[str] = []
    if signal.rsi_1h >= 78:
        risks.append("RSI过热")
    if signal.rsi_1h <= 22:
        risks.append("RSI过冷")
    if signal.atr_pct is not None and signal.atr_pct >= 4:
        risks.append("ATR波动偏大")
    if signal.funding_pct is not None and abs(signal.funding_pct) >= 0.08:
        risks.append("资金费率拥挤")
    return "；".join(risks) if risks else "未见明显拥挤风险"


def _signal_recommendation(signal: Signal, warnings: str) -> str:
    stop_pct = max(1.2, (_mode_atr_pct(signal, "intraday") * 1.4))
    leverage = suggest_leverage(stop_pct, "intraday")
    if warnings or stop_pct > 6:
        return "只模拟观察"
    if signal.score >= 8 and signal.quote_volume_m >= 100:
        return f"可小仓，≤{leverage:.1f}x"
    return "等待确认"


def run_scan(
    market: str,
    top: int,
    mode: str = "intraday",
    *,
    settings_loader: Callable[[], dict[str, Any]] = load_settings,
    client_factory: Callable[[], Any] = BinanceClient,
    scan_fn: Callable[[Any, str, dict[str, Any], int], tuple[list[Signal], list[Signal]]] = scan_market,
    report_writer: Callable[[list[Signal], list[Signal], Path], tuple[Path, Path]] = write_scan_report,
    output_dir: Path = REPORT_DIR,
) -> ScanResult:
    settings = settings_loader()
    client = client_factory()
    markets = ["spot", "futures"] if market == "both" else [market]
    all_longs: list[Signal | ScoredSignal] = []
    all_shorts: list[Signal | ScoredSignal] = []
    for item in markets:
        longs, shorts = scan_fn(client, item, settings, top)
        btc_momentum, eth_momentum = _benchmark_momentum(client, item)
        all_longs.extend(_score_many(longs, mode, settings, btc_momentum, eth_momentum))
        all_shorts.extend(_score_many(shorts, mode, settings, btc_momentum, eth_momentum))
    all_longs.sort(key=lambda x: (x.score, x.quote_volume_m), reverse=True)
    all_shorts.sort(key=lambda x: (x.score, x.quote_volume_m), reverse=True)
    markdown_path, csv_path = report_writer(all_longs, all_shorts, output_dir)
    return ScanResult(all_longs, all_shorts, markdown_path, csv_path)


def _score_many(
    signals: list[Signal],
    mode: str,
    settings: dict[str, Any],
    btc_momentum_24h: float,
    eth_momentum_24h: float,
) -> list[ScoredSignal]:
    config = settings.get("signal_score")
    return [
        score_signal(
            signal,
            mode=mode,
            btc_momentum_24h=btc_momentum_24h,
            eth_momentum_24h=eth_momentum_24h,
            config=config,
        )
        for signal in signals
    ]


def _benchmark_momentum(client: Any, market: str) -> tuple[float, float]:
    values: list[float] = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        try:
            rows = client.klines(market, symbol, "1h", 25)
            current = float(rows[-1][4])
            previous = float(rows[-24][4])
            values.append((current / previous - 1) * 100 if previous else 0.0)
        except Exception:
            values.append(0.0)
    return values[0], values[1]


def auto_plan_prices(signal: Signal | ScoredSignal, mode: str) -> AutoPlanPrices:
    base = signal.signal if isinstance(signal, ScoredSignal) else signal
    settings = load_settings()
    atr_pct = _mode_atr_pct(base, mode)
    adaptive = adapt_parameters(base, mode, base_risk_pct=float(settings.get("default_risk_pct", 1.0)))
    sizing = initial_sizing_decision(
        signal,
        mode,
        settings,
        base_risk_pct=min(adaptive.risk_pct, float(settings.get("default_risk_pct", 1.0))),
    )
    score_note = ""
    if isinstance(signal, ScoredSignal):
        action_level = signal.breakdown.action_level
        adaptive = replace(
            adaptive,
            risk_pct=sizing.risk_pct,
            allow_live=adaptive.allow_live and sizing.allowed and action_level in {"tradeable", "small_trade"},
            reasons=[
                *adaptive.reasons,
                f"评分等级 {signal.breakdown.grade}，评分风险系数 {sizing.score_multiplier:.0%}",
                f"{sizing.stage_label} {sizing.stage_multiplier:.0%}",
            ],
            warnings=[*adaptive.warnings, *sizing.warnings],
        )
        score_note = (
            f"，评分 {signal.score}/100（{signal.breakdown.grade}级），"
            f"评分系数 {sizing.score_multiplier:.0%}，阶段 {sizing.stage_multiplier:.0%}"
        )
    else:
        adaptive = replace(
            adaptive,
            risk_pct=sizing.risk_pct,
            allow_live=adaptive.allow_live and sizing.allowed,
            reasons=[*adaptive.reasons, *sizing.reasons],
            warnings=[*adaptive.warnings, *sizing.warnings],
        )
    if mode == "swing":
        configured_multiplier = float(settings.get("swing_atr_multiplier", 1.8))
        multiplier = max(configured_multiplier, adaptive.atr_multiplier)
        stop_pct = max(3.0, atr_pct * multiplier)
        reward_risk = adaptive.reward_risk
        high_volatility_limit = 14.0
        mode_label = "1-3天波段"
    else:
        configured_multiplier = float(settings.get("intraday_atr_multiplier", 1.4))
        multiplier = max(configured_multiplier, adaptive.atr_multiplier)
        stop_pct = max(1.2, atr_pct * multiplier)
        reward_risk = adaptive.reward_risk
        high_volatility_limit = 6.0
        mode_label = "日内短线"
    entry = base.last
    stop_distance = entry * stop_pct / 100
    target_distance = stop_distance * reward_risk
    if base.side == "short":
        stop = entry + stop_distance
        target = entry - target_distance
    else:
        stop = entry - stop_distance
        target = entry + target_distance
    warnings = list(adaptive.warnings)
    if stop_pct > high_volatility_limit:
        warnings.append("波动过大，不建议真下单，只建议模拟观察")
    warning = "；".join(warnings) if warnings else None
    risk_note = (
        f"{mode_label}：ATR {atr_pct:.2f}%，ATR倍数 {multiplier:.2f}，"
        f"止损距离 {stop_pct:.2f}%，目标 {reward_risk:.1f}R，"
        f"动态风险 {adaptive.risk_pct:.2f}%，建议杠杆≤{adaptive.suggested_leverage:.1f}x"
        f"{score_note}"
    )
    return AutoPlanPrices(
        entry=round(entry, 8),
        stop=round(stop, 8),
        target=round(target, 8),
        stop_pct=round(stop_pct, 4),
        reward_risk=reward_risk,
        risk_note=risk_note,
        warning=warning,
        adaptive=adaptive,
    )


def _mode_atr_pct(signal: Signal, mode: str) -> float:
    if mode == "swing":
        atr = signal.atr_4h_pct if signal.atr_4h_pct is not None else signal.atr_pct
    else:
        atr = signal.atr_1h_pct if signal.atr_1h_pct is not None else signal.atr_pct
    return atr if atr is not None and atr > 0 else 0.0


def detect_positions(
    symbol: str,
    market: str,
    signal: ScoredSignal | None,
    portfolio_path: Path | None = None,
    client: BinanceClient | None = None,
) -> tuple[PositionSnapshot, PositionSnapshot | None, PositionAdvice]:
    portfolio = SimulatedPortfolio(portfolio_path) if portfolio_path else SimulatedPortfolio()
    simulated = portfolio.get_position(market, symbol)
    real = None
    warnings: list[str] = []
    if os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET"):
        real_client = client or BinanceClient()
        try:
            real = (
                read_real_futures_position(real_client, symbol)
                if market == "futures"
                else read_real_spot_position(real_client, symbol)
            )
        except BinanceAuthError as exc:
            warnings.append(f"真实仓读取失败：{exc}")
    if signal is None:
        advice = PositionAdvice(action="wait", summary="请先选择扫描信号，再判断开仓/加仓/减仓", warnings=warnings)
    else:
        advice = advise_position(signal, simulated, real)
        if warnings:
            advice = PositionAdvice(action=advice.action, summary=advice.summary, warnings=warnings + advice.warnings)
    return simulated, real, advice


def quick_backtest_for_signal(
    signal: Signal | ScoredSignal,
    mode: str,
    stop_pct: float,
    reward_risk: float,
    client: BinanceClient | Any | None = None,
) -> BacktestResult | None:
    base = signal.signal if isinstance(signal, ScoredSignal) else signal
    interval = "4h" if mode == "swing" else "1h"
    lookahead = 12 if mode == "swing" else 6
    try:
        rows = (client or BinanceClient()).klines(base.market, base.symbol, interval, 240)
    except Exception:
        return None
    closes = [float(row[4]) for row in rows]
    return backtest_atr_plan(closes, base.side, stop_pct, reward_risk, lookahead=lookahead)


def create_plan_from_form(
    symbol: str,
    market: str,
    side: str,
    entry: str,
    stop: str,
    target: str,
    equity: str,
    risk_pct: str,
    leverage: str,
) -> TradePlan:
    return create_trade_plan(
        symbol=symbol.strip().upper(),
        market=market,
        side=side,
        entry=parse_required_float(entry, "入场价"),
        stop=parse_required_float(stop, "止损价"),
        target=parse_required_float(target, "目标价"),
        equity=parse_required_float(equity, "本金"),
        risk_pct=parse_required_float(risk_pct, "风险%"),
        leverage=parse_required_float(leverage, "杠杆"),
    )


def evaluate_plan_from_form(
    symbol: str,
    market: str,
    side: str,
    entry: str,
    stop: str,
    target: str,
    equity: str,
    risk_pct: str,
    leverage: str,
    signal: Signal | ScoredSignal | None,
    position: PositionSnapshot | None,
    mode: str,
    allocation_pct_override: float | None = None,
) -> tuple[TradePlan, PlanRiskReview]:
    total_equity = parse_required_float(equity, "本金")
    plan = create_plan_from_form(symbol, market, side, entry, stop, target, str(total_equity), risk_pct, leverage)
    base_signal = signal.signal if isinstance(signal, ScoredSignal) else signal
    settings = load_settings()
    review = evaluate_plan_risk(
        plan,
        base_signal,
        position,
        mode,
        min_live_score=int(settings.get("min_live_score", 75)),
    )
    risk_bucket = classify_risk_bucket(review, base_signal, mode)
    allocation_pct = (
        max(0.0, min(100.0, float(allocation_pct_override)))
        if allocation_pct_override is not None
        else allocation_pct_for_bucket(settings, risk_bucket)
    )
    allocated_equity = round(total_equity * allocation_pct / 100, 8)
    if allocated_equity <= 0:
        allocated_equity = total_equity
    if abs(allocated_equity - plan.equity) > 1e-9:
        plan = create_plan_from_form(
            symbol,
            market,
            side,
            entry,
            stop,
            target,
            str(allocated_equity),
            risk_pct,
            leverage,
        )
        review = evaluate_plan_risk(
            plan,
            base_signal,
            position,
            mode,
            min_live_score=int(settings.get("min_live_score", 75)),
        )
    allocation_label = "激进线全账户" if allocation_pct_override is not None else f"{risk_bucket}池"
    reason = f"资金分池：总本金 {total_equity:.2f}，{allocation_label} {allocation_pct:.0f}% = {allocated_equity:.2f} USDT"
    review = replace(
        review,
        risk_bucket=risk_bucket,
        allocation_pct=allocation_pct,
        allocation_equity=allocated_equity,
        total_equity=total_equity,
        reasons=[*review.reasons, reason],
    )
    return plan, review


def parse_required_float(value: str, label: str) -> float:
    text = value.strip()
    if not text:
        raise ValueError(f"请填写{label}")
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{label}必须是数字") from exc


def parse_optional_float(value: str, fallback: float) -> float:
    text = value.strip()
    if not text:
        return fallback
    try:
        parsed = float(text)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def _safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return cleaned.strip("_") or "UNKNOWN"


def save_trade_plan(plan: TradePlan, output_dir: Path = REPORT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    symbol = _safe_filename_part(plan.symbol)
    side = _safe_filename_part(plan.side)
    path = output_dir / f"plan_{symbol}_{side}.md"
    path.write_text(trade_plan_to_markdown(plan), encoding="utf-8")
    return path


def order_from_form(
    market: str,
    symbol: str,
    side: str,
    quantity: str,
    order_type: str,
    price: str,
    allow_live: bool,
    confirm: str,
    *,
    reduce_only: bool = False,
    client: BinanceClient | None = None,
    place_order_fn: Callable[[BinanceClient | Any, str, dict[str, Any], bool, str | None], dict[str, Any]] = place_order,
) -> dict[str, Any]:
    parsed_price = float(price) if price.strip() else None
    order_client = client or BinanceClient()
    normalized_quantity = normalize_order_quantity(
        order_client,
        market,
        symbol.strip().upper(),
        quantity,
        order_type,
    )
    payload = build_order_payload(
        symbol=symbol.strip().upper(),
        side=side,
        quantity=normalized_quantity,
        order_type=order_type,
        price=parsed_price,
        reduce_only=reduce_only and market == "futures",
    )
    return place_order_fn(order_client, market, payload, allow_live, confirm or None)


def normalize_order_quantity(
    client: BinanceClient | Any,
    market: str,
    symbol: str,
    quantity: str,
    order_type: str,
) -> str:
    parsed = Decimal(quantity.strip())
    rules = _symbol_quantity_rules(client, market, symbol, order_type)
    if rules is None:
        return _decimal_text(parsed)
    step = Decimal(str(rules["step_size"]))
    min_qty = Decimal(str(rules["min_qty"]))
    if step > 0:
        parsed = (parsed / step).to_integral_value(rounding=ROUND_DOWN) * step
    if parsed <= 0:
        raise ValueError("下单数量必须大于 0")
    if parsed < min_qty:
        raise ValueError(f"{symbol} 下单数量低于最小数量 {min_qty}")
    return _decimal_text(parsed)


def _symbol_quantity_rules(
    client: BinanceClient | Any,
    market: str,
    symbol: str,
    order_type: str,
) -> dict[str, str] | None:
    if not hasattr(client, "public_get"):
        return None
    try:
        data = client.public_get(
            market,
            "/fapi/v1/exchangeInfo" if market == "futures" else "/api/v3/exchangeInfo",
            {"symbol": symbol},
        )
    except Exception:
        return None
    symbols = data.get("symbols", []) if isinstance(data, dict) else []
    row = next((item for item in symbols if item.get("symbol") == symbol), None)
    if not row:
        return None
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    lot_filter = filters.get("MARKET_LOT_SIZE") if order_type.upper() == "MARKET" else None
    lot_filter = lot_filter or filters.get("LOT_SIZE")
    if not lot_filter:
        return None
    return {
        "step_size": str(lot_filter.get("stepSize", "0")),
        "min_qty": str(lot_filter.get("minQty", "0")),
    }


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def simulate_order_from_form(
    market: str,
    symbol: str,
    side: str,
    quantity: str,
    order_type: str,
    price: str,
    fallback_price: str,
    leverage: str = "1",
    portfolio_path: Path | None = None,
) -> tuple[dict[str, Any], PositionSnapshot]:
    fill_price = price.strip() or fallback_price.strip()
    if not fill_price:
        raise ValueError("模拟市价单需要入场价或限价作为成交价")
    result = order_from_form(
        market=market,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price,
        allow_live=False,
        confirm="",
    )
    portfolio = SimulatedPortfolio(portfolio_path) if portfolio_path else SimulatedPortfolio()
    position = portfolio.apply_fill(
        market=market,
        symbol=symbol.strip().upper(),
        side=side,
        quantity=float(quantity),
        price=float(fill_price),
        leverage=parse_optional_float(leverage, 1.0),
    )
    return result, position
