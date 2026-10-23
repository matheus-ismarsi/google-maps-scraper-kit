@echo off
rem Instala (uma vez) a ponte do WhatsApp Web do CRM. Usa o Chrome ja instalado, sem baixar outro navegador.
cd /d "%~dp0wa-bridge"
where npm >nul 2>nul || (echo Instale o Node.js em https://nodejs.org e rode de novo. & pause & exit /b 1)
set PUPPETEER_SKIP_DOWNLOAD=true
call npm install --no-audit --no-fund
if errorlevel 1 (echo. & echo A instalacao falhou. & pause & exit /b 1)
echo.
echo Pronto! Feche o CRM (janela preta) e abra de novo pelo abrir-crm.bat.
pause
