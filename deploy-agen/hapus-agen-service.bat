@echo off
title Hapus Agen Service - PT Badak LNG
set "PS=%~dp0pasang-agen-service.ps1"

net session >nul 2>&1
if %errorlevel% neq 0 goto ELEV
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%" -Uninstall
exit /b

:ELEV
echo Meminta izin Administrator...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%PS%\" -Uninstall'"
exit /b
