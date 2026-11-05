"""GA4 bölümünün veri klasörü: Netmera Otomasyonu veri klasöründe `ga4\\` (kod klasörü ve OneDrive dışında).

Birleşik uygulamada (2026-10-28) Analytics Veri Çekme'nin ayarları, Google oturumu, metadata önbelleği ve öğrenilen
adlar burada durur. Projeler ve işler Netmera projelerindedir (bkz. projeler.py, store.py).
"""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]


class PathError(ValueError):
    """Çıktı/ayar konumu güvenlik kuralına uymuyor."""


def check_local_path(path: Path) -> Path:
    """Mutlak, yerel ve proje/OneDrive dışı bir yol döndürür."""
    if not path.is_absolute():
        raise PathError("Konum mutlak dosya yolu olmalı (ör. C:/Raporlar/ga4-sonuclar.csv).")
    path = path.resolve()
    prohibited = [APP_ROOT]
    prohibited += [Path(os.environ[k]).resolve() for k in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer") if os.environ.get(k)]
    if any(path == base or base in path.parents for base in prohibited) or any(p.casefold().startswith("onedrive") for p in path.parts):
        raise PathError("Gerçek GA4 çıktısı ve ayarları uygulama klasöründe veya OneDrive içinde tutulamaz.")
    return path


def data_root() -> Path:
    """<Netmera veri klasörü>\\ga4 (testlerde ANALYTICS_CEKME_DATA ile değişir)."""
    override = os.environ.get("ANALYTICS_CEKME_DATA")
    if override:
        return check_local_path(Path(override))
    from ..paths import data_root as netmera_root
    return check_local_path(netmera_root() / "ga4")


def legacy_root() -> Path | None:
    """Ayrı Analytics Veri Çekme uygulamasının veri klasörü (içe aktarma için); yoksa None."""
    local = os.environ.get("LOCALAPPDATA")
    if os.environ.get("NETMERA_GA4_LEGACY"):
        return Path(os.environ["NETMERA_GA4_LEGACY"])
    return Path(local) / "AnalyticsVeriCekme" if local else None
