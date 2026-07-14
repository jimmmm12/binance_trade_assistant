@echo off
cd /d "%~dp0"

echo LIVE ORDER TEMPLATE
echo This file can place a real Binance order after you edit it.
echo Read LIVE_ORDER_README.md before using it.
echo.

set BINANCE_API_KEY=PUT_YOUR_API_KEY_HERE
set BINANCE_API_SECRET=PUT_YOUR_API_SECRET_HERE
set BINANCE_ENABLE_LIVE_TRADING=true

python -m trade_assistant.main order --market spot --symbol UNIUSDT --side BUY --quantity 1 --type MARKET --allow-live --confirm 确认下单

echo.
pause

