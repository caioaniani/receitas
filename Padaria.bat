@echo off
chcp 65001 >nul
title Padaria Opao - Tela do padeiro

REM ============================================================
REM  Padaria Opão — Launcher da TV do padeiro (Windows 10)
REM
REM  O que este arquivo faz:
REM    1. Liga o teclado de toque (o grande, que sobe da barra)
REM       para aparecer ao tocar em qualquer campo de texto.
REM    2. Se registra para abrir junto com o Windows.
REM    3. Abre o Chrome em tela cheia (kiosk) na tela /padeiro.
REM
REM  Uso: deixe este arquivo numa pasta fixa (ex.: C:\Padaria\)
REM  e dê dois cliques UMA vez. Depois ele sobe sozinho no boot.
REM ============================================================

set "URL=https://gestao.opaopadariaartesanal.com.br/padeiro"

REM --- 1) Teclado de toque: aparecer ao tocar nos campos (modo desktop) ---
echo Ativando o teclado de toque...
reg add "HKCU\SOFTWARE\Microsoft\TabletTip\1.7" /v EnableDesktopModeAutoInvoke /t REG_DWORD /d 1 /f >nul

REM --- 2) Abrir junto com o Windows (atalho na pasta Inicializar) ---
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\Padaria.lnk"
if not exist "%LNK%" (
  echo Configurando para abrir junto com o Windows...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%'); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
)

REM --- 3) Deixar o teclado de toque pronto em segundo plano ---
start "" "%CommonProgramFiles%\microsoft shared\ink\TabTip.exe"

REM --- 4) Abrir o Chrome em tela cheia na /padeiro ---
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
  echo.
  echo  [AVISO] Chrome nao encontrado. Abrindo no navegador padrao...
  start "" "%URL%"
  goto :fim
)

echo Abrindo a tela do padeiro...
start "" "%CHROME%" --kiosk --noerrdialogs --disable-session-crashed-bubble --autoplay-policy=no-user-gesture-required "%URL%"

:fim
exit /b 0
