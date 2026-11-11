@echo off
chcp 65001 >nul
title OwnAPI Desktop Bridge and Local Server Launcher
cd /d "%~dp0"

echo ============================================================================
echo   OwnAPI Desktop Bridge ^& Local Server Launcher
echo   Web Dashboard: http://127.0.0.1:8000
echo ============================================================================
echo.

python -m ownapi.cli start %*
if errorlevel 1 (
    echo.
    echo [!] OwnAPI baslatilirken bir hata olustu.
    pause
)
