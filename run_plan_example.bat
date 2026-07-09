@echo off
cd /d "%~dp0"
echo Example: UNIUSDT futures long, 1000 USDT equity, 1 percent risk, 2x leverage.
echo Edit this file to change symbol, entry, stop, target, equity, risk, or leverage.
echo.
python -m trade_assistant.main plan --symbol UNIUSDT --market futures --side long --entry 3.25 --stop 3.12 --target 3.38 --equity 1000 --risk-pct 1 --leverage 2
echo.
pause

