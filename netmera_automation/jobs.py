"""İş ve profil tanımları: low-code çekirdeği.

Event adı, property filtresi, operatör, değer, metrik, tarih aralığı ve platform
sütunu yalnızca ayardan gelir. Desteklenmeyen ayar sessizce atlanmaz; hata verir.
Yeni bir Netmera *ekranı* eklemek ise kod gerektirir (bkz. JOB_TYPES).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .paths import APP_ROOT, TIMEZONE, PathError, check_local_path

JOB_TYPES = {"event": "Event Insight", "people": "People", "tag": "Tags", "segment": "Segments",
             "segment_run": "Segment hesapla", "formula": "Hesaplama", "ga4": "Google Analytics (GA4)"}
# Hesaplama işi: değişken kaynakları, hücre bulma biçimleri ve hücrenin hangi güne göre aranacağı.
FORMULA_SOURCES = {"output": "Sonuçların yazıldığı dosya", "excel": "Başka bir Excel dosyası", "google_sheets": "Başka bir Google Sheet"}
FORMULA_MODES = {"fixed_cell": "Sabit hücre (ör. C5)", "date_row": "Tarih sütununda günün satırı", "header_lookup": "Başlık satırında günün sütunu"}
DATE_BASES = {"data_day": "Verinin günü", "run_day": "Çalışma günü", "previous_day": "Bir önceki gün"}
EXPERIMENTAL_TYPES = {"tag", "segment", "segment_run"}
# Tarihi olan işler: sonuç bir döneme aittir (çekim anına değil); geçmiş günler için çalıştırılabilir.
DATED_TYPES = {"event", "segment_run", "ga4"}
# Segment hesaplama: (en az, en çok, varsayılan). Liste N saniyede bir yenilenir, en çok M dakika beklenir.
SEGMENT_POLL_SECONDS = (20, 600, 60)
SEGMENT_MAX_WAIT_MINUTES = (5, 240, 60)
# By Session, Find sonrası Dimensions bölümündeki radio ve ayrı özet tablosudur (2026-10-16).
METRICS = {"total_event_count": "Total Event Count", "unique_user_count": "Unique User Count", "by_session": "By Session"}
METRIC_TR = {"total_event_count": "Toplam olay", "unique_user_count": "Tekil kullanıcı", "by_session": "Oturuma göre", "users": "Kullanıcı sayısı",
             "formula": "Hesaplanan değer", "ga4": "GA4 metriği"}
AUDIENCES = {"all_users": "All Users — Tüm kullanıcılar", "tag": "Tag — Etiketli kullanıcılar"}
COLUMNS = ["All Total", "Mobile Total", "iOS", "Android", "Web Total", "SMS", "Email"]
# Event property operatörleri. Liste öneridir; property türüne göre panel farklı ad gösterirse aynen yazılabilir.
OPERATOR_SUGGESTIONS = ["Equals", "Not Equal", "Contains", "In", "Exists", "Not Exists"]
MULTI_VALUE_OPERATORS = {"In"}
NO_VALUE_OPERATORS = {"Exists", "Not Exists"}
VERSION_OPERATOR_SUGGESTIONS = ["Greater Than", "Equals", "Not Equal", "Less Than"]
PERIODS = {"yesterday": "Dün (önceki tam gün)", "today": "Bugün (şu ana kadar)", "last_n_days": "Son N tam gün",
           "custom": "Belirli tarih aralığı"}
# People şeması: prod'da 'Add Filter Rule' menüsünden 2026-10-16'da çıkarıldı (tools/sema.py).
# Dosya paketle birlikte gelir; alan ve değer adları bu şemaya göre doğrulanır.
def _load_people_schema() -> dict:
    try:
        return json.loads((APP_ROOT / "config" / "people-semasi.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alan_listesi": [], "alanlar": {}, "dogrulandi": []}


PEOPLE_SCHEMA = _load_people_schema()
PEOPLE_FIELD_INFO = {name: info for name, info in PEOPLE_SCHEMA["alanlar"].items() if not info.get("ust_alan")}
PEOPLE_FIELDS = [name for name in PEOPLE_SCHEMA.get("alan_listesi", []) if name in PEOPLE_FIELD_INFO]
PEOPLE_VERIFIED_FIELDS = list(PEOPLE_SCHEMA.get("dogrulandi", []))  # prod'da uçtan uca denenenler
# Yazım varyantları (İngiliz yazımı vb.) panelin adına çevrilir; böylece tek bir yazım hatası tüm projeyi
# açılamaz hale getirmez. Çeviri, işin tanımında otomatik yapılır ve uyarı olarak gösterilir.
PEOPLE_FIELD_ALIASES = {"behaviour": "Behavior", "platform permissions": "Platform Permission",
                        "app tracking": "App Tracking (Android Only)", "app tracking (android only)": "App Tracking (Android Only)"}
PEOPLE_COUNT_SOURCES = {"results_total": "Show results sonrası alttaki toplam",
                        "people_match": "'people match' sayacı"}
PLATFORMS = {"android": "Android", "ios": "iOS"}
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
SOURCES = {"netmera": "Netmera (prod)", "demo": "Demo (sahte sayılar)"}
BROWSERS = {"chrome": "Google Chrome", "msedge": "Microsoft Edge"}
ON_ERROR = {"write_successful": "Başarılı işleri yaz, hatalıları HATA olarak işaretle",
            "write_nothing": "Bir iş bile hatalıysa hiçbir şey yazma"}
EXCEL_MODES = {"rebuild": "Raporu güncelle (Gunluk + Sonuclar)",
               "append_rows": "Mevcut sayfanın altına ekle", "new_sheet": "Her çalışmada yeni sayfa aç",
               "mapped_cells": "Belirli hücrelere yaz (mevcut dosya)"}


class JobError(ValueError):
    """Kullanıcıya gösterilecek Türkçe ayar hatası."""


def _text(value, field, *, max_len=200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JobError(f"{field} boş olamaz.")
    value = value.strip()
    if len(value) > max_len:
        raise JobError(f"{field} en çok {max_len} karakter olabilir.")
    return value


def slug(text: str, fallback: str = "is") -> str:
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    value = re.sub(r"[^a-z0-9]+", "_", text.translate(table).lower()).strip("_")
    return value[:50] or fallback


JOB_ID = re.compile(r'^[a-z0-9][a-z0-9_]{0,59}$')


def normalize_job(raw: dict, *, allow_incomplete_dates: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise JobError("İş tanımı bir nesne olmalı.")
    job_type = raw.get("type")
    if job_type not in JOB_TYPES:
        raise JobError(f"Desteklenmeyen iş türü: {job_type}")
    label = _text(raw.get("label"), "İş adı (Excel sütun adı)", max_len=100 if job_type == "ga4" else 80)
    supplied_id = raw.get('id')
    job_id = supplied_id if isinstance(supplied_id, str) and JOB_ID.fullmatch(supplied_id) else slug(supplied_id or label)
    job = {"id": job_id, "label": label, "type": job_type,
           "enabled": raw.get("enabled", True) is True}
    where = f"'{label}'"
    if job_type == "event":
        job["event"] = _text(raw.get("event"), f"{where}: Event adı", max_len=120)
        filters = raw.get("filters", [])
        if not isinstance(filters, list):
            raise JobError(f"{where}: filtreler liste olmalı.")
        job["filters"] = []
        for index, item in enumerate(filters, 1):
            if not isinstance(item, dict):
                raise JobError(f"{where}: {index}. filtre geçersiz.")
            prop = _text(item.get("property"), f"{where}: {index}. filtrenin property adı", max_len=120)
            operator = _text(item.get("operator"), f"{where}: {index}. filtrenin operatörü", max_len=40)
            values = item.get("values")
            if isinstance(values, str):
                values = [values]
            if operator in NO_VALUE_OPERATORS:
                if values:
                    raise JobError(f"{where}: {operator} değer almaz; değer kutusunu boş bırakın.")
                values = []
            else:
                if not isinstance(values, list) or not values:
                    raise JobError(f"{where}: {index}. filtre için en az bir değer gerekir.")
                values = [_text(v, f"{where}: {index}. filtre değeri", max_len=200) for v in values]
                if len(values) != len(set(values)):
                    raise JobError(f"{where}: {index}. filtrede aynı değer iki kez yazılmış.")
                if len(values) > 1 and operator not in MULTI_VALUE_OPERATORS:
                    raise JobError(f"{where}: birden çok değer yalnız In ile kullanılabilir.")
            job["filters"].append({"property": prop, "operator": operator, "values": values})
        metric = raw.get("metric")
        if metric not in METRICS:
            raise JobError(f"{where}: metrik seçilmedi.")
        job["metric"] = metric
        columns = raw.get("columns") or ["All Total"]
        if not isinstance(columns, list) or any(c not in COLUMNS for c in columns) or len(columns) != len(set(columns)):
            raise JobError(f"{where}: sütunlar {', '.join(COLUMNS)} arasından seçilmeli.")
        job["columns"] = [c for c in COLUMNS if c in columns]
        job["period"] = normalize_period(raw.get("period"), where)
        # Panel 'No valid record found' gösterirse: varsayılan HATA; kullanıcı açarsa 0.
        job["empty_is_zero"] = raw.get("empty_is_zero", False) is True
        audience = raw.get("audience", "all_users")
        if not isinstance(audience, str) or audience not in AUDIENCES:
            raise JobError(f"{where}: hedef kitle All Users veya Tag olmalı.")
        job["audience"] = audience
        if audience == "tag":
            job["audience_name"] = _text(raw.get("audience_name"), f"{where}: hedef Tag adı", max_len=150)
        elif raw.get("audience_name"):
            raise JobError(f"{where}: All Users hedefinde Tag adı kullanılamaz; hedef kitleyi Tag seçin.")
    elif job_type == "people":
        job.update(_normalize_people(raw, where, allow_incomplete_dates=allow_incomplete_dates))
    elif job_type == "segment_run":
        job.update(_normalize_segment_run(raw, where))
    elif job_type == "formula":
        job.update(_normalize_formula(raw, where))
    elif job_type == "ga4":
        job.update(_normalize_ga4(raw, label, job["enabled"], where))
    else:
        job["name"] = _text(raw.get("name"), f"{where}: {JOB_TYPES[job_type]} adı", max_len=150)
    return job


def people_date_mode(field: str) -> str | None:
    """Şemada gözlenen tarih aralığı seçeneği; panelin yazımını korur."""
    return next((choice for entry in PEOPLE_FIELD_INFO.get(field, {}).get("satir_secimleri", [])
                 for choice in entry.get("secenekler", [])
                 if choice.casefold() in ("between dates", "date between")), None)


def _people_rule(raw, where: str, *, allow_incomplete_dates: bool = False, segment: bool = False) -> dict:
    """Panelde 'Add Filter Rule' ile eklenen kural.

    field : şemadaki alan adı (ör. App Version, Behavior, Response).
    value : seçilecek değer. Şemada değer listesi varsa birebir o listeden olmalı.
    operator (ops.) : satırdaki operatör (ör. Greater Than); boşsa panelin varsayılanı kalır.
    steps (ops.)   : değerden sonra sırayla seçilecek ek seçenekler (ör. ['Did', 'more than', 'in the last', 'Day']).
    numbers (ops.) : adımlardan sonra satırdaki sayı kutularına sırayla girilecek sayılar (ör. [3, 7]).
    date_range (ops.): başlangıç/bitiş ISO tarihi; bu kurala ait sabit aralık.
    """
    if not isinstance(raw, dict):
        raise JobError(f"{where}: kural geçersiz.")
    field = _text(raw.get("field"), f"{where}: kural alanı", max_len=60)
    field = PEOPLE_FIELD_ALIASES.get(field.casefold(), field)
    if field not in PEOPLE_FIELD_INFO:
        raise JobError(f"{where}: '{field}' alanı People şemasında yok. Bilinen alanlar: {', '.join(PEOPLE_FIELDS)}")
    value = _text(raw.get("value"), f"{where}: '{field}' için seçilecek değer", max_len=120)
    info = PEOPLE_FIELD_INFO[field]
    if field == "App Version" and not re.fullmatch(r"(Android|iOS) [0-9]+(?:\.[0-9]+)+", value):
        raise JobError(f"{where}: App Version değeri 'Android 1.2.3' ya da 'iOS 1.2.3' biçiminde olmalı.")
    choices = info.get("degerler")
    if choices and value not in choices:
        raise JobError(f"{where}: '{value}' bu alanın değerlerinde yok. Seçenekler: {', '.join(choices)}")
    rule = {"field": field, "value": value}
    operator = (raw.get("operator") or "").strip()
    if operator and operator != "Equals":
        rule["operator"] = _text(operator, f"{where}: operatör", max_len=40)
    steps = raw.get("steps") or []
    if not isinstance(steps, list) or len(steps) > 6:
        raise JobError(f"{where}: adımlar en çok 6 öğelik bir liste olmalı.")
    if steps:
        rule["steps"] = [_text(step, f"{where}: adım", max_len=60) for step in steps]
    numbers = raw.get("numbers", raw.get("number"))
    if numbers not in (None, "", []):
        numbers = numbers if isinstance(numbers, list) else [numbers]
        if len(numbers) > 3:
            raise JobError(f"{where}: en çok 3 sayı girilebilir.")
        cleaned = []
        for number in numbers:
            if isinstance(number, str) and number.strip().isdigit():
                number = int(number)
            if type(number) is not int or not 0 <= number <= 100000:
                raise JobError(f"{where}: sayılar 0 ile 100000 arasında tam sayı olmalı.")
            cleaned.append(number)
        rule["numbers"] = cleaned
    mode = people_date_mode(field)
    selected_range = mode and mode in rule.get("steps", [])
    if segment:
        # Segmentte between dates kutularına çalışmanın tarihi yazılır; sabit tarih saklanmaz.
        if selected_range and any(s.casefold() in ("in the last", "day", "week", "month") for s in rule.get("steps", [])):
            raise JobError(f"{where}: tarih aralığı ile son N gün/birim seçimi birlikte kullanılamaz.")
        attributes = raw.get("attributes") or []
        if attributes:
            if "+ attribute filter" not in PEOPLE_FIELD_INFO[field].get("satir_etiketleri", []):
                raise JobError(f"{where}: '{field}' alanında attribute filtresi yok.")
            rule["attributes"] = _attribute_filters(attributes, where)
            if not segment and any(a.get('value_source') for a in rule['attributes']):
                raise JobError(f'{where}: tablodan attribute değeri yalnız Segment hesapla işinde desteklenir.')
        return rule
    interval = raw.get("date_range")
    if selected_range or interval is not None:
        if not mode:
            raise JobError(f"{where}: bu alan için tarih aralığı desteklenmiyor.")
        if not isinstance(interval, dict) or not interval.get("start") or not interval.get("end"):
            if allow_incomplete_dates and selected_range and interval is None:
                # v0.5'te tarih modu saklanabiliyordu; tarih tanımı yoktu. Yalnız
                # düzenleme için yüklenir. Kaydetme/çalıştırma sıkı doğrulama yapar.
                return rule
            raise JobError(f"{where}: between dates için başlangıç ve bitiş tarihlerini seçin.")
        if any(s.casefold() in ("in the last", "day", "week", "month") for s in rule.get("steps", [])):
            raise JobError(f"{where}: tarih aralığı ile son N gün/birim seçimi birlikte kullanılamaz.")
        if len(rule.get("numbers", [])) > 1:
            raise JobError(f"{where}: between dates için yalnız olay adedi girilir; son N gün sayısını kaldırın.")
        first, last = parse_day_range(interval["start"], interval["end"], where)
        rule["date_range"] = {"start": first.isoformat(), "end": last.isoformat()}
        if not selected_range:
            rule.setdefault("steps", []).append(mode)
    return rule


def _normalize_ga4(raw: dict, label: str, enabled: bool, where: str) -> dict:
    """GA4 işi: Analytics Veri Çekme iş tanımı (metrik, boyut, zaman, filtre, tarih, Sheets, şablon, paket) aynen doğrulanır.
    Her metrik bir sütundur: kırılımsız işte tek satırın değeri, kırılımlıda GA4'ün kendi toplamı hücreye/hesaplamaya girer."""
    from .ga4 import isler as G
    payload = raw.get("ga4")
    if not isinstance(payload, dict):
        raise JobError(f"{where}: GA4 iş tanımı eksik.")
    try:
        job = G.normalize_job({**payload, "ad": label, "etkin": enabled})
    except G.JobError as exc:
        raise JobError(str(exc)) from None
    return {"ga4": {k: v for k, v in job.items() if k not in ("ad", "etkin")}, "columns": list(job["metrikler"])}


def ga4_today(planned_at: datetime) -> date:
    return planned_at.astimezone(ZoneInfo(TIMEZONE)).date()


def job_days(job: dict, planned_at: datetime) -> tuple[date, date]:
    """Tarihli işin ilk ve son günü: Event/Segment için dönem, GA4 için işin kendi tarih türü (hafta/ay/yıl dahil)."""
    if job["type"] == "ga4":
        from .ga4.rapor import resolve_dates
        return resolve_dates(job["ga4"]["tarih"], ga4_today(planned_at))
    return period_days(job["period"], planned_at)


def is_yesterday(job: dict) -> bool:
    return (job["ga4"]["tarih"]["tur"] == "dun" and not job["ga4"].get("zaman")) if job["type"] == "ga4" else job["period"]["type"] == "yesterday"


def sheets_id(raw, where: str) -> str:
    """Google Sheets düzenleme bağlantısı veya dosya kimliği → kimlik."""
    if not isinstance(raw, str):
        raise JobError(f"{where}: Google Sheets bağlantısı metin olmalı.")
    value = raw.strip()
    if value.startswith('https://'):
        from urllib.parse import urlsplit
        url = urlsplit(value)
        matched = re.match(r'^/spreadsheets/d/([A-Za-z0-9_-]+)(?:/|$)', url.path)
        if url.netloc != 'docs.google.com' or not matched:
            raise JobError(f'{where}: Google Sheets dosyasının düzenleme bağlantısını girin.')
        value = matched[1]
    if value and not re.fullmatch(r'[A-Za-z0-9_-]{15,160}', value):
        raise JobError(f'{where}: Google Sheets dosya kimliği geçersiz.')
    return value


def _cell_ref(value, label: str) -> str:
    matched = re.fullmatch(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})", value.strip()) if isinstance(value, str) else None
    if not matched or int(matched[2]) > 1048576:
        raise JobError(f"{label} C5 gibi yazılmalı.")
    return matched[1].upper() + matched[2]


def _column_ref(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{1,3}", value.strip()):
        raise JobError(f"{label} A, B gibi yazılmalı.")
    return value.strip().upper()


def _formula_variable(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise JobError(f"{where}: değişken geçersiz.")
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z]", name):
        raise JobError(f"{where}: değişken adı A, B, C gibi tek büyük harf olmalı.")
    where = f"{where}: {name}"
    kind = raw.get("kind")
    if kind == "job":
        column = raw.get("column") or None
        # Event platform sütunu (All Total, iOS…) ya da GA4 metrik adı; işte var mı, profil düzeyinde denetlenir.
        if column is not None and (not isinstance(column, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_: ]{0,99}", column)):
            raise JobError(f"{where}: sütun geçersiz.")
        return {"name": name, "kind": "job", "job_id": _text(raw.get("job_id"), f"{where}: iş", max_len=60), "column": column}
    if kind != "cell":
        raise JobError(f"{where}: değişken türü 'İş sonucu' veya 'Tablo hücresi' olmalı.")
    source = raw.get("source", "output")
    if source not in FORMULA_SOURCES:
        raise JobError(f"{where}: hücre kaynağı geçersiz.")
    var = {"name": name, "kind": "cell", "source": source, "sheet": _text(raw.get("sheet"), f"{where}: sayfa adı", max_len=100)}
    if source == "excel":
        path = _text(raw.get("file"), f"{where}: Excel dosyası", max_len=400)
        try:
            checked = check_local_path(Path(path))
        except PathError as exc:
            raise JobError(f"{where}: {exc}") from None
        if checked.suffix.lower() not in (".xlsx", ".xlsm"):
            raise JobError(f"{where}: Excel dosyası .xlsx olmalı.")
        var["file"] = str(checked)
    elif source == "google_sheets":
        var["sheets_id"] = sheets_id(raw.get("sheets_id", ""), where)
        if not var["sheets_id"]:
            raise JobError(f"{where}: Google Sheets bağlantısını girin.")
    mode = raw.get("mode", "fixed_cell")
    if mode not in FORMULA_MODES:
        raise JobError(f"{where}: hücre bulma biçimi geçersiz.")
    var["mode"] = mode
    if mode == "fixed_cell":
        var["cell"] = _cell_ref(raw.get("cell"), f"{where}: hücre")
        return var
    if mode == "date_row":
        var["date_column"] = _column_ref(raw.get("date_column"), f"{where}: tarih sütunu")
        var["value_column"] = _column_ref(raw.get("value_column"), f"{where}: değer sütunu")
        if var["date_column"] == var["value_column"]:
            raise JobError(f"{where}: tarih ve değer sütunu farklı olmalı.")
    else:
        rows = [raw.get("header_row"), raw.get("value_row")]
        rows = [int(r) if isinstance(r, str) and r.strip().isdigit() else r for r in rows]
        if any(type(r) is not int or not 1 <= r <= 1048576 for r in rows) or rows[0] == rows[1]:
            raise JobError(f"{where}: başlık satırı ve değer satırı farklı satır numaraları olmalı.")
        var["header_row"], var["value_row"] = rows
    basis = raw.get("date_basis", "data_day")
    if basis not in DATE_BASES:
        raise JobError(f"{where}: hangi güne göre aranacağı geçersiz.")
    var["date_basis"] = basis
    if raw.get("month_first") is True:
        var["month_first"] = True
    return var


def _normalize_formula(raw: dict, where: str) -> dict:
    """Hesaplama: (A + B) / 2 gibi formül + değişkenler. İş başvuruları profil düzeyinde denetlenir."""
    from .formula import names
    expression = _text(raw.get("expression"), f"{where}: formül", max_len=200)
    try:
        used = names(expression)
    except ValueError as exc:
        raise JobError(f"{where}: {exc}.") from None
    variables = raw.get("variables")
    if not isinstance(variables, list) or not 1 <= len(variables) <= 10:
        raise JobError(f"{where}: 1–10 değişken gerekir.")
    variables = [_formula_variable(v, where) for v in variables]
    declared = [v["name"] for v in variables]
    if len(declared) != len(set(declared)):
        raise JobError(f"{where}: aynı değişken adı iki kez kullanılmış.")
    if used - set(declared):
        raise JobError(f"{where}: formüldeki {', '.join(sorted(used - set(declared)))} tanımlı değil.")
    if set(declared) - used:
        raise JobError(f"{where}: {', '.join(sorted(set(declared) - used))} formülde kullanılmıyor.")
    return {"expression": " ".join(expression.split()), "variables": variables}


def formula_refs(profile_jobs: list[dict]) -> None:
    """Hesaplamanın başvurduğu iş var mı, sütunu doğru mu; başka hesaplamaya başvuru önce gelmeli (döngü olmaz)."""
    for index, job in enumerate(profile_jobs):
        if job["type"] != "formula":
            continue
        for var in job["variables"]:
            if var["kind"] != "job":
                continue
            target = next((j for j in profile_jobs if j["id"] == var["job_id"]), None)
            where = f"'{job['label']}': {var['name']}"
            if target is None or target is job:
                raise JobError(f"{where}: başvurulan iş bulunamadı. İşi silmeden veya yeniden adlandırmadan önce hesaplamayı düzenleyin.")
            if target["type"] == "formula" and profile_jobs.index(target) > index:
                raise JobError(f"{where}: başka bir hesaplamayı kullanıyorsa o hesaplama listede daha üstte olmalı.")
            if var["column"] is None and target.get("columns"):
                var["column"] = "All Total"  # sütun seçilmediyse Event işinin ana sütunu
            if var["column"] not in (target.get("columns") or [None]):
                raise JobError(f"{where}: '{target['label']}' işinde {var['column'] or 'bu'} sütunu yok.")


def formula_job_ids(profile_jobs: list[dict], selected: list[dict]) -> list[str]:
    """Seçili hesaplamaların ihtiyaç duyduğu (zincirleme) iş kimlikleri."""
    by_id, needed, stack = {j["id"]: j for j in profile_jobs}, [], list(selected)
    while stack:
        job = stack.pop()
        for var in job.get("variables", []) if job.get("type") == "formula" else []:
            ref = by_id.get(var.get("job_id"))
            if ref is not None and ref["id"] not in needed:
                needed.append(ref["id"])
                stack.append(ref)
    return needed


def _attribute_source(raw, where: str) -> dict:
    if not isinstance(raw, dict) or raw.get('source') not in ('excel', 'google_sheets'):
        raise JobError(f'{where}: filtre kaynağı Excel veya Google Sheets olmalı.')
    if set(raw) - {'source', 'file', 'sheets_id', 'sheet', 'date_column', 'value_column', 'first_row', 'last_row', 'month_first'}:
        raise JobError(f'{where}: filtre kaynağında desteklenmeyen ayar var.')
    if 'month_first' in raw and type(raw['month_first']) is not bool:
        raise JobError(f'{where}: tarih sırası seçimi geçersiz.')
    out = {'source': raw['source'], 'sheet': _text(raw.get('sheet'), f'{where}: kaynak sayfa', max_len=100),
           'date_column': _column_ref(raw.get('date_column'), f'{where}: tarih sütunu'),
           'value_column': _column_ref(raw.get('value_column'), f'{where}: değer sütunu')}
    if out['date_column'] == out['value_column']:
        raise JobError(f'{where}: tarih ve değer sütunları farklı olmalı.')
    if out['source'] == 'excel':
        try:
            path = check_local_path(Path(_text(raw.get('file'), f'{where}: kaynak dosya', max_len=400)))
        except PathError as exc:
            raise JobError(f'{where}: {exc}') from None
        if path.suffix.lower() not in ('.xlsx', '.xlsm'):
            raise JobError(f'{where}: kaynak Excel dosyası .xlsx veya .xlsm olmalı.')
        out['file'] = str(path)
    else:
        out['sheets_id'] = sheets_id(raw.get('sheets_id', ''), where)
        if not out['sheets_id']:
            raise JobError(f'{where}: kaynak Google Sheets bağlantısı gerekli.')
    for key, default in (('first_row', 2), ('last_row', 10000)):
        out[key] = _bounded_int(raw.get(key), (1, 100000, default), f'{where}: kaynak satırı')
    if not 0 <= out['last_row'] - out['first_row'] < 20000:
        raise JobError(f'{where}: kaynak aralığı sıralı ve en çok 20.000 satır olmalı.')
    out['month_first'] = raw.get('month_first', False) is True
    return out


def _attribute_filters(raw, where: str) -> list[dict]:
    """Behavior kuralındaki '+ attribute filter' satırları (ör. item category · Equals · DemoStore)."""
    if not isinstance(raw, list) or len(raw) > 5:
        raise JobError(f"{where}: en çok 5 attribute filtresi eklenebilir.")
    out = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise JobError(f"{where}: {index}. attribute filtresi geçersiz.")
        name = _text(item.get("name"), f"{where}: {index}. attribute adı", max_len=120)
        operator = _text(item.get("operator") or "Equals", f"{where}: {index}. attribute operatörü", max_len=40)
        values = item.get("values")
        if isinstance(values, str):
            values = values.split(",")
        values = [v.strip() for v in (values or []) if isinstance(v, str) and v.strip()]
        if operator in NO_VALUE_OPERATORS:
            values = []
        # Boş değer kabul edilir: Netmera'da değeri girilmemiş filtreyle kayıtlı segmentler var (kullanıcı kaydı
        # 2026-10-27: 'item category · In' boş). Kurallar yeniden kurulursa satır yine boş eklenir.
        elif len(values) > 1 and operator not in ("In", "Not In"):
            raise JobError(f"{where}: '{name}' için birden çok değer yalnız In ile kullanılabilir.")
        attribute = {"name": name, "operator": operator, "values": [_text(v, f"{where}: değer", max_len=200) for v in values]}
        if item.get('value_source') is not None:
            if operator not in ('In', 'Not In', 'Equals', 'Not Equal'):
                raise JobError(f'{where}: tablodan değer yalnız In, Not In, Equals veya Not Equal için desteklenir.')
            attribute['value_source'] = _attribute_source(item['value_source'], where)
            attribute['values'] = []  # önceki tarihin elle yazılmış değerlerine geri dönüş yok
        out.append(attribute)
    if any(a.get('value_source') for a in out) and len({a['name'] for a in out}) != len(out):
        raise JobError(f'{where}: tablodan beslenen kuralda attribute adları tekil olmalı.')
    return out


def _bounded_int(raw, limits: tuple, label: str) -> int:
    low, high, default = limits
    value = default if raw in (None, "") else raw
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise JobError(f"{label} {low}–{high} arasında olmalı.")
    return value


def _normalize_segment_run(raw: dict, where: str) -> dict:
    """Segmenti arayıp bulur, 'between dates' kutularına çalışmanın tarihini yazar, kaydeder,
    Calculating bitene dek listeyi yeniler ve kullanıcı sayısını okur. İstenirse kuralları da yeniden kurar."""
    job = {"name": _text(raw.get("name"), f"{where}: Segment adı", max_len=100),
           "period": normalize_period(raw.get("period"), where),
           "rebuild": raw.get("rebuild", False) is True,
           "create_if_missing": raw.get("create_if_missing", False) is True,
           # Aralıktaki her gün ayrı kaydedilip hesaplanır (Event'teki 'Her günü ayrı çek' gibi).
           "split_days": raw.get("split_days", False) is True,
           "poll_seconds": _bounded_int(raw.get("poll_seconds"), SEGMENT_POLL_SECONDS, f"{where}: yenileme aralığı (sn)"),
           "max_wait_minutes": _bounded_int(raw.get("max_wait_minutes"), SEGMENT_MAX_WAIT_MINUTES, f"{where}: en uzun bekleme (dk)")}
    if job["create_if_missing"] and not job["rebuild"]:
        raise JobError(f"{where}: yeni segment oluşturmak için kuralları da tanımlayın (Kuralları yeniden kur).")
    if not job["rebuild"]:
        from .filter_source import dynamic
        if dynamic(raw):
            raise JobError(f'{where}: tablodan filtre için segment kurallarını alın ve Kuralları yeniden kur seçeneğini açın.')
        return job
    items_raw, connector = raw.get("items"), raw.get("connector", "AND")
    if not isinstance(items_raw, list) or not 1 <= len(items_raw) <= 10:
        raise JobError(f"{where}: 1–10 segment kuralı/grubu gerekir.")
    if connector not in ("AND", "OR"):
        raise JobError(f"{where}: bağlaç AND veya OR olmalı.")
    items = []
    for index, item in enumerate(items_raw, 1):
        if isinstance(item, dict) and item.get("kind") == "group":
            rules = item.get("rules")
            if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
                raise JobError(f"{where}: {index}. grup 1–8 kural içermeli.")
            if item.get("connector", "AND") not in ("AND", "OR"):
                raise JobError(f"{where}: {index}. grubun bağlacı AND veya OR olmalı.")
            items.append({"kind": "group", "connector": item.get("connector", "AND"),
                          "rules": [_people_rule(r, f"{where}: {index}. grup", segment=True) for r in rules]})
        else:
            items.append({"kind": "rule", **_people_rule(item, f"{where}: {index}. kural", segment=True)})
    rules = [r for it in items for r in (it["rules"] if it["kind"] == "group" else [it])]
    if not any(people_date_mode(r["field"]) in r.get("steps", []) for r in rules):
        raise JobError(f"{where}: tarihleri değiştirilecek en az bir 'between dates' kuralı gerekir.")
    job.update(items=items, connector=connector)
    return job


def _rule_from_node(node: dict) -> dict:
    """Paneldeki bir kural satırı → iş kuralı. Düğmeler şemadaki seçenek ailelerine göre ayrılır."""
    field = PEOPLE_FIELD_ALIASES.get(node.get("alan", "").casefold(), node.get("alan", ""))
    if field not in PEOPLE_FIELD_INFO:
        raise JobError(f"'{field}' alanı şemada yok; elle tanımlayın.")
    choices = {c for entry in PEOPLE_FIELD_INFO[field].get("satir_secimleri", [])
               for c in (entry.get("secenekler") if isinstance(entry.get("secenekler"), list) else [])}
    operators = set(VERSION_OPERATOR_SUGGESTIONS) | set(OPERATOR_SUGGESTIONS)
    rule = {"field": field}
    rest = []
    for text in node.get("ana") or node.get("dugmeler", []):
        if text == field:
            continue
        if text in operators and field == "App Version" and "operator" not in rule and not rest:
            if text != "Equals":  # sürüm operatörü değerden önce gelir; Equals panelin varsayılanı
                rule["operator"] = text
        elif text in choices:
            rule.setdefault("steps", []).append(text)
        else:
            rest.append(text)
    if not rest:
        raise JobError(f"'{field}' kuralının değeri okunamadı.")
    rule["value"] = rest[0]
    numbers = [int(n) for n in node.get("sayilar", []) if str(n).strip().isdigit()]
    if numbers:
        rule["numbers"] = numbers[:1] if people_date_mode(field) in rule.get("steps", []) else numbers
    attributes = [a for a in node.get("ozellikler", []) if a.get("name")]
    if attributes:
        rule["attributes"] = [{"name": a["name"], "operator": a.get("operator") or "Equals", "values": a.get("values", [])} for a in attributes]
    return rule


def _attribute_values_from_text(attrs: list[dict], inner: str) -> None:
    """'item category in DemoStore, DemoCategory' gibi parantez içinden, değeri boş attribute'lara değer yazar."""
    for k, attr in enumerate(attrs):
        if attr.get("values") or not attr.get("name"):
            continue
        operator = re.escape(attr.get("operator") or "") or r"\S+"
        match = re.search(re.escape(attr["name"]) + r"\s+(?:" + operator + r"|in|equals|=|is)\s+", inner, re.I)
        if not match:
            continue
        end = len(inner)
        for other in attrs[k + 1:]:
            nxt = re.search(r"\s*(?:[,;]|\band\b)?\s*" + re.escape(other["name"]) + r"\s", inner[match.end():], re.I)
            if nxt:
                end = min(end, match.end() + nxt.start())
        attr["values"] = [v.strip() for v in inner[match.end():end].split(",") if v.strip()]


def attribute_values_from_summary(node: dict, texts: list[str], name: str):
    """Tekil, güncel kural özetindeki attribute değerleri; belirsizlikte None (boş liste geçerlidir)."""
    rule = _rule_from_node(node)
    attrs = node.get('ozellikler', [])
    target = [a for a in attrs if a.get('name') == name]
    if len(target) != 1:
        return None
    mode = next((s for s in rule.get('steps', []) if s in ('Did', 'Did not')), None)
    parts = [rule['field']] + ([mode] if mode else []) + [rule['value']]
    prefix = r'^' + r'\s+'.join(re.escape(p) for p in parts) + r'(?=\s|\()'
    candidates = []
    for text in dict.fromkeys(re.sub(r'\s+', ' ', t).strip() for t in texts):
        if not re.search(prefix, text, re.I):
            continue
        tail = re.search(r'\((.*)\)\s*$', text)
        if not tail:
            continue
        inner = tail.group(1)
        pattern = re.escape(name) + r'\s+' + re.escape(target[0].get('operator') or 'Equals') + r'(?=\s|$)'
        matches = list(re.finditer(pattern, inner, re.I))
        if len(matches) != 1:
            continue
        match, end = matches[0], len(inner)
        for other in attrs:
            if other.get('name') == name:
                continue
            nxt = re.search(r'\s*(?:[,;]|\band\b)?\s*' + re.escape(other['name']) + r'\s+', inner[match.end():], re.I)
            if nxt:
                end = min(end, match.end() + nxt.start())
        candidates.append([v.strip() for v in inner[match.end():end].split(',') if v.strip()])
    return candidates[0] if len(candidates) == 1 else None


def fill_attribute_values(tree: list[dict], texts: list[str], *, prefer_summary=False) -> list[dict]:
    """Formda değer kutusu boş görünen attribute filtrelerinin değerlerini Netmera'nın kural özetinden
    ('Behavior Did not purchase between … (item category in DemoStore, DemoCategory)') tamamlar. Ağaç yerinde güncellenir."""
    rules = [n for n in tree if n["tur"] == "kural"]
    if prefer_summary:
        for node in rules:
            for attr in node.get('ozellikler', []):
                values = attribute_values_from_summary(node, texts, attr.get('name'))
                if values is not None:
                    attr['values'] = values
        return tree
    used: set[int] = set()
    for index, node in enumerate(rules):
        attrs = node.get("ozellikler") or []
        if not any(a.get("name") and not a.get("values") for a in attrs):
            continue
        buttons = node.get("ana") or node.get("dugmeler", [])
        fits = lambda t: all(a["name"].casefold() in t.casefold() for a in attrs if a.get("name")) and any(  # noqa: E731
            b.casefold() in t.casefold() for b in buttons if b not in ("Did", "Did not") and len(b) > 2)
        order = ([index] if len(texts) == len(rules) else []) + list(range(len(texts)))
        pick = next((i for i in order if i not in used and fits(texts[i])), None)
        if pick is None:
            continue
        used.add(pick)
        tail = re.search(r"\((.*)\)\s*$", texts[pick])
        if tail:
            _attribute_values_from_text(attrs, tail.group(1))
    return tree


def segment_items_from_tree(tree: list[dict]) -> dict:
    """Segment formundan okunan kural ağacını (PEOPLE_TREE_JS) 'Segment hesapla' kurallarına çevirir ve doğrular."""
    items, connector, group = [], "AND", None
    for node in tree:
        if node["tur"] == "grup":
            group = {"kind": "group", "connector": "AND", "rules": []}
            items.append(group)
        elif node["tur"] == "grup_son":
            group = None
        elif node["tur"] == "baglac" and node.get("deger") in ("AND", "OR"):
            if group is not None:
                group["connector"] = node["deger"]
            else:
                connector = node["deger"]
        elif node["tur"] == "kural":
            rule = _rule_from_node(node)
            if group is not None:
                group["rules"].append(rule)
            else:
                items.append({"kind": "rule", **rule})
    if not items:
        raise JobError("Segmentte kural bulunamadı.")
    checked = _normalize_segment_run({"name": "okunan", "rebuild": True, "items": items, "connector": connector}, "Okunan segment")
    return {"items": checked["items"], "connector": checked["connector"]}


def same_segment_rules(job: dict, read: dict) -> bool:
    """İşin kuralları formdan okunanlarla aynı mı (tarihler hariç; adım ve değer sırası önemsiz)."""
    def canon(items, connector):
        def rule(r):
            return {"field": r["field"], "value": r["value"], "operator": r.get("operator"),
                    "steps": sorted(r.get("steps", [])), "numbers": r.get("numbers", []),
                    "attributes": sorted((a["name"], a["operator"], tuple(sorted(a["values"]))) for a in r.get("attributes", []))}
        return {"connector": connector if len(items) > 1 else None,
                "items": [{"group": it["connector"], "rules": [rule(r) for r in it["rules"]]} if it["kind"] == "group"
                          else rule(it) for it in items]}
    return canon(job["items"], job["connector"]) == canon(read["items"], read["connector"])


def segment_rules_with_dates(job: dict, first: date, last: date) -> list[dict]:
    """Segment kurallarının, between dates kurallarına çalışmanın tarihi eklenmiş kopyası (People kurucusu için)."""
    interval = {"start": first.isoformat(), "end": last.isoformat()}

    def dated(rule):
        return {**rule, "date_range": interval} if people_date_mode(rule["field"]) in rule.get("steps", []) else rule
    return [{**it, "rules": [dated(r) for r in it["rules"]]} if it["kind"] == "group" else dated(it) for it in job["items"]]


def _legacy_people_items(raw: dict) -> tuple[list, str]:
    """v0.2/v0.3 People biçimlerini, kullanıcının 2026-10-16'da gösterdiği yapıya çevirir:
    sürüm kuralları (OR) + grup[AND](App Installation = Installed)."""
    if "platforms" in raw:
        versions = [{"platform": p, "operator": raw.get("version_operator") or "Greater Than", "version": raw.get("version", "")}
                    for p in raw["platforms"]]
        installed = "Installed"
    else:
        versions = [r for r in raw.get("rules", []) if r.get("field") == "app_version"]
        installs = [r.get("value", "Installed") for r in raw.get("rules", []) if r.get("field") == "app_installation"]
        installed = installs[0] if installs else None
    items = [{"kind": "rule", "field": "App Version", "value": f"{PLATFORMS.get(v.get('platform'), v.get('platform'))} {v.get('version')}",
              "operator": v.get("operator") or "Greater Than"} for v in versions]
    if installed:
        items.append({"kind": "group", "connector": "AND", "rules": [{"field": "App Installation", "value": installed}]})
    return items, "OR"


def _normalize_people(raw: dict, where: str, *, allow_incomplete_dates: bool = False) -> dict:
    """People = panelde sırayla kurulan kurallar ve gruplar (Event gibi dinamik; alan adları paneldeki gibi).

    Prod gözlemi (2026-10-16): üst düzeyde yalnız ilk bağlaç seçilebilir, diğerleri onu kopyalar; her
    grubun kendi AND/OR seçicisi vardır. Bu yüzden 'A VEYA B, VE C' için C bir grupta kurulur.
    """
    items_raw, connector = raw.get("items"), raw.get("connector", "AND")
    if items_raw is None:
        items_raw, connector = _legacy_people_items(raw)
    if not isinstance(items_raw, list) or not 1 <= len(items_raw) <= 10:
        raise JobError(f"{where}: 1–10 People kuralı/grubu gerekir.")
    if connector not in ("AND", "OR"):
        raise JobError(f"{where}: bağlaç AND veya OR olmalı.")
    items = []
    for index, item in enumerate(items_raw, 1):
        if isinstance(item, dict) and item.get("kind") == "group":
            rules = item.get("rules")
            if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
                raise JobError(f"{where}: {index}. grup 1–8 kural içermeli.")
            group_connector = item.get("connector", "AND")
            if group_connector not in ("AND", "OR"):
                raise JobError(f"{where}: {index}. grubun bağlacı AND veya OR olmalı.")
            items.append({"kind": "group", "connector": group_connector,
                          "rules": [_people_rule(r, f"{where}: {index}. grup", allow_incomplete_dates=allow_incomplete_dates) for r in rules]})
        else:
            items.append({"kind": "rule", **_people_rule(item, f"{where}: {index}. kural", allow_incomplete_dates=allow_incomplete_dates)})
    count_from = raw.get("count_from", "results_total")
    if count_from not in PEOPLE_COUNT_SOURCES:
        raise JobError(f"{where}: sayı kaynağı geçersiz.")
    return {"items": items, "connector": connector, "count_from": count_from}


def people_rule_text(rule: dict) -> str:
    if rule["field"] == "App Version":
        head = f"App Version {rule.get('operator', 'Equals')} {rule['value']}"
    else:
        head = f"{rule['field']} = {rule['value']}"
    tail = [" ".join(rule.get("steps", []))] if rule.get("steps") else []
    if rule.get("numbers"):
        tail.append(" ".join(str(n) for n in rule["numbers"]))
    if rule.get("date_range"):
        interval = rule["date_range"]
        tail.append(f"{date.fromisoformat(interval['start']):%d.%m.%Y}–{date.fromisoformat(interval['end']):%d.%m.%Y}")
    for item in rule.get("attributes", []):
        values = ", ".join(item["values"]) or ("" if item["operator"] in NO_VALUE_OPERATORS else "(değer boş)")
        tail.append(" ".join(["[" + item["name"], item["operator"], values]).strip() + "]")
    return " · ".join([head] + [t for t in tail if t])


def people_text(job: dict) -> str:
    joiner = " VE " if job["connector"] == "AND" else " VEYA "
    parts = []
    for item in job["items"]:
        if item["kind"] == "group":
            inner = (" VE " if item["connector"] == "AND" else " VEYA ").join(people_rule_text(r) for r in item["rules"])
            parts.append(f"[grup {item['connector']}: {inner}]")
        else:
            parts.append(people_rule_text(item))
    return joiner.join(parts)


def normalize_period(raw, where="") -> dict:
    raw = raw or {"type": "yesterday"}
    kind = raw.get("type") if isinstance(raw, dict) else None
    if kind not in PERIODS:
        raise JobError(f"{where}: tarih aralığı seçilmedi.")
    if kind == "last_n_days":
        days = raw.get("days")
        if type(days) is not int or not 1 <= days <= 90:
            raise JobError(f"{where}: gün sayısı 1 ile 90 arasında olmalı.")
        return {"type": kind, "days": days}
    if kind == "custom":
        first, last = parse_day_range(raw.get("start"), raw.get("end"), where)
        return {"type": kind, "start": first.isoformat(), "end": last.isoformat()}
    return {"type": kind}


def parse_day_range(start, end, where="") -> tuple[date, date]:
    """'YYYY-MM-DD' aralığı; bitiş boşsa başlangıçla aynı gün. Gelecek gün kabul edilmez."""
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) if end else first
    except (TypeError, ValueError):
        raise JobError(f"{where}: tarih YYYY-AA-GG biçiminde olmalı.") from None
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    if first > last:
        raise JobError(f"{where}: başlangıç tarihi bitişten sonra olamaz.")
    if last > today:
        raise JobError(f"{where}: gelecek bir gün seçilemez.")
    if (last - first).days > 365:
        raise JobError(f"{where}: aralık en çok 1 yıl olabilir.")
    return first, last


def period_days(period: dict, planned_at: datetime) -> tuple[date, date]:
    """Panelde seçilecek ilk ve son gün (ikisi de dahil)."""
    today = planned_at.astimezone(ZoneInfo(TIMEZONE)).date()
    if period["type"] == "today":
        return today, today
    if period["type"] == "custom":
        return date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
    last = today - timedelta(days=1)
    first = last - timedelta(days=period.get("days", 1) - 1)
    return first, last


def period_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    zone = ZoneInfo(TIMEZONE)
    return datetime.combine(first, time.min, zone), datetime.combine(last + timedelta(days=1), time.min, zone)


def signature(job: dict) -> str:
    """Sorguyu tanımlayan alanların kısa özeti. Filtre değişirse Excel'de ayrı kayıt olur."""
    payload = {k: v for k, v in job.items() if k not in {"id", "label", "enabled"}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


def column_label(job: dict, column: str | None) -> str:
    return job["label"] if column in (None, "All Total") else f"{job['label']} [{column}]"


def period_text(period: dict) -> str:
    if period["type"] == "last_n_days":
        return f"Son {period['days']} gün"
    if period["type"] == "custom":
        first, last = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
        return f"{first:%d.%m.%Y}" + ("" if first == last else f"–{last:%d.%m.%Y}")
    return PERIODS[period["type"]].split(" (")[0]


def describe(job: dict, labels: dict | None = None) -> str:
    """labels: iş kimliği → iş adı (hesaplamada başvurulan işin adını göstermek için)."""
    if job["type"] == "ga4":
        from .ga4 import isler as G
        return "GA4 · " + G.describe({"ad": job["label"], "etkin": job["enabled"], **job["ga4"]})
    if job["type"] == "formula":
        parts = []
        for var in job["variables"]:
            if var["kind"] == "job":
                name = (labels or {}).get(var["job_id"], var["job_id"])
                parts.append(f"{var['name']} = '{name}'" + (f" [{var['column']}]" if var["column"] not in (None, "All Total") else ""))
                continue
            where = {"output": "çıktı dosyası", "excel": Path(var.get("file", "")).name, "google_sheets": "Google Sheet"}[var["source"]]
            spot = (var["cell"] if var["mode"] == "fixed_cell" else
                    f"{var['date_column']} sütununda günün satırı → {var['value_column']}" if var["mode"] == "date_row" else
                    f"{var['header_row']}. satırda günün sütunu → {var['value_row']}. satır")
            parts.append(f"{var['name']} = {where} · {var['sheet']} · {spot}")
        return f"Hesaplama: {job['expression']} · " + " · ".join(parts)
    if job["type"] == "segment_run":
        rules = f"kurallar yeniden kurulur: {people_text(job)}" if job.get("rebuild") else "mevcut kuralların between dates tarihleri değişir"
        create = (" · yoksa oluşturulur" if job.get("create_if_missing") else "") + (" · her gün ayrı" if job.get("split_days") else "")
        return (f"Segment: {job['name']} · {period_text(job['period'])} · {rules}{create} · kaydet → Calculating bitene dek "
                f"{job['poll_seconds']} sn'de bir yenile (en çok {job['max_wait_minutes']} dk) → Users")
    if job["type"] == "event":
        parts = [f"{job['event']} · {METRICS[job['metric']]}",
                 f"Tag: {job['audience_name']}" if job.get('audience') == 'tag' else 'All Users']
        for f in job["filters"]:
            parts.append(" ".join([f["property"], f["operator"], ", ".join(f["values"])]).strip())
        period = job["period"]
        if period["type"] == "last_n_days":
            parts.append(f"Son {period['days']} gün")
        elif period["type"] == "custom":
            first, last = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
            parts.append(f"{first:%d.%m.%Y}" + ("" if first == last else f"–{last:%d.%m.%Y}"))
        else:
            parts.append(PERIODS[period["type"]].split(" (")[0])
        if job["columns"] != ["All Total"]:
            parts.append("Sütun: " + ", ".join(job["columns"]))
        if job.get("empty_is_zero"):
            parts.append("kayıt yoksa 0")
        return " · ".join(parts)
    if job["type"] == "people":
        source = "alttaki toplam (Show results)" if job["count_from"] == "results_total" else "'people match' sayacı"
        return f"{people_text(job)} · {source}"
    return f"{JOB_TYPES[job['type']]}: {job['name']} · listedeki kullanıcı sayısı"


def _schedule(raw, default_name: str = '') -> dict:
    raw = raw or {}
    times = raw.get("times", [])
    if not isinstance(times, list) or any(not isinstance(t, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in times):
        raise JobError("Saatler SS:DD biçiminde olmalı (ör. 09:00).")
    days = raw.get("days", DAYS[:5])
    if not isinstance(days, list) or any(d not in DAYS for d in days):
        raise JobError("Günler geçersiz.")
    if times and not days:
        raise JobError("Zamanlama için en az bir gün seçin.")
    name = raw.get('name','')
    if name == '':
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]',' ',default_name)[:80]
    if not isinstance(name,str) or len(name.strip()) > 80 or re.search(r'[\\/:*?"<>|\x00-\x1f]',name):
        raise JobError('Görev adı en çok 80 karakter olmalı; / \\ : * ? " < > | ve kontrol karakterleri kullanılamaz.')
    return {"times": sorted(set(times)), "days": [d for d in DAYS if d in days], 'name':' '.join(name.split())}


def normalize_profile(raw: dict, *, allow_incomplete_dates: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise JobError("Profil bir nesne olmalı.")
    profile_id = raw.get("id")
    if not isinstance(profile_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", profile_id):
        raise JobError("Profil kimliği geçersiz.")
    source = raw.get("source", "netmera")
    if source not in SOURCES:
        raise JobError("Kaynak Netmera veya Demo olmalı.")
    browser = raw.get("browser", "chrome")
    if browser not in BROWSERS:
        raise JobError("Tarayıcı Chrome veya Edge olmalı.")
    timeout = raw.get("timeout_seconds", 45)
    if type(timeout) is not int or not 10 <= timeout <= 300:
        raise JobError("Bekleme süresi 10–300 saniye olmalı.")
    step_delay = raw.get("step_delay_ms", 300)
    if type(step_delay) is not int or not 0 <= step_delay <= 3000:
        raise JobError("Adımlar arası bekleme 0–3000 ms olmalı.")
    on_error = raw.get("on_error", "write_successful")
    if on_error not in ON_ERROR:
        raise JobError("Hata politikası geçersiz.")
    catch_up = raw.get("catch_up_days", 7)
    if type(catch_up) is not int or not 0 <= catch_up <= 14:
        raise JobError("Kaçan günleri tamamlama 0–14 gün olmalı.")
    app = raw.get("netmera_app", "")
    output = raw.get("output_file", "")
    if not isinstance(app, str) or not isinstance(output, str):
        raise JobError("Uygulama adı ve Excel yolu metin olmalı.")
    target=raw.get('output_target','excel')
    if target not in ('excel','google_sheets'):raise JobError('Çıktı hedefi Excel veya Google Sheets olmalı.')
    sheets_id=raw.get('sheets_id','')
    if not isinstance(sheets_id,str):raise JobError('Google Sheets bağlantısı metin olmalı.')
    sheets_id=sheets_id.strip()
    if sheets_id.startswith('https://'):
        from urllib.parse import urlsplit
        url=urlsplit(sheets_id)
        matched=re.match(r'^/spreadsheets/d/([A-Za-z0-9_-]+)(?:/|$)',url.path)
        if url.netloc!='docs.google.com' or not matched:raise JobError('Google Sheets dosyasının düzenleme bağlantısını girin.')
        sheets_id=matched[1]
    if sheets_id and not re.fullmatch(r'[A-Za-z0-9_-]{15,160}',sheets_id):raise JobError('Google Sheets dosya kimliği geçersiz.')
    excel_mode = raw.get("excel_mode", "rebuild")
    if excel_mode not in EXCEL_MODES:
        raise JobError("Excel yazma biçimi geçersiz.")
    if target=='google_sheets' and excel_mode!='mapped_cells':raise JobError('Google Sheets için Belirli hücrelere yaz modunu seçin.')
    excel_sheet = _text(raw.get("excel_sheet", "Netmera"), "Excel sayfa adı", max_len=31)
    if re.search(r"[\\/*?:\[\]]", excel_sheet) or excel_sheet.startswith("'") or excel_sheet.endswith("'") or excel_sheet.casefold() == "_netmerakayit":
        raise JobError("Excel sayfa adı geçersiz veya uygulama kaydı için ayrılmış.")
    if source == "netmera" and "uat" in app.casefold():
        raise JobError("Bu uygulama prod içindir; UAT uygulaması seçilemez.")
    jobs_raw = raw.get("jobs", [])
    if not isinstance(jobs_raw, list):
        raise JobError("İş listesi geçersiz.")
    jobs = [normalize_job(item, allow_incomplete_dates=allow_incomplete_dates) for item in jobs_raw]
    formula_refs(jobs)
    from .ga4.projeler import ProjeHatasi, ga4_ayari
    try:
        ga4 = ga4_ayari(raw.get("ga4"))
    except ProjeHatasi as exc:
        raise JobError(str(exc)) from None
    ids, labels = set(), set()
    for job in jobs:
        base, n = job["id"], 2
        while job["id"] in ids:
            suffix = f'_{n}'
            job["id"], n = base[:60 - len(suffix)] + suffix, n + 1
        ids.add(job["id"])
        for column in job.get("columns", [None]):
            label = column_label(job, column).casefold()
            if label in labels:
                raise JobError(f"İş adı tekrar ediyor: '{job['label']}'. Excel sütunları için her ad tekil olmalı.")
            labels.add(label)
    from .cell_mapping import normalize_mappings
    excel_mappings=normalize_mappings(raw.get('excel_mappings',[]),jobs)
    return {
        "id": profile_id,
        "name": _text(raw.get("name"), "Profil adı", max_len=60),
        "source": source,
        "netmera_app": app.strip(),
        "output_file": output.strip(),
        "output_target": target,
        "sheets_id": sheets_id,
        "excel_mode": excel_mode,
        "excel_sheet": excel_sheet,
        "excel_mappings": excel_mappings,
        "browser": browser,
        "headless": raw.get("headless", False) is True,
        "timeout_seconds": timeout,
        "step_delay_ms": step_delay,
        "on_error": on_error,
        "catch_up_days": catch_up,
        "notify": raw.get("notify", False) is True,
        "schedule": _schedule(raw.get("schedule"),f"{raw['name']} günlük raporu"),
        "jobs": jobs,
        "ga4": ga4,
    }


def output_set(profile: dict) -> bool:
    """Excel/Google Sheets hücre hedefi seçilmiş mi."""
    return bool(profile.get('sheets_id')) if profile.get('output_target') == 'google_sheets' else bool(profile.get('output_file'))


def ga4_sheets_jobs(profile: dict) -> list[dict]:
    """Kendi Sheets sekmesine yazılan GA4 işleri ('etkin'den bağımsız; Analytics Veri Çekme kuralı)."""
    return [j for j in profile["jobs"] if j["type"] == "ga4" and j["ga4"].get("sheets") is not None]


def ga4_only(profile: dict) -> bool:
    """Netmera paneline gitmeyen proje: etkin işlerin hepsi GA4/Hesaplama (ör. Analytics'ten aktarılan proje)."""
    enabled = [j for j in profile["jobs"] if j["enabled"]]
    return all(j["type"] in ("ga4", "formula") for j in enabled) and bool(enabled or ga4_sheets_jobs(profile))


def readiness(profile: dict) -> list[str]:
    """Çalıştırmadan önce eksik ayarlar (boşsa hazır). Yalnız GA4 olan projede Netmera uygulaması ve (GA4 Sheets
    sekmelerine yazılıyorsa) Excel hedefi istenmez."""
    missing = []
    for job in profile["jobs"]:
        if job["enabled"]:
            try:
                normalize_job(job)
            except JobError as exc:
                missing.append(f"İşler: {exc}")
    if not any(job["enabled"] for job in profile["jobs"]) and not ga4_sheets_jobs(profile):
        missing.append("En az bir işi etkinleştirin.")
    netmera_jobs = [j for j in profile["jobs"] if j["enabled"] and j["type"] not in ("ga4", "formula")]
    if profile["source"] == "netmera" and not profile["netmera_app"] and (netmera_jobs or not ga4_only(profile)):
        missing.append("Ayarlar: Netmera'da seçili uygulamanın tam adını yazın.")
    if ga4_only(profile) and not output_set(profile):
        return missing  # sonuçlar GA4 Sheets sekmelerine/ekrana; hücre hedefi isteğe bağlı
    if profile.get('output_target')=='google_sheets':
        if not profile.get('sheets_id'):missing.append('Ayarlar: Google Sheets dosya bağlantısını girin.')
    elif not profile["output_file"]:
        missing.append("Ayarlar: Excel çıktı dosyasını seçin.")
    else:
        try:
            path = check_local_path(Path(profile["output_file"]))
            if path.suffix.lower() != ".xlsx":
                missing.append("Ayarlar: Excel dosyası .xlsx ile bitmeli.")
        except PathError as exc:
            missing.append(f"Ayarlar: {exc}")
    if profile.get('excel_mode')=='mapped_cells' and not profile.get('excel_mappings'):
        missing.append('Ayarlar: Google Sheets hücre eşlemesi ekleyin.' if profile.get('output_target')=='google_sheets' else 'Ayarlar: Excel hücre eşlemesi ekleyin.')
    return missing
