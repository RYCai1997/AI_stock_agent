@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto :ERROR
".venv\Scripts\python.exe" -m pip install -r stock_selector\requirements.txt
if errorlevel 1 goto :ERROR
echo Installation completed. Run Launch_Stock_System.bat.
pause
exit /b 0
:ERROR
echo Installation failed. Check Python and network access.
pause
exit /b 1
