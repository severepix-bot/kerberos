@echo off
cd /d "%~dp0"
if /I "%~1"=="remoto" goto remote
call iniciar.bat %*
goto end
:remote
py -3 -m assistente remoto
:end
