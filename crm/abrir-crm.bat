@echo off
rem Dois cliques: sobe o CRM local e abre no navegador. Feche esta janela para parar.
cd /d "%~dp0"
python server.py %*
if errorlevel 1 pause
