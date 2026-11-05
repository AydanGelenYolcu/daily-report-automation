"""Dışa aktarma (Excel/CSV/Google Sheets), Google Sheets yazımı (sahte Sheets sunucusu), zamanlı çalışma ve Windows görevi.
Ağa çıkmaz, gerçek Windows görevi oluşturmaz; yalnız sentetik veri."""
import base64
from datetime import date, datetime
import csv
import io
import json
import os
import time
import unittest
from unittest import mock
import urllib.error
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from netmera_automation.ga4 import disari, isler as I, projeler, rapor as R, sheets, store, zamanli, zamanlayici
from netmera_automation.ga4.istemci import ClientError
from netmera_automation.ga4.sentetik import SyntheticClient
from tests.ga4.helpers import TempDataCase, job
from tests.ga4.test_kimlik import Yanit
from tests.ga4.test_server import SunucuTemel

BUGUN = date(2026, 10, 25)
TABLO = "1SentetikTabloKimligi_0123456789abc"
YENI = "1YeniSentetikTablo_0123456789abcdef"
SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def is_(**changes):
    return I.normalize_job(job(**changes), today=BUGUN)


class GA4Gibi(SyntheticClient):
    """Sheets yalnız GA4 sonucunu kabul eder; testte sentetik sayılar GA4 kaynağı gibi davranır."""
    kaynak = "ga4"


class SahteSheets:
    """Sheets API'nin kullanılan uçlarını bellekte taklit eder."""

    def __init__(self, sekmeler=None):
        self.sekmeler = {k: [list(r) for r in v] for k, v in (sekmeler or {}).items()}
        self.yeni = {}  # dışa aktarmayla açılan tablolar: sekme → satırlar
        self.istekler = []

    @staticmethod
    def _sekme_hucre(aralik):
        sekme, _, hucre = aralik.rpartition("!")
        return sekme.strip("'").replace("''", "'"), hucre

    def open(self, istek, timeout=None):
        url, yontem = istek.full_url, istek.get_method()
        govde = json.loads(istek.data) if istek.data else None
        self.istekler.append((yontem, url, govde))
        yol = unquote(urlsplit(url).path)
        if yontem == "POST" and yol == "/v4/spreadsheets":
            if govde.get("sheets"):
                self.yeni = {s["properties"]["title"]: [] for s in govde["sheets"]}
                return Yanit(json.dumps({"spreadsheetId": YENI, "sheets": [{"properties": {"sheetId": 100 + i, "title": t}}
                                                                           for i, t in enumerate(self.yeni)]}).encode())
            return Yanit(json.dumps({"spreadsheetId": TABLO}).encode())
        kimlik = yol.split("/")[3].split(":")[0] if yol.count("/") >= 3 else ""
        hedef = self.yeni if kimlik == YENI else self.sekmeler
        if kimlik not in (TABLO, YENI):
            raise urllib.error.HTTPError(url, 404, "x", {}, io.BytesIO(json.dumps({"error": {"status": "NOT_FOUND"}}).encode()))
        if yontem == "GET" and yol == f"/v4/spreadsheets/{kimlik}":
            return Yanit(json.dumps({"spreadsheetId": kimlik, "properties": {"title": "Sentetik tablo"},
                                     "sheets": [{"properties": {"sheetId": i, "title": t}} for i, t in enumerate(hedef)]}).encode())
        if yontem == "POST" and yol.endswith(":batchUpdate") and "/values" not in yol:
            for r in govde["requests"]:
                if "addSheet" in r:
                    hedef[r["addSheet"]["properties"]["title"]] = []
            return Yanit(b'{"replies": [{}]}')
        if yontem == "GET" and "/values/" in yol:
            sekme, _ = self._sekme_hucre(yol.split("/values/", 1)[1])
            return Yanit(json.dumps({"values": hedef[sekme]}).encode())
        if yontem == "POST" and yol.endswith("/values:batchUpdate"):
            assert govde["valueInputOption"] == "RAW"
            for d in govde["data"]:
                sekme, hucre = self._sekme_hucre(d["range"])
                satir = int("".join(ch for ch in hucre.split(":")[0] if ch.isdigit()))
                tablo = hedef[sekme]
                for i, degerler in enumerate(d["values"]):
                    while len(tablo) < satir + i:
                        tablo.append([])
                    tablo[satir + i - 1] = list(degerler)
            return Yanit(b"{}")
        if yontem == "POST" and yol.endswith(":clear"):
            sekme, _ = self._sekme_hucre(yol.split("/values/", 1)[1][: -len(":clear")])
            hedef[sekme] = []
            return Yanit(b"{}")
        if yontem == "POST" and yol.endswith(":append"):
            sekme, _ = self._sekme_hucre(yol.split("/values/", 1)[1][: -len(":append")])
            hedef[sekme].extend(list(r) for r in govde["values"])
            return Yanit(b"{}")
        raise AssertionError(f"beklenmeyen istek {yontem} {url}")


def ozet_(*sonuclar, kaynak="sentetik"):
    return {"calisma": "x", "zaman": "z", "kaynak": kaynak, "sonuclar": list(sonuclar)}


ETIKET = {"country": "Ülke", "platform": "Platform", "activeUsers": "Etkin kullanıcılar", "sessions": "Oturumlar", "engagementRate": "Etkileşim oranı"}


class DisariAktarma(unittest.TestCase):
    def setUp(self):
        self.client = SyntheticClient(BUGUN)
        self.ulke = R.run_job(is_(ad="Ülke", boyutlar=["country"], metrikler=["activeUsers", "engagementRate"]), self.client, today=BUGUN)
        self.aylik = R.run_job(is_(ad="Aylık", boyutlar=["platform"], zaman="ay", tarih={"tur": "son_n_ay", "n": 2}), self.client, today=BUGUN)

    def oku_csv(self, metin):
        self.assertTrue(metin.startswith("﻿"))
        return list(csv.reader(io.StringIO(metin[1:]), delimiter=";"))

    def test_tablo_duzeni_turkce_basliklar_ve_toplam(self):
        s = disari.sayfalar(ozet_(self.ulke), duzen="tablo", bolme="tek", toplam=True, etiketler=ETIKET)
        self.assertEqual(len(s), 1)
        basliklar, satirlar, oran = disari.gorunum(s[0])
        self.assertEqual(basliklar, ["İş", "Kaynak", "Dönem", "Satır türü", "Ülke", "Etkin kullanıcılar", "Etkileşim oranı"])
        self.assertEqual(satirlar[0][3:5], ["Toplam", "(toplam)"])
        self.assertIsInstance(satirlar[1][5], int)  # sayılar sayı kalır
        self.assertEqual(oran, [False] * 6 + [True])
        self.assertEqual((s[0]["isler"], s[0]["gizli"]), (["Ülke"], []))
        toplamsiz = disari.gorunum(disari.sayfalar(ozet_(self.ulke), toplam=False)[0])
        self.assertNotIn("Satır türü", toplamsiz[0])
        self.assertEqual(len(toplamsiz[1]), len(self.ulke["satirlar"]))
        eski = disari.sayfalar(ozet_(self.ulke), duzen="genis")  # v0.6.0 adı da kabul edilir
        self.assertEqual(disari.gorunum(eski[0])[0][:3], ["İş", "Kaynak", "Dönem"])

    def test_sutun_sablonu_gizle_tasi_ve_yeni_sutun(self):
        sablon = {"Ülke": {"tablo": {"sutunlar": ["country", "activeUsers", "donem"], "gizli": ["is", "kaynak", "engagementRate"]}}}
        s = disari.sayfalar(ozet_(self.ulke), sablonlar=sablon, etiketler=ETIKET)[0]
        basliklar, satirlar, _ = disari.gorunum(s)
        self.assertEqual(basliklar, ["Ülke", "Etkin kullanıcılar", "Dönem", "Satır türü"])  # şablonda olmayan sütun sona eklenir
        self.assertEqual(len(satirlar[0]), 4)
        self.assertEqual(s["gizli"], ["is", "kaynak", "engagementRate"])
        self.assertEqual(len(s["sutunlar"]), 7)  # gizliler de önizleme için sayfada durur
        liste = disari.gorunum(disari.sayfalar(ozet_(self.ulke), duzen="liste", sablonlar=sablon)[0])[0]
        self.assertIn("İş", liste)  # şablon satır düzeni başınadır
        # Birden çok işli sayfa: sütun yalnız her işte gizliyse gizlenir
        ikili = disari.sayfalar(ozet_(self.ulke, self.aylik), sablonlar=sablon)[0]
        self.assertEqual(ikili["gizli"], [])
        ikisi = {**sablon, "Aylık": {"tablo": {"sutunlar": [], "gizli": ["is", "kaynak"]}}}
        self.assertEqual(disari.sayfalar(ozet_(self.ulke, self.aylik), sablonlar=ikisi)[0]["gizli"], ["is", "kaynak"])
        with self.assertRaises(disari.ExportError):  # hiç sütun kalmazsa
            disari.sayfalar(ozet_(self.ulke), toplam=False, sablonlar={"Ülke": {"tablo": {"sutunlar": [], "gizli": [
                "is", "kaynak", "donem", "country", "activeUsers", "engagementRate"]}}})
        with self.assertRaises(I.JobError):
            I.normalize_sablon({"tablo": {"sutunlar": ["is"], "gizli": ["is"]}}, "x")
        with self.assertRaises(I.JobError):
            I.normalize_sablon({"tablo": {"sutunlar": ["kötü sütun"]}}, "x")

    def test_liste_duzeni_ve_bolme(self):
        liste_sayfa = disari.sayfalar(ozet_(self.ulke, self.aylik), duzen="liste", toplam=False, etiketler=ETIKET)[0]
        basliklar, liste, _ = disari.gorunum(liste_sayfa)
        self.assertEqual(basliklar, ["İş", "Kaynak", "Dönem", "Kırılım", "Metrik", "Değer"])
        self.assertEqual(len(liste), 3 * 2 + 2 * 3)
        self.assertIn("Ülke=", liste[0][3])
        self.assertEqual({r[4] for r in liste}, {"Etkin kullanıcılar", "Etkileşim oranı"})
        is_basina = disari.sayfalar(ozet_(self.ulke, self.aylik), bolme="is")
        self.assertEqual([x["ad"] for x in is_basina], ["Ülke", "Aylık"])
        donem = disari.sayfalar(ozet_(self.ulke, self.aylik), bolme="donem", toplam=True)
        self.assertEqual([x["ad"] for x in donem], ["2026-08", "2026-09", "2026-10-24"])
        self.assertNotIn("Toplam", [r[3] for r in donem[0]["satirlar"]])  # aylık işin tüm aralık toplamı döneme yazılmaz
        self.assertEqual(donem[2]["satirlar"][0][3], "Toplam")  # tek günlük işin toplamı o döneme aittir
        with self.assertRaises(disari.ExportError):
            disari.sayfalar(ozet_(self.ulke), duzen="pdf")

    def test_xlsx_gecerli_ve_sayfali(self):
        s = disari.sayfalar(ozet_(self.ulke, self.aylik), bolme="is", etiketler=ETIKET,
                            sablonlar={"Aylık": {"tablo": {"sutunlar": ["platform"], "gizli": ["kaynak"]}}})
        s[1]["ad"] = "Ay/lık: [özel]"  # Excel'de yasak karakterler temizlenir
        d = disari.xlsx_dosyasi(s, "ga4-test")
        self.assertEqual((d["ad"], d["tur"], d["sayfa_sayisi"]), ("ga4-test.xlsx", "xlsx", 2))
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(d["icerik"]))) as z:
            for ad in z.namelist():
                if ad.endswith(".xml") or ad.endswith(".rels"):
                    ET.fromstring(z.read(ad))  # her parça geçerli XML
            kitap = ET.fromstring(z.read("xl/workbook.xml"))
            self.assertEqual([x.get("name") for x in kitap.iter(f"{SS}sheet")], ["Ülke", "Ay-lık- -özel-"])
            sayfa = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            ilk = [c for c in sayfa.iter(f"{SS}row")][0]
            self.assertEqual("".join(ilk.itertext())[:9], "İşKaynakD")
            self.assertIsNotNone(sayfa.find(f"{SS}autoFilter"))  # başlıkta sırala/filtre okları
            sayi = [c for c in sayfa.iter(f"{SS}c") if c.get("s") in ("2", "3")]
            self.assertTrue(sayi and all(c.get("t") is None for c in sayi))  # sayılar sayı hücresi
            ikinci = ET.fromstring(z.read("xl/worksheets/sheet2.xml"))
            self.assertEqual("".join([c for c in ikinci.iter(f"{SS}row")][0].itertext())[:8], "PlatformİşD"[:8])  # şablon sırası

    def test_csv_tek_dosya_ve_zip(self):
        tek = disari.csv_dosyasi(disari.sayfalar(ozet_(self.ulke), etiketler=ETIKET), "ga4-test")
        self.assertEqual(tek["tur"], "csv")
        satirlar = self.oku_csv(tek["icerik"])
        self.assertEqual(satirlar[0][:4], ["İş", "Kaynak", "Dönem", "Satır türü"])
        self.assertRegex(satirlar[1][6], r"^0,\d+$")  # Türkçe ondalık virgül
        coklu = disari.csv_dosyasi(disari.sayfalar(ozet_(self.ulke, self.aylik), bolme="is"), "ga4-test")
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(coklu["icerik"]))) as z:
            self.assertEqual(sorted(z.namelist()), ["ga4-test-Aylık.csv", "ga4-test-Ülke.csv"])

    def test_onizleme_butun_sayfalar(self):
        o = disari.onizleme(disari.sayfalar(ozet_(self.ulke, self.aylik), bolme="is", etiketler=ETIKET), satir=2)
        self.assertEqual([x["ad"] for x in o["sayfalar"]], ["Ülke", "Aylık"])
        ikinci = o["sayfalar"][1]  # her sayfa kendi satırlarıyla gelir
        self.assertEqual((len(ikinci["satirlar"]), ikinci["satir_sayisi"], ikinci["isler"]), (2, 1 + 6, ["Aylık"]))
        self.assertEqual(ikinci["sutunlar"][2], {"anahtar": "donem", "baslik": "Dönem", "oran": False})

    def test_google_sheets_sayfali_tablo(self):
        sahte = SahteSheets()
        istemci = sheets.SheetsIstemci(lambda: "SAHTE", opener=sahte)
        s = disari.sayfalar(ozet_(self.ulke, self.aylik, kaynak="ga4"), bolme="is", etiketler=ETIKET)
        sonuc = istemci.sayfali_tablo("Sentetik dışa aktarma", s)
        self.assertEqual((sonuc["tablo"], sonuc["sayfa_sayisi"]), (YENI, 2))
        self.assertEqual(list(sahte.yeni), ["Ülke", "Aylık"])
        self.assertEqual(sahte.yeni["Ülke"][0][:3], ["İş", "Kaynak", "Dönem"])
        self.assertEqual(len(sahte.yeni["Aylık"]), 1 + len(disari.gorunum(s[1])[1]))
        istekler = [r for _, u, g in sahte.istekler if u.endswith(":batchUpdate") and "/values" not in u for r in g["requests"]]
        self.assertEqual(sorted({next(iter(r)) for r in istekler}), ["repeatCell", "setBasicFilter"])


class SheetsBirlestirme(unittest.TestCase):
    B = ["donem", "platform", "activeUsers", "guncellendi"]
    A = ["donem", "platform"]

    def test_sutun_duzeni_degisince_sekme_tasinir(self):
        mevcut = [self.B, ["2026-10-23", "web", 1, "e"], ["2026-10-24", "web", 3, "e"]]
        yeni_bas = ["platform", "donem", "sessions", "guncellendi"]  # sıra değişti, activeUsers kaldırıldı, sessions eklendi
        p = sheets.birlestir(mevcut, yeni_bas, self.A, [["web", "2026-10-24", 30, "t"], ["web", "2026-10-25", 9, "t"]],
                             izinli=["donem", "platform", "activeUsers", "sessions", "guncellendi"])
        self.assertEqual(p["yeniden"], [yeni_bas, ["web", "2026-10-23", "", "e"], ["web", "2026-10-24", 30, "t"]])
        self.assertEqual((len(p["guncelle"]), p["ekle"]), (1, [["web", "2026-10-25", 9, "t"]]))
        with self.assertRaises(ClientError):  # anahtar sütun (kırılım) eski başlıkta yoksa eşleştirilemez
            sheets.birlestir([["donem", "activeUsers"], ["2026-10-24", 1]], self.B, self.A, [], izinli=self.B)
        with self.assertRaises(ClientError):  # başka işe ait sütun
            sheets.birlestir([["donem", "platform", "Satış"], ["x", "y", 1]], self.B, self.A, [], izinli=self.B)

    def test_sablonlu_sutunlar_anahtari_korur(self):
        j = is_(boyutlar=["platform"], metrikler=["activeUsers", "sessions"],
                sablon={"tablo": {"sutunlar": ["sessions", "platform"], "gizli": ["activeUsers", "is", "donem"]}})
        basliklar, anahtarlar, izinli = sheets.sutunlar(j)
        self.assertEqual(basliklar, ["sessions", "platform", "donem", "guncellendi"])  # donem gizlenemez, şablondaki yerinde
        self.assertEqual((anahtarlar, izinli), (["donem", "platform"], ["donem", "platform", "activeUsers", "sessions", "guncellendi"]))
        self.assertEqual(sheets.sutunlar(is_(boyutlar=["platform"]))[0], ["donem", "platform", "activeUsers", "guncellendi"])

    def test_bos_sekme_baslik_ve_ekleme(self):
        p = sheets.birlestir([], self.B, self.A, [["2026-10-24", "web", 5, "t"]])
        self.assertEqual((p["baslik_yaz"], p["guncelle"], p["ekle"]), (True, [], [["2026-10-24", "web", 5, "t"]]))

    def test_tarihe_gore_guncelle_ve_alta_ekle(self):
        mevcut = [self.B, ["2026-10-23", "web", 1, "e"], [46319, "iOS", 2, "e"], ["2026-10-24", "web", 3, "e"]]  # 46319 = 2026-10-24 seri
        yeni = [["2026-10-24", "web", 30, "t"], ["2026-10-24", "iOS", 20, "t"], ["2026-10-25", "web", 9, "t"], ["2026-10-25", "web", 9, "t"]]
        p = sheets.birlestir(mevcut, self.B, self.A, yeni)
        self.assertEqual(p["guncelle"], [(4, ["2026-10-24", "web", 30, "t"]), (3, ["2026-10-24", "iOS", 20, "t"])])
        self.assertEqual(p["ekle"], [["2026-10-25", "web", 9, "t"]])  # aynı anahtar iki kez gelirse bir kez yazılır

    def test_farkli_baslikli_sekmeye_dokunmaz(self):
        with self.assertRaises(ClientError) as yakala:
            sheets.birlestir([["Tarih", "Satış"], ["1", "2"]], self.B, self.A, [["2026-10-24", "web", 5, "t"]])
        self.assertEqual(yakala.exception.code, "SHEETS_BASLIK")
        self.assertTrue(sheets.birlestir([[], ["", ""]], self.B, self.A, [])["baslik_yaz"])


class ZamanliCalisma(TempDataCase):
    def setUp(self):
        super().setUp()
        self.sahte = SahteSheets()
        self.sheets = sheets.SheetsIstemci(lambda: "SAHTE", opener=self.sahte)
        self.client = GA4Gibi(BUGUN)
        self.simdi = datetime(2026, 10, 25, 9, 30).astimezone()
        self.proje = {"sheets": {"tablo": TABLO, "guncelle_gun": 3, "toplam": True}}

    def test_yazim_isi_son_n_gun_kurali(self):
        dun = is_()
        self.assertEqual((zamanli.yazim_isi(dun, 3)["zaman"], zamanli.yazim_isi(dun, 3)["tarih"]), ("gun", {"tur": "son_n_gun", "n": 3}))
        self.assertIs(zamanli.yazim_isi(dun, 1).get("zaman"), None)  # yalnız dün
        gunluk = is_(zaman="gun", tarih={"tur": "son_n_gun", "n": 2})
        self.assertEqual(zamanli.yazim_isi(gunluk, 5)["tarih"], {"tur": "son_n_gun", "n": 5})
        aylik = is_(zaman="ay", tarih={"tur": "son_n_ay", "n": 3})
        self.assertEqual(zamanli.yazim_isi(aylik, 3), aylik)

    def test_proje_varsayilanlari_ve_is_ozel_ayari(self):
        self.assertEqual(projeler.hedef(is_(ad="A", sheets={}), self.proje),
                         {"tablo": TABLO, "sekme": "A", "guncelle_gun": 3, "toplam": True})
        ozel = is_(ad="A", sheets={"sekme": "Özel", "guncelle_gun": 7, "toplam": False, "tablo": YENI})
        self.assertEqual(projeler.hedef(ozel, self.proje), {"tablo": YENI, "sekme": "Özel", "guncelle_gun": 7, "toplam": False})
        self.assertIsNone(projeler.hedef(is_(ad="A"), self.proje))
        with self.assertRaises(ClientError) as yakala:
            projeler.hedef(is_(ad="A", sheets={}), {"sheets": {"tablo": None}})
        self.assertEqual(yakala.exception.code, "SHEETS_TABLO_YOK")

    def test_ilk_yazim_sonra_guncelleme_ve_donem_toplamlari(self):
        platform = is_(ad="Platform", boyutlar=["platform"], metrikler=["activeUsers"], sheets={})
        ozet = zamanli.calistir([platform], self.client, self.sheets, proje=self.proje, now=self.simdi)
        self.assertEqual((ozet["tamam"], ozet["hata"]), (1, 0), ozet)
        sekme = self.sahte.sekmeler["Platform"]
        self.assertEqual(sekme[0], ["donem", "platform", "activeUsers", "guncellendi"])
        self.assertEqual(len(sekme), 1 + 3 * (3 + 1))  # son 3 gün × (3 platform + günün toplamı)
        toplam_satirlari = [s for s in sekme[1:] if s[1] == "(toplam)"]
        self.assertEqual(sorted(s[0] for s in toplam_satirlari), ["2026-10-22", "2026-10-23", "2026-10-24"])
        gun = [s for s in sekme[1:] if s[0] == "2026-10-24"]
        self.assertLess(next(s[2] for s in gun if s[1] == "(toplam)"), sum(s[2] for s in gun if s[1] != "(toplam)"))  # GA4 tekil toplamı
        ertesi = datetime(2026, 10, 26, 9, 30).astimezone()
        ozet = zamanli.calistir([platform], GA4Gibi(date(2026, 10, 26)), self.sheets, proje=self.proje, now=ertesi)
        self.assertEqual((ozet["isler"][0]["guncellenen"], ozet["isler"][0]["eklenen"]), (8, 4))
        self.assertEqual(sorted({s[0] for s in self.sahte.sekmeler["Platform"][1:]}), ["2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"])

    def test_sablon_degisince_sekme_yeni_duzene_gecer(self):
        platform = is_(ad="Platform", boyutlar=["platform"], metrikler=["activeUsers", "sessions"], sheets={})
        zamanli.calistir([platform], self.client, self.sheets, proje=self.proje, now=self.simdi)
        sekme = self.sahte.sekmeler["Platform"]
        self.assertEqual(sekme[0], ["donem", "platform", "activeUsers", "sessions", "guncellendi"])
        once = {(r[0], r[1]): r[2] for r in sekme[1:]}
        sablonlu = {**platform, "sablon": {"tablo": {"sutunlar": ["platform", "donem", "activeUsers"], "gizli": ["sessions"]}}}
        ozet = zamanli.calistir([sablonlu], self.client, self.sheets, proje=self.proje, now=self.simdi)
        self.assertTrue(ozet["isler"][0]["duzen_degisti"], ozet)
        sekme = self.sahte.sekmeler["Platform"]
        self.assertEqual(sekme[0], ["platform", "donem", "activeUsers", "guncellendi"])
        self.assertEqual(len(sekme), 1 + len(once))  # satırlar korundu, çoğalmadı
        self.assertEqual({(r[1], r[0]): r[2] for r in sekme[1:]}, once)  # aynı satıra aynı sayı
        ozet = zamanli.calistir([sablonlu], self.client, self.sheets, proje=self.proje, now=self.simdi)
        self.assertFalse(ozet["isler"][0]["duzen_degisti"])  # bir sonraki yazım normal güncelleme

    def test_sentetik_tablosuz_ve_hatali_isler(self):
        with self.assertRaises(ClientError):
            zamanli.calistir([is_(sheets={})], SyntheticClient(BUGUN), self.sheets, proje=self.proje, now=self.simdi)
        tablosuz = zamanli.calistir([is_(ad="T", sheets={})], self.client, self.sheets, proje={"sheets": {"tablo": None}}, now=self.simdi)
        self.assertIn("tablosu seçilmedi", tablosuz["isler"][0]["mesaj"])
        bozuk = is_(ad="Bozuk", boyutlar=["bilinmeyen"], sheets={"sekme": "B"})
        ozet = zamanli.calistir([bozuk, is_(ad="Hedefsiz")], self.client, self.sheets, proje=self.proje, now=self.simdi)
        self.assertEqual([(s["is"], s["durum"]) for s in ozet["isler"]], [("Bozuk", "hata")])
        self.assertNotIn("B", self.sahte.sekmeler)  # hatalı iş sekme bile açmaz
        self.sahte.sekmeler["Dolu"] = [["Tarih", "Satış"], ["x", "y"]]
        ozet = zamanli.calistir([is_(ad="D", sheets={"sekme": "dolu"})], self.client, self.sheets, proje=self.proje, now=self.simdi)
        self.assertIn("başlıklar", ozet["isler"][0]["mesaj"])
        self.assertEqual(self.sahte.sekmeler["Dolu"], [["Tarih", "Satış"], ["x", "y"]])

    def test_kilit_ve_kayitli_ozet(self):
        klasor = self.tmp / "k"
        with zamanli.kilit(klasor):
            with self.assertRaises(ClientError) as yakala:
                with zamanli.kilit(klasor):
                    pass
            self.assertEqual(yakala.exception.code, "MESGUL")
            bekleme = []
            with self.assertRaises(ClientError):
                with zamanli.kilit(klasor, bekle=0.01, uyku=bekleme.append):
                    pass
        yol = klasor / zamanli.KILIT
        yol.write_text("")
        eski = time.time() - zamanli.KILIT_OMRU - 10
        os.utime(yol, (eski, eski))
        with zamanli.kilit(klasor):  # çökmüş çalışmanın eski kilidi yok sayılır
            pass
        pid = projeler.varsayilan()
        projeler.guncelle(pid, {"sheets": {"tablo": TABLO}})
        store.save([job(ad="Platform", sheets={})], pid)
        ozet = zamanli.kayitli_calistir(pid, tetikleyen="test", istemciler=(self.client, self.sheets))
        self.assertEqual((ozet["tetikleyen"], ozet["proje"], ozet["tamam"]), ("test", pid, 1))
        kayit = zamanli.son_ozet(pid)
        self.assertEqual(kayit["isler"][0]["sekme"], "Platform")
        self.assertNotIn("activeUsers", json.dumps(kayit))  # özette metrik değeri yok
        ozet = zamanli.kayitli_calistir(pid, tetikleyen="test")  # GA4 ayarı yok → genel hata kaydedilir
        self.assertIn("GA4 bağlantısı kurulmadı", ozet["genel_hata"])


class Zamanlayici(TempDataCase):
    def test_ayar_ve_gorev_xml(self):
        self.assertEqual(zamanlayici.ayar_dogrula({"saat": "08:05", "gunler": ["FRI", "MON"]}), {"saat": "08:05", "gunler": ["MON", "FRI"]})
        for kotu in ({"saat": "8:5", "gunler": ["MON"]}, {"saat": "25:00", "gunler": ["MON"]}, {"saat": "08:00", "gunler": []},
                     {"saat": "08:00", "gunler": ["PZT"]}):
            with self.subTest(kotu=kotu), self.assertRaises(zamanlayici.ZamanlamaHatasi):
                zamanlayici.ayar_dogrula(kotu)
        xml = zamanlayici.gorev_xml("gunluk", {"saat": "08:05", "gunler": ["MON"]}, zamanlayici.python_yolu(),
                                    baslangic=date(2026, 10, 25), proje_adi="Günlük <Rapor>")
        self.assertIn("<StartBoundary>2026-10-25T08:05:00</StartBoundary>", xml)
        self.assertIn("zamanli --proje gunluk</Arguments>", xml)
        self.assertIn("Günlük &lt;Rapor&gt;", xml)
        self.assertTrue(zamanlayici.bizim_mi("gunluk", xml))
        self.assertFalse(zamanlayici.bizim_mi("aylik", xml))  # başka projenin görevi değil
        self.assertFalse(zamanlayici.bizim_mi("gunluk", xml.replace("zamanli --proje gunluk", "calistir x")))
        self.assertEqual(zamanlayici.gorev_adi("gunluk"), "Analytics Veri Cekme - gunluk")
        with self.assertRaises(zamanlayici.ZamanlamaHatasi):
            zamanlayici.gorev_adi("../kotu")

    def test_durum_kurulum_ve_baska_goreve_dokunmama(self):
        xml = zamanlayici.gorev_xml("genel", {"saat": "08:05", "gunler": ["MON"]}, zamanlayici.python_yolu())
        kayitli = {"g": None}
        okuyucu = lambda pid: kayitli["g"]
        komutlar = []

        def sahte_calistir(*args):
            komutlar.append(args[:3])
            if args[1] == "/Create":
                kayitli["g"] = {"xml": xml, "sahip": True, "durum": 3, "etkin": True, "sonraki": "2026-10-26T08:05:00+03:00", "sonuc": 0}
            if args[1] == "/Delete":
                kayitli["g"] = None
            return mock.Mock(returncode=0)

        with mock.patch.object(zamanlayici, "_calistir", sahte_calistir):
            self.assertEqual(zamanlayici.durum("genel", okuyucu), {"kurulu": False})
            with self.assertRaises(zamanlayici.ZamanlamaHatasi):
                zamanlayici.simdi_calistir("genel", okuyucu)
            sonuc = zamanlayici.kur("genel", {"saat": "08:05", "gunler": ["MON"]}, okuyucu=okuyucu)
            self.assertTrue(sonuc["kurulu"])
            zamanlayici.simdi_calistir("genel", okuyucu)
            self.assertEqual(zamanlayici.kaldir("genel", okuyucu), {"kurulu": False})
            self.assertEqual([k[1:3] for k in komutlar], [("/Create", "/TN"), ("/Run", "/TN"), ("/Delete", "/TN")])
            kayitli["g"] = {"xml": xml.replace(zamanlayici.uri("genel"), "urn:baska"), "sahip": True}
            self.assertTrue(zamanlayici.durum("genel", okuyucu)["cakisma"])
            with self.assertRaises(zamanlayici.ZamanlamaHatasi):
                zamanlayici.kur("genel", {"saat": "08:05", "gunler": ["MON"]}, okuyucu=okuyucu)
            self.assertEqual(zamanlayici.kaldir("genel", okuyucu), {"kurulu": False})  # yabancı göreve dokunmaz
            self.assertEqual(len(komutlar), 3)
        hata = lambda pid: (_ for _ in ()).throw(OSError("yok"))
        self.assertIsNone(zamanlayici.durum("genel", hata)["kurulu"])
        with self.assertRaises(zamanlayici.ZamanlamaHatasi):
            zamanlayici.kaldir("genel", hata)  # okunamıyorsa silme denenmez


class SunucuDisariVeSheets(SunucuTemel):
    def test_disari_aktar_ve_sheets_uclari(self):
        self.assertEqual(self.istek("POST", "/api/disari-aktar", {})[0], 400)  # önce çalıştırma
        store.save([job(ad="Ülke", boyutlar=["country"])])
        self.assertEqual(self.istek("POST", "/api/calistir", {"kaynak": "sentetik"})[0], 200)
        kod, o = self.istek("POST", "/api/disari-aktar/onizleme", {"duzen": "tablo", "bolme": "is", "etiketler": {"country": "Ülke"}})
        self.assertEqual((kod, [x["ad"] for x in o["sayfalar"]]), (200, ["Ülke"]))
        self.assertIn("Ülke", [c["baslik"] for c in o["sayfalar"][0]["sutunlar"]])
        # Önizlemedeki kaydedilmemiş düzenleme dışa aktarmaya uygulanır; kaydedilince işin şablonu olur
        duzen = {"Ülke": {"tablo": {"sutunlar": ["country", "activeUsers"], "gizli": ["is", "kaynak", "satir"]}}}
        kod, d = self.istek("POST", "/api/disari-aktar", {"hedef": "csv", "sablonlar": duzen, "etiketler": {"country": "Ülke"}})
        self.assertTrue(d["icerik"].lstrip("\ufeff").startswith("Ülke;activeUsers;Dönem"), d["icerik"][:60])
        kod, r = self.istek("POST", "/api/isler/sablon", {"adlar": ["Ülke"], "duzen": "tablo", "sablon": duzen["Ülke"]["tablo"]})
        self.assertEqual((kod, store.load()[0]["sablon"]), (200, duzen["Ülke"]))
        kod, o = self.istek("POST", "/api/disari-aktar/onizleme", {"duzen": "tablo"})
        self.assertEqual(o["sayfalar"][0]["sira"][:2], ["country", "activeUsers"])
        kod, r = self.istek("POST", "/api/isler/sablon", {"adlar": ["Ülke"], "duzen": "tablo", "sablon": None})
        self.assertNotIn("sablon", store.load()[0])
        self.assertEqual(self.istek("POST", "/api/isler/sablon", {"adlar": ["Yok"], "duzen": "tablo", "sablon": None})[0], 400)
        # Sheets listesi bu sayfadan açılıp kapanır; düzenleyicide kaydedilen iş Sheets ayarını kaybetmez
        kod, r = self.istek("POST", "/api/isler/sheets", {"ad": "Ülke", "sheets": {"sekme": "Ülke sekmesi"}})
        self.assertEqual((kod, r["isler"][0]["sheets"]), (200, {"sekme": "Ülke sekmesi"}))
        kod, r = self.istek("POST", "/api/isler/sheets", {"ad": "Ülke", "sheets": None})
        self.assertNotIn("sheets", r["isler"][0])
        kod, d = self.istek("POST", "/api/disari-aktar", {"hedef": "xlsx", "duzen": "tablo", "bolme": "tek", "toplam": True})
        self.assertEqual((kod, d["tur"]), (200, "xlsx"))
        self.assertTrue(d["ad"].startswith("ga4-sentetik-"))  # birleşik: proje adı Netmera projesinin adı
        kod, d = self.istek("POST", "/api/disari-aktar", {"hedef": "csv"})
        self.assertIn("(toplam)", d["icerik"])
        kod, d = self.istek("POST", "/api/disari-aktar", {"hedef": "gsheets"})
        self.assertEqual(kod, 400)
        self.assertIn("Sentetik sayılar", d["error"])
        self.assertEqual(self.istek("POST", "/api/disari-aktar", {"hedef": "pdf"})[0], 400)
        kod, r = self.istek("POST", "/api/sheets/yaz", {})
        self.assertEqual(kod, 400)
        store.save([job(ad="Ülke", boyutlar=["country"], sheets={"sekme": "Ülke"}, etkin=False), job(ad="Yazılmayan")])
        kod, r = self.istek("POST", "/api/sheets/yaz", {})  # kapalı iş de yazılır: Sheets listesi "etkin"den bağımsız
        self.assertEqual(kod, 200)
        self.assertIn("GA4 bağlantısı kurulmadı", r["genel_hata"])  # giriş yok: hiçbir şey yazılmaz, sebep özette
        with mock.patch.object(zamanlayici, "_gorev_oku", lambda pid: None):
            kod, z = self.istek("GET", "/api/zamanlama")
        self.assertEqual((kod, z["gorev"], z["isler"][0]["sekme"], z["isler"][0]["tablo"]), (200, {"kurulu": False}, "Ülke", None))
        self.assertEqual([(x["ad"], x["yaz"]) for x in z["isler"]], [("Ülke", True), ("Yazılmayan", False)])  # bütün işler listelenir
        self.assertEqual(z["son"]["tetikleyen"], "arayuz")  # birleşik: proje adı Netmera projesinin adı
        self.assertEqual(self.istek("POST", "/api/zamanlama/kur", {"saat": "9", "gunler": ["MON"]})[0], 400)
        self.assertEqual(self.istek("GET", "/api/sheets/bilgi?tablo=kotu")[0], 400)


if __name__ == "__main__":
    unittest.main()
