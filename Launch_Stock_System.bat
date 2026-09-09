@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%USERPROFILE%\Documents\Stock\.baostock_only" set "PYTHONPATH=%USERPROFILE%\Documents\Stock\.baostock_only;%PYTHONPATH%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%i"
set /p "ASOF=Screening date YYYY-MM-DD [%TODAY%]: "
if "%ASOF%"=="" set "ASOF=%TODAY%"
set /p "HOLDINGS=Optional holdings CSV path [leave blank for selection only]: "
if "%HOLDINGS%"=="" goto :NO_HOLDINGS
"%PY%" -X utf8 stock_selector\run_official_strategy.py --as-of "%ASOF%" --holdings "%HOLDINGS%"
goto :DONE
:NO_HOLDINGS
"%PY%" -X utf8 stock_selector\run_official_strategy.py --as-of "%ASOF%"
:DONE
pause
