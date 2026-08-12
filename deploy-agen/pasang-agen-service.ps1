<#
================================================================================
  pasang-agen-service.ps1
  Pasang AGEN MONITORING sebagai Scheduled Task pada SERVER:
    - jalan sebagai SYSTEM, otomatis START saat BOOT (tanpa perlu login)
    - TERSEMBUNYI (tanpa jendela)
    - RESTART otomatis bila proses crash
    - TANPA batas waktu (default Windows membunuh task setelah 3 hari -> dimatikan)
  Semua tetap READ-ONLY (non-interference). Wajib dijalankan sebagai Administrator.

  Pasang : powershell -ExecutionPolicy Bypass -File .\pasang-agen-service.ps1
  Hapus  : powershell -ExecutionPolicy Bypass -File .\pasang-agen-service.ps1 -Uninstall
  (atau pakai pasang-agen-service.bat / hapus-agen-service.bat -- dobel-klik)
================================================================================
#>
param(
  [string]$Dash       = "http://10.10.88.144:9090",     # alamat dashboard pusat (UBAH bila IP berubah)
  [int]   $Port       = 9091,
  [string]$InstallDir = "C:\ProgramData\BadakMonitor",  # folder tetap tempat agen disimpan
  [string]$TaskName   = "BadakMonitor-Agen",
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

function Assert-Admin {
  if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')) {
    Write-Host "  HARUS dijalankan sebagai Administrator." -ForegroundColor Red
    Read-Host "Tekan Enter untuk menutup"; exit 1
  }
}

Assert-Admin

if ($Uninstall) {
  Write-Host "== MENGHAPUS Agen Service ==" -ForegroundColor Cyan
  try { Stop-ScheduledTask       -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
  try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
  Remove-NetFirewallRule -DisplayName "Agen $Port" -ErrorAction SilentlyContinue
  try { Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue } catch {}
  Write-Host "  Selesai: task, rule firewall, dan folder dihapus." -ForegroundColor Green
  Read-Host "Tekan Enter untuk menutup"; exit 0
}

Write-Host "== PEMASANGAN Agen Monitoring sebagai Scheduled Task ==" -ForegroundColor Cyan
Write-Host "  Dashboard : $Dash"
Write-Host "  Folder    : $InstallDir"
Write-Host "  Task      : $TaskName  (SYSTEM, saat boot, tersembunyi)"
Write-Host ""

# 1) folder + unduh agen TERBARU dari dashboard
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$agentPath = Join-Path $InstallDir "Agent.ps1"
Write-Host "[1/4] Mengunduh agen terbaru dari dashboard..."
try {
  Invoke-WebRequest "$Dash/agent.ps1" -OutFile $agentPath -UseBasicParsing -TimeoutSec 15
} catch {
  Write-Host "  GAGAL unduh dari $Dash. Pastikan dashboard MENYALA & jaringan sama." -ForegroundColor Red
  Read-Host "Tekan Enter untuk menutup"; exit 1
}

# 2) firewall (sekali; menetap)
Write-Host "[2/4] Membuka firewall port $Port (TCP)..."
Remove-NetFirewallRule -DisplayName "Agen $Port" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "Agen $Port" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow | Out-Null

# 3) daftarkan Scheduled Task
Write-Host "[3/4] Mendaftarkan Scheduled Task..."
$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$agentPath`" -Port $Port -CentralUrl `"$Dash`""
$action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
$trigger   = New-ScheduledTaskTrigger -AtStartup
try { $trigger.Delay = "PT30S" } catch {}   # jeda 30 dtk agar jaringan siap saat boot
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
             -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Description "Agen Monitoring PT Badak NGL (read-only, auto-start)" | Out-Null

# 4) jalankan sekarang + verifikasi
Write-Host "[4/4] Menjalankan agen sekarang..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4
$state = (Get-ScheduledTask -TaskName $TaskName).State
$ok = $false
try { $ok = [bool](Invoke-WebRequest "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 5) } catch {}

Write-Host ""
if ($ok) { Write-Host "SELESAI [OK]  Task=$state, agen membalas di http://localhost:$Port" -ForegroundColor Green }
else     { Write-Host "Task=$state, tapi agen belum membalas (beri beberapa detik lalu cek http://localhost:$Port)" -ForegroundColor Yellow }
Write-Host "  - Auto-start tiap boot sebagai SYSTEM, tersembunyi, restart bila crash, tanpa batas waktu."
Write-Host "  - Kelola: Task Scheduler -> '$TaskName'.   Hapus: hapus-agen-service.bat"
Read-Host "Tekan Enter untuk menutup"
