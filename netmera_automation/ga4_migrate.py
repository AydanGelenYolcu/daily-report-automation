"""Ayrı Analytics Veri Çekme uygulamasının verisini birleşik uygulamaya bir kez aktarır (kullanıcı kararı 2026-10-28).

Kaynak `%LOCALAPPDATA%\\AnalyticsVeriCekme\\` yalnız okunur; hiçbir dosyası değiştirilmez veya silinmez.
- Bağlantı: ayarlar.json (mülkler, giriş yöntemi), Google istemcisi ve oturumu (DPAPI, aynı Windows kullanıcısı),
  hizmet hesabı anahtarı, öğrenilmiş GA4 adları ve metadata önbelleği → <Netmera veri klasörü>\\ga4\\.
- Projeler: her Analytics projesi işleri, GA4 Sheets ayarları, paketleri ve zamanlama saatiyle yeni bir Netmera
  projesi olur (ad çakışırsa "(GA4)" eklenir). Windows görevi kurulmaz; Zamanlama sayfasından kurulur.
Sonuç `ga4\\ice-aktarim.json` dosyasına yazılır ve aktarım bir daha yapılmaz.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil

from . import store
from .ga4 import isler as G
from .ga4.paths import data_root as ga4_root, legacy_root

FLAG = "ice-aktarim.json"
FILES = ("ayarlar.json", "google-istemci.json", "google-oturum.json", "ga4-adlar.json", "baglanti-durumu.json")
FOLDERS = ("anahtar", "meta")


def done() -> dict | None:
    try:
        return json.loads((ga4_root() / FLAG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _copy_settings(src: Path, dst: Path) -> list[str]:
    copied = []
    for name in FILES:
        if (src / name).is_file() and not (dst / name).exists():
            shutil.copy2(src / name, dst / name)
            copied.append(name)
    for name in FOLDERS:
        if (src / name).is_dir() and not (dst / name).exists():
            shutil.copytree(src / name, dst / name)
            copied.append(name + "\\")
    settings = dst / "ayarlar.json"
    if "ayarlar.json" in copied:
        # Giriş dosyalarının yolu eski klasörü gösterir: aynı adlı kopyaya çevrilir (eski dosyalar yerinde kalır).
        data = json.loads(settings.read_text(encoding="utf-8"))
        kimlik = data.get("kimlik") or {}
        for key in ("istemci_dosyasi", "anahtar_dosyasi"):
            if isinstance(kimlik.get(key), str):
                old = Path(kimlik[key])
                try:
                    rel = old.resolve().relative_to(src.resolve())
                except ValueError:
                    continue
                kimlik[key] = str(dst / rel)
        settings.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return copied


def _project(folder: Path, log) -> dict | None:
    try:
        info = json.loads((folder / "proje.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log(f"   ✗ {folder.name}: proje.json okunamadı, atlandı")
        return None
    jobs, skipped = [], []
    try:
        raw_jobs = json.loads((folder / "isler.json").read_text(encoding="utf-8-sig")).get("isler") or []
    except (OSError, ValueError, AttributeError):
        raw_jobs = []
    for raw in raw_jobs:
        try:
            jobs.append(G.normalize_job(raw))
        except G.JobError as exc:
            skipped.append(f"{raw.get('ad', '?') if isinstance(raw, dict) else '?'}: {exc}")
    names = {p["name"].casefold() for p in store.list_profiles()}
    name = str(info.get("ad") or folder.name).strip()[:60] or folder.name
    if name.casefold() in names:
        name = (name[:54] + " (GA4)")
    profile = store.create_profile(name)
    profile["jobs"] = [{"type": "ga4", "label": j["ad"], "enabled": j["etkin"],
                        "ga4": {k: v for k, v in j.items() if k not in ("ad", "etkin")}} for j in jobs]
    profile["ga4"] = {"aciklama": info.get("aciklama") or "", "sheets": info.get("sheets"), "paketler": info.get("paketler") or []}
    schedule = info.get("zamanlama")
    if isinstance(schedule, dict) and schedule.get("saat") and schedule.get("gunler"):
        profile["schedule"] = {**profile["schedule"], "times": [schedule["saat"]], "days": list(schedule["gunler"])}
    try:
        profile = store.save_profile(profile)
    except ValueError as exc:  # örn. iki işin sütun adı çakışıyor: işler olmadan proje yine açılır, nedeni bildirilir
        skipped.append(f"işler kaydedilemedi: {exc}")
        profile["jobs"] = []
        profile = store.save_profile(profile)
    log(f"   ✓ '{info.get('ad', folder.name)}' → '{profile['name']}' ({len(profile['jobs'])} GA4 işi)")
    for item in skipped:
        log(f"     ⓘ atlandı: {item}")
    return {"kaynak": str(info.get("ad") or folder.name), "proje": profile["id"], "ad": profile["name"],
            "is": len(profile["jobs"]), "atlanan": skipped}


def import_legacy(log=print) -> dict:
    """Bir kez çalışır. Eski veri yoksa da işaret dosyası yazılır (her açılışta yeniden aranmaz)."""
    previous = done()
    if previous is not None:
        return previous
    src = legacy_root()
    dst = ga4_root()
    dst.mkdir(parents=True, exist_ok=True)
    result = {"zaman": datetime.now().astimezone().isoformat(timespec="seconds"), "kaynak": str(src) if src else None,
              "dosyalar": [], "projeler": []}
    if src and src.is_dir():
        log(f"Analytics Veri Çekme verisi içe aktarılıyor: {src}")
        result["dosyalar"] = _copy_settings(src, dst)
        root = src / "projeler"
        for folder in sorted(root.iterdir()) if root.is_dir() else []:
            if folder.is_dir() and (folder / "proje.json").is_file():
                item = _project(folder, log)
                if item:
                    result["projeler"].append(item)
        if result["projeler"]:
            log("ⓘ Eski Analytics Windows görevi kuruluysa, yeni projelerde sonuçları doğruladıktan sonra eski görevi kaldırın "
                "(aksi hâlde aynı Sheets sekmesine iki uygulama yazar).")
    (dst / FLAG).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
