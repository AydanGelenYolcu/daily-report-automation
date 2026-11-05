from datetime import date, datetime
import unittest

from netmera_automation import jobs as J
from netmera_automation import store
from tests.helpers import event_job

SLOT = datetime.fromisoformat("2026-03-10T09:00:00+03:00")


class JobTests(unittest.TestCase):
    def test_session_and_tag_preserve_query_identity_and_legacy_defaults(self):
        old = J.normalize_job(event_job())
        explicit = J.normalize_job(event_job(audience='all_users'))
        self.assertEqual(old, explicit)
        self.assertNotIn('audience_name', old)
        session = J.normalize_job(event_job(metric='by_session'))
        tagged = J.normalize_job(event_job(metric='by_session', audience='tag', audience_name='  Sentetik Tag  '))
        other = J.normalize_job(event_job(metric='by_session', audience='tag', audience_name='Diğer Etiket'))
        self.assertEqual(len({J.signature(j) for j in [old, session, tagged, other]}),4)
        self.assertEqual(tagged['audience_name'],'Sentetik Tag')
        self.assertIn('By Session',J.describe(tagged))
        self.assertIn('Tag: Sentetik Tag',J.describe(tagged))
        for fields in ({'audience':'tag'}, {'audience':'tag','audience_name':' '},
                       {'audience':'tag','audience_name':['Sentetik Tag']},
                       {'audience':'all_users','audience_name':'Sentetik Tag'}, {'audience':{}}):
            with self.subTest(fields=fields), self.assertRaises(J.JobError):
                J.normalize_job(event_job(**fields))

    def test_seed_jobs_are_valid(self):
        profile = J.normalize_profile({"id": "p", "name": "P", "jobs": store.seed()["jobs"]})
        self.assertEqual(len(profile["jobs"]), 6)
        self.assertEqual(profile["jobs"][0]["items"][2]["kind"], "group")
        self.assertEqual(profile["jobs"][4]["filters"][0]["values"], ["DemoCategory", "DemoStore"])

    def test_filter_rules(self):
        ok = J.normalize_job(event_job(filters=[{"property": "Alan", "operator": "Not Equal", "values": "x"}]))
        self.assertEqual(ok["filters"][0]["values"], ["x"])
        exists = J.normalize_job(event_job(filters=[{"property": "Alan", "operator": "Exists", "values": []}]))
        self.assertEqual(exists["filters"][0]["values"], [])
        with self.assertRaises(J.JobError):  # Exists değer almaz
            J.normalize_job(event_job(filters=[{"property": "Alan", "operator": "Exists", "values": ["x"]}]))
        bad = [
            {"property": "Alan", "operator": "Equals", "values": ["a", "b"]},  # çoklu değer In ister
            {"property": "", "operator": "Equals", "values": ["a"]},
            {"property": "Alan", "operator": "In", "values": ["a", "a"]},
            {"property": "Alan", "operator": "In", "values": []},
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(J.JobError):
                J.normalize_job(event_job(filters=[item]))

    def test_unsupported_settings_fail_loudly(self):
        for change in ({"metric": "sum"}, {"metric": "session_guess"}, {"columns": ["Desktop"]}, {"period": {"type": "last_n_days", "days": 0}},
                       {"audience": "segment"}, {"event": "  "}):
            with self.subTest(change=change), self.assertRaises(J.JobError):
                J.normalize_job(event_job(**change))
        with self.assertRaises(J.JobError):
            J.normalize_job({"type": "people", "label": "P", "items": [{"field": "App Version"}]})
        for bad_people in ({"items": []}, {"connector": "XOR"}, {"items": [{"kind": "group", "rules": []}]},
                           {"items": [{"kind": "group", "connector": "X", "rules": [{"field": "a", "value": "b"}]}]}):
            with self.subTest(bad_people=bad_people), self.assertRaises(J.JobError):
                J.normalize_job({"type": "people", "label": "P", "items": [{"field": "App Installation", "value": "Installed"}], **bad_people})

    def test_old_people_formats_become_user_structure(self):
        for old in ({"platforms": ["android", "ios"], "version": "1.2.3", "version_operator": "Greater Than", "installed": True},
                    {"rules": [{"field": "app_version", "platform": "android", "operator": "Greater Than", "version": "1.2.3"},
                               {"field": "app_version", "platform": "ios", "operator": "Greater Than", "version": "1.2.3"},
                               {"field": "app_installation", "value": "Installed"}], "connector": "OR", "mode": "flat"}):
            job = J.normalize_job({"type": "people", "label": "P", **old})
            self.assertEqual(job["connector"], "OR")
            self.assertEqual([i.get("value") for i in job["items"][:2]], ["Android 1.2.3", "iOS 1.2.3"])
            self.assertEqual(job["items"][2], {"kind": "group", "connector": "AND", "rules": [{"field": "App Installation", "value": "Installed"}]})
            self.assertIn("[grup AND: App Installation = Installed]", J.describe(job))

    def test_people_schema_steps_and_numbers(self):
        behavior = J.normalize_job({"type": "people", "label": "B", "items": [
            {"kind": "rule", "field": "Behavior", "value": "Wish List", "steps": ["Did", "more than", "in the last", "Day"], "numbers": [3, "7"]}]})
        rule = behavior["items"][0]
        self.assertEqual((rule["steps"], rule["numbers"]), (["Did", "more than", "in the last", "Day"], [3, 7]))
        self.assertIn("Did more than in the last Day", J.describe(behavior))
        # Yazım varyantı panelin adına çevrilir (ör. 'Behaviour' → 'Behavior'); eski proje yüklenmeye devam eder.
        alias = J.normalize_job({"type": "people", "label": "A", "items": [{"kind": "rule", "field": "Behaviour", "value": "add to cart"}]})
        self.assertEqual(alias["items"][0]["field"], "Behavior")
        # Şemada olmayan alan ve değer açıkça reddedilir.
        for bad in ({"field": "Uydurma", "value": "x"}, {"field": "Channel Permission", "value": "Telefon"},
                    {"field": "App Version", "value": "1.2.3"}, {"field": "Behavior", "value": "x", "numbers": [-1]},
                    {"field": "Behavior", "value": "x", "steps": ["a"] * 7}):
            with self.subTest(bad=bad), self.assertRaises(J.JobError):
                J.normalize_job({"type": "people", "label": "P", "items": [bad]})
        self.assertIn("Mobile", J.PEOPLE_FIELD_INFO["Channel Permission"]["degerler"])
        self.assertNotIn("Mobile", J.PEOPLE_FIELDS)  # Mobile bir alan değil, Channel Permission değeridir

    def test_custom_period_and_run_dates(self):
        job = J.normalize_job(event_job(period={"type": "custom", "start": "2026-03-01", "end": "2026-03-03"}))
        self.assertEqual(J.period_days(job["period"], SLOT), (date(2026, 3, 1), date(2026, 3, 3)))
        self.assertIn("01.03.2026–03.03.2026", J.describe(job))
        for start, end in (("2026-03-05", "2026-03-01"), ("2999-01-01", None), ("03/01/2026", None)):
            with self.subTest(start=start), self.assertRaises(J.JobError):
                J.parse_day_range(start, end)
        self.assertEqual(J.parse_day_range("2026-03-01", None), (date(2026, 3, 1), date(2026, 3, 1)))
        with self.assertRaises(J.JobError):
            J.normalize_job({"type": "funnel", "label": "F"})

    def test_people_date_range_is_validated_and_part_of_query(self):
        def job(**changes):
            return J.normalize_job({"type": "people", "label": "Aralık", "items": [
                {"field": "Behavior", "value": "Sentetik Davranış", "numbers": [1],
                 "date_range": {"start": "2026-03-01", "end": "2026-03-03"}, **changes}]})
        a = job()
        self.assertEqual(a["items"][0]["steps"], ["between dates"])
        self.assertIn("01.03.2026–03.03.2026", J.describe(a))
        self.assertEqual(J.normalize_job(a), a)
        self.assertNotEqual(J.signature(a), J.signature(job(date_range={"start":"2026-03-01", "end":"2026-03-02"})))
        self.assertEqual(job(date_range={"start":"2026-03-01", "end":"2026-03-01"})["items"][0]["date_range"]["end"], "2026-03-01")
        for bad in ({"date_range": {}}, {"date_range": {"start":"2026-03-01"}},
                    {"date_range": {"start":"2026-03-03", "end":"2026-03-01"}},
                    {"date_range": {"start":"2026-02-30", "end":"2026-03-01"}},
                    {"date_range": {"start":"2999-01-01", "end":"2999-01-02"}},
                    {"date_range": None, "steps":["between dates"]},
                    {"steps":["in the last"]}, {"steps":["Day"]}, {"numbers":[1,7]},
                    {"field":"App Installation", "value":"Installed"}):
            with self.subTest(bad=bad), self.assertRaises(J.JobError):
                job(**bad)

    def test_periods_use_istanbul_calendar_days(self):
        self.assertEqual(J.period_days({"type": "yesterday"}, SLOT), (date(2026, 3, 9), date(2026, 3, 9)))
        self.assertEqual(J.period_days({"type": "today"}, SLOT), (date(2026, 3, 10), date(2026, 3, 10)))
        self.assertEqual(J.period_days({"type": "last_n_days", "days": 7}, SLOT), (date(2026, 3, 3), date(2026, 3, 9)))
        late_utc = datetime.fromisoformat("2026-03-09T22:30:00+00:00")  # İstanbul'da 10 Mart 01:30
        self.assertEqual(J.period_days({"type": "yesterday"}, late_utc), (date(2026, 3, 9), date(2026, 3, 9)))
        start, end = J.period_bounds(date(2026, 3, 9), date(2026, 3, 9))
        self.assertEqual((start.isoformat(), end.isoformat()), ("2026-03-09T00:00:00+03:00", "2026-03-10T00:00:00+03:00"))

    def test_signature_tracks_query_not_label(self):
        a = J.normalize_job(event_job())
        self.assertEqual(J.signature(a), J.signature({**a, "label": "Başka ad", "enabled": False}))
        b = J.normalize_job(event_job(filters=[{"property": "Alan", "operator": "Equals", "values": ["x"]}]))
        self.assertNotEqual(J.signature(a), J.signature(b))

    def test_profile_rules(self):
        base = {"id": "p", "name": "P", "jobs": [event_job()]}
        with self.assertRaises(J.JobError):
            J.normalize_profile({**base, "netmera_app": "DemoCompany UAT"})
        with self.assertRaises(J.JobError):  # aynı Excel sütunu iki kez
            J.normalize_profile({**base, "jobs": [event_job(), event_job()]})
        with self.assertRaises(J.JobError):
            J.normalize_profile({**base, "schedule": {"times": ["25:00"]}})
        profile = J.normalize_profile({**base, "jobs": [event_job(), event_job(label="Sentetik Olay", columns=["iOS"])]})
        self.assertEqual(profile["jobs"][1]["id"], "sentetik_olay_2")
        self.assertEqual(J.column_label(profile["jobs"][1], "iOS"), "Sentetik Olay [iOS]")
        self.assertIn("Netmera", " ".join(J.readiness(profile)))
        self.assertEqual(J.normalize_profile(base)['excel_mode'],'rebuild')
        for change in ({'excel_mode':'overwrite-anything'},{'excel_sheet':'a/b'},{'excel_sheet':'_netmerakayit'},{'excel_sheet':''}):
            with self.subTest(change=change), self.assertRaises(J.JobError):
                J.normalize_profile({**base,**change})


if __name__ == "__main__":
    unittest.main()


class TableLayoutTests(unittest.TestCase):
    """Prod'da gözlenen tablo biçimleri (2026-10-16); sayılar uydurmadır."""

    def test_two_metric_groups_read_by_colspan(self):
        from netmera_automation.panel import extract_event_count
        cols = [[c, 1] for c in J.COLUMNS]
        headers = [[["Select", 1], ["Events", 1], ["Total Event Count", 7], ["Unique User Count", 7]], cols + cols]
        row = ["", "Open App"] + [str(i) for i in range(10, 17)] + [str(i) for i in range(20, 27)]
        self.assertEqual(extract_event_count(headers, [["" for _ in row], row], "Open App", "Total Event Count"), 10)
        self.assertEqual(extract_event_count(headers, [row], "Open App", "Unique User Count"), 20)
        self.assertEqual(extract_event_count(headers, [row], "Open App", "Unique User Count", "iOS"), 22)

    def test_filter_column_and_masking(self):
        from netmera_automation.panel import event_table_layout, mask
        headers = [[["Select", 1], ["Events", 1], ["Filter", 1], ["Unique User Count", 7]], [[c, 1] for c in J.COLUMNS]]
        self.assertEqual(event_table_layout(headers, "Unique User Count")[:3], (1, 3, 2))
        self.assertEqual(mask("people match 12.345 ve 1234, sürüm 1.2.3, a@b.com"), "people match # ve #, sürüm 1.2.3, <e-posta>")
