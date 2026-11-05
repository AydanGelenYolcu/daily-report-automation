"""Arayüzün JavaScript'i sözdizimi hatasıyla yüklenmemeli; aksi halde hiçbir proje görünmez/tıklanmaz.

Node yüklü değilse test atlanır; geliştirme bilgisayarında Node ile çalışır.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "netmera_automation" / "ui" / "index.html"


def script_source() -> str:
    match = re.search(r"<script>(.*)</script>", HTML.read_text(encoding="utf-8"), re.S)
    if not match:
        raise AssertionError("index.html içinde <script> yok")
    return match.group(1).replace("__TOKEN__", "test-token")


@unittest.skipUnless(shutil.which("node"), "Node yüklü değil")
class UiScriptTests(unittest.TestCase):
    def test_script_parses(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ui.js"
            path.write_text(script_source(), encoding="utf-8")
            result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
