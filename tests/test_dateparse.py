"""Başlık tarih okuyucusu: biçimden bağımsız, sentetik değerler."""
from datetime import date, datetime
import unittest

from netmera_automation import dateparse as D

DAY = date(2026, 10, 17)


class ParseTests(unittest.TestCase):
    def test_full_dates_in_many_spellings(self):
        for value in (date(2026, 10, 17), datetime(2026, 10, 17, 9, 30), 46312,  # seri sayı
                      '2026-10-17', '2026.10.17', '2026/10/17', '2026 10 17', '20261017',
                      '2026-10-17T00:00:00', '2026-10-17 09:00', '17.10.2026 09:00:00',
                      '17 Ekim 2026', '17 EKİM 2026', '17 Eki 2026', '17-Eki-2026', '17.Ekim.2026', '17 Eki 26',
                      'Ekim 17 2026', 'Ekim 17, 2026', '17 October 2026', 'Oct 17th, 2026', '17-Oct-2026',
                      'Cuma 17 Ekim 2026', 'Cum, 17.10.2026', '17 Ekim 2026 Cuma', 'Friday, October 17, 2026',
                      '17 Ekim 2026', '  17.10.2026  '):
            with self.subTest(value=value):
                found = D.resolve([(1, value)])
                self.assertEqual(found, [(1, 'date', DAY)])

    def test_ambiguous_numeric_dates_follow_turkish_order_by_default(self):
        for value in ('17.10.2026', '17/10/2026', '17-10-2026', '17.10.26', '17/10/26'):
            with self.subTest(value=value):
                self.assertEqual(D.resolve([(1, value)]), [(1, 'date', DAY)])
        self.assertEqual(D.resolve([(1, '05/10/2026')]), [(1, 'date', date(2026, 10, 5))])
        self.assertEqual(D.resolve([(1, '05/10/2026')], month_first=True), [(1, 'date', date(2026, 5, 10))])

    def test_row_level_detection_of_american_order(self):
        cells = [(1, '10/05/2026'), (2, '10/17/2026')]  # ikinci hücre kesin ay/gün
        self.assertEqual(D.resolve(cells), [(1, 'date', date(2026, 10, 5)), (2, 'date', DAY)])
        with self.assertRaisesRegex(ValueError, 'tek biçimde'):
            D.resolve([(1, '10/17/2026'), (2, '17/10/2026')])
        # Satırdaki kesin gün/ay örneği, "ay önce" seçeneğinden önce gelir.
        self.assertEqual(D.resolve([(1, '17/10/2026')], month_first=True), [(1, 'date', DAY)])

    def test_yearless_dates_and_day_numbers(self):
        for value in ('17 Ekim', '17 eki', 'Ekim 17', 'Oct 17', '17 Ekim Cuma', '17.10', '17/10', '17. Ekim'):
            with self.subTest(value=value):
                self.assertEqual(D.resolve([(1, value)]), [(1, 'md', (10, 17))])
        for value in (17, 17.0, '17', ' 17 ', '17.', 'Cuma 17', '17 Cum', 'Cum. 17'):
            with self.subTest(value=value):
                self.assertEqual(D.resolve([(1, value)]), [(1, 'day', 17)])

    def test_non_dates_are_ignored(self):
        for value in (None, '', 'Toplam', 'Ekim', 'Hafta 3', 'Ekim Toplam', 32, 0, 1234, 3.5, '32', '31.02.2026',
                      '2026-13-01', '#REF!', True, '17 Ekim 18 Ekim', 'Q4 2026', '12:30', float('nan')):
            with self.subTest(value=value):
                self.assertEqual(D.resolve([(1, value)]), [])

    def test_find_prefers_dates_over_day_numbers_and_flags_overflow(self):
        oct_row = [(i, date(2026, 10, i)) for i in range(1, 32)]
        self.assertEqual(D.find(oct_row + [(40, 17)], DAY), ([17], False))      # 17 sayısı yok sayılır
        nov_row = [(i, date(2026, 11, i)) for i in range(1, 31)] + [(31, date(2026, 12, 1))]
        self.assertEqual(D.find(nov_row, date(2026, 12, 1)), ([31], True))
        text_row = [(i, f'{i} Ekim') for i in range(1, 32)]
        self.assertEqual(D.find(text_row, DAY), ([17], False))
        numbers = [(i, str(i)) for i in range(1, 32)]
        self.assertEqual(D.find(numbers, DAY), ([17], False))


class BrowserReasonTests(unittest.TestCase):
    def test_known_playwright_errors_get_short_reasons_without_page_text(self):
        from netmera_automation.panel import browser_reason
        self.assertEqual(browser_reason(Exception('Error: strict mode violation: locator("x") resolved to 2 elements: <div>gizli</div>')),
                         'birden çok eleman eşleşti')
        self.assertEqual(browser_reason(Exception('Element is not editable')), 'kutu yazılabilir değil')
        self.assertEqual(browser_reason(ValueError('başka bir şey <b>metin</b>')), 'tarayıcı hatası: ValueError')


if __name__ == '__main__':
    unittest.main()
