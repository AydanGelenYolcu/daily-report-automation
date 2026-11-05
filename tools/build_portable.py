"""Kurulumsuz (taşınabilir) Windows paketi üretir: dist/NetmeraOtomasyonu-<sürüm>-portable.zip

İçerik: resmî gömülebilir Python (python.org) + sabit sürümlü paketler (PyPI) + uygulama.
Şirket bilgisayarında hiçbir şey kurulmaz; ZIP açılır, NetmeraOtomasyonu.cmd çalıştırılır.
Tarayıcı indirilmez: bilgisayardaki Chrome/Edge kullanılır.
Pakete oturum, kimlik bilgisi, çıktı veya test dosyası girmez.

Kullanım (geliştirme bilgisayarında):  .venv\\Scripts\\python.exe tools\\build_portable.py
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netmera_automation import __version__  # noqa: E402

PY_VERSION = "3.12.10"
PY_TAG = "312"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
CACHE = Path(os.environ["LOCALAPPDATA"]) / "NetmeraOtomasyon-build"
STAGE = CACHE / "stage" / "NetmeraOtomasyonu"
DIST = ROOT / "dist" / f"NetmeraOtomasyonu-{__version__}-portable.zip"
APP_FILES = ["NetmeraOtomasyonu.cmd", "README.md", "BASLA.md", "GOOGLE_SHEETS.md", "requirements.txt", "config/varsayilan-isler.json", "config/people-semasi.json", "config/ga4-ornek-isler.json"]
PIP_TARGET = ["--platform", "win_amd64", "--python-version", PY_VERSION[:4], "--implementation", "cp", "--only-binary=:all:"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remove_stage_tree(path: Path) -> None:
    """Resolve and bound every recursive cleanup to this build's staging tree."""
    stage = STAGE.resolve()
    target = path.resolve()
    if stage != CACHE.resolve() / "stage" / "NetmeraOtomasyonu" or (target != stage and stage not in target.parents):
        raise RuntimeError("Temizlik hedefi bu paketin derleme klasörü dışında.")
    shutil.rmtree(target)


def download_python() -> Path:
    target = CACHE / "indirilen" / f"python-{PY_VERSION}-embed-amd64.zip"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"İndiriliyor: {PY_URL}")
        with urllib.request.urlopen(PY_URL, timeout=120) as response, target.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    print(f"Python ZIP SHA-256: {sha256(target)}")
    return target


def download_wheels() -> Path:
    wheels = CACHE / "indirilen" / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "download", "-q", "-r", str(ROOT / "requirements.txt"),
                    "-d", str(wheels), *PIP_TARGET], check=True)
    return wheels


def build() -> Path:
    python_zip, wheels = download_python(), download_wheels()
    if STAGE.exists():
        remove_stage_tree(STAGE)
    python_dir = STAGE / "python"
    with zipfile.ZipFile(python_zip) as archive:
        archive.extractall(python_dir)
    # ._pth: site-packages ve uygulama klasörü (bir üst klasör) yola eklenir.
    (python_dir / f"python{PY_TAG}._pth").write_text(
        f"python{PY_TAG}.zip\n.\nLib\\site-packages\n..\nimport site\n", encoding="ascii")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-index", "--find-links", str(wheels),
                    "--ignore-requires-python", "--target", str(python_dir / "Lib" / "site-packages"), "-r", str(ROOT / "requirements.txt"),
                    *PIP_TARGET], check=True)
    for name in APP_FILES:
        destination = STAGE / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    shutil.copytree(ROOT / "netmera_automation", STAGE / "netmera_automation",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for cache in STAGE.rglob("__pycache__"):
        remove_stage_tree(cache)

    DIST.parent.mkdir(exist_ok=True)
    DIST.unlink(missing_ok=True)
    with zipfile.ZipFile(DIST, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                archive.write(path, f"NetmeraOtomasyonu/{path.relative_to(STAGE).as_posix()}")
    print(f"Paket: {DIST} ({DIST.stat().st_size / 1e6:.1f} MB)")
    return DIST


if __name__ == "__main__":
    build()
