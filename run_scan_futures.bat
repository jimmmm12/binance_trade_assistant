@echo off
cd /d "%~dp0"
echo Scanning Binance USDT futures...
python -m trade_assistant.main scan --market futures --top 30
echo.
echo Done.
echo Report:
echo %~dp0reports\latest.md
echo %~dp0reports\latest.csv
echo.
pause

