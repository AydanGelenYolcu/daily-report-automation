"""Sonuçları CSV'ye ekler. Uzun biçim: her satır bir metrik değeri.

Türkçe Excel'de doğrudan açılsın diye ayırıcı ';', ondalık ',' ve UTF-8 (BOM) kullanılır.
Sentetik ve gerçek sonuçlar aynı dosyada karıştırılmaz; gerçek sonuç OneDrive'a yazılmaz.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .paths import check_local_path

COLUMNS = ["calisma", "zaman", "kaynak", "is", "property_ref", "tarih_baslangic", "tarih_bitis", "saat_dilimi",
           "kirilim", "metrik", "deger", "durum", "uyarilar", "hata"]
STATUS = {"tamam": "TAMAM", "uyari": "UYARI", "hata": "HATA"}


class OutputError(ValueError):
    """Çıktı dosyasına yazılamadı."""


def format_value(value) -> str:
    return str(value) if isinstance(value, int) else format(value, ".10g").replace(".", ",")


def check_target(path: Path, kaynak: str) -> Path:
    """Çalıştırmadan önce çağrılır: hedef uygun değilse GA4'e hiç istek gitmez."""
    path = Path(path)
    path = path.resolve() if kaynak == "sentetik" else check_local_path(path)
    if path.suffix.lower() != ".csv":
        raise OutputError("Çıktı dosyası .csv uzantılı olmalı.")
    if path.exists() and path.stat().st_size:
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, delimiter=";")
                header = next(reader, None)
                sources = {row[2] for row in reader if len(row) > 2}
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise OutputError(f"Mevcut CSV okunamadı ({type(exc).__name__}); dosya açıksa kapat veya yeni dosya seç.") from None
        if header != COLUMNS:
            raise OutputError("Mevcut CSV'nin başlıkları bu uygulamanınkiyle aynı değil; yeni bir dosya seç.")
        if sources - {kaynak}:
            raise OutputError(f"Bu dosyada '{', '.join(sorted(sources - {kaynak}))}' kaynaklı satırlar var; "
                              "sentetik ve gerçek sonuçlar aynı dosyada karıştırılmaz.")
    return path


def rows(summary: dict) -> list[dict]:
    out = []
    for result in summary["sonuclar"]:
        base = {"calisma": summary["calisma"], "zaman": summary["zaman"], "kaynak": summary["kaynak"], "is": result["is"],
                "property_ref": result["property_ref"], "tarih_baslangic": result["tarih_cozulen"][0],
                "tarih_bitis": result["tarih_cozulen"][1], "saat_dilimi": result["saat_dilimi"] or "",
                "durum": STATUS[result["durum"]], "uyarilar": ",".join(result["uyarilar"]), "hata": result["hata"] or ""}
        if result["durum"] == "hata":
            out.append({**base, "kirilim": "", "metrik": "", "deger": ""})
            continue
        # GA4'ün kendi toplamı ayrı satır: satırlar toplanırsa tekil kullanıcı fazla/eksik sayılır
        for metric, value in (result.get("toplam") or {}).items():
            out.append({**base, "kirilim": "(toplam)", "metrik": metric, "deger": format_value(value)})
        for row in result["satirlar"]:
            breakdown = " | ".join(f"{k}={v}" for k, v in row["boyutlar"].items())
            for metric, value in row["metrikler"].items():
                out.append({**base, "kirilim": breakdown, "metrik": metric, "deger": format_value(value)})
    return out


def append_csv(path: Path, summary: dict) -> tuple[Path, int]:
    path = check_target(path, summary["kaynak"])
    lines = rows(summary)
    new = not path.exists() or not path.stat().st_size
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, COLUMNS, delimiter=";")
            if new:
                writer.writeheader()
            writer.writerows(lines)
    except PermissionError:
        raise OutputError(f"CSV yazılamadı; dosya Excel'de açık olabilir. Kapatıp yeniden dene: {path}") from None
    except OSError as exc:
        raise OutputError(f"CSV yazılamadı ({type(exc).__name__}): {path}") from None
    return path, len(lines)
