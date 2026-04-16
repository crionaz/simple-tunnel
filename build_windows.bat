@echo off
REM =============================================================================
REM  LAN Game Tunnel - Windows Native Build
REM
REM  Run this directly on Windows to build LANGameTunnel.exe
REM  Requires: Python 3.10+ installed and in PATH
REM =============================================================================

echo ============================================
echo  LAN Game Tunnel - Windows Build
echo ============================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

echo [1/3] Installing PyInstaller...
pip install pyinstaller >nul 2>&1

echo [2/3] Building executable...
pyinstaller --noconfirm --onefile --noconsole --uac-admin ^
    --name LANGameTunnel ^
    --distpath dist ^
    --workpath build_tmp\work ^
    --specpath build_tmp ^
    client.py

echo.
echo [3/3] Done!
echo.
if exist "dist\LANGameTunnel.exe" (
    echo   Output: dist\LANGameTunnel.exe
    echo   Copy it anywhere and double-click to run.
    echo   Make sure TAP-Windows driver is installed ^(see install_tap.bat^)
) else (
    echo   Build failed. Check output above for errors.
)
echo.
pause
