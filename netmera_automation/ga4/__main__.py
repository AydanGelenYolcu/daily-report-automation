"""Komutlar (varsayılan kaynak sentetik: sayılar uydurmadır, GA4'e bağlanılmaz):
  python -m analytics_cekme dogrula ISLER.json                     iş dosyasını ve metrik/boyut adlarını denetler
  python -m analytics_cekme meta --property PROP_REF [--ara METIN] [--yenile]
  python -m analytics_cekme dene ISLER.json [--is AD]               çalıştırır, dosyaya yazmaz
  python -m analytics_cekme calistir ISLER.json --cikti DOSYA.csv [--is AD]
Her komut --kaynak ga4 ile gerçek property'ye bağlanır (yerel ayar dosyası ve kimlik gerekir).
Gerçek bağlantı kurulumu (hizmet hesabı):
  python -m analytics_cekme kurulum --anahtar İNDİRİLEN.json --property-ref PROP_PROD --property-id 123456789
  python -m analytics_cekme baglanti                                veri çekmeden erişimi doğrular
Arayüz:
  python -m analytics_cekme arayuz [--port 8790] [--no-browser]
Çıkış kodları: 0 başarılı, 2 ayar/iş hatası, 5 bazı işler hatalı, 4 beklenmeyen hata.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import VERSION, cikti, isler, meta, rapor
from .ayarlar import SettingsError
from .istemci import ClientError
from .paths import PathError

ICONS = {"tamam": "✓", "uyari": "⚠", "hata": "✗"}


def make_client(kind: str):
    if kind == "sentetik":
        from .sentetik import SyntheticClient
        return SyntheticClient()
    from . import ayarlar
    from .ga4 import RestClient
    return RestClient(ayarlar.load())


def _number(value) -> str:
    return f"{value:,}".replace(",", ".") if isinstance(value, int) else cikti.format_value(value)


def print_summary(summary: dict):
    for result in summary["sonuclar"]:
        start, end = result["tarih_cozulen"]
        print(f"{ICONS[result['durum']]} {result['is']} ({start}{'' if start == end else ' – ' + end})")
        if result["hata"]:
            print(f"    {result['hata']}")
        for code in result["uyarilar"]:
            print(f"    ⚠ {rapor.WARNINGS[code]}")
        if result.get("toplam"):
            print("    TOPLAM (GA4) → " + ", ".join(f"{m} = {_number(v)}" for m, v in result["toplam"].items()))
        for row in result["satirlar"]:
            prefix = " | ".join(f"{k}={v}" for k, v in row["boyutlar"].items())
            values = ", ".join(f"{m} = {_number(v)}" for m, v in row["metrikler"].items())
            print(f"    {prefix + ' → ' if prefix else ''}{values}")
    print(f"Özet: {summary['tamam']} tamam, {summary['uyari']} uyarılı, {summary['hata']} hatalı.")


def validate(jobs: list[dict], client) -> int:
    failed, cache = 0, {}
    for job in jobs:
        try:
            ref = job["property_ref"]
            if ref not in cache:
                cache[ref] = meta.fetch(client, ref)
            errors = meta.validate(job, client, cache[ref])
        except ClientError as exc:
            errors = [str(exc)]
        failed += bool(errors)
        print(f"{'✗' if errors else '✓'} {job['ad']} — {isler.describe(job)}")
        for error in errors:
            print(f"    {error}")
    print(f"{len(jobs) - failed}/{len(jobs)} iş geçerli.")
    return 2 if failed else 0


def show_metadata(client, ref: str, search: str | None, refresh: bool) -> int:
    data = meta.fetch(client, ref, refresh=refresh)
    needle = (search or "").casefold()
    for title, items in (("Metrikler", data["metrikler"]), ("Boyutlar", data["boyutlar"])):
        names = [n for n in sorted(items) if needle in n.casefold() or needle in str(items[n]).casefold()]
        print(f"{title} ({len(names)}):")
        for name in names:
            info = items[name]
            print(f"  {name}" + (f"  [{info['tur']}]" if isinstance(info, dict) and info.get("tur") else ""))
    return 0


def setup(args) -> int:
    from . import ayarlar
    from .kimlik import eposta_goster
    path, anahtar, bilgi = ayarlar.kurulum(args.anahtar, args.property_ref, args.property_id)
    print(f"✓ Anahtar doğrulandı ve kopyalandı: {anahtar}")
    print(f"✓ Ayar dosyası yazıldı: {path}")
    print(f"ⓘ GA4'te bu hizmet hesabını mülke Görüntüleyen olarak ekle: {eposta_goster(bilgi['eposta'])}")
    if Path(args.anahtar).resolve() != anahtar:
        print(f"⚠ İndirilen asıl dosyayı sil (kopyası artık güvenli konumda): {Path(args.anahtar).resolve()}")
    print("Sonraki adım: python -m analytics_cekme baglanti")
    return 0


def check_connection(client_factory=None) -> int:
    """Veri çekmeden erişimi doğrular: ayar → kimlik → belirteç → her property'nin metrik/boyut listesi."""
    from . import ayarlar
    from .ga4 import RestClient
    from .kimlik import eposta_goster
    settings = ayarlar.load()
    print(f"✓ Ayar dosyası: {ayarlar.settings_path()}")
    if not settings.get("kimlik"):
        raise ClientError("KIMLIK_SECILMEDI")
    client = (client_factory or RestClient)(settings)
    hesap = getattr(client.token_provider, "__self__", None)
    if hesap is not None:
        etiket = "Google hesabı" if settings["kimlik"]["tur"] == "google_hesabi" else "Hizmet hesabı"
        print(f"✓ {etiket}: {eposta_goster(hesap.bilgi()['eposta'])}")
    client.token_provider()
    print("✓ Google erişim belirteci alındı")
    failed = 0
    for ref in settings["propertyler"]:
        try:
            data = client.metadata(ref)
            boyutlar, metrikler = data.get("dimensions") or [], data.get("metrics") or []
            ozel = sum(1 for x in boyutlar + metrikler if x.get("customDefinition"))
            print(f"✓ {ref}: erişim var — {len(boyutlar)} boyut, {len(metrikler)} metrik ({ozel} özel tanım)")
        except ClientError as exc:
            failed += 1
            print(f"✗ {ref}: {exc}")
    return 2 if failed else 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="analytics_cekme", description=f"GA4 veri çekme v{VERSION}")
    parser.add_argument("--kaynak", choices=["sentetik", "ga4"], default="sentetik",
                        help="sentetik (varsayılan, uydurma sayılar) veya ga4 (gerçek property)")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("dogrula", help="İş dosyasını ve metrik/boyut adlarını denetle")
    check.add_argument("isler")
    listing = sub.add_parser("meta", help="Property'nin metrik/boyut listesini göster")
    listing.add_argument("--property", required=True, help="Takma ad, ör. PROP_PROD")
    listing.add_argument("--ara")
    listing.add_argument("--yenile", action="store_true", help="Önbelleği yok say")
    trial = sub.add_parser("dene", help="Çalıştır, dosyaya yazma")
    run = sub.add_parser("calistir", help="Çalıştır ve CSV'ye ekle")
    for command in (trial, run):
        command.add_argument("isler")
        command.add_argument("--is", dest="job_name", help="Yalnız bu adı taşıyan iş")
    run.add_argument("--cikti", required=True, help="CSV dosyası; gerçek sonuçta OneDrive dışında mutlak yol")
    install = sub.add_parser("kurulum", help="Hizmet hesabı anahtarını yerleştir ve ayar dosyasını yaz")
    install.add_argument("--anahtar", required=True, help="Google Cloud'dan indirilen hizmet hesabı JSON dosyası")
    install.add_argument("--property-ref", default="PROP_PROD", help="İşlerde kullanılacak takma ad (varsayılan PROP_PROD)")
    install.add_argument("--property-id", required=True, help="GA4 mülk kimliği (yalnız rakam)")
    sub.add_parser("baglanti", help="Veri çekmeden GA4 erişimini doğrula")
    timed = sub.add_parser("zamanli", help="Projenin Google Sheets'e yazılan etkin işlerini GA4'ten çekip tablosuna yaz (Windows görevi bunu çalıştırır)")
    timed.add_argument("--proje", help="Proje kimliği (verilmezse tüm projeler sırayla)")
    ui = sub.add_parser("arayuz", help="Yerel arayüzü aç")
    ui.add_argument("--port", type=int)
    ui.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "kurulum":
            return setup(args)
        if args.command == "baglanti":
            return check_connection()
        if args.command == "zamanli":
            from . import projeler, zamanli
            kimlikler = [args.proje] if args.proje else [p["id"] for p in projeler.listele()]
            hata = 0
            for pid in kimlikler:
                # Zamanlanmış görevler aynı anda başlarsa sırada bekler
                ozet = zamanli.kayitli_calistir(pid, tetikleyen="zamanlayici", bekle=600)
                if ozet.get("genel_hata"):
                    print(f"✗ [{pid}] {ozet['genel_hata']}", file=sys.stderr)
                    hata += 1
                    continue
                for s in ozet["isler"]:
                    print(f"{ICONS[s['durum']]} [{pid}] {s['is']} → {s['sekme']}: " +
                          (s["mesaj"] if s["durum"] == "hata" else f"{s['guncellenen']} güncellendi, {s['eklenen']} eklendi"))
                hata += ozet["hata"]
            return 5 if hata else 0
        if args.command == "arayuz":
            from .server import serve
            return serve(open_browser=not args.no_browser, port=args.port)
        if args.command == "calistir":
            target = cikti.check_target(Path(args.cikti), args.kaynak)
        client = make_client(args.kaynak)
        if args.kaynak == "sentetik":
            print("ⓘ Kaynak: SENTETİK — sayılar uydurmadır, GA4'e bağlanılmadı.")
        if args.command == "meta":
            return show_metadata(client, args.property, args.ara, args.yenile)
        jobs = isler.load_jobs(args.isler)
        if args.command == "dogrula":
            return validate(jobs, client)
        summary = rapor.run_jobs(isler.select(jobs, args.job_name), client)
        print_summary(summary)
        if args.command == "calistir":
            path, count = cikti.append_csv(target, summary)
            print(f"✓ {count} satır yazıldı: {path}")
        return 5 if summary["hata"] else 0
    except (isler.JobError, SettingsError, PathError, cikti.OutputError, ClientError, ValueError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # zamanlanmış çalışmada sessiz kalmasın
        print(f"✗ Beklenmeyen hata: {type(exc).__name__}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
