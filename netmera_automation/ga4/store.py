"""GA4 işlerinin deposu: Netmera projesinin iş listesindeki `ga4` türündeki işler (birleşik uygulama, 2026-10-28).

İş, Analytics Veri Çekme biçimindedir (`ad`, `etkin`, `metrikler`, …); Netmera profilinde
`{"type": "ga4", "label": ad, "enabled": etkin, "ga4": {...geri kalan alanlar}}` olarak saklanır. Netmera'nın
diğer işleri (Event, People, Hesaplama…) ve sırası korunur; GA4 işi yeniden adlandırılınca iç kimliği aynı kalır
(Hesaplama ve hücre eşlemesi bozulmasın). `proje` verilmezse varsayılan proje kullanılır.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from . import isler, projeler

FILE_NAME = "profil.json"


def path(proje: str | None = None) -> Path:
    pid = proje or projeler.varsayilan()
    return projeler.klasor(pid).parent / FILE_NAME


def ga4_isi(job: dict) -> dict:
    """Netmera işi → GA4 işi (Analytics biçimi)."""
    return {"ad": job["label"], "etkin": job["enabled"], **job["ga4"]}


def netmera_isi(job: dict, kimlik: str | None = None) -> dict:
    """GA4 işi (doğrulanmış) → Netmera işi."""
    out = {"type": "ga4", "label": job["ad"], "enabled": job["etkin"], "ga4": {k: v for k, v in job.items() if k not in ("ad", "etkin")}}
    if kimlik:
        out["id"] = kimlik
    return out


def load(proje: str | None = None) -> list[dict]:
    profile = projeler._yukle(proje or projeler.varsayilan())
    return [ga4_isi(j) for j in profile["jobs"] if j["type"] == "ga4"]


def save(jobs: list[dict], proje: str | None = None) -> list[dict]:
    """Doğrular ve projeye yazar. Boş liste de geçerlidir (projenin GA4 işleri kalkar, diğer işler kalır)."""
    normal = [isler.normalize_job(job) for job in jobs]
    adlar = [isler.ad_anahtari(j["ad"]) for j in normal]
    if len(set(adlar)) != len(adlar):
        raise isler.JobError("Aynı adla iki iş olamaz.")
    pid = proje or projeler.varsayilan()
    profile = projeler._yukle(pid)
    slots = [i for i, j in enumerate(profile["jobs"]) if j["type"] == "ga4"]
    old = [profile["jobs"][i] for i in slots]
    old_by_key = {isler.ad_anahtari(j["label"]): j for j in old}
    new_keys = set(adlar)
    yeni = []
    for i, job in enumerate(normal):
        key = adlar[i]
        if key in old_by_key:
            kimlik = old_by_key[key]["id"]
        elif i < len(old) and isler.ad_anahtari(old[i]["label"]) not in new_keys:
            kimlik = old[i]["id"]  # aynı yerdeki iş yeniden adlandırıldı: kimlik korunur
        else:
            kimlik = None
        yeni.append(netmera_isi(job, kimlik))
    rebuilt, kalan = [], list(yeni)
    for i, job in enumerate(profile["jobs"]):
        if job["type"] != "ga4":
            rebuilt.append(job)
        elif kalan:
            rebuilt.append(kalan.pop(0))
    rebuilt.extend(kalan)
    profile["jobs"] = rebuilt
    from .. import store as netmera_store
    from ..jobs import JobError as NetmeraJobError
    try:
        netmera_store.save_profile(profile)
    except NetmeraJobError as exc:
        raise isler.JobError(str(exc)) from None
    return normal


def upsert(job: dict, eski_ad: str | None = None, proje: str | None = None) -> list[dict]:
    """`eski_ad` yoksa yeni iş ekler (aynı ad varsa reddedilir, üzerine yazılmaz); varsa o işi değiştirir (ad değişebilir)."""
    yeni = isler.normalize_job(job)
    jobs = load(proje)
    konum = None
    if eski_ad:
        hedef = isler.ad_anahtari(eski_ad)
        konum = next((i for i, j in enumerate(jobs) if isler.ad_anahtari(j["ad"]) == hedef), None)
        if konum is None:
            raise isler.JobError(f"'{eski_ad}' adlı iş bulunamadı; sayfayı yenile.")
    if any(isler.ad_anahtari(j["ad"]) == isler.ad_anahtari(yeni["ad"]) for i, j in enumerate(jobs) if i != konum):
        raise isler.JobError(f"'{yeni['ad']}' adı başka bir işte kullanılıyor.")
    if konum is None:
        jobs.append(yeni)
    else:
        if "paket" not in job and jobs[konum].get("paket"):  # düzenleyici paket bilgisini göndermese de iş paketinde kalır
            yeni["paket"] = jobs[konum]["paket"]
        jobs[konum] = yeni
    return save(jobs, proje)


def delete(ad: str, proje: str | None = None) -> list[dict]:
    jobs = load(proje)
    kalan = [j for j in jobs if isler.ad_anahtari(j["ad"]) != isler.ad_anahtari(ad)]
    if len(kalan) == len(jobs):
        raise isler.JobError(f"'{ad}' adlı iş bulunamadı.")
    return save(kalan, proje)


def degistir(adlar: list[str], degisiklik, proje: str | None = None) -> list[dict]:
    """Adı verilen işlere `degisiklik(job)` uygular (job kopyasını değiştirir) ve doğrulayıp kaydeder."""
    istenen = {isler.ad_anahtari(a) for a in adlar}
    jobs = load(proje)
    bulunan = [j for j in jobs if isler.ad_anahtari(j["ad"]) in istenen]
    if len(bulunan) != len(istenen):
        raise isler.JobError("İş bulunamadı; sayfayı yenile.")
    for j in bulunan:
        degisiklik(j)
    return save(jobs, proje)


def sheets_ayarla(ad: str, sheets: dict | None, proje: str | None = None) -> list[dict]:
    """İşin Google Sheets'e yazılıp yazılmayacağı ve sekmesi/özel ayarı (None: yazılmaz)."""
    def uygula(j):
        if sheets is None:
            j.pop("sheets", None)
        else:
            j["sheets"] = sheets
    return degistir([ad], uygula, proje)


def sablon_kaydet(adlar: list[str], duzen: str, sablon: dict | None, proje: str | None = None) -> list[dict]:
    """İşlerin dışa aktarma sütun şablonunu (bir satır düzeni için) kaydeder; None: şablon kaldırılır."""
    if duzen not in isler.SABLON_DUZENLER:
        raise isler.JobError("Satır düzeni 'tablo' ya da 'liste' olmalı.")

    def uygula(j):
        mevcut = dict(j.get("sablon") or {})
        if sablon is None:
            mevcut.pop(duzen, None)
        else:
            mevcut[duzen] = sablon
        if mevcut:
            j["sablon"] = mevcut
        else:
            j.pop("sablon", None)
    return degistir(adlar, uygula, proje)


def import_jobs(raw_text: str, proje: str | None = None) -> dict:
    """Site Yapı Çıkarıcı taslağı, dışa aktarılmış taslak veya CLI iş dosyası içe aktarılır. Geçersiz iş atlanır, aynı ad numaralanır."""
    try:
        raw = json.loads(raw_text)
    except ValueError:
        raise isler.JobError("Dosya JSON değil.") from None
    items = raw.get("isler") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise isler.JobError("Dosyada 'isler' listesi yok (ga4-taslak-isler-*.json ya da analytics-taslak-*.json bekleniyor).")
    jobs = load(proje)
    adlar = {isler.ad_anahtari(j["ad"]) for j in jobs}
    eklenen, atlanan = [], []
    for item in items[:200]:
        try:
            job = isler.normalize_job(item)
        except isler.JobError as exc:
            atlanan.append({"ad": str(item.get("ad", "?"))[:100] if isinstance(item, dict) else "?", "hata": str(exc)})
            continue
        temel, n = job["ad"][:94], 1
        while isler.ad_anahtari(job["ad"]) in adlar:
            n += 1
            job["ad"] = f"{temel} ({n})"
        adlar.add(isler.ad_anahtari(job["ad"]))
        jobs.append(job)
        eklenen.append(job["ad"])
    save(jobs, proje)
    from . import adlar
    return {"eklenen": eklenen, "atlanan": atlanan, "ogrenilen_ad": adlar.ogren(raw.get("etiketler"))}


def export_jobs(proje: str | None = None) -> dict:
    """Projenin işlerini içe aktarılabilir taslak dosyası olarak verir (sonuç/sayı içermez)."""
    pid = proje or projeler.varsayilan()
    bilgi = projeler.oku(pid)
    jobs = load(pid)
    if not jobs:
        raise isler.JobError("Bu projede dışa aktarılacak iş yok.")
    icerik = {"aciklama": f"Netmera Otomasyonu GA4 iş taslağı — proje: {bilgi['ad']} ({len(jobs)} iş). İşler → Taslak içe aktar ile yüklenir.",
              "isler": jobs}
    return {"ad": f"analytics-taslak-{pid}-{datetime.now():%Y-%m-%d}.json", "icerik": json.dumps(icerik, ensure_ascii=False, indent=2),
            "is_sayisi": len(jobs)}
