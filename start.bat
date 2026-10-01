@echo off
chcp 65001 >nul
title Arb Bot - srpske kladionice
cd /d "%~dp0"

echo ============================================
echo    ARB BOT - srpske kladionice
echo ============================================
echo.

rem --- Python ---
where python >nul 2>nul
if errorlevel 1 (
    echo [GRESKA] Python nije instaliran ili nije u PATH-u.
    echo Instaliraj ga sa https://www.python.org/downloads/ ^(stikliraj "Add to PATH"^).
    pause
    exit /b 1
)

rem --- .env ---
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [!] Napravljen je fajl .env - upisi TELEGRAM_TOKEN i sacuvaj.
    notepad ".env"
)

rem --- biblioteke ---
echo Proveravam biblioteke...
python -m pip install -q -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo [GRESKA] Instalacija biblioteka nije uspela. Proveri internet konekciju.
    pause
    exit /b 1
)
echo OK.
echo.

rem --- bot, sa automatskim restartom ako padne ---
:loop
echo [%date% %time%] Pokrecem bota... ^(zatvori prozor ili Ctrl+C za gasenje^)
echo.
python bot.py
set CODE=%errorlevel%

if %CODE%==0 (
    echo Bot je ugasen.
    goto end
)
if %CODE%==2 (
    echo.
    echo [GRESKA] Podesavanja nisu dobra - proveri fajl .env
    notepad ".env"
    goto end
)

echo.
echo [!] Bot je pao ^(kod %CODE%^). Ponovo pokrecem za 10 sekundi...
timeout /t 10 /nobreak >nul
goto loop

:end
pause
