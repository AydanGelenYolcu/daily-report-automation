"""Yerel arayüz: yalnız 127.0.0.1'de çalışır, her açılışta yeni gizli anahtar kullanır."""
from __future__ import annotations

from collections import deque
from contextlib import nullcontext
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import traceback
import urllib.request
from urllib.parse import parse_qs, urlparse
import webbrowser

from . import __version__, jobs as J, runner, scheduler, store, login_config, google_auth, project_transfer
from .panel import ERRORS, PanelError
from .paths import PathError, check_local_path, data_root

UI_FILE = Path(__file__).with_name("ui") / "index.html"
USER_ERRORS = (runner.RunError, store.BusyError, J.JobError, PathError, ValueError)


class Worker:
    """Tarayıcı işlemleri tek arka plan iş parçacığında, sırayla yürür."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.task = None
        self.profile_id = None
        self.lines: deque = deque(maxlen=500)
        self.cancel = threading.Event()
        self.error = None
        self.result = None

    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def log(self, text: str):
        self.lines.append(f"{datetime.now():%H:%M:%S}  {text}")

    def start(self, task: str, profile_id: str, fn):
        with self.lock:
            if self.busy():
                raise store.BusyError("Şu anda başka bir işlem sürüyor; bitmesini bekleyin veya durdurun.")
            self.task, self.profile_id, self.error, self.result = task, profile_id, None, None
            self.cancel = threading.Event()
            self.lines.clear()
            self.thread = threading.Thread(target=self._wrap, args=(fn,), daemon=True)
            self.thread.start()

    def _wrap(self, fn):
        try:
            self.result = fn(self.log, self.cancel)
        except PanelError as exc:
            self.error = exc.message()
        except USER_ERRORS as exc:
            self.error = str(exc)
        except Exception as exc:
            self.error = f"Beklenmeyen hata ({type(exc).__name__}). Ayrıntı: hata-gunlugu.txt"
            try:
                with (data_root() / "hata-gunlugu.txt").open("a", encoding="utf-8") as handle:
                    handle.write(f"\n--- {datetime.now().isoformat()} {self.task}\n{traceback.format_exc()}")
            except OSError:
                pass
        if self.error:
            self.log(f"✗ {self.error}")

    def status(self) -> dict:
        # Çalışmanın tam GA4 tablolarını her 900 ms'de tekrar göndermek gezinmeyi boğuyordu.
        result = self.result if isinstance(self.result, dict) else None
        if result and self.task == 'run':
            result = {k: result[k] for k in ('run_id', 'ok', 'failed', 'seconds') if k in result}
        return {"busy": self.busy(), "task": self.task, "profile_id": self.profile_id,
                "lines": list(self.lines), "error": self.error,
                "result": result}


def constants() -> dict:
    seed = store.seed()
    return {"job_types": J.JOB_TYPES, "experimental": sorted(J.EXPERIMENTAL_TYPES), "metrics": J.METRICS,
            "metric_tr": J.METRIC_TR, "audiences": J.AUDIENCES, "columns": J.COLUMNS, "operators": J.OPERATOR_SUGGESTIONS,
            "multi_operators": sorted(J.MULTI_VALUE_OPERATORS), "no_value_operators": sorted(J.NO_VALUE_OPERATORS),
            "version_operators": J.VERSION_OPERATOR_SUGGESTIONS,
            "excel_modes":J.EXCEL_MODES,
            "formula_sources": J.FORMULA_SOURCES, "formula_modes": J.FORMULA_MODES, "date_bases": J.DATE_BASES,
            "periods": J.PERIODS, "platforms": J.PLATFORMS, "people_fields": J.PEOPLE_FIELDS,
            "people_values": {k: v["degerler"] for k, v in J.PEOPLE_FIELD_INFO.items() if v.get("degerler")},
            "people_step_choices": {k: [e["secenekler"] for e in v.get("satir_secimleri", [])
                                       if isinstance(e.get("secenekler"), list)] for k, v in J.PEOPLE_FIELD_INFO.items()},
            "people_numbered": sorted(J.PEOPLE_FIELD_INFO),
            "people_verified_fields": J.PEOPLE_VERIFIED_FIELDS, "people_count_sources": J.PEOPLE_COUNT_SOURCES, "days": J.DAYS, "sources": J.SOURCES,
            "browsers": J.BROWSERS, "on_error": J.ON_ERROR, "errors": ERRORS,
            "known_events": seed.get("bilinen_eventler", []), "known_properties": seed.get("bilinen_propertyler", []),
            "version": __version__, "data_dir": str(data_root()), "project_transfer_max_bytes": project_transfer.MAX_BYTES}


def pick_excel_file(current: str) -> str | None:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
$d = New-Object System.Windows.Forms.SaveFileDialog
$d.Title = 'Netmera sonuçlarının yazılacağı Excel dosyası'
$d.Filter = 'Excel (*.xlsx)|*.xlsx'
$d.OverwritePrompt = $false
$d.FileName = 'netmera-sonuclar.xlsx'
if ($env:NETMERA_CURRENT) { $d.InitialDirectory = Split-Path $env:NETMERA_CURRENT; $d.FileName = Split-Path $env:NETMERA_CURRENT -Leaf }
if ($d.ShowDialog($owner) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; Write-Output $d.FileName }
"""
    env = {**os.environ, "NETMERA_CURRENT": current or ""}
    result = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", env=env, timeout=600,
                            creationflags=scheduler.NO_WINDOW)
    chosen = result.stdout.strip().splitlines()
    return chosen[-1] if chosen else None


def readiness(profile: dict, connection: dict | None = None) -> list[str]:
    """Profil eksikleri + profil dosyasında görünmeyen Google bağlantısı (zamanlanmış çalışma da buna dayanır)."""
    missing = J.readiness(profile)
    if profile["source"] == "netmera" and any(j["enabled"] or j["ga4"].get("sheets") is not None
                                              for j in profile["jobs"] if j["type"] == "ga4"):
        from .ga4 import ayarlar as ga4_settings
        try:
            ga4_settings.load()
        except ga4_settings.SettingsError:
            missing.append("GA4 bağlantısı: kenar çubuğunda Google Analytics → GA4 bağlantısı sayfasından Google hesabınla bağlan ve mülk seç.")
    reads_google = any(v["kind"] == "cell" and v["source"] == "google_sheets"
                       for j in profile["jobs"] if j["enabled"] and j["type"] == "formula" for v in j["variables"])
    from .filter_source import rules as segment_rules
    reads_google = reads_google or any(a.get('value_source', {}).get('source') == 'google_sheets'
                                      for j in profile['jobs'] if j['enabled'] and j['type'] == 'segment_run'
                                      for r in segment_rules(j) for a in r.get('attributes', []))
    if reads_google and profile.get('output_target') != 'google_sheets':
        if profile['source'] != 'netmera':
            missing.append('İşler: Hesaplama veya segment filtresi Google Sheet okuyor; Demo projesi Google’a bağlanmaz.')
        elif not (connection if connection is not None else google_auth.status(profile['id']))['connected']:
            missing.append('Google bağlantısı: Hesaplama veya segment filtresi Google Sheet’ten okuyor; Google ile bağlanın.')
    if profile.get('output_target') == 'google_sheets':
        if profile['source'] != 'netmera':
            missing.append('Ayarlar: Demo projesi Google Sheets’e yazmaz; veri kaynağını Netmera seçin veya yerel Excel kullanın.')
        elif not (connection if connection is not None else google_auth.status(profile['id']))['connected']:
            missing.append('Ayarlar → Google bağlantısı: Google ile bağlanın (ekip istemcisi yoksa önce OAuth dosyasını seçin).')
    return missing


def project_summary(profile: dict, connection: dict | None = None) -> dict:
    last = store.last_run_preview(profile['id']) or {}
    return {"id": profile["id"], "name": profile["name"], "source": profile["source"],
            "app": profile["netmera_app"], "jobs": len(profile["jobs"]), "ga4": sum(j["type"] == "ga4" for j in profile["jobs"]),
            "ga4_only": J.ga4_only(profile),
            "enabled": sum(j["enabled"] for j in profile["jobs"]), "times": profile["schedule"]["times"],
            "days": profile["schedule"]["days"], "ready": not readiness(profile, connection),
            "last": {k: last.get(k) for k in ("planned_at", "ok", "failed", "write")} if last else None}


# Birleşik uygulamada GA4 projesi = Netmera projesi; Analytics'in kendi proje ve Windows görevi uçları kapalı.
GA4_BLOCKED = {"/api/projeler/olustur", "/api/projeler/sil", "/api/zamanlama/kur", "/api/zamanlama/kaldir", "/api/zamanlama/calistir"}


class App:
    def __init__(self, port: int):
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.worker = Worker()
        from .performance import RequestMetrics
        self.metrics = RequestMetrics()
        self.httpd: ThreadingHTTPServer | None = None
        from .ga4 import server as ga4_server
        self.ga4 = ga4_server.App(port)  # Analytics Veri Çekme uçları /api/ga4/… altında (aynı kod, Netmera projesiyle)

    def ga4_handle(self, method: str, route: str, body: dict, query: dict, pid: str | None):
        from .ga4.istemci import ClientError
        sub = "/api/" + route[len("/api/ga4/"):]
        if sub in GA4_BLOCKED:
            raise ValueError("Birleşik uygulamada proje ve zamanlama Projeler ve Zamanlama sayfalarından yönetilir.")
        pid = pid or (body.get("proje") if method == "POST" else (query.get("proje") or [None])[0])
        if pid and (store.profile_dir(pid) / "profil.json").is_file():
            # Kaynak projeden gelir: Demo projesi sentetik GA4, gerçek proje GA4 (iki sonuç asla karışmaz).
            kaynak = "sentetik" if store.load_profile(pid)["source"] == "demo" else "ga4"
            if method == "POST":
                body.setdefault("proje", pid)
                body["kaynak"] = kaynak
            else:
                query.setdefault("proje", [pid])
                query["kaynak"] = [kaynak]
            if sub in ("/api/disari-aktar", "/api/disari-aktar/onizleme", "/api/csv"):
                self._ga4_last_run(pid)
        if sub in ("/api/durum", "/api/google/baglan"):
            from . import google_shared
            if not google_shared.client_path().exists():
                google_shared.ensure_client(google_auth.team_client())  # GA4 JSON'u varsa ekip dosyası onun yerine geçmez
        try:
            changes_profile = method == 'POST' and (sub.startswith('/api/isler/') or sub.startswith('/api/paketler/') or sub == '/api/projeler/guncelle')
            # GA4 ve Netmera aynı profil dosyasını değiştiriyor. İki ayrı
            # read-modify-write birbirinin anahtar/ayarlarını geri çeviremesin.
            with store.profile_lock(pid) if pid and changes_profile else nullcontext():
                return self.ga4.handle(method, sub, body, query)
        except ClientError as exc:
            raise ValueError(str(exc)) from None

    # --- API ------------------------------------------------------------------
    def state(self, profile_id: str | None) -> dict:
        scanned = store._scan()
        profiles = [p for p in scanned if 'error' not in p]
        if not profiles:
            store.ensure_defaults()
            profiles = store.list_profiles()
        profile = next((p for p in profiles if p["id"] == profile_id), profiles[0])
        folder = store.profile_dir(profile["id"])
        catalog = store.read_json(folder / "katalog.json", {}) or {}
        connection = google_auth.status(profile['id'])  # ortak Google oturumu: proje başına tekrar çözülmez
        labels = {job['id']: job['label'] for job in profile['jobs']}
        return {
            "profiles": [{"id": p["id"], "name": p["name"], "source": p["source"]} for p in profiles],
            "projects": [project_summary(p, connection) for p in profiles],
            "broken_projects": [p for p in scanned if 'error' in p],
            "profile": profile, "readiness": readiness(profile, connection),
            "revision": store.profile_revision(profile),
            "descriptions": {job["id"]: J.describe(job, labels) for job in profile["jobs"]},
            "last_run": store.last_run_preview(profile['id']),
            "logged_in_once": (folder / "browser-profile").is_dir(),
            "login_config": login_config.status(profile['id']) if profile['source']=='netmera' else {'available':False},
            "google": connection,
            "catalog": catalog.get("events", []), "catalog_at": catalog.get("fetched_at"),
            "ga4": self.ga4_state(),
            "constants": constants(),
        }

    def _ga4_last_run(self, pid: str) -> None:
        """Dışa aktarma son GA4 sonucunu kullanır: Netmera çalışmasındaki GA4 tabloları GA4 sayfasındaki çalıştırmadan
        daha yeniyse onlar aktarılır (Çalıştır → 'Tabloları gör ve dışa aktar')."""
        last = store.read_json(store.profile_dir(pid) / "son-calisma.json") or {}
        tables = last.get("ga4") or []
        current = self.ga4.son_ozet or {}
        if not tables or (current.get("proje") == pid and str(current.get("zaman", "")) >= str(last.get("planned_at", ""))):
            return
        count = lambda d: sum(t.get("durum") == d for t in tables)  # noqa: E731
        self.ga4.son_ozet = {"calisma": last.get("run_id", ""), "zaman": last.get("planned_at"), "proje": pid, "sonuclar": tables,
                             "kaynak": "sentetik" if store.load_profile(pid)["source"] == "demo" else "ga4",
                             "tamam": count("tamam"), "uyari": count("uyari"), "hata": count("hata")}

    def ga4_state(self) -> dict:
        from . import ga4_migrate
        try:
            connection = self.ga4.baglanti_ozeti()
        except Exception:  # bozuk ayar dosyası arayüzü açmayı engellemesin
            connection = None
        return {"connection": connection, "import": ga4_migrate.done()}

    def handle(self, method: str, route: str, body: dict, query: dict):
        pid = body.get("profile_id") or (query.get("profile") or [None])[0]
        if route == "/api/ping":
            return {"ok": True, "version": __version__}
        if route.startswith("/api/ga4/"):
            return self.ga4_handle(method, route, body, query, pid)
        if route == "/api/state":
            return self.state(pid)
        if route == "/api/status":
            return self.worker.status()
        if route == '/api/last-run':
            store.load_profile(pid)
            offset = int((query.get('offset') or ['0'])[0])
            if offset < 0:
                raise ValueError('Sonuç sayfası geçersiz.')
            run = store.read_json(store.profile_dir(pid) / 'son-calisma.json')
            return {'last_run': store.run_preview(run, offset=offset) if run else None}
        if route == '/api/ga4-last-run':
            store.load_profile(pid)
            self._ga4_last_run(pid)
            result = self.ga4.son_ozet
            return {'result': result if result and result.get('proje') == pid else None}
        if route == '/api/google/status':
            profile = store.load_profile(pid)
            return {'google': google_auth.status(pid), 'readiness': readiness(profile)}
        if route == '/api/login-config/status':
            profile=store.load_profile(pid)
            return {'login_config':login_config.status(pid) if profile['source']=='netmera' else {'available':False}}
        if route == "/api/schedule/status":
            profile = store.load_profile(pid)
            return {**scheduler.status(profile), 'last_scheduled':store.read_json(store.profile_dir(pid) / 'zamanli-son-durum.json')}
        if route == "/api/diagnostics":
            folder = store.profile_dir(store.load_profile(pid)["id"]) / "teshis"
            files = sorted(folder.glob("*.json"), reverse=True)[:8] if folder.is_dir() else []
            return {"items": [{"name": f.name, "data": store.read_json(f)} for f in files],
                    "performance": self.metrics.snapshot()}
        if route == '/api/performance':
            return {'requests': self.metrics.snapshot()}
        if route == '/api/google/find-client':
            profile=store.load_profile(pid)
            return {'found':google_auth.find_download() if profile['source']=='netmera' else None}
        if method != "POST":
            raise ValueError("Bilinmeyen istek.")
        if route == "/api/projects/export":
            return project_transfer.export_projects(body.get("profile_ids"))
        if route == "/api/projects/import-preview":
            return project_transfer.preview_import(body.get("bundle"))
        if route == "/api/projects/import":
            result = project_transfer.import_projects(body.get("bundle"), body.get("indices"), keep_enabled=body.get("keep_enabled", False))
            if result.get('connections_added'):
                self.ga4._ga4 = None
            return result
        if route in ('/api/login-config/save','/api/login-config/clear'):
            profile=store.load_profile(pid)
            if profile['source']!='netmera':
                raise ValueError('Demo projesi giriş bilgisi kullanmaz.')
            if route.endswith('/clear'):
                result=login_config.clear(pid)
            else:
                result=login_config.save(pid,enabled=body.get('enabled'),keep_logged_in=body.get('keep_logged_in'),
                                         username=body.get('username',''),password=body.get('password',''))
            return {'login_config':result}
        if route == "/api/profile/save":
            profile_id = body['profile']['id']
            with store.profile_lock(profile_id):
                current = store.load_profile(profile_id)
                if body.get('revision') and body['revision'] != store.profile_revision(current):
                    raise ValueError('Ayarlar başka bir işlemde değişti. Sayfayı yenileyip tekrar kaydedin; eski ayarlar yazılmadı.')
                profile = store.save_profile(body["profile"])
            return {"profile": profile, "revision": store.profile_revision(profile), "readiness": readiness(profile)}
        if route == '/api/jobs/enabled':
            from . import job_management
            if type(body.get('enabled')) is not bool:
                raise ValueError('İş durumu açık veya kapalı olmalı.')
            if self.worker.busy() and self.worker.profile_id == pid:
                raise ValueError('Bu projede işlem sürüyor; bitince iş durumunu değiştirin.')
            job_management.bulk(pid, [body.get('job_id')], 'enable' if body['enabled'] else 'disable')
            profile = store.load_profile(pid)
            return {'revision': store.profile_revision(profile), 'job_id': body['job_id'], 'enabled': body['enabled']}
        if route in ('/api/jobs/transfer', '/api/jobs/delete-all', '/api/jobs/bulk'):
            from . import job_management
            target = body.get('target_profile_id')
            if self.worker.busy() and self.worker.profile_id in (pid, target):
                raise ValueError('Kaynak veya hedef projede işlem sürüyor. Bitince tekrar deneyin.')
            if route.endswith('/bulk'):
                if body.get('action') == 'delete' and body.get('confirm') is not True:
                    raise ValueError('Seçilen işleri silmek için ekrandaki onay gerekli.')
                return job_management.bulk(pid, body.get('job_ids'), body.get('action'))
            if route.endswith('delete-all'):
                if body.get('confirm') is not True:
                    raise ValueError('Tüm işleri silmek için ekrandaki onay gerekli.')
                return job_management.delete_all(pid, body.get('expected_job_ids'))
            return job_management.transfer(pid, target, body.get('job_ids'), body.get('mode'))
        if route == "/api/job/check":
            job = J.normalize_job(body["job"])
            labels = {j["id"]: j["label"] for j in store.load_profile(pid)["jobs"]} if pid else None
            return {"job": job, "description": J.describe(job, labels)}
        if route == "/api/profile/create":
            profile = store.create_profile(J._text(body.get("name"), "Profil adı", max_len=60),
                                           source=body.get("source", "netmera"), copy_from=body.get("copy_from"))
            return {"profile_id": profile["id"]}
        if route == "/api/profile/delete":
            if self.worker.busy() and self.worker.profile_id == pid:
                raise ValueError("Bu profilde işlem sürerken silinemez.")
            try:
                profile = store.load_profile(pid)
            except J.JobError:
                # Açılamayan (bozuk) proje de silinebilmeli; görev adı için ham addan yararlanılır.
                broken = next((b for b in store.broken_profiles() if b["id"] == pid), None)
                if broken is None:
                    raise
                profile = {"id": pid, "name": broken["name"], "schedule": {}}
            scheduler.remove(profile)
            store.delete_profile(pid)
            return {"ok": True}
        if route == "/api/run":
            profile = store.load_profile(pid)
            job_ids = body.get("job_ids") or None
            dates = J.parse_day_range(body["date_start"], body.get("date_end"), "Çalıştırma tarihi") if body.get("date_start") else None
            write, watch = body.get("write", True) is True, body.get("watch", False) is True
            split = body.get("split_days", False) is True
            self.worker.start("run", pid, lambda log, cancel: runner.run_profile(
                profile, job_ids=job_ids, write=write, watch=watch, dates=dates, split_days=split, log=log, cancel=cancel))
            return {"started": True}
        if route == "/api/login":
            profile = store.load_profile(pid)
            self.worker.start("login", pid, lambda log, cancel: {"logged_in": runner.login(profile, log=log, cancel=cancel)})
            return {"started": True}
        if route == "/api/catalog":
            profile = store.load_profile(pid)
            self.worker.start("catalog", pid, lambda log, cancel: {"events": runner.fetch_catalog(profile, log=log)})
            return {"started": True}
        if route == "/api/segment-rules":
            profile = store.load_profile(pid)
            name = J._text(body.get("name"), "Segment adı", max_len=100)
            self.worker.start("segment_rules", pid, lambda log, cancel: {"segment_rules": runner.fetch_segment_rules(profile, name, log=log)})
            return {"started": True}
        if route == "/api/cancel":
            self.worker.cancel.set()
            return {"ok": True}
        if route == "/api/excel/rebuild":
            profile = store.load_profile(pid)
            notes=[]
            with store.profile_lock(profile['id']):written=runner.rebuild_excel(profile,notes)
            message = 'Çıktıya yeni sonuçlar yazıldı.' if written else 'Yeni çıktı yazılmadı. Bekleyen sonuç yok veya hedef hücreler atlandı.'
            last=store.read_json(store.profile_dir(pid)/'son-calisma.json') or {}
            if not written and last.get('write') is False:
                message += ' Son TEST sonuçları yazım kuyruğuna alınmaz; Çalıştır ve yaz ile yeni çalışma başlatın.'
            return {'ok':True,'written':written>0,'written_batches':written,'message':message,'notes':notes}
        if route == '/api/excel/preview':
            from .cell_mapping import preview
            profile=J.normalize_profile(body['profile']) if 'profile' in body else store.load_profile(pid)
            last=store.read_json(store.profile_dir(profile['id'])/'son-calisma.json') or {}
            if profile.get('output_target')=='google_sheets':
                from .google_sheets import preview as google_preview
                return google_preview(profile,last.get('rows',[]),day_rows=last.get('day_rows',[]))
            return preview(profile,last.get('rows',[]),day_rows=last.get('day_rows',[]))
        if route == '/api/notify/test':
            from .notify import show
            return {'shown': show('Netmera Otomasyonu — deneme', 'Bu bildirimi görüyorsan zamanlanmış çalışma sonuçları da böyle gelir.')}
        if route == '/api/google/check':
            from .google_sheets import check
            profile=J.normalize_profile(body['profile']) if 'profile' in body else store.load_profile(pid)
            return check(profile)
        if route == '/api/google/team/save':
            profile=store.load_profile(pid)
            if profile['source']!='netmera':raise ValueError('Google bağlantısı Demo projesinde kullanılmaz.')
            return {'google':google_auth.save_team(pid),'path':str(google_auth.TEAM_FILE)}
        if route in ('/api/google/client','/api/google/clear','/api/google/connect'):
            self.ga4._degisti()  # tek Google ayarı: GA4 istemcisi yeni JSON/girişle yeniden kurulsun
            profile=store.load_profile(pid)
            if profile['source']!='netmera':raise ValueError('Google bağlantısı Demo projesinde kullanılmaz.')
            if self.worker.busy():raise ValueError('Önce süren işlemi tamamlayın veya durdurun.')
            if route.endswith('/connect'):
                self.worker.start('google',pid,lambda log,cancel:google_auth.connect(pid,log,cancel))
                return {'started':True}
            with store.profile_lock(pid):
                if route.endswith('/clear'):info=google_auth.clear(pid)
                elif body.get('download'):info=google_auth.save_client_from_download(pid,body['download'])
                else:info=google_auth.save_client(pid,body.get('client'))
            return {'google':info}
        if route == '/api/schedule/run':
            profile = store.load_profile(pid)
            missing = readiness(profile)
            if missing:
                raise ValueError('Çalıştırmadan önce: ' + ' '.join(missing))
            scheduler.run_now(profile)
            return {'started':True}
        if route == "/api/open":
            profile = store.load_profile(pid)
            what = body.get("what")
            if what == "excel":
                if profile.get('output_target')=='google_sheets':
                    if not profile.get('sheets_id'):raise ValueError('Önce Google Sheets dosya bağlantısını kaydedin.')
                    webbrowser.open('https://docs.google.com/spreadsheets/d/'+profile['sheets_id']+'/edit')
                    return {'ok':True}
                target = check_local_path(Path(profile["output_file"]))
                if not target.is_file():
                    raise ValueError("Excel dosyası henüz oluşmadı; önce bir çalışma yapın.")
            elif what == "folder":
                target = check_local_path(Path(profile["output_file"])).parent
            elif what == 'logs':
                target = store.profile_dir(pid) / 'gunluk'
                target.mkdir(parents=True,exist_ok=True)
            elif what == 'login_config':
                if profile['source']!='netmera':
                    raise ValueError('Demo projesi giriş config dosyası kullanmaz.')
                login_config.status(pid)
                target=login_config.config_path(pid)
            else:
                target = store.profile_dir(pid)
            os.startfile(target)  # noqa: S606 — yalnız kendi doğrulanmış yollarımız
            return {"ok": True}
        if route == "/api/pick-file":
            chosen = pick_excel_file(body.get("current", ""))
            if chosen:
                check_local_path(Path(chosen))
            return {"path": chosen}
        if route in ("/api/schedule/install",'/api/schedule/reset'):
            profile = store.load_profile(pid)
            missing = readiness(profile)
            if missing:
                raise ValueError("Zamanlamadan önce: " + " ".join(missing))
            if route == '/api/schedule/reset':
                return scheduler.reset(profile)
            return {"task": scheduler.install(profile)}
        if route == "/api/schedule/remove":
            return {"removed": scheduler.remove(store.load_profile(pid))}
        if route == "/api/shutdown":
            self.worker.cancel.set()
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
            return {"ok": True}
        raise ValueError("Bilinmeyen istek.")

    # --- HTTP -----------------------------------------------------------------
    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, payload, content_type="application/json; charset=utf-8"):
                data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.response_code, self.response_size = code, len(data)
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Sekme kapanmışsa aynı bağlantıya ikinci bir hata yanıtı yazma.

            def _allowed_host(self) -> bool:
                return self.headers.get("Host") in {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}

            def _dispatch(self, method: str):
                if not self._allowed_host():
                    return self._send(403, {"error": "Yalnız yerel erişim."})
                url = urlparse(self.path)
                if method == "GET" and url.path in ("/", "/index.html"):
                    if parse_qs(url.query).get("t", [""])[0] != app.token:
                        return self._send(403, "Bu adresi NetmeraOtomasyonu.cmd ile açın.".encode(), "text/plain; charset=utf-8")
                    html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", app.token).replace("__VERSION__", __version__)
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                if not url.path.startswith("/api/"):
                    return self._send(404, {"error": "Bulunamadı"})
                if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                    return self._send(403, {"error": "Oturum anahtarı geçersiz; sayfayı uygulamadan yeniden açın."})
                body = {}
                if method == "POST":
                    if not self.headers.get("Content-Type", "").startswith("application/json"):
                        return self._send(415, {"error": "JSON bekleniyor."})
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        return self._send(400, {"error": "Geçersiz istek uzunluğu."})
                    if length < 0 or self.headers.get("Transfer-Encoding"):
                        return self._send(400, {"error": "Geçersiz istek uzunluğu veya aktarım biçimi."})
                    limit = project_transfer.MAX_BYTES + 64_000 if url.path in ("/api/projects/import", "/api/projects/import-preview") else 1_000_000
                    if length > limit:
                        return self._send(413, {"error": "İstek çok büyük."})
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, RecursionError):
                        return self._send(400, {"error": "Geçersiz JSON."})
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "JSON gövdesi bir nesne olmalı."})
                try:
                    return self._send(200, app.handle(method, url.path, body, parse_qs(url.query)))
                except PanelError as exc:
                    return self._send(400, {"error": exc.message()})
                except (*USER_ERRORS, KeyError, RuntimeError, OSError) as exc:
                    return self._send(400, {"error": str(exc) or type(exc).__name__})

            def do_GET(self):
                self._timed_dispatch('GET')

            def do_POST(self):
                self._timed_dispatch('POST')

            def _timed_dispatch(self, method):
                started = time.monotonic()
                self.response_code, self.response_size = 500, 0
                try:
                    self._dispatch(method)
                finally:
                    # Yetkisiz istekler ve URL sorgusu (oturum anahtarı) ölçümlere girmez.
                    if secrets.compare_digest(self.headers.get('X-Token', ''), app.token):
                        route = urlparse(self.path).path
                        app.metrics.record(method, route, time.monotonic() - started, self.response_code, self.response_size)

        return Handler


def _free_port(preferred: int = 8787) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("Boş port bulunamadı.")


def _local_api(info: dict, route: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{info['port']}{route}", data=data,
                                     headers={"X-Token": info["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read() or b"{}")


def _existing_instance() -> str | None:
    """Açık arayüzün adresi. Açık olan başka bir sürümse (ör. yeni ZIP/kod açıldı) boştaysa kapatılır,
    böylece yeni sürüm başlar; eski sunucu yeni sayfanın isteklerini tanımaz ('Bilinmeyen istek')."""
    info = store.read_json(data_root() / "arayuz.json")
    if not isinstance(info, dict):
        return None
    try:
        version = _local_api(info, "/api/ping").get("version")
        url = f"http://127.0.0.1:{info['port']}/?t={info['token']}"
    except (OSError, KeyError, ValueError):
        return None
    if version == __version__:
        return url
    try:
        if _local_api(info, "/api/status").get("busy"):
            print(f"Açık arayüz eski sürüm ({version or 'bilinmiyor'}) ve şu anda bir işlem yürütüyor. "
                  "İşlem bitince o siyah pencereyi kapatıp bu dosyayı yeniden çalıştırın.")
            return url
        _local_api(info, "/api/shutdown", {})
    except (OSError, KeyError, ValueError):
        pass
    print(f"Eski sürüm arayüzü ({version or 'bilinmiyor'}) kapatıldı; {__version__} başlatılıyor.")
    for _ in range(20):  # eski sunucu kapanana dek bekle
        try:
            _local_api(info, "/api/ping")
        except (OSError, ValueError):
            break
        time.sleep(0.25)
    return None


def serve(open_browser: bool = True, port: int | None = None) -> int:
    store.ensure_defaults()
    try:
        from . import ga4_migrate
        ga4_migrate.import_legacy()  # bir kez: ayrı Analytics Veri Çekme uygulamasının projeleri ve bağlantısı
    except Exception:
        with (data_root() / "hata-gunlugu.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- {datetime.now().isoformat()} ga4 içe aktarma\n{traceback.format_exc()}")
    url = _existing_instance()
    if url:
        print("Arayüz zaten açık; tarayıcıda gösteriliyor.")
        if open_browser:
            webbrowser.open(url)
        return 0
    app = App(port or _free_port())
    app.httpd = ThreadingHTTPServer(("127.0.0.1", app.port), app.make_handler())
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    store.write_json(data_root() / "arayuz.json", {"port": app.port, "token": app.token, "pid": os.getpid()})
    print(f"Netmera Otomasyonu {__version__} arayüzü açık: {url}")
    print("Bu pencereyi kapatırsanız arayüz kapanır (zamanlanmış görevler etkilenmez).")
    if open_browser:
        webbrowser.open(url)
    try:
        app.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.httpd.server_close()
        (data_root() / "arayuz.json").unlink(missing_ok=True)
    return 0
