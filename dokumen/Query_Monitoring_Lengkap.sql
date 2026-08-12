-- ============================================================================
--  QUERY LENGKAP - SISTEM MONITORING SERVER PT BADAK NGL
--  Database : Oracle XE 21c (Local XE) - user: monitoring
--  Tabel    : 1) monitoring.server_monitoring  (metrik CPU/RAM/Disk/Network, tiap 10 detik)
--             2) monitoring.deteksi_aplikasi   (aplikasi mencurigakan yang ter-flag)
--             3) monitoring.status_layanan     (alarm service & port kritis, dicatat saat berubah)
--             4) monitoring.deteksi_situs      (situs berbahaya dari riwayat browser)
--             5) monitoring.server_label       (hostname -> nama panggilan, disinkron OTOMATIS
--                oleh dashboard dari targets.json; query di sini me-LEFT JOIN tabel ini
--                supaya hasilnya menampilkan nama yang sama dengan di dashboard.
--                Catatan: restart dashboard_server.py SEKALI setelah pembaruan ini
--                agar tabelnya terbuat & terisi)
--
--  CARA PAKAI di SQL Developer:
--    1) Kerjakan "PENGATURAN" di bawah SEKALI per sesi - pilih server dengan
--       Cara A (pilih pakai NOMOR, tanpa ketik nama) atau Cara B (ketik manual).
--    2) Setelah itu tinggal klik query mana pun -> Ctrl+Enter. Semua query
--       otomatis memakai server yang dipilih (tidak perlu edit satu-satu).
--    3) Mau ganti server? Ulangi langkah PENGATURAN dengan nomor/nama lain.
--    JANGAN tekan F5/Run Script untuk SELURUH file (Bagian 6 berisi DELETE!) -
--    F5 hanya untuk BLOK KECIL yang diminta di Cara A.
-- ============================================================================

-- ############################################################################
-- PENGATURAN - PILIH SERVER DI SINI (2 cara: A otomatis pakai nomor, B ketik manual)
-- ############################################################################

-- ========== CARA A - OTOMATIS: pilih server pakai NOMOR (tanpa ketik nama) =========
-- Langkah 1: jalankan query ini dulu (Ctrl+Enter) untuk melihat daftar server bernomor:

SELECT no, nama_server, nama_panggilan, ip_terakhir FROM (
    SELECT ROW_NUMBER() OVER (ORDER BY x.nama_server) AS no, x.*
    FROM (
        SELECT m.nama_server,
               NVL(MAX(l.label), '-') AS nama_panggilan,
               MAX(m.ip) KEEP (DENSE_RANK LAST ORDER BY m.waktu) AS ip_terakhir
        FROM monitoring.server_monitoring m
        LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
        GROUP BY m.nama_server
    ) x
);

-- Langkah 2: isi NOMOR pilihanmu di bawah, lalu BLOK mulai baris "DEFINE NOMOR"
--            sampai titik-koma SELECT di bawahnya -> tekan F5 (Run Script).
--            NAMA_SERVER dan IP_SERVER akan TERISI OTOMATIS dari nomor itu.
--            (wajib F5/Run Script, bukan Ctrl+Enter, supaya variabelnya tersimpan)

DEFINE NOMOR = 1                            -- <== ganti: nomor server dari daftar Langkah 1

COLUMN nama_server NEW_VALUE NAMA_SERVER
COLUMN ip          NEW_VALUE IP_SERVER
SELECT nama_server, ip FROM (
    SELECT x.nama_server, x.ip, ROW_NUMBER() OVER (ORDER BY x.nama_server) AS no
    FROM (
        SELECT m.nama_server,
               MAX(m.ip) KEEP (DENSE_RANK LAST ORDER BY m.waktu) AS ip
        FROM monitoring.server_monitoring m
        GROUP BY m.nama_server
    ) x
) WHERE no = &NOMOR;

-- ========== CARA B - MANUAL: ketik sendiri nama & IP (kalau lebih suka begini) =========
-- NAMA_SERVER = hostname yang tampil di dashboard (mis. DESKTOP-22AN8KV, HYPE) -
-- BUKAN label/nama panggilan spt "Laptop Belva", karena Oracle menyimpan hostname.
-- Isi nilainya, blok 2 baris DEFINE di bawah -> Ctrl+Enter.
-- (LEWATI 2 baris ini kalau sudah pakai Cara A - jangan dijalankan lagi,
--  karena akan menimpa hasil pilihan nomor dari Cara A.)

DEFINE NAMA_SERVER = 'DESKTOP-22AN8KV'      -- <== ganti: server yang mau dicek/dihapus
DEFINE IP_SERVER   = '10.10.88.144'         -- <== ganti: IP server tsb (dipakai query tertentu)

-- ========== RENTANG TANGGAL (dipakai query 1.5 dan 6.C - isi manual) =========
DEFINE TGL_AWAL  = '2026-08-12'             -- <== ganti: awal rentang tanggal (YYYY-MM-DD)
DEFINE TGL_AKHIR = '2026-08-13'             -- <== ganti: akhir rentang (data SEBELUM tanggal ini)

-- Cek hasil pilihan saat ini (opsional, jalankan sebagai script/F5): tampil di tab Script Output
-- DEFINE NAMA_SERVER
-- DEFINE IP_SERVER

-- Kalau saat menjalankan query malah muncul pop-up "Enter Substitution Variable",
-- artinya PENGATURAN belum dijalankan di sesi ini -> klik Cancel, kerjakan Cara A
-- (atau Cara B) dulu, baru ulangi query-nya.


-- ############################################################################
-- BAGIAN 0. CEK AWAL (memastikan koneksi & data masuk)
-- ############################################################################

-- 0.1 Lihat daftar tabel milik user monitoring (memastikan 4 tabel sudah ada)
SELECT table_name FROM all_tables WHERE owner = 'MONITORING';

-- 0.2 Hitung jumlah baris tiap tabel (cek data masuk atau tidak)
SELECT 'server_monitoring' AS tabel, COUNT(*) AS jumlah_baris FROM monitoring.server_monitoring
UNION ALL
SELECT 'deteksi_aplikasi', COUNT(*) FROM monitoring.deteksi_aplikasi
UNION ALL
SELECT 'status_layanan', COUNT(*) FROM monitoring.status_layanan
UNION ALL
SELECT 'deteksi_situs', COUNT(*) FROM monitoring.deteksi_situs;

-- 0.3 Kapan data TERAKHIR masuk? (kalau waktunya lama, berarti dashboard mati / Oracle tidak tersambung)
SELECT MAX(waktu) AS data_terakhir_masuk FROM monitoring.server_monitoring;

-- 0.4 Server apa saja yang pernah terekam + rentang datanya
--     (jalankan ini dulu untuk tahu nama server yang sah dipakai di PENGATURAN)
--     Kolom nama_panggilan = label yang diketik lewat tombol pensil di dashboard
--     ('-' artinya server itu belum diberi nama panggilan)
SELECT m.nama_server,
       NVL(MAX(l.label), '-') AS nama_panggilan,
       m.ip,
       MIN(m.waktu) AS data_pertama,
       MAX(m.waktu) AS data_terakhir,
       COUNT(*)     AS jumlah_data
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
GROUP BY m.nama_server, m.ip
ORDER BY m.nama_server;

-- 0.5 Daftar pasangan hostname <-> nama panggilan yang dikenal dashboard saat ini
--     (isi tabel ini otomatis mengikuti targets.json; label kosong = belum diberi nama)
SELECT nama_server, NVL(label, '-') AS nama_panggilan
FROM monitoring.server_label
ORDER BY nama_server;

-- RESEP: mau menampilkan nama panggilan di query lain? Tambahkan 2 hal ini:
--   1) di bagian FROM :  LEFT JOIN monitoring.server_label l ON l.nama_server = <kolom nama servernya>
--   2) di bagian SELECT: NVL(l.label, '-') AS nama_panggilan
-- (untuk tabel deteksi_aplikasi/status_layanan/deteksi_situs, kolom namanya "server",
--  jadi JOIN-nya: ON l.nama_server = server)


-- ############################################################################
-- BAGIAN 1. LIHAT DATA METRIK (DETAIL)
-- ############################################################################

-- 1.1 Semua data monitoring SEMUA server, terbaru paling atas (bisa lambat kalau sudah jutaan baris)
SELECT m.waktu, m.nama_server, NVL(l.label, '-') AS nama_panggilan, m.ip, m.status,
       m.latency_ms, m.cpu_pct, m.mem_pct, m.disk_pct, m.rx_kbps, m.tx_kbps
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
ORDER BY m.waktu DESC;

-- 1.2 Hanya 100 data TERBARU semua server (lebih cepat, cocok untuk cek harian)
SELECT m.waktu, m.nama_server, NVL(l.label, '-') AS nama_panggilan, m.ip, m.status,
       m.latency_ms, m.cpu_pct, m.mem_pct, m.disk_pct, m.rx_kbps, m.tx_kbps
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
ORDER BY m.waktu DESC
FETCH FIRST 100 ROWS ONLY;

-- 1.3 Data 1 SERVER (server = sesuai PENGATURAN di atas)
SELECT m.waktu, m.nama_server, NVL(l.label, '-') AS nama_panggilan, m.ip, m.status,
       m.latency_ms, m.cpu_pct, m.mem_pct, m.disk_pct, m.rx_kbps, m.tx_kbps
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
WHERE m.nama_server = '&NAMA_SERVER'
ORDER BY m.waktu DESC;

-- 1.4 Data 1 server dalam RENTANG WAKTU terakhir
SELECT m.waktu, m.nama_server, NVL(l.label, '-') AS nama_panggilan, m.ip, m.status,
       m.latency_ms, m.cpu_pct, m.mem_pct, m.disk_pct, m.rx_kbps, m.tx_kbps
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
WHERE m.nama_server = '&NAMA_SERVER'
  AND m.waktu >= SYSDATE - INTERVAL '1' DAY    -- ganti: '3' HOUR / '7' DAY / '30' DAY
ORDER BY m.waktu DESC;

-- 1.5 Data 1 server pada rentang TANGGAL tertentu (tanggal = sesuai PENGATURAN)
SELECT m.waktu, m.nama_server, NVL(l.label, '-') AS nama_panggilan, m.ip, m.status,
       m.latency_ms, m.cpu_pct, m.mem_pct, m.disk_pct, m.rx_kbps, m.tx_kbps
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
WHERE m.nama_server = '&NAMA_SERVER'
  AND m.waktu >= TO_DATE('&TGL_AWAL',  'YYYY-MM-DD')
  AND m.waktu <  TO_DATE('&TGL_AKHIR', 'YYYY-MM-DD')
ORDER BY m.waktu;

-- 1.6 KONDISI TERKINI semua server (1 baris terakhir per server - mirip tampilan kartu dashboard,
--     lengkap dengan nama panggilan seperti di kartu)
SELECT t.nama_server,
       NVL(l.label, '-') AS nama_panggilan,
       t.ip, t.status, t.latency_ms, t.cpu_pct, t.mem_pct, t.disk_pct, t.waktu
FROM (
    SELECT s.*,
           ROW_NUMBER() OVER (PARTITION BY nama_server ORDER BY waktu DESC) AS rn
    FROM monitoring.server_monitoring s
) t
LEFT JOIN monitoring.server_label l ON l.nama_server = t.nama_server
WHERE t.rn = 1
ORDER BY t.nama_server;


-- ############################################################################
-- BAGIAN 2. RINGKASAN & STATISTIK
-- ############################################################################

-- 2.1 Ringkasan SELURUH server: rata-rata CPU/RAM/Disk + jumlah data (dengan nama panggilan)
SELECT m.nama_server,
       NVL(MAX(l.label), '-') AS nama_panggilan,
       ROUND(AVG(m.cpu_pct), 1)  AS cpu_rata2,
       ROUND(AVG(m.mem_pct), 1)  AS ram_rata2,
       ROUND(AVG(m.disk_pct), 1) AS disk_rata2,
       ROUND(AVG(m.latency_ms), 1) AS latensi_rata2,
       COUNT(*) AS jumlah_data
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
GROUP BY m.nama_server
ORDER BY m.nama_server;

-- 2.2 Ringkasan LENGKAP per server: rata-rata + tertinggi (untuk laporan)
SELECT m.nama_server,
       NVL(MAX(l.label), '-') AS nama_panggilan,
       ROUND(AVG(m.cpu_pct), 1) AS cpu_avg,  MAX(m.cpu_pct)  AS cpu_max,
       ROUND(AVG(m.mem_pct), 1) AS ram_avg,  MAX(m.mem_pct)  AS ram_max,
       ROUND(AVG(m.disk_pct), 1) AS disk_avg, MAX(m.disk_pct) AS disk_max,
       MAX(m.latency_ms) AS latensi_max
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
GROUP BY m.nama_server
ORDER BY m.nama_server;

-- 2.3 % UPTIME per server (persentase waktu server ONLINE - angka penting untuk laporan PKL)
SELECT m.nama_server,
       NVL(MAX(l.label), '-') AS nama_panggilan,
       COUNT(*) AS total_cek,
       SUM(CASE WHEN m.status = 'ONLINE' THEN 1 ELSE 0 END) AS jml_online,
       ROUND(100 * SUM(CASE WHEN m.status = 'ONLINE' THEN 1 ELSE 0 END) / COUNT(*), 2) AS uptime_persen
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
GROUP BY m.nama_server
ORDER BY uptime_persen DESC;

-- 2.4 TREN PER JAM 1 server, 24 jam terakhir (rata-rata tiap jam - bahan grafik di Excel)
SELECT TO_CHAR(TRUNC(waktu, 'HH24'), 'DD-MON-YYYY HH24:MI') AS jam,
       ROUND(AVG(cpu_pct), 1)  AS cpu_rata2,
       ROUND(AVG(mem_pct), 1)  AS ram_rata2,
       ROUND(AVG(disk_pct), 1) AS disk_rata2
FROM monitoring.server_monitoring
WHERE nama_server = '&NAMA_SERVER'
  AND waktu >= SYSDATE - INTERVAL '1' DAY
GROUP BY TRUNC(waktu, 'HH24')
ORDER BY TRUNC(waktu, 'HH24');

-- 2.5 TREN PER HARI seluruh server (rekap harian - bahan laporan mingguan/bulanan)
SELECT TO_CHAR(TRUNC(m.waktu), 'DD-MON-YYYY') AS tanggal,
       m.nama_server,
       NVL(MAX(l.label), '-') AS nama_panggilan,
       ROUND(AVG(m.cpu_pct), 1)  AS cpu_rata2,
       ROUND(AVG(m.mem_pct), 1)  AS ram_rata2,
       ROUND(AVG(m.disk_pct), 1) AS disk_rata2,
       ROUND(100 * SUM(CASE WHEN m.status = 'ONLINE' THEN 1 ELSE 0 END) / COUNT(*), 1) AS uptime_persen
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
GROUP BY TRUNC(m.waktu), m.nama_server
ORDER BY TRUNC(m.waktu) DESC, m.nama_server;

-- 2.6 SERVER BERMASALAH: yang pernah melewati ambang batas (CPU>85 / RAM>90 / Disk>90)
--     (ambang sama dengan REPORT_BATAS di dashboard - cocokkan dengan angka resmi Pak Erwan)
SELECT m.nama_server,
       NVL(MAX(l.label), '-') AS nama_panggilan,
       SUM(CASE WHEN m.cpu_pct  > 85 THEN 1 ELSE 0 END) AS kejadian_cpu_tinggi,
       SUM(CASE WHEN m.mem_pct  > 90 THEN 1 ELSE 0 END) AS kejadian_ram_tinggi,
       SUM(CASE WHEN m.disk_pct > 90 THEN 1 ELSE 0 END) AS kejadian_disk_tinggi
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
GROUP BY m.nama_server
HAVING SUM(CASE WHEN m.cpu_pct > 85 THEN 1 ELSE 0 END) > 0
    OR SUM(CASE WHEN m.mem_pct > 90 THEN 1 ELSE 0 END) > 0
    OR SUM(CASE WHEN m.disk_pct > 90 THEN 1 ELSE 0 END) > 0
ORDER BY m.nama_server;

-- 2.7 Momen PALING BERAT tiap server (kapan CPU tertinggi terjadi)
SELECT t.nama_server, NVL(l.label, '-') AS nama_panggilan,
       t.waktu, t.cpu_pct, t.mem_pct, t.disk_pct
FROM (
    SELECT s.*, ROW_NUMBER() OVER (PARTITION BY nama_server ORDER BY cpu_pct DESC) AS rn
    FROM monitoring.server_monitoring s
) t
LEFT JOIN monitoring.server_label l ON l.nama_server = t.nama_server
WHERE t.rn = 1
ORDER BY t.nama_server;

-- 2.8 Riwayat OFFLINE 1 server (kapan saja server tercatat mati)
SELECT m.waktu, m.nama_server, NVL(l.label, '-') AS nama_panggilan, m.ip, m.status
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
WHERE m.nama_server = '&NAMA_SERVER'
  AND m.status <> 'ONLINE'
ORDER BY m.waktu DESC;


-- ############################################################################
-- BAGIAN 3. DETEKSI APLIKASI MENCURIGAKAN (tabel deteksi_aplikasi)
-- ############################################################################

-- 3.1 Semua temuan aplikasi ter-flag SEMUA server, terbaru paling atas
SELECT d.waktu, d.server, NVL(l.label, '-') AS nama_panggilan, d.ip, d.aplikasi, d.kategori
FROM monitoring.deteksi_aplikasi d
LEFT JOIN monitoring.server_label l ON l.nama_server = d.server
ORDER BY d.waktu DESC;

-- 3.2 Temuan aplikasi di 1 server (sesuai PENGATURAN)
SELECT d.waktu, d.server, NVL(l.label, '-') AS nama_panggilan, d.ip, d.aplikasi, d.kategori
FROM monitoring.deteksi_aplikasi d
LEFT JOIN monitoring.server_label l ON l.nama_server = d.server
WHERE d.server = '&NAMA_SERVER'
ORDER BY d.waktu DESC;

-- 3.3 Rekap: aplikasi apa yang paling sering terdeteksi + di berapa server
SELECT aplikasi, kategori,
       COUNT(*) AS berapa_kali,
       COUNT(DISTINCT server) AS di_berapa_server,
       MAX(waktu) AS terakhir_terlihat
FROM monitoring.deteksi_aplikasi
GROUP BY aplikasi, kategori
ORDER BY berapa_kali DESC;

-- 3.4 Rekap per KATEGORI per server (game/torrent/vpn/dll - bahan laporan keamanan)
SELECT d.server, NVL(MAX(l.label), '-') AS nama_panggilan, d.kategori, COUNT(*) AS jumlah_temuan
FROM monitoring.deteksi_aplikasi d
LEFT JOIN monitoring.server_label l ON l.nama_server = d.server
GROUP BY d.server, d.kategori
ORDER BY d.server, jumlah_temuan DESC;


-- ############################################################################
-- BAGIAN 4. LAYANAN & PORT KRITIS (tabel status_layanan)
--   Dicatat HANYA saat status berubah (hidup->mati / mati->hidup) = riwayat alarm
-- ############################################################################

-- 4.1 Semua riwayat alarm layanan kritis, terbaru paling atas
--     ok = 1 artinya layanan HIDUP kembali, ok = 0 artinya layanan MATI
SELECT s.waktu, s.server, NVL(l.label, '-') AS nama_panggilan, s.ip, s.jenis, s.nama, s.status,
       CASE s.ok WHEN 1 THEN 'HIDUP' ELSE 'MATI' END AS kondisi
FROM monitoring.status_layanan s
LEFT JOIN monitoring.server_label l ON l.nama_server = s.server
ORDER BY s.waktu DESC;

-- 4.2 Alarm yang layanannya MATI saja (kejadian penting untuk ditindaklanjuti)
SELECT s.waktu, s.server, NVL(l.label, '-') AS nama_panggilan, s.jenis, s.nama, s.status
FROM monitoring.status_layanan s
LEFT JOIN monitoring.server_label l ON l.nama_server = s.server
WHERE s.ok = 0
ORDER BY s.waktu DESC;

-- 4.3 KONDISI TERAKHIR tiap layanan (status terkini: masih mati atau sudah hidup lagi?)
SELECT t.server, NVL(l.label, '-') AS nama_panggilan, t.jenis, t.nama,
       CASE t.ok WHEN 1 THEN 'HIDUP' ELSE 'MATI' END AS kondisi_terakhir,
       t.waktu AS sejak
FROM (
    SELECT s.*,
           ROW_NUMBER() OVER (PARTITION BY server, jenis, nama ORDER BY waktu DESC) AS rn
    FROM monitoring.status_layanan s
) t
LEFT JOIN monitoring.server_label l ON l.nama_server = t.server
WHERE t.rn = 1
ORDER BY t.server, t.jenis, t.nama;

-- 4.4 Layanan yang PALING SERING mati (perlu perhatian khusus)
SELECT s.server, NVL(MAX(l.label), '-') AS nama_panggilan, s.jenis, s.nama,
       SUM(CASE WHEN s.ok = 0 THEN 1 ELSE 0 END) AS berapa_kali_mati,
       MAX(s.waktu) AS kejadian_terakhir
FROM monitoring.status_layanan s
LEFT JOIN monitoring.server_label l ON l.nama_server = s.server
GROUP BY s.server, s.jenis, s.nama
ORDER BY berapa_kali_mati DESC;


-- ############################################################################
-- BAGIAN 5. DETEKSI SITUS BERBAHAYA (tabel deteksi_situs)
-- ############################################################################

-- 5.1 Semua temuan situs berbahaya SEMUA server, terbaru paling atas
SELECT d.waktu, d.server, NVL(l.label, '-') AS nama_panggilan, d.ip, d.domain, d.kategori
FROM monitoring.deteksi_situs d
LEFT JOIN monitoring.server_label l ON l.nama_server = d.server
ORDER BY d.waktu DESC;

-- 5.2 Temuan situs di 1 server (sesuai PENGATURAN)
SELECT d.waktu, d.server, NVL(l.label, '-') AS nama_panggilan, d.ip, d.domain, d.kategori
FROM monitoring.deteksi_situs d
LEFT JOIN monitoring.server_label l ON l.nama_server = d.server
WHERE d.server = '&NAMA_SERVER'
ORDER BY d.waktu DESC;

-- 5.3 Rekap: domain apa yang paling sering muncul + kategorinya
SELECT domain, kategori,
       COUNT(*) AS berapa_kali,
       COUNT(DISTINCT server) AS di_berapa_server,
       MAX(waktu) AS terakhir_terlihat
FROM monitoring.deteksi_situs
GROUP BY domain, kategori
ORDER BY berapa_kali DESC;

-- 5.4 Rekap kategori situs per server (judi/porno/phishing/dll)
SELECT d.server, NVL(MAX(l.label), '-') AS nama_panggilan, d.kategori, COUNT(*) AS jumlah_temuan
FROM monitoring.deteksi_situs d
LEFT JOIN monitoring.server_label l ON l.nama_server = d.server
GROUP BY d.server, d.kategori
ORDER BY d.server, jumlah_temuan DESC;


-- ############################################################################
-- BAGIAN 6. HAPUS DATA  !!! HATI-HATI - DATA YANG DIHAPUS TIDAK BISA KEMBALI !!!
--
--   Semua query di bawah otomatis memakai NAMA_SERVER / tanggal dari PENGATURAN,
--   jadi pastikan PENGATURAN di atas sudah benar SEBELUM menghapus.
--
--   ATURAN MAIN yang aman:
--   1) SELALU jalankan query "lihat dulu" (SELECT) sebelum DELETE,
--      supaya tahu persis berapa data yang akan terhapus.
--   2) Setelah DELETE, data belum benar-benar hilang sampai kamu COMMIT.
--      - COMMIT;   -> simpan permanen (tidak bisa dibatalkan lagi)
--      - ROLLBACK; -> batalkan DELETE (kalau ternyata salah hapus)
-- ############################################################################

-- ---------- 6.A HAPUS DATA 1 SERVER (server = sesuai PENGATURAN) ----------

-- 6.A.1 LIHAT DULU berapa data yang akan terhapus (wajib sebelum DELETE!)
SELECT COUNT(*) AS akan_terhapus FROM monitoring.server_monitoring
WHERE nama_server = '&NAMA_SERVER';

-- 6.A.2 Hapus data metrik server tsb
DELETE FROM monitoring.server_monitoring
WHERE nama_server = '&NAMA_SERVER';

-- 6.A.3 Hapus juga jejaknya di tabel audit (opsional, biar bersih total)
DELETE FROM monitoring.deteksi_aplikasi WHERE server = '&NAMA_SERVER';
DELETE FROM monitoring.status_layanan   WHERE server = '&NAMA_SERVER';
DELETE FROM monitoring.deteksi_situs    WHERE server = '&NAMA_SERVER';

-- 6.A.4 Simpan permanen (atau ROLLBACK; kalau salah)
COMMIT;

-- (Varian: hapus lebih spesifik pakai nama + IP sekaligus - berguna kalau
--  hostname sama pernah tercatat dengan beberapa IP dan mau hapus salah satunya)
-- SELECT COUNT(*) AS akan_terhapus FROM monitoring.server_monitoring
-- WHERE nama_server = '&NAMA_SERVER' AND ip = '&IP_SERVER';
-- DELETE FROM monitoring.server_monitoring
-- WHERE nama_server = '&NAMA_SERVER' AND ip = '&IP_SERVER';
-- COMMIT;

-- ---------- 6.A-ALT HAPUS PAKAI NAMA PANGGILAN (label dashboard) ----------
-- Data di Oracle tersimpan dengan HOSTNAME, jadi DELETE biasa harus pakai hostname.
-- Tapi lewat tabel server_label, label bisa "diterjemahkan" otomatis ke hostname.
-- Ganti nilai label di bawah (contoh: 'Laptop Belva' / 'zaky').

DEFINE NAMA_PANGGILAN = 'Laptop Belva'      -- <== ganti: label yang tampil di dashboard

-- 6.A-ALT.1 LIHAT DULU: label ini milik hostname siapa & berapa datanya?
SELECT m.nama_server, COUNT(*) AS akan_terhapus
FROM monitoring.server_monitoring m
WHERE m.nama_server IN (SELECT nama_server FROM monitoring.server_label
                        WHERE LOWER(label) = LOWER('&NAMA_PANGGILAN'))
GROUP BY m.nama_server;

-- 6.A-ALT.2 Hapus data metrik server yang labelnya tsb
DELETE FROM monitoring.server_monitoring
WHERE nama_server IN (SELECT nama_server FROM monitoring.server_label
                      WHERE LOWER(label) = LOWER('&NAMA_PANGGILAN'));

-- 6.A-ALT.3 (opsional) hapus juga jejaknya di tabel audit
DELETE FROM monitoring.deteksi_aplikasi
WHERE server IN (SELECT nama_server FROM monitoring.server_label
                 WHERE LOWER(label) = LOWER('&NAMA_PANGGILAN'));
DELETE FROM monitoring.status_layanan
WHERE server IN (SELECT nama_server FROM monitoring.server_label
                 WHERE LOWER(label) = LOWER('&NAMA_PANGGILAN'));
DELETE FROM monitoring.deteksi_situs
WHERE server IN (SELECT nama_server FROM monitoring.server_label
                 WHERE LOWER(label) = LOWER('&NAMA_PANGGILAN'));

COMMIT;

-- Catatan varian label ini:
--   * Kalau hasil 6.A-ALT.1 KOSONG, berarti label salah ketik / belum disinkron
--     (restart dashboard sekali) -> jangan lanjut DELETE.
--   * Label hanya dikenal untuk server yang MASIH terdaftar di dashboard.
--     Server yang sudah dihapus dari dashboard tidak punya label lagi ->
--     pakai cara 6.A biasa dengan hostname.

-- ---------- 6.B HAPUS DATA LAMA SAJA (data terbaru tetap disimpan) ----------

-- 6.B.1 Lihat dulu: berapa baris yang lebih tua dari 30 hari?
SELECT COUNT(*) AS akan_terhapus FROM monitoring.server_monitoring
WHERE waktu < SYSDATE - 30;      -- angka 30 = hari, silakan ganti

-- 6.B.2 Hapus data metrik yang lebih tua dari 30 hari
--       (catatan: dashboard sudah punya retention otomatis 90 hari;
--        query ini untuk hapus manual kalau perlu lebih cepat)
DELETE FROM monitoring.server_monitoring
WHERE waktu < SYSDATE - 30;

COMMIT;

-- ---------- 6.C HAPUS DATA RENTANG TANGGAL TERTENTU (tanggal = sesuai PENGATURAN) ----------
-- Berguna untuk membuang data uji coba di hari tertentu.

-- 6.C.1 Lihat dulu
SELECT COUNT(*) AS akan_terhapus FROM monitoring.server_monitoring
WHERE nama_server = '&NAMA_SERVER'
  AND waktu >= TO_DATE('&TGL_AWAL',  'YYYY-MM-DD')
  AND waktu <  TO_DATE('&TGL_AKHIR', 'YYYY-MM-DD');

-- 6.C.2 Hapus
DELETE FROM monitoring.server_monitoring
WHERE nama_server = '&NAMA_SERVER'
  AND waktu >= TO_DATE('&TGL_AWAL',  'YYYY-MM-DD')
  AND waktu <  TO_DATE('&TGL_AKHIR', 'YYYY-MM-DD');

COMMIT;

-- ---------- 6.D HAPUS SEMUA DATA SEMUA SERVER (reset total) ----------
-- Dipakai misalnya: selesai uji coba, mau mulai pengambilan data resmi dari nol.

-- Cara 1: DELETE (masih bisa ROLLBACK sebelum COMMIT, tapi lambat kalau data jutaan)
DELETE FROM monitoring.server_monitoring;
DELETE FROM monitoring.deteksi_aplikasi;
DELETE FROM monitoring.status_layanan;
DELETE FROM monitoring.deteksi_situs;
COMMIT;

-- Cara 2: TRUNCATE (langsung permanen TANPA bisa ROLLBACK, tapi sangat cepat)
-- TRUNCATE TABLE monitoring.server_monitoring;
-- TRUNCATE TABLE monitoring.deteksi_aplikasi;
-- TRUNCATE TABLE monitoring.status_layanan;
-- TRUNCATE TABLE monitoring.deteksi_situs;


-- ############################################################################
-- BAGIAN 7. PEMELIHARAAN & INFO TAMBAHAN (berguna untuk admin)
-- ############################################################################

-- 7.1 Perkiraan pemakaian ruang disk tiap tabel (MB) - pilih sesuai login:

-- 7.1a Kalau login sebagai user MONITORING (lihat tabel milik sendiri):
SELECT segment_name AS tabel,
       ROUND(bytes / 1024 / 1024, 2) AS ukuran_mb
FROM user_segments
WHERE segment_type = 'TABLE'
ORDER BY bytes DESC;

-- 7.1b Kalau login sebagai SYSTEM (butuh hak admin; error ORA-00942 kalau bukan):
-- SELECT segment_name AS tabel,
--        ROUND(bytes / 1024 / 1024, 2) AS ukuran_mb
-- FROM dba_segments
-- WHERE owner = 'MONITORING' AND segment_type = 'TABLE'
-- ORDER BY bytes DESC;

-- 7.2 Kecepatan data masuk: berapa baris per hari (memperkirakan pertumbuhan database)
SELECT TO_CHAR(TRUNC(waktu), 'DD-MON-YYYY') AS tanggal,
       COUNT(*) AS baris_masuk
FROM monitoring.server_monitoring
GROUP BY TRUNC(waktu)
ORDER BY TRUNC(waktu) DESC;

-- 7.3 Riwayat pergantian IP: server sama tapi tercatat dengan IP berbeda
--     (wajar kalau IP DHCP berubah; berguna untuk menelusuri riwayat IP)
SELECT m.nama_server, NVL(MAX(l.label), '-') AS nama_panggilan, m.ip,
       MIN(m.waktu) AS pertama, MAX(m.waktu) AS terakhir, COUNT(*) AS jml
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
GROUP BY m.nama_server, m.ip
HAVING m.nama_server IN (
    SELECT nama_server FROM monitoring.server_monitoring
    GROUP BY nama_server HAVING COUNT(DISTINCT ip) > 1
)
ORDER BY m.nama_server, pertama;

-- 7.4 Rata-rata pemakaian JARINGAN per server (KB per detik)
SELECT m.nama_server,
       NVL(MAX(l.label), '-') AS nama_panggilan,
       ROUND(AVG(m.rx_kbps), 1) AS unduh_rata2_kbps,
       ROUND(AVG(m.tx_kbps), 1) AS unggah_rata2_kbps,
       ROUND(MAX(m.rx_kbps), 1) AS unduh_tertinggi_kbps,
       ROUND(MAX(m.tx_kbps), 1) AS unggah_tertinggi_kbps
FROM monitoring.server_monitoring m
LEFT JOIN monitoring.server_label l ON l.nama_server = m.nama_server
WHERE m.status = 'ONLINE'
GROUP BY m.nama_server
ORDER BY m.nama_server;

-- ============================================================================
-- SELESAI.
-- Ingat alurnya: (1) edit PENGATURAN paling atas -> jalankan baris DEFINE,
--                (2) jalankan query yang dibutuhkan saja dengan Ctrl+Enter.
-- Kalau SQL Developer malah MENANYAKAN nilai (muncul pop-up "Enter Substitution
-- Variable"), artinya baris DEFINE belum dijalankan di sesi ini - blok 4 baris
-- DEFINE lalu Ctrl+Enter dulu.
-- ============================================================================
