"""Sentetik GA4 kaynağı: gerçek API ile aynı REST biçiminde, uydurma ama tekrarlanabilir sayılar.

Ağ bağlantısı yoktur. Bazı property_ref değerleri hata/uyarı senaryoları üretir (SCENARIOS, EMPTY...),
böylece hata yolları gerçek hesaba bağlanmadan denenir.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
from itertools import product
import re

from .istemci import ClientError

# ad: (GA4 türü, alt sınır, üst sınır) — günlük, kırılımsız değer aralığı
METRICS = {
    "activeUsers": ("TYPE_INTEGER", 800, 6000), "totalUsers": ("TYPE_INTEGER", 900, 7000),
    "newUsers": ("TYPE_INTEGER", 100, 2000), "sessions": ("TYPE_INTEGER", 1000, 9000),
    "engagedSessions": ("TYPE_INTEGER", 600, 6000), "screenPageViews": ("TYPE_INTEGER", 3000, 30000),
    "eventCount": ("TYPE_INTEGER", 8000, 90000), "keyEvents": ("TYPE_INTEGER", 10, 400),
    "transactions": ("TYPE_INTEGER", 5, 300), "engagementRate": ("TYPE_FLOAT", 0.3, 0.8),
    "averageSessionDuration": ("TYPE_SECONDS", 40, 400), "purchaseRevenue": ("TYPE_CURRENCY", 5000, 250000),
    # Hazır rapor kütüphanesinin alanları (hazir_raporlar.py)
    "totalRevenue": ("TYPE_CURRENCY", 6000, 300000), "userEngagementDuration": ("TYPE_SECONDS", 20000, 900000),
    "eventsPerSession": ("TYPE_FLOAT", 2, 12), "eventCountPerUser": ("TYPE_FLOAT", 4, 25),
    "screenPageViewsPerUser": ("TYPE_FLOAT", 1.5, 9), "sessionKeyEventRate": ("TYPE_FLOAT", 0.01, 0.2),
    "userKeyEventRate": ("TYPE_FLOAT", 0.01, 0.25), "itemsViewed": ("TYPE_INTEGER", 500, 9000),
    "itemsAddedToCart": ("TYPE_INTEGER", 50, 1500), "itemsPurchased": ("TYPE_INTEGER", 5, 400),
    "itemRevenue": ("TYPE_CURRENCY", 3000, 200000), "advertiserAdClicks": ("TYPE_INTEGER", 100, 5000),
    "advertiserAdCost": ("TYPE_CURRENCY", 500, 40000), "advertiserAdCostPerClick": ("TYPE_CURRENCY", 2, 15),
    "returnOnAdSpend": ("TYPE_FLOAT", 0.5, 9), "addToCarts": ("TYPE_INTEGER", 50, 2000),
    "ecommercePurchases": ("TYPE_INTEGER", 5, 300), "advertiserAdImpressions": ("TYPE_INTEGER", 5000, 200000),
    "advertiserAdCostPerKeyEvent": ("TYPE_CURRENCY", 5, 80), "itemsCheckedOut": ("TYPE_INTEGER", 20, 800),
    "itemsViewedInPromotion": ("TYPE_INTEGER", 200, 8000), "itemsClickedInPromotion": ("TYPE_INTEGER", 10, 600),
    "itemPromotionClickThroughRate": ("TYPE_FLOAT", 0.01, 0.15), "screenPageViewsPerSession": ("TYPE_FLOAT", 1.2, 6),
}
USER_METRICS = {"activeUsers", "totalUsers", "newUsers"}  # günler arasında toplanmaz
RATE_METRICS = {"engagementRate", "averageSessionDuration", "eventsPerSession", "eventCountPerUser", "screenPageViewsPerUser",
                "sessionKeyEventRate", "userKeyEventRate", "advertiserAdCostPerClick", "returnOnAdSpend", "advertiserAdCostPerKeyEvent",
                "itemPromotionClickThroughRate", "screenPageViewsPerSession"}  # kırılımla bölünmez
# Zaman boyutları istek aralığındaki günlerden üretilir: gün → dönem anahtarı
TIME = {"date": lambda d: d.strftime("%Y%m%d"), "isoYearIsoWeek": lambda d: f"{d.isocalendar()[0]}{d.isocalendar()[1]:02d}",
        "yearMonth": lambda d: d.strftime("%Y%m"), "year": lambda d: d.strftime("%Y")}
DIMENSIONS = {
    **{name: None for name in TIME},
    "deviceCategory": ["desktop", "mobile", "tablet"],
    "platform": ["web", "Android", "iOS"],
    "country": ["Turkey", "Germany", "Netherlands"],
    "eventName": ["page_view", "session_start", "purchase", "sentetik_olay"],
    "sessionDefaultChannelGroup": ["Organic Search", "Direct", "Paid Search", "Email"],
    "sessionSource": ["google", "(direct)", "sentetik-bulten"],
    "pagePath": ["/", "/etkinlik/sentetik-1", "/sepet"],
    "customEvent:sentetik_kategori": ["Konser", "Futbol", "Tiyatro"],
    # Hazır rapor kütüphanesinin alanları (uydurma değerler)
    "sessionPrimaryChannelGroup": ["Organic Search", "Direct", "Paid Search", "Email"],
    "firstUserPrimaryChannelGroup": ["Organic Search", "Direct", "Paid Social"],
    "sessionManualCampaignName": ["sentetik-ekim-bulteni", "sentetik-sosyal-kampanya", "(not set)"],
    "sessionManualSourceMedium": ["sentetik-bulten / email", "sentetik-sosyal / paid_social", "(not set)"],
    "sessionGoogleAdsCampaignName": ["Sentetik Marka", "Sentetik Genel"],
    "isKeyEvent": ["true", "false"],
    "unifiedScreenClass": ["Sentetik ana sayfa", "Sentetik etkinlik", "Sentetik sepet"],
    "unifiedScreenName": ["Sentetik ana sayfa", "Sentetik etkinlik"],
    "unifiedPagePathScreen": ["/", "/etkinlik/sentetik-1", "/sepet"],
    "pageTitle": ["Sentetik ana sayfa", "Sentetik etkinlik", "Sentetik sepet"],
    "landingPagePlusQueryString": ["/", "/etkinlik/sentetik-1?utm_source=sentetik", "/sepet"],
    "itemName": ["Sentetik bilet A", "Sentetik bilet B", "Sentetik bilet C"],
    "region": ["İstanbul", "Ankara", "Berlin"], "city": ["İstanbul", "Ankara", "Berlin"],
    "language": ["Turkish", "English", "German"], "userAgeBracket": ["18-24", "25-34", "35-44"],
    "userGender": ["female", "male", "unknown"], "browser": ["Chrome", "Safari", "Edge"],
    "operatingSystem": ["Android", "iOS", "Windows"], "screenResolution": ["1920x1080", "390x844"],
    "appVersion": ["1.0.0", "1.1.0"],
    "sessionCampaignName": ["sentetik-ekim-bulteni", "sentetik-sosyal-kampanya", "(direct)"],
    "month": ["09", "10"], "landingPage": ["/", "/etkinlik/sentetik-1", "/sepet"],
    "itemId": ["SENTETIK-1", "SENTETIK-2"], "itemPromotionName": ["Sentetik indirim", "Sentetik erken bilet"],
    "audienceName": ["Sentetik tüm kullanıcılar", "Sentetik alıcılar"],
}
INCOMPATIBLE = {("customEvent:sentetik_kategori", "sessions"), ("customEvent:sentetik_kategori", "engagedSessions")}
SCENARIOS = {"SENTETIK_YETKI": "PERMISSION_DENIED", "SENTETIK_KOTA": "RESOURCE_EXHAUSTED",
             "SENTETIK_API_KAPALI": "SERVICE_DISABLED"}
EMPTY, THRESHOLD, OTHER = "SENTETIK_BOS", "SENTETIK_ESIK", "SENTETIK_DIGER"
RELATIVE = re.compile(r"^(\d+)daysAgo$")


def _fraction(*parts) -> float:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / 0xFFFFFFFFFFFF


def _matches(expression: dict | None, row: dict) -> bool | None:
    """True/False; filtre alanı istekte boyut değilse None (sayı yalnız ölçeklenir)."""
    if not expression:
        return True
    if "andGroup" in expression:
        results = [_matches(e, row) for e in expression["andGroup"]["expressions"]]
        return False if False in results else None if None in results else True
    if "notExpression" in expression:
        inner = _matches(expression["notExpression"], row)
        return None if inner is None else not inner
    spec = expression["filter"]
    if spec["fieldName"] not in row:
        return None
    value = row[spec["fieldName"]]
    if "inListFilter" in spec:
        rule = spec["inListFilter"]
        values = rule["values"] if rule.get("caseSensitive") else [v.casefold() for v in rule["values"]]
        return (value if rule.get("caseSensitive") else value.casefold()) in values
    rule = spec["stringFilter"]
    target, kind, sensitive = rule["value"], rule.get("matchType", "EXACT"), rule.get("caseSensitive", False)
    if kind in ("FULL_REGEXP", "PARTIAL_REGEXP"):
        try:
            pattern = re.compile(target, 0 if sensitive else re.IGNORECASE)
        except re.error:
            raise ClientError("INVALID_ARGUMENT") from None
        return bool(pattern.fullmatch(value) if kind == "FULL_REGEXP" else pattern.search(value))
    a, b = (value, target) if sensitive else (value.casefold(), target.casefold())
    return {"EXACT": a == b, "CONTAINS": b in a, "BEGINS_WITH": a.startswith(b), "ENDS_WITH": a.endswith(b)}[kind]


class SyntheticClient:
    kaynak = "sentetik"

    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        self.requests: list[tuple[str, dict]] = []

    def _scenario(self, ref: str):
        if ref in SCENARIOS:
            raise ClientError(SCENARIOS[ref])

    def metadata(self, ref: str) -> dict:
        self._scenario(ref)
        return {"name": f"properties/{ref}/metadata",
                "dimensions": [{"apiName": n, "uiName": n, "customDefinition": n.startswith("customEvent:")} for n in DIMENSIONS],
                "metrics": [{"apiName": n, "uiName": n, "type": t} for n, (t, _, _) in METRICS.items()]}

    def check_compatibility(self, ref: str, body: dict) -> dict:
        self._scenario(ref)
        dims = [d["name"] for d in body.get("dimensions", [])]
        metrics = [m["name"] for m in body.get("metrics", [])]
        state = lambda bad: "INCOMPATIBLE" if bad else "COMPATIBLE"
        return {"dimensionCompatibilities": [{"dimensionMetadata": {"apiName": d}, "compatibility": state(any((d, m) in INCOMPATIBLE for m in metrics))} for d in dims],
                "metricCompatibilities": [{"metricMetadata": {"apiName": m}, "compatibility": state(any((d, m) in INCOMPATIBLE for d in dims))} for m in metrics]}

    def _day(self, text: str) -> date:
        if text == "today":
            return self.today
        if text == "yesterday":
            return self.today - timedelta(1)
        found = RELATIVE.match(text)
        if found:
            return self.today - timedelta(int(found.group(1)))
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ClientError("INVALID_ARGUMENT") from None

    def run_report(self, ref: str, body: dict) -> dict:
        self._scenario(ref)
        self.requests.append((ref, body))
        dims = [d["name"] for d in body.get("dimensions", [])]
        metrics = [m["name"] for m in body.get("metrics", [])]
        if any(d not in DIMENSIONS for d in dims) or any(m not in METRICS for m in metrics) or \
                any((d, m) in INCOMPATIBLE for d in dims for m in metrics):
            raise ClientError("INVALID_ARGUMENT")
        span = body["dateRanges"][0]
        start, end = self._day(span["startDate"]), self._day(span["endDate"])
        if start > end:
            raise ClientError("INVALID_ARGUMENT")
        days = [start + timedelta(i) for i in range((end - start).days + 1)]
        zaman = next((name for name in dims if name in TIME), None)
        donem_gun = {}  # dönem anahtarı → içindeki gün sayısı (aralıkla kesilen ilk/son dönem kısa olabilir)
        for d in days:
            if zaman:
                anahtar = TIME[zaman](d)
                donem_gun[anahtar] = donem_gun.get(anahtar, 0) + 1
        choices = [sorted({TIME[name](d) for d in days}) if name in TIME else DIMENSIONS[name] for name in dims]
        combos = list(product(*choices))
        split = max(1, len(combos) // (len(donem_gun) or 1))
        expression = body.get("dimensionFilter")
        rows = []
        for combo in ([] if ref == EMPTY else combos):
            row = dict(zip(dims, combo))
            verdict = _matches(expression, row)
            if verdict is False:
                continue
            scale = 1.0 if verdict else 0.2 + 0.6 * _fraction(ref, repr(expression))
            values = []
            for name in metrics:
                kind, low, high = METRICS[name]
                value = low + (high - low) * _fraction(ref, name, combo, start, end, repr(expression))
                if name not in RATE_METRICS:
                    span_days = donem_gun[row[zaman]] if zaman else len(days)
                    value *= scale * (span_days ** 0.7 if name in USER_METRICS else span_days) / split ** 0.5
                values.append({"value": str(round(value)) if kind == "TYPE_INTEGER" else f"{value:.4f}"})
            rows.append({"dimensionValues": [{"value": v} for v in combo], "metricValues": values})
        for o in reversed(body.get("orderBys") or []):  # GA4 gibi; kararlı sıralama ile çok anahtarlı
            if o.get("dimension", {}).get("dimensionName") in dims:
                i = dims.index(o["dimension"]["dimensionName"])
                rows.sort(key=lambda r, i=i: r["dimensionValues"][i]["value"], reverse=bool(o.get("desc")))
            elif o.get("metric", {}).get("metricName") in metrics:
                i = metrics.index(o["metric"]["metricName"])
                rows.sort(key=lambda r, i=i: float(r["metricValues"][i]["value"]), reverse=bool(o.get("desc")))
        totals = None
        if "TOTAL" in (body.get("metricAggregations") or []) and rows:
            totals = []
            for i, name in enumerate(metrics):
                kind = METRICS[name][0]
                values = [float(r["metricValues"][i]["value"]) for r in rows]
                if name in RATE_METRICS:
                    value = sum(values) / len(values)
                elif name in USER_METRICS and len(rows) > 1:
                    value = sum(values) * 0.97  # aynı kişi birden çok satırda: tekil toplam satırların toplamından küçük
                else:
                    value = sum(values)
                totals.append({"value": str(round(value)) if kind == "TYPE_INTEGER" else f"{value:.4f}"})
        if ref == OTHER and dims:
            rows.append({"dimensionValues": [{"value": "(other)"} for _ in dims],
                         "metricValues": [{"value": "1" if METRICS[m][0] == "TYPE_INTEGER" else "0.1000"} for m in metrics]})
        response = {"dimensionHeaders": [{"name": d} for d in dims],
                    "metricHeaders": [{"name": m, "type": METRICS[m][0]} for m in metrics],
                    "metadata": {"currencyCode": "TRY", "timeZone": "Europe/Istanbul"}, "kind": "analyticsData#runReport"}
        if rows:  # GA4 boş sonuçta rows/rowCount alanlarını göndermez
            response.update(rows=rows[:int(body.get("limit", 10000))], rowCount=len(rows))
        if totals:
            response["totals"] = [{"dimensionValues": [{"value": "RESERVED_TOTAL"} for _ in dims], "metricValues": totals}]
        if ref == THRESHOLD:
            response["metadata"]["subjectToThresholding"] = True
        if ref == OTHER:
            response["metadata"]["dataLossFromOtherRow"] = True
        if body.get("returnPropertyQuota"):
            used = 10 * len(self.requests)
            response["propertyQuota"] = {"tokensPerDay": {"consumed": used, "remaining": 200000 - used}}
        return response
