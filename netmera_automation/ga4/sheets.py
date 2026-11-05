"""Google Sheets çıktısı: her iş kendi sekmesine tablo olarak yazılır, dönem + kırılıma göre güncellenir/eklenir.

Sekme düzeni: 1. satır başlık — donem, kırılımlar, metrikler, guncellendi. Anahtar = donem + kırılım değerleri.
Anahtarı sekmede olan satır yerinde güncellenir, olmayan alta eklenir (Netmera'daki "tarihe bak, üstüne yaz" kalıbı).
Sekmede bu uygulamanın başlıkları yoksa ve sekme boş değilse hiçbir şey yazılmaz.
Değerler RAW yazılır: dönem metni "2026-10-24" metin kalır (Sheets yerel tarih biçimine çevirmez), sayılar sayıdır.
"""
from __future__ import annotations

from datetime import date, timedelta
import json
import math
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from .disari import kayitlar
from .ga4 import NoRedirect, _reason
from .istemci import ClientError

KOK = "https://sheets.googleapis.com/v4/spreadsheets"
MAX_SATIR = 100_000
MAX_YANIT = 20 * 1024 * 1024
TIMEOUT = 60
GUNCELLENDI = "guncellendi"

MESAJLAR = {
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "Google Sheets izni yok. Bağlantı sayfasında Google hesabını yeniden bağla ve Sheets iznini onayla.",
    "SERVICE_DISABLED": "Google Sheets API bu Google Cloud projesinde kapalı: API'ler ve Hizmetler → Kitaplık → 'Google Sheets API' → Etkinleştir.",
    "PERMISSION_DENIED": "Bu Google tablosuna yazma iznin yok. Tabloyu açabildiğin ve düzenleyebildiğin hesapla bağlı olduğunu kontrol et.",
    "NOT_FOUND": "Google tablosu bulunamadı. Bağlantıyı/kimliği kontrol et; tablo silinmiş olabilir.",
    "RESOURCE_EXHAUSTED": "Google Sheets kota sınırı aşıldı; bir dakika sonra yeniden dene.",
    "INVALID_ARGUMENT": "Google Sheets isteği reddetti (sekme adı veya aralık geçersiz olabilir).",
    "UNAUTHENTICATED": "Google oturumu geçersiz; Bağlantı sayfasından yeniden bağlan.",
}
DURUMLAR = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED"}


def _hata(kod: str) -> ClientError:
    return ClientError(f"SHEETS_{kod}", MESAJLAR.get(kod) or f"Google Sheets isteği başarısız ({kod}).")


def _aralik(sekme: str, hucre: str) -> str:
    return "'" + sekme.replace("'", "''") + "'!" + hucre


def tablo_url(tablo: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{tablo}/edit"


class SheetsIstemci:
    def __init__(self, token_provider, *, opener=None):
        self.token_provider = token_provider
        self.opener = opener or urllib.request.build_opener(NoRedirect)

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        veri = json.dumps(body).encode("utf-8") if body is not None else None
        basliklar = {"Accept": "application/json", "Authorization": f"Bearer {self.token_provider()}"}
        if veri is not None:
            basliklar["Content-Type"] = "application/json"
        istek = urllib.request.Request(url, data=veri, method=method, headers=basliklar)
        try:
            with self.opener.open(istek, timeout=TIMEOUT) as yanit:
                ham = yanit.read(MAX_YANIT + 1)
        except urllib.error.HTTPError as exc:
            neden = _reason(exc)
            raise _hata(neden if neden in MESAJLAR else DURUMLAR.get(exc.code, "UNAVAILABLE" if exc.code >= 500 else f"HTTP_{exc.code}")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ClientError("SHEETS_AG", "Google Sheets'e bağlanılamadı (ağ veya proxy).") from None
        if len(ham) > MAX_YANIT:
            raise ClientError("SHEETS_YANIT", "Google Sheets yanıtı çok büyük.")
        try:
            sonuc = json.loads(ham or b"{}")
        except ValueError:
            raise ClientError("SHEETS_YANIT", "Google Sheets yanıtı okunamadı.") from None
        if not isinstance(sonuc, dict):
            raise ClientError("SHEETS_YANIT", "Google Sheets yanıtı okunamadı.")
        return sonuc

    def bilgi(self, tablo: str) -> dict:
        v = self._call("GET", f"{KOK}/{quote(tablo, safe='')}?" + urlencode({"fields": "spreadsheetId,properties.title,sheets.properties(sheetId,title,sheetType)"}))
        if v.get("spreadsheetId") != tablo:
            raise ClientError("SHEETS_YANIT", "Google tablo kimliği doğrulanamadı.")
        sekmeler = [s["properties"]["title"] for s in v.get("sheets") or []
                    if isinstance(s, dict) and isinstance(s.get("properties"), dict) and isinstance(s["properties"].get("title"), str)
                    and s["properties"].get("sheetType", "GRID") == "GRID"]
        baslik = (v.get("properties") or {}).get("title")
        return {"tablo": tablo, "baslik": baslik[:200] if isinstance(baslik, str) else "", "sekmeler": sekmeler[:200], "url": tablo_url(tablo)}

    def olustur(self, baslik: str) -> dict:
        v = self._call("POST", KOK, {"properties": {"title": baslik[:200]}})
        tablo = v.get("spreadsheetId")
        if not isinstance(tablo, str) or not tablo:
            raise ClientError("SHEETS_YANIT", "Yeni Google tablosu oluşturulamadı.")
        return {"tablo": tablo, "baslik": baslik[:200], "url": tablo_url(tablo)}

    def sayfali_tablo(self, baslik: str, sayfalar: list[dict]) -> dict:
        """Dışa aktarma: yeni tablo açar, her sayfayı ayrı sekmeye yazar (başlık kalın, sabit, filtreli). Tek seferlik kopya."""
        from .disari import gorunum, sheets_sekme_adlari
        adlar = sheets_sekme_adlari(sayfalar)
        gorunen = [gorunum(s) for s in sayfalar]
        v = self._call("POST", KOK, {"properties": {"title": baslik[:200]}, "sheets": [
            {"properties": {"title": ad, "gridProperties": {"frozenRowCount": 1, "rowCount": max(100, len(g[1]) + 1),
                                                            "columnCount": max(10, len(g[0]))}}}
            for ad, g in zip(adlar, gorunen)]})
        tablo = v.get("spreadsheetId")
        kimlikler = [((s.get("properties") or {}).get("sheetId")) for s in v.get("sheets") or []]
        if not isinstance(tablo, str) or len(kimlikler) != len(adlar) or not all(isinstance(k, int) for k in kimlikler):
            raise ClientError("SHEETS_YANIT", "Yeni Google tablosu oluşturulamadı.")
        self.guncelle(tablo, [(_aralik(ad, "A1"), [g[0], *g[1]]) for ad, g in zip(adlar, gorunen)])
        istekler = []
        for kimlik, (basliklar, satirlar, _) in zip(kimlikler, gorunen):
            istekler.append({"repeatCell": {"range": {"sheetId": kimlik, "startRowIndex": 0, "endRowIndex": 1},
                                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}})
            istekler.append({"setBasicFilter": {"filter": {"range": {"sheetId": kimlik, "startRowIndex": 0, "endRowIndex": len(satirlar) + 1,
                                                                     "startColumnIndex": 0, "endColumnIndex": max(1, len(basliklar))}}}})
        self._call("POST", f"{KOK}/{quote(tablo, safe='')}:batchUpdate", {"requests": istekler})
        return {"tablo": tablo, "url": tablo_url(tablo), "sayfa_sayisi": len(adlar)}

    def sekme_ekle(self, tablo: str, sekme: str):
        self._call("POST", f"{KOK}/{quote(tablo, safe='')}:batchUpdate", {"requests": [{"addSheet": {"properties": {"title": sekme}}}]})

    def oku(self, tablo: str, sekme: str) -> list[list]:
        q = urlencode({"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER", "majorDimension": "ROWS"})
        v = self._call("GET", f"{KOK}/{quote(tablo, safe='')}/values/{quote(_aralik(sekme, 'A:ZZ'), safe='')}?{q}")
        degerler = v.get("values") or []
        if not isinstance(degerler, list) or len(degerler) > MAX_SATIR:
            raise ClientError("SHEETS_BUYUK", f"Sekme {MAX_SATIR:,} satırdan büyük; yeni bir sekmeye yaz.".replace(",", "."))
        return degerler

    def guncelle(self, tablo: str, veriler: list[tuple[str, list[list]]]):
        if veriler:
            self._call("POST", f"{KOK}/{quote(tablo, safe='')}/values:batchUpdate",
                       {"valueInputOption": "RAW", "data": [{"range": r, "majorDimension": "ROWS", "values": v} for r, v in veriler]})

    def temizle(self, tablo: str, sekme: str):
        """Sekmenin değerlerini siler (biçim, sekme ve tablo kalır)."""
        self._call("POST", f"{KOK}/{quote(tablo, safe='')}/values/{quote(_aralik(sekme, 'A:ZZ'), safe='')}:clear", {})

    def ekle(self, tablo: str, sekme: str, satirlar: list[list]):
        if satirlar:
            q = urlencode({"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"})
            self._call("POST", f"{KOK}/{quote(tablo, safe='')}/values/{quote(_aralik(sekme, 'A1'), safe='')}:append?{q}",
                       {"majorDimension": "ROWS", "values": satirlar})


def _sutun(n: int) -> str:
    harf = ""
    while n:
        n, kalan = divmod(n - 1, 26)
        harf = chr(65 + kalan) + harf
    return harf


def _anahtar_hucre(deger) -> str:
    """Sekmedeki hücreyi anahtar metnine çevirir: Sheets tarih seri sayısı → ISO gün, tam sayı → '12'."""
    if isinstance(deger, bool):
        return str(deger)
    if isinstance(deger, (int, float)) and math.isfinite(deger):
        if 20000 <= deger <= 2958465 and float(deger).is_integer():
            return (date(1899, 12, 30) + timedelta(days=int(deger))).isoformat()
        return str(int(deger)) if float(deger).is_integer() else str(deger)
    return str(deger if deger is not None else "").strip()


def _anahtar(satir: list, yerler: list[int]) -> tuple:
    return tuple(_anahtar_hucre(satir[i]) if i < len(satir) else "" for i in yerler)


def birlestir(mevcut: list[list], basliklar: list[str], anahtarlar: list[str], yeni: list[list], izinli: list[str] | None = None) -> dict:
    """Saf işlev: hangi satır güncellenecek, hangisi eklenecek. Satırlar sütun ADINA göre eşleşir (sıra şablonla değişebilir).

    Sekmenin başlığı bu işin sütunlarından oluşuyor ama sırası/kapsamı farklıysa (şablon değişti, metrik eklendi/kaldırıldı)
    sekme yeni düzene taşınır: mevcut satırlar korunur, kaldırılan sütun çıkar, yeni sütun boş gelir ("yeniden" = tüm ızgara).
    Başlıkta bu işe ait olmayan bir sütun varsa ya da anahtar sütunlardan biri yoksa hata verir, hiçbir şey yazılmaz."""
    dolu = [s for s in mevcut if any(_anahtar_hucre(x) for x in s)]
    if not dolu:
        return {"baslik": basliklar, "guncelle": [], "ekle": yeni, "baslik_yaz": True, "yeniden": None}
    eski = [_anahtar_hucre(x) for x in mevcut[0]]
    while eski and not eski[-1]:
        eski.pop()
    yeniden = None
    if eski != basliklar:
        uygun = set(izinli or basliklar) | set(basliklar)
        if not eski or len(set(eski)) != len(eski) or not set(eski) <= uygun or not set(anahtarlar) <= set(eski):
            raise ClientError("SHEETS_BASLIK", "Sekmenin ilk satırındaki başlıklar bu işin sütunlarıyla uyuşmuyor (kırılım değişmiş ya da sekme "
                                               "başka amaçla kullanılıyor). Yeni bir sekme adı ver ya da sekmeyi temizle; hiçbir şey yazılmadı.")
        yeniden = [list(basliklar)]
        for satir in mevcut[1:]:
            deger = {ad: (satir[i] if i < len(satir) else "") for i, ad in enumerate(eski)}
            yeniden.append([deger.get(ad, "") for ad in basliklar])
        mevcut = yeniden
    yerler_ = [basliklar.index(a) for a in anahtarlar]
    yerler = {}
    for no, satir in enumerate(mevcut[1:], start=2):
        anahtar = _anahtar(satir, yerler_)
        if any(anahtar):
            yerler.setdefault(anahtar, no)
    guncelle, ekle, gorulen = [], [], set()
    for satir in yeni:
        anahtar = _anahtar(satir, yerler_)
        if anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        if anahtar in yerler:
            guncelle.append((yerler[anahtar], satir))
        else:
            ekle.append(satir)
    if yeniden is not None:
        for no, satir in guncelle:
            yeniden[no - 1] = satir
    return {"baslik": basliklar, "guncelle": guncelle, "ekle": ekle, "baslik_yaz": False, "yeniden": yeniden}


def sutunlar(job: dict) -> tuple[list[str], list[str], list[str]]:
    """Sekme başlığı, anahtar sütunlar (donem + kırılımlar) ve bu işe ait olabilecek bütün sütunlar.

    İşin "tablo" sütun şablonu uygulanır: sıra şablondan, kaldırılan metrik yazılmaz. Anahtar sütunlar satır eşleşmesi için
    gerektiğinden kaldırılamaz (şablonda gizli olsalar da yazılır, yerleri şablondaki gibi). `guncellendi` her zaman sonda."""
    kirilimlar = list(job["boyutlar"])
    anahtarlar = ["donem", *kirilimlar]
    tum = [*anahtarlar, *job["metrikler"]]
    sablon = (job.get("sablon") or {}).get("tablo")
    if sablon:
        gizli = {a for a in sablon.get("gizli", []) if a not in anahtarlar}
        sira = [a for a in sablon.get("sutunlar", []) if a in tum and a not in gizli]
        sira += [a for a in tum if a not in sira and a not in gizli]
    else:
        sira = tum
    return [*sira, GUNCELLENDI], anahtarlar, [*tum, GUNCELLENDI]


def satirlar(job: dict, result: dict, donem_toplamlari: dict[str, dict], zaman_damgasi: str, basliklar: list[str] | None = None) -> list[list]:
    """Sonucu sekme satırlarına çevirir (başlık sırasıyla). donem_toplamlari: dönem → GA4 toplamı (kırılımlı işte "(toplam)" satırı)."""
    basliklar = basliklar or sutunlar(job)[0]
    kirilimlar = list(job["boyutlar"])
    sozluk = []
    for donem, metrikler in donem_toplamlari.items():
        if kirilimlar and metrikler:
            sozluk.append({"donem": donem, **{b: ("(toplam)" if i == 0 else "") for i, b in enumerate(kirilimlar)}, **metrikler})
    for k in kayitlar(result, toplam=False):
        sozluk.append({"donem": k["donem"], **{b: k["boyutlar"].get(b, "") for b in kirilimlar}, **k["metrikler"]})
    return [[zaman_damgasi if ad == GUNCELLENDI else ("" if d.get(ad) is None else d.get(ad, "")) for ad in basliklar] for d in sozluk]


def yaz(istemci: SheetsIstemci, tablo: str, sekme: str, basliklar: list[str], anahtarlar: list[str], yeni: list[list],
        izinli: list[str] | None = None) -> dict:
    bilgi = istemci.bilgi(tablo)
    if sekme.casefold() not in {s.casefold() for s in bilgi["sekmeler"]}:
        istemci.sekme_ekle(tablo, sekme)
        mevcut = []
    else:
        sekme = next(s for s in bilgi["sekmeler"] if s.casefold() == sekme.casefold())
        mevcut = istemci.oku(tablo, sekme)
    plan = birlestir(mevcut, basliklar, anahtarlar, yeni, izinli)
    son = _sutun(len(basliklar))
    if plan["yeniden"] is not None:
        # Sütun düzeni değişti: sekme temizlenip mevcut satırlar yeni düzende yeniden yazılır (satır sayısı aynı kalır)
        istemci.temizle(tablo, sekme)
        istemci.guncelle(tablo, [(_aralik(sekme, "A1"), plan["yeniden"])])
    else:
        guncellemeler = [(_aralik(sekme, f"A{no}:{son}{no}"), [satir]) for no, satir in plan["guncelle"]]
        if plan["baslik_yaz"]:
            guncellemeler.insert(0, (_aralik(sekme, f"A1:{son}1"), [basliklar]))
        istemci.guncelle(tablo, guncellemeler)
    istemci.ekle(tablo, sekme, plan["ekle"])
    return {"tablo": tablo, "tablo_adi": bilgi["baslik"], "sekme": sekme, "url": bilgi["url"],
            "guncellenen": len(plan["guncelle"]), "eklenen": len(plan["ekle"]), "duzen_degisti": plan["yeniden"] is not None}
