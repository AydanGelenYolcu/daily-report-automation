"""Windows Görev Zamanlayıcı kaydı: her proje için seçilen gün ve saatte `python -m analytics_cekme zamanli --proje <kimlik>`.

Netmera Otomasyonu'ndaki kalıbın sadeleştirilmiş kopyası. Yönetici yetkisi gerekmez; görev yalnız kullanıcı oturumu
açıkken çalışır. Bilgisayar o saatte kapalı/uykudaysa açılınca bir kez çalışır (StartWhenAvailable).
Görev adı proje kimliğiyle kurulur (proje yeniden adlandırılsa da değişmez). Uygulama yalnız kendi oluşturduğu görevi
(adı, URI'si, komutu ve sahibi eşleşen) değiştirir veya siler. Gün/saat ayarı projenin dosyasında tutulur (projeler.py).
"""
from __future__ import annotations

import base64
from datetime import date
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

from .paths import APP_ROOT

GUNLER = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday", "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}
VARSAYILAN_AYAR = {"saat": "09:30", "gunler": list(GUNLER)}
SAAT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
KIMLIK = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
NO_WINDOW = 0x08000000
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class ZamanlamaHatasi(ValueError):
    """Görev ayarı geçersiz ya da Windows görevi değiştirilemedi."""


def _kimlik(pid: str) -> str:
    if not isinstance(pid, str) or not KIMLIK.match(pid):
        raise ZamanlamaHatasi("Proje kimliği geçersiz.")
    return pid


def gorev_adi(pid: str) -> str:
    return f"Analytics Veri Cekme - {_kimlik(pid)}"


def uri(pid: str) -> str:
    return f"urn:analytics-veri-cekme:zamanli:{_kimlik(pid)}"


def argumanlar(pid: str) -> str:
    return f"-m analytics_cekme zamanli --proje {_kimlik(pid)}"


def ayar_dogrula(raw) -> dict:
    if not isinstance(raw, dict):
        raise ZamanlamaHatasi("Zamanlama ayarı geçersiz.")
    saat = str(raw.get("saat", "")).strip()
    if not SAAT.match(saat):
        raise ZamanlamaHatasi("Saat SS:DD biçiminde olmalı (ör. 09:30).")
    gunler = raw.get("gunler")
    if not isinstance(gunler, list) or not gunler or any(g not in GUNLER for g in gunler):
        raise ZamanlamaHatasi("En az bir gün seç.")
    return {"saat": saat, "gunler": [g for g in GUNLER if g in gunler]}


def python_yolu() -> Path:
    exe = Path(sys.executable)
    penceresiz = exe.with_name("pythonw.exe")  # görev çalışırken siyah pencere açılmasın
    return penceresiz if penceresiz.is_file() else exe


def gorev_xml(pid: str, ayar: dict, python: Path, kok: Path = APP_ROOT, baslangic: date | None = None, proje_adi: str = "") -> str:
    ayar = ayar_dogrula(ayar)
    gunler = "".join(f"<{GUNLER[g]} />" for g in ayar["gunler"])
    kullanici = escape(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\"))
    aciklama = escape(f"Analytics Veri Çekme — {proje_adi or pid}: Google Sheets hedefi olan etkin işleri GA4'ten çekip tablolara yazar.")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{NS['t']}">
  <RegistrationInfo>
    <URI>{uri(pid)}</URI>
    <Description>{aciklama}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{(baslangic or date.today()).isoformat()}T{ayar['saat']}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek><DaysOfWeek>{gunler}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{kullanici}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{argumanlar(pid)}</Arguments>
      <WorkingDirectory>{escape(str(kok))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def bizim_mi(pid: str, xml: str) -> bool:
    """Görev bu uygulamanın bu projesine mi ait: URI, tek Exec eylemi, python(w).exe ve aynı argümanlar."""
    try:
        kok = ET.fromstring(xml)
        eylemler = kok.find("t:Actions", NS)
        if eylemler is None or len(eylemler) != 1 or eylemler[0].tag != "{" + NS["t"] + "}Exec":
            return False
        komut = eylemler[0].findtext("t:Command", default="", namespaces=NS)
        arg = eylemler[0].findtext("t:Arguments", default="", namespaces=NS)
        return (kok.findtext("t:RegistrationInfo/t:URI", default="", namespaces=NS) == uri(pid)
                and PureWindowsPath(komut).name.casefold() in ("python.exe", "pythonw.exe") and arg.split() == argumanlar(pid).split())
    except ET.ParseError:
        return False


def _calistir(*args) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, encoding="oem" if args[0] == "schtasks" else "utf-8",
                          errors="replace", timeout=20, creationflags=NO_WINDOW if os.name == "nt" else 0)


def _gorev_oku(pid: str) -> dict | None:
    """Görevi PowerShell COM ile okur (Windows dilinden bağımsız). Yoksa None."""
    betik = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function tarih($d) { if ($d.Year -ge 2000) { $d.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null } }
$s = New-Object -ComObject 'Schedule.Service'; $s.Connect()
try { $t = $s.GetFolder('\').GetTask(__AD__) } catch { @{var=$false} | ConvertTo-Json -Compress; exit }
$ben = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$sahip = $false
try { $u = [string]$t.Definition.Principal.UserId
      $sid = if ($u.StartsWith('S-1-')) { $u } else { (New-Object Security.Principal.NTAccount($u)).Translate([Security.Principal.SecurityIdentifier]).Value }
      $sahip = $sid -eq $ben } catch { $sahip = $false }
@{var=$true; xml=[string]$t.Xml; sahip=[bool]$sahip; durum=[int]$t.State; etkin=[bool]$t.Enabled;
  son=(tarih $t.LastRunTime); sonraki=(tarih $t.NextRunTime); sonuc=[long]$t.LastTaskResult} | ConvertTo-Json -Compress
""".replace("__AD__", "'" + gorev_adi(pid).replace("'", "''") + "'")
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    sonuc = _calistir(str(exe), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
                      base64.b64encode(betik.encode("utf-16-le")).decode("ascii"))
    if sonuc.returncode:
        raise ZamanlamaHatasi("Windows görev durumu okunamadı.")
    veri = json.loads(sonuc.stdout.lstrip("\ufeff").strip())
    return veri if veri.get("var") else None


def durum(pid: str, okuyucu=None) -> dict:
    try:
        g = (okuyucu or _gorev_oku)(pid)
    except (OSError, ValueError, ZamanlamaHatasi, subprocess.TimeoutExpired):
        return {"kurulu": None, "hata": "Windows görev durumu okunamadı (Görev Zamanlayıcı erişimini kontrol et)."}
    if not g:
        return {"kurulu": False}
    if not (g.get("sahip") and bizim_mi(pid, g.get("xml", ""))):
        return {"kurulu": False, "cakisma": True, "hata": f"'{gorev_adi(pid)}' adlı başka bir görev var; uygulama ona dokunmaz."}
    return {"kurulu": True, "etkin": g.get("etkin"), "calisiyor": g.get("durum") in (2, 4), "son": g.get("son"),
            "sonraki": g.get("sonraki"), "sonuc_kodu": f"0x{int(g.get('sonuc') or 0) & 0xffffffff:08X}"}


def kur(pid: str, ayar: dict, *, proje_adi: str = "", okuyucu=None) -> dict:
    ayar = ayar_dogrula(ayar)
    mevcut = durum(pid, okuyucu)
    if mevcut.get("kurulu") is None or mevcut.get("cakisma"):
        raise ZamanlamaHatasi(mevcut.get("hata") or "Görev durumu okunamadı; kurulum yapılmadı.")
    with tempfile.TemporaryDirectory(prefix="analytics-gorev-") as klasor:
        yol = Path(klasor) / "gorev.xml"
        yol.write_text(gorev_xml(pid, ayar, python_yolu(), proje_adi=proje_adi), encoding="utf-16")
        if _calistir("schtasks", "/Create", "/TN", gorev_adi(pid), "/XML", str(yol), "/F").returncode:
            raise ZamanlamaHatasi("Windows görevi oluşturulamadı. Yetki veya kurum politikasını kontrol et.")
    sonra = durum(pid, okuyucu)
    if not sonra.get("kurulu"):
        raise ZamanlamaHatasi("Görev oluşturuldu ama doğrulanamadı; Görev Zamanlayıcı'yı kontrol et.")
    return sonra


def kaldir(pid: str, okuyucu=None) -> dict:
    """Projenin görevini siler. Görev yoksa sessizce döner; okunamıyorsa ya da başkasınınsa hata verir."""
    mevcut = durum(pid, okuyucu)
    if mevcut.get("kurulu") is None:
        raise ZamanlamaHatasi(mevcut["hata"])
    if not mevcut.get("kurulu"):
        return {"kurulu": False}  # aynı adlı yabancı göreve dokunulmaz
    if _calistir("schtasks", "/Delete", "/TN", gorev_adi(pid), "/F").returncode:
        raise ZamanlamaHatasi("Windows görevi silinemedi.")
    return durum(pid, okuyucu)


def simdi_calistir(pid: str, okuyucu=None) -> dict:
    mevcut = durum(pid, okuyucu)
    if not mevcut.get("kurulu"):
        raise ZamanlamaHatasi("Önce zamanlamayı kur.")
    if mevcut.get("calisiyor"):
        raise ZamanlamaHatasi("Görev zaten çalışıyor.")
    if _calistir("schtasks", "/Run", "/TN", gorev_adi(pid)).returncode:
        raise ZamanlamaHatasi("Windows görevi başlatılamadı.")
    return durum(pid, okuyucu)
