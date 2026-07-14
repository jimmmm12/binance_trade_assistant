from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from .binance_client import BinanceClient
from .broker import build_order_payload, place_order
from .report import trade_plan_to_markdown, write_scan_report
from .risk import create_trade_plan
from .strategy import scan_market
from .strategy_scoring import score_signal


DEFAULT_SETTINGS = {
    "quote_asset": "USDT",
    "exclude_symbols": ["BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT"],
    "min_quote_volume": 50000000,
    "default_equity": 1000,
    "default_risk_pct": 1,
    "default_leverage": 2,
    "scan_limit": 30,
    "daily_loss_stop_pct": 2.0,
    "daily_loss_warning_pct": 1.5,
    "intraday_atr_multiplier": 1.4,
    "swing_atr_multiplier": 1.8,
    "min_live_score": 75,
    "risk_allocation": {
        "high_risk_pct": 20.0,
        "low_risk_pct": 80.0,
    },
    "auto_execution": {
        "max_new_positions_per_cycle": 2,
        "secondary_min_open_score": 65,
        "secondary_risk_multiplier": 0.5,
        "websocket_ready_wait_seconds": 30,
        "websocket_max_age_seconds": 20,
        "post_only_entries": True,
        "post_only_fallback_offset_bps": 3.0,
        "symbol_reentry_cooldown_minutes": 30,
        "max_entries_per_symbol_per_day": 3,
        "estimated_round_trip_cost_bps": 16.0,
        "min_expected_net_gain_pct": 0.45,
        "min_net_to_cost_multiple": 2.5,
        "live_min_score": 72,
        "live_min_volume_ratio": 1.2,
        "live_allowed_strategies": ["trend_following", "breakout"],
        "live_block_warning_markers": [
            "多周期方向冲突",
            "识别出的市场趋势相反",
            "主动成交方向与信号背离",
            "价格离保护结构较远",
        ],
        "portfolio_max_signed_correlation": 0.75,
        "portfolio_max_correlated_positions": 1,
        "portfolio_correlation_lookback": 48,
    },
    "opportunity_selection": {
        "enabled": True,
        "min_research_score_live": 62.0,
        "min_research_score_aggressive": 55.0,
        "structural_override_score": 82.0,
        "min_quote_volume_m": 30.0,
        "min_intraday_move_pct": 0.8,
        "ideal_intraday_move_pct": 3.5,
        "max_usable_intraday_move_pct": 5.5,
        "high_volatility_percentile": 92.0,
        "high_volatility_penalty": 12.0,
        "soft_warning_penalty": 5.0,
        "structural_warning_penalty": 18.0,
        "non_preferred_strategy_penalty": 16.0,
        "max_protection_distance_atr": 2.8,
        "allow_benchmark_divergence_aggressive": True,
        "preferred_live_strategies": ["trend_following", "breakout"],
        "weights": {
            "base_signal": 0.35,
            "liquidity": 0.18,
            "relative_strength": 0.18,
            "trend_confirmation": 0.19,
            "cost_edge": 0.10,
        },
    },
    "aggressive_line": {
        "first_entry_pct": 0.75,
        "max_single_risk_pct": 5.0,
        "max_symbol_exposure_pct": 300.0,
        "max_total_exposure_pct": 600.0,
        "min_leverage": 5.0,
        "max_leverage": 8.0,
        "risk_allocation_pct": 100.0,
        "min_notional_usdt": 8.0,
        "min_open_score": 66,
        "live_min_score": 66,
        "live_min_volume_ratio": 0.7,
        "min_expected_net_gain_pct": 0.22,
        "min_net_to_cost_multiple": 1.4,
        "min_net_reward_r": 0.75,
        "live_allowed_strategies": ["trend_following", "breakout", "mean_reversion"],
        "live_block_warning_markers": [
            "多周期方向冲突",
            "识别出的市场趋势相反",
            "主动成交方向与信号背离"
        ],
        "margin_tiers": [
            {"min_score": 90, "margin_pct": 55.0},
            {"min_score": 80, "margin_pct": 42.0},
            {"min_score": 70, "margin_pct": 32.0},
            {"min_score": 66, "margin_pct": 22.0}
        ],
        "leverage_cut_atr_pct": 4.5,
        "leverage_floor_atr_pct": 6.0,
        "recovery_after_consecutive_losses": 5,
        "recovery_min_score": 78,
        "recovery_min_volume_ratio": 0.9,
        "recovery_risk_multiplier": 0.45,
        "add_stage_pcts": [0.25, 0.15],
        "max_add_count": 2,
        "min_profit_r_for_add": 1.1,
        "min_add_score": 78,
        "risk_reduce_pct": 0.35,
        "profit_take_rules": [
            {"r": 1.75, "reduce_pct": 0.2, "marker": "1.75R减仓"},
            {"r": 3.0, "reduce_pct": 0.25, "marker": "3R减仓"},
        ],
        "trailing_atr_multiplier": 2.5,
        "time_stop_hours": 18.0,
        "time_stop_min_r": 0.25,
        "time_stop_min_score": 72,
        "max_margin_drawdown_reduce_pct": 15.0,
        "max_margin_drawdown_close_pct": 28.0,
        "max_position_leverage": 8.0,
        "loss_streak_reduce_after": 3,
        "loss_streak_stop_after": 8,
        "loss_streak_reduction_multiplier": 0.35,
        "score_tiers": [
            {"min_score": 90, "multiplier": 1.0},
            {"min_score": 80, "multiplier": 0.9},
            {"min_score": 70, "multiplier": 0.8},
            {"min_score": 66, "multiplier": 0.55},
        ],
    },
    "automation_positioning": {
        "max_single_risk_pct": 1.0,
        "score_tiers": [
            {"min_score": 90, "multiplier": 1.0},
            {"min_score": 80, "multiplier": 0.7},
            {"min_score": 70, "multiplier": 0.4},
        ],
        "min_open_score": 70,
        "first_entry_pct": 0.4,
        "max_initial_margin_pct": 20.0,
        "add_stage_pcts": [0.3, 0.3],
        "max_add_count": 2,
        "min_profit_r_for_add": 1.0,
        "min_add_score": 85,
        "add_order_pct_of_initial": 0.3,
        "allow_loss_add": False,
        "loss_add_min_score": 92,
        "max_loss_add_r": -0.35,
        "profit_take_rules": [
            {"r": 1.0, "reduce_pct": 0.3, "marker": "1R减仓"},
            {"r": 2.0, "reduce_pct": 0.3, "marker": "2R减仓"}
        ],
        "risk_reduce_pct": 0.5,
        "liquidity_sweep_protection": True,
        "stop_confirmation_min_score": 70,
        "atr_stop_multiplier": 2.0,
        "trailing_atr_multiplier": 2.0,
        "time_stop_hours": 48.0,
        "time_stop_min_r": 0.5,
        "time_stop_min_score": 78,
        "reduce_score_threshold": 60,
        "max_margin_drawdown_reduce_pct": 15.0,
        "max_margin_drawdown_close_pct": 28.0,
        "max_position_leverage": 5.0,
        "max_symbol_exposure_pct": 40.0,
        "max_total_exposure_pct": 180.0,
        "loss_streak_reduce_after": 3,
        "loss_streak_stop_after": 5,
        "loss_streak_reduction_multiplier": 0.5
    },
    "signal_score": {
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
    },
    "system_risk": {
        "max_single_risk_pct": 1.0,
        "max_daily_loss_pct": 2.0,
        "max_total_exposure_multiple": 3.0,
        "max_symbol_exposure_pct": 40.0,
        "max_leverage": 5.0,
        "reduce_after_consecutive_losses": 3,
        "stop_after_consecutive_losses": 5,
    },
    "order_manager": {
        "partial_fill_policy": "wait",
        "partial_fill_timeout_seconds": 30,
        "auto_place_protective_orders": True,
    },
}


class SettingsUnavailable(RuntimeError):
    """Raised when the external settings file is temporarily unreadable."""

    user_facing = True


def resolve_root(
    frozen: bool | None = None,
    executable: str | Path | None = None,
    file_path: str | Path | None = None,
) -> Path:
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        return Path(executable or sys.executable).resolve().parent
    return Path(file_path or __file__).resolve().parents[1]


def bundled_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()
    return ROOT


ROOT = resolve_root()
CONFIG_PATH = ROOT / "config" / "settings.json"
REPORT_DIR = ROOT / "reports"


def ensure_config_exists(default_config_path: Path | None = None) -> None:
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    source = default_config_path or bundled_root() / "config" / "settings.json"
    if source.exists() and source.resolve() != CONFIG_PATH.resolve():
        shutil.copyfile(source, CONFIG_PATH)
        return
    CONFIG_PATH.write_text(json.dumps(DEFAULT_SETTINGS, indent=2, ensure_ascii=False), encoding="utf-8")


def load_settings() -> dict:
    ensure_config_exists()
    last_error: json.JSONDecodeError | None = None
    for attempt in range(3):
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            if not raw.strip():
                raise json.JSONDecodeError("settings file is empty", raw, 0)
            stored = json.loads(raw)
            if not isinstance(stored, dict):
                raise json.JSONDecodeError("settings root must be an object", raw, 0)
            return _merge_settings(DEFAULT_SETTINGS, stored)
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.05)
    raise SettingsUnavailable("配置文件正在写入或格式无效；本轮已安全跳过，下一轮会自动重试") from last_error


def _merge_settings(defaults: dict, stored: dict) -> dict:
    merged: dict = {}
    for key, default_value in defaults.items():
        stored_value = stored.get(key, default_value)
        if isinstance(default_value, dict) and isinstance(stored_value, dict):
            merged[key] = _merge_settings(default_value, stored_value)
        else:
            merged[key] = stored_value
    for key, value in stored.items():
        if key not in merged:
            merged[key] = value
    return merged


def cmd_scan(args: argparse.Namespace) -> None:
    settings = load_settings()
    client = BinanceClient()
    markets = ["spot", "futures"] if args.market == "both" else [args.market]
    all_longs = []
    all_shorts = []
    for market in markets:
        longs, shorts = scan_market(client, market, settings, args.top)
        score_config = settings.get("signal_score")
        all_longs.extend(score_signal(item, "intraday", config=score_config) for item in longs)
        all_shorts.extend(score_signal(item, "intraday", config=score_config) for item in shorts)
    all_longs.sort(key=lambda x: (x.score, x.quote_volume_m), reverse=True)
    all_shorts.sort(key=lambda x: (x.score, x.quote_volume_m), reverse=True)
    md_path, csv_path = write_scan_report(all_longs, all_shorts, REPORT_DIR)
    print(f"Markdown report: {md_path}")
    print(f"CSV report: {csv_path}")
    print("Top long:", all_longs[0].symbol if all_longs else "none")
    print("Top short:", all_shorts[0].symbol if all_shorts else "none")


def cmd_plan(args: argparse.Namespace) -> None:
    plan = create_trade_plan(
        symbol=args.symbol,
        market=args.market,
        side=args.side,
        entry=args.entry,
        stop=args.stop,
        target=args.target,
        equity=args.equity,
        risk_pct=args.risk_pct,
        leverage=args.leverage,
    )
    text = trade_plan_to_markdown(plan)
    path = REPORT_DIR / f"plan_{args.symbol}_{args.side}.md"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"Saved: {path}")


def cmd_order(args: argparse.Namespace) -> None:
    client = BinanceClient()
    payload = build_order_payload(args.symbol, args.side, args.quantity, args.type, args.price)
    result = place_order(client, args.market, payload, args.allow_live, args.confirm)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance trade assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan spot/futures markets")
    scan.add_argument("--market", choices=["spot", "futures", "both"], default="both")
    scan.add_argument("--top", type=int, default=30)
    scan.set_defaults(func=cmd_scan)

    plan = sub.add_parser("plan", help="create a risk-based trade plan")
    plan.add_argument("--symbol", required=True)
    plan.add_argument("--market", choices=["spot", "futures"], required=True)
    plan.add_argument("--side", choices=["long", "short"], required=True)
    plan.add_argument("--entry", type=float, required=True)
    plan.add_argument("--stop", type=float, required=True)
    plan.add_argument("--target", type=float, required=True)
    plan.add_argument("--equity", type=float, default=1000)
    plan.add_argument("--risk-pct", type=float, default=1)
    plan.add_argument("--leverage", type=float, default=1)
    plan.set_defaults(func=cmd_plan)

    order = sub.add_parser("order", help="build or place an order; dry-run by default")
    order.add_argument("--market", choices=["spot", "futures"], required=True)
    order.add_argument("--symbol", required=True)
    order.add_argument("--side", choices=["BUY", "SELL"], required=True)
    order.add_argument("--quantity", type=float, required=True)
    order.add_argument("--type", choices=["MARKET", "LIMIT"], default="MARKET")
    order.add_argument("--price", type=float)
    order.add_argument("--allow-live", action="store_true")
    order.add_argument("--confirm")
    order.set_defaults(func=cmd_order)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
