@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Export Browser Cookies

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY where python >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=python"
if not defined PY where python3 >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=python3"

if not defined PY (
    echo Python 3.9+ was not found: https://www.python.org/downloads/
    pause
    exit /b 1
)

%PY% export_cookies.py edge
echo.
pause
