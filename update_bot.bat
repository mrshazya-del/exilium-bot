@echo off
title Exilium Bot Updater
cd /d "%~dp0"

echo =========================================================
echo    PULLING LATEST EXILIUM BOT UPDATES FROM GITHUB
echo =========================================================
echo.

where git >nul 2>nul
if %errorlevel% equ 0 (
    echo [*] Git detected. Pulling updates via Git...
    if not exist ".git" (
        git init
        git remote add origin https://github.com/mrshazya-del/exilium-bot.git
    ) else (
        git remote set-url origin https://github.com/mrshazya-del/exilium-bot.git
    )
    git pull origin main
) else (
    echo [*] Git not found. Downloading updates directly from GitHub Archive...
    python -c "import bot; print(bot.download_github_update_sync())"
)

echo.
echo [*] Checking dependencies...
python -m pip install -r requirements.txt --quiet

echo.
echo =========================================================
echo [OK] Update completed successfully!
echo =========================================================
pause
