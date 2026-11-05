"""Dışa aktarma: son çalıştırmanın sonuçlarını sayfalara böler ve Excel (.xlsx), CSV ya da yeni bir Google tablosuna yazar.

Seçenekler (arayüzde her indirmede sorulur):
  duzen   tablo — bir satır bir kırılım/dönem, her metrik ayrı sütun (ekrandaki gibi)
          liste — her satırda tek değer, metrik adı ayrı sütunda (pivot tablo / filtre için)
  bolme   tek   — hepsi tek sayfada · is — her iş ayrı sayfada · donem — her gün/ay ayrı sayfada
          (Excel ve Google Sheets'te sayfa = sekme; CSV'de sayfa = ayrı dosya, birden fazlası ZIP)
  toplam  GA4'ün kendi toplam satırı eklensin mi (tekil kullanıcıda satırların toplamı değildir)

Zaman kırılımlı işte GA4 toplamı tüm aralık içindir; dönem başına bölmede yazılmaz.
Sütun şablonu (iş başına, satır düzeni başına): görünen sütunların sırası + gizlenen sütunlar. Sayfa tüm sütunları tutar,
`gorunum()` yalnız görünenleri şablon sırasıyla verir; böylece arayüz önizlemede sütunları anında gizleyip taşıyabilir.
Excel dosyası yalnız standart kütüphaneyle üretilir (ek paket yok).
"""
from __future__ import annotations

import base64
import csv
import io
import re
import zipfile
from xml.sax.saxutils import escape

from .cikti import STATUS, format_value
from .isler import ZAMAN_BOYUTLARI

HEDEFLER = ("xlsx", "csv", "gsheets")
DUZENLER = ("tablo", "liste")
BOLMELER = ("tek", "is", "donem")
TOPLAM = "(toplam)"
ESKI_ADLAR = {"genis": "tablo", "uzun": "liste"}  # v0.6.0 seçenek adları
SABIT = {"is": "İş", "kaynak": "Kaynak", "donem": "Dönem", "satir": "Satır türü", "kirilim": "Kırılım", "metrik": "Metrik",
         "deger": "Değer", "durum": "Durum", "uyarilar": "Uyarılar", "hata": "Hata"}
YASAK_XLSX = re.compile(r"[\[\]:*?/\\]")
KONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class ExportError(ValueError):
    """Dışa aktarma seçenekleri geçersiz."""


def donem_etiketi(boyut: str, deger) -> str:
    """GA4 zaman değerini okunur ve sıralanabilir metne çevirir: 20261024 → 2026-10-24, 202610 → 2026-10, 202643 → 2026-H43."""
    v = str(deger)
    if boyut == "date" and re.fullmatch(r"\d{8}", v):
        return f"{v[:4]}-{v[4:6]}-{v[6:]}"
    if boyut == "yearMonth" and re.fullmatch(r"\d{6}", v):
        return f"{v[:4]}-{v[4:]}"
    if boyut in ("isoYearIsoWeek", "yearWeek") and re.fullmatch(r"\d{6}", v):
        return f"{v[:4]}-H{v[4:]}"
    return v


def aralik_etiketi(result: dict) -> str:
    bas, bit = result["tarih_cozulen"]
    return bas if bas == bit else f"{bas} – {bit}"


def zaman_boyutu(result: dict) -> str | None:
    return next((b for b in result["boyutlar"] if b in ZAMAN_BOYUTLARI), None) if result.get("zaman") else None


def kayitlar(result: dict, toplam: bool) -> list[dict]:
    """Bir işin satırları ortak biçimde: dönem, zaman dışı kırılımlar, metrikler, satır türü."""
    zaman = zaman_boyutu(result)
    kirilimlar = [b for b in result["boyutlar"] if b != zaman]
    out = []
    if toplam and result.get("toplam") and result["boyutlar"]:
        out.append({"donem": aralik_etiketi(result), "boyutlar": {b: TOPLAM for b in kirilimlar}, "metrikler": result["toplam"],
                    "tur": "toplam", "tum_aralik": bool(zaman)})
    for row in result["satirlar"]:
        donem = donem_etiketi(zaman, row["boyutlar"][zaman]) if zaman else aralik_etiketi(result)
        out.append({"donem": donem, "boyutlar": {b: row["boyutlar"][b] for b in kirilimlar}, "metrikler": row["metrikler"],
                    "tur": "veri", "tum_aralik": False})
    return out


def _sira_ekle(liste: list, yeni):
    for x in yeni:
        if x not in liste:
            liste.append(x)


def sablon_uygula(anahtarlar: list[str], isler_: list[str], sablonlar: dict, duzen: str) -> tuple[list[str], list[str]]:
    """Sayfadaki işlerin şablonlarından görünen sıra ve gizli sütunlar. Şablonda olmayan (sonradan eklenen) sütun sona eklenir.
    Birden çok işli sayfada bir sütun yalnız şablonu olan her işte gizliyse gizlenir."""
    her_is = [((sablonlar.get(ad) or {}).get(duzen)) for ad in isler_]
    varolan = [x for x in her_is if x]
    if not varolan:
        return list(anahtarlar), []
    gizli = [a for a in anahtarlar if all(x and a in x.get("gizli", []) for x in her_is)]
    sira = []
    for x in varolan:
        for a in x.get("sutunlar", []):
            if a in anahtarlar and a not in gizli and a not in sira:
                sira.append(a)
    sira += [a for a in anahtarlar if a not in gizli and a not in sira]
    return sira, gizli


def gorunum(sayfa: dict) -> tuple[list[str], list[list], list[bool]]:
    """Görünen sütunlar şablon sırasıyla: (başlıklar, satırlar, oran bayrakları)."""
    yer = {c["anahtar"]: i for i, c in enumerate(sayfa["sutunlar"])}
    secili = [yer[a] for a in sayfa["sira"] if a in yer]
    return ([sayfa["sutunlar"][i]["baslik"] for i in secili], [[satir[i] for i in secili] for satir in sayfa["satirlar"]],
            [sayfa["sutunlar"][i]["oran"] for i in secili])


def _sayfa(ad: str, parcalar: list[tuple[dict, list[dict]]], kaynak: str, duzen: str, toplam: bool, etiket, sablonlar: dict) -> dict:
    """parcalar: (sonuç, bu sayfaya giren kayıtlar). Değerler türlü kalır (sayı sayı, metin metin)."""
    durumlu = any(r["durum"] != "tamam" for r, _ in parcalar)
    taban = ["is", "kaynak", "donem"] + (["satir"] if toplam else [])
    son = ["durum", "uyarilar", "hata"] if durumlu else []
    satirlar = []

    def ortak(result):
        return {"is": result["is"], "kaynak": kaynak, "durum": STATUS[result["durum"]],
                "uyarilar": ", ".join(result["uyarilar"]), "hata": result["hata"] or ""}

    if duzen == "tablo":
        kirilimlar, metrikler = [], []
        for result, _ in parcalar:
            zaman = zaman_boyutu(result)
            _sira_ekle(kirilimlar, [b for b in result["boyutlar"] if b != zaman])
            _sira_ekle(metrikler, result["metrikler"])
        anahtarlar = taban + kirilimlar + metrikler + son
        for result, kayit in parcalar:
            if result["durum"] == "hata":
                satirlar.append({**ortak(result), "donem": aralik_etiketi(result)})
                continue
            for k in kayit:
                satirlar.append({**ortak(result), "donem": k["donem"], "satir": "Toplam" if k["tur"] == "toplam" else "",
                                 **k["boyutlar"], **{m: v for m, v in k["metrikler"].items() if v is not None}})
    else:
        anahtarlar = taban + ["kirilim", "metrik", "deger"] + son
        for result, kayit in parcalar:
            if result["durum"] == "hata":
                satirlar.append({**ortak(result), "donem": aralik_etiketi(result)})
                continue
            for k in kayit:
                kirilim = " | ".join(f"{etiket(b)}={v}" for b, v in k["boyutlar"].items())
                for m, v in k["metrikler"].items():
                    satirlar.append({**ortak(result), "donem": k["donem"], "satir": "Toplam" if k["tur"] == "toplam" else "",
                                     "kirilim": kirilim, "metrik": etiket(m), "deger": v})
    isler_ = list(dict.fromkeys(r["is"] for r, _ in parcalar))
    sira, gizli = sablon_uygula(anahtarlar, isler_, sablonlar, duzen)
    return {"ad": ad, "isler": isler_, "duzen": duzen,
            "sutunlar": [{"anahtar": a, "baslik": SABIT.get(a) or etiket(a), "oran": a.endswith("Rate")} for a in anahtarlar],
            "satirlar": [[s.get(a, "") if s.get(a) is not None else "" for a in anahtarlar] for s in satirlar],
            "sira": sira, "gizli": gizli}


def sayfalar(ozet: dict, *, duzen: str = "tablo", bolme: str = "tek", toplam: bool = True, adlar: list[str] | None = None,
             etiketler: dict | None = None, sablonlar: dict | None = None) -> list[dict]:
    """sablonlar: iş adı → işin `sablon` alanı ({"tablo": {...}, "liste": {...}})."""
    duzen = ESKI_ADLAR.get(duzen, duzen)
    if duzen not in DUZENLER:
        raise ExportError(f"Satır düzeni şunlardan biri olmalı: {', '.join(DUZENLER)}.")
    if bolme not in BOLMELER:
        raise ExportError(f"Bölme şunlardan biri olmalı: {', '.join(BOLMELER)}.")
    etiketler = {str(k): str(v)[:100] for k, v in (etiketler or {}).items() if isinstance(k, str) and isinstance(v, str)}
    etiket = lambda api: etiketler.get(api) or api
    sonuclar = [r for r in ozet["sonuclar"] if not adlar or r["is"] in adlar]
    if not sonuclar:
        raise ExportError("Dışa aktarılacak sonuç yok.")
    gruplar: dict[str, list] = {}
    for result in sonuclar:
        kayit = kayitlar(result, toplam)
        if bolme == "tek":
            gruplar.setdefault("Sonuçlar", []).append((result, kayit))
        elif bolme == "is":
            gruplar.setdefault(result["is"], []).append((result, kayit))
        elif result["durum"] == "hata":
            gruplar.setdefault("Hatalı işler", []).append((result, []))
        else:
            donemler: dict[str, list] = {}
            for k in kayit:
                if not k["tum_aralik"]:  # zaman kırılımlı işin toplamı tek bir döneme ait değil
                    donemler.setdefault(k["donem"], []).append(k)
            for donem, ks in donemler.items():
                gruplar.setdefault(donem, []).append((result, ks))
    sirali = sorted(gruplar.items()) if bolme == "donem" else list(gruplar.items())
    out = [_sayfa(ad, parcalar, ozet["kaynak"], duzen, toplam, etiket, sablonlar or {}) for ad, parcalar in sirali]
    bos = next((x["ad"] for x in out if not x["sira"]), None)
    if bos:
        raise ExportError(f"'{bos}' sayfasında görünen sütun kalmadı; en az bir sütunu geri getir.")
    return out


def _benzersiz(adlar: list[str], *, uzunluk: int, temizle) -> list[str]:
    out, gorulen = [], set()
    for ad in adlar:
        temel = (temizle(ad).strip().strip("'") or "Sayfa")[:uzunluk]
        aday, n = temel, 1
        while aday.casefold() in gorulen:
            n += 1
            ek = f" ({n})"
            aday = temel[: uzunluk - len(ek)] + ek
        gorulen.add(aday.casefold())
        out.append(aday)
    return out


def _dosya_adi(metin: str) -> str:
    temiz = re.sub(r"[^\w\-]+", "-", metin, flags=re.UNICODE).strip("-")
    return (temiz or "sayfa")[:80]


# ---------- CSV ----------

def _csv(sayfa: dict) -> str:
    basliklar, satirlar, _ = gorunum(sayfa)
    tampon = io.StringIO()
    yazici = csv.writer(tampon, delimiter=";")
    yazici.writerow(basliklar)
    for satir in satirlar:
        yazici.writerow([format_value(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in satir])
    return "﻿" + tampon.getvalue()  # Türkçe Excel UTF-8'i BOM ile tanır


def csv_dosyasi(sayfalar_: list[dict], on_ek: str) -> dict:
    if len(sayfalar_) == 1:
        return {"ad": f"{on_ek}.csv", "tur": "csv", "icerik": _csv(sayfalar_[0]), "sayfa_sayisi": 1}
    tampon = io.BytesIO()
    adlar = _benzersiz([_dosya_adi(s["ad"]) for s in sayfalar_], uzunluk=80, temizle=str)
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for ad, sayfa in zip(adlar, sayfalar_):
            z.writestr(f"{on_ek}-{ad}.csv", _csv(sayfa).encode("utf-8"))
    return {"ad": f"{on_ek}.zip", "tur": "zip", "icerik": base64.b64encode(tampon.getvalue()).decode("ascii"), "sayfa_sayisi": len(sayfalar_)}


# ---------- Excel (.xlsx, Office Open XML) ----------

def _sutun(n: int) -> str:
    harf = ""
    while n:
        n, kalan = divmod(n - 1, 26)
        harf = chr(65 + kalan) + harf
    return harf


def _metin(v) -> str:
    return escape(KONTROL.sub("", str(v)))


def _hucre(ref: str, v, oran: bool, baslik: bool = False) -> str:
    if baslik:
        return f'<c r="{ref}" t="inlineStr" s="1"><is><t xml:space="preserve">{_metin(v)}</t></is></c>'
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "" if v == "" else f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_metin(v)}</t></is></c>'
    stil = 2 if oran else 3 if isinstance(v, int) else 4
    return f'<c r="{ref}" s="{stil}"><v>{v!r}</v></c>'


def _calisma_sayfasi(sayfa: dict) -> str:
    basliklar, veri, oran = gorunum(sayfa)
    sutun_sayisi = max(1, len(basliklar))
    genislik = []
    for i, b in enumerate(basliklar):
        uzun = max([len(str(b))] + [len(str(s[i])) for s in veri[:200]])
        genislik.append(min(60, max(8, uzun + 2)))
    satirlar = ['<row r="1">' + "".join(_hucre(f"{_sutun(i + 1)}1", b, False, True) for i, b in enumerate(basliklar)) + "</row>"]
    for r, satir in enumerate(veri, start=2):
        satirlar.append(f'<row r="{r}">' + "".join(_hucre(f"{_sutun(i + 1)}{r}", v, oran[i]) for i, v in enumerate(satir)) + "</row>")
    son = f"{_sutun(sutun_sayisi)}{len(veri) + 1}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
            '<cols>' + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(genislik)) + '</cols>'
            '<sheetData>' + "".join(satirlar) + '</sheetData>'
            f'<autoFilter ref="A1:{son}"/></worksheet>')


STILLER = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.0%"/></numFmts>'
           '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
           '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
           '<fill><patternFill patternType="solid"><fgColor rgb="FFEEF1F6"/><bgColor indexed="64"/></patternFill></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
           '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
           '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
           '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def xlsx_baytlari(sayfalar_: list[dict]) -> bytes:
    adlar = _benzersiz([s["ad"] for s in sayfalar_], uzunluk=31, temizle=lambda a: YASAK_XLSX.sub("-", a))
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sayfalar_) + 1)) + '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="xl/workbook.xml"/></Relationships>')
        filtreler = "".join(
            f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
            f"{_metin(chr(39) + ad.replace(chr(39), chr(39) * 2) + chr(39))}!$A$1:${_sutun(max(1, len(s['sira'])))}${len(s['satirlar']) + 1}"
            "</definedName>" for i, (ad, s) in enumerate(zip(adlar, sayfalar_)))
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{_metin(ad)}" sheetId="{i}" r:id="rId{i}"/>' for i, ad in enumerate(adlar, start=1))
                   + f'</sheets><definedNames>{filtreler}</definedNames></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sayfalar_) + 1))
                   + f'<Relationship Id="rId{len(sayfalar_) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                     'Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", STILLER)
        for i, sayfa in enumerate(sayfalar_, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _calisma_sayfasi(sayfa))
    return tampon.getvalue()


def xlsx_dosyasi(sayfalar_: list[dict], on_ek: str) -> dict:
    return {"ad": f"{on_ek}.xlsx", "tur": "xlsx", "icerik": base64.b64encode(xlsx_baytlari(sayfalar_)).decode("ascii"),
            "sayfa_sayisi": len(sayfalar_)}


# ---------- Google Sheets (yeni tablo, her sayfa bir sekme) ----------

def sheets_sekme_adlari(sayfalar_: list[dict]) -> list[str]:
    return _benzersiz([s["ad"] for s in sayfalar_], uzunluk=100, temizle=str)


ONIZLEME_SATIR = 1000


def onizleme(sayfalar_: list[dict], satir: int = ONIZLEME_SATIR) -> dict:
    """Arayüzün Excel benzeri önizlemesi: her sayfa tüm sütunlarıyla (gizliler dahil) ve en çok `satir` satırla."""
    return {"sayfalar": [{"ad": s["ad"], "isler": s["isler"], "duzen": s["duzen"], "sutunlar": s["sutunlar"], "sira": s["sira"],
                          "gizli": s["gizli"], "satirlar": s["satirlar"][:satir], "satir_sayisi": len(s["satirlar"])} for s in sayfalar_]}
