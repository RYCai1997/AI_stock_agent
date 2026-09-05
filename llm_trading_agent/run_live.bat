@echo off
REM ============================================
REM  Mode 1: Live Paper Trading (every 4 hours)
REM ============================================
cd /d "%~dp0"
python main.py --mode live
pause
