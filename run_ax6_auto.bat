@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
title AX6 Auto Flasher

rem ---- check administrator rights (skip if we already relaunched elevated) ----
if "%AX6_ELEVATED%"=="1" goto :run

net session >nul 2>&1
if not errorlevel 1 goto :run

fltmc >nul 2>&1
if not errorlevel 1 goto :run

echo.
echo Requesting administrator privileges...
set AX6_ELEVATED=1
if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList '%*'"
)
exit /b

:run
rem ---- prefer the packaged ax6_auto.exe (no Python installation needed) ----
set "AX6CMD="
if exist "%~dp0ax6_auto.exe" (
    set "AX6CMD="%~dp0ax6_auto.exe""
    goto :start
)
if exist "..\python\python.exe" (
    set "AX6CMD="..\python\python.exe" "ax6_auto.py""
    goto :start
)
echo.
echo ERROR: neither ax6_auto.exe nor ..\python\python.exe was found.
echo Keep ax6_auto.exe next to this .bat file, or keep this folder inside xmir-patcher.
echo.
pause
exit /b 1

:start
echo.
%AX6CMD% %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
    echo ---------------- FAILED ^(exit code %RC%^) ----------------
    echo See ax6_auto.log for details.
) else (
    echo ---------------- FINISHED ----------------
)
echo.
pause
endlocal
