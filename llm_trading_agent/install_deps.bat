@echo off
REM ============================================
REM  Install dependencies (first run only)
REM  Double-click this file before first use.
REM  Installs into the project venv (.venv) so the
REM  Anaconda base environment is never touched.
REM ============================================
cd /d "%~dp0"

REM --- Prefer the project venv; otherwise fall back to PATH python ---
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
    echo Using project venv: .venv\Scripts\python.exe
) else (
    set "PY=python"
    echo No .venv found - using "python" from PATH.
)

"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :ERR

echo.
echo Done. Double-click Launch_Agent.bat to open the console.
pause
exit /b 0

:ERR
echo.
echo [ERROR] Dependency installation failed.
echo         If you see proxy/network errors, retry or set a mirror first:
echo         %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pause
exit /b 1
