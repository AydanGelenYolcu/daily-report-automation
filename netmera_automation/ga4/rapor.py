"""İş tanımını GA4 runReport isteğine çevirir, yanıtı doğrular ve sonuca dönüştürür.

Göreli tarihler (yesterday, NdaysAgo) GA4 tarafında property'nin saat diliminde çözülür. Sonuçtaki
`tarih_cozulen` çalışma bilgisayarının takvim gününe göredir; saat dilimi yanıtta ayrıca saklanır.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import uuid

from .isler import ZAMAN
from .istemci import ClientError

WARNINGS = {
    "esik": "GA4 eşik uyguladı: küçük değerler gizlenmiş olabilir (Google signals / demografi).",
    "diger_satiri": "(other) satırı var: çok fazla farklı değer olduğu için bazıları birleştirildi.",
    "ornekleme": "GA4 sonucu örnekleme ile hesapladı; panelle küçük fark olabilir.",
    "satir_kesildi": "Satır sayısı limiti aştı; yalnız ilk satırlar alındı. İşte 'limit' değerini artır.",
    "kota_az": "Property'nin günlük GA4 kotasının %10'undan azı kaldı.",
    "sifir_yazildi": "GA4 satır döndürmedi; iş ayarı gereği 0 yazıldı.",
}


def _ay_kaydir(gun: date, ay: int) -> date:
    """Ayın 1'ini `ay` kadar geri/ileri taşır."""
    toplam = gun.year * 12 + gun.month - 1 + ay
    return date(toplam // 12, toplam % 12 + 1, 1)


def date_range(period: dict, today: date | None = None) -> dict:
    """Gün türleri GA4'ün göreli tarihleriyle (mülk saat dilimi), hafta/ay/yıl türleri bilgisayarın takvimine göre
    hesaplanan kesin tarihlerle gönderilir (GA4'te 'geçen ay' gibi göreli tarih yok)."""
    kind = period["tur"]
    if kind == "dun":
        return {"startDate": "yesterday", "endDate": "yesterday"}
    if kind == "bugun":
        return {"startDate": "today", "endDate": "today"}
    if kind == "son_n_gun":
        return {"startDate": f"{period['n']}daysAgo", "endDate": "yesterday"}
    if kind == "aralik":
        return {"startDate": period["baslangic"], "endDate": period["bitis"]}
    start, end = resolve_dates(period, today or date.today())
    return {"startDate": start.isoformat(), "endDate": end.isoformat()}


def resolve_dates(period: dict, today: date) -> tuple[date, date]:
    kind = period["tur"]
    dun = today - timedelta(1)
    if kind == "dun":
        return dun, dun
    if kind == "bugun":
        return today, today
    if kind == "son_n_gun":
        return today - timedelta(period["n"]), dun
    if kind == "son_n_hafta":  # ISO hafta: pazartesi başlar; bu hafta hariç
        bu_pazartesi = today - timedelta(today.weekday())
        return bu_pazartesi - timedelta(weeks=period["n"]), bu_pazartesi - timedelta(1)
    if kind == "son_n_ay":
        bu_ay = today.replace(day=1)
        return _ay_kaydir(bu_ay, -period["n"]), bu_ay - timedelta(1)
    if kind == "son_n_yil":
        return date(today.year - period["n"], 1, 1), date(today.year - 1, 12, 31)
    # Ay/yıl başından: dünün ayı/yılı. Ayın 1'inde önceki ayın tamamı olur (aralık hiç boş kalmaz).
    if kind == "ay_basindan":
        return dun.replace(day=1), dun
    if kind == "yil_basindan":
        return date(dun.year, 1, 1), dun
    return date.fromisoformat(period["baslangic"]), date.fromisoformat(period["bitis"])


def boyut_listesi(job: dict) -> list[str]:
    """İstekteki boyutlar: varsa zaman boyutu en başta, sonra işin kırılımları."""
    return ([ZAMAN[job["zaman"]][0]] if job.get("zaman") else []) + job["boyutlar"]


def _filter_expression(item: dict) -> dict:
    if item["operator"] == "IN_LIST":
        spec = {"inListFilter": {"values": item["degerler"], "caseSensitive": item["harf_duyarli"]}}
    else:
        spec = {"stringFilter": {"matchType": item["operator"], "value": item["deger"], "caseSensitive": item["harf_duyarli"]}}
    expression = {"filter": {"fieldName": item["alan"], **spec}}
    return {"notExpression": expression} if item["haric"] else expression


def dimension_filter(filters: list[dict]) -> dict | None:
    """Filtreler VE ile birleşir. Tek filtre gruba sarılmaz."""
    expressions = [_filter_expression(item) for item in filters]
    if not expressions:
        return None
    return expressions[0] if len(expressions) == 1 else {"andGroup": {"expressions": expressions}}


def build_request(job: dict, today: date | None = None) -> dict:
    body = {"dateRanges": [date_range(job["tarih"], today)], "metrics": [{"name": m} for m in job["metrikler"]],
            "limit": job["limit"], "returnPropertyQuota": True}
    boyutlar = boyut_listesi(job)
    if boyutlar:
        body["dimensions"] = [{"name": d} for d in boyutlar]
        # GA4 ekranı gibi: varsa zaman eskiden yeniye, sonra ilk metrik büyükten küçüğe (satır sınırı aşılırsa büyükler kalır)
        zaman_sirasi = [{"dimension": {"dimensionName": boyutlar[0]}}] if job.get("zaman") else []
        body["orderBys"] = zaman_sirasi + [{"metric": {"metricName": job["metrikler"][0]}, "desc": True}]
        # Ekrandaki "Toplam" satırı: GA4'ün kendi hesapladığı toplam. Tekil kullanıcıda satırların toplamı değildir.
        body["metricAggregations"] = ["TOTAL"]
    expression = dimension_filter(job["filtreler"])
    if expression:
        body["dimensionFilter"] = expression
    return body


def _number(value, kind: str | None):
    if not isinstance(value, str):
        raise ClientError("YANIT_GECERSIZ")
    try:
        number = int(value) if kind == "TYPE_INTEGER" else float(value)
    except ValueError:
        raise ClientError("YANIT_GECERSIZ") from None
    if isinstance(number, float) and not math.isfinite(number):
        raise ClientError("YANIT_GECERSIZ")
    return number


def parse_response(job: dict, response) -> tuple[list[dict], list[str], dict]:
    """Satırlar, uyarı kodları ve yanıt bilgisi. Sütunlar istenenle eşleşmezse sonuç kullanılmaz."""
    if not isinstance(response, dict):
        raise ClientError("YANIT_GECERSIZ")
    dims = [h.get("name") for h in response.get("dimensionHeaders") or []]
    metric_headers = response.get("metricHeaders") or []
    names = [h.get("name") for h in metric_headers]
    raw_rows = response.get("rows") or []
    if (raw_rows or dims or names) and (dims != boyut_listesi(job) or names != job["metrikler"]):
        raise ClientError("YANIT_GECERSIZ", "GA4 yanıtındaki sütunlar istenen metrik/boyutlarla eşleşmiyor; sonuç kullanılmadı.")
    types = [h.get("type") for h in metric_headers]
    rows = []
    for raw in raw_rows:
        values = [v.get("value") for v in raw.get("dimensionValues") or []]
        metrics = raw.get("metricValues") or []
        if len(values) != len(dims) or len(metrics) != len(names) or not all(isinstance(v, str) for v in values):
            raise ClientError("YANIT_GECERSIZ")
        rows.append({"boyutlar": dict(zip(dims, values)),
                     "metrikler": {n: _number(m.get("value"), t) for n, m, t in zip(names, metrics, types)}})

    meta = response.get("metadata") or {}
    warnings = []
    if meta.get("subjectToThresholding"):
        warnings.append("esik")
    if meta.get("dataLossFromOtherRow") or any("(other)" in row["boyutlar"].values() for row in rows):
        warnings.append("diger_satiri")
    if meta.get("samplingMetadatas"):
        warnings.append("ornekleme")
    try:
        if int(response.get("rowCount", len(rows))) > len(rows):
            warnings.append("satir_kesildi")
    except (TypeError, ValueError):
        raise ClientError("YANIT_GECERSIZ") from None
    daily = (response.get("propertyQuota") or {}).get("tokensPerDay") or {}
    used, left = daily.get("consumed"), daily.get("remaining")
    if isinstance(used, int) and isinstance(left, int) and left < (used + left) * 0.1:
        warnings.append("kota_az")
    toplam = None
    totals = response.get("totals") or []
    if totals:
        metrics = totals[0].get("metricValues") if isinstance(totals[0], dict) else None
        if not isinstance(metrics, list) or len(metrics) != len(names):
            raise ClientError("YANIT_GECERSIZ")
        toplam = {n: _number(m.get("value"), t) for n, m, t in zip(names, metrics, types)}
    info = {"saat_dilimi": meta.get("timeZone") if isinstance(meta.get("timeZone"), str) else None,
            "gunluk_kota_kalan": left if isinstance(left, int) else None, "toplam": toplam}
    return rows, warnings, info


def _gecersiz_aciklama(job: dict, client) -> str | None:
    """GA4 'geçersiz istek' dediğinde hangi adın bu mülkte olmadığını metadata listesinden bulur (yalnız hata anında)."""
    from . import meta  # meta → rapor içe aktarımı döngü yapmasın
    try:
        hatalar = meta.check_names(job, meta.fetch(client, job["property_ref"]))
    except Exception:
        return None
    if not hatalar:
        return None
    return ("Bu mülkte olmayan alan var — " + " ".join(h.replace(" Listeyi 'meta' komutuyla gör.", "") for h in hatalar)
            + " İşi açıp alanı listeden yeniden seç.")


def run_job(job: dict, client, *, today: date) -> dict:
    """Tek işi çalıştırır. Hata hâlinde satır üretilmez; sayı tahmin edilmez, 0 yazılmaz."""
    start, end = resolve_dates(job["tarih"], today)
    result = {"is": job["ad"], "property_ref": job["property_ref"], "metrikler": job["metrikler"], "boyutlar": boyut_listesi(job),
              "zaman": job.get("zaman"), "tarih_istegi": date_range(job["tarih"], today),
              "tarih_cozulen": [start.isoformat(), end.isoformat()],
              "durum": "hata", "satirlar": [], "toplam": None, "uyarilar": [], "hata": None, "hata_kodu": None,
              "saat_dilimi": None, "gunluk_kota_kalan": None}
    try:
        rows, warnings, info = parse_response(job, client.run_report(job["property_ref"], build_request(job, today)))
        if not rows:
            if not job["kayit_yoksa_sifir"]:
                raise ClientError("KAYIT_YOK")
            rows = [{"boyutlar": {}, "metrikler": {m: 0 for m in job["metrikler"]}}]
            warnings.append("sifir_yazildi")
        result.update(info, durum="uyari" if warnings else "tamam", satirlar=rows, uyarilar=warnings)
    except ClientError as exc:
        aciklama = _gecersiz_aciklama(job, client) if exc.code == "INVALID_ARGUMENT" else None
        result.update(hata=aciklama or str(exc), hata_kodu=exc.code)
    except Exception as exc:  # bir işin hatası diğerlerini durdurmasın
        result.update(hata=f"Beklenmeyen hata: {type(exc).__name__}", hata_kodu="BEKLENMEYEN")
    return result


def run_jobs(jobs: list[dict], client, *, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    results = [run_job(job, client, today=now.date()) for job in jobs]
    return {"calisma": uuid.uuid4().hex[:12], "zaman": now.isoformat(timespec="seconds"), "kaynak": client.kaynak,
            "sonuclar": results, **{key: sum(r["durum"] == key for r in results) for key in ("tamam", "uyari", "hata")}}
