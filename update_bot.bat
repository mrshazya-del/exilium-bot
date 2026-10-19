@echo off
title Exilium Bot Manual Updater
cd /d "%~dp0"

echo =========================================================
echo    PULLING LATEST EXILIUM BOT UPDATES FROM GITHUB
echo =========================================================
echo.

if not exist ".git" (
    echo [*] Initializing Git repository...
    git init
    git remote add origin https://github.com/mrshazya-del/exilium-bot.git
) else (
    git remote set-url origin https://github.com/mrshazya-del/exilium-bot.git
)

echo [*] Pulling updates from GitHub...
git pull origin main

echo.
echo [*] Dependencies check...
python -m pip install -r requirements.txt --quiet

echo.
echo =========================================================
echo [OK] Update completed successfully!
echo =========================================================
pause
