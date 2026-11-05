import contextlib
import csv
import io
import json
import unittest

from netmera_automation.ga4 import ayarlar, cikti
from netmera_automation.ga4.__main__ import main
from tests.ga4.helpers import SAMPLE, TempDataCase, job


class CliTests(TempDataCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def jobs_file(self, *items):
        path = self.tmp / "isler.json"
        path.write_text(json.dumps({"isler": list(items)}), encoding="utf-8")
        return str(path)

    def read_csv(self, path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle, delimiter=";"))

    def test_validate_sample_and_bad_names(self):
        code, out, _ = self.run_cli("dogrula", str(SAMPLE))
        self.assertEqual(code, 0)
        self.assertIn("SENTETİK", out)
        self.assertIn("4/4 iş geçerli", out)
        code, out, _ = self.run_cli("dogrula", self.jobs_file(job(metrikler=["activeUser"])))
        self.assertEqual(code, 2)
        self.assertIn("Benzer: activeUsers", out)

    def test_trial_prints_without_writing(self):
        code, out, _ = self.run_cli("dene", str(SAMPLE), "--is", "cihaza göre oturum")
        self.assertEqual(code, 0)
        self.assertIn("deviceCategory=mobile → sessions = ", out)
        self.assertIn("Özet: 1 tamam", out)
        self.assertEqual(list(self.tmp.glob("**/*.csv")), [])

    def test_run_appends_csv_once_with_header(self):
        target = self.tmp / "cikti" / "sonuc.csv"
        self.assertEqual(self.run_cli("calistir", str(SAMPLE), "--cikti", str(target))[0], 0)
        self.assertEqual(self.run_cli("calistir", str(SAMPLE), "--cikti", str(target))[0], 0)
        lines = self.read_csv(target)
        self.assertEqual(lines[0], cikti.COLUMNS)
        self.assertEqual(sum(line == cikti.COLUMNS for line in lines), 1)
        body = lines[1:]
        self.assertEqual({line[2] for line in body}, {"sentetik"})
        self.assertEqual(len({line[0] for line in body}), 2)  # iki ayrı çalışma kimliği
        rate = next(line for line in body if line[9] == "engagementRate")
        self.assertIn(",", rate[10])

    def test_failed_job_is_marked_not_zero(self):
        target = self.tmp / "sonuc.csv"
        code, out, _ = self.run_cli("calistir", self.jobs_file(job(), job(ad="Yetkisiz", property_ref="SENTETIK_YETKI")),
                                    "--cikti", str(target))
        self.assertEqual(code, 5)
        self.assertIn("Görüntüleyen", out)
        failed = [line for line in self.read_csv(target)[1:] if line[3] == "Yetkisiz"]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0][10], failed[0][11]), ("", "HATA"))

    def test_sources_are_not_mixed(self):
        target = self.tmp / "sonuc.csv"
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(cikti.COLUMNS)
            writer.writerow(["x", "z", "ga4"] + [""] * (len(cikti.COLUMNS) - 3))
        code, _, err = self.run_cli("calistir", str(SAMPLE), "--cikti", str(target))
        self.assertEqual(code, 2)
        self.assertIn("karıştırılmaz", err)
        other = self.tmp / "baska.csv"
        other.write_text("a;b\n", encoding="utf-8")
        self.assertEqual(self.run_cli("calistir", str(SAMPLE), "--cikti", str(other))[0], 2)
        self.assertEqual(self.run_cli("calistir", str(SAMPLE), "--cikti", str(self.tmp / "sonuc.txt"))[0], 2)

    def test_real_source_needs_settings_auth_and_safe_output(self):
        code, _, err = self.run_cli("--kaynak", "ga4", "dene", str(SAMPLE))
        self.assertEqual(code, 2)
        self.assertIn("ayar dosyası yok", err)
        path = ayarlar.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"propertyler": {"PROP_PROD": {"property_id": "1"}}}), encoding="utf-8")
        code, out, _ = self.run_cli("--kaynak", "ga4", "dene", str(SAMPLE))
        self.assertEqual(code, 5)
        self.assertIn("Google hesabını bağla", out)
        code, _, err = self.run_cli("--kaynak", "ga4", "calistir", str(SAMPLE), "--cikti", str(self.tmp / "OneDrive" / "s.csv"))
        self.assertEqual(code, 2)
        self.assertIn("OneDrive", err)

    def test_metadata_listing(self):
        code, out, _ = self.run_cli("meta", "--property", "PROP_TEST", "--ara", "users")
        self.assertEqual(code, 0)
        self.assertIn("activeUsers  [TYPE_INTEGER]", out)
        self.assertNotIn("sessions", out)


if __name__ == "__main__":
    unittest.main()
