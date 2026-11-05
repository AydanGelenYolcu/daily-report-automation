"""Rapor başlıklarındaki tarihleri biçimden bağımsız okur.

Bir hücre üç şeyden birine çevrilir:
  ('date', date)        tam tarih (gerçek tarih hücresi, seri sayı, yıllı metin)
  ('md', (ay, gün))     yılsız tarih ("17 Ekim", "17.10")
  ('day', gün)          yalnız gün numarası (17, "17", "Cuma 17")
Tanınmayan hücre None olur; uygulama tahmin etmez.

Gün/ay sırası ("05/10/2026" 5 Ekim mi 10 Mayıs mı?) tek hücreden değil, satırın/sütunun
tamamından çözülür: kesin bir ay/gün örneği (10/17/2026) varsa hepsi ay/gün, kesin gün/ay
örneği (17/10/2026) varsa hepsi gün/ay sayılır; ikisi birden varsa hata verilir.
Kesin örnek yoksa eşlemedeki "ay önce" seçeneği, o da yoksa Türkçe gün/ay kullanılır.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re

_MONTH_NAMES = {
    1: ['ocak', 'oca', 'january', 'jan'],
    2: ['şubat', 'subat', 'şub', 'sub', 'february', 'feb'],
    3: ['mart', 'mar', 'march'],
    4: ['nisan', 'nis', 'april', 'apr'],
    5: ['mayıs', 'mayis', 'may'],
    6: ['haziran', 'haz', 'june', 'jun'],
    7: ['temmuz', 'tem', 'july', 'jul'],
    8: ['ağustos', 'agustos', 'ağu', 'agu', 'august', 'aug'],
    9: ['eylül', 'eylul', 'eyl', 'september', 'sept', 'sep'],
    10: ['ekim', 'eki', 'october', 'oct'],
    11: ['kasım', 'kasim', 'kas', 'november', 'nov'],
    12: ['aralık', 'aralik', 'ara', 'december', 'dec'],
}
MONTHS = {name: number for number, names in _MONTH_NAMES.items() for name in names}
WEEKDAYS = {'pazartesi', 'salı', 'sali', 'çarşamba', 'carsamba', 'perşembe', 'persembe', 'cuma', 'cumartesi', 'pazar',
            'pzt', 'sal', 'çar', 'car', 'per', 'cum', 'cmt', 'paz', 'pts', 'çrş', 'prş', 'cts',
            'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
            'mon', 'tue', 'tues', 'wed', 'thu', 'thur', 'thurs', 'fri', 'sat', 'sun'}
SEP = r'[./\-\s]'
# Saat yalnız ":" ile tanınır; "17.10" gibi tarihler saat sanılmasın.
_TIME = re.compile(r'(?:t|\s)+\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:z|[+-]\d{2}:?\d{2}|am|pm|öö|ös)?$')
_YMD = re.compile(rf'(\d{{4}}){SEP}+(\d{{1,2}}){SEP}+(\d{{1,2}})')
_NUM = re.compile(rf'(\d{{1,2}}){SEP}+(\d{{1,2}})(?:{SEP}+(\d{{4}}|\d{{2}}))?')
SERIAL_MIN, SERIAL_MAX = 20000, 2958465  # ~1954 … 9999: Excel/Sheets tarih seri sayısı


def _year(text: str) -> int:
    value = int(text)
    return value + 2000 if value < 100 else value


def _date(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def _md(m, d):
    # 29 Şubat dahil geçerli ay-gün mü? Artık yıl üzerinden denetlenir.
    return (int(m), int(d)) if _date(2024, m, d) else None


def _clean(text: str) -> str:
    text = text.replace(' ', ' ').replace(' ', ' ').replace('İ', 'i').replace('I', 'ı').lower().strip()
    text = _TIME.sub('', text).strip()
    text = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', text)        # 17th → 17
    words = [w for w in re.split(r'[\s,;]+', text) if w]
    words = [w for w in words if w.strip('.') not in WEEKDAYS]  # "Cuma", "Cum." atılır
    return ' '.join(words).strip(' .')


def parse(value):
    """Tek hücre → ('date'|'md'|'day'|'num', değer) veya None. 'num' gün/ay sırası belirsiz (a, b, yıl) demektir."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return 'date', value.date()
    if isinstance(value, date):
        return 'date', value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        if float(value).is_integer() and 1 <= value <= 31:
            return 'day', int(value)
        if SERIAL_MIN <= value <= SERIAL_MAX:
            return 'date', date(1899, 12, 30) + timedelta(days=math.floor(value))
        return None
    if not isinstance(value, str):
        return None
    text = _clean(value)
    if not text:
        return None
    if re.fullmatch(r'\d{1,2}', text):
        return ('day', int(text)) if 1 <= int(text) <= 31 else None
    if re.fullmatch(r'\d{8}', text):                              # 20261017
        found = _date(text[:4], text[4:6], text[6:])
        return ('date', found) if found else None
    if m := _YMD.fullmatch(text):                                 # 2026-10-17, 2026.10.17, 2026/10/17
        found = _date(m[1], m[2], m[3])
        return ('date', found) if found else None
    if m := _NUM.fullmatch(text):                                 # 17.10.2026, 10/17/26, 17.10
        return 'num', (int(m[1]), int(m[2]), _year(m[3]) if m[3] else None)
    tokens = re.findall(r'\d+|[a-zçğıöşü]+', text)
    months = [MONTHS[t] for t in tokens if t in MONTHS]
    if len(months) != 1 or any(not t.isdigit() and t not in MONTHS for t in tokens):
        return None                                               # "Toplam", "Hafta 3" gibi metinler
    numbers = [t for t in tokens if t.isdigit()]
    years = [n for n in numbers if len(n) == 4]
    days = [n for n in numbers if len(n) <= 2]
    if len(years) > 1 or not days or len(numbers) > 2:
        return None
    if not years and len(days) == 2:                              # "17 Eki 26": ilk sayı gün, ikinci yıl
        day, year = days[0], _year(days[1])
    else:
        day, year = days[0], (int(years[0]) if years else None)
    if year is None:
        found = _md(months[0], day)
        return ('md', found) if found else None
    found = _date(year, months[0], day)
    return ('date', found) if found else None


def resolve(cells, month_first: bool = False, where: str = 'Başlık'):
    """[(konum, ham değer)] → [(konum, tür, değer)]; belirsiz gün/ay sırası satır geneline göre çözülür."""
    parsed = [(pos, parse(raw)) for pos, raw in cells]
    nums = [p[1] for _, p in parsed if p and p[0] == 'num']
    say_mdy = any(a <= 12 < b for a, b, _ in nums)
    say_dmy = any(b <= 12 < a for a, b, _ in nums)
    if say_mdy and say_dmy:
        raise ValueError(f'{where} içinde hem gün/ay hem ay/gün sıralı tarihler var; tarihleri tek biçimde yazın.')
    mdy = say_mdy or (month_first and not say_dmy)
    out = []
    for pos, p in parsed:
        if not p:
            continue
        kind, value = p
        if kind == 'num':
            a, b, year = value
            month, day = (a, b) if mdy else (b, a)
            if year is None:
                md = _md(month, day)
                if md:
                    out.append((pos, 'md', md))
            else:
                found = _date(year, month, day)
                if found:
                    out.append((pos, 'date', found))
        else:
            out.append((pos, kind, value))
    return out


def matches(kind: str, value, day: date) -> bool:
    if kind == 'date':
        return value == day
    if kind == 'md':
        return value == (day.month, day.day)
    return value == day.day


def find(cells, day: date, month_first: bool = False, where: str = 'Başlık'):
    """Hedef günün konumları ve taşan-gün bilgisi. Tam/yılsız tarih varsa gün numaralarına bakılmaz."""
    resolved = resolve(cells, month_first, where)
    dated = [(pos, kind, value) for pos, kind, value in resolved if kind in ('date', 'md')]
    pool = dated or resolved
    hits = [pos for pos, kind, value in pool if matches(kind, value, day)]
    months: dict[int, int] = {}
    for _, kind, value in dated:
        month = value.month if kind == 'date' else value[0]
        months[month] = months.get(month, 0) + 1
    overflow = bool(hits and dated and len(months) > 1 and months.get(day.month, 0) < 28)
    return hits, overflow
