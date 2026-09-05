@echo off
REM ============================================
REM  Mode 2: One-shot Backtest since START_DATE
REM ============================================
cd /d "%~dp0"
python main.py --mode backtest
pause
