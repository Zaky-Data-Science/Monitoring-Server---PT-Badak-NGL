# -*- coding: utf-8 -*-
"""
================================================================================
  dashboard_server.py
  Dashboard Monitoring Server (read-only) -- PT Badak NGL / prototipe PKL
  Menampilkan resource laptop ini (CPU/RAM/Disk/Network) + cek konektivitas
  server pabrik (ONLINE/OFFLINE + latency), refresh otomatis. Non-Interference:
  hanya MEMBACA, tidak pernah mengubah apa pun di server mana pun.

  CARA JALANKAN:
     python dashboard_server.py
  Lalu buka di Chrome:  http://localhost:9090

  Butuh: Python 3 + psutil  (pip install psutil)
  Daftar server diatur di file targets.json (satu folder dengan file ini).
================================================================================
"""
import csv
import datetime
import json
import os
import re
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import psutil

try:
    import oracledb
    ORACLE_AVAILABLE = True
except ImportError:
    ORACLE_AVAILABLE = False

HOST = "0.0.0.0"          # 0.0.0.0 = bisa diakses dari perangkat lain di jaringan; ganti "127.0.0.1" utk lokal saja
PORT = 9090
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_INTERVAL = 10                              # detik antar pencatatan ke file
LOG_DIR = os.path.join(BASE_DIR, "logs")       # folder tempat CSV riwayat disimpan

# ---- Konfigurasi Oracle (ganti ini ke Oracle kantor nanti) -------------------
ORACLE_ENABLED  = True
ORACLE_USER     = "monitoring"
ORACLE_PASSWORD = "Monitor_2026#"
ORACLE_DSN      = "localhost:1521/XEPDB1"
ORACLE_INTERVAL = 10                           # detik antar simpan ke Oracle
AGENT_PORT_DEFAULT = 9091
SECURITY_INTERVAL = 300                         # detik antar pindai aplikasi (penanda 🛡️); apps jarang berubah

# cache status keamanan per server (diisi worker latar) -> dipakai penanda 🛡️ di kartu
# { nama_server: {"count": int, "flagged": [{name,kategori}], "checked": "HH:MM:SS", "keys": set()} }
_sec_cache = {}

CRITICAL_INTERVAL = 60     # detik antar cek service/port kritis (lebih responsif dari scan aplikasi)
ORACLE_RETENTION_DAYS = 90 # simpan metrik server_monitoring N hari terakhir (sisanya auto-hapus)
# cache status layanan/port kritis per server
# { nama_server: {"down": n, "total": n, "items": [...], "checked": "HH:MM:SS", "downkeys": set()} }
_crit_cache = {}

# ---- state untuk hitung laju jaringan (Kbps) ---------------------------------
_prev_net = {"t": None, "sent": 0, "recv": 0}

# prime CPU percent supaya panggilan pertama tidak 0
psutil.cpu_percent(interval=None)


def load_targets():
    """Baca daftar server dari targets.json (kalau gagal, kembalikan list kosong)."""
    try:
        with open(os.path.join(BASE_DIR, "targets.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("targets", [])
    except Exception:
        return []


def save_targets(targets):
    """Simpan daftar server ke targets.json (atomik: tulis ke .tmp lalu ganti)."""
    path = os.path.join(BASE_DIR, "targets.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"targets": targets}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    # setiap perubahan daftar server/label ikut disinkronkan ke tabel server_label
    # di Oracle (dipakai query SQL Developer untuk menampilkan nama panggilan)
    try:
        sync_labels_to_oracle()
    except Exception:
        pass


def label_for(name):
    """Ambil nama panggilan (label) sebuah server dari targets.json ('' kalau tak ada)."""
    for t in load_targets():
        if str(t.get("name", "")).strip().lower() == str(name).strip().lower():
            return str(t.get("label", "") or "").strip()
    return ""


def get_local_metrics():
    """Ambil metrik resource laptop ini via psutil (read-only)."""
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    # drive sistem (Windows biasanya C:\, dibuat aman untuk OS lain)
    disk_path = "C:\\" if os.name == "nt" else "/"
    disk = psutil.disk_usage(disk_path)

    net = psutil.net_io_counters()
    now = time.time()
    rx_kbps = tx_kbps = 0.0
    if _prev_net["t"] is not None:
        dt = now - _prev_net["t"]
        if dt > 0:
            rx_kbps = (net.bytes_recv - _prev_net["recv"]) * 8 / 1000.0 / dt
            tx_kbps = (net.bytes_sent - _prev_net["sent"]) * 8 / 1000.0 / dt
    _prev_net.update({"t": now, "sent": net.bytes_sent, "recv": net.bytes_recv})

    freq = psutil.cpu_freq()
    uptime_sec = int(now - psutil.boot_time())
    try:
        load1 = os.getloadavg()[0]          # tidak ada di Windows
    except (AttributeError, OSError):
        load1 = round(cpu / 100.0 * psutil.cpu_count(), 2)

    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": socket.gethostname(),
        "cpu_percent": round(cpu, 1),
        "cpu_cores": psutil.cpu_count(logical=True),
        "cpu_ghz": round(freq.current / 1000.0, 2) if freq else 0,
        "mem_percent": round(mem.percent, 1),
        "mem_used_gb": round(mem.used / (1024 ** 3), 2),
        "mem_total_gb": round(mem.total / (1024 ** 3), 2),
        "disk_percent": round(disk.percent, 1),
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "rx_kbps": round(max(rx_kbps, 0), 0),
        "tx_kbps": round(max(tx_kbps, 0), 0),
        "uptime_sec": uptime_sec,
        "load1": load1,
    }


def check_target(t):
    """Cek 1 server: TCP connect + ukur latency. Read-only, tidak mengubah apa pun."""
    ip, port = t.get("ip", ""), int(t.get("port", 0))
    start = time.time()
    online, latency = False, 0.0
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        online = (s.connect_ex((ip, port)) == 0)
        latency = round((time.time() - start) * 1000, 1)
        s.close()
    except Exception:
        online = False
    sec = _sec_cache.get(t.get("name", ip))
    crit = _crit_cache.get(t.get("name", ip))
    return {
        "name": t.get("name", ip),
        "label": str(t.get("label", "") or "").strip(),
        "ip": ip,
        "port": port,
        "status": "ONLINE" if online else "OFFLINE",
        "latency_ms": latency if online else None,
        # penanda keamanan (None = belum dipindai; 0 = bersih; >0 = ada aplikasi ter-flag)
        "flagged_count": (sec.get("count") if sec else None),
        "flagged": (sec.get("flagged") if sec else []),
        # layanan/port kritis (None = belum dicek; 0 = semua hidup; >0 = ada yang mati)
        "critical_down": (crit.get("down") if crit else None),
        "critical_total": (crit.get("total") if crit else None),
    }


def get_targets_status():
    targets = load_targets()
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(check_target, targets))


def _fmt_cell(v):
    """Excel Indonesia pakai koma sebagai desimal -> ubah titik jadi koma."""
    if isinstance(v, float):
        return str(v).replace(".", ",")
    return v


def _append_csv(path, header, row):
    """Tambah 1 baris ke CSV. Pakai pemisah ';' + desimal koma supaya rapi di Excel Indonesia."""
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        if is_new:
            f.write("﻿")      # BOM sekali di awal, bantu Excel kenali encoding
        w = csv.writer(f, delimiter=";")
        if is_new:
            f.write("sep=;\r\n")   # petunjuk pemisah kolom untuk Excel (locale apa pun)
            w.writerow(header)
        w.writerow([_fmt_cell(x) for x in row])


RETENTION_DAYS = 14   # hapus otomatis file log lebih tua dari ini (biar tidak numpuk)


def cleanup_old_logs():
    """Hapus file CSV di folder logs yang lebih tua dari RETENTION_DAYS."""
    try:
        cutoff = time.time() - RETENTION_DAYS * 86400
        for f in os.listdir(LOG_DIR):
            p = os.path.join(LOG_DIR, f)
            if f.endswith(".csv") and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


def log_worker():
    """Berjalan di background: rekam metrik + status server ke CSV harian tiap LOG_INTERVAL detik."""
    os.makedirs(LOG_DIR, exist_ok=True)
    cleanup_old_logs()   # bersihkan log lama saat mulai
    net_state = {"t": None, "sent": 0, "recv": 0}
    while True:
        try:
            now = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            datestr = time.strftime("%Y%m%d")

            # --- resource laptop ---
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
            net = psutil.net_io_counters()
            rx = tx = 0.0
            if net_state["t"] is not None:
                dt = now - net_state["t"]
                if dt > 0:
                    rx = (net.bytes_recv - net_state["recv"]) * 8 / 1000.0 / dt
                    tx = (net.bytes_sent - net_state["sent"]) * 8 / 1000.0 / dt
            net_state.update({"t": now, "sent": net.bytes_sent, "recv": net.bytes_recv})

            _append_csv(
                os.path.join(LOG_DIR, f"resource_{datestr}.csv"),
                ["timestamp", "hostname", "cpu_percent", "mem_percent",
                 "mem_used_gb", "disk_percent", "rx_kbps", "tx_kbps"],
                [ts, socket.gethostname(), round(cpu, 1), round(mem.percent, 1),
                 round(mem.used / (1024 ** 3), 2), round(disk.percent, 1),
                 round(max(rx, 0), 0), round(max(tx, 0), 0)],
            )

            # --- status tiap server target ---
            for t in get_targets_status():
                _append_csv(
                    os.path.join(LOG_DIR, f"targets_{datestr}.csv"),
                    ["timestamp", "name", "ip", "port", "status", "latency_ms"],
                    [ts, t["name"], t["ip"], t["port"], t["status"],
                     t["latency_ms"] if t["latency_ms"] is not None else ""],
                )
        except Exception:
            pass  # jangan sampai error kecil menghentikan pencatatan
        time.sleep(LOG_INTERVAL)


def fetch_agent_metrics(ip, port, timeout=8):
    """Ambil metrik dari agen (read-only). Kembalikan dict, atau None kalau gagal."""
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and "cpu_percent" in data:
            return data
    except Exception:
        pass
    return None


def load_watchlist():
    try:
        with open(os.path.join(BASE_DIR, "watchlist.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def log_detection(host, ip, flagged):
    """Catat daftar aplikasi ter-flag ke Oracle (audit trail deteksi_aplikasi)."""
    if not (ORACLE_ENABLED and ORACLE_AVAILABLE and flagged):
        return
    try:
        conn = get_oracle_conn()
        cur = conn.cursor()
        now = datetime.datetime.now()
        cur.executemany(
            "INSERT INTO deteksi_aplikasi (waktu, server, ip, aplikasi, kategori) VALUES (:1,:2,:3,:4,:5)",
            [(now, host, ip, str(f["name"])[:300], str(f["kategori"])[:80]) for f in flagged])
        conn.commit()
    except Exception:
        _oracle["conn"] = None


def check_apps(ip, port, log=True):
    """Ambil daftar aplikasi dari agen, tandai yang cocok watchlist.
    log=True -> catat temuan ke Oracle (dipakai saat pengguna klik 'Cek aplikasi').
    log=False -> hanya baca (dipakai worker keamanan latar untuk penanda 🛡️).
    """
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/apps", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "agen tidak terjangkau / belum jalan"}
    apps = data.get("apps", []) or []
    if isinstance(apps, str):
        apps = [apps]
    wl = load_watchlist()
    desc = wl.get("_deskripsi", {}) or {}
    flagged = []
    for app in apps:
        low = str(app).lower()
        for cat, keywords in wl.items():
            if cat.startswith("_"):
                continue
            for kw in keywords:
                if str(kw).lower() in low:
                    flagged.append({"name": app, "kategori": cat, "cocok": kw, "alasan": desc.get(cat, "")})
                    break
            else:
                continue
            break
    host = data.get("hostname", ip)
    if log:
        log_detection(host, ip, flagged)
    return {"ok": True, "hostname": host, "total": len(apps),
            "flagged": flagged, "apps": sorted(apps, key=lambda x: str(x).lower())}


# ================= Pemantauan SERVICE & PORT KRITIS (Exaquantum/SQL/historian) =================
def load_critical():
    """Baca critical.json. Fallback aman kalau file tak ada."""
    try:
        with open(os.path.join(BASE_DIR, "critical.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"default": {"services": [], "ports": []}, "per_server": {}}


def checks_for_server(name):
    """Daftar service & port kritis untuk 1 server (per_server menimpa default)."""
    cfg = load_critical()
    per = cfg.get("per_server") or {}
    entry = None
    for k, v in per.items():
        if str(k).strip().lower() == str(name).strip().lower():
            entry = v
            break
    if entry is None:
        entry = cfg.get("default") or {}
    return (entry.get("services") or []), (entry.get("ports") or [])


def check_critical(name, ip, port):
    """Tanya agen status service/port kritis. Kembalikan ringkasan + rincian ber-label."""
    services, ports = checks_for_server(name)
    if not services and not ports:
        return {"ok": True, "hostname": name, "total": 0, "down": 0, "items": [], "no_config": True}
    svc_q = ",".join(str(s.get("name", "")).strip() for s in services if s.get("name"))
    port_q = ",".join(str(p.get("port", "")).strip() for p in ports if p.get("port"))
    url = f"http://{ip}:{port}/check?services={quote(svc_q)}&ports={quote(port_q)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "agen tidak terjangkau / belum jalan"}
    if "services" not in data and "ports" not in data:
        return {"ok": False, "error": "agen belum mendukung /check - perbarui Agent.ps1 di server itu"}
    svc_label = {str(s.get("name", "")).strip().lower(): (s.get("label") or s.get("name")) for s in services}
    port_label = {str(p.get("port")): (p.get("label") or f"Port {p.get('port')}") for p in ports}
    items = []
    for s in (data.get("services") or []):
        nm = str(s.get("name", "")); st = str(s.get("status", "?")).upper()
        ok = bool(s.get("running")) or st == "RUNNING"
        items.append({"jenis": "SERVICE", "nama": nm, "label": svc_label.get(nm.lower(), nm), "status": st, "ok": ok})
    for p in (data.get("ports") or []):
        pn = str(p.get("port")); st = str(p.get("status", "?")).upper()
        ok = bool(p.get("listening")) or st == "LISTENING"
        items.append({"jenis": "PORT", "nama": pn, "label": port_label.get(pn, f"Port {pn}"), "status": st, "ok": ok})
    down = [it for it in items if not it["ok"]]
    return {"ok": True, "hostname": data.get("hostname", name), "total": len(items), "down": len(down), "items": items}


def ensure_critical_table():
    """Buat tabel status_layanan bila belum ada (abaikan bila sudah ada)."""
    if not (ORACLE_ENABLED and ORACLE_AVAILABLE):
        return
    ddl = ("CREATE TABLE status_layanan (waktu TIMESTAMP, server VARCHAR2(120), ip VARCHAR2(45), "
           "jenis VARCHAR2(10), nama VARCHAR2(160), status VARCHAR2(20), ok NUMBER(1))")
    try:
        cur = get_oracle_conn().cursor()
        try:
            cur.execute(ddl); _oracle["conn"].commit()
            print("  [Kritis] tabel status_layanan dibuat.")
        except oracledb.DatabaseError as e:
            if "ORA-00955" not in str(e):   # 00955 = nama sudah dipakai -> aman
                raise
    except Exception as e:
        print("  [Kritis] gagal siapkan tabel:", str(e)[:120])


def log_status_change(server, ip, changed):
    """Catat PERUBAHAN status layanan/port ke Oracle (audit + alarm)."""
    if not (ORACLE_ENABLED and ORACLE_AVAILABLE and changed):
        return
    try:
        conn = get_oracle_conn(); cur = conn.cursor(); now = datetime.datetime.now()
        cur.executemany(
            "INSERT INTO status_layanan (waktu, server, ip, jenis, nama, status, ok) VALUES (:1,:2,:3,:4,:5,:6,:7)",
            [(now, str(server)[:120], str(ip)[:45], it["jenis"], str(it["nama"])[:160],
              str(it["status"])[:20], 1 if it["ok"] else 0) for it in changed])
        conn.commit()
    except Exception:
        _oracle["conn"] = None


# ================= Riwayat & Tren + % Uptime (dari server_monitoring) =================
# rentang -> (batas waktu SQL, ukuran bucket menit). Nilai terkontrol (bukan input user) -> aman.
HISTORY_RANGES = {
    "3h":  ("SYSDATE - INTERVAL '3' HOUR", 10),
    "24h": ("SYSDATE - INTERVAL '1' DAY",  60),
    "7d":  ("SYSDATE - INTERVAL '7' DAY",  360),
    "30d": ("SYSDATE - INTERVAL '30' DAY", 1440),
}


def get_history(server, rng):
    """Riwayat metrik + % uptime + downtime untuk 1 server pada rentang tertentu."""
    if not (ORACLE_ENABLED and ORACLE_AVAILABLE):
        return {"ok": False, "error": "Oracle nonaktif"}
    if not server:
        return {"ok": False, "error": "server kosong"}
    if rng not in HISTORY_RANGES:
        rng = "24h"
    cutoff, n = HISTORY_RANGES[rng]
    try:
        conn = get_oracle_conn(); cur = conn.cursor()
        # ---- ringkasan (dari data mentah) ----
        cur.execute(f"""
            SELECT COUNT(*), SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END),
                   ROUND(AVG(cpu_pct),1), ROUND(MAX(cpu_pct),1), ROUND(AVG(mem_pct),1), ROUND(MAX(mem_pct),1),
                   ROUND(MAX(disk_pct),1), ROUND(AVG(latency_ms),1),
                   TO_CHAR(MIN(waktu),'YYYY-MM-DD HH24:MI'), TO_CHAR(MAX(waktu),'YYYY-MM-DD HH24:MI')
            FROM server_monitoring WHERE nama_server=:s AND waktu > {cutoff}
        """, {"s": server})
        row = cur.fetchone()
        total = row[0] or 0
        if not total:
            return {"ok": True, "server": server, "range": rng, "empty": True}
        online = row[1] or 0
        summary = {
            "samples": total, "uptime_pct": round(100.0 * online / total, 2),
            "offline_samples": total - online,
            "offline_min_est": round((total - online) * ORACLE_INTERVAL / 60.0, 1),
            "cpu_avg": row[2], "cpu_max": row[3], "mem_avg": row[4], "mem_max": row[5],
            "disk_max": row[6], "lat_avg": row[7], "first": row[8], "last": row[9],
        }
        # ---- tren (bucket per N menit) ----
        cur.execute(f"""
            SELECT TO_CHAR(b,'MM-DD HH24:MI'),
                   ROUND(AVG(cpu),1), ROUND(AVG(mem),1), ROUND(AVG(disk),1), ROUND(AVG(lat),1),
                   ROUND(100*SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END)/COUNT(*),0)
            FROM (
              SELECT (TRUNC(d) + FLOOR((d - TRUNC(d))*1440/:n)*:n/1440) b, cpu, mem, disk, lat, status
              FROM ( SELECT CAST(waktu AS DATE) d, cpu_pct cpu, mem_pct mem, disk_pct disk, latency_ms lat, status
                     FROM server_monitoring WHERE nama_server=:s AND waktu > {cutoff} )
            )
            GROUP BY b ORDER BY b
        """, {"n": n, "s": server})
        series = [{"t": r[0], "cpu": r[1], "mem": r[2], "disk": r[3], "lat": r[4], "online": r[5]}
                  for r in cur.fetchall()]
        # ---- downtime (dari transisi status) ----
        cur.execute(f"""
            SELECT waktu, status FROM (
              SELECT waktu, status, LAG(status) OVER (ORDER BY waktu) prev
              FROM server_monitoring WHERE nama_server=:s AND waktu > {cutoff}
            ) WHERE prev IS NULL OR status <> prev ORDER BY waktu
        """, {"s": server})
        episodes, open_start = [], None
        for waktu, status in cur.fetchall():
            if status != 'ONLINE' and open_start is None:
                open_start = waktu
            elif status == 'ONLINE' and open_start is not None:
                episodes.append({"start": open_start.strftime("%Y-%m-%d %H:%M"),
                                 "end": waktu.strftime("%Y-%m-%d %H:%M"),
                                 "minutes": round((waktu - open_start).total_seconds() / 60.0, 1)})
                open_start = None
        if open_start is not None:
            episodes.append({"start": open_start.strftime("%Y-%m-%d %H:%M"), "end": None, "minutes": None})
        # ---- prediksi disk penuh (regresi linear disk harian, 14 hari terakhir) ----
        cur.execute("""SELECT TRUNC(CAST(waktu AS DATE)), ROUND(AVG(disk_pct),2)
                       FROM server_monitoring
                       WHERE nama_server=:s AND disk_pct IS NOT NULL AND waktu > SYSDATE - 14
                       GROUP BY TRUNC(CAST(waktu AS DATE)) ORDER BY 1""", {"s": server})
        pts = cur.fetchall()
        forecast = None
        if len(pts) >= 3:
            x0 = pts[0][0]
            xs = [(p[0] - x0).days for p in pts]
            ys = [float(p[1]) for p in pts]
            n2 = len(xs); mx = sum(xs) / n2; my = sum(ys) / n2
            den = sum((x - mx) ** 2 for x in xs)
            slope = (sum((xs[i] - mx) * (ys[i] - my) for i in range(n2)) / den) if den else 0.0
            current = ys[-1]
            if slope > 0.02 and current < 100:
                days = round((100 - current) / slope, 1)
                status = "kritis" if days < 14 else ("waspada" if days < 45 else "aman")
                forecast = {"slope_per_day": round(slope, 3), "current": round(current, 1),
                            "days_to_full": days, "status": status}
            else:
                forecast = {"slope_per_day": round(slope, 3), "current": round(current, 1),
                            "days_to_full": None, "status": "stabil"}
        return {"ok": True, "server": server, "range": rng, "summary": summary,
                "series": series, "downtime": episodes[-15:], "forecast": forecast}
    except Exception as e:
        _oracle["conn"] = None
        return {"ok": False, "error": str(e)[:160]}


# ================= Deteksi SITUS berbahaya dari riwayat browser =================
def load_site_watchlist():
    try:
        with open(os.path.join(BASE_DIR, "site_watchlist.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def ensure_situs_table():
    if not (ORACLE_ENABLED and ORACLE_AVAILABLE):
        return
    ddl = ("CREATE TABLE deteksi_situs (waktu TIMESTAMP, server VARCHAR2(120), ip VARCHAR2(45), "
           "domain VARCHAR2(300), kategori VARCHAR2(80))")
    try:
        cur = get_oracle_conn().cursor()
        try:
            cur.execute(ddl); _oracle["conn"].commit()
            print("  [Situs] tabel deteksi_situs dibuat.")
        except oracledb.DatabaseError as e:
            if "ORA-00955" not in str(e):
                raise
    except Exception as e:
        print("  [Situs] gagal siapkan tabel:", str(e)[:120])


def sync_labels_to_oracle():
    """Salin nama panggilan (label) dari targets.json ke tabel server_label di Oracle.
    Tabel kecil ini dipakai query SQL Developer (LEFT JOIN) agar hasil query
    menampilkan nama yang sama dengan di dashboard. Dipanggil saat start &
    tiap kali daftar server / label berubah (lewat save_targets)."""
    if not (ORACLE_ENABLED and ORACLE_AVAILABLE):
        return
    try:
        conn = get_oracle_conn()
        cur = conn.cursor()
        try:
            cur.execute("CREATE TABLE server_label (nama_server VARCHAR2(120) PRIMARY KEY, label VARCHAR2(60))")
            print("  [Label] tabel server_label dibuat.")
        except oracledb.DatabaseError as e:
            if "ORA-00955" not in str(e):   # 00955 = tabel sudah ada -> aman
                raise
        rows = [(str(t.get("name", "")).strip()[:120], (str(t.get("label", "") or "").strip()[:60] or None))
                for t in load_targets() if str(t.get("name", "")).strip()]
        cur.execute("DELETE FROM server_label")
        if rows:
            cur.executemany("INSERT INTO server_label (nama_server, label) VALUES (:1, :2)", rows)
        conn.commit()
    except Exception as e:
        _oracle["conn"] = None
        print("  [Label] gagal sinkron label ke Oracle:", str(e)[:120])


def check_sites(ip, port, log=True):
    """Ambil domain riwayat browser dari agen, tandai yang cocok site_watchlist.json."""
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/sites", timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "agen tidak terjangkau / belum jalan"}
    if "domains" not in data:
        return {"ok": False, "error": "agen belum mendukung /sites - perbarui Agent.ps1 di server itu"}
    domains = data.get("domains", []) or []
    wl = load_site_watchlist()
    desc = wl.get("_deskripsi", {}) or {}
    flagged, seen = [], set()
    for d in domains:
        low = str(d).lower()
        for cat, keywords in wl.items():
            if cat.startswith("_"):
                continue
            hit = None
            for kw in keywords:
                if str(kw).lower() in low:
                    hit = kw
                    break
            if hit is not None:
                key = (low, cat)
                if key not in seen:
                    seen.add(key)
                    flagged.append({"domain": d, "kategori": cat, "cocok": hit, "alasan": desc.get(cat, "")})
                break
    host = data.get("hostname", ip)
    if log and flagged and ORACLE_ENABLED and ORACLE_AVAILABLE:
        try:
            conn = get_oracle_conn(); cur = conn.cursor(); now = datetime.datetime.now()
            cur.executemany(
                "INSERT INTO deteksi_situs (waktu, server, ip, domain, kategori) VALUES (:1,:2,:3,:4,:5)",
                [(now, host, ip, str(f["domain"])[:300], str(f["kategori"])[:80]) for f in flagged])
            conn.commit()
        except Exception:
            _oracle["conn"] = None
    return {"ok": True, "hostname": host, "total": len(domains), "flagged": flagged}


_oracle = {"conn": None}


def get_oracle_conn():
    if _oracle["conn"] is None:
        _oracle["conn"] = oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
    return _oracle["conn"]


def oracle_log_worker():
    """Tiap ORACLE_INTERVAL detik: simpan status + metrik tiap server ke tabel Oracle."""
    while True:
        try:
            targets_raw = load_targets()
            status = get_targets_status()
            by_key = {(t["ip"], int(t["port"])): t for t in status}
            now = datetime.datetime.now()
            rows = []
            for tr in targets_raw:
                ip = tr.get("ip", "")
                port = int(tr.get("port", 0))
                st = by_key.get((ip, port), {})
                agent_port = int(tr.get("agent_port") or AGENT_PORT_DEFAULT)
                m = fetch_agent_metrics(ip, agent_port) if st.get("status") == "ONLINE" else None
                rows.append((
                    now, tr.get("name", ip), ip, st.get("status", "OFFLINE"), st.get("latency_ms"),
                    (m["cpu_percent"] if m else None), (m["mem_percent"] if m else None),
                    (m["disk_percent"] if m else None),
                    (round(m["rx_kbps"]) if m else None), (round(m["tx_kbps"]) if m else None),
                ))
            if rows:
                conn = get_oracle_conn()
                cur = conn.cursor()
                cur.executemany(
                    """INSERT INTO server_monitoring
                       (waktu, nama_server, ip, status, latency_ms, cpu_pct, mem_pct, disk_pct, rx_kbps, tx_kbps)
                       VALUES (:1,:2,:3,:4,:5,:6,:7,:8,:9,:10)""", rows)
                conn.commit()
        except Exception as e:
            _oracle["conn"] = None   # reset supaya reconnect siklus berikutnya
            print("  [Oracle] gagal simpan:", str(e)[:140])
        time.sleep(ORACLE_INTERVAL)


# Ambang "batas aman" untuk evaluasi laporan (sesuaikan dgn kebijakan IT PT Badak)
REPORT_BATAS = {
    "cpu_avg": 85.0,   # CPU rata-rata sehat < 85%
    "cpu_max": 95.0,   # lonjakan CPU < 95%
    "mem_avg": 90.0,   # RAM rata-rata < 90%
    "disk_max": 90.0,  # disk terpakai maks < 90% (free >= 10%)
    "lat_avg": 100.0,  # latensi LAN rata-rata <= 100 ms
    "online": 95.0,    # ketersediaan ONLINE >= 95%
}
REPORT_MIN_SAMPEL = 5      # server dgn sampel < ini dianggap uji coba -> tidak dilaporkan
REPORT_MAX_RIWAYAT = 3000  # batas baris pada sheet Data Pengukuran

_XLS_ILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
def _xls(v):
    """Buang karakter kontrol ilegal agar tidak ditolak openpyxl saat ditulis ke sel Excel."""
    return _XLS_ILLEGAL.sub('', v) if isinstance(v, str) else v


def load_thresholds():
    """Ambang batas efektif: default REPORT_BATAS ditimpa thresholds.json (bisa diatur via UI)."""
    b = dict(REPORT_BATAS)
    try:
        with open(os.path.join(BASE_DIR, "thresholds.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in b:
            if k in data:
                b[k] = float(data[k])
    except Exception:
        pass
    return b


def save_thresholds(data):
    """Simpan ambang batas ke thresholds.json (hanya kunci yang dikenal)."""
    cur = load_thresholds()
    for k in REPORT_BATAS:
        if k in data:
            try:
                cur[k] = float(data[k])
            except (TypeError, ValueError):
                pass
    path = os.path.join(BASE_DIR, "thresholds.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2)
    os.replace(tmp, path)
    return cur


def generate_report_excel(server=None):
    """Bangun laporan monitoring (.xlsx) bergaya PT Badak dari data Oracle.

    server=None -> laporan KESELURUHAN (semua server).
    server="NAMA" -> laporan hanya untuk 1 server itu.
    Kembalikan bytes / None.
    """
    if not (ORACLE_ENABLED and ORACLE_AVAILABLE):
        return None
    try:
        import io
        import datetime as _dt
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        conn = get_oracle_conn()
        cur = conn.cursor()
        B = load_thresholds()

        # ---- palet & gaya (mengikuti template Exaquantum) --------------------
        NAVY, BAND, GRAY = "1F3864", "D9E2F3", "808080"
        fill_navy = PatternFill("solid", fgColor=NAVY)
        fill_band = PatternFill("solid", fgColor=BAND)
        fill_pass = PatternFill("solid", fgColor="C6EFCE")
        fill_warn = PatternFill("solid", fgColor="FFEB9C")
        fill_fail = PatternFill("solid", fgColor="FFC7CE")
        f_title = Font(name="Calibri", size=16, bold=True, color=NAVY)
        f_sub = Font(name="Calibri", size=9, color=GRAY)
        f_hdr = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        f_band = Font(name="Calibri", size=11, bold=True, color=NAVY)
        f_bold = Font(name="Calibri", size=11, bold=True)
        f_norm = Font(name="Calibri", size=11)
        f_pass = Font(name="Calibri", size=11, bold=True, color="006100")
        f_warn = Font(name="Calibri", size=11, bold=True, color="9C6500")
        f_fail = Font(name="Calibri", size=11, bold=True, color="9C0006")
        center = Alignment(horizontal="center", vertical="center")
        thin = Side(style="thin", color="BFBFBF")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        def st_font(s):
            return {"PASS": f_pass, "WARN": f_warn, "FAIL": f_fail,
                    "LOLOS": f_pass, "TIDAK LOLOS": f_fail}.get(s, f_norm)

        def st_fill(s):
            return {"PASS": fill_pass, "WARN": fill_warn, "FAIL": fill_fail,
                    "LOLOS": fill_pass, "TIDAK LOLOS": fill_fail}.get(s)

        def band_row(ws, row, text, span=12):
            ws.cell(row=row, column=1, value=text).font = f_band
            for c in range(1, span + 1):
                ws.cell(row=row, column=c).fill = fill_band

        def table_header(ws, row, headers):
            for i, h in enumerate(headers, start=1):
                cell = ws.cell(row=row, column=i, value=h)
                cell.font, cell.fill, cell.alignment, cell.border = f_hdr, fill_navy, center, border

        # ---- filter opsional per server --------------------------------------
        # Untuk laporan 1 server: filter by nama & tampilkan walau sampel sedikit.
        srv_filter = " AND nama_server = :srv" if server else ""
        min_sampel = 1 if server else REPORT_MIN_SAMPEL

        # peta hostname -> label (nama panggilan) dari targets.json, dibangun sekali
        label_map = {str(t.get("name", "")).strip().lower(): str(t.get("label", "") or "").strip()
                     for t in load_targets()}

        def disp_server(host):
            """Tampilkan nama panggilan kalau ada, kalau tidak pakai hostname asli."""
            lbl = label_map.get(str(host).strip().lower(), "")
            return lbl if lbl else host

        def disp_full(host):
            """Label + hostname: 'Laptop Zakky (DESKTOP-22AN8KV)'. Untuk tabel ringkasan."""
            lbl = label_map.get(str(host).strip().lower(), "")
            return f"{lbl} ({host})" if lbl else host

        # ---- ambil agregat (buang baris uji coba utk laporan keseluruhan) -----
        agg_binds = {"minn": min_sampel}
        if server:
            agg_binds["srv"] = server
        cur.execute(f"""
            SELECT nama_server, ip, COUNT(*) n,
                   ROUND(AVG(cpu_pct),1), ROUND(MAX(cpu_pct),1),
                   ROUND(AVG(mem_pct),1), ROUND(MAX(mem_pct),1),
                   ROUND(AVG(disk_pct),1), ROUND(MAX(disk_pct),1),
                   ROUND(AVG(latency_ms),1),
                   SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END),
                   TO_CHAR(MAX(waktu),'YYYY-MM-DD HH24:MI')
            FROM server_monitoring
            WHERE cpu_pct IS NOT NULL{srv_filter}
            GROUP BY nama_server, ip
            HAVING COUNT(*) >= :minn
            ORDER BY nama_server
        """, agg_binds)
        agg = cur.fetchall()

        # periode: keseluruhan DB, atau khusus server ybs
        if server:
            cur.execute("SELECT COUNT(*), TO_CHAR(MIN(waktu),'YYYY-MM-DD HH24:MI'), "
                        "TO_CHAR(MAX(waktu),'YYYY-MM-DD HH24:MI') FROM server_monitoring "
                        "WHERE nama_server = :srv", srv=server)
        else:
            cur.execute("SELECT COUNT(*), TO_CHAR(MIN(waktu),'YYYY-MM-DD HH24:MI'), "
                        "TO_CHAR(MAX(waktu),'YYYY-MM-DD HH24:MI') FROM server_monitoring")
        _tot, tmin, tmax = cur.fetchone()

        wb = Workbook()

        # ================================================= Sheet 1: Ringkasan
        ws = wb.active
        ws.title = "Ringkasan"
        ws.sheet_view.showGridLines = False
        now_str = _dt.datetime.now().strftime("%d-%m-%Y %H:%M")
        if server:
            lbl = label_for(server)
            tampil = f"{lbl} ({server})" if lbl else server
            ws["A1"] = f"LAPORAN MONITORING SERVER - {tampil}"
            ws["A2"] = (f"PT Badak NGL - IT Section  |  Server: {tampil}  |  "
                        f"Periode: {tmin} s/d {tmax}  |  Dibuat: {now_str}")
        else:
            ws["A1"] = "LAPORAN MONITORING SERVER & KEAMANAN"
            ws["A2"] = (f"PT Badak NGL - IT Section  |  {len(agg)} server dipantau  |  "
                        f"Periode: {tmin} s/d {tmax}  |  Dibuat: {now_str}")
        ws["A1"].font = f_title
        ws["A2"].font = f_sub
        ws["A3"] = ("Dashboard read-only (prinsip Non-Interference) - metrik dibaca dari agen, "
                    "disimpan ke Oracle otomatis.")
        ws["A3"].font = f_sub

        # -- A. rekap per server
        band_row(ws, 5, "A. REKAP PER SERVER (seluruh periode)")
        table_header(ws, 6, ["Server", "IP", "Jumlah Sampel", "CPU rata2 (%)", "CPU maks (%)",
                             "RAM rata2 (%)", "RAM maks (%)", "Disk rata2 (%)", "Disk maks (%)",
                             "Latensi rata2 (ms)", "ONLINE (%)", "Status"])
        temuan = []
        r = 7
        for (nama, ip, n, cavg, cmax, mavg, mmax, davg, dmax, lavg, nonline, tlast) in agg:
            online = round(100.0 * nonline / n, 1) if n else 0.0
            st, catatan = "PASS", []
            if dmax is not None and dmax >= B["disk_max"]:
                st = "WARN"; catatan.append(f"Disk terpakai maks {dmax}% (batas {B['disk_max']}%)")
            if cavg is not None and cavg >= B["cpu_avg"]:
                st = "WARN"; catatan.append(f"CPU rata2 {cavg}% (batas {B['cpu_avg']}%)")
            if mavg is not None and mavg >= B["mem_avg"]:
                st = "WARN"; catatan.append(f"RAM rata2 {mavg}% (batas {B['mem_avg']}%)")
            if online < B["online"]:
                st = "FAIL"; catatan.append(f"Uptime ONLINE {online}% (< {B['online']}%)")
            for i, v in enumerate([disp_full(nama), ip, n, cavg, cmax, mavg, mmax, davg, dmax, lavg, online, st], start=1):
                cell = ws.cell(row=r, column=i, value=_xls(v))
                cell.font, cell.border = f_norm, border
                if i >= 3:
                    cell.alignment = center
            sc = ws.cell(row=r, column=12)
            sc.font, sc.fill = st_font(st), st_fill(st)
            if catatan:
                temuan.append((tlast, nama, st, "; ".join(catatan)))
            r += 1

        # -- B. evaluasi agregat vs batas aman
        def _avg(idx):
            xs = [a[idx] for a in agg if a[idx] is not None]
            return round(sum(xs) / len(xs), 1) if xs else None

        def _max(idx):
            xs = [a[idx] for a in agg if a[idx] is not None]
            return max(xs) if xs else None

        tot_samp = sum(a[2] for a in agg)
        tot_online = sum(a[10] for a in agg)
        g_online = round(100.0 * tot_online / tot_samp, 1) if tot_samp else 0.0
        g_cpu, g_cmax, g_mem, g_dmax, g_lat = _avg(3), _max(4), _avg(5), _max(8), _avg(9)

        def _res(ok):
            return "LOLOS" if ok else "TIDAK LOLOS"

        r += 1
        band_row(ws, r, "B. EVALUASI TERHADAP BATAS AMAN (agregat semua server)")
        r += 1
        table_header(ws, r, ["Metrik", "Nilai Terukur", "Batas Aman", "Hasil"])
        r += 1
        evals = [
            ("CPU rata-rata semua server (%)", g_cpu, f"< {B['cpu_avg']}%", g_cpu is not None and g_cpu < B["cpu_avg"]),
            ("CPU tertinggi tercatat (%)", g_cmax, f"< {B['cpu_max']}%", g_cmax is not None and g_cmax < B["cpu_max"]),
            ("RAM rata-rata semua server (%)", g_mem, f"< {B['mem_avg']}%", g_mem is not None and g_mem < B["mem_avg"]),
            ("Disk terpakai tertinggi (%)", g_dmax, f"< {B['disk_max']}%", g_dmax is not None and g_dmax < B["disk_max"]),
            ("Latensi rata-rata (ms)", g_lat, f"<= {B['lat_avg']} ms", g_lat is not None and g_lat <= B["lat_avg"]),
            ("Ketersediaan ONLINE (%)", g_online, f">= {B['online']}%", g_online >= B["online"]),
        ]
        for (metrik, nilai, batas, ok) in evals:
            hasil = _res(ok)
            ws.cell(row=r, column=1, value=metrik).font = f_norm
            c2 = ws.cell(row=r, column=2, value=nilai); c2.font, c2.alignment = f_norm, center
            c3 = ws.cell(row=r, column=3, value=batas); c3.font, c3.alignment = f_norm, center
            hc = ws.cell(row=r, column=4, value=hasil)
            hc.font, hc.fill, hc.alignment = st_font(hasil), st_fill(hasil), center
            for cc in range(1, 5):
                ws.cell(row=r, column=cc).border = border
            r += 1

        # -- C. kesimpulan
        n_fail = sum(1 for t in temuan if t[2] == "FAIL")
        n_warn = sum(1 for t in temuan if t[2] == "WARN")
        n_ok = len(agg) - len(temuan)
        r += 1
        band_row(ws, r, "C. KESIMPULAN")
        r += 1
        for col, (lab, val) in enumerate([("Total sampel terekam", tot_samp), ("Server SEHAT", n_ok),
                                          ("WARN", n_warn), ("FAIL", n_fail)]):
            ws.cell(row=r, column=1 + col * 2, value=lab).font = f_bold
            ws.cell(row=r, column=2 + col * 2, value=val).font = f_norm
        r += 1
        overall = ("SEMUA SERVER DALAM BATAS AMAN" if (n_warn == 0 and n_fail == 0)
                   else "ADA SERVER PERLU PERHATIAN - lihat kolom Status & daftar temuan")
        ws.cell(row=r, column=1, value="Status keseluruhan").font = f_bold
        ws.cell(row=r, column=2, value=overall).font = f_pass if (n_warn == 0 and n_fail == 0) else f_warn

        # -- D. daftar temuan
        r += 2
        band_row(ws, r, f"D. DAFTAR TEMUAN WARN / FAIL  ({len(temuan)} item)")
        r += 1
        table_header(ws, r, ["Waktu (data terakhir)", "Server", "Status", "Catatan"])
        r += 1
        if temuan:
            for (w, srv, st, cat) in temuan:
                ws.cell(row=r, column=1, value=w).font = f_norm
                ws.cell(row=r, column=2, value=srv).font = f_norm
                sc = ws.cell(row=r, column=3, value=st)
                sc.font, sc.fill, sc.alignment = st_font(st), st_fill(st), center
                ws.cell(row=r, column=4, value=cat).font = f_norm
                for cc in range(1, 5):
                    ws.cell(row=r, column=cc).border = border
                r += 1
        else:
            ws.cell(row=r, column=1, value="Tidak ada temuan - semua metrik dalam batas aman.").font = f_norm

        for i, wd in enumerate([32, 15, 14, 14, 13, 14, 13, 15, 12, 16, 12, 34], start=1):
            ws.column_dimensions[get_column_letter(i)].width = wd
        ws.freeze_panes = "A4"

        # ================================================= Sheet 2: Data Pengukuran
        ws2 = wb.create_sheet("Data Pengukuran")
        ws2.sheet_view.showGridLines = False
        table_header(ws2, 1, ["Waktu", "Server", "IP", "Status", "CPU (%)", "RAM (%)",
                              "Disk terpakai (%)", "Latensi (ms)", "RX (Kbps)", "TX (Kbps)", "Penilaian"])
        data_binds = {"lim": REPORT_MAX_RIWAYAT}
        if server:
            data_binds["srv"] = server
        cur.execute(f"""SELECT TO_CHAR(waktu,'YYYY-MM-DD HH24:MI:SS'), nama_server, ip, status,
                          latency_ms, cpu_pct, mem_pct, disk_pct, rx_kbps, tx_kbps
                       FROM server_monitoring WHERE cpu_pct IS NOT NULL{srv_filter}
                       ORDER BY waktu DESC FETCH FIRST :lim ROWS ONLY""", data_binds)
        rr = 2
        for (w, nama, ip, status, lat, cpu, mem, disk, rx, tx) in cur.fetchall():
            pen = "PASS"
            if (cpu is not None and cpu >= B["cpu_max"]) or (mem is not None and mem >= 95) \
               or (disk is not None and disk >= B["disk_max"]):
                pen = "WARN"
            if status != "ONLINE":
                pen = "FAIL"
            for i, v in enumerate([w, disp_server(nama), ip, status, cpu, mem, disk, lat, rx, tx, pen], start=1):
                cell = ws2.cell(row=rr, column=i, value=_xls(v))
                cell.font, cell.border = f_norm, border
                if i >= 4:
                    cell.alignment = center
            pc = ws2.cell(row=rr, column=11)
            pc.font, pc.fill = st_font(pen), st_fill(pen)
            rr += 1
        for i, wd in enumerate([20, 17, 15, 10, 9, 9, 16, 12, 11, 11, 12], start=1):
            ws2.column_dimensions[get_column_letter(i)].width = wd
        ws2.freeze_panes = "A2"

        # ================================================= Sheet 3: Deteksi Aplikasi
        ws3 = wb.create_sheet("Deteksi Aplikasi")
        ws3.sheet_view.showGridLines = False
        ws3["A1"] = "AUDIT DETEKSI APLIKASI (dicocokkan dengan watchlist IT)"
        ws3["A1"].font = f_band
        for c in range(1, 6):
            ws3.cell(row=1, column=c).fill = fill_band
        table_header(ws3, 2, ["Waktu", "Server", "IP", "Aplikasi", "Kategori"])
        if server:
            cur.execute("""SELECT TO_CHAR(waktu,'YYYY-MM-DD HH24:MI:SS'), server, ip, aplikasi, kategori
                           FROM deteksi_aplikasi WHERE server = :srv ORDER BY waktu DESC""", srv=server)
        else:
            cur.execute("""SELECT TO_CHAR(waktu,'YYYY-MM-DD HH24:MI:SS'), server, ip, aplikasi, kategori
                           FROM deteksi_aplikasi ORDER BY waktu DESC""")
        drows = cur.fetchall()
        rr = 3
        if drows:
            for (w, srv, ip, apl, kat) in drows:
                for i, v in enumerate([w, srv, ip, apl, kat], start=1):
                    cell = ws3.cell(row=rr, column=i, value=_xls(v))
                    cell.font, cell.border = f_norm, border
                kc = ws3.cell(row=rr, column=5)
                kc.font, kc.fill = f_warn, fill_warn
                rr += 1
        else:
            ws3.cell(row=3, column=1,
                     value="Tidak ada aplikasi ter-flag pada periode ini - tidak ditemukan aplikasi "
                           "berisiko (AnyDesk/TeamViewer/torrent/dll) di server yang dipantau.").font = f_norm
            ws3.merge_cells(start_row=3, start_column=1, end_row=3, end_column=5)
        for i, wd in enumerate([20, 18, 15, 40, 22], start=1):
            ws3.column_dimensions[get_column_letter(i)].width = wd
        ws3.freeze_panes = "A3"

        # ================================================= Sheet 4: Uptime & Downtime
        ws4 = wb.create_sheet("Uptime & Downtime")
        ws4.sheet_view.showGridLines = False
        band_row(ws4, 1, "A. RINGKASAN UPTIME PER SERVER (seluruh data)")
        table_header(ws4, 2, ["Server", "Uptime (%)", "Sampel", "Downtime (kejadian)",
                              "Total mati (menit)", "Data pertama", "Data terakhir"])
        rr = 3
        all_dt = []
        for a in agg:
            nama = a[0]
            cur.execute("""SELECT COUNT(*), SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END),
                              TO_CHAR(MIN(waktu),'YYYY-MM-DD HH24:MI'), TO_CHAR(MAX(waktu),'YYYY-MM-DD HH24:MI')
                           FROM server_monitoring WHERE nama_server=:s""", {"s": nama})
            c0 = cur.fetchone()
            tot = c0[0] or 0
            if not tot:
                continue
            onl = c0[1] or 0
            up = round(100.0 * onl / tot, 2)
            off_min = round((tot - onl) * ORACLE_INTERVAL / 60.0, 1)
            cur.execute("""SELECT waktu, status FROM (
                              SELECT waktu, status, LAG(status) OVER (ORDER BY waktu) prev
                              FROM server_monitoring WHERE nama_server=:s
                            ) WHERE prev IS NULL OR status <> prev ORDER BY waktu""", {"s": nama})
            eps, open_s = [], None
            for w, st in cur.fetchall():
                if st != 'ONLINE' and open_s is None:
                    open_s = w
                elif st == 'ONLINE' and open_s is not None:
                    eps.append((open_s, w, round((w - open_s).total_seconds() / 60.0, 1))); open_s = None
            if open_s is not None:
                eps.append((open_s, None, None))
            for (sa, se, mi) in eps:
                all_dt.append((disp_full(nama), sa.strftime("%Y-%m-%d %H:%M"),
                               se.strftime("%Y-%m-%d %H:%M") if se else "masih mati", mi))
            hasil = "PASS" if up >= B["online"] else ("WARN" if up >= B["online"] - 5 else "FAIL")
            for i, v in enumerate([disp_full(nama), up, tot, len(eps), off_min, c0[2], c0[3]], start=1):
                cell = ws4.cell(row=rr, column=i, value=_xls(v)); cell.font, cell.border = f_norm, border
                if i >= 2:
                    cell.alignment = center
            uc = ws4.cell(row=rr, column=2); uc.font, uc.fill = st_font(hasil), st_fill(hasil)
            rr += 1
        rr += 1
        band_row(ws4, rr, f"B. RINCIAN DOWNTIME ({len(all_dt)} kejadian)")
        rr += 1
        table_header(ws4, rr, ["Server", "Mulai", "Selesai", "Lama (menit)"])
        rr += 1
        if all_dt:
            for (lbl, sa, se, mi) in all_dt:
                for i, v in enumerate([lbl, sa, se, (mi if mi is not None else "-")], start=1):
                    cell = ws4.cell(row=rr, column=i, value=_xls(v)); cell.font, cell.border = f_norm, border
                    if i >= 2:
                        cell.alignment = center
                rr += 1
        else:
            ws4.cell(row=rr, column=1, value="Tidak ada downtime tercatat.").font = f_norm
        for i, wd in enumerate([26, 12, 12, 18, 16, 16, 16], start=1):
            ws4.column_dimensions[get_column_letter(i)].width = wd
        ws4.freeze_panes = "A3"

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as e:
        _oracle["conn"] = None
        print("  [Laporan Excel] gagal:", str(e)[:160])
        return None


def generate_report_print(server=None):
    """Laporan HTML siap-cetak (Ctrl+P -> Simpan PDF). server=None -> semua; server=NAMA -> 1 server (+rincian)."""
    import datetime as _dt
    now = _dt.datetime.now().strftime("%d-%m-%Y %H:%M")
    targets = get_targets_status()
    if server:
        targets = [t for t in targets if str(t.get("name")) == server]
    B = load_thresholds()
    conn = None
    if ORACLE_ENABLED and ORACLE_AVAILABLE:
        try:
            conn = get_oracle_conn()
        except Exception:
            conn = None

    def e(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rows, n_up, n_crit, n_flag = [], 0, 0, 0
    for t in targets:
        nama = t["name"]; label = (t.get("label") or "").strip()
        disp = f"{e(label)} <span style='color:#8a94a6'>({e(nama)})</span>" if label else e(nama)
        online = t["status"] == "ONLINE"
        n_up += 1 if online else 0
        cd, fc = t.get("critical_down"), t.get("flagged_count")
        n_crit += 1 if (cd and cd > 0) else 0
        n_flag += 1 if (fc and fc > 0) else 0
        up, cpu, mem, disk = "-", "-", "-", "-"
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""SELECT COUNT(*), SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END),
                                  ROUND(AVG(cpu_pct),1), ROUND(AVG(mem_pct),1), ROUND(MAX(disk_pct),1)
                               FROM server_monitoring WHERE nama_server=:s AND waktu > SYSDATE - 30""", {"s": nama})
                r = cur.fetchone(); tot = r[0] or 0
                if tot:
                    up = round(100.0 * (r[1] or 0) / tot, 2); cpu, mem, disk = r[2], r[3], r[4]
            except Exception:
                pass
        upcol = "#127a51" if (isinstance(up, (int, float)) and up >= B["online"]) else \
                ("#8a5a12" if (isinstance(up, (int, float)) and up >= B["online"] - 5) else "#b23333")
        upstr = (f"{up}%" if up != "-" else "-")
        stb = "<b style='color:#127a51'>ONLINE</b>" if online else "<b style='color:#b23333'>OFFLINE</b>"
        critb = (f"<span style='color:#b23333'>&#9881; {cd} mati</span>" if (cd and cd > 0)
                 else ("<span style='color:#127a51'>OK</span>" if cd == 0 else "-"))
        flagb = (f"<span style='color:#8a5a12'>&#128737; {fc}</span>" if (fc and fc > 0)
                 else ("Aman" if fc == 0 else "-"))
        rows.append(f"<tr><td>{disp}</td><td>{e(t['ip'])}</td><td>{stb}</td>"
                    f"<td style='color:{upcol};font-weight:700'>{upstr}</td>"
                    f"<td>{cpu}</td><td>{mem}</td><td>{disk}</td><td>{critb}</td><td>{flagb}</td></tr>")
    rows_html = "\n".join(rows) or "<tr><td colspan='9'>Belum ada server.</td></tr>"
    overall = "SEMUA NORMAL" if (n_crit == 0 and n_flag == 0 and n_up == len(targets) and targets) else "PERLU PERHATIAN"
    ovcol = "#127a51" if overall == "SEMUA NORMAL" else "#b23333"

    # judul, meta, dan (untuk per-server) rincian layanan kritis + downtime
    if server and targets:
        lbl = (targets[0].get("label") or "").strip()
        judul = f"LAPORAN SERVER &mdash; {e(lbl + ' (' + server + ')') if lbl else e(server)}"
        meta = f"Server: {e(server)} &middot; IP {e(targets[0]['ip'])} &middot; Uptime dihitung 30 hari terakhir (Oracle)."
    else:
        judul = "LAPORAN MONITORING SERVER"
        meta = (f"{len(targets)} server dipantau &middot; {n_up} aktif &middot; {n_crit} ada layanan kritis mati "
                f"&middot; {n_flag} ada aplikasi ter-flag. Uptime dihitung dari 30 hari terakhir (Oracle).")

    extra = ""
    if server and targets:
        t0 = targets[0]; ip0 = t0["ip"]
        try:
            port0 = int(t0.get("port", 9091))
        except (TypeError, ValueError):
            port0 = 9091
        cc = _crit_cache.get(server)
        items = cc.get("items") if cc else None
        if items is None:
            cr = check_critical(server, ip0, port0)
            items = cr.get("items") if cr.get("ok") else []
        if items:
            crows = "".join(
                f"<tr><td>{e(it.get('label'))}</td><td>{e(it['jenis'])}</td><td>{e(it['nama'])}</td>"
                f"<td style='color:{'#127a51' if it['ok'] else '#b23333'};font-weight:700'>{e(it['status'])}</td></tr>"
                for it in items)
            extra += ("<h2 style='font-size:14px;color:#1f3864;margin:18px 0 6px'>Layanan &amp; Port Kritis</h2>"
                      "<table><tr><th>Nama</th><th>Jenis</th><th>Target</th><th>Status</th></tr>" + crows + "</table>")
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""SELECT waktu, status FROM (
                                  SELECT waktu, status, LAG(status) OVER (ORDER BY waktu) prev
                                  FROM server_monitoring WHERE nama_server=:s AND waktu > SYSDATE - 30
                                ) WHERE prev IS NULL OR status <> prev ORDER BY waktu""", {"s": server})
                eps, op = [], None
                for w, st in cur.fetchall():
                    if st != 'ONLINE' and op is None:
                        op = w
                    elif st == 'ONLINE' and op is not None:
                        eps.append((op, w, round((w - op).total_seconds() / 60.0, 1))); op = None
                if op is not None:
                    eps.append((op, None, None))
                drows = "".join(
                    f"<tr><td>{ep[0].strftime('%Y-%m-%d %H:%M')}</td>"
                    f"<td>{ep[1].strftime('%Y-%m-%d %H:%M') if ep[1] else 'masih mati'}</td>"
                    f"<td>{'-' if ep[2] is None else ep[2]}</td></tr>" for ep in eps[-20:])
                extra += ("<h2 style='font-size:14px;color:#1f3864;margin:18px 0 6px'>Downtime (30 hari terakhir)</h2>"
                          "<table><tr><th>Mulai</th><th>Selesai</th><th>Lama (menit)</th></tr>"
                          + (drows or "<tr><td colspan='3'>Tidak ada downtime.</td></tr>") + "</table>")
            except Exception:
                pass

    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<title>Laporan Monitoring - PT Badak NGL</title>
<style>
  @page{{ size:A4; margin:14mm }}
  body{{ font-family:"Segoe UI",Arial,sans-serif; color:#1b2432; margin:0; padding:22px }}
  .head{{ display:flex; justify-content:space-between; align-items:flex-end; border-bottom:3px solid #9e1327; padding-bottom:12px; margin-bottom:14px }}
  .head h1{{ margin:0; font-size:20px; color:#9e1327; letter-spacing:.3px }} .head .sub{{ font-size:12px; color:#666; margin-top:3px }}
  .meta{{ font-size:12px; color:#555; margin-bottom:14px }}
  table{{ width:100%; border-collapse:collapse; font-size:12px }}
  th,td{{ border:1px solid #dbe1ea; padding:7px 9px; text-align:left }}
  th{{ background:#1f3864; color:#fff; font-weight:700 }}
  tr:nth-child(even) td{{ background:#f6f8fc }}
  .foot{{ margin-top:16px; font-size:11px; color:#8a94a6; border-top:1px solid #e5eaf1; padding-top:8px }}
  .btn{{ background:#9e1327; color:#fff; border:none; border-radius:8px; padding:10px 20px; font-size:14px; cursor:pointer; font-weight:700 }}
  @media print{{ .noprint{{ display:none }} body{{ padding:0 }} }}
</style></head><body>
<div class="noprint" style="margin-bottom:14px"><button class="btn" onclick="window.print()">&#128424; Cetak / Simpan sebagai PDF</button></div>
<div class="head"><div><h1>{judul}</h1><div class="sub">PT Badak NGL &mdash; IT Section &middot; prinsip Non-Interference</div></div>
  <div style="text-align:right;font-size:12px">Status: <b style="color:{ovcol}">{overall}</b><br>{now}</div></div>
<div class="meta">{meta}</div>
<table>
<tr><th>Server</th><th>IP</th><th>Status</th><th>Uptime</th><th>CPU rata&sup2;</th><th>RAM rata&sup2;</th><th>Disk maks</th><th>Layanan kritis</th><th>Keamanan</th></tr>
{rows_html}
</table>
{extra}
<div class="foot">Dibuat otomatis oleh Dashboard Monitoring PT Badak NGL. Untuk menyimpan sebagai PDF: klik tombol di atas atau tekan Ctrl+P lalu pilih &ldquo;Simpan sebagai PDF&rdquo;.</div>
</body></html>"""


def build_setup_script(dash, service=False):
    """Script PowerShell 1-perintah untuk pasang agen. dash = alamat dashboard (dari Host header)."""
    fw = ("$fw=\"Remove-NetFirewallRule -DisplayName 'Agen 9091' -ErrorAction SilentlyContinue; "
          "New-NetFirewallRule -DisplayName 'Agen 9091' -Direction Inbound -LocalPort 9091 "
          "-Protocol TCP -Action Allow | Out-Null\"\n")
    if service:
        return (
            "$DASH='" + dash + "'\n"
            "if(-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')){\n"
            "  Write-Host 'Perlu Administrator. Membuka jendela admin...' -ForegroundColor Yellow\n"
            "  $tmp=Join-Path $env:TEMP 'badak-setup-service.ps1'\n"
            "  try{ iwr \"$DASH/setup-service\" -OutFile $tmp -UseBasicParsing;"
            " Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File',$tmp }\n"
            "  catch{ Write-Host 'Ditolak/gagal. Buka PowerShell sebagai Administrator lalu jalankan lagi:' -ForegroundColor Red;"
            " Write-Host \"  irm $DASH/setup-service | iex\" -ForegroundColor Cyan }\n"
            "  return }\n"
            "Write-Host 'Memasang agen sebagai service...' -ForegroundColor Cyan\n"
            "$dir='C:\\ProgramData\\BadakMonitor'; New-Item -ItemType Directory -Force -Path $dir | Out-Null\n"
            "iwr \"$DASH/agent.ps1\" -OutFile \"$dir\\Agent.ps1\" -UseBasicParsing\n"
            "Remove-NetFirewallRule -DisplayName 'Agen 9091' -ErrorAction SilentlyContinue\n"
            "New-NetFirewallRule -DisplayName 'Agen 9091' -Direction Inbound -LocalPort 9091 -Protocol TCP -Action Allow | Out-Null\n"
            "$arg=\"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $dir\\Agent.ps1 -Port 9091 -CentralUrl $DASH\"\n"
            "$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg\n"
            "$t=New-ScheduledTaskTrigger -AtStartup; try{ $t.Delay='PT30S' }catch{}\n"
            "$p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest\n"
            "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew\n"
            "Unregister-ScheduledTask -TaskName 'BadakMonitor-Agen' -Confirm:$false -ErrorAction SilentlyContinue\n"
            "Register-ScheduledTask -TaskName 'BadakMonitor-Agen' -Action $a -Trigger $t -Principal $p -Settings $s -Description 'Agen Monitoring PT Badak NGL' | Out-Null\n"
            "Start-ScheduledTask -TaskName 'BadakMonitor-Agen'\n"
            "Write-Host 'SELESAI: agen jalan sebagai service (auto-start saat boot).' -ForegroundColor Green\n"
        )
    return (
        "$DASH='" + dash + "'\n"
        + fw +
        "if(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')){ iex $fw }\n"
        "else{ try{ Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-Command',$fw -Wait }catch{ Write-Warning 'Firewall dilewati (butuh admin).' } }\n"
        "Write-Host 'Menjalankan agen dari' $DASH '(biarkan jendela ini terbuka)' -ForegroundColor Cyan\n"
        "iwr \"$DASH/agent.ps1\" -OutFile \"$env:TEMP\\Agent.ps1\" -UseBasicParsing\n"
        "powershell -ExecutionPolicy Bypass -File \"$env:TEMP\\Agent.ps1\" -Port 9091 -CentralUrl \"$DASH\"\n"
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # matikan log ramai di konsol

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/metrics"):
            self._send(200, json.dumps(get_local_metrics()))
        elif self.path.startswith("/api/agent"):
            # jembatan: tarik metrik dari Agen yang jalan di server target (read-only)
            q = parse_qs(urlparse(self.path).query)
            ip = q.get("ip", [""])[0]
            port = q.get("port", ["9091"])[0]
            try:
                with urllib.request.urlopen(f"http://{ip}:{port}/", timeout=8) as resp:
                    self._send(200, resp.read())
            except Exception:
                self._send(200, json.dumps({"ok": False, "error": "agen tidak terjangkau"}))
        elif self.path.startswith("/api/apps"):
            q = parse_qs(urlparse(self.path).query)
            ip = q.get("ip", [""])[0]
            port = q.get("port", ["9091"])[0]
            res = check_apps(ip, port)   # cek manual (log ke Oracle spt biasa)
            if res.get("ok"):
                # segarkan cache penanda 🛡️ agar badge kartu langsung ikut, dgn kunci = nama target
                nm = None
                try:
                    for t in load_targets():
                        if t.get("ip") == ip and int(t.get("port", 0)) == int(port or 0):
                            nm = t.get("name"); break
                except (TypeError, ValueError):
                    nm = None
                nm = nm or res.get("hostname") or ip
                fl = res.get("flagged", [])
                _sec_cache[nm] = {"count": len(fl), "flagged": fl,
                                  "checked": time.strftime("%H:%M:%S"),
                                  "keys": frozenset(str(f["name"]).lower() for f in fl)}
            self._send(200, json.dumps(res))
        elif self.path.startswith("/api/critical"):
            q = parse_qs(urlparse(self.path).query)
            ip = q.get("ip", [""])[0]; port = q.get("port", ["9091"])[0]
            server = (q.get("server", [""])[0] or "").strip()
            if not server:
                for t in load_targets():
                    if t.get("ip") == ip and str(t.get("port")) == str(port):
                        server = t.get("name"); break
            res = check_critical(server or ip, ip, port)
            if res.get("ok") and not res.get("no_config"):
                items = res.get("items", [])
                state = {it["jenis"] + ":" + it["nama"]: it["ok"] for it in items}
                downkeys = [k for k in state if not state[k]]
                _crit_cache[server or ip] = {"down": len(downkeys), "total": len(items), "items": items,
                                             "checked": time.strftime("%H:%M:%S"), "state": state}
            self._send(200, json.dumps(res))
        elif self.path.startswith("/api/history"):
            q = parse_qs(urlparse(self.path).query)
            server = (q.get("server", [""])[0] or "").strip()
            rng = (q.get("range", ["24h"])[0] or "24h").strip()
            self._send(200, json.dumps(get_history(server, rng)))
        elif self.path.startswith("/api/top"):
            q = parse_qs(urlparse(self.path).query)
            ip = q.get("ip", [""])[0]; port = q.get("port", ["9091"])[0]
            try:
                with urllib.request.urlopen(f"http://{ip}:{port}/top", timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if "by_ram" not in data:
                    data = {"ok": False, "error": "agen belum mendukung /top - perbarui Agent.ps1 di server itu"}
                else:
                    data["ok"] = True
            except Exception:
                data = {"ok": False, "error": "agen tidak terjangkau / belum jalan"}
            self._send(200, json.dumps(data))
        elif self.path.startswith("/api/sites"):
            q = parse_qs(urlparse(self.path).query)
            ip = q.get("ip", [""])[0]; port = q.get("port", ["9091"])[0]
            self._send(200, json.dumps(check_sites(ip, port)))
        elif self.path.startswith("/api/thresholds"):
            self._send(200, json.dumps(load_thresholds()))
        elif self.path.startswith("/api/report/excel"):
            q = parse_qs(urlparse(self.path).query)
            server = (q.get("server", [""])[0] or "").strip() or None
            data = generate_report_excel(server=server)
            if not data:
                self._send(503, "Laporan gagal dibuat (Oracle mati / openpyxl belum ada).", "text/plain")
            else:
                if server:
                    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", server)
                    fname = f"Laporan_Monitoring_{safe}.xlsx"
                else:
                    fname = "Laporan_Monitoring_PTBadakNGL.xlsx"
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        elif self.path.startswith("/api/report/print"):
            q = parse_qs(urlparse(self.path).query)
            srv = (q.get("server", [""])[0] or "").strip() or None
            try:
                self._send(200, generate_report_print(server=srv), "text/html")
            except Exception as ex:
                self._send(500, "Gagal membuat laporan cetak: " + str(ex)[:140], "text/plain")
        elif self.path.startswith("/api/targets"):
            self._send(200, json.dumps(get_targets_status()))
        elif self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html")
            except FileNotFoundError:
                self._send(404, "index.html tidak ditemukan", "text/plain")
        elif self.path.lower() == "/agent.ps1":
            # sajikan Agent.ps1 agar bisa diunduh & dijalankan dari mesin lain via terminal
            try:
                with open(os.path.join(BASE_DIR, "Agent.ps1"), "rb") as f:
                    self._send(200, f.read(), "text/plain")
            except FileNotFoundError:
                self._send(404, "Agent.ps1 tidak ditemukan", "text/plain")
        elif self.path.startswith("/setup-service"):
            host = self.headers.get("Host") or f"{socket.gethostbyname(socket.gethostname())}:{PORT}"
            self._send(200, build_setup_script("http://" + host, service=True), "text/plain")
        elif self.path.startswith("/setup"):
            host = self.headers.get("Host") or f"{socket.gethostbyname(socket.gethostname())}:{PORT}"
            self._send(200, build_setup_script("http://" + host, service=False), "text/plain")
        else:
            self._send(404, "Not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}

        if self.path.startswith("/api/targets/add"):
            name = str(data.get("name", "")).strip()
            ip = str(data.get("ip", "")).strip()
            try:
                port = int(data.get("port"))
            except (TypeError, ValueError):
                port = 0
            if not ip or port <= 0 or port > 65535:
                self._send(400, json.dumps({"ok": False, "error": "IP atau port tidak valid"}))
                return
            if not name:
                name = ip
            targets = load_targets()
            # de-dup by NAMA (hostname): kalau nama sudah terdaftar, cukup UPDATE IP/port-nya
            found = False
            for t in targets:
                if str(t.get("name", "")).strip().lower() == name.strip().lower():
                    t["ip"] = ip
                    t["port"] = port
                    found = True
                    break
            if not found and not any(t.get("ip") == ip and int(t.get("port", 0)) == port for t in targets):
                targets.append({"name": name, "ip": ip, "port": port})
            save_targets(targets)
            self._send(200, json.dumps({"ok": True}))
        elif self.path.startswith("/api/targets/remove"):
            ip = str(data.get("ip", "")).strip()
            try:
                port = int(data.get("port"))
            except (TypeError, ValueError):
                port = 0
            targets = [t for t in load_targets()
                       if not (t.get("ip") == ip and int(t.get("port", 0)) == port)]
            save_targets(targets)
            self._send(200, json.dumps({"ok": True}))
        elif self.path.startswith("/api/targets/label"):
            # set / hapus nama panggilan (label) sebuah server, dicocokkan by nama (hostname)
            name = str(data.get("name", "")).strip()
            label = str(data.get("label", "")).strip()[:60]   # batasi panjang label
            if not name:
                self._send(400, json.dumps({"ok": False, "error": "nama server kosong"}))
                return
            targets = load_targets()
            found = False
            for t in targets:
                if str(t.get("name", "")).strip().lower() == name.lower():
                    if label:
                        t["label"] = label
                    else:
                        t.pop("label", None)   # label kosong -> hapus, kembali ke hostname
                    found = True
                    break
            if not found:
                self._send(404, json.dumps({"ok": False, "error": "server tidak ditemukan"}))
                return
            save_targets(targets)
            self._send(200, json.dumps({"ok": True, "label": label}))
        elif self.path.startswith("/api/thresholds"):
            saved = save_thresholds(data)
            self._send(200, json.dumps({"ok": True, "batas": saved}))
        else:
            self._send(404, json.dumps({"ok": False, "error": "Not found"}), "application/json")


def security_scan_worker():
    """Pindai aplikasi tiap server berkala -> isi _sec_cache untuk penanda 🛡️ di kartu.
    Ke Oracle hanya mencatat aplikasi ter-flag yang BARU muncul (hindari spam audit)."""
    time.sleep(8)   # jeda saat start agar server siap
    while True:
        try:
            for t in load_targets():
                name = t.get("name", t.get("ip"))
                ip = t.get("ip", "")
                try:
                    port = int(t.get("port", AGENT_PORT_DEFAULT) or AGENT_PORT_DEFAULT)
                except (TypeError, ValueError):
                    port = AGENT_PORT_DEFAULT
                res = check_apps(ip, port, log=False)   # jangan spam Oracle dari worker
                if not res.get("ok"):
                    continue                            # agen tak terjangkau -> pertahankan cache lama
                flagged = res.get("flagged", [])
                keys = frozenset(str(f["name"]).lower() for f in flagged)
                prevkeys = _sec_cache.get(name, {}).get("keys") or frozenset()
                new = [f for f in flagged if str(f["name"]).lower() not in prevkeys]
                if new:
                    log_detection(res.get("hostname", ip), ip, new)   # audit hanya yang baru
                _sec_cache[name] = {"count": len(flagged), "flagged": flagged,
                                    "checked": time.strftime("%H:%M:%S"), "keys": keys}
        except Exception as e:
            print("  [Keamanan] scan gagal:", str(e)[:120])
        time.sleep(SECURITY_INTERVAL)


def critical_scan_worker():
    """Cek service/port kritis tiap server berkala -> isi _crit_cache untuk badge,
    dan catat PERUBAHAN status (up<->down) ke Oracle sebagai alarm/audit."""
    time.sleep(12)   # beri jeda saat start
    while True:
        try:
            for t in load_targets():
                name = t.get("name", t.get("ip")); ip = t.get("ip", "")
                try:
                    port = int(t.get("port", AGENT_PORT_DEFAULT) or AGENT_PORT_DEFAULT)
                except (TypeError, ValueError):
                    port = AGENT_PORT_DEFAULT
                res = check_critical(name, ip, port)
                if not res.get("ok") or res.get("no_config"):
                    continue
                items = res.get("items", [])
                prev = (_crit_cache.get(name, {}) or {}).get("state") or {}
                changed, state = [], {}
                for it in items:
                    key = it["jenis"] + ":" + it["nama"]
                    state[key] = it["ok"]
                    if key in prev and prev[key] != it["ok"]:
                        changed.append(it)                      # transisi hidup<->mati
                    elif key not in prev and not it["ok"]:
                        changed.append(it)                      # pertama kali & sudah mati
                if changed:
                    log_status_change(name, ip, changed)
                downkeys = [k for k in state if not state[k]]
                _crit_cache[name] = {"down": len(downkeys), "total": len(items), "items": items,
                                     "checked": time.strftime("%H:%M:%S"), "state": state}
        except Exception as e:
            print("  [Kritis] scan gagal:", str(e)[:120])
        time.sleep(CRITICAL_INTERVAL)


def retention_worker():
    """Bersihkan data lama agar tabel tak membengkak (metrik >N hari, audit >365 hari). Tiap 12 jam."""
    time.sleep(30)
    while True:
        try:
            if ORACLE_ENABLED and ORACLE_AVAILABLE:
                conn = get_oracle_conn(); cur = conn.cursor()
                cur.execute("DELETE FROM server_monitoring WHERE waktu < SYSDATE - :d", {"d": ORACLE_RETENTION_DAYS})
                n = cur.rowcount
                cur.execute("DELETE FROM deteksi_aplikasi WHERE waktu < SYSDATE - 365")
                cur.execute("DELETE FROM status_layanan WHERE waktu < SYSDATE - 365")
                conn.commit()
                if n:
                    print(f"  [Retensi] hapus {n} baris metrik lebih tua dari {ORACLE_RETENTION_DAYS} hari")
        except Exception as e:
            _oracle["conn"] = None
            print("  [Retensi] gagal:", str(e)[:120])
        time.sleep(12 * 3600)


def main():
    ip_lokal = socket.gethostbyname(socket.gethostname())
    print("=" * 60)
    print("  DASHBOARD MONITORING SERVER  --  PT Badak NGL (prototipe)")
    print("=" * 60)
    print(f"  Buka di Chrome : http://localhost:{PORT}")
    print(f"  Dari HP/PC lain: http://{ip_lokal}:{PORT}")
    print(f"  Riwayat CSV    : {LOG_DIR}  (rekam tiap {LOG_INTERVAL} detik)")
    if ORACLE_ENABLED and ORACLE_AVAILABLE:
        print(f"  Oracle         : {ORACLE_USER}@{ORACLE_DSN}  (simpan tiap {ORACLE_INTERVAL} detik)")
    elif ORACLE_ENABLED and not ORACLE_AVAILABLE:
        print("  Oracle         : library 'oracledb' belum ada (pip install oracledb)")
    print("  Tekan Ctrl+C untuk berhenti.")
    print("=" * 60)
    print(f"  Keamanan       : pindai aplikasi tiap {SECURITY_INTERVAL} detik (penanda perisai di kartu)")
    print(f"  Layanan kritis : cek service/port tiap {CRITICAL_INTERVAL} detik (Exaquantum/SQL/historian)")
    if ORACLE_ENABLED and ORACLE_AVAILABLE:
        ensure_critical_table()
        ensure_situs_table()
        sync_labels_to_oracle()   # tabel server_label: nama panggilan utk query SQL Developer
    threading.Thread(target=log_worker, daemon=True).start()    # pencatatan CSV background
    if ORACLE_ENABLED and ORACLE_AVAILABLE:
        threading.Thread(target=oracle_log_worker, daemon=True).start()   # pencatatan Oracle background
    threading.Thread(target=security_scan_worker, daemon=True).start()    # pemindai aplikasi -> penanda 🛡️
    threading.Thread(target=critical_scan_worker, daemon=True).start()    # cek service/port kritis
    threading.Thread(target=retention_worker, daemon=True).start()        # bersihkan data lama
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
