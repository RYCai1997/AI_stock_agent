@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM  AI Quant Trading Agent - GUI Launcher (double-click me)
REM  Finds a Python that actually has tkinter, then starts the
REM  GUI via pythonw (no console window). If anything fails you
REM  will see a visible error here instead of a silent no-op.
REM ============================================================

set "GUI=%~dp0launcher_gui.pyw"
if not exist "%GUI%" goto :NOGUI

REM --- Candidate 0: project venv (preferred — isolated deps from install_deps.bat) ---
call :TRY "%~dp0.venv\Scripts\pythonw.exe"
if not errorlevel 1 goto :LAUNCH

REM --- Candidate 1: Anaconda (bundles tkinter, verified on this PC) ---
call :TRY "D:\Software\Anaconda\pythonw.exe"
if not errorlevel 1 goto :LAUNCH

REM --- Candidate 2: any pythonw on PATH (probed for tkinter) ---
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    call :TRY "%%i"
    if not errorlevel 1 goto :LAUNCH
)

echo [ERROR] No Python with tkinter was found.
echo         Install Anaconda Python, then run install_deps.bat once.
echo         Details are saved to runs\launcher_error.log if the GUI itself crashed.
pause
exit /b 1

:LAUNCH
start "" "%PW_EXE%" "%GUI%"
endlocal
exit /b 0

:TRY
REM %1 = candidate pythonw.exe ; probe its sibling python.exe for tkinter
set "PW_EXE=%~1"
if not exist "%PW_EXE%" exit /b 1
set "P_EXE=%~dpn1.exe"
if not exist "%P_EXE%" exit /b 1
"%P_EXE%" -c "import tkinter" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

:NOGUI
echo [ERROR] launcher_gui.pyw is missing next to this script.
pause
exit /b 1
