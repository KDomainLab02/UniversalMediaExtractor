@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Universal Media Extractor

REM ==================================================================
REM  Universal Media Extractor - Windows launcher
REM  Keep this file ASCII-only: all messages live in launcher.py so
REM  cmd never mis-parses multi-byte characters.
REM
REM  Python discovery order:
REM    1. the py launcher
REM    2. python / python3 on PATH, skipping the Microsoft Store stub
REM       in WindowsApps (that stub is not Python and exits silently)
REM    3. the usual per-user install location
REM    4. python_path.txt - one line, full path to a portable python.exe
REM ==================================================================

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :run

for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | findstr /i "WindowsApps" >nul
    if errorlevel 1 if not defined PY set "PY=%%P"
)
if defined PY goto :run

for /f "delims=" %%P in ('where python3 2^>nul') do (
    echo %%P | findstr /i "WindowsApps" >nul
    if errorlevel 1 if not defined PY set "PY=%%P"
)
if defined PY goto :run

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%D\python.exe" if not defined PY set "PY=%%D\python.exe"
)
if defined PY goto :run

if exist "%~dp0python_path.txt" set /p PY=<"%~dp0python_path.txt"
if defined PY goto :run

:nopython
echo.
echo   Python 3.9+ was not found on this computer.
echo   Install it first: https://www.python.org/downloads/
echo   During setup, tick "Add python.exe to PATH".
echo   Or create python_path.txt next to this file with the
echo   full path to a portable python.exe (one line).
echo.
pause
exit /b 1

:run
%PY% launcher.py
echo.
pause
exit /b 0
