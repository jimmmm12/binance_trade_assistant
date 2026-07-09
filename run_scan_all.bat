@echo off
cd /d "%~dp0"
echo Scanning Binance spot and USDT futures...
python -m trade_assistant.main scan --market both --top 20
echo.
echo Done.
echo Report:
echo %~dp0reports\latest.md
echo %~dp0reports\latest.csv
echo.
pause

