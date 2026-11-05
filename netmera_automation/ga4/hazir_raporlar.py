"""Hazır rapor kütüphanesi: kullanıcının GA4 mülkündeki Raporlar menüsünü (koleksiyon › konu › rapor) izleyen iş tanımları.

EVIDENCE — 2026-10-26: Yapı kullanıcının kendi GA4'ünden (Claude in Chrome ile, yalnız menü ve sütun başlıkları; sayı,
hesap/mülk adı ve kimlik alınmadı) okundu: koleksiyonlar "Kullanıcı" (Kullanıcı özellikleri, Teknoloji) ve "Life cycle"
(Edinme, Etkileşim, Para Kazanma, Elde Tutma). Sütunlar o raporların ekrandaki sütunlarıdır; GA4 Data API'de karşılığı
olmayanlar alınmadı ve rapor açıklamasında yazar (şema: https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema):
  "Geri gelen kullanıcılar", "Ortalama … etkileşim süresi", "Etkin kullanıcı başına etkileşimli oturum sayısı",
  potansiyel müşteri (lead) metrikleri, "Ortalama ürün geliri". Bir sorgu en çok 10 metrik alır.
Ekran adı → API adı eşlemesinin bir kısmı INFERENCE'tır (adlar.py'de işaretli). Kullanıcının kendi oluşturduğu raporlar
("test") kütüphaneye alınmadı. Huni (yolculuk), kohort (elde tutma) ve lead raporları basit sorguyla üretilemez;
İşlemler raporu işlem (sipariş) düzeyidir, kayıt düzeyine yakın olduğu için kapsam dışı. Bunlar listelenir, çekilemez.

Paket = bir konu (ör. Life cycle › Edinme). Kütüphaneye rapor eklenince çekilmiş paketlere kendiliğinden eklenir
(paketler.esitle). Bir raporun kimliği değişmemeli: değişirse eski iş "kütüphanede yok" diye işaretlenir.
"""
from __future__ import annotations

SURUM = 2
VARSAYILAN_TARIH = {"tur": "son_n_gun", "n": 28}  # GA4 raporlarının varsayılanı: son 28 gün

_KULLANICI_ORTAK = ["activeUsers", "newUsers", "engagedSessions", "engagementRate", "eventCount", "keyEvents", "userKeyEventRate", "totalRevenue"]
_SURE_YOK = "GA4'teki ortalama etkileşim süresi sütunlarının API karşılığı yok; alınmadı."

KUTUPHANE = [
    {"kimlik": "yasam-dongusu/edinme", "koleksiyon": "Life cycle", "ad": "Edinme", "raporlar": [
        {"kimlik": "kullanici-edinme", "ad": "Kullanıcı edinme", "boyutlar": ["firstUserPrimaryChannelGroup"],
         "metrikler": ["totalUsers", "newUsers", "activeUsers", "eventCount", "keyEvents", "userKeyEventRate"],
         "aciklama": f"Yeni kullanıcıların ilk geldiği kanal. \"Geri gelen kullanıcılar\" ve etkin kullanıcı başına etkileşimli oturum API'de yok. {_SURE_YOK}"},
        {"kimlik": "trafik-edinme", "ad": "Trafik edinme", "boyutlar": ["sessionPrimaryChannelGroup"],
         "metrikler": ["screenPageViews", "sessions", "engagementRate", "addToCarts", "ecommercePurchases", "totalRevenue", "eventsPerSession"],
         "aciklama": f"Oturumların geldiği kanal. {_SURE_YOK}"},
        {"kimlik": "google-disi-kampanyalar", "ad": "Google dışı kampanya", "boyutlar": ["sessionCampaignName"],
         "metrikler": ["activeUsers", "sessions", "engagedSessions", "advertiserAdClicks", "advertiserAdCost", "advertiserAdImpressions",
                       "keyEvents", "advertiserAdCostPerKeyEvent", "totalRevenue", "returnOnAdSpend"],
         "aciklama": "Oturum kampanyasına göre. GA4'teki \"Tıklama başına reklam maliyeti\" 10 metrik sınırı yüzünden alınmadı (maliyet / tıklama). Maliyet sütunları yalnız reklam maliyeti GA4'e geliyorsa dolar."},
        {"kimlik": "kullanici-edinme-kohortlari", "ad": "Kullanıcı edinme kohortları", "boyutlar": ["firstUserPrimaryChannelGroup"],
         "metrikler": ["newUsers", "totalRevenue", "transactions"],
         "aciklama": "İlk kullanıcı kanalına göre yeni kullanıcı, gelir, işlem. GA4'teki kohort zaman ekseni (edinmeden sonraki günler) bu sorguda yok."},
        {"kimlik": "potansiyel-musteri-edinme", "ad": "Potansiyel müşteri edinme", "boyutlar": [], "metrikler": [], "desteklenmiyor":
         "Yeni / uygun / dönüşümü gerçekleşen potansiyel müşteri metriklerinin GA4 Data API'de karşılığı yok."},
    ]},
    {"kimlik": "yasam-dongusu/etkilesim", "koleksiyon": "Life cycle", "ad": "Etkileşim", "raporlar": [
        {"kimlik": "etkinlikler", "ad": "Etkinlikler", "boyutlar": ["eventName"],
         "metrikler": ["eventCount", "totalUsers", "eventCountPerUser", "totalRevenue"], "aciklama": "Etkinlik adına göre."},
        {"kimlik": "sayfalar-ve-ekranlar", "ad": "Sayfalar ve ekranlar", "boyutlar": ["operatingSystem"], "zaman": "ay",
         "secenekler": ["operatingSystem", "unifiedPagePathScreen", "unifiedScreenClass", "pagePath", "pageTitle"],
         "metrikler": ["screenPageViews", "activeUsers", "screenPageViewsPerUser", "eventCount", "keyEvents", "totalRevenue", "sessions"],
         "aciklama": f"Senin GA4'ünde bu rapor Ay × İşletim sistemi gösteriyor; burada ay ay (yıl ile) × işletim sistemi. Ana kırılım sayfa yoluna çevrilebilir. {_SURE_YOK}"},
        {"kimlik": "acilis-sayfasi", "ad": "Açılış sayfası", "boyutlar": ["landingPage"],
         "metrikler": ["sessions", "activeUsers", "newUsers", "keyEvents", "totalRevenue", "sessionKeyEventRate"],
         "aciklama": f"Oturumun başladığı sayfa. {_SURE_YOK}"},
    ]},
    {"kimlik": "yasam-dongusu/para-kazanma", "koleksiyon": "Life cycle", "ad": "Para Kazanma", "raporlar": [
        {"kimlik": "e-ticaret-satin-alma", "ad": "E-ticaret satın alma işlemleri", "boyutlar": ["itemName"],
         "metrikler": ["itemsViewed", "itemsAddedToCart", "itemsPurchased", "itemRevenue"], "aciklama": "Ürün adına göre."},
        {"kimlik": "uygulama-ici-satin-alma", "ad": "Uygulama içi satın alma işlemleri", "boyutlar": ["itemId"],
         "metrikler": ["itemsPurchased", "itemRevenue"],
         "aciklama": "Ürün kimliğine göre miktar ve ürün geliri. \"Ortalama ürün geliri\" API'de yok. GA4 bu raporu uygulama akışlarıyla sınırlar; burada bütün akışlar (gerekirse Platform filtresi ekle)."},
        {"kimlik": "tanitimlar", "ad": "Tanıtımlar", "boyutlar": ["itemPromotionName"],
         "metrikler": ["itemsViewedInPromotion", "itemsClickedInPromotion", "itemPromotionClickThroughRate", "itemsAddedToCart",
                       "itemsCheckedOut", "itemsPurchased", "itemRevenue"],
         "aciklama": "Öğe promosyonu adına göre."},
        {"kimlik": "satin-alma-yolculugu", "ad": "Satın alma yolculuğu", "boyutlar": [], "metrikler": [], "desteklenmiyor":
         "Huni raporu (adım adım kullanıcı kaybı); basit sorguyla üretilemez."},
        {"kimlik": "odeme-yolculugu", "ad": "Ödeme yolculuğu", "boyutlar": [], "metrikler": [], "desteklenmiyor":
         "Huni raporu (adım adım kullanıcı kaybı); basit sorguyla üretilemez."},
        {"kimlik": "islemler", "ad": "İşlemler", "boyutlar": [], "metrikler": [], "desteklenmiyor":
         "İşlem numarası düzeyinde (sipariş başına) rapor; kayıt düzeyine yakın olduğu için bu uygulamanın kapsamı dışında."},
    ]},
    {"kimlik": "yasam-dongusu/elde-tutma", "koleksiyon": "Life cycle", "ad": "Elde Tutma", "raporlar": [
        {"kimlik": "elde-tutma", "ad": "Elde tutma", "boyutlar": [], "metrikler": [], "desteklenmiyor":
         "Kohort analizi (kullanıcıların sonraki günlerde geri gelmesi) gerektirir; bu uygulamanın basit sorgusuyla birebir üretilemez."},
    ]},
    {"kimlik": "kullanici/demografi", "koleksiyon": "Kullanıcı", "ad": "Kullanıcı özellikleri", "raporlar": [
        {"kimlik": "demografik-ayrintilar", "ad": "Demografik grup ayrıntıları", "boyutlar": ["country"],
         "secenekler": ["country", "region", "city", "language", "userAgeBracket", "userGender"],
         "metrikler": _KULLANICI_ORTAK, "aciklama": f"Varsayılan: ülke. Yaş ve cinsiyet yalnız Google sinyalleri açıksa; eşik uygulanabilir. {_SURE_YOK}"},
        {"kimlik": "kitleler", "ad": "Kitleler", "boyutlar": ["audienceName"],
         "metrikler": ["totalUsers", "newUsers", "sessions", "screenPageViewsPerSession", "averageSessionDuration", "totalRevenue"],
         "aciklama": "Kitle adına göre. Bir kullanıcı birden çok kitlede olabilir; satırlar toplanmaz."},
    ]},
    {"kimlik": "kullanici/teknoloji", "koleksiyon": "Kullanıcı", "ad": "Teknoloji", "raporlar": [
        {"kimlik": "teknoloji-ayrintilari", "ad": "Teknoloji ayrıntıları", "boyutlar": ["browser"],
         "secenekler": ["browser", "deviceCategory", "platform", "operatingSystem", "screenResolution", "appVersion"],
         "metrikler": [m for m in _KULLANICI_ORTAK if m != "userKeyEventRate"], "aciklama": f"Varsayılan: tarayıcı. {_SURE_YOK}"},
    ]},
]


def paket(kimlik: str) -> dict | None:
    return next((p for p in KUTUPHANE if p["kimlik"] == kimlik), None)


def paket_adi(p: dict) -> str:
    return f"{p['koleksiyon']} › {p['ad']}"


def butun_alanlar() -> tuple[set[str], set[str]]:
    """Kütüphanenin kullandığı bütün (boyut, metrik) adları — sentetik kaynak ve testler için."""
    boyutlar, metrikler = set(), set()
    for p in KUTUPHANE:
        for r in p["raporlar"]:
            boyutlar.update(r.get("secenekler") or [], r["boyutlar"])
            boyutlar.update(f["alan"] for f in r.get("filtreler", []))
            metrikler.update(r["metrikler"])
    return boyutlar, metrikler


def is_tanimi(p: dict, r: dict, *, tarih: dict | None = None, boyut: str | None = None, property_ref: str = "PROP_PROD",
              etkin: bool = True) -> dict:
    """Kütüphane raporundan iş tanımı (doğrulanmamış ham sözlük; isler.normalize_job ile doğrulanır)."""
    boyutlar = list(r["boyutlar"])
    if boyut and boyut in (r.get("secenekler") or []):
        boyutlar[0] = boyut
    tanim = {"ad": r["ad"], "aciklama": f"GA4 hazır raporu: {paket_adi(p)} › {r['ad']} (yakın kopya). {r.get('aciklama', '')}".strip()[:500],
             "property_ref": property_ref, "metrikler": list(r["metrikler"]), "boyutlar": boyutlar,
             "filtreler": [dict(f) for f in r.get("filtreler", [])], "tarih": dict(tarih or VARSAYILAN_TARIH), "etkin": etkin,
             "paket": {"kimlik": f"kutuphane:{p['kimlik']}", "rapor": r["kimlik"]}}
    if r.get("zaman"):
        tanim["zaman"] = r["zaman"]
    return tanim
