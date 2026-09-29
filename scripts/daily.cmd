@echo off
rem Daily automation cycle for the Alpaca PAPER account (called by Windows Task Scheduler).
cd /d "%~dp0..\backend"
if not exist "..\logs" mkdir "..\logs"
echo ==== %date% %time% ==== >> "..\logs\daily.log"
".venv\Scripts\python.exe" -m app daily --trigger schedule >> "..\logs\daily.log" 2>&1
