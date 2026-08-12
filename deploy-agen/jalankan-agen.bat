@echo off
title Agen Monitoring - PT Badak LNG (port 9091)

rem ============================================================
rem   ALAMAT DASHBOARD PUSAT  --  UBAH DI SINI bila IP berubah.
rem   Bisa juga pakai nama komputer: http://DESKTOP-22AN8KV:9090
set "DASH=http://10.10.88.144:9090"
rem ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 goto ELEVATE

echo [1/3] Membuka firewall port 9091 (TCP)...
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'Agen 9091' -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName 'Agen 9091' -Direction Inbound -LocalPort 9091 -Protocol TCP -Action Allow | Out-Null"
goto DOWNLOAD

:ELEVATE
echo Meminta izin Administrator (untuk membuka firewall port 9091)...
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if errorlevel 1 goto NOADMIN
exit /b

:NOADMIN
echo.
echo   Izin admin ditolak -- firewall TIDAK diubah, agen tetap dijalankan.
echo   Bila dashboard tak bisa membaca PC ini, minta IT membuka port 9091 (sekali saja).
echo.

:DOWNLOAD
echo [2/3] Mengunduh agen TERBARU dari %DASH% ...
powershell -NoProfile -Command "try { Invoke-WebRequest '%DASH%/agent.ps1' -OutFile '%TEMP%\Agent.ps1' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 (
  echo.
  echo   GAGAL mengunduh. Pastikan dashboard MENYALA di %DASH% dan jaringan sama.
  echo.
  pause
  exit /b
)

echo [3/3] Menjalankan agen di port 9091...
echo   Biarkan jendela ini TERBUKA. Menutup jendela = agen berhenti.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\Agent.ps1" -Port 9091 -CentralUrl "%DASH%"
pause
