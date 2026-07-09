@echo off
cd /d "%~dp0"
echo Dry-run order only. This will not place a live trade.
echo.
python -m trade_assistant.main order --market spot --symbol UNIUSDT --side BUY --quantity 1 --type MARKET
echo.
pause

