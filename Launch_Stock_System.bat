@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if exist "%USERPROFILE%\Documents\Stock\.baostock_only" set "PYTHONPATH=%USERPROFILE%\Documents\Stock\.baostock_only;%PYTHONPATH%"

set "PY=python.exe"
set "PYW=pythonw.exe"

if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -c "import pandas,numpy,baostock" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%~dp0.venv\Scripts\python.exe"
    set "PYW=%~dp0.venv\Scripts\pythonw.exe"
  )
)

"%PY%" -c "import tkinter,pandas,numpy,baostock" >nul 2>nul
if errorlevel 1 goto :ERROR

start "" "%PYW%" -X utf8 "%~dp0stock_selector\gui.pyw"
if errorlevel 1 goto :ERROR
exit /b 0

:ERROR
echo The stock system could not start because Python or its dependencies are incomplete.
echo Please run Install_Stock_System.bat first, then try again.
pause
exit /b 1
