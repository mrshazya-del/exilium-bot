@echo off
title Exilium AI Assistant 24/7 Service
cd /d "%~dp0"

echo =========================================================
echo    EXILIUM SILKROAD ONLINE - AI ASSISTANT 24/7 SERVICE
echo =========================================================
echo.

:: Detect Python executable
set "PY_CMD=python"
where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        set "PY_CMD=py"
    ) else (
        :: Check common Windows Python install locations
        for %%P in (
            "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
            "C:\Program Files\Python313\python.exe"
            "C:\Program Files\Python312\python.exe"
            "C:\Program Files\Python311\python.exe"
            "C:\Program Files\Python310\python.exe"
            "C:\Python313\python.exe"
            "C:\Python312\python.exe"
            "C:\Python311\python.exe"
            "C:\Python310\python.exe"
        ) do (
            if exist "%%~P" (
                set "PY_CMD=%%~P"
                goto :found_python
            )
        )

        echo =========================================================
        echo [X] خطأ: برنامج بايثون (Python) غير مثبت على هذا السيرفر!
        echo.
        echo يرجى تحميل وتثبيت بايثون من الرابط التالي:
        echo https://www.python.org/downloads/
        echo.
        echo [هام جدا]: في اول شاشة تثبيت، فعل الخيار بالاسفل:
        echo   [x] Add python.exe to PATH
        echo =========================================================
        echo.
        pause
        exit /b 1
    )
)
:found_python

:: Auto-pull latest updates from GitHub
where git >nul 2>nul
if %errorlevel% equ 0 (
    echo [*] Syncing latest updates from GitHub...
    git pull origin main
)

:: 1. Verify / Install requirements
echo [*] Python detected: %PY_CMD%
echo [*] Checking dependencies...
%PY_CMD% -m pip install -r requirements.txt --quiet

echo [*] Starting Exilium Bot...
echo.

:loop
%PY_CMD% bot.py
echo.
echo =========================================================
echo [*] Bot process exited or restarted at %date% %time%
echo [*] Restarting in 3 seconds...
echo =========================================================
timeout /t 3 /nobreak >nul
goto loop
