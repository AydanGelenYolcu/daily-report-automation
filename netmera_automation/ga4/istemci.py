"""Veri kaynağı sözleşmesi ve ortak hata türü.

Gerçek (ga4.py) ve sentetik (sentetik.py) istemci aynı GA4 REST yanıt biçimini döndürür;
böylece rapor.py hangi kaynakla çalıştığını bilmeden aynı doğrulamayı yapar.
"""
from __future__ import annotations

from typing import Protocol

# Kod → kullanıcının yapacağı somut adım. Ham Google yanıtı ve property kimliği iletiye girmez.
MESSAGES = {
    "PERMISSION_DENIED": "Bağlı hesabın bu mülke erişimi yok. Google hesabınla bağlandıysan doğru mülkü seçtiğini ve hesabın GA4'te en az "
                         "Görüntüleyen rolü olduğunu kontrol et; hizmet hesabıysa e-postasını GA4 → Yönetici → Mülk erişim yönetimi'nde "
                         "Görüntüleyen olarak ekle (birkaç dakika bekleyebilir).",
    "KIMLIK_DOSYASI": "Hizmet hesabı anahtar dosyası okunamadı. `kurulum` komutunu yeniden çalıştır.",
    "SERVICE_DISABLED": "Google Analytics Data API bu Google Cloud projesinde kapalı. APIs & Services → Library → "
                        "'Google Analytics Data API' → Enable; birkaç dakika sonra yeniden dene.",
    "RESOURCE_EXHAUSTED": "GA4 kota sınırı aşıldı; otomatik tekrar yapılmadı. Bir saat sonra yeniden dene.",
    "INVALID_ARGUMENT": "GA4 isteği reddetti: metrik/boyut adı geçersiz ya da birbiriyle uyumsuz olabilir. "
                        "Arayüzde işi açıp doğrulama sonucuna bak (komut satırında: dogrula).",
    "UNAUTHENTICATED": "Google kimlik doğrulaması geçersiz veya süresi dolmuş.",
    "UNAVAILABLE": "GA4 sunucusu yanıt vermedi (geçici hata). Daha sonra yeniden dene.",
    "AG_HATASI": "GA4'e bağlanılamadı (ağ veya proxy). Şirket ağının analyticsdata.googleapis.com adresine izin verdiğini kontrol et.",
    "KIMLIK_SECILMEDI": "GA4 bağlantısı kurulmadı: arayüzde Bağlantı sayfasından Google hesabını bağla. Şimdilik sentetik kaynak kullan.",
    "YANIT_GECERSIZ": "GA4 yanıtı beklenen biçimde değil; sonuç kullanılmadı.",
    "KAYIT_YOK": "GA4 bu sorgu için satır döndürmedi. Filtre değerinin yazımını kontrol et; gerçekten sıfırsa "
                 "işte \"kayit_yoksa_sifir\": true kullan (yalnız boyutsuz işler).",
}


class ClientError(Exception):
    """Kaynak isteği tamamlanamadı. İleti kullanıcıya gösterilir."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or MESSAGES.get(code) or f"GA4 isteği başarısız ({code}).")


class Client(Protocol):
    kaynak: str  # "ga4" veya "sentetik"; çıktıda sonuçların karışmasını önler

    def metadata(self, property_ref: str) -> dict: ...

    def run_report(self, property_ref: str, body: dict) -> dict: ...

    def check_compatibility(self, property_ref: str, body: dict) -> dict: ...
