@echo off
setlocal
cd /d "%~dp0"
set DIST_PARENT=release
set DIST_DIR=release\BinanceTradeAssistant

if not exist ".venv\Scripts\python.exe" (
    echo Creating local build environment in .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create local build environment.
        pause
        exit /b 1
    )
)

echo Checking Python dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

if exist "%DIST_DIR%" (
    echo Existing build output found: %DIST_DIR%
    choice /C YN /M "Delete this build output and rebuild"
    if errorlevel 2 (
        echo Build cancelled.
        pause
        exit /b 1
    )
    rmdir /s /q "%DIST_DIR%"
)

echo Building BinanceTradeAssistant.exe...
".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm ^
    --windowed ^
    --name BinanceTradeAssistant ^
    --distpath "%DIST_PARENT%" ^
    --icon "assets\app_icon.ico" ^
    --add-data "config;config" ^
    --add-data "assets;assets" ^
    --add-binary "D:\veighna_studio\DLLs\_sqlite3.pyd;." ^
    --add-binary "D:\veighna_studio\DLLs\sqlite3.dll;." ^
    --hidden-import PySide6.QtCore ^
    --hidden-import PySide6.QtGui ^
    --hidden-import PySide6.QtWidgets ^
    --hidden-import _sqlite3 ^
    --hidden-import websocket ^
    trade_assistant\gui\app.py

if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo Build complete: %DIST_DIR%\BinanceTradeAssistant.exe
pause
