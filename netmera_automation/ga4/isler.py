"""İş tanımları: GA4'ten hangi toplamların hangi filtre ve tarihle çekileceği.

Metrik/boyut adları GA4 API adlarıdır (ör. activeUsers, deviceCategory). Adın property'de gerçekten
bulunduğu meta.py ile ayrıca doğrulanır; burada yalnız biçim ve kurallar denetlenir. Tanınmayan alan
sessizce yok sayılmaz, hata verir.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re

API_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)?$")
PROPERTY_REF = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
MAX_METRICS, MAX_DIMENSIONS, MAX_FILTERS = 10, 9, 10
DEFAULT_LIMIT, MAX_LIMIT = 1000, 10000
STRING_OPERATORS = ("EXACT", "BEGINS_WITH", "ENDS_WITH", "CONTAINS", "FULL_REGEXP", "PARTIAL_REGEXP")
LIST_OPERATOR = "IN_LIST"
PERIODS = {"dun": "Dün", "bugun": "Bugün (şu ana kadar)", "son_n_gun": "Son N tam gün (bugün hariç)",
           "son_n_hafta": "Son N tam hafta (Pzt–Paz, bu hafta hariç)", "son_n_ay": "Son N tam ay (bu ay hariç)",
           "son_n_yil": "Son N tam yıl (bu yıl hariç)", "ay_basindan": "Ay başından düne", "yil_basindan": "Yıl başından düne",
           "aralik": "Belirli tarih aralığı"}
N_LIMITS = {"son_n_gun": 365, "son_n_hafta": 104, "son_n_ay": 36, "son_n_yil": 10}
# Zaman kırılımı → GA4 boyutu. Tekil kullanıcı her dönem için GA4'te hesaplanır (günlerin toplamı değildir).
ZAMAN = {"gun": ("date", "Gün"), "hafta": ("isoYearIsoWeek", "Hafta (Pzt başlar)"), "ay": ("yearMonth", "Ay"), "yil": ("year", "Yıl")}
ZAMAN_BOYUTLARI = {boyut for boyut, _ in ZAMAN.values()}
GA4_EARLIEST = date(2015, 8, 14)  # GA4 Data API bu tarihten önceki aralığı kabul etmez
JOB_KEYS = {"ad", "aciklama", "etkin", "property_ref", "metrikler", "boyutlar", "zaman", "filtreler", "tarih", "limit",
            "kayit_yoksa_sifir", "sheets", "sablon", "paket"}
# Paket: işin hangi hazır rapor grubundan (kütüphane) ya da GA4 taramasından geldiği; eşitleme bununla yapılır
PAKET_KIMLIK = re.compile(r"^(kutuphane|tarama):[a-z0-9][a-z0-9/-]{0,79}$")
PAKET_DURUMLARI = ("kutuphanede_yok", "taramada_yok")
# Dışa aktarma sütun şablonu: satır düzeni başına görünen sütunların sırası ve gizlenen sütunlar
SABLON_DUZENLER = ("tablo", "liste")
SABIT_SUTUNLAR = {"is", "kaynak", "donem", "satir", "kirilim", "metrik", "deger", "durum", "uyarilar", "hata"}
# Google Sheets hedefi: tablo kimliği (bağlantıdaki /d/<kimlik>/), sekme adı, her yazımda yeniden çekilecek son gün sayısı
SHEETS_KEYS = {"tablo", "sekme", "guncelle_gun", "toplam"}
TABLO_ID = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
TABLO_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,100})")
SEKME_YASAK = set("[]*?:/\\")
FILTER_KEYS = {"alan", "operator", "deger", "degerler", "harf_duyarli", "haric"}


class JobError(ValueError):
    """İş tanımı geçersiz; çalıştırılmadan durulur."""


_I_HARFLERI = str.maketrans("İIı", "iii")


def ad_anahtari(ad: str) -> str:
    """İş adı karşılaştırması: büyük/küçük harf ve Türkçe I/İ/ı farkı yok sayılır ("İkinci" = "ikinci")."""
    return str(ad).strip().translate(_I_HARFLERI).casefold()


def _text(value, where: str, *, max_len: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JobError(f"{where} boş olamaz.")
    value = value.strip()
    if len(value) > max_len:
        raise JobError(f"{where} en çok {max_len} karakter olabilir.")
    return value


def _bool(raw: dict, key: str, where: str, default: bool = False) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise JobError(f"{where}: '{key}' true veya false olmalı.")
    return value


def _keys(raw, allowed: set, where: str):
    if not isinstance(raw, dict):
        raise JobError(f"{where} bir nesne ({{...}}) olmalı.")
    extra = sorted(set(raw) - allowed)
    if extra:
        raise JobError(f"{where}: tanınmayan alan: {', '.join(extra)}. İzinli: {', '.join(sorted(allowed))}.")


def _api_name(value, where: str) -> str:
    name = _text(value, where, max_len=100)
    if not API_NAME.match(name):
        raise JobError(f"{where}: '{name}' geçerli bir GA4 API adı değil (ör. activeUsers, customEvent:kategori).")
    return name


def _names(raw, where: str, *, minimum: int, maximum: int) -> list[str]:
    if not isinstance(raw, list):
        raise JobError(f"{where} liste olmalı.")
    names = [_api_name(item, where) for item in raw]
    if len(set(names)) != len(names):
        raise JobError(f"{where}: aynı ad iki kez yazılmış.")
    if not minimum <= len(names) <= maximum:
        raise JobError(f"{where}: {minimum}–{maximum} ad olmalı.")
    return names


def _int(value, where: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise JobError(f"{where} {low}–{high} arası tam sayı olmalı.")
    return value


def _day(value, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise JobError(f"{where} YYYY-AA-GG biçiminde olmalı (ör. 2026-10-01).") from None


def normalize_period(raw, where: str, *, today: date | None = None) -> dict:
    _keys(raw, {"tur", "n", "baslangic", "bitis"}, where)
    kind = raw.get("tur")
    if kind not in PERIODS:
        raise JobError(f"{where}: 'tur' şunlardan biri olmalı: {', '.join(PERIODS)}.")
    allowed = {"tur"} | ({"n"} if kind in N_LIMITS else {"baslangic", "bitis"} if kind == "aralik" else set())
    extra = sorted(set(raw) - allowed)
    if extra:
        raise JobError(f"{where}: '{kind}' için kullanılmayan alan: {', '.join(extra)}.")
    if kind in N_LIMITS:
        return {"tur": kind, "n": _int(raw.get("n"), f"{where}: 'n'", 1, N_LIMITS[kind])}
    if kind == "aralik":
        start, end = _day(raw.get("baslangic"), f"{where} baslangic"), _day(raw.get("bitis"), f"{where} bitis")
        if start > end:
            raise JobError(f"{where}: başlangıç bitişten sonra olamaz.")
        if start < GA4_EARLIEST:
            raise JobError(f"{where}: GA4 {GA4_EARLIEST.isoformat()} öncesini sorgulamaz.")
        if end > (today or date.today()):
            raise JobError(f"{where}: bitiş tarihi gelecekte olamaz.")
        return {"tur": kind, "baslangic": start.isoformat(), "bitis": end.isoformat()}
    return {"tur": kind}


def tablo_kimligi(metin) -> str:
    """Google Sheets bağlantısından ya da yalın kimlikten tablo kimliğini çıkarır."""
    metin = str(metin or "").strip()
    bulunan = TABLO_URL.search(metin)
    kimlik = bulunan.group(1) if bulunan else metin
    if not TABLO_ID.match(kimlik):
        raise JobError("Google Sheets tablosu tanınmadı: tablonun bağlantısını (docs.google.com/spreadsheets/d/…) ya da kimliğini yapıştır.")
    return kimlik


def normalize_sheets(raw, where: str) -> dict:
    """İşin Sheets ayarı. Yazılmayan alan projenin ayarından gelir: tablo → projenin tablosu, sekme → iş adı,
    guncelle_gun / toplam → projenin varsayılanı. Yalnız yazılan alanlar saklanır."""
    _keys(raw, SHEETS_KEYS, f"{where} sheets")
    out = {}
    if raw.get("tablo") not in (None, ""):
        out["tablo"] = tablo_kimligi(raw["tablo"])
    if raw.get("sekme") not in (None, ""):
        sekme = _text(raw["sekme"], f"{where} sheets sekme", max_len=100)
        if set(sekme) & SEKME_YASAK or sekme.startswith("'"):
            raise JobError(f"{where}: sekme adında şu karakterler olamaz: [ ] * ? : / \\ ve baştaki '.")
        out["sekme"] = sekme
    if raw.get("guncelle_gun") is not None:
        out["guncelle_gun"] = _int(raw["guncelle_gun"], f"{where}: sheets 'guncelle_gun'", 0, 31)
    if raw.get("toplam") is not None:
        out["toplam"] = _bool(raw, "toplam", f"{where} sheets")
    return out


def normalize_paket(raw, where: str) -> dict:
    _keys(raw, {"kimlik", "rapor", "durum"}, f"{where} paket")
    kimlik = raw.get("kimlik")
    if not isinstance(kimlik, str) or not PAKET_KIMLIK.match(kimlik):
        raise JobError(f"{where}: paket kimliği geçersiz.")
    out = {"kimlik": kimlik, "rapor": _text(raw.get("rapor"), f"{where} paket rapor", max_len=200)}
    if raw.get("durum") is not None:
        if raw["durum"] not in PAKET_DURUMLARI:
            raise JobError(f"{where}: paket durumu şunlardan biri olmalı: {', '.join(PAKET_DURUMLARI)}.")
        out["durum"] = raw["durum"]
    return out


def normalize_sablon(raw, where: str) -> dict:
    """{"tablo": {"sutunlar": [...], "gizli": [...]}, "liste": {...}}. Sütun = sabit sütun adı ya da GA4 API adı."""
    _keys(raw, set(SABLON_DUZENLER), f"{where} sablon")
    out = {}
    for duzen, ayar in raw.items():
        _keys(ayar, {"sutunlar", "gizli"}, f"{where} sablon {duzen}")
        listeler = {}
        for alan in ("sutunlar", "gizli"):
            liste = ayar.get(alan, [])
            if not isinstance(liste, list) or len(liste) > 60:
                raise JobError(f"{where}: sablon {duzen} '{alan}' en çok 60 öğelik liste olmalı.")
            for ad in liste:
                if not isinstance(ad, str) or not (ad in SABIT_SUTUNLAR or API_NAME.match(ad)):
                    raise JobError(f"{where}: sablon {duzen} '{alan}' içinde geçersiz sütun: {ad!r}.")
            if len(set(liste)) != len(liste):
                raise JobError(f"{where}: sablon {duzen} '{alan}' içinde aynı sütun iki kez var.")
            listeler[alan] = list(liste)
        if set(listeler["sutunlar"]) & set(listeler["gizli"]):
            raise JobError(f"{where}: sablon {duzen}: bir sütun hem görünür hem gizli olamaz.")
        if not listeler["sutunlar"] and not listeler["gizli"]:
            continue
        out[duzen] = listeler
    return out


def normalize_filter(raw, where: str) -> dict:
    _keys(raw, FILTER_KEYS, where)
    operators = STRING_OPERATORS + (LIST_OPERATOR,)
    op = raw.get("operator")
    if op not in operators:
        raise JobError(f"{where}: 'operator' şunlardan biri olmalı: {', '.join(operators)}.")
    item = {"alan": _api_name(raw.get("alan"), f"{where} alan"), "operator": op,
            "harf_duyarli": _bool(raw, "harf_duyarli", where), "haric": _bool(raw, "haric", where)}
    if op == LIST_OPERATOR:
        if "deger" in raw:
            raise JobError(f"{where}: IN_LIST için 'deger' değil 'degerler' listesi kullan.")
        values = raw.get("degerler")
        if not isinstance(values, list) or not values:
            raise JobError(f"{where}: 'degerler' en az bir değer içeren liste olmalı.")
        values = [_text(v, f"{where} degerler", max_len=500) for v in values]
        if len(set(values)) != len(values):
            raise JobError(f"{where}: 'degerler' içinde aynı değer iki kez var.")
        item["degerler"] = values
    else:
        if "degerler" in raw:
            raise JobError(f"{where}: {op} tek değer alır; 'deger' kullan. Birden çok değer için IN_LIST.")
        item["deger"] = _text(raw.get("deger"), f"{where} deger", max_len=500)
    return item


def normalize_job(raw, *, today: date | None = None) -> dict:
    _keys(raw, JOB_KEYS, "İş")
    name = _text(raw.get("ad"), "İş adı", max_len=100)
    where = f"'{name}'"
    ref = raw.get("property_ref")
    if not isinstance(ref, str) or not PROPERTY_REF.match(ref):
        raise JobError(f"{where}: property_ref büyük harf, rakam ve alt çizgiden oluşan takma ad olmalı (ör. PROP_PROD). "
                       "Gerçek property kimliği iş dosyasına yazılmaz.")
    filters = raw.get("filtreler", [])
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise JobError(f"{where}: 'filtreler' en çok {MAX_FILTERS} öğelik liste olmalı.")
    if "tarih" not in raw:
        raise JobError(f"{where}: 'tarih' zorunlu (ör. {{\"tur\": \"dun\"}}).")
    job = {
        "ad": name,
        "etkin": _bool(raw, "etkin", where, True),
        "property_ref": ref,
        "metrikler": _names(raw.get("metrikler"), f"{where} metrikler", minimum=1, maximum=MAX_METRICS),
        "boyutlar": _names(raw.get("boyutlar", []), f"{where} boyutlar", minimum=0,
                           maximum=MAX_DIMENSIONS - (1 if raw.get("zaman") else 0)),
        "filtreler": [normalize_filter(f, f"{where} filtre {i}") for i, f in enumerate(filters, 1)],
        "tarih": normalize_period(raw["tarih"], f"{where} tarih", today=today),
        "limit": _int(raw.get("limit", DEFAULT_LIMIT), f"{where}: 'limit'", 1, MAX_LIMIT),
        "kayit_yoksa_sifir": _bool(raw, "kayit_yoksa_sifir", where),
    }
    zaman = raw.get("zaman")
    if zaman is not None:
        if zaman not in ZAMAN:
            raise JobError(f"{where}: 'zaman' şunlardan biri olmalı: {', '.join(ZAMAN)} (yoksa alanı hiç yazma).")
        if ZAMAN[zaman][0] in job["boyutlar"]:
            raise JobError(f"{where}: '{ZAMAN[zaman][0]}' hem zaman kırılımı hem boyut olarak seçilmiş; boyutlardan kaldır.")
        job["zaman"] = zaman
    if job["kayit_yoksa_sifir"] and (job["boyutlar"] or zaman):
        raise JobError(f"{where}: kayit_yoksa_sifir yalnız boyutsuz ve zaman kırılımsız (tek satırlık toplam) işlerde kullanılabilir.")
    if "aciklama" in raw:
        job["aciklama"] = _text(raw["aciklama"], f"{where} aciklama", max_len=500)
    if raw.get("sheets") is not None:
        job["sheets"] = normalize_sheets(raw["sheets"], where)
    if raw.get("sablon"):
        sablon = normalize_sablon(raw["sablon"], where)
        if sablon:
            job["sablon"] = sablon
    if raw.get("paket") is not None:
        job["paket"] = normalize_paket(raw["paket"], where)
    return job


def load_jobs(path, *, today: date | None = None) -> list[dict]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise JobError(f"İş dosyası bulunamadı: {path}") from None
    except json.JSONDecodeError as exc:
        raise JobError(f"İş dosyası JSON değil (satır {exc.lineno}, sütun {exc.colno}): {path}") from None
    except OSError as exc:
        raise JobError(f"İş dosyası okunamadı ({type(exc).__name__}): {path}") from None
    _keys(raw, {"isler", "aciklama"}, "İş dosyası")
    items = raw.get("isler")
    if not isinstance(items, list) or not items:
        raise JobError("İş dosyasında 'isler' listesi boş veya yok.")
    jobs = [normalize_job(item, today=today) for item in items]
    seen = set()
    for job in jobs:
        key = ad_anahtari(job["ad"])
        if key in seen:
            raise JobError(f"'{job['ad']}' adı iki kez kullanılmış; her işe farklı ad ver.")
        seen.add(key)
    return jobs


def select(jobs: list[dict], name: str | None = None) -> list[dict]:
    """Ad verilmezse etkin işler; verilirse (etkin olmasa da) yalnız o iş."""
    if name is None:
        chosen = [job for job in jobs if job["etkin"]]
        if not chosen:
            raise JobError("Etkin iş yok.")
        return chosen
    chosen = [job for job in jobs if ad_anahtari(job["ad"]) == ad_anahtari(name)]
    if not chosen:
        raise JobError(f"'{name}' adlı iş yok. Mevcut işler: {', '.join(job['ad'] for job in jobs)}.")
    return chosen


def filter_text(item: dict) -> str:
    value = "[" + ", ".join(item["degerler"]) + "]" if item["operator"] == LIST_OPERATOR else item["deger"]
    text = f"{item['alan']} {item['operator']} {value}"
    if item["harf_duyarli"]:
        text += " (harf duyarlı)"
    return f"DEĞİL ({text})" if item["haric"] else text


def period_text(period: dict) -> str:
    birim = {"son_n_gun": "gün", "son_n_hafta": "hafta", "son_n_ay": "ay", "son_n_yil": "yıl"}.get(period["tur"])
    if birim:
        return f"Son {period['n']} tam {birim}"
    if period["tur"] == "aralik":
        return f"{period['baslangic']} – {period['bitis']}"
    return PERIODS[period["tur"]]


def describe(job: dict) -> str:
    parts = [", ".join(job["metrikler"])]
    if job.get("zaman"):
        parts.append({"gun": "gün gün", "hafta": "hafta hafta", "ay": "ay ay", "yil": "yıl yıl"}[job["zaman"]])
    if job["boyutlar"]:
        parts.append("kırılım: " + ", ".join(job["boyutlar"]))
    if job["filtreler"]:
        parts.append("filtre: " + " VE ".join(filter_text(f) for f in job["filtreler"]))
    parts.append(period_text(job["tarih"]))
    return " · ".join(parts)
