"""Hesaplama işi: güvenli formül, hücre okuma ve çalışmada yazım. Yalnız sentetik veri ve geçici dosyalar."""
from datetime import date, datetime, timedelta
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.utils.cell import range_boundaries

from netmera_automation import formula as F, jobs as J, runner, store
from tests.helpers import TempDataCase, event_job


class EvaluatorTests(unittest.TestCase):
    def test_arithmetic_precedence_and_no_rounding(self):
        self.assertEqual(F.evaluate("(A + B) / 4", {"A": 25, "B": 5}), 7.5)
        self.assertEqual(F.evaluate("A + B * 2 - 1", {"A": 1, "B": 3}), 6)
        self.assertEqual(F.evaluate("-A / 3", {"A": 1}), -1 / 3)
        self.assertIsInstance(F.evaluate("(A + B) / 2", {"A": 25, "B": 5}), int)  # 15.0 → 15
        self.assertEqual(F.evaluate("A × 2 ÷ 4", {"A": 10}), 5)

    def test_only_safe_expressions_are_accepted(self):
        for bad in ["A ** 2", "__import__('os')", "A.real", "A if B else 2", "max(A, B)", "a + 1", "AB + 1", "2,5 * A", "A +", "[A]"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                F.names(bad)
        with self.assertRaises(ValueError):
            F.evaluate("A / (B - 5)", {"A": 1, "B": 5})

    def test_cell_values_to_numbers(self):
        cases = {5: 5, 2.5: 2.5, "25": 25, "1.234": 1234, "1.234.567": 1234567, "7,5": 7.5, "1.234,5": 1234.5, "-3": -3, "2.75": 2.75}
        for raw, want in cases.items():
            self.assertEqual(F.to_number(raw, "X"), want)
        for bad in (None, "", "  ", "abc", True, datetime(2026, 1, 1), "12%", 10 ** 400, "9" * 400, "9" * 400 + ".5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                F.to_number(bad, "X")
        with self.assertRaises(ValueError) as error:
            F.to_number("sentetik-ozel-hucre-icerigi", "Rapor!A1")
        self.assertNotIn("sentetik-ozel-hucre-icerigi", str(error.exception))

    def test_arithmetic_overflow_is_a_formula_error(self):
        for expression, values in [("A * A", {"A": 10 ** 300}), ("A * 2", {"A": 1e308}),
                                   ("A / 1", {"A": 10 ** 400})]:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                F.evaluate(expression, values)


def formula_job(**changes):
    job = {"type": "formula", "label": "Sentetik Hesap", "expression": "(A + B) / 2",
           "variables": [{"name": "A", "kind": "job", "job_id": "sentetik_olay"},
                         {"name": "B", "kind": "cell", "source": "output", "sheet": "Rapor", "mode": "fixed_cell", "cell": "C5"}]}
    job.update(changes)
    return job


class FormulaJobTests(TempDataCase):
    def test_validation_messages(self):
        cases = [({"expression": "A + C"}, "C tanımlı değil"),
                 ({"expression": "A * 2"}, "B formülde kullanılmıyor"),
                 ({"variables": [{"name": "A", "kind": "job", "job_id": "x"}, {"name": "A", "kind": "job", "job_id": "y"}], "expression": "A"}, "iki kez"),
                 ({"variables": [{"name": "A", "kind": "cell", "source": "output", "sheet": "R", "mode": "fixed_cell", "cell": "5C"}], "expression": "A"}, "C5 gibi"),
                 ({"variables": [{"name": "A", "kind": "cell", "source": "google_sheets", "sheets_id": "", "sheet": "R", "mode": "fixed_cell", "cell": "C5"}], "expression": "A"}, "Google")]
        for change, text in cases:
            with self.subTest(change=change), self.assertRaises(J.JobError) as ctx:
                J.normalize_job(formula_job(**change))
            self.assertIn(text, str(ctx.exception))

    def test_profile_checks_referenced_jobs(self):
        store.ensure_defaults()
        p = store.load_profile("demo-sahte-veri")
        p["jobs"] = [event_job(), formula_job()]
        store.save_profile(p)
        p["jobs"] = [formula_job()]
        with self.assertRaises(J.JobError):
            J.normalize_profile(p)  # başvurulan iş yok
        p["jobs"] = [event_job(), formula_job(variables=[{"name": "A", "kind": "job", "job_id": "sentetik_olay", "column": "iOS"},
                                                       formula_job()["variables"][1]])]
        with self.assertRaises(J.JobError):
            J.normalize_profile(p)  # işte iOS sütunu yok


class GoogleSourceReadinessTests(TempDataCase):
    def test_google_read_needs_connection_even_with_local_excel_output(self):
        from netmera_automation import server
        store.ensure_defaults()
        p = store.load_profile("production")
        google = {"name": "B", "kind": "cell", "source": "google_sheets", "sheets_id": "a" * 20, "sheet": "b",
                  "mode": "date_row", "date_column": "B", "value_column": "C"}
        p.update(netmera_app="Sentetik", output_target="excel", jobs=[event_job(), formula_job(variables=[formula_job()["variables"][0], google])])
        p = store.save_profile(p)
        self.assertTrue(any("Google Sheet’ten okuyor" in m for m in server.readiness(p)))
        with mock.patch.object(server.google_auth, "status", return_value={"connected": True}):
            self.assertFalse(any("Google" in m for m in server.readiness(p)))


class GoogleFormulaSourcesTests(TempDataCase):
    """Sahte Google yanıtları: sayı/tarih ayrımı ve yalnız gerekli aralıkların okunması."""
    def setUp(self):
        super().setUp()
        from netmera_automation import google_auth
        self.title, self.sid = "Rapor", "synthetic_sheet_12345"
        self.values, self.formats, self.calls = {(1, 1): 25000}, {}, []
        self.source = F.Sources({"id": "sentetik", "source": "netmera"})
        self.addCleanup(self.source.close)
        for target, kw in [("status", {"return_value": {"connected": True}}),
                           ("access_token", {"return_value": "synthetic-token"}),
                           ("request_json", {"side_effect": self.request})]:
            patcher = mock.patch.object(google_auth, target, **kw)
            patcher.start(); self.addCleanup(patcher.stop)

    def var(self, **changes):
        return {"source": "google_sheets", "sheets_id": self.sid, "sheet": self.title,
                "mode": "fixed_cell", "cell": "A1", **changes}

    def request(self, url, **kwargs):
        self.assertEqual(kwargs.get("method", "GET"), "GET")
        query = parse_qs(urlsplit(url).query)
        self.calls.append(query)
        ranges = query.get("ranges", [])
        if not ranges:
            return {"spreadsheetId": self.sid, "sheets": [{"properties": {
                "title": self.title, "gridProperties": {"rowCount": 100, "columnCount": 10}}}]}
        area = ranges[0].split("!")[-1] if "!" in ranges[0] else "A1:J100"
        c1, r1, c2, r2 = range_boundaries(area)
        if "/values:batchGet" in url:
            return {"valueRanges": [{"values": [[self.values.get((r, c), "") for c in range(c1, c2 + 1)]
                                                  for r in range(r1, r2 + 1)]}]}
        raw = self.values.get((r1, c1))
        effective = ({"boolValue": raw} if isinstance(raw, bool) else {"numberValue": raw}
                     if isinstance(raw, (int, float)) else {"stringValue": raw} if raw is not None else {})
        cell = {"effectiveValue": effective, "effectiveFormat": {"numberFormat": {"type": self.formats.get((r1, c1), "NUMBER")}}}
        return {"spreadsheetId": self.sid, "sheets": [{"properties": {"title": self.title}, "data": [
            {"startRow": r1 - 1, "startColumn": c1 - 1, "rowData": [{"values": [cell]}]}]}]}

    def test_large_count_is_a_number_and_fixed_cell_read_is_bounded_and_cached(self):
        self.assertEqual(self.source.read(self.var(), date(2026, 10, 24)), (25000, "Rapor!A1"))
        self.assertEqual(self.source.read(self.var(), date(2026, 10, 25))[0], 25000)
        self.assertEqual([q["ranges"] for q in self.calls if "ranges" in q], [["'Rapor'!A1"]])

    def test_date_column_with_time_selects_numeric_metric_without_loading_other_columns(self):
        day = date(2026, 10, 24)
        serial = (day - date(1899, 12, 30)).days
        self.values = {(1, 1): "Date", (2, 1): serial + .75, (3, 1): serial + 1,
                       (2, 2): 46000.5, (3, 2): 2500000}
        var = self.var(mode="date_row", date_column="A", value_column="B")
        self.assertEqual(self.source.read(var, day)[0], 46000.5)
        self.assertEqual(self.source.read(var, day + timedelta(days=1))[0], 2500000)
        self.assertEqual([q["ranges"] for q in self.calls if "ranges" in q],
                         [["'Rapor'!A1:A100"], ["'Rapor'!B2"], ["'Rapor'!B3"]])

    def test_header_lookup_keeps_date_parsing_and_large_numeric_value(self):
        self.values = {(2, 3): "24.10.2026 15:30", (5, 3): 25000}
        var = self.var(mode="header_lookup", header_row=2, value_row=5)
        self.assertEqual(self.source.read(var, date(2026, 10, 24)), (25000, "Rapor!C5"))
        self.assertEqual([q["ranges"] for q in self.calls if "ranges" in q], [["'Rapor'!A2:J2"], ["'Rapor'!C5"]])

    def test_google_date_time_boolean_and_blank_are_not_metric_numbers(self):
        for kind, raw in [("DATE", 25000), ("DATE_TIME", 46000.5), ("TIME", .5), ("NUMBER", True), ("NUMBER", None)]:
            with self.subTest(kind=kind, raw=raw):
                self.source.close()
                self.source = F.Sources({"id": "sentetik", "source": "netmera"})
                self.values, self.formats = {(1, 1): raw}, {(1, 1): kind}
                with self.assertRaises(ValueError):
                    self.source.read(self.var(), date(2026, 10, 24))

    def test_long_sheet_name_and_apostrophe_are_preserved(self):
        self.title = "Sentetik 'rapor' " + "a" * 30
        self.assertEqual(self.source.read(self.var(), date(2026, 10, 24))[1], self.title + "!A1")
        self.assertEqual([q["ranges"] for q in self.calls if "ranges" in q],
                         [["'" + self.title.replace("'", "''") + "'!A1"]])

    def test_duplicate_date_stops_before_reading_metric(self):
        self.values = {(1, 1): "24.10.2026", (2, 1): "24.10.2026", (1, 2): 25000}
        with self.assertRaises(ValueError):
            self.source.read(self.var(mode="date_row", date_column="A", value_column="B"), date(2026, 10, 24))
        self.assertEqual([q["ranges"] for q in self.calls if "ranges" in q], [["'Rapor'!A1:A100"]])

    def test_wrong_file_or_cell_response_is_rejected(self):
        from netmera_automation import google_auth
        metadata = self.request('https://sheets.googleapis.com/metadata')
        for response in ({**metadata, 'spreadsheetId': 'wrong'},
                         {'spreadsheetId': self.sid, 'sheets': []}):
            with self.subTest(response=response), mock.patch.object(google_auth, 'request_json', return_value=response):
                with self.assertRaises(ValueError):
                    self.source.read(self.var(), date(2026, 10, 24))
        # Önce metadata doğrulanır; sonraki yanıt başka hücreyi içerirse değer kullanılmaz.
        wrong = self.request('https://sheets.googleapis.com/grid?' + urlencode({'ranges': "'Rapor'!B2"}))
        with mock.patch.object(google_auth, 'request_json', side_effect=[metadata, wrong]):
            with self.assertRaises(ValueError):
                self.source.read(self.var(), date(2026, 10, 24))

    def test_cell_outside_grid_stops_without_fetching_values(self):
        with self.assertRaises(ValueError):
            self.source.read(self.var(cell='K101'), date(2026, 10, 24))
        self.assertFalse(any('ranges' in q for q in self.calls))


class FormulaRunTests(TempDataCase):
    def test_snapshot_only_formula_does_not_gain_historical_period(self):
        person={'type':'people','label':'Sentetik Anlık','items':[{'field':'Platform','value':'iOS'}]}
        calc=formula_job(expression='A',variables=[{'name':'A','kind':'job','job_id':'sentetik_anlik'}])
        p=self.profile([person,calc])
        result=self.run_it(p,write=False,dates=(date(2026,10,21),date(2026,10,21)))
        self.assertEqual(result['rows'][-1]['status'],'OK')
        self.assertIsNone(result['rows'][-1]['period_start'])

    def test_cell_only_A_uses_selected_day_with_US_headers_78_and_values_79(self):
        from openpyxl.utils import get_column_letter
        book = load_workbook(self.path)
        for day in range(1, 32):
            book['Rapor'].cell(78, day + 2, f'10/{day}/2026')
        book['Rapor']['W79'], book['Rapor']['X79'] = 123.25, 45.5
        book.save(self.path); book.close()
        var = {'name':'A','kind':'cell','source':'excel','file':str(self.path),'sheet':'Rapor',
               'mode':'header_lookup','header_row':78,'value_row':79,'date_basis':'data_day','month_first':True}
        p = self.profile([formula_job(expression='A',variables=[var])])
        planned = datetime(2026,10,30,9,tzinfo=ZoneInfo('Europe/Istanbul'))
        with mock.patch.object(runner,'now',return_value=planned):
            result = self.run_it(p,write=False,job_ids=['sentetik_hesap'],dates=(date(2026,10,21),date(2026,10,21)))
            daily = self.run_it(p,write=False,dates=(date(2026,10,21),date(2026,10,22)),split_days=True)
            ranged = self.run_it(p,write=False,dates=(date(2026,10,21),date(2026,10,22)))
        self.assertEqual((result['rows'][0]['status'],result['rows'][0]['value']),('OK',123.25))
        self.assertIn('W79',result['rows'][0]['note'])
        self.assertTrue(result['rows'][0]['period_start'].startswith('2026-10-21'))
        self.assertEqual(daily['rows'],[])
        self.assertEqual([r['value'] for r in daily['day_rows']],[123.25,45.5])
        self.assertEqual(ranged['rows'][0]['status'],'FORMULA_CELL_INVALID')
        self.assertIn('tek günlük',ranged['rows'][0]['note'])

    def test_cell_only_daily_copy_writes_another_table_and_feeds_formula_chain(self):
        book = load_workbook(self.path)
        book['Rapor']['C78'],book['Rapor']['D78'] = '04/03/2026','04/04/2026'
        book['Rapor']['C79'],book['Rapor']['D79'] = 21.5,22.5
        book.save(self.path);book.close()
        var = {'name':'A','kind':'cell','source':'excel','file':str(self.path),'sheet':'Rapor','mode':'header_lookup',
               'header_row':78,'value_row':79,'date_basis':'data_day','month_first':True}
        target=self.tmp/'hedef.xlsx';book=Workbook();book.active.title='Rapor'
        book.active['B1'],book.active['C1']='04/03/2026','04/04/2026';book.save(target);book.close()
        chain=formula_job(label='Zincir',expression='A * 2',variables=[{'name':'A','kind':'job','job_id':'sentetik_hesap'}])
        p=self.profile([formula_job(expression='A',variables=[var]),chain])
        p.update(output_file=str(target),excel_mode='mapped_cells',excel_mappings=[{'job_id':'sentetik_hesap','column':None,
            'sheet':'Rapor','mode':'header_lookup','header_row':1,'value_row':2,'month_first':True,'date_basis':'data_day'}])
        p=store.save_profile(p)
        result=self.run_it(p,write=True,dates=(date(2026,4,3),date(2026,4,4)),split_days=True)
        self.assertEqual(result['failed'],0)
        self.assertEqual([r['value'] for r in result['day_rows']],[21.5,43,22.5,45])
        book=load_workbook(target);self.assertEqual((book['Rapor']['B2'].value,book['Rapor']['C2'].value),(21.5,22.5));book.close()

    def setUp(self):
        super().setUp()
        store.ensure_defaults()
        self.path = self.tmp / "cikti" / "rapor.xlsx"
        self.path.parent.mkdir(parents=True)
        self.yesterday = datetime.now(ZoneInfo("Europe/Istanbul")).date() - timedelta(days=1)
        book = Workbook()
        sheet = book.active
        sheet.title = "Rapor"
        sheet["C5"] = 5
        for i, day in enumerate([self.yesterday - timedelta(days=1), self.yesterday], start=2):
            sheet[f"A{i}"], sheet[f"B{i}"] = day, 10 * i  # tarih sütunu A, değer B
        book.save(self.path)
        book.close()

    def profile(self, jobs, mappings=()):
        p = store.load_profile("demo-sahte-veri")
        p.update(jobs=jobs, output_file=str(self.path), excel_mode="mapped_cells" if mappings else "rebuild",
                 excel_mappings=list(mappings))
        return store.save_profile(p)

    def run_it(self, p, **kw):
        with mock.patch("netmera_automation.synthetic.time.sleep"):
            return runner.run_profile(p, log=lambda _: None, **kw)

    def test_job_result_plus_fixed_cell(self):
        p = self.profile([event_job(), formula_job()])
        summary = self.run_it(p, write=False)
        event, calc = (next(r for r in summary["rows"] if r["job_type"] == t) for t in ("event", "formula"))
        self.assertEqual(calc["status"], "OK")
        self.assertEqual(calc["value"], (event["value"] + 5) / 2 if (event["value"] + 5) % 2 else (event["value"] + 5) // 2)
        self.assertEqual(calc["period_start"], event["period_start"])  # dönem girdiden gelir
        self.assertIn("Rapor!C5", calc["note"])

    def test_date_row_cell_and_trying_only_the_formula_runs_its_input(self):
        variables = [{"name": "A", "kind": "job", "job_id": "sentetik_olay"},
                     {"name": "B", "kind": "cell", "source": "excel", "file": str(self.path), "sheet": "Rapor",
                      "mode": "date_row", "date_column": "A", "value_column": "B"}]
        p = self.profile([event_job(enabled=False), formula_job(expression="A - B", variables=variables)])
        summary = self.run_it(p, write=False, job_ids=["sentetik_hesap"])
        event, calc = (next(r for r in summary["rows"] if r["job_type"] == t) for t in ("event", "formula"))
        self.assertEqual(calc["value"], event["value"] - 30)  # dünün satırı: 3. satır → 30

    def test_empty_cell_fails_and_nothing_is_written(self):
        mappings = [{"job_id": "sentetik_hesap", "column": None, "sheet": "Rapor", "mode": "fixed_cell", "cell": "E5"}]
        p = self.profile([event_job(), formula_job(variables=[formula_job()["variables"][0],
                                                              {**formula_job()["variables"][1], "cell": "C9"}])], mappings)
        summary = self.run_it(p, write=True)
        calc = next(r for r in summary["rows"] if r["job_type"] == "formula")
        self.assertEqual(calc["status"], "FORMULA_CELL_INVALID")
        self.assertIn("boş", calc["note"])
        book = load_workbook(self.path)
        self.assertIsNone(book["Rapor"]["E5"].value)
        book.close()

    def test_decimal_result_is_written_to_mapped_cell(self):
        mappings = [{"job_id": "sentetik_hesap", "column": None, "sheet": "Rapor", "mode": "fixed_cell", "cell": "E5"}]
        p = self.profile([event_job(), formula_job(expression="(A + B) / 4")], mappings)
        summary = self.run_it(p, write=True)
        calc = next(r for r in summary["rows"] if r["job_type"] == "formula")
        book = load_workbook(self.path)
        self.assertEqual(book["Rapor"]["E5"].value, calc["value"])
        self.assertEqual(book["Rapor"]["C5"].value, 5)  # okunan hücre değişmez
        book.close()

    def test_two_sheets_without_netmera_do_not_open_browser(self):
        """A dosyasında günün satırındaki C + B dosyasındaki B5; Netmera işi yok, tarayıcı açılmaz."""
        other = self.tmp / "b.xlsx"
        book = Workbook(); book.active.title = "b"; book.active["B5"] = 7; book.save(other); book.close()
        variables = [{"name": "X", "kind": "cell", "source": "output", "sheet": "Rapor", "mode": "date_row",
                      "date_column": "A", "value_column": "B", "date_basis": "previous_day"},
                     {"name": "Y", "kind": "cell", "source": "excel", "file": str(other), "sheet": "b", "mode": "fixed_cell", "cell": "B5"}]
        p = store.load_profile("production")
        p.update(netmera_app="Sentetik", output_file=str(self.path), jobs=[formula_job(expression="X + Y", variables=variables)])
        p = store.save_profile(p)
        with mock.patch.object(runner, "Browser", side_effect=AssertionError("tarayıcı açılmamalı")):
            summary = runner.run_profile(p, write=False, log=lambda _: None)
        self.assertEqual([(r["status"], r["value"]) for r in summary["rows"]], [("OK", 30 + 7)])  # dünün satırı: 30

    def test_split_days_compute_per_day(self):
        variables = [{"name": "A", "kind": "job", "job_id": "sentetik_olay"},
                     {"name": "B", "kind": "cell", "source": "output", "sheet": "Rapor", "mode": "date_row", "date_column": "A", "value_column": "B"}]
        p = self.profile([event_job(), formula_job(expression="A + B", variables=variables)])
        first = self.yesterday - timedelta(days=1)
        summary = self.run_it(p, write=False, dates=(first, self.yesterday), split_days=True)
        calcs = sorted((r for r in summary["day_rows"] if r["job_type"] == "formula"), key=lambda r: r["period_start"])
        events = sorted((r for r in summary["day_rows"] if r["job_type"] == "event"), key=lambda r: r["period_start"])
        self.assertEqual([c["value"] for c in calcs], [events[0]["value"] + 20, events[1]["value"] + 30])
        self.assertFalse([r for r in summary["rows"] if r["job_type"] == "formula"])
        from netmera_automation import cell_mapping
        p.update(excel_mode='mapped_cells',excel_mappings=[{'job_id':calcs[0]['job_id'],'column':None,
                 'sheet':'Rapor','mode':'date_row','date_column':'A','value_column':'D','date_basis':'data_day'}])
        p=store.save_profile(p);before=self.path.read_bytes()
        preview=cell_mapping.preview(p,summary['rows'],day_rows=summary['day_rows'])
        self.assertEqual(preview['ready'],2)
        self.assertEqual([i['value'] for i in preview['items']],[c['value'] for c in calcs])
        self.assertEqual(self.path.read_bytes(),before)


if __name__ == "__main__":
    unittest.main()
