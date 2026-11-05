"""Taşınabilir ZIP'i boşluklu/Türkçe bir klasöre açıp yalnız kendi Python'uyla dener.

1) ZIP'te yasak dosya yok mu (oturum, çıktı, sır, test)?
2) CLI: profilleri oluşturur, demo profilini çalıştırıp Excel yazar.
3) Arayüz sunucusu açılıyor ve anahtarsız isteği reddediyor mu?
4) Gömülü Python + Playwright, gerçek Chrome ile SENTETİK sahte panelde tüm okuma testlerini geçiyor mu?
Netmera'ya veya internete bağlanmaz (sahte panel istekleri Playwright'ta yakalanır).
"""
from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netmera_automation import __version__  # noqa: E402

ZIP = ROOT / "dist" / f"NetmeraOtomasyonu-{__version__}-portable.zip"
FORBIDDEN = ("browser-profile", "sonuclar.jsonl", "giris-ayarlari.json", "giris-son-durum.json", "google-sheets-korumali.json", "google-yazim-kaydi.json", "google-ekip-istemcisi.json", "google-istemci.json", "google-oturum.json", "ga4-hizmet-hesabi.json", "profil.json", ".xlsx", ".env", "token", "credential", "tests/", ".venv/", "__pycache__")
TEST_MODULES=['test_jobs','test_run_and_excel','test_login','test_server','test_panel_browser','test_ui_browser',
              'test_excel_insert','test_scheduler_status','test_scheduler_cleanup','test_cell_mapping','test_google_sheets','test_catch_up','test_dateparse',
              'test_filter_source','test_segment_run','test_formula','test_ga4_merge','test_merged_ui','test_job_management','test_store','test_project_transfer','test_ui_stability']


def run(cmd, cwd, env, timeout=120):
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise SystemExit(f"BAŞARISIZ: {cmd}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--modules',nargs='+',choices=TEST_MODULES,default=TEST_MODULES,
                        help='Yalnız seçilen test grupları; varsayılan tümü.')
    args=parser.parse_args()
    with zipfile.ZipFile(ZIP) as archive:
        names = archive.namelist()
        bad = [n for n in names if any(f in n.lower() for f in FORBIDDEN) and "site-packages" not in n]
        assert not bad, bad
        with TemporaryDirectory(prefix="netmera-portable-") as tmp:
            target = Path(tmp) / "Şirket Masaüstü (3)"
            archive.extractall(target)
            app = target / "NetmeraOtomasyonu"
            py = app / "python" / "python.exe"
            env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
            env.update(PYTHONUTF8="1", NETMERA_OTOMASYON_DATA=str(Path(tmp) / "veri"),
                       ANALYTICS_CEKME_DATA=str(Path(tmp) / 'veri' / 'ga4'), NETMERA_GA4_LEGACY=str(Path(tmp) / 'eski-yok'),
                       PATH=os.environ.get("SystemRoot", r"C:\Windows") + r"\System32")  # sistem Python'u görünmesin
            out = run([str(py), "-c", "import sys; print(sys.version.split()[0], sys.prefix)"], app, env)
            assert out.startswith("3.12") and str(app / "python") in out, out
            print("✓ gömülü Python:", out.split()[0])
            print(run([str(py), "-m", "netmera_automation", "profiller"], app, env).strip())
            run([str(py), "-m", "netmera_automation", "run", "--profile", "demo-sahte-veri"], app, env)
            excel = Path(tmp) / "veri" / "profiller" / "demo-sahte-veri" / "demo-sonuclar.xlsx"
            assert excel.is_file()
            print("✓ demo çalıştı, Excel yazıldı")

            server = subprocess.Popen([str(py), "-m", "netmera_automation", "ui", "--no-browser", "--port", "0"],
                                      cwd=app, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                info_path = Path(tmp) / "veri" / "arayuz.json"
                for _ in range(50):
                    if info_path.is_file():
                        break
                    time.sleep(0.2)
                info = json.loads(info_path.read_text(encoding="utf-8"))
                base = f"http://127.0.0.1:{info['port']}"
                request = urllib.request.Request(base + "/api/state", headers={"X-Token": info["token"]})
                state = json.loads(urllib.request.urlopen(request, timeout=5).read())
                assert len(state["profiles"]) == 2
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=5)
                    raise AssertionError("anahtarsız istek kabul edildi")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 403
                print("✓ arayüz sunucusu açıldı; anahtarsız istek reddedildi")
            finally:
                server.terminate()
                server.wait(10)

            # Sahte panel testleri: uygulama kodu ZIP'ten, test dosyaları depodan.
            # Her modül yeni süreçte: Playwright/Chrome belleği sonraki gruba taşınmaz.
            modules=args.modules
            total=0
            test_env={**env,'NODE_OPTIONS':'--max-old-space-size=128'}
            for module in modules:
                code = ("import sys, unittest; sys.path.append(%r); "
                        "import netmera_automation; assert 'NetmeraOtomasyonu' in netmera_automation.__file__, netmera_automation.__file__; "
                        "r = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromName(%r)); "
                        "print('PAKET_TEST', r.testsRun, len(r.skipped)); "
                        "sys.exit(0 if r.wasSuccessful() and not r.skipped else 1)") % (str(ROOT),'tests.'+module)
                output=run([str(py),"-c",code],app,test_env,timeout=900)  # tarayıcı grupları uzun sürebilir
                result=next(line for line in output.splitlines() if line.startswith('PAKET_TEST '))
                number,skipped=map(int,result.split()[1:]);assert skipped==0
                total+=number
                print(f'✓ {module}: {number} senaryo, 0 atlanan',flush=True)
            print(f'Paket senaryolari: {total}, Atlanan: 0')
            print('✓ seçilen gruplar geçti: '+', '.join(modules))
    print(f"ZIP doğrulandı: {ZIP.name}, {len(names)} dosya")


if __name__ == "__main__":
    main()
