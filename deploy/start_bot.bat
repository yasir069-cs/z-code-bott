@echo off
REM Windows auto-start for the Crypto Signal Bot (your own PC, free forever).
REM Schedule via Task Scheduler:
REM   schtasks /create /tn "CryptoSignalBot" /tr "C:\Users\AAQIB\.zcode\workspace\default\deploy\start_bot.bat" /sc daily /st 18:25
REM (starts 5 min before the 18:30 IST session; bot sleeps by itself until then)

cd /d "C:\Users\AAQIB\.zcode\workspace\default"
".venv\Scripts\python.exe" main.py >> bot_boot.log 2>&1
