"""Segment attribute değerleri: yalnız seçilen iki sütunu okur, her tarih için yeniden çözer."""
from copy import deepcopy
from datetime import date
import math
from urllib.parse import quote, urlencode

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

from . import dateparse


class FilterSourceError(ValueError):
    pass


def rules(job):
    items = job.get('items') or []
    if not isinstance(items, list):
        return []
    return [r for item in items if isinstance(item, dict)
            for r in (item.get('rules', []) if item.get('kind') == 'group' and isinstance(item.get('rules'), list) else [item])
            if isinstance(r, dict)]


def dynamic(job):
    return any(a.get('value_source') for r in rules(job) for a in r.get('attributes', []))


def _rows(source, profile_id):
    first, last = source['first_row'], source['last_row']
    if source['source'] == 'google_sheets':
        from . import google_auth as auth
        sheet = "'" + source['sheet'].replace("'", "''") + "'!"
        ranges = [sheet + f'{source[key]}{first}:{source[key]}{last}' for key in ('date_column', 'value_column')]
        query = urlencode([('ranges', r) for r in ranges] + [('valueRenderOption', 'UNFORMATTED_VALUE'), ('dateTimeRenderOption', 'SERIAL_NUMBER')])
        base = 'https://sheets.googleapis.com/v4/spreadsheets/' + quote(source['sheets_id'], safe='')
        result = auth.request_json(base + '/values:batchGet?' + query, token=auth.access_token(profile_id))
        columns = result.get('valueRanges')
        if not isinstance(columns, list) or len(columns) != 2:
            raise FilterSourceError('Filtre kaynağı: Google iki sütunu doğrulayamadı.')
        def values(column):
            rows = column.get('values', [])
            if not isinstance(rows, list) or len(rows) > last - first + 1 or any(not isinstance(r, list) or len(r) > 1 for r in rows):
                raise FilterSourceError('Filtre kaynağı: Google sütun yanıtı geçersiz.')
            return [r[0] if r else None for r in rows]
        dates, vals = [values(c) for c in columns]
        return [(first + i, dates[i] if i < len(dates) else None, vals[i] if i < len(vals) else None)
                for i in range(max(len(dates), len(vals)))]
    book = load_workbook(source['file'], read_only=True, data_only=True)
    try:
        if source['sheet'] not in book.sheetnames:
            raise FilterSourceError('Filtre kaynağı: seçilen Excel sayfası yok.')
        sheet = book[source['sheet']]
        stop = min(last, sheet.max_row)
        columns = []
        for key in ('date_column', 'value_column'):
            col = column_index_from_string(source[key])
            columns.append([row[0] for row in sheet.iter_rows(min_row=first, max_row=stop, min_col=col, max_col=col, values_only=True)] if stop >= first else [])
        return [(first + i, d, v) for i, (d, v) in enumerate(zip(*columns))]
    finally:
        book.close()


def _value(value):
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise FilterSourceError('Filtre kaynağı: eşleşen satırın değeri boş veya geçersiz.')
        return value
    if type(value) in (int, float) and math.isfinite(value) and abs(value) < 10**15:
        return str(int(value)) if value == int(value) else str(value)
    raise FilterSourceError('Filtre kaynağı: değer metin veya geçerli sayı olmalı; uzun kimlikleri metin olarak saklayın.')


def read(source, first: date, last: date, profile_id=None):
    from .google_auth import GoogleError
    try:
        rows = _rows(source, profile_id)
        cells = [(n, d) for n, d, v in rows if d not in (None, '') or v not in (None, '')]
        dates = {n: (kind, value) for n, kind, value in dateparse.resolve(cells, source['month_first'], 'Filtre kaynağı')}
        if any(n not in dates or dates[n][0] != 'date' for n, _ in cells):
            raise FilterSourceError('Filtre kaynağı: dolu satırlarda yıl içeren tam tarih gerekli; boş veya belirsiz tarih var.')
        selected, seen = [], set()
        for n, _, value in rows:
            if n in dates and first <= dates[n][1] <= last:
                text = _value(value)
                if text not in seen:
                    seen.add(text)
                    selected.append(text)
        if not selected:
            raise FilterSourceError('Filtre kaynağı: işlenen tarih/aralık için değer bulunamadı; segment kaydedilmedi.')
        return selected
    except FilterSourceError:
        raise
    except GoogleError as exc:
        raise FilterSourceError('Filtre kaynağı: ' + str(exc)) from None
    except Exception:
        # Dosya yolu, ham yanıt veya hücre içerikleri teşhise taşınmaz.
        raise FilterSourceError('Filtre kaynağı okunamadı. Dosya/sayfa, satır sınırı, tarih biçimi ve Google erişimini kontrol edin.') from None


def resolve_job(job, first, last, profile_id=None):
    resolved = deepcopy(job)
    for rule in rules(resolved):
        for attribute in rule.get('attributes', []):
            if attribute.get('value_source'):
                attribute['values'] = read(attribute['value_source'], first, last, profile_id)
                if attribute['operator'] not in ('In', 'Not In') and len(attribute['values']) != 1:
                    raise FilterSourceError('Filtre kaynağı: birden çok değer için In veya Not In seçin.')
    return resolved
