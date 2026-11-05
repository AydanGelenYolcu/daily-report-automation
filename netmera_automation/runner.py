"""İşleri çalıştırır: arayüz ve Görev Zamanlayıcı aynı kodu kullanır."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from . import store
from .excel import ExcelLockedError, write_incremental_workbook, write_workbook
from .jobs import (DATED_TYPES, JobError, column_label, formula_job_ids, describe, ga4_sheets_jobs, is_yesterday, job_days,
                   normalize_job, output_set, period_bounds, readiness, signature)
from .panel import DAY_CHUNK, ERRORS, FATAL, NO_RETRY, PanelError, PanelReader, PANEL_URL
from .paths import TIMEZONE, check_local_path
from .synthetic import SyntheticReader
from .login_session import AutoLogin, is_login_page


# Bir işin en çok kaç kez okunacağı (ilk deneme dahil). Giriş yenileme bu sayıyı tüketmez.
MAX_ATTEMPTS = 3


class RunError(RuntimeError):
    """Kullanıcıya olduğu gibi gösterilebilen Türkçe hata."""


def now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE)).replace(microsecond=0)


def _browser_error(exc: Exception) -> PanelError:
    text = str(exc)
    if "is not found" in text or "Executable doesn't exist" in text or "not found at" in text:
        return PanelError("BROWSER_NOT_FOUND")
    if "ProcessSingleton" in text or "already in use" in text or "user data directory" in text.lower():
        return PanelError("PROFILE_IN_USE")
    return PanelError("BROWSER_START_FAILED")


class Browser:
    """Gerçek Chrome/Edge'i, profile özel kalıcı oturum klasörüyle açar.

    Kullanıcının günlük Chrome profili kullanılmaz: Chrome 136+ varsayılan profilde
    otomasyon bağlantısını kapatır; ayrıca açık Chrome profili kilitler.
    """

    def __init__(self, profile: dict, *, visible: bool, slow: bool = False):
        self.profile, self.visible, self.slow = profile, visible, slow

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        folder = check_local_path(store.profile_dir(self.profile["id"]) / "browser-profile")
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                str(folder), channel=self.profile["browser"], headless=not self.visible,
                accept_downloads=False, no_viewport=True, slow_mo=max(self.profile.get("step_delay_ms", 300), 600 if self.slow else 0),
                args=["--start-maximized"],
            )
        except Exception as exc:
            self._pw.stop()
            raise _browser_error(exc) from None
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15000)
        return self

    def __exit__(self, *exc):
        try:
            self.context.close()
        finally:
            self._pw.stop()


def _row(profile, job, column, value, status, first, last, planned, note=None) -> dict:
    start = end = None
    if job["type"] in DATED_TYPES:
        start, end = (d.isoformat() for d in period_bounds(first, last))
    return {
        "source": profile["source"], "job_id": job["id"], "job_label": job["label"], "job_type": job["type"],
        "column": column, "column_label": column_label(job, column),
        "metric": job.get("metric") or ("ga4" if job["type"] == "ga4" else "users"), "description": describe(job), "signature": signature(job),
        "audience": job.get("audience"), "audience_name": job.get("audience_name"),
        "value": value if status == "OK" else None, "status": status,
        "period_start": start, "period_end": end,
        "snapshot_slot": None if job["type"] in DATED_TYPES else planned.isoformat(),
        "planned_at": planned.isoformat(), "captured_at": now().isoformat(), "note": note,
    }


def rebuild_excel(profile: dict, notes: list | None = None) -> int:
    if profile.get('output_target')=='google_sheets':
        from .google_sheets import write
        return write(profile,store.excel_batches(profile),notes)
    if profile.get('excel_mode')=='mapped_cells':
        from .cell_mapping import write_mapped_workbook
        return write_mapped_workbook(profile,store.excel_batches(profile),notes)
    if profile.get('excel_mode','rebuild') != 'rebuild':
        return write_incremental_workbook(check_local_path(Path(profile['output_file'])), store.excel_batches(profile),
                                          profile['source'], profile['excel_mode'], profile.get('excel_sheet','Netmera'))
    rows = list(store.load_results(profile["id"]).values())
    rows = [r for r in rows if r["source"] == profile["source"]]
    if not rows:
        return 0
    order = [column_label(job, c) for job in profile["jobs"] for c in (job.get("columns") or [None])]
    write_workbook(check_local_path(Path(profile["output_file"])), rows, profile["source"], order)
    return 1


def _single_day(row: dict) -> date | None:
    try:
        first, end = datetime.fromisoformat(row['period_start']), datetime.fromisoformat(row['period_end'])
    except (KeyError, TypeError, ValueError):
        return None
    return first.date() if end - first == timedelta(days=1) else None


def catch_up_plan(profile: dict, jobs: list[dict], planned: datetime) -> dict[date, list[dict]]:
    """'Dün' dönemli Event işleri için uygulamanın son başarılı gününden sonra çekilmemiş günler.

    Dün normal çalışmada çekilir. Hiç başarılı sonucu olmayan iş için geçmişe gidilmez.
    En çok profile['catch_up_days'] gün geriye bakılır; yalnız hücre eşlemeli çıktıda kullanılır.
    """
    limit = profile.get('catch_up_days', 0)
    if not limit or profile.get('excel_mode') != 'mapped_cells':
        return {}
    yesterday = planned.date() - timedelta(days=1)
    last: dict[str, date] = {}
    for row in store.load_results(profile['id']).values():
        day = _single_day(row) if row.get('job_type') in DATED_TYPES and row.get('status') == 'OK' else None
        if day and row.get('source') == profile['source']:
            last[row['job_id']] = max(last.get(row['job_id'], day), day)
    plan: dict[date, list[dict]] = defaultdict(list)
    for job in jobs:
        if job['type'] not in DATED_TYPES or not is_yesterday(job) or job['id'] not in last:
            continue
        day = max(last[job['id']] + timedelta(days=1), yesterday - timedelta(days=limit))
        while day < yesterday:
            plan[day].append(job)
            day += timedelta(days=1)
    return dict(sorted(plan.items()))


def _run_catch_up(reader, profile, plan, planned, log, cancel, auto_login=None) -> dict[date, list[dict]]:
    """Kaçan/seçilen günler: aynı sorgunun işleri ardışık en çok DAY_CHUNK günlük parçalarda tek Find ile
    okunur (Dimensions → Days of a Month). Satırlar güne göre ayrılır."""
    by_job: dict[str, list[date]] = defaultdict(list)
    jobs = {}
    for day, day_jobs in plan.items():
        for job in day_jobs:
            by_job[job["id"]].append(day)
            jobs[job["id"]] = job
    units: dict[tuple, list[dict]] = {}
    for job_id, days in by_job.items():
        days = sorted(days)
        chunk = [days[0]]
        for day in days[1:] + [None]:
            # Segment her gün için ayrı kaydedilip hesaplanır: her gün ayrı birim (ayrı deneme hakkı).
            size = DAY_CHUNK if jobs[job_id]["type"] == "event" else 1
            if day is not None and (day - chunk[-1]).days == 1 and len(chunk) < size:
                chunk.append(day)
                continue
            units.setdefault((query_key(jobs[job_id]), chunk[0], chunk[-1]), []).append(jobs[job_id])
            if day is not None:
                chunk = [day]
    rows: list[dict] = []
    state = {"fatal": None}
    for (_, first, last), members in sorted(units.items(), key=lambda item: (item[0][1], item[0][0])):
        if cancel.is_set():
            break
        log(f"↺ Kaçan/seçilen günler {first:%d.%m}–{last:%d.%m} · {len(members)} tarihli iş")
        _run_unit(reader, profile, members, first, last, planned, rows, log, cancel, state, auto_login, per_day=True)
    batches: dict[date, list[dict]] = defaultdict(list)
    for row in rows:
        row['catch_up'] = True  # yalnız boş hücreye yazılır; sabit hücreye yazılmaz
        batches[_single_day(row)].append(row)
    return dict(sorted(batches.items()))


def run_profile(profile: dict, *, job_ids: list[str] | None = None, write: bool = True, watch: bool = False,
                dates: tuple | None = None, split_days: bool = False,
                log=print, cancel: threading.Event | None = None) -> dict:
    cancel = cancel or threading.Event()
    jobs = [j for j in profile["jobs"] if (j["id"] in job_ids if job_ids else j["enabled"])]
    # Hesaplamanın kullandığı işler (kapalı olsalar da, ör. 'Dene' ile tek hesaplama) birlikte okunur.
    needed = set(formula_job_ids(profile["jobs"], jobs)) - {j["id"] for j in jobs}
    if needed:
        jobs = [j for j in profile["jobs"] if j in jobs or j["id"] in needed]
    if split_days:
        # Geçmiş günleri doldurma: her gün ayrı değer, kendi sütunu, yalnız boş hücre. Test'te yazılmaz, yalnız gösterilir.
        if not dates:
            raise RunError("Günlere bölmek için bir tarih aralığı seçin.")
        if write and profile.get('excel_mode') != 'mapped_cells':
            raise RunError("Günlere bölerek yazma yalnız “Belirli hücrelere yaz” modunda kullanılabilir; Test et ile sonuçları görebilirsiniz.")
        if (dates[1] - dates[0]).days > 30:
            raise RunError("Günlere bölme en çok 31 gün için kullanılabilir.")
    if not jobs:
        if write and not job_ids and ga4_sheets_jobs(profile):
            return _ga4_sheets_only(profile, log)  # etkin iş yok ama Sheets'e yazılan GA4 işleri var (Analytics kuralı)
        raise RunError("Çalıştırılacak iş yok.")
    try:
        jobs = [normalize_job(job) for job in jobs]
    except JobError as exc:
        raise RunError(str(exc)) from None
    missing = readiness({**profile, "jobs": jobs})
    if job_ids:  # tek iş denemesi: etkin olması gerekmez
        missing = [m for m in missing if not m.startswith("En az bir işi")]
    if not write:  # test: Excel yolu gerekmez
        missing = [m for m in missing if "Excel" not in m and "OneDrive" not in m and 'Google Sheets' not in m]
    if missing:
        raise RunError(" ".join(missing))
    formulas = [j for j in jobs if j["type"] == "formula"]
    jobs = [j for j in jobs if j["type"] != "formula"]
    if write and profile.get('output_target')=='google_sheets':
        from .google_auth import status
        if profile['source']=='demo':raise RunError('Demo sonuçları Google Sheets’e gönderilmez; yerel Excel kullanın.')
        if not status(profile['id'])['connected']:raise RunError('Ayarlar bölümünden Google hesabını bağlayın; henüz Netmera sorgusu çalıştırılmadı.')
    planned = now()
    rows: list[dict] = []
    started = time.monotonic()
    destination='Google Sheets' if profile.get('output_target')=='google_sheets' else 'Excel'
    log(f"Çalışma başladı: {profile['name']} · {len(jobs) + len(formulas)} iş · {destination+' yazılacak' if write else 'TEST (çıktıya yazılmaz)'}")
    dated = [j for j in jobs if j["type"] in DATED_TYPES]
    snapshot_jobs = [j for j in jobs if j["type"] not in DATED_TYPES]
    if dates:
        log(f"Event/Segment tarih işleri için seçilen tarih: {dates[0]:%d.%m.%Y}–{dates[1]:%d.%m.%Y}")
        if snapshot_jobs:
            # Geçmişe dönük alınamayan sayılar: tarih ne seçilirse seçilsin çekim anındaki güncel değer alınır.
            log("ⓘ People/Tag/Segment sayısı geçmiş güne göre alınamaz; bu işler çekim anındaki güncel sayıyı okur (seçilen tarih yok sayılır): "
                + ", ".join(j['label'] for j in snapshot_jobs))
    catch = catch_up_plan(profile, jobs, planned) if write and not dates and not job_ids else {}
    main_jobs = jobs
    if split_days:
        catch = {dates[0] + timedelta(days=n): dated for n in range((dates[1] - dates[0]).days + 1)} if dated else {}
        main_jobs = snapshot_jobs
        log(f"Günlere bölünmüş doldurma: {len(catch)} gün × {len(dated)} tarihli iş (yalnız boş hücrelere yazılır).")
    # 'Her günü ayrı hesapla' seçili segment işleri: aralığın her günü ayrı birim olur (seçilen tarih varsa o aralık).
    per_day_jobs = [] if split_days else [j for j in main_jobs if j["type"] == "segment_run" and j.get("split_days")]
    if per_day_jobs and write and profile.get('excel_mode') != 'mapped_cells':
        raise RunError("“Her günü ayrı hesapla” seçili segment işini yazmak için “Belirli hücrelere yaz” modu gerekir; Test et ile sonuçları görebilirsiniz.")
    catch_batches: dict[date, list[dict]] = {}
    if catch and not split_days:
        log("Kaçan günler: " + ", ".join(f"{d:%d.%m}" for d in catch) + " — Event işleri bu günler için de çekilecek (yalnız boş hücrelere yazılır).")
        snapshots = [j['label'] for j in jobs if j['type'] not in DATED_TYPES]
        if snapshots:
            log("ⓘ People/Tag/Segment sayıları geçmiş günler için alınamaz; o günlerin hücreleri boş kalır: " + ", ".join(snapshots))
    for job in per_day_jobs:
        first, last = dates or job_days(job, planned)
        if (last - first).days > 30:
            raise RunError(f"'{job['label']}': her günü ayrı hesaplama en çok 31 gün için kullanılabilir.")
        for n in range((last - first).days + 1):
            catch.setdefault(first + timedelta(days=n), [])
            if job not in catch[first + timedelta(days=n)]:
                catch[first + timedelta(days=n)].append(job)
        log(f"'{job['label']}': {first:%d.%m}–{last:%d.%m} her gün ayrı hesaplanacak ({(last - first).days + 1} gün).")
    if per_day_jobs:
        catch = dict(sorted(catch.items()))
        main_jobs = [j for j in main_jobs if j not in per_day_jobs]
    # GA4 işleri tarayıcı gerektirmez: ayrı okuyucuyla okunur. Netmera işleri için tarayıcı yalnız gerekiyorsa açılır.
    ga4_main = [j for j in main_jobs if j["type"] == "ga4"]
    ga4_catch = {d: [j for j in js if j["type"] == "ga4"] for d, js in catch.items()}
    ga4_catch = {d: js for d, js in ga4_catch.items() if js}
    main_jobs = [j for j in main_jobs if j["type"] != "ga4"]
    catch = {d: [j for j in js if j["type"] != "ga4"] for d, js in catch.items()}
    catch = {d: js for d, js in catch.items() if js}
    ga4_reader = None
    with store.profile_lock(profile["id"]):
        if ga4_main or ga4_catch:
            from .ga4_reader import Ga4Reader
            ga4_reader = Ga4Reader(profile, log)
            if ga4_main:
                _run_jobs(ga4_reader, profile, ga4_main, planned, rows, log, cancel, dates)
            if ga4_catch:
                catch_batches = _run_catch_up(ga4_reader, profile, ga4_catch, planned, log, cancel)
        if not main_jobs and not catch:
            pass  # yalnız GA4 / tablo hücresi okuyan işler: Netmera/tarayıcı açılmaz
        elif profile["source"] == "demo":
            reader = SyntheticReader(log)
            if main_jobs:
                _run_jobs(reader, profile, main_jobs, planned, rows, log, cancel, dates)
            if catch:
                _merge_batches(catch_batches, _run_catch_up(reader, profile, catch, planned, log, cancel))
        else:
            with Browser(profile, visible=watch or not profile["headless"], slow=watch) as browser:
                reader = PanelReader(browser.page, profile["netmera_app"], profile["timeout_seconds"], log)
                reader.profile_id = profile['id']
                reader.cancel = cancel
                auto_login=AutoLogin(profile,reader,log,cancel)
                try:
                    _open_with_login(reader,auto_login)
                except PanelError as exc:
                    raise RunError(exc.message()) from None
                if main_jobs:
                    _run_jobs(reader, profile, main_jobs, planned, rows, log, cancel, dates,auto_login)
                if catch and any(r["status"] in FATAL for r in rows):
                    log("Kaçan günler atlandı: ana çalışmada panel/oturum hatası var.")
                elif catch:
                    _merge_batches(catch_batches, _run_catch_up(reader, profile, catch, planned, log, cancel, auto_login))
                if watch and any(r["status"] != "OK" for r in rows):
                    log("İzleme modu: hata görüldü, tarayıcı 20 sn açık kalıyor.")
                    browser.page.wait_for_timeout(20000)

        if formulas:
            _add_formula_rows(profile, formulas, rows, catch_batches, planned, log, dates=dates, split_days=split_days)
        if dates:
            for row in rows:
                if row["job_type"] not in DATED_TYPES and row["job_type"] != "formula":
                    row["note"] = "; ".join(filter(None, ["çekim anı (seçilen tarih kullanılmadı)", row.get("note")]))
        failed = [r for r in rows if r["status"] != "OK"]
        summary = {"profile_id": profile["id"], "run_id":uuid.uuid4().hex, "planned_at": planned.isoformat(), "write": write,
                   "seconds": round(time.monotonic() - started), "rows": rows,
                   "ok": len(rows) - len(failed), "failed": len(failed), "excel": None,
                   "excel_written": None if not write else False}
        catch_batches = dict(sorted(catch_batches.items()))
        catch_rows = [r for day_rows in catch_batches.values() for r in day_rows]
        summary['ok'] += sum(r['status'] == 'OK' for r in catch_rows)
        summary['failed'] += sum(r['status'] != 'OK' for r in catch_rows)
        # Günlük (kaçan/seçilen) sonuçlar ayrıca gösterilir; hücre önizlemesi bunları da eşler.
        summary["day_rows"] = catch_rows
        # GA4 tablolarının tamamı (kırılım satırları, toplam, uyarılar): sonuç ekranı ve dışa aktarma için
        summary["ga4"] = ga4_reader.tables if ga4_reader else []
        summary["split"] = split_days
        if catch_batches:
            summary["catch_up"] = {"days": [d.isoformat() for d in catch_batches],
                                   "ok": sum(r["status"] == "OK" for r in catch_rows),
                                   "failed": sum(r["status"] != "OK" for r in catch_rows)}
        if write:
            if summary['failed'] and profile["on_error"] == "write_nothing":
                summary["excel"] = "Hata olduğu için (ayar: hiçbir şey yazma) sonuç kaydedilmedi."
            else:
                store.append_results(profile["id"], rows + catch_rows)
                if profile.get('excel_mode','rebuild') != 'rebuild':
                    order = [column_label(job,c) for job in profile['jobs'] for c in (job.get('columns') or [None])]
                    for day, day_rows in catch_batches.items():
                        if any(r['status'] == 'OK' for r in day_rows):
                            store.append_excel_batch(profile, {'id':f"{summary['run_id']}-k{day:%Y%m%d}", 'source':profile['source'],
                                                               'planned_at':planned.isoformat(), 'rows':day_rows,
                                                               'column_order':order, 'catch_up':True})
                    if rows:
                        store.append_excel_batch(profile, {'id':summary['run_id'], 'source':profile['source'],
                                                           'planned_at':planned.isoformat(), 'rows':rows,'column_order':order})
                notes: list[str] = []
                try:
                    if not output_set(profile):  # yalnız GA4 proje: hücre hedefi seçilmemiş, sonuçlar kayıtta ve GA4 sekmelerinde
                        raise _NoOutput()
                    written = rebuild_excel(profile, notes)
                    summary['excel_written'] = written > 0
                    summary['excel'] = (("Google Sheets güncellendi." if profile.get('output_target')=='google_sheets' else "Excel güncellendi.")
                                        if written else 'Yeni sonuç yazılmadı; bekleyen kayıtlar zaten işlenmiş veya hedef hücreler atlanmış. Ayrıntıları günlükten kontrol edin.')
                except _NoOutput:
                    summary["excel"] = "Excel/Sheets hücre hedefi seçilmedi; sonuçlar kaydedildi (GA4 Sheets sekmeleri ayrıca yazılır)."
                    summary["excel_written"] = None
                except ExcelLockedError:
                    summary["excel"] = "Excel dosyası açık olduğu için yazılamadı. Sonuçlar kaydedildi; dosyayı kapatıp 'Excel'i yeniden oluştur' deyin veya sonraki çalışmayı bekleyin."
                except ValueError as exc:
                    summary["excel"] = f"Çıktıya yazılmadı: {exc}"
                except OSError:
                    summary['excel'] = "Excel yazılamadı (dosya/klasör erişimi). Sonuçlar saklandı; dosya erişimini kontrol edip Excel yazımını yeniden deneyin."
                for note in notes:
                    log("ⓘ " + note)
                if catch_batches:
                    summary["catch_up"]["notes"] = notes
            log(summary["excel"])
            _ga4_sheets(profile, job_ids, summary, log)
        summary["seconds"] = round(time.monotonic() - started)
        store.write_last_run(profile["id"], summary)
        log(f"Bitti: {summary['ok']} başarılı, {summary['failed']} hatalı · {summary['seconds']} sn")
        return summary


class _NoOutput(Exception):
    pass


def _ga4_sheets_only(profile, log) -> dict:
    """Etkin işi olmayan, yalnız GA4 Sheets sekmelerine yazan proje (zamanlanmış görev): okuma/Excel yok."""
    planned, started = now(), time.monotonic()
    summary = {"profile_id": profile["id"], "run_id": uuid.uuid4().hex, "planned_at": planned.isoformat(), "write": True,
               "seconds": 0, "rows": [], "day_rows": [], "ga4": [], "ok": 0, "failed": 0, "excel": None, "excel_written": None}
    log(f"Çalışma başladı: {profile['name']} · yalnız GA4 Sheets sekmeleri")
    with store.profile_lock(profile["id"]):
        _ga4_sheets(profile, None, summary, log)
        ozet = summary.get("ga4_sheets") or {}
        summary.update(ok=ozet.get("tamam", 0) + ozet.get("uyari", 0), failed=ozet.get("hata", 0) if ozet else 1,
                       seconds=round(time.monotonic() - started))
        store.write_last_run(profile["id"], summary)
    log(f"Bitti: {summary['ok']} başarılı, {summary['failed']} hatalı · {summary['seconds']} sn")
    return summary


def _ga4_sheets(profile, job_ids, summary, log) -> None:
    """GA4 işlerinin kendi Sheets sekmelerine yazımı (Analytics Veri Çekme'nin zamanlı yazımı, aynen): Sheets listesinde
    işaretli GA4 işleri, 'etkin' olup olmadıklarından bağımsız; tek iş denemesinde yalnız seçilenler. Hata çalışmayı durdurmaz."""
    jobs = [j for j in profile["jobs"] if j["type"] == "ga4" and j["ga4"].get("sheets") is not None
            and (j["id"] in job_ids if job_ids else True)]
    if not jobs:
        return
    if profile["source"] == "demo":
        log("ⓘ GA4 Sheets sekmeleri: Demo (sentetik) sayılar Google Sheets'e yazılmaz.")
        return
    from .ga4 import store as ga4_store, zamanli
    log(f"GA4 Sheets sekmeleri yazılıyor: {len(jobs)} iş")
    try:
        ozet = zamanli.kayitli_calistir(profile["id"], [ga4_store.ga4_isi(j) for j in jobs], tetikleyen="netmera", bekle=60)
    except Exception as exc:  # kilit/ağ: Netmera sonuçları zaten yazıldı
        log(f"✗ GA4 Sheets yazımı yapılamadı: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
        return
    summary["ga4_sheets"] = ozet
    if ozet.get("genel_hata"):
        log(f"✗ GA4 Sheets: {ozet['genel_hata']}")
    for item in ozet.get("isler", []):
        log(f"   {'✓' if item['durum'] != 'hata' else '✗'} {item['is']} → {item.get('sekme')}: "
            + (item.get("mesaj") or f"{item.get('guncellenen', 0)} satır güncellendi, {item.get('eklenen', 0)} eklendi"))


def _formula_row(profile, job, planned, value, status, inputs, note, *, dates=None, daily=False) -> dict:
    """Hesaplama sonucu satırı; dönem ilk tarihli girdiden gelir (yoksa çekim anı)."""
    dated = next((r for r in inputs if r.get("period_start")), None)
    first, end = (d.isoformat() for d in period_bounds(*dates)) if dates and not dated else (None, None)
    return {
        "source": profile["source"], "job_id": job["id"], "job_label": job["label"], "job_type": "formula",
        "column": None, "column_label": column_label(job, None), "metric": "formula", "description": describe(job),
        "signature": signature(job), "audience": None, "audience_name": None,
        "value": value if status == "OK" else None, "status": status,
        "period_start": dated["period_start"] if dated else first, "period_end": dated["period_end"] if dated else end,
        "snapshot_slot": None if dated or dates else planned.isoformat(), "planned_at": planned.isoformat(),
        "captured_at": now().isoformat(), "note": note, **({"catch_up": True} if daily or any(r.get("catch_up") for r in inputs) else {}),
    }


def _formula_rows(profile, formulas, own, fallback, planned, sources, log, *, main: bool, elsewhere: set, dates=None, cell_days=False) -> list[dict]:
    """own: bu birimin (ana çalışma veya bir gün) satırları; fallback: günlük birimde ana çalışmanın
    (anlık) satırları. Günlük birimde hesap yalnız girdilerinden biri o güne aitse yapılır."""
    from .formula import evaluate
    from .cell_mapping import _target_day
    out: list[dict] = []
    for job in formulas:
        job_vars = [v for v in job["variables"] if v["kind"] == "job"]
        cell_vars = [v for v in job["variables"] if v["kind"] == "cell"]
        cell_dates = dates if cell_vars else None
        pool = own + out
        def find(var):
            hits = [r for r in pool if r["job_id"] == var["job_id"] and r.get("column") == var["column"]]
            return hits or [r for r in fallback if r["job_id"] == var["job_id"] and r.get("column") == var["column"]]
        found = {v["name"]: find(v) for v in job_vars}
        local = {v["name"] for v in job_vars if any(r in pool for r in found[v["name"]])}
        # Günlük girdiyi kullanan hesaplama o günün biriminde yapılır. Açıkça günlere bölünmüş
        # çalışmada yalnız hücre kullanan hesaplamalar da her seçili gün için çözülür.
        if ((not job_vars and job['id'] in elsewhere) or any(v["job_id"] in elsewhere and v["name"] not in local for v in job_vars)
                if main else not local and not (cell_days and not job_vars)):
            continue
        values, inputs, notes, status = {}, [], [], "OK"
        for var in job_vars:
            hits = found[var["name"]]
            if len(hits) != 1:
                status = status if status != "OK" else "FORMULA_INPUT_MISSING"
                notes.append(f"{var['name']}: sonuç {'yok' if not hits else 'tekil değil'}")
            elif hits[0]["status"] != "OK":
                status = status if status != "OK" else "FORMULA_INPUT_FAILED"
                notes.append(f"{var['name']}: '{hits[0]['job_label']}' {hits[0]['status']}")
            else:
                values[var["name"]] = hits[0]["value"]
                inputs.append(hits[0])
                notes.append(f"{var['name']}={hits[0]['value']:g}" if isinstance(hits[0]["value"], float) else f"{var['name']}={hits[0]['value']}")
        stub = _formula_row(profile, job, planned, None, "OK", inputs, None, dates=cell_dates)
        for var in cell_vars:
            if status != "OK":
                break
            try:
                day = _target_day(stub, var.get("date_basis", "data_day"))
                values[var["name"]], where = sources.read(var, day)
                notes.append(f"{var['name']}={values[var['name']]:g} ({where}; {day:%d.%m.%Y})")
            except (ValueError, KeyError, TypeError) as exc:
                status = "FORMULA_CELL_INVALID"
                notes.append(f"{var['name']}: {exc if isinstance(exc, ValueError) else 'hücre okunamadı'}")
        value = None
        if status == "OK":
            try:
                value = evaluate(job["expression"], values)
            except ValueError as exc:
                status, value = "FORMULA_ERROR", None
                notes.append(str(exc))
        result = f" → {value:g}" if isinstance(value, float) else f" → {value}" if value is not None else ""
        row = _formula_row(profile, job, planned, value, status, inputs, f"{job['expression']} · " + ", ".join(notes) + result,
                           dates=cell_dates, daily=not main and cell_days)
        when = f" · {row['period_start'][:10]}" if not main and row["period_start"] else ""
        log(f"∑ {job['label']}{when}: " + (row["note"] if status == "OK" else f"✗ {row['note']}"))
        out.append(row)
    return out


def _add_formula_rows(profile, formulas, rows, catch_batches, planned, log, *, dates=None, split_days=False) -> None:
    """Okunan iş sonuçlarından hesaplamaları yapar; ana satırlara ve her günün satırlarına ekler."""
    from .formula import Sources
    sources = Sources(profile)
    try:
        elsewhere = {r["job_id"] for day_rows in catch_batches.values() for r in day_rows}
        if split_days:
            for n in range((dates[1] - dates[0]).days + 1):
                catch_batches.setdefault(dates[0] + timedelta(days=n), [])
            for job in formulas:
                refs = [v['job_id'] for v in job['variables'] if v['kind'] == 'job']
                if not refs or any(ref in elsewhere for ref in refs):
                    elsewhere.add(job['id'])
        rows.extend(_formula_rows(profile, formulas, rows, [], planned, sources, log, main=True, elsewhere=elsewhere,
                                  dates=dates if not split_days else None))
        for day, day_rows in catch_batches.items():
            day_rows.extend(_formula_rows(profile, formulas, day_rows, rows, planned, sources, log, main=False,
                                         elsewhere=set(), dates=(day, day) if split_days else None, cell_days=split_days))
    finally:
        sources.close()


def _open_with_login(reader,auto_login):
    try:
        reader.open_panel()
    except PanelError:
        if not is_login_page(reader.page):
            raise
        auto_login.recover()
        reader.open_panel()


def query_key(job: dict) -> str:
    """Aynı Netmera sorgusu (event, filtre, hedef kitle) → tek Find; metrik ve sütunlar ayrı okunur. Diğer işler kendi başına."""
    if job["type"] != "event":
        return json.dumps({"id": job["id"]})
    return json.dumps({k: job.get(k) for k in ("event", "filters", "audience", "audience_name")}, sort_keys=True, ensure_ascii=False)


def _merge_batches(into: dict, more: dict) -> None:
    for day, rows in more.items():
        into.setdefault(day, []).extend(rows)


def _units(reader, jobs, planned, dates):
    """Çalışma birimleri: (ilk gün, son gün, işler). Aynı sorgu ve dönemdeki Event işleri birlikte okunur."""
    many = hasattr(reader, "fetch_many")
    units, index = [], {}
    for job in jobs:
        if job["type"] not in DATED_TYPES:
            units.append((planned.date(), planned.date(), [job]))  # People/Tag/Segment: çekim anı
            continue
        first, last = dates or job_days(job, planned)  # çalıştırırken seçilen tarih işin tarihini geçersiz kılar
        key = (query_key(job), first, last)
        if many and key in index:
            units[index[key]][2].append(job)
        else:
            index[key] = len(units)
            units.append((first, last, [job]))
    return units


def _run_unit(reader, profile, members, first, last, planned, rows, log, cancel, state, auto_login=None, per_day=False):
    """Bir birimi en çok MAX_ATTEMPTS kez dener; yalnız hatalı işler yeniden okunur."""
    columns = lambda j: j.get("columns") or [None]  # noqa: E731
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]

    def fail(job, code):
        for day in (days if per_day else [None]):
            a, b = (day, day) if per_day else (first, last)
            rows.extend(_row(profile, job, c, None, code, a, b, planned) for c in columns(job))

    if cancel.is_set() or state["fatal"]:
        for job in members:
            fail(job, "CANCELLED" if cancel.is_set() else state["fatal"].code)
        return
    many = len(members) > 1 or per_day
    title = ", ".join(j["label"] for j in members)
    if per_day:
        title += f" · {first:%d.%m}–{last:%d.%m} günlük"
    log(f"▶ {title}" + (f"  (tek sorguda {len(members)} iş)" if len(members) > 1 else ""))
    pending, attempt = list(members), 1
    while pending:
        if cancel.is_set():
            for job in pending:
                fail(job, "CANCELLED")
            return
        try:
            if hasattr(reader, "fetch_many") and pending[0]["type"] == "event":
                out = reader.fetch_many(pending, first, last, per_day=per_day) if per_day else reader.fetch_many(pending, first, last)
                out = {k: v if isinstance(v, PanelError) else (v, None) for k, v in out.items()}
            elif per_day:
                out = {}
                for job in pending:  # okuyucu günlük toplu okumayı bilmiyorsa günler tek tek okunur
                    fetched = {day: reader.fetch(job, day, day) for day in days}
                    out[job["id"]] = ({d: v[0] for d, v in fetched.items()}, next((v[1] for v in fetched.values() if v[1]), None))
            else:
                out = {pending[0]["id"]: reader.fetch(pending[0], first, last)}
        except PanelError as exc:
            out = {job["id"]: exc for job in pending}
        except Exception as exc:  # Playwright/tarayıcı hatası: çökme yerine normal deneme kuralı
            error = PanelError("STEP_FAILED", detail=f"tarayıcı hatası: {type(exc).__name__}")
            out = {job["id"]: error for job in pending}
        failed = []
        for job in pending:
            result = out.get(job["id"], PanelError("STEP_FAILED"))
            if isinstance(result, PanelError):
                failed.append((job, result))
                continue
            values, note = result
            if per_day:
                for day in days:
                    value = values.get(day, PanelError("NO_RECORD"))
                    if isinstance(value, PanelError):
                        rows.extend(_row(profile, job, c, None, value.code, day, day, planned) for c in columns(job))
                    else:
                        rows.extend(_row(profile, job, c, value[c], "OK", day, day, planned, note) for c in columns(job))
            else:
                rows.extend(_row(profile, job, c, values[c], "OK", first, last, planned, note) for c in columns(job))
            log(f"   ✓ {job['label']}" if many else "   ✓ tamam")
        if not failed:
            return
        exc = failed[0][1]
        if auto_login and is_login_page(reader.page):
            try:
                auto_login.recover()
            except PanelError as login_error:
                exc = login_error
                failed = [(job, login_error) for job, _ in failed]
            else:
                log('   ↻ Giriş yenilendi; aynı iş baştan okunuyor')
                pending = [job for job, _ in failed]
                continue  # Giriş düzeltmesi, sorgunun deneme hakkını tüketmez.
        for job, error in failed:
            log(f"   ✗ {job['label'] + ': ' if many else ''}{error.message()}")
        if exc.code not in FATAL:
            _diagnose_and_reset(reader, profile, failed[0][0], exc, log)
        if exc.code in FATAL or exc.code in NO_RETRY or attempt == MAX_ATTEMPTS:
            for job, error in failed:
                fail(job, exc.code if exc.code in FATAL else error.code)
            if exc.code in FATAL:
                state["fatal"] = exc
            return
        log(f"   ↻ yeniden deneniyor ({attempt + 1}/{MAX_ATTEMPTS})")
        pending = [job for job, _ in failed]
        attempt += 1


def _run_jobs(reader, profile, jobs, planned, rows, log, cancel, dates=None,auto_login=None):
    state = {"fatal": None}
    for first, last, members in _units(reader, jobs, planned, dates):
        _run_unit(reader, profile, members, first, last, planned, rows, log, cancel, state, auto_login)


def _diagnose_and_reset(reader, profile: dict, job: dict, exc: PanelError, log) -> None:
    """Hata anındaki ekran yapısını (sayılar maskeli) kaydeder, sonra paneli sıfırlar.

    Sıfırlama, açık kalan bir pencerenin sonraki işlerin tıklamalarını engellemesini önler.
    """
    if is_login_page(getattr(reader,'page',None)):
        return  # Giriş formu ve hata mesajları teşhise alınmaz.
    if hasattr(reader, "snapshot"):
        try:
            folder = store.profile_dir(profile["id"]) / "teshis"
            folder.mkdir(parents=True, exist_ok=True)
            snap = {"zaman": now().isoformat(), "is": job["label"], "adim": exc.step, "kod": exc.code,
                    "ayrinti": exc.detail, **reader.snapshot()}
            path = folder / f"{now():%Y%m%d-%H%M%S}-{job['id']}.json"
            store.write_json(path, snap)
            for old in sorted(folder.glob("*.json"))[:-30]:
                old.unlink(missing_ok=True)
            log(f"   ⓘ teşhis kaydedildi (sayılar maskeli): {path.name}")
        except Exception:
            log("   ⓘ teşhis alınamadı")
    if hasattr(reader, "recover"):
        try:
            reader.recover()
        except PanelError as reset_error:
            log(f"   ✗ panel sıfırlanamadı: {reset_error.message()}")
        except Exception as reset_error:  # sıfırlama hatası çalışmayı durdurmaz; sonraki deneme sayfayı yeniden açar
            log(f"   ✗ panel sıfırlanamadı (tarayıcı hatası: {type(reset_error).__name__})")


def login(profile: dict, *, log=print, cancel: threading.Event | None = None, minutes: int = 15) -> bool:
    """Tarayıcıyı açar; kullanıcı giriş yapıp uygulamayı seçince doğrular ve kapatır."""
    cancel = cancel or threading.Event()
    if profile["source"] == "demo":
        log("Demo profili Netmera'ya bağlanmaz; giriş gerekmez.")
        return True
    if not profile["netmera_app"]:
        raise RunError("Önce Ayarlar'da Netmera uygulama adını yazın.")
    with store.profile_lock(profile["id"]):
        with Browser(profile, visible=True) as browser:
            page = browser.page
            page.goto(PANEL_URL, wait_until="domcontentloaded")
            reader = PanelReader(page, profile["netmera_app"], profile["timeout_seconds"], log)
            log("Açılan Chrome penceresinde kendiniz giriş yapın (şifre bu uygulamaya yazılmaz/kaydedilmez).")
            log(f"Girişten sonra panelde '{profile['netmera_app']}' uygulamasını seçin. Doğrulanınca pencere kendiliğinden kapanır.")
            deadline = time.monotonic() + minutes * 60
            last_hint = ""
            while time.monotonic() < deadline and not cancel.is_set():
                if page.is_closed():
                    log("Tarayıcı penceresi kapatıldı; giriş doğrulanmadı.")
                    return False
                try:
                    reader.verify_app()
                    log("✓ Giriş ve uygulama doğrulandı. Oturum bu bilgisayarda, otomasyon profilinde saklandı.")
                    page.wait_for_timeout(1500)
                    return True
                except PanelError as exc:
                    hint = exc.message()
                    if hint != last_hint and exc.code == "APP_MISMATCH":
                        log(hint)
                    last_hint = hint
                except Exception:
                    pass
                time.sleep(2)
            log("Giriş süresi doldu veya iptal edildi.")
            return False


def fetch_catalog(profile: dict, *, log=print) -> list[str]:
    if profile["source"] == "demo":
        return []
    with store.profile_lock(profile["id"]):
        with Browser(profile, visible=not profile["headless"]) as browser:
            reader = PanelReader(browser.page, profile["netmera_app"], profile["timeout_seconds"], log)
            try:
                _open_with_login(reader,AutoLogin(profile,reader,log))
                names = reader.event_catalog()
            except PanelError as exc:
                raise RunError(exc.message()) from None
    store.write_json(store.profile_dir(profile["id"]) / "katalog.json", {"events": names, "fetched_at": now().isoformat()})
    log(f"{len(names)} event adı alındı.")
    return names


def fetch_segment_rules(profile: dict, name: str, *, log=print) -> dict:
    """Segmentin mevcut kurallarını okur; segment değiştirilmez/kaydedilmez."""
    if profile["source"] == "demo":
        raise RunError("Demo projesi Netmera'ya bağlanmaz; kuralları elle girin.")
    with store.profile_lock(profile["id"]):
        with Browser(profile, visible=not profile["headless"]) as browser:
            reader = PanelReader(browser.page, profile["netmera_app"], profile["timeout_seconds"], log)
            try:
                _open_with_login(reader, AutoLogin(profile, reader, log))
                rules = reader.segment_rules(name)
            except PanelError as exc:
                raise RunError(exc.message()) from None
    count = sum(len(i["rules"]) if i["kind"] == "group" else 1 for i in rules["items"])
    log(f"✓ '{name}' segmentinden {count} kural okundu (segment değiştirilmedi).")
    return {**rules, "name": name}


__all__ = ["RunError", "run_profile", "login", "fetch_catalog", "fetch_segment_rules", "rebuild_excel", "ERRORS"]
