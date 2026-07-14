# Binance Trade Assistant

交易系统内核、状态恢复、订单生命周期和风控说明见：`docs/trading-system-architecture.md`。

A local Binance spot/futures trade assistant with VeighNa-friendly structure.

It can:

- scan Binance spot and USDT perpetual markets
- rank long/short candidates
- generate Markdown and CSV reports
- calculate position size, stop loss, targets, and risk
- create dry-run order payloads
- optionally place live orders only when multiple safety switches are enabled

It does not place live orders by default.

## Quick Start

```powershell
cd D:\document\work\binance_trade_assistant
python -m trade_assistant.main scan --market both --top 25
```

Reports are written to:

```text
reports/latest.md
reports/latest.csv
```

## Trade Plan

```powershell
python -m trade_assistant.main plan --symbol UNIUSDT --market futures --side long --entry 3.25 --stop 3.12 --target 3.38 --equity 1000 --risk-pct 1 --leverage 2
```

## Dry-Run Order

```powershell
python -m trade_assistant.main order --market spot --symbol UNIUSDT --side BUY --quantity 1 --type MARKET
```

## Live Order Safety

Live order placement requires all of these:

```powershell
$env:BINANCE_API_KEY="..."
$env:BINANCE_API_SECRET="..."
$env:BINANCE_ENABLE_LIVE_TRADING="true"
python -m trade_assistant.main order --market spot --symbol UNIUSDT --side BUY --quantity 1 --type MARKET --allow-live --confirm 确认下单
```

Use API keys with the smallest permissions possible. Do not enable withdrawals.

## VeighNa Compatibility

The project keeps market data, signal, plan, and order payloads separated so that later adapters can map them to VeighNa data objects and gateways.

## Desktop App

The project also includes a local Windows desktop interface.

Development launch:

```powershell
cd D:\document\work\binance_trade_assistant
python -m pip install -r requirements.txt
python -m trade_assistant.gui.app
```

Build the Windows executable:

```powershell
cd D:\document\work\binance_trade_assistant
.\build_exe.bat
```

The generated executable is written to:

```text
release\BinanceTradeAssistant\BinanceTradeAssistant.exe
```

Run tests:

```powershell
$env:QT_QPA_PLATFORM="offscreen"
python -m pytest -q
```

Development notes and the next plan are kept in:

```text
docs\2026-07-10-development-log.md
```

Live trading remains locked unless all safety switches are enabled:

```text
BINANCE_API_KEY
BINANCE_API_SECRET
BINANCE_ENABLE_LIVE_TRADING=true
确认下单
```
