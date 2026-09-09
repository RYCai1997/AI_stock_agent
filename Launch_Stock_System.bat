@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYW=pythonw"
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%USERPROFILE%\Documents\Stock\.baostock_only" set "PYTHONPATH=%USERPROFILE%\Documents\Stock\.baostock_only;%PYTHONPATH%"
start "A股现货择时选股系统" "%PYW%" -X utf8 stock_selector\gui.pyw
