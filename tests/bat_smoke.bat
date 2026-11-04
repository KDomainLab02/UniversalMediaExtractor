@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title bat-smoke

call :find_python
if not defined PY goto :no_python

echo FOUND_PY=[%PY%]

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python
echo VERSION_OK

%PY% -c "import flask, yt_dlp, imageio_ffmpeg, websocket" >nul 2>nul
if not errorlevel 1 goto :deps_ok
echo DEPS_MISSING
goto :end

:deps_ok
echo DEPS_OK

:end
echo SMOKE_DONE
exit /b 0

:find_python
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY where python >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=python"
if not defined PY where python3 >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=python3"
goto :eof

:no_python
echo NO_PYTHON
exit /b 1

:old_python
echo OLD_PYTHON
exit /b 1
