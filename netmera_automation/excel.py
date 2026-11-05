"""Excel çıktıları: raporu güncelleme veya çalışma bazında satır/sayfa ekleme.

- "Gunluk": her çalışma günü bir satır, her iş bir sütun (rapora kopyalamak için).
- "Sonuclar": her ölçüm ayrı satır; dönem, çekim zamanı, durum, sorgu imzası.
Raporu güncellemede dosya kimliği zorunlu. Eklemeli modlar seçilmiş mevcut
.xlsx dosyasını yedekleyerek kullanabilir; önceki hücrelere veri yazılmaz.
"""
from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import shutil
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .jobs import JOB_TYPES, METRIC_TR
from .paths import TIMEZONE, data_root

MARKERS = {"netmera": "netmera-otomasyonu-prod-v2", "demo": "netmera-otomasyonu-demo-v2"}
TITLES = {"netmera": "Netmera sonuçları (prod)", "demo": "SENTETİK DEMO — gerçek Netmera verisi değildir"}
NAVY, MUTED, WARN = "243649", "606D7C", "9C4A00"
DETAIL_HEADERS = ["İş", "Sütun", "Ekran", "Ölçüm", "Değer", "Durum", "Dönem ilk gün", "Dönem son gün",
                  "Çekim zamanı", "Planlanan çalışma", "Kaynak notu", "Sorgu tanımı", "Sorgu imzası"]


class ExcelLockedError(OSError):
    pass


def _local(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


def _day(value: str | None):
    parsed = _local(value)
    return parsed.date() if parsed else None


def _header(sheet, row: int, headers: list[str]):
    for col, text in enumerate(headers, 1):
        cell = sheet.cell(row, col, text)
        cell.data_type = 's'
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[row].height = 34


def _cell(sheet, row, col, value, fmt=None):
    cell = sheet.cell(row, col, value)
    if isinstance(value, str):
        cell.data_type = "s"  # metin asla formül olarak yorumlanmaz
    cell.font = Font(name="Arial", size=10, color=WARN if value == "HATA" else NAVY, bold=value == "HATA")
    cell.alignment = Alignment(vertical="center", horizontal="right" if isinstance(value, (int, float, datetime)) or value == "HATA" else "left")
    if fmt:
        cell.number_format = fmt
    return cell


def _title(sheet, source: str, note: str):
    sheet.sheet_view.showGridLines = False
    sheet["A1"] = TITLES[source]
    sheet["A1"].font = Font(name="Arial", size=14, bold=True, color=WARN if source == "demo" else NAVY)
    sheet["A2"] = note
    sheet["A2"].font = Font(name="Arial", size=9, italic=True, color=MUTED)
    sheet["A3"] = f"Saat dilimi {TIMEZONE}. Boş/HATA hücresi sıfır demek değildir. Uygulama bu dosyayı yeniden yazar; elle not eklemeyin."
    sheet["A3"].font = Font(name="Arial", size=9, italic=True, color=MUTED)


def daily_table(rows: list[dict], column_order: list[str]) -> tuple[list[str], list[list]]:
    """Çalışma günü × iş sütunu. Aynı gün birden çok çalışma varsa en son başarılı değer."""
    by_day: dict = {}
    for row in sorted(rows, key=lambda r: r["planned_at"]):
        day = _day(row["planned_at"])
        entry = by_day.setdefault(day, {"last": None, "values": {}, "periods": set()})
        entry["last"] = row["planned_at"]
        current = entry["values"].get(row["column_label"])
        if row["status"] == "OK":
            entry["values"][row["column_label"]] = row["value"]
            if row["period_start"]:
                first, last = _day(row["period_start"]), _day(row["period_end"])
                entry["periods"].add((first, last))
        elif current is None:
            entry["values"][row["column_label"]] = "HATA"
    labels = list(column_order) + sorted({r["column_label"] for r in rows} - set(column_order))
    labels = [label for label in labels if any(label in e["values"] for e in by_day.values())]
    headers = ["Çalışma günü", "Son çalışma", "Event verisinin dönemi"] + labels
    table = []
    for day in sorted(by_day, reverse=True):
        entry = by_day[day]
        periods = []
        for first, last in sorted(entry["periods"]):
            # period_end hariç sınırdır; gösterimde son dahil gün kullanılır
            shown_last = last.fromordinal(last.toordinal() - 1)
            periods.append(first.strftime("%d.%m.%Y") if first == shown_last else f"{first:%d.%m.%Y}–{shown_last:%d.%m.%Y}")
        table.append([day, _local(entry["last"]), ", ".join(periods) or None] + [entry["values"].get(label) for label in labels])
    return headers, table


@contextmanager
def _excel_lock(path: Path):
    """Aynı kullanıcının farklı profilleri aynı dosyaya eşzamanlı yazamaz."""
    key = hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode('utf-8')).hexdigest()
    folder = data_root() / 'excel-kilitleri'; folder.mkdir(parents=True,exist_ok=True)
    with (folder / f'{key}.lock').open('a+b') as handle:
        handle.seek(0,2)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ExcelLockedError('Aynı Excel dosyasına başka bir çalışma yazıyor.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def write_workbook(path: Path, rows: list[dict], source: str, column_order: list[str]) -> None:
    with _excel_lock(path):
        _write_workbook(path,rows,source,column_order)


def _write_workbook(path: Path, rows: list[dict], source: str, column_order: list[str]) -> None:
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Çıktı uzantısı .xlsx olmalı.")
    marker = MARKERS[source]
    if path.exists():
        try:
            existing = load_workbook(path, read_only=True)
        except PermissionError as exc:
            raise ExcelLockedError("Excel dosyası açık veya kilitli.") from exc
        except Exception as exc:
            raise ValueError("Mevcut dosya okunamadı; üzerine yazılmadı. Farklı bir dosya adı seçin.") from exc
        try:
            if existing.properties.identifier != marker:
                raise ValueError("Bu Excel dosyası bu uygulamanın bu profil türüne ait değil; üzerine yazılmadı. Yeni bir dosya adı seçin.")
        finally:
            existing.close()
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    workbook.properties.identifier = marker
    workbook.properties.title = TITLES[source]

    daily = workbook.active
    daily.title = "Gunluk"
    _title(daily, source, "Her satır bir çalışma günü. Event işleri seçilen dönemi (çoğunlukla dünü), People/Tag/Segment çekim anını gösterir.")
    headers, table = daily_table(rows, column_order)
    _header(daily, 5, headers)
    for r, values in enumerate(table, 6):
        for c, value in enumerate(values, 1):
            fmt = "dd.mm.yyyy" if c == 1 else "dd.mm.yyyy hh:mm" if c == 2 else "#,##0" if isinstance(value, int) else None
            _cell(daily, r, c, value, fmt)
    for c in range(1, len(headers) + 1):
        daily.column_dimensions[get_column_letter(c)].width = 14 if c == 1 else 17 if c == 2 else 22 if c == 3 else 20
    daily.freeze_panes = "B6"
    if table:
        t = Table(displayName="Gunluk", ref=f"A5:{get_column_letter(len(headers))}{5 + len(table)}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        daily.add_table(t)

    detail = workbook.create_sheet("Sonuclar")
    _title(detail, source, "Her ölçüm bir satır. Aynı iş + sorgu + dönem yeniden çekilirse satır güncellenir; hatalı tekrar eski doğru değeri silmez.")
    _header(detail, 5, DETAIL_HEADERS)
    ordered = sorted(rows, key=lambda r: (r["planned_at"], r["column_label"]), reverse=True)
    for r, row in enumerate(ordered, 6):
        first = _day(row["period_start"])
        last_excl = _day(row["period_end"])
        values = [row["job_label"], row["column"], JOB_TYPES[row["job_type"]], METRIC_TR[row["metric"]],
                  row["value"] if row["status"] == "OK" else None,
                  "OK" if row["status"] == "OK" else f"HATA: {row['status']}",
                  first, last_excl.fromordinal(last_excl.toordinal() - 1) if last_excl else None,
                  _local(row["captured_at"]), _local(row["planned_at"]), row.get("note"),
                  row["description"], row["signature"]]
        for c, value in enumerate(values, 1):
            fmt = "#,##0" if c == 5 else "dd.mm.yyyy" if c in (7, 8) else "dd.mm.yyyy hh:mm" if c in (9, 10) else None
            _cell(detail, r, c, value, fmt)
    for c, width in enumerate([34, 12, 15, 16, 13, 26, 13, 13, 17, 17, 22, 60, 13], 1):
        detail.column_dimensions[get_column_letter(c)].width = width
    detail.freeze_panes = "B6"
    if ordered:
        t = Table(displayName="Sonuclar", ref=f"A5:M{5 + len(ordered)}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        detail.add_table(t)

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".netmera-", suffix=".xlsx", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        workbook.save(temporary)
        try:
            os.replace(temporary, path)
        except PermissionError as exc:
            raise ExcelLockedError("Excel dosyası açık veya kilitli.") from exc
    finally:
        workbook.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


LEDGER_SHEET = '_NetmeraKayit'
LEDGER_MARKER = 'netmera-export-ledger-v1'


def _append_header(sheet, headers: list[str]) -> tuple[int, dict[str, int]]:
    """Var olan sayfanın başlıklarını eşler; başka sütun ve satırları değiştirmez."""
    if sheet.protection.sheet:
        raise ValueError(f"'{sheet.title}' sayfası korumalı; dosya değiştirilmedi.")
    if any(t.totalsRowCount for t in sheet.tables.values()):
        raise ValueError('Toplam satırı olan Excel tablosuna otomatik ekleme desteklenmiyor; yeni sayfa modunu seçin.')
    normalize = lambda value: str(value or '').strip().casefold()
    if len(set(map(normalize,headers))) != len(headers):
        raise ValueError('İş sütunu adları tarih/dönem başlıklarından farklı ve birbirinden tekil olmalı.')
    if sheet.max_row == 1 and sheet.max_column == 1 and sheet.cell(1,1).value is None:
        _header(sheet, 1, headers)
        for col in range(1, len(headers)+1):
            sheet.column_dimensions[get_column_letter(col)].width = 23
        sheet.freeze_panes = 'D2'
        return 1, {h:i for i,h in enumerate(headers,1)}
    candidates = []
    required = headers[3:]
    aliases = {'Çalışma günü':['Çalışma günü','Tarih','Date'], 'Son çalışma':['Son çalışma','Çekim zamanı'],
               'Event verisinin dönemi':['Event verisinin dönemi','Dönem']}
    for r in range(1, min(sheet.max_row,25)+1):
        values = [sheet.cell(r,c).value for c in range(1,sheet.max_column+1)]
        names = list(map(normalize,values))
        if required and all(names.count(normalize(h)) == 1 for h in required):
            columns = {h:names.index(normalize(h))+1 for h in required}
            for h,choices in aliases.items():
                matched = [i+1 for i,name in enumerate(names) if name in set(map(normalize,choices)) and i+1 not in columns.values()]
                if len(matched) > 1:
                    raise ValueError(f"'{sheet.title}' sayfasında '{h}' için birden çok sütun eşleşiyor.")
                if matched:
                    columns[h] = matched[0]
            candidates.append((r,columns))
    if len(candidates) != 1:
        raise ValueError(f"'{sheet.title}' sayfasında tek bir uygun başlık satırı bulunamadı (ilk 25 satır). "
                         "Gerekli iş başlıkları: " + ', '.join(required) + ". Boş bir sayfa adı seçebilir veya yeni sayfa modunu kullanabilirsiniz.")
    return candidates[0]


def write_incremental_workbook(path: Path, batches: list[dict], source: str, mode: str, sheet_name: str) -> int:
    with _excel_lock(path):
        return _write_incremental_workbook(path,batches,source,mode,sheet_name)


def _write_incremental_workbook(path: Path, batches: list[dict], source: str, mode: str, sheet_name: str) -> int:
    """Her çalışma bir satır/yeni sayfa. Gömülü kayıt, tekrar denemede çift yazmayı önler.

    Yalnız .xlsx; mevcut dosyanın diğer hücreleri openpyxl ile korunur. Özgün
    dosya değiştirilmeden önce son sürümünün tam kopyası yedeklenir.
    """
    path = Path(path)
    if path.suffix.lower() != '.xlsx' or mode not in ('append_rows','new_sheet'):
        raise ValueError('Eklemeli çıktı .xlsx ve geçerli bir ekleme modu gerektirir.')
    if not batches:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    before = (path.stat().st_mtime_ns, path.stat().st_size) if existed else None
    try:
        book = load_workbook(path, data_only=False, keep_links=True) if existed else Workbook()
    except PermissionError as exc:
        raise ExcelLockedError('Excel dosyası açık veya kilitli.') from exc
    except Exception as exc:
        raise ValueError('Mevcut Excel okunamadı; dosya değiştirilmedi.') from exc
    temporary = backup_temp = None
    try:
        if book.properties.identifier in MARKERS.values() and book.properties.identifier != MARKERS[source]:
            raise ValueError('Demo ve prod sonuçları aynı dosyaya yazılamaz.')
        ledger = next((s for s in book.worksheets if s.title.casefold() == LEDGER_SHEET.casefold()),None)
        if ledger is not None:
            if ledger['A1'].value != LEDGER_MARKER:
                raise ValueError(f'{LEDGER_SHEET} adı başka bir sayfa tarafından kullanılıyor; dosya değiştirilmedi.')
            if any(r[1] != source for r in ledger.iter_rows(min_row=3, max_col=2, values_only=True) if r[0]):
                raise ValueError('Demo ve prod sonuçları aynı dosyaya yazılamaz.')
        else:
            ledger = book.create_sheet(LEDGER_SHEET)
            ledger['A1'] = LEDGER_MARKER
            ledger.append(['Çalışma kimliği','Kaynak'])
        seen = {r[0] for r in ledger.iter_rows(min_row=3, max_col=1, values_only=True) if r[0]}
        written = 0
        for batch in batches:
            if batch['id'] in seen:
                continue
            if batch['source'] != source:
                raise ValueError('Bekleyen Excel çalışmasının veri kaynağı değişmiş; dosya değiştirilmedi.')
            headers, table = daily_table(batch['rows'],batch['column_order'])
            if len(table) != 1:
                raise ValueError('Bir Excel ekleme kaydı tek çalışma günü içermeli.')
            if mode == 'new_sheet':
                stamp = _local(batch['planned_at']).strftime('%Y%m%d-%H%M%S')
                base = f'{sheet_name[:15]} {stamp}'
                title, n = base, 2
                while title.casefold() in {s.casefold() for s in book.sheetnames}:
                    suffix = f' ({n})'; title = base[:31-len(suffix)] + suffix; n += 1
                sheet = book.create_sheet(title)
            else:
                sheet = next((s for s in book.worksheets if s.title.casefold() == sheet_name.casefold()),None)
                sheet = sheet if sheet is not None else book.create_sheet(sheet_name)
            header_row, columns = _append_header(sheet,headers)
            r = sheet.max_row + 1
            for index,(name,value) in enumerate(zip(headers,table[0]),1):
                if name not in columns:
                    continue
                col = columns[name]
                fmt = 'dd.mm.yyyy' if index == 1 else 'dd.mm.yyyy hh:mm:ss' if index == 2 else '#,##0' if isinstance(value,int) else None
                _cell(sheet,r,col,value,fmt)
            # Var olan Excel tablolarında eklenen satır tablo kapsamına alınır.
            from openpyxl.utils.cell import range_boundaries
            for t in sheet.tables.values():
                left,top,right,bottom = range_boundaries(t.ref)
                if top == header_row and bottom == r-1 and not t.totalsRowCount:
                    t.ref = f'{get_column_letter(left)}{top}:{get_column_letter(right)}{r}'
            ledger.append([batch['id'],source]); seen.add(batch['id']); written += 1
        if not written:
            return 0
        if not existed:
            blank = book.worksheets[0]
            if blank.title == 'Sheet' and blank.max_row == 1 and blank['A1'].value is None and blank is not sheet:
                book.remove(blank)
            book.properties.identifier = MARKERS[source]
            book.properties.title = TITLES[source]
        ledger.sheet_state = 'veryHidden'
        with tempfile.NamedTemporaryFile(prefix='.netmera-', suffix='.xlsx',dir=path.parent,delete=False) as handle:
            temporary = Path(handle.name)
        book.save(temporary)
        current = (path.stat().st_mtime_ns,path.stat().st_size) if path.exists() else None
        if before != current:
            raise ExcelLockedError('Dosya yazım sırasında değişti; tekrar deneyin.')
        if existed:
            backup = path.with_name(path.stem + '.netmera-yedek.xlsx')
            with tempfile.NamedTemporaryFile(prefix='.netmera-yedek-', suffix='.xlsx',dir=path.parent,delete=False) as handle:
                backup_temp = Path(handle.name)
            shutil.copy2(path,backup_temp)
            os.replace(backup_temp,backup)
        try:
            os.replace(temporary,path)
        except PermissionError as exc:
            raise ExcelLockedError('Excel dosyası açık veya kilitli.') from exc
        return written
    finally:
        book.close()
        for temp in (temporary,backup_temp):
            if temp is not None:
                temp.unlink(missing_ok=True)
