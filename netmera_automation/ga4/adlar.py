"""GA4 Türkçe arayüz adları: API adı → hazır rapordaki ad, Keşfet/rapor oluşturucudaki ad, diğer adlar.

GA4 Data API `getMetadata` yalnız İngilizce ad (uiName) döndürür; Türkçe ekran adlarını API vermez. GA4 Türkçe arayüzü
de aynı alana yerine göre farklı ad verir (ör. activeUsers: hazır raporda "Etkin kullanıcı sayısı", Keşfet'te
"Etkin kullanıcılar"). Bu tablo o yüzden burada tutulur:
  - Başlangıç içeriği Site Yapı Çıkarıcı GA4 profilinin sözlüğünden kopyalandı (ortak dosya yok) ve genişletildi.
  - FACT — 2026-10-26: "rapor" adlarının çoğu kullanıcının kendi GA4 raporlarının sütun başlıklarından okundu (yalnız
    başlık; sayı alınmadı). Başlığın API adıyla eşlemesi şemadaki İngilizce adla yapıldı; "# INFERENCE" işaretli
    satırlarda eşleme kesin değil. "kesfet" adları INFERENCE (Keşfet ekranı okunmadı).
  - Öğrenme: içe aktarılan taslakta `etiketler` (ekran adı → API adı) varsa o ad, o mülk için "rapor" adı olur
    (veri klasöründe `ga4-adlar.json`; yalnız ad, sayı yok).
"""
from __future__ import annotations

import json
import os
import threading

from .paths import data_root

OGRENILEN = "ga4-adlar.json"
_KILIT = threading.Lock()

# api: (rapor adı, Keşfet adı ya da None, diğer adlar)
SOZLUK: dict[str, tuple[str, str | None, tuple[str, ...]]] = {
    # --- metrikler ---
    "activeUsers": ("Etkin kullanıcı sayısı", "Etkin kullanıcılar", ("Active users",)),
    "totalUsers": ("Toplam kullanıcı sayısı", "Toplam kullanıcılar", ("Toplam kullanıcı", "Total users")),
    "newUsers": ("Yeni kullanıcı sayısı", "Yeni kullanıcılar", ("New users",)),
    "sessions": ("Oturumlar", None, ("Oturum sayısı", "Sessions")),
    "engagedSessions": ("Etkileşimli oturum sayısı", "Etkileşimli oturumlar", ("Engaged sessions",)),
    "engagementRate": ("Etkileşim oranı", None, ("Engagement rate",)),
    "bounceRate": ("Hemen çıkma oranı", None, ("Bounce rate",)),
    "averageSessionDuration": ("Ortalama oturum süresi", None, ("Online oturum süresi", "Average session duration")),  # Kitleler'deki "Online oturum süresi": INFERENCE
    "userEngagementDuration": ("Kullanıcı etkileşimi", "Kullanıcı etkileşim süresi", ("User engagement",)),
    "sessionsPerUser": ("Kullanıcı başına oturum sayısı", "Kullanıcı başına oturum", ("Sessions per user",)),
    "eventsPerSession": ("Online oturum etkinlikleri sayısı", "Oturum başına etkinlik", ("Oturum başına etkinlik sayısı", "Events per session")),  # INFERENCE
    "screenPageViews": ("Görüntüleme Sayısı", "Görüntülemeler", ("Görüntüleme sayısı", "Views")),
    "screenPageViewsPerUser": ("Online etkin kullanıcı görüntüleme sayısı", None, ("Etkin kullanıcı başına görüntüleme", "Views per active user")),  # INFERENCE
    "screenPageViewsPerSession": ("Online oturum görüntüleme sayısı", None, ("Oturum başına görüntüleme", "Views per session")),  # INFERENCE
    "eventCount": ("Etkinlik sayısı", None, ("Event count",)),
    "eventCountPerUser": ("Etkin kullanıcı başına etkinlik sayısı", None, ("Event count per active user",)),
    "keyEvents": ("Önemli faaliyetler", "Önemli etkinlikler", ("Key events",)),
    "sessionKeyEventRate": ("Oturum önemli etkinlik oranı", None, ("Session key event rate",)),
    "userKeyEventRate": ("Kullanıcı önemli etkinlik oranı", None, ("User key event rate",)),
    "totalRevenue": ("Toplam gelir", None, ("Total revenue",)),
    "purchaseRevenue": ("Satın alma geliri", None, ("Purchase revenue",)),
    "transactions": ("İşlemler", None, ("İşlem sayısı", "Transactions")),
    "ecommercePurchases": ("E-ticaret satın alma işlemi sayısı", None, ("Satın Alma İşlemleri", "Ecommerce purchases")),  # Trafik edinme'deki "Satın Alma İşlemleri": INFERENCE
    "itemsViewed": ("Görüntülenen ürün sayısı", "Görüntülenen öğeler", ("Görüntülenen öğe sayısı", "Items viewed")),
    "itemsAddedToCart": ("Alışveriş sepetine eklenen ürün sayısı", "Sepete eklenen öğeler", ("Sepete eklenen öğe sayısı", "Items added to cart")),
    "itemsPurchased": ("Satın alınan ürünler", "Satın alınan öğeler", ("Satın alınan öğe sayısı", "Miktar", "Items purchased")),  # "Miktar": INFERENCE
    "itemsCheckedOut": ("Ödemesi yapılan ürün sayısı", None, ("Items checked out",)),
    "itemsViewedInPromotion": ("Promosyonda görüntülenen ürün sayısı", None, ("Items viewed in promotion",)),
    "itemsClickedInPromotion": ("Promosyonda tıklanan ürün sayısı", None, ("Items clicked in promotion",)),
    "itemPromotionClickThroughRate": ("Öğe promosyonu tıklama oranı", None, ("Item promotion click through rate",)),
    "itemRevenue": ("Öğe geliri", None, ("Ürün geliri", "Item revenue")),
    "addToCarts": ("Alışveriş sepetine ürün ekleme", "Sepete eklemeler", ("Add to carts",)),
    "checkouts": ("Ödeme işlemleri", None, ("Checkouts",)),
    "totalPurchasers": ("Toplam satın alan", None, ("Toplam alıcı", "Total purchasers")),
    "advertiserAdCost": ("Reklam maliyeti", None, ("Ads cost",)),
    "advertiserAdClicks": ("Reklam tıklaması sayısı", "Reklam tıklamaları", ("Ads clicks",)),
    "advertiserAdImpressions": ("Reklam gösterimi sayısı", "Reklam gösterimleri", ("Ads impressions",)),
    "advertiserAdCostPerClick": ("Tıklama başına reklam maliyeti", None, ("Ads cost per click",)),
    "advertiserAdCostPerKeyEvent": ("Önemli etkinlik başına maliyet", None, ("Cost per key event",)),
    "returnOnAdSpend": ("Reklam harcamalarından elde edilen gelir", None, ("ROAS", "Return on ad spend")),
    "crashFreeUsersRate": ("Kilitlenme sorunu yaşamayan kullanıcılar", None, ("Crash-free users",)),
    # --- boyutlar ---
    "eventName": ("Etkinlik adı", None, ("Event name",)),
    "isKeyEvent": ("Önemli etkinlik mi", None, ("Is key event",)),
    "date": ("Tarih", None, ("Date",)),
    "isoYearIsoWeek": ("Hafta (ISO)", None, ("ISO hafta", "ISO week of ISO year")),
    "yearMonth": ("Yıl ve ay", None, ("Ay", "Year month")),
    "year": ("Yıl", None, ("Year",)),
    "unifiedPagePathScreen": ("Sayfa yolu ve ekran sınıfı", None, ("Page path and screen class",)),
    "unifiedScreenClass": ("Sayfa başlığı ve ekran sınıfı", None, ("Page title and screen class",)),
    "unifiedScreenName": ("Sayfa başlığı ve ekran adı", None, ("Page title and screen name",)),
    "pagePath": ("Sayfa yolu", None, ("Page path",)),
    "pagePathPlusQueryString": ("Sayfa yolu ve sorgu dizesi", None, ("Page path and query string",)),
    "pageTitle": ("Sayfa başlığı", None, ("Page title",)),
    "landingPage": ("Açılış sayfası", None, ("Landing page",)),
    "month": ("Ay", None, ("Month",)),
    "landingPagePlusQueryString": ("Açılış sayfası + sorgu dizesi", None, ("Landing page + query string",)),
    "sessionPrimaryChannelGroup": ("Oturumla ilişkilendirilen birincil kanal grubu (Varsayılan Kanal Grubu)", "Oturum birincil kanal grubu",
                                   ("Oturum ana kanal grubu", "Session primary channel group")),
    "primaryChannelGroup": ("Birincil kanal grubu", None, ("Primary channel group",)),
    "sessionDefaultChannelGroup": ("Oturum varsayılan kanal grubu", "Oturumla ilişkilendirilen varsayılan kanal grubu",
                                   ("Session default channel group",)),
    "firstUserDefaultChannelGroup": ("İlk kullanıcı varsayılan kanal grubu", None, ("First user default channel group",)),
    "firstUserPrimaryChannelGroup": ("İlk kullanıcı birincil kanal grubu (Varsayılan Kanal Grubu)", "İlk kullanıcı birincil kanal grubu",
                                     ("İlk kullanıcı ana kanal grubu", "First user primary channel group")),
    "firstUserSource": ("Kullanıcıyla ilk ilişkilendirilen kaynak", "İlk kullanıcı kaynağı", ("First user source",)),
    "sessionSourceMedium": ("Oturum kaynağı / aracı", "Oturum kaynağı aracı", ("Oturum kaynağı/aracısı", "Session source / medium")),
    "sessionSource": ("Oturum kaynağı", None, ("Session source",)),
    "sessionMedium": ("Oturum aracı", None, ("Session medium",)),
    "sessionCampaignName": ("Oturum kampanyası", None, ("Session campaign",)),
    "sessionManualCampaignName": ("Oturum manuel kampanya adı", None, ("Session manual campaign name",)),
    "sessionManualSourceMedium": ("Oturum manuel kaynağı / aracı", None, ("Session manual source / medium",)),
    "sessionManualSource": ("Oturum manuel kaynağı", None, ("Session manual source",)),
    "sessionManualMedium": ("Oturum manuel aracı", None, ("Session manual medium",)),
    "sessionGoogleAdsCampaignName": ("Oturum Google Ads kampanyası", None, ("Session Google Ads campaign",)),
    "country": ("Ülke", None, ("Başlıca ülkeler", "Country")),
    "region": ("Bölge", None, ("Region",)),
    "city": ("Şehir", None, ("City",)),
    "language": ("Dil", None, ("Language",)),
    "userAgeBracket": ("Yaş", None, ("Age",)),
    "userGender": ("Cinsiyet", None, ("Gender",)),
    "audienceName": ("Kitle adı", "Kitle", ("Audience name",)),
    "deviceCategory": ("Cihaz kategorisi", None, ("Device category",)),
    "platform": ("Platform", None, ()),
    "operatingSystem": ("İşletim sistemi", None, ("Operating system",)),
    "browser": ("Tarayıcı", None, ("Browser",)),
    "screenResolution": ("Ekran çözünürlüğü", None, ("Screen resolution",)),
    "appVersion": ("Uygulama sürümü", None, ("App version",)),
    "streamName": ("Akış adı", None, ("Stream name",)),
    "itemName": ("Ürün adı", "Öğe adı", ("Item name",)),
    "itemId": ("Ürün Kimliği", "Öğe kimliği", ("Item ID",)),
    "itemPromotionName": ("Öğe promosyonu adı", None, ("Item promotion name",)),
    "itemCategory": ("Öğe kategorisi", None, ("Item category",)),
    "itemBrand": ("Öğe markası", None, ("Item brand",)),
}


def ogrenilen() -> dict[str, list[str]]:
    try:
        veri = json.loads((data_root() / OGRENILEN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(veri, dict):
        return {}
    return {str(k): [str(a)[:120] for a in v if isinstance(a, str)][:10] for k, v in veri.items() if isinstance(v, list)}


def ogren(etiketler) -> int:
    """{GA4 ekran adı: API adı} eşlemesini kaydeder; son görülen ad o alanın rapor adı olur. Kaç yeni ad öğrenildiğini döndürür."""
    if not isinstance(etiketler, dict):
        return 0
    from .isler import API_NAME
    with _KILIT:
        veri = ogrenilen()
        yeni = 0
        for ad, api in etiketler.items():
            if not (isinstance(ad, str) and isinstance(api, str) and ad.strip() and API_NAME.match(api)):
                continue
            ad = " ".join(ad.split())[:120]
            liste = veri.setdefault(api, [])
            if ad in liste:
                liste.remove(ad)
            else:
                yeni += 1
            liste.append(ad)  # en son görülen sonda: rapor adı
            del liste[:-10]
        yol = data_root() / OGRENILEN
        yol.parent.mkdir(parents=True, exist_ok=True)
        gecici = yol.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
        gecici.write_text(json.dumps(veri, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(gecici, yol)
    return yeni


def bilgi(api: str, en: str | None = None, *, ozel: bool = False, ogrenilmis: dict | None = None) -> dict:
    """Arayüzde gösterilecek adlar: ad (rapor adı, önde), kesfet, diger (arama için), en (GA4'ün İngilizce adı)."""
    ogrenilmis = ogrenilen() if ogrenilmis is None else ogrenilmis
    gorulen = ogrenilmis.get(api) or []
    rapor, kesfet, diger = SOZLUK.get(api, (None, None, ()))
    if ozel:  # özel boyut/metrik: kullanıcının GA4'te verdiği ad
        rapor, kesfet = (gorulen[-1] if gorulen else en or api), None
    elif gorulen:
        rapor, kesfet = gorulen[-1], (kesfet if kesfet != gorulen[-1] else None) or (rapor if rapor != gorulen[-1] else None)
    rapor = rapor or en or api
    tum = [a for a in dict.fromkeys([*gorulen, *([SOZLUK[api][0]] if api in SOZLUK else []), *diger, *([en] if en else [])]) if a and a not in (rapor, kesfet)]
    return {"ad": rapor, "kesfet": kesfet, "diger": tum, "en": en or api}
