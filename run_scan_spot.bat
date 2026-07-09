@echo off
cd /d "%~dp0"
echo Scanning Binance spot...
python -m trade_assistant.main scan --market spot --top 30
echo.
echo Done.
echo Report:
echo %~dp0reports\latest.md
echo %~dp0reports\latest.csv
echo.
pause

