@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Build ax6_auto.exe

echo.
echo ==== Building ax6_auto.exe (PyInstaller onefile) ====
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found in PATH. Install Python 3.8+ first.
    echo https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

python -m pip install --upgrade pip
python -m pip install pyinstaller paramiko
if errorlevel 1 (
    echo.
    echo ERROR: failed to install build dependencies.
    pause
    exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --console --name ax6_auto --hidden-import paramiko --collect-submodules paramiko ax6_auto.py
if errorlevel 1 (
    echo.
    echo Build FAILED.
    pause
    exit /b 1
)

echo.
echo Build OK: %~dp0dist\ax6_auto.exe
echo Copy it next to this .bat file if you want to use run_ax6_auto.bat.
echo.
pause
endlocal
