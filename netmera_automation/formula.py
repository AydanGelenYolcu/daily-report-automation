"""Hesaplama işi: iş sonuçları ve tablo hücreleriyle (A + B) / 2 gibi formüller.

Formül yalnız sayı, + - * / ve parantez içerir; Python'un ast ağacı izin verilen düğümlerle sınırlanır,
eval kullanılmaz. Tablo hücresi yerel Excel'den (kayıtlı hesaplanmış değer) veya Google Sheets'ten
(hesaplanmış değer) okunur. Boş veya sayı olmayan hücrede hesap yapılmaz (kullanıcı kararı 2026-10-28).
"""
from __future__ import annotations

import ast
from datetime import date, datetime
import math
from pathlib import Path
import re
from urllib.parse import quote, urlencode

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_from_string

from . import dateparse
from .paths import check_local_path

_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}
NAME = re.compile(r"[A-Z]")


def _tree(expression: str) -> ast.Expression:
    text = expression.replace("×", "*").replace("÷", "/")
    if "," in text:
        raise ValueError("ondalık sayıda nokta kullanın (ör. 2.5)")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise ValueError("formül okunamadı; yalnız A, B, sayılar, + - * / ve parantez kullanın") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.UAdd, ast.USub, ast.Load) + tuple(_OPS)):
            continue
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            continue
        if isinstance(node, ast.Name) and NAME.fullmatch(node.id):
            continue
        raise ValueError("formülde yalnız A, B gibi tek büyük harf, sayılar, + - * / ve parantez olabilir")
    return tree


def names(expression: str) -> set[str]:
    """Formülde geçen değişken adları; formül geçersizse ValueError."""
    return {n.id for n in ast.walk(_tree(expression)) if isinstance(n, ast.Name)}


def evaluate(expression: str, values: dict[str, int | float]) -> int | float:
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.UnaryOp):
            return -walk(node.operand) if isinstance(node.op, ast.USub) else walk(node.operand)
        return _OPS[type(node.op)](walk(node.left), walk(node.right))
    try:
        result = walk(_tree(expression))
        finite = math.isfinite(result)
    except ZeroDivisionError:
        raise ValueError("sıfıra bölme") from None
    except OverflowError:
        raise ValueError("sonuç geçerli bir sayı değil (sayısal sınır aşıldı)") from None
    if not finite:
        raise ValueError("sonuç geçerli bir sayı değil")
    # Yuvarlama yapılmaz (kullanıcı kararı); yalnız tam sayıya eşit sonuç tam sayı olarak yazılır.
    return int(result) if isinstance(result, float) and result.is_integer() and abs(result) < 2 ** 53 else result


def to_number(value, where: str) -> int | float:
    """Hücre değeri → sayı. Boş, tarih, metin veya okunamayan değer hata verir; 0 sayılmaz."""
    def checked(number):
        try:
            finite = math.isfinite(number)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{where} sayı değil (sayısal sınır aşıldı)")
        return number

    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{where} boş")
    if isinstance(value, bool) or isinstance(value, (datetime, date)):
        raise ValueError(f"{where} sayı değil")
    if isinstance(value, (int, float)):
        return checked(value)
    text = str(value).strip().replace(" ", "").replace(" ", "")
    if re.fullmatch(r"-?\d+", text):
        return checked(int(text))
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", text):  # 1.234.567 (binlik nokta)
        return checked(int(text.replace(".", "")))
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})*,\d+", text) or re.fullmatch(r"-?\d+,\d+", text):  # 1.234,5
        return checked(float(text.replace(".", "").replace(",", ".")))
    if re.fullmatch(r"-?\d+\.\d+", text):
        return checked(float(text))
    raise ValueError(f"{where} sayı değil")


# --- tablo hücresi okuma -------------------------------------------------------------

class _GoogleSheet:
    """Yalnız tarih ekseni ve seçilen hesap hücreleri; tam sayfa indirilmez ve hiçbir şey yazılmaz."""

    def __init__(self, sheets_id: str, sheet: str, profile_id: str):
        from . import google_auth as auth
        if not auth.status(profile_id)["connected"]:
            raise ValueError("Google hesabı bağlı değil (Ayarlar → Google bağlantısı)")
        self.sid, self.token = sheets_id, auth.access_token(profile_id)
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(sheets_id, safe="")
        self.axes, self.cells = {}, {}
        metadata = self._request({"fields": "spreadsheetId,sheets(properties(title,gridProperties(rowCount,columnCount)))"})
        matches = self._matches(metadata, sheet)
        props = matches[0].get("properties", {})
        self.title = props["title"]
        grid = props.get("gridProperties", {})
        self.rows, self.columns = grid.get("rowCount"), grid.get("columnCount")
        if any(type(n) is not int or n < 1 for n in (self.rows, self.columns)):
            raise ValueError("Google kaynak sayfasının hücre sınırları okunamadı")

    def _matches(self, data, title):
        sheets = data.get("sheets")
        if data.get("spreadsheetId") != self.sid or not isinstance(sheets, list):
            raise ValueError("Google kaynak dosya kimliği veya sayfası doğrulanamadı")
        matches = [s for s in sheets if isinstance(s, dict) and isinstance(s.get("properties"), dict)
                   and isinstance(s["properties"].get("title"), str) and s["properties"]["title"].casefold() == title.casefold()]
        if len(matches) != 1:
            raise ValueError("Google kaynak sayfası tekil olarak bulunamadı; adını kontrol edin")
        return matches

    def _request(self, query, suffix=""):
        from . import google_auth as auth
        try:
            return auth.request_json(self.base + suffix + "?" + urlencode(query), token=self.token)
        except auth.GoogleError as exc:
            raise ValueError(f"Google Sheet okunamadı: {exc}") from None

    def _range(self, area):
        return "'" + self.title.replace("'", "''") + "'!" + area

    def _axis(self, area, *, vertical):
        if area not in self.axes:
            data = self._request({"ranges": self._range(area), "valueRenderOption": "UNFORMATTED_VALUE",
                                  "dateTimeRenderOption": "SERIAL_NUMBER"}, "/values:batchGet")
            ranges = data.get("valueRanges", [])
            if not isinstance(ranges, list) or len(ranges) != 1:
                raise ValueError("Google tarih ekseni tam okunamadı")
            lines = ranges[0].get("values", [])
            if not isinstance(lines, list) or any(not isinstance(line, list) for line in lines):
                raise ValueError("Google tarih ekseni okunamadı")
            if vertical:
                if len(lines) > self.rows or any(len(line) > 1 for line in lines):
                    raise ValueError("Google tarih sütunu beklenen aralığın dışında")
                self.axes[area] = [(i, line[0]) for i, line in enumerate(lines, 1) if line]
            else:
                if len(lines) > 1 or (lines and len(lines[0]) > self.columns):
                    raise ValueError("Google başlık satırı beklenen aralığın dışında")
                self.axes[area] = list(enumerate(lines[0], 1)) if lines else []
        return self.axes[area]

    def read(self, var, day):
        if var["mode"] == "fixed_cell":
            cell = var["cell"]
        elif var["mode"] == "date_row":
            if self.rows > 100000:
                raise ValueError("tarih araması en çok 100.000 satırı destekler")
            if column_index_from_string(var["date_column"]) > self.columns:
                raise ValueError("Tarih sütunu Google kaynak sayfasının dışında")
            area = f"{var['date_column']}1:{var['date_column']}{self.rows}"
            hits, _ = dateparse.find(self._axis(area, vertical=True), day, var.get("month_first", False), "Tarih sütunu")
            if len(hits) != 1:
                raise ValueError(f"{day:%d.%m.%Y} kaynak tarih sütununda tek satırda bulunamadı")
            cell = f"{var['value_column']}{hits[0]}"
        else:
            if self.columns > 16384 or var["header_row"] > self.rows:
                raise ValueError("Kaynak başlık satırı sınırların dışında (en çok 16.384 sütun)")
            area = f"A{var['header_row']}:{get_column_letter(self.columns)}{var['header_row']}"
            hits, overflow = dateparse.find(self._axis(area, vertical=False), day, var.get("month_first", False), "Başlık satırı")
            if len(hits) != 1 or overflow:
                raise ValueError(f"{day:%d.%m.%Y} kaynak başlık satırında tek sütunda bulunamadı veya taşan gün")
            cell = f"{get_column_letter(hits[0])}{var['value_row']}"
        column, row = coordinate_from_string(cell)
        column = column_index_from_string(column)
        if row > self.rows or column > self.columns:
            raise ValueError("Hesaplama hücresi Google kaynak sayfasının dışında")
        where = f"{self.title}!{cell}"
        if cell not in self.cells:
            fields = "spreadsheetId,sheets(properties(title),data(startRow,startColumn,rowData(values(effectiveValue,effectiveFormat(numberFormat(type))))))"
            data = self._matches(self._request({"ranges": self._range(cell), "fields": fields}), self.title)[0].get("data", [])
            raw = None
            if data:
                if len(data) != 1 or data[0].get("startRow", 0) != row - 1 or data[0].get("startColumn", 0) != column - 1:
                    raise ValueError("Google hesaplama hücresinin konumu doğrulanamadı")
                lines = data[0].get("rowData", [])
                cells = lines[0].get("values", []) if lines else []
                if len(lines) > 1 or len(cells) > 1:
                    raise ValueError("Google hesaplama hücresi tekil okunamadı")
                if cells:
                    item = cells[0]
                    kind = item.get("effectiveFormat", {}).get("numberFormat", {}).get("type")
                    if kind in ("DATE", "DATE_TIME", "TIME"):
                        raise ValueError(f"{where} sayı değil (tarih/saat hücresi)")
                    value = item.get("effectiveValue", {})
                    if value and set(value) not in ({"numberValue"}, {"stringValue"}):
                        raise ValueError(f"{where} sayı değil")
                    raw = value.get("numberValue", value.get("stringValue"))
            self.cells[cell] = to_number(raw, where)
        return self.cells[cell], where

    def close(self):
        pass


class Sources:
    """Bir çalışmadaki hücre okumaları: her dosya/sayfa bir kez açılır."""

    def __init__(self, profile: dict):
        self.profile, self.books = profile, {}

    def close(self):
        for book in self.books.values():
            book.close()

    def _sheet(self, var: dict):
        source = var["source"]
        if source == "output":
            source = "google_sheets" if self.profile.get("output_target") == "google_sheets" else "excel"
            location = self.profile.get("sheets_id") if source == "google_sheets" else self.profile.get("output_file")
            if not location:
                raise ValueError("projenin çıktı dosyası seçilmemiş")
        else:
            location = var["sheets_id"] if source == "google_sheets" else var["file"]
        key = (source, location, var["sheet"].casefold() if source == "google_sheets" else "")
        if key not in self.books:
            if source == "google_sheets":
                if self.profile["source"] == "demo":
                    raise ValueError("Demo projesi Google Sheets'e bağlanmaz")
                self.books[key] = _GoogleSheet(location, var["sheet"], self.profile["id"])
            else:
                path = check_local_path(Path(location))
                if not path.is_file():
                    raise ValueError(f"Excel dosyası bulunamadı: {path.name}")
                try:
                    self.books[key] = load_workbook(path, data_only=True, read_only=False)
                except PermissionError:
                    raise ValueError(f"{path.name} açık veya kilitli; okunamadı") from None
                except Exception:
                    raise ValueError(f"{path.name} okunamadı") from None
        book = self.books[key]
        if isinstance(book, _GoogleSheet):
            return book
        sheet = next((s for s in book if s.title.casefold() == var["sheet"].casefold()), None)
        if sheet is None:
            raise ValueError(f"'{var['sheet']}' sayfası bulunamadı")
        return sheet

    def read(self, var: dict, day: date) -> tuple[int | float, str]:
        """Değişkenin hücresini bulur ve sayıya çevirir. (değer, 'Sayfa!C5')"""
        from .cell_mapping import header_column
        sheet = self._sheet(var)
        if isinstance(sheet, _GoogleSheet):
            return sheet.read(var, day)
        if var["mode"] == "fixed_cell":
            cell = var["cell"]
        elif var["mode"] == "date_row":
            if sheet.max_row > 100000:
                raise ValueError("tarih araması en çok 100.000 satırı destekler")
            col = column_index_from_string(var["date_column"])
            found, _ = dateparse.find([(i, sheet.cell(i, col).value) for i in range(1, sheet.max_row + 1)], day,
                                      var.get("month_first", False), f"Tarih sütunu {var['date_column']}")
            if len(found) != 1:
                raise ValueError(f"{day:%d.%m.%Y} {var['sheet']} sayfasının {var['date_column']} sütununda tek satırda bulunamadı")
            cell = f"{var['value_column']}{found[0]}"
        else:
            column = header_column(lambda r, c: sheet.cell(r, c).value, sheet.max_column,
                                   {"header_row": var["header_row"], "month_first": var.get("month_first", False)}, day)
            from openpyxl.utils import get_column_letter
            cell = f"{get_column_letter(column)}{var['value_row']}"
        where = f"{var['sheet']}!{cell}"
        return to_number(sheet[cell].value, where), where
