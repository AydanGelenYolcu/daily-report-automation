"""İsteğe bağlı Windows bildirimi. Ek kurulum gerektirmez; şirket ayarı engellerse sessizce vazgeçer."""
from __future__ import annotations

import os
import subprocess

# Metin ortam değişkeniyle geçer: başlık/metin komut olarak yorumlanmaz.
SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:NETMERA_BILDIRIM_BASLIK)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:NETMERA_BILDIRIM_METIN)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def show(title: str, text: str) -> bool:
    """Bildirimi Windows'a iletir. True: Windows kabul etti (odak modu yine gizleyebilir)."""
    if os.name != "nt":
        return False
    env = {**os.environ, "NETMERA_BILDIRIM_BASLIK": title[:120], "NETMERA_BILDIRIM_METIN": text[:240]}
    try:
        from .scheduler import NO_WINDOW
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", SCRIPT],
                                env=env, capture_output=True, timeout=20, creationflags=NO_WINDOW)
        return result.returncode == 0
    except Exception:
        return False


def run_text(summary: dict | None, failure: str | None) -> tuple[str, str]:
    """Zamanlanmış çalışmanın bildirim başlığı ve tek satırlık özeti."""
    if failure or not summary:
        return "Netmera Otomasyonu — çalışma hatalı", failure or "Çalışma tamamlanmadı; uygulamada Zamanlama sekmesine bakın."
    text = f"{summary['ok']} başarılı, {summary['failed']} hatalı"
    catch = summary.get("catch_up")
    if catch:
        text += f" · kaçan {len(catch['days'])} gün tamamlandı"
    if summary.get("write") and summary.get("excel"):
        text += f" · {summary['excel']}"
    problem = summary["failed"] or (summary.get("write") and not summary.get("excel_written"))
    return ("Netmera Otomasyonu — kontrol et" if problem else "Netmera Otomasyonu — tamam"), text
