"""Paketler: hazır rapor kütüphanesinden (hazir_raporlar.py) grup halinde çekilen raporlar ve kendiliğinden güncelleme.

Paket kimliği `kutuphane:<grup>`. Kütüphaneye rapor eklenince çekilmiş pakete kendiliğinden eklenir (esitle); kullanıcının
sildiği rapor `haric` listesine girer, geri gelmez. İşin kendisi normal iştir; `paket` alanı yalnız nereden geldiğini söyler.
Eşitleme mevcut rapora dokunmaz (kullanıcının değiştirdiği iş ezilmez). v0.9.0'daki `tarama:` paketleri (Site Yapı Çıkarıcı)
kaldırıldı; eski kayıtlar okunur, işleri normal iş olarak durur.
"""
from __future__ import annotations

from datetime import datetime

from . import adlar, hazir_raporlar as H, isler, projeler, store


def _simdi() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _paket_isleri(jobs: list[dict], kimlik: str) -> dict[str, dict]:
    return {j["paket"]["rapor"]: j for j in jobs if (j.get("paket") or {}).get("kimlik") == kimlik}


def _eksik(tanim: dict, meta: dict | None) -> list[str]:
    if not meta:
        return []
    boyutlar = [*tanim["boyutlar"], *(f["alan"] for f in tanim.get("filtreler", []))]
    return [b for b in boyutlar if b not in meta["boyutlar"]] + [m for m in tanim["metrikler"] if m not in meta["metrikler"]]


def _cikar(tanim: dict, eksik: list[str]) -> dict | None:
    """Mülkte olmayan alanları çıkarır; metrik kalmazsa rapor çekilemez (None)."""
    t = {**tanim, "metrikler": [m for m in tanim["metrikler"] if m not in eksik], "boyutlar": [b for b in tanim["boyutlar"] if b not in eksik],
         "filtreler": [f for f in tanim.get("filtreler", []) if f["alan"] not in eksik]}
    return t if t["metrikler"] else None


def _benzersiz_ad(ad: str, adlar_: set[str]) -> str:
    temel, n, aday = ad[:94], 1, ad
    while isler.ad_anahtari(aday) in adlar_:
        n += 1
        aday = f"{temel} ({n})"
    adlar_.add(isler.ad_anahtari(aday))
    return aday


def _paket_kaydi(proje: dict, kimlik: str, ad: str, tarih: dict | None) -> list[dict]:
    liste = [dict(p) for p in proje.get("paketler") or []]
    kayit = next((p for p in liste if p["kimlik"] == kimlik), None)
    if kayit is None:
        liste.append({"kimlik": kimlik, "ad": ad, "kaynak": kimlik.split(":")[0], "tarih": tarih, "cekildi": _simdi(), "haric": [],
                      "surum": H.SURUM if kimlik.startswith("kutuphane:") else 0})
    else:
        kayit.update(ad=ad, cekildi=_simdi())
    return liste


# ---------- kütüphane ----------

def agac(meta: dict | None = None, jobs: list[dict] | None = None) -> list[dict]:
    """Arayüz için kütüphane ağacı; her raporda mülkte olmayan alanlar ve projede zaten olup olmadığı."""
    out, ogrenilmis = [], adlar.ogrenilen()
    for p in H.KUTUPHANE:
        kimlik = f"kutuphane:{p['kimlik']}"
        mevcut = _paket_isleri(jobs or [], kimlik)
        raporlar = []
        for r in p["raporlar"]:
            tanim = H.is_tanimi(p, r) if not r.get("desteklenmiyor") else None
            alanlar = sorted({*r["boyutlar"], *r.get("secenekler", []), *r["metrikler"], *(f["alan"] for f in r.get("filtreler", []))})
            raporlar.append({"kimlik": r["kimlik"], "ad": r["ad"], "aciklama": r.get("aciklama", ""), "boyutlar": r["boyutlar"],
                             "secenekler": r.get("secenekler", []), "metrikler": r["metrikler"], "filtreler": r.get("filtreler", []),
                             "desteklenmiyor": r.get("desteklenmiyor"), "eksik": _eksik(tanim, meta) if tanim else [],
                             "projede": r["kimlik"] in mevcut, "adlar": {a: adlar.bilgi(a, ogrenilmis=ogrenilmis)["ad"] for a in alanlar}})
        out.append({"kimlik": kimlik, "koleksiyon": p["koleksiyon"], "ad": p["ad"], "tam_ad": H.paket_adi(p), "raporlar": raporlar})
    return out


def cek(pid: str, secim: list[dict], *, tarih: dict | None = None, etkin: bool = True, eksik_onay: bool = False,
        meta: dict | None = None, property_ref: str = "PROP_PROD") -> dict:
    """secim: [{"paket": "kutuphane:…", "raporlar": [kimlik…] | None (hepsi), "boyutlar": {rapor: boyut}}].
    Mülkte olmayan alan varsa ve onay yoksa hiçbir şey eklenmez, onay istenir (sessiz varsayılan yok)."""
    if tarih is not None:
        tarih = isler.normalize_period(tarih, "Tarih")
    proje = projeler.oku(pid)
    jobs = store.load(pid)
    ad_kumesi = {isler.ad_anahtari(j["ad"]) for j in jobs}
    eklenecek, zaten, eksikler, atlanan = [], [], [], []
    paketler_ = {}
    for s in secim:
        kimlik = str(s.get("paket", ""))
        p = H.paket(kimlik.removeprefix("kutuphane:"))
        if p is None or not kimlik.startswith("kutuphane:"):
            raise isler.JobError(f"Kütüphanede böyle bir rapor grubu yok: {kimlik}")
        istenen = set(s.get("raporlar") or [r["kimlik"] for r in p["raporlar"]])
        mevcut = _paket_isleri(jobs, kimlik)
        for r in p["raporlar"]:
            if r["kimlik"] not in istenen or r.get("desteklenmiyor"):
                continue
            if r["kimlik"] in mevcut:
                zaten.append(r["ad"])
                continue
            tanim = H.is_tanimi(p, r, tarih=tarih, boyut=(s.get("boyutlar") or {}).get(r["kimlik"]), property_ref=property_ref, etkin=etkin)
            eksik = _eksik(tanim, meta)
            if eksik:
                eksikler.append({"rapor": r["ad"], "alanlar": [adlar.bilgi(a)["ad"] + f" ({a})" for a in eksik]})
                tanim = _cikar(tanim, eksik)
                if tanim is None:
                    atlanan.append(r["ad"])
                    continue
            eklenecek.append(tanim)
            paketler_[kimlik] = p
    if eksikler and not eksik_onay:
        return {"onay_gerekli": True, "eksikler": eksikler, "eklenen": [], "zaten": zaten, "atlanan": atlanan}
    for tanim in eklenecek:
        tanim["ad"] = _benzersiz_ad(tanim["ad"], ad_kumesi)
        jobs.append(isler.normalize_job(tanim))
    if eklenecek:
        store.save(jobs, pid)
        liste = proje.get("paketler") or []
        for kimlik, p in paketler_.items():
            proje["paketler"] = liste = _paket_kaydi({"paketler": liste}, kimlik, H.paket_adi(p), tarih)
            alinan = {t["paket"]["rapor"] for t in eklenecek if t["paket"]["kimlik"] == kimlik}
            for kayit in liste:
                if kayit["kimlik"] == kimlik:  # yeniden çekilen rapor artık hariç değil
                    kayit["haric"] = [h for h in kayit["haric"] if h not in alinan]
        projeler.guncelle(pid, {"paketler": liste})
    return {"onay_gerekli": False, "eksikler": eksikler, "eklenen": [t["ad"] for t in eklenecek], "zaten": zaten, "atlanan": atlanan}


def esitle(pid: str, meta: dict | None = None) -> dict:
    """Çekilmiş kütüphane paketlerine kütüphanede yeni olan raporları ekler; kütüphaneden kalkanı işaretler (silmez)."""
    proje = projeler.oku(pid)
    jobs = store.load(pid)
    ad_kumesi = {isler.ad_anahtari(j["ad"]) for j in jobs}
    eklenen, isaretlenen, onay_bekleyen = [], [], []
    degisti = False
    for kayit in proje.get("paketler") or []:
        if kayit["kaynak"] != "kutuphane":
            continue
        p = H.paket(kayit["kimlik"].removeprefix("kutuphane:"))
        mevcut = _paket_isleri(jobs, kayit["kimlik"])
        kimlikler = {r["kimlik"] for r in p["raporlar"]} if p else set()
        for rapor, j in mevcut.items():
            durum = j["paket"].get("durum")
            if rapor not in kimlikler and durum != "kutuphanede_yok":
                j["paket"] = {**j["paket"], "durum": "kutuphanede_yok"}
                isaretlenen.append(j["ad"])
                degisti = True
            elif rapor in kimlikler and durum == "kutuphanede_yok":
                j["paket"] = {k: v for k, v in j["paket"].items() if k != "durum"}
                degisti = True
        if not p:
            continue
        for r in p["raporlar"]:
            if r.get("desteklenmiyor") or r["kimlik"] in mevcut or r["kimlik"] in kayit["haric"]:
                continue
            tanim = H.is_tanimi(p, r, tarih=kayit.get("tarih"))
            if _eksik(tanim, meta):  # mülkte olmayan alanla sessizce eklenmez; arayüz "çek" ile sorar
                onay_bekleyen.append({"paket": kayit["ad"], "rapor": r["ad"]})
                continue
            tanim["ad"] = _benzersiz_ad(tanim["ad"], ad_kumesi)
            jobs.append(isler.normalize_job(tanim))
            eklenen.append({"paket": kayit["ad"], "rapor": tanim["ad"]})
            degisti = True
    if degisti:
        store.save(jobs, pid)
    if eklenen or any(k["kaynak"] == "kutuphane" and k.get("surum") != H.SURUM for k in proje.get("paketler") or []):
        projeler.guncelle(pid, {"paketler": [{**k, "surum": H.SURUM} if k["kaynak"] == "kutuphane" else k for k in proje["paketler"]]})
    return {"eklenen": eklenen, "isaretlenen": isaretlenen, "onay_bekleyen": onay_bekleyen}


def is_silindi(pid: str, job: dict):
    """Paketten bir iş silinince o rapor paketin `haric` listesine girer: eşitleme geri getirmez."""
    paket = job.get("paket")
    if not paket:
        return
    proje = projeler.oku(pid)
    liste = [dict(k) for k in proje.get("paketler") or []]
    for k in liste:
        if k["kimlik"] == paket["kimlik"] and paket["rapor"] not in k["haric"]:
            k["haric"] = sorted({*k["haric"], paket["rapor"]})
            projeler.guncelle(pid, {"paketler": liste})
            return


def paket_sil(pid: str, kimlik: str) -> dict:
    """Paketin bütün işlerini ve kaydını siler (Google tablosuna ve GA4'e dokunulmaz)."""
    jobs = store.load(pid)
    kalan = [j for j in jobs if (j.get("paket") or {}).get("kimlik") != kimlik]
    proje = projeler.oku(pid)
    if len(kalan) == len(jobs) and not any(k["kimlik"] == kimlik for k in proje.get("paketler") or []):
        raise isler.JobError("Böyle bir paket yok; sayfayı yenile.")
    store.save(kalan, pid)
    projeler.guncelle(pid, {"paketler": [k for k in proje.get("paketler") or [] if k["kimlik"] != kimlik]})
    return {"silinen": len(jobs) - len(kalan)}


def ozet(pid: str) -> list[dict]:
    """Projedeki paketler: ad, kaynak, rapor sayısı, kütüphanede bekleyen yeni rapor sayısı."""
    proje = projeler.oku(pid)
    jobs = store.load(pid)
    out = []
    for k in proje.get("paketler") or []:
        mevcut = _paket_isleri(jobs, k["kimlik"])
        yeni = 0
        if k["kaynak"] == "kutuphane":
            p = H.paket(k["kimlik"].removeprefix("kutuphane:"))
            yeni = sum(1 for r in (p or {}).get("raporlar", []) if not r.get("desteklenmiyor") and r["kimlik"] not in mevcut
                       and r["kimlik"] not in k["haric"])
        out.append({**k, "rapor_sayisi": len(mevcut), "yeni": yeni})
    return out
