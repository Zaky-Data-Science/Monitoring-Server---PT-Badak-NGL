<#
================================================================================
  Agent.ps1  -- AGEN METRIK (read-only), tanpa install apa pun.
  Dijalankan DI SERVER / LAPTOP yang mau dibaca CPU/RAM/Disk/Network-nya.
  Menyajikan angka resource sebagai JSON di sebuah port.

  Tahan banyak pengakses: metrik dihitung maks tiap ~2 detik lalu di-CACHE,
  jadi tiap request dilayani INSTAN (tidak macet walau banyak tab/pemakai).

  CARA JALANKAN (di mesin target):
     powershell -ExecutionPolicy Bypass -File .\Agent.ps1 -Port 9091
  Hanya MEMBACA angka. Tidak mengubah apa pun. Ctrl+C = berhenti.
  Kalau muncul popup Windows Firewall, klik "Allow".
================================================================================
#>
param([int]$Port = 9091, [string]$CentralUrl = "http://DESKTOP-22AN8KV:9090")

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $Port)
try { $listener.Start() } catch {
    Write-Host ("  GAGAL: port {0} sudah dipakai - agen lain mungkin sudah jalan di PC ini." -f $Port) -ForegroundColor Red
    Write-Host "  Tutup jendela agen yang lama dulu, lalu jalankan lagi (atau pakai -Port lain)." -ForegroundColor Yellow
    exit 1
}

# --- nilai STATIS (dibaca sekali) ---
$g_host  = $env:COMPUTERNAME
$g_cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
$g_ghz   = [math]::Round((Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty MaxClockSpeed) / 1000, 2)
$netPrev = @{ t = $null; rx = 0.0; tx = 0.0 }

# --- cache ---
$cacheJson = $null
$cacheTick = -100000
$sw = [System.Diagnostics.Stopwatch]::StartNew()

Write-Host "==================================================" -ForegroundColor Green
Write-Host "  AGEN METRIK berjalan di port $Port" -ForegroundColor Green
Write-Host "  Nama PC : $g_host  (cache 2 detik, tahan banyak akses)" -ForegroundColor Green
Write-Host "  Biarkan jendela ini TERBUKA. Ctrl+C untuk berhenti." -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green

# --- Daftar OTOMATIS ke dashboard pusat (biar tidak perlu ketik IP manual) ---
$myip = $null
try {
    $cfg = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
           Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1
    if ($cfg) { $myip = $cfg.IPv4Address.IPAddress }
} catch {}
if ($myip) {
    try {
        $body = @{ name = $g_host; ip = $myip; port = $Port } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$CentralUrl/api/targets/add" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 5 | Out-Null
        Write-Host "  Terdaftar OTOMATIS ke dashboard: $CentralUrl  (nama: $g_host, ip: $myip)" -ForegroundColor Cyan
    } catch {
        Write-Host "  (Info) Belum bisa daftar ke dashboard pusat ($CentralUrl). Agen tetap jalan lokal." -ForegroundColor Yellow
    }
} else {
    Write-Host "  (Info) IP intranet tidak terdeteksi - lewati auto-daftar. Agen tetap jalan." -ForegroundColor Yellow
}

while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
        $client.LingerState = New-Object System.Net.Sockets.LingerOption($true, 1)
        $stream = $client.GetStream()
        $reqPath = "/"
        try {
            $stream.ReadTimeout = 300; $rbuf = New-Object byte[] 2048
            $read = $stream.Read($rbuf, 0, $rbuf.Length)
            $reqLine = [System.Text.Encoding]::ASCII.GetString($rbuf, 0, $read)
            if ($reqLine -match 'GET\s+(\S+)') { $reqPath = $matches[1] }
        } catch {}

        # hitung ulang metrik HANYA kalau cache sudah basi (>2 detik) -> request lain dilayani instan
        if ($null -eq $cacheJson -or ($sw.ElapsedMilliseconds - $cacheTick) -gt 2000) {
            $os   = Get-CimInstance Win32_OperatingSystem
            $cpu  = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
            if ($null -eq $cpu) { $cpu = 0 }
            $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"

            $rxB = 0.0; $txB = 0.0
            foreach ($s in (Get-NetAdapterStatistics -ErrorAction SilentlyContinue)) { $rxB += [double]$s.ReceivedBytes; $txB += [double]$s.SentBytes }
            $now = Get-Date; $rx = 0.0; $tx = 0.0
            if ($null -ne $netPrev.t) {
                $dt = ($now - $netPrev.t).TotalSeconds
                if ($dt -gt 0) { $rx = ($rxB - $netPrev.rx) * 8 / 1000 / $dt; $tx = ($txB - $netPrev.tx) * 8 / 1000 / $dt }
            }
            $netPrev.t = $now; $netPrev.rx = $rxB; $netPrev.tx = $txB

            $totalMB = [math]::Round($os.TotalVisibleMemorySize / 1024, 0)
            $usedMB  = $totalMB - [math]::Round($os.FreePhysicalMemory / 1024, 0)
            $memPct  = if ($totalMB -gt 0) { [math]::Round($usedMB / $totalMB * 100, 1) } else { 0 }
            $diskPct = if ($disk.Size -gt 0) { [math]::Round(($disk.Size - $disk.FreeSpace) / $disk.Size * 100, 1) } else { 0 }

            $data = @{
                hostname      = $g_host
                cpu_percent   = [double]$cpu
                cpu_cores     = $g_cores
                cpu_ghz       = $g_ghz
                mem_percent   = $memPct
                mem_used_gb   = [math]::Round($usedMB / 1024, 2)
                mem_total_gb  = [math]::Round($totalMB / 1024, 2)
                disk_percent  = $diskPct
                disk_used_gb  = [math]::Round(($disk.Size - $disk.FreeSpace) / 1GB, 1)
                disk_total_gb = [math]::Round($disk.Size / 1GB, 1)
                rx_kbps       = [math]::Round([math]::Max($rx, 0), 0)
                tx_kbps       = [math]::Round([math]::Max($tx, 0), 0)
                uptime_sec    = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalSeconds, 0)
            }
            $cacheJson = $data | ConvertTo-Json -Compress
            $cacheTick = $sw.ElapsedMilliseconds
        }

        # tentukan isi respons: metrik (default) ATAU daftar aplikasi terpasang (/apps)
        $respJson = $cacheJson
        if ($reqPath -like '/apps*') {
            $appList = @()
            foreach ($rp in @(
                'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
                'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
                try { $appList += (Get-ItemProperty $rp -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName } | Select-Object -ExpandProperty DisplayName) } catch {}
            }
            $respJson = @{ hostname = $g_host; apps = @($appList | Sort-Object -Unique) } | ConvertTo-Json -Compress
        }
        elseif ($reqPath -like '/check*') {
            # cek status SERVICE & PORT kritis (read-only). Contoh:
            #   /check?services=MSSQLSERVER,Exaquantum&ports=1433,80
            $svcNames = @(); $portNums = @()
            $q = ''
            if ($reqPath -match '\?(.*)$') { $q = $matches[1] }
            foreach ($pair in ($q -split '&')) {
                $kv = $pair -split '=', 2
                if ($kv.Count -eq 2) {
                    $key = $kv[0]; $val = [System.Uri]::UnescapeDataString($kv[1])
                    if ($key -eq 'services' -and $val) { $svcNames = @($val -split ',' | Where-Object { $_ }) }
                    elseif ($key -eq 'ports' -and $val) { $portNums = @($val -split ',' | Where-Object { $_ }) }
                }
            }
            $svcRes = @()
            foreach ($n in $svcNames) {
                $nm = "$n".Trim(); if (-not $nm) { continue }
                $svc = Get-Service -Name $nm -ErrorAction SilentlyContinue
                if (-not $svc) { $svc = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "*$nm*" } | Select-Object -First 1 }
                if ($svc) { $st = "$($svc.Status)".ToUpper() } else { $st = 'NOT_FOUND' }
                $svcRes += @{ name = $nm; status = $st; running = ($st -eq 'RUNNING') }
            }
            $portRes = @()
            foreach ($p in $portNums) {
                $pn = 0; [int]::TryParse("$p".Trim(), [ref]$pn) | Out-Null
                if ($pn -le 0) { continue }
                $listen = $false
                try {
                    if (Get-NetTCPConnection -LocalPort $pn -State Listen -ErrorAction SilentlyContinue) { $listen = $true }
                } catch {
                    try { if (netstat -an | Select-String ":$pn\s" | Select-String 'LISTENING') { $listen = $true } } catch {}
                }
                $pst = if ($listen) { 'LISTENING' } else { 'CLOSED' }
                $portRes += @{ port = $pn; status = $pst; listening = $listen }
            }
            $respJson = @{ hostname = $g_host; services = @($svcRes); ports = @($portRes) } | ConvertTo-Json -Compress -Depth 4
        }
        elseif ($reqPath -like '/top*') {
            # proses pemakai RAM/CPU tertinggi (read-only, mirip Task Manager)
            $procs = Get-Process -ErrorAction SilentlyContinue
            $byRam = @($procs | Sort-Object WorkingSet64 -Descending | Select-Object -First 8 |
                ForEach-Object { @{ name = $_.ProcessName; ram_mb = [math]::Round($_.WorkingSet64/1MB,1); cpu_s = [math]::Round(([double]$_.CPU),0) } })
            $byCpu = @($procs | Where-Object { $_.CPU } | Sort-Object CPU -Descending | Select-Object -First 8 |
                ForEach-Object { @{ name = $_.ProcessName; ram_mb = [math]::Round($_.WorkingSet64/1MB,1); cpu_s = [math]::Round(([double]$_.CPU),0) } })
            $respJson = @{ hostname = $g_host; by_ram = @($byRam); by_cpu = @($byCpu) } | ConvertTo-Json -Compress -Depth 4
        }
        elseif ($reqPath -like '/sites*') {
            # riwayat browser: kumpulkan DOMAIN yang pernah dibuka (read-only). Dashboard yang menandai berbahaya.
            $domains = New-Object System.Collections.Generic.HashSet[string]
            $files = @()
            foreach ($b in @(
                "$env:LOCALAPPDATA\Google\Chrome\User Data",
                "$env:LOCALAPPDATA\Microsoft\Edge\User Data",
                "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data",
                "$env:APPDATA\Opera Software\Opera Stable",
                "$env:APPDATA\Opera Software\Opera GX Stable")) {
                if (Test-Path $b) { $files += Get-ChildItem -Path $b -Recurse -Filter 'History' -Depth 2 -File -ErrorAction SilentlyContinue }
            }
            $ff = "$env:APPDATA\Mozilla\Firefox\Profiles"
            if (Test-Path $ff) { $files += Get-ChildItem -Path $ff -Recurse -Filter 'places.sqlite' -Depth 2 -File -ErrorAction SilentlyContinue }
            $rx = [regex]'https?://([a-zA-Z0-9\.\-]{3,120})'
            foreach ($f in ($files | Select-Object -First 12)) {
                if ($f.Length -gt 150MB) { continue }
                try {
                    $fs = [System.IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite')
                    $sr = New-Object System.IO.StreamReader($fs)
                    $text = $sr.ReadToEnd(); $sr.Close(); $fs.Close()
                    foreach ($m in $rx.Matches($text)) {
                        $d = $m.Groups[1].Value.ToLower().Trim('.')
                        if ($d) { [void]$domains.Add($d) }
                    }
                } catch {}
            }
            $list = @($domains) | Sort-Object -Unique | Select-Object -First 1200
            $respJson = @{ hostname = $g_host; domains = @($list); total = $list.Count } | ConvertTo-Json -Compress -Depth 3
        }

        $body    = [System.Text.Encoding]::UTF8.GetBytes($respJson)
        $headers = "HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nAccess-Control-Allow-Origin: *`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
        $hbytes  = [System.Text.Encoding]::ASCII.GetBytes($headers)
        $stream.Write($hbytes, 0, $hbytes.Length)
        $stream.Write($body, 0, $body.Length)
        $stream.Flush()
    } catch {}
    finally { $client.Close() }
}
