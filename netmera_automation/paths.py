"""Uygulamanın veri klasörü. Kod klasörü ve OneDrive dışında tutulur."""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
TIMEZONE = "Europe/Istanbul"


class PathError(ValueError):
    """Çıktı/oturum konumu güvenlik kuralına uymuyor."""


def check_local_path(path: Path) -> Path:
    """Mutlak, yerel ve proje/OneDrive dışı bir yol döndürür."""
    if not path.is_absolute():
        raise PathError("Konum mutlak dosya yolu olmalı (ör. C:/Raporlar/netmera.xlsx).")
    path = path.resolve()
    prohibited = [APP_ROOT]
    prohibited += [Path(os.environ[k]).resolve() for k in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer") if os.environ.get(k)]
    if any(path == base or base in path.parents for base in prohibited) or any(p.casefold().startswith("onedrive") for p in path.parts):
        raise PathError("Gerçek çıktı ve tarayıcı oturumu uygulama klasöründe veya OneDrive içinde tutulamaz.")
    return path


def data_root() -> Path:
    """%LOCALAPPDATA%\\NetmeraOtomasyon (testlerde NETMERA_OTOMASYON_DATA ile değişir)."""
    override = os.environ.get("NETMERA_OTOMASYON_DATA")
    if override:
        return check_local_path(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise PathError("Windows LOCALAPPDATA konumu bulunamadı.")
    return check_local_path(Path(local) / "NetmeraOtomasyon")
