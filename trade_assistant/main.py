from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .binance_client import BinanceClient
from .broker import build_order_payload, place_order
from .report import trade_plan_to_markdown, write_scan_report
from .risk import create_trade_plan
from .strategy import scan_market


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
    "min_live_score": 70,
}


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
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def cmd_scan(args: argparse.Namespace) -> None:
    settings = load_settings()
    client = BinanceClient()
    markets = ["spot", "futures"] if args.market == "both" else [args.market]
    all_longs = []
    all_shorts = []
    for market in markets:
        longs, shorts = scan_market(client, market, settings, args.top)
        all_longs.extend(longs)
        all_shorts.extend(shorts)
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
