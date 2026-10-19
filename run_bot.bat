@echo off
title Exilium AI Assistant 24/7 Service
cd /d "%~dp0"

echo =========================================================
echo    EXILIUM SILKROAD ONLINE - AI ASSISTANT 24/7 SERVICE
echo =========================================================
echo.

:: 1. Verify / Install requirements
echo [*] Checking dependencies...
python -m pip install -r requirements.txt --quiet

echo [*] Starting Exilium Bot...
echo.

:loop
python bot.py
echo.
echo =========================================================
echo [*] Bot process exited or restarted at %date% %time%
echo [*] Restarting in 3 seconds...
echo =========================================================
timeout /t 3 /nobreak >nul
goto loop
