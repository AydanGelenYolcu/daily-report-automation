"""Property'nin metrik/boyut listesi (getMetadata) ve iş tanımının bu listeye göre doğrulanması.

Gerçek kaynakta liste 24 saat yerelde önbelleğe alınır (yalnız ad/tür; veri içermez).
"""
from __future__ import annotations

import difflib
import json
import time

from .istemci import ClientError
from .paths import data_root
from .rapor import boyut_listesi, dimension_filter

CACHE_SECONDS = 24 * 3600


def _shape(response) -> dict:
    """apiName → {ad (arayüz adı), kategori, ozel (özel tanım mı)}; metriklerde ayrıca tur (TYPE_INTEGER …)."""
    def alan(x) -> dict:
        return {"ad": x.get("uiName") or x["apiName"], "kategori": x.get("category") or "",
                "ozel": bool(x.get("customDefinition"))}
    try:
        return {"boyutlar": {d["apiName"]: alan(d) for d in response["dimensions"]},
                "metrikler": {m["apiName"]: {**alan(m), "tur": m.get("type")} for m in response["metrics"]}}
    except (KeyError, TypeError):
        raise ClientError("YANIT_GECERSIZ") from None


def gecerli_mi(meta) -> bool:
    """Önbellekteki eski biçim (boyut değeri düz metin) yeniden alınsın."""
    try:
        return all(isinstance(v, dict) for v in meta["boyutlar"].values())
    except (KeyError, TypeError, AttributeError):
        return False


def fetch(client, ref: str, *, refresh: bool = False, now: float | None = None) -> dict:
    if client.kaynak != "ga4":
        return _shape(client.metadata(ref))
    now = time.time() if now is None else now
    path = data_root() / "meta" / f"{ref}.json"
    if not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if 0 <= now - cached["alindi"] < CACHE_SECONDS and gecerli_mi(cached["meta"]):
                return cached["meta"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    meta = _shape(client.metadata(ref))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"alindi": now, "meta": meta}, ensure_ascii=False), encoding="utf-8")
    return meta


def _unknown(kind: str, name: str, names) -> str:
    close = difflib.get_close_matches(name, list(names), n=3, cutoff=0.6)
    hint = f" Benzer: {', '.join(close)}." if close else " Listeyi 'meta' komutuyla gör."
    return f"Bilinmeyen {kind}: {name}.{hint}"


def check_names(job: dict, meta: dict) -> list[str]:
    errors = [_unknown("metrik", m, meta["metrikler"]) for m in job["metrikler"] if m not in meta["metrikler"]]
    errors += [_unknown("boyut", d, meta["boyutlar"]) for d in job["boyutlar"] if d not in meta["boyutlar"]]
    for item in job["filtreler"]:
        if item["alan"] in meta["metrikler"]:
            errors.append(f"'{item['alan']}' bir metrik; filtre şimdilik yalnız boyut üzerinde kurulur.")
        elif item["alan"] not in meta["boyutlar"]:
            errors.append(_unknown("filtre alanı", item["alan"], meta["boyutlar"]))
    return errors


def check_compatibility(job: dict, client) -> list[str]:
    body = {"metrics": [{"name": m} for m in job["metrikler"]], "dimensions": [{"name": d} for d in boyut_listesi(job)]}
    expression = dimension_filter(job["filtreler"])
    if expression:
        body["dimensionFilter"] = expression
    response = client.check_compatibility(job["property_ref"], body)
    errors = []
    for item in response.get("dimensionCompatibilities") or []:
        if item.get("compatibility") == "INCOMPATIBLE":
            errors.append(f"Boyut '{(item.get('dimensionMetadata') or {}).get('apiName')}' bu metriklerle birlikte kullanılamaz.")
    for item in response.get("metricCompatibilities") or []:
        if item.get("compatibility") == "INCOMPATIBLE":
            errors.append(f"Metrik '{(item.get('metricMetadata') or {}).get('apiName')}' bu boyutlarla birlikte kullanılamaz.")
    return errors


def validate(job: dict, client, meta: dict) -> list[str]:
    return check_names(job, meta) or check_compatibility(job, client)
