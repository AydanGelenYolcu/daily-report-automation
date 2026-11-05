"""Netmera paneli okuyucusu (gerçek Chrome/Edge, ayrı kalıcı profil).

Yalnız tanımlı sorguların toplamlarını okur. Kişi listesi açmaz, dışa aktarma
yapmaz, mesaj göndermez, API anahtarı kullanmaz. Tek yazma işlemi: kullanıcının
tanımladığı "Segment hesapla" işinde segmentin tarihlerini/kurallarını değiştirip
kaydetmek (kullanıcı kararı, 2026-10-27).

Seçiciler UAT arayüzünde gözlenen etiketlere dayanır (2026-10-15). Prod'da
bir adım bulunamazsa sayı tahmin edilmez: adım adı ve hata kodu döner.
Tag/Segment ekranları ve "Dün" dışındaki tarihler gözlenmemiş, DENEYSEL'dir.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import re
import time
from urllib.parse import urlparse

from zoneinfo import ZoneInfo

from .jobs import (COLUMNS, METRICS, MULTI_VALUE_OPERATORS, PEOPLE_FIELD_INFO, PLATFORMS, people_date_mode,
                   attribute_values_from_summary, fill_attribute_values, same_segment_rules, segment_items_from_tree, segment_rules_with_dates)
from .paths import TIMEZONE
from . import filter_source

PANEL_URL = "https://panel.netmera.com/"
APP_TITLE = '[class*="app-subtitle_"]:visible'
# People filtre grubu kabı: sınıfı 'group-container_' ile BAŞLAR. Aynı adı içeren başka bir sarmalayıcı da var (prod, 2026-10-16).
GROUP = '[class^="group-container_"], [class*=" group-container_"]'
PEOPLE_DATE_INPUT = ('input[type="date"], input[placeholder*="DD/MM" i], '
                     'input[placeholder="Start date"], input[placeholder="End date"], .ant-picker input')
# People kural ağacı: kural (alan + düğme yazıları), bağlaç (seçici veya etiket), grup sınırları. Sayı okunmaz.
PEOPLE_TREE_JS = """() => {
  const txt = e => (e ? (e.innerText || '').replace(/\\s+/g, ' ').trim() : '');
  const area = document.querySelector('[class*="targeting-condition-list"]');
  const out = [];
  const walk = (node, depth) => {
    for (const ch of node.children) {
      const cls = String(ch.className || '');
      if (cls.includes('condition-wrapper')) {
        // Attribute satırı: [ad][operatör][değer kutusu/kutuları]. 'ana' = bu satırların dışındaki düğmeler.
        const attrs = [...ch.querySelectorAll('.ant-space-horizontal')].filter(s => s.querySelector('input[placeholder="Enter value..."]'));
        out.push({tur: 'kural', derinlik: depth, alan: txt(ch.querySelector('[class*="condition-type"]')),
                  dugmeler: [...ch.querySelectorAll('button')].map(txt).filter(Boolean),
                  ana: [...ch.querySelectorAll('button')].filter(b => !attrs.some(s => s.contains(b))).map(txt).filter(t => t && !t.startsWith('+ ')),
                  ozellikler: attrs.map(s => { const b = [...s.querySelectorAll('button')].map(txt).filter(t => t && !t.startsWith('+ '));
                    return {name: b[0] || '', operator: b[1] || '',
                            values: [...[...s.querySelectorAll('input[placeholder="Enter value..."]')].map(i => i.value.trim()),
                                     ...[...s.querySelectorAll('.ant-tag')].map(txt)].filter(Boolean)}; }),
                  sayilar: [...ch.querySelectorAll('input[role="spinbutton"]')].map(e => e.value),
                  tarihler: [...ch.querySelectorAll('input[type="date"], input[placeholder*="DD/MM" i], input[placeholder="Start date"], input[placeholder="End date"], .ant-picker input')]
                    .filter(e => e.getClientRects().length).map(e => ({value:e.value, type:e.type}))});
      } else if (/(^|\\s)group-container_/.test(cls) && ch.querySelector('.ant-select')) {
        out.push({tur: 'grup', derinlik: depth}); walk(ch, depth + 1); out.push({tur: 'grup_son', derinlik: depth});
      } else if (ch.querySelector(':scope > .nm-select-wrapper, :scope > [class*="logic-label"]')) {
        const sel = ch.querySelector('.ant-select-selection-item'), lab = ch.querySelector('[class*="logic-label"]');
        out.push({tur: 'baglac', derinlik: depth, deger: txt(sel || lab)});
      } else { walk(ch, depth); }
    }
  };
  if (area) walk(area, 0);
  return out;
}"""
# Dimensions bölümü: metrik radyoları ve günlük kırılım (prod, kullanıcı kaydı 2026-10-19).
DIM_RADIO = {"total_event_count": "TOTAL", "unique_user_count": "UNIQUE_USER", "by_session": "SESSION"}
DAYS_BREAKDOWN = "Days of a Month"
DAY_CHUNK = 10  # Dimensions tablosu sayfa başına 10 satır gösterir; tek sorgu en çok 10 gün okur.
NO_RECORD_TEXT = "No valid record found"  # Event Insight boş sonuç yazısı (prod, 2026-10-16)
# Prod menüsündeki bağlantı adresleri (2026-10-16 keşfi).
SCREENS = {"event": "/analytics/event-insight", "people": "/targeting/people",
           "tag": "/targeting/tags/list", "segment": "/targeting/segment/list"}
# Segment düzenleme formu: /targeting/segment/save/<ID> (yapı çıkarımı 2026-10-27). Kaydet = data-testid save-segment.
SEGMENT_SAVE = "/targeting/segment/save"
SEGMENT_LIST_JS = """t => {
  const head = [...t.querySelectorAll('thead tr')].pop();
  return {headers: head ? [...head.querySelectorAll('th')].map(c => c.innerText.trim()) : [],
          rows: [...t.querySelectorAll('tbody tr')].filter(r => !r.classList.contains('ant-table-measure-row'))
            .map(r => ({key: r.getAttribute('data-row-key'), cells: [...r.querySelectorAll('td')].map(c => c.innerText.trim())}))};
}"""
SEGMENT_WAITING = ("calculating", "pending", "queued", "processing", "in progress")
SEGMENT_DONE = ("ready", "active", "completed", "calculated")
ATTRIBUTE_VALUE = 'input[placeholder="Enter value..."]'

STEPS = {
    "PANEL_OPEN": "Panel açılıyor",
    "NAV": "Menüden ekrana gidiliyor",
    "DATE_SET": "Tarih aralığı seçiliyor",
    "EVENT_SELECT": "Event seçiliyor",
    "FILTER_ADD": "Event property filtresi ekleniyor",
    "FILTER_OPEN": "Filtre: 'Add Event Property' penceresi açılıyor",
    "FILTER_PROPERTY": "Filtre: property seçiliyor",
    "FILTER_OPERATOR": "Filtre: operatör seçiliyor",
    "FILTER_VALUE": "Filtre: değer yazılıyor",
    "FILTER_CONFIRM": "Filtre: 'Add' ile onaylanıyor",
    "AUDIENCE_CHECK": "Event hedef kitlesi seçilip kontrol ediliyor",
    "METRIC_SELECT": "Metrik seçiliyor",
    "QUERY_RUN": "Find çalıştırılıp sonuç bekleniyor",
    "RESULT_READ": "Sonuç tablosu okunuyor",
    "PEOPLE_FILTER": "People filtreleri kuruluyor",
    "PEOPLE_GROUP": "People: 'Add Filter Group' ve grubun bağlacı seçiliyor",
    "PEOPLE_RULE": "People: 'Add Filter Rule' ve kural alanı seçiliyor",
    "PEOPLE_VALUE": "People: kuralın değeri aranıp seçiliyor",
    "PEOPLE_STEP": "People: kuralın ek seçenekleri seçiliyor",
    "PEOPLE_NUMBER": "People: kuralın sayıları giriliyor",
    "PEOPLE_DATE": "People: başlangıç ve bitiş tarihleri girilip kontrol ediliyor",
    "PEOPLE_VERIFY": "People: kurulan kurallar iş tanımıyla karşılaştırılıyor",
    "PEOPLE_VERSION": "People: sürüm aranıp seçiliyor",
    "PEOPLE_OPERATOR": "People: sürüm operatörü seçiliyor",
    "PEOPLE_INSTALLED": "People: 'App Installation' kuralı ekleniyor",
    "PEOPLE_CONNECTOR": "People: kurallar arası bağlaç seçiliyor",
    "PEOPLE_RESULTS": "People: Show results ve alttaki toplam okunuyor",
    "PEOPLE_COUNT": "Kişi sayacı okunuyor",
    "LIST_SEARCH": "Listede ad aranıyor",
    "SEGMENT_OPEN": "Segment düzenleme formu açılıyor (Edit)",
    "SEGMENT_READ": "Segmentin kuralları okunuyor (kaydedilmez)",
    "SEGMENT_CREATE": "Yeni segment formu açılıyor (Create Segment)",
    "SEGMENT_CLEAR": "Segmentin mevcut kuralları siliniyor",
    "SEGMENT_DATES": "Segmentin between dates tarihleri yazılıyor",
    "SEGMENT_ATTRIBUTE": "Kurala attribute filtresi ekleniyor",
    "ATTRIBUTE_CLEAR": "Seçili attribute değerleri × ile temizleniyor",
    "ATTRIBUTE_VALUES": "Attribute değerleri sırayla giriliyor (+ Add Value)",
    "ATTRIBUTE_ADD": "Yeni attribute satırı ekleniyor",
    "ATTRIBUTE_NAME": "Attribute adı seçiliyor",
    "ATTRIBUTE_OPERATOR": "Attribute operatörü seçiliyor",
    "SEGMENT_SAVE": "Segment kaydediliyor (Save)",
    "SEGMENT_WAIT": "Segment hesaplaması bekleniyor (Calculating)",
    "CATALOG": "Event listesi okunuyor",
}

ERRORS = {
    "LOGIN_REQUIRED": "Panelde oturum yok veya uygulama seçilmemiş. 'Giriş yap' ile bir kez giriş yapın.",
    "LOGIN_CONFIG_REQUIRED": "Otomatik giriş ayarı kullanılamadı. Ayarlar → Otomatik giriş bölümünü kontrol edin.",
    "LOGIN_FORM_UNVERIFIED": "Netmera giriş formunun yapısı doğrulanamadı; hesap bilgileri gönderilmedi.",
    "AUTO_LOGIN_FAILED": "Otomatik giriş tamamlanamadı. Tekrar denemeler durduruldu; hesabı/şifreyi kontrol edip Giriş ayarlarını kaydet düğmesine basın.",
    "AUTO_LOGIN_ACTION_REQUIRED": "Girişte ek doğrulama, şifre yenileme veya uygulama seçimi gerekiyor. Giriş yap düğmesiyle elle tamamlayın; ardından giriş ayarını yeniden kaydedin.",
    "AUTO_LOGIN_LIMIT": "Bu çalışmada bir kez otomatik giriş denendi. Oturum yeniden düştü; tekrar parola gönderilmedi.",
    "APP_MISMATCH": "Panelde açık uygulama, Ayarlar'daki uygulama adıyla aynı değil. Doğru uygulamayı seçin veya adı düzeltin.",
    "ORIGIN_MISMATCH": "Tarayıcı panel.netmera.com dışında bir sayfada.",
    "OPTION_MISSING_OR_AMBIGUOUS": "Açılır listede bu ad bulunamadı veya birden çok eşleşti. Paneldeki yazımla birebir aynı olmalı.",
    "DATE_MISMATCH": "Panelde seçilen tarih beklenen günle aynı değil; sayı okunmadı.",
    "AUDIENCE_UNVERIFIED": "Event hedef kitlesi veya seçili Tag adı doğrulanamadı; sayı okunmadı.",
    "METRIC_SELECTION_UNVERIFIED": "Metrik düğmesi seçilemedi.",
    "COUNT_REFRESH_UNVERIFIED": "Hesaplamanın bittiği doğrulanamadı (yükleme işareti görülmedi).",
    "METRIC_TABLE_MISMATCH": "Sonuç tablosu seçilen metriği göstermiyor.",
    "SESSION_SECTION_UNVERIFIED": "Find sonrası Dimensions bölümü açılamadı veya bu sorguya ait tek bölüm bulunamadı (ana tablodaki event satırının seçim kutusu ile açılır); sayı okunmadı.",
    "TABLE_SCHEMA_CHANGED": "Sonuç tablosunun sütunları beklenenden farklı; panel değişmiş olabilir.",
    "RESULT_ROW_MISSING_OR_AMBIGUOUS": "Tabloda bu event için tek bir sonuç satırı bulunamadı.",
    "PEOPLE_STRUCTURE_MISMATCH": "People ekranında kurulan kurallar iş tanımıyla aynı değil; sayı okunmadı. Ayrıntıda farkı görürsünüz.",
    "PEOPLE_STEP_CONTROL_MISSING": "Ek seçenek için doğru açılır alan bulunamadı veya tek değil. Panel yapısı şemayla eşleşmiyor; başka bir alana tıklanmadı.",
    "PEOPLE_STEP_UNVERIFIED": "Ek seçenek tıklandı fakat kural satırında seçildiği doğrulanamadı; sayı okunmadı.",
    "PEOPLE_DATE_CONTROL_MISSING": "Bu kuralda iki yazılabilir tarih kutusu bulunamadı. Tarih seçicisi beklenen yapıda değil; sayı okunmadı.",
    "PEOPLE_DATE_MISMATCH": "People kuralındaki tarihler seçilen aralıkla aynı değil; sayı okunmadı.",
    "NO_RECORD": "Panel bu sorgu için kayıt bulamadı ('No valid record found'). Gerçekten sıfır olabilir ya da filtre değeri yanlış yazılmış olabilir (büyük/küçük harf dahil). Panelde elle kontrol edin; sıfırsa işte 'Kayıt yoksa 0 yaz'ı açın.",
    "QUERY_FILTER_UNVERIFIED": "Tablodaki filtre yazısı istenen filtreyle eşleşmedi.",
    "COUNT_INVALID": "Sayı okunamadı (boş, tire veya beklenmeyen biçim). Sıfır yazılmadı.",
    "COUNT_MISSING_OR_AMBIGUOUS": "Kişi sayacı bulunamadı.",
    "APP_VERSION_OPTION_UNAVAILABLE": "Bu sürüm panelde yok (ör. 'Android 1.2.3'). Başka sürüm seçilmedi.",
    "UNEXPECTED_EXISTING_FILTER": "People ekranında önceden kalmış filtre var; boş formla başlanamadı.",
    "NAME_NOT_FOUND_OR_AMBIGUOUS": "Listede bu ad tek ve birebir eşleşmedi.",
    "SEGMENT_FORM_MISMATCH": "Açılan segment formundaki ad aranan segmentle aynı değil; hiçbir şey değiştirilmedi.",
    "SEGMENT_NO_DATE_RULE": "Segmentte 'between dates' seçili kural bulunamadı; tarih yazılmadı, kaydedilmedi.",
    "SEGMENT_CLEAR_FAILED": "Segmentin mevcut kuralları silinemedi; kaydedilmedi.",
    "SEGMENT_RULES_UNREADABLE": "Segmentin kuralları okundu ama iş tanımına çevrilemedi; kuralları elle girin veya ayrıntıyı bildirin.",
    "ATTRIBUTE_UNVERIFIED": "Attribute filtresi (ad, operatör veya değer) kuralda doğrulanamadı; kaydedilmedi.",
    "ATTRIBUTE_SOURCE": "Tarihe bağlı filtre kaynağı okunamadı; segment kaydedilmedi.",
    "ATTRIBUTE_TARGET": "Seçili attribute veya diğer segment kuralları doğrulanamadı; segment kaydedilmedi.",
    "SEGMENT_SAVE_UNVERIFIED": "Save sonrası segment listesine dönülmedi (onay penceresi veya form hatası olabilir). Otomatik onay verilmedi.",
    "SEGMENT_CALC_TIMEOUT": "Segment ayarlanan süre içinde hesaplanmadı (hâlâ Calculating). Sayı okunmadı; panelde kontrol edin.",
    "SEGMENT_CALC_FAILED": "Segment hesaplaması beklenmeyen durumla bitti; sayı okunmadı.",
    "NAV_FAILED": "Menüde ekran bağlantısı bulunamadı.",
    "BROWSER_NOT_FOUND": "Seçilen tarayıcı bu bilgisayarda bulunamadı. Ayarlar'dan diğerini (Chrome/Edge) seçin.",
    "PROFILE_IN_USE": "Otomasyon profili başka bir pencerede açık. O pencereyi kapatıp tekrar deneyin.",
    "BROWSER_START_FAILED": "Tarayıcı başlatılamadı (kurum politikası otomasyonu engelliyor olabilir).",
    "FORMULA_INPUT_MISSING": "Hesaplamanın kullandığı işin sonucu bu çalışmada yok; hesap yapılmadı.",
    "FORMULA_INPUT_FAILED": "Hesaplamanın kullandığı işlerden biri hatalı; hesap yapılmadı, hücreye yazılmadı.",
    "FORMULA_CELL_INVALID": "Hesaplamanın okuduğu hücre boş, sayı değil veya bulunamadı; hesap yapılmadı, hücreye yazılmadı.",
    "FORMULA_ERROR": "Formül hesaplanamadı (ör. sıfıra bölme); hücreye yazılmadı.",
    "GA4_ERROR": "GA4 sorgusu tamamlanamadı; sayı yazılmadı. Ayrıntıda GA4'ün nedeni var.",
    "GA4_NOT_CONNECTED": "GA4 bağlantısı kurulmadı veya mülk seçilmedi: Bağlantılar → Google Analytics'ten Google hesabınla bağlan ve mülk seç.",
    "CANCELLED": "Kullanıcı durdurdu.",
    "TIMEOUT": "Adım süre sınırında tamamlanmadı.",
    "STEP_FAILED": "Adım tamamlanamadı.",
}
# Bu hatalarda aynı iş yeniden denenmez (segmenti tekrar kaydedip saatlerce beklemek anlamsız).
NO_RETRY = {"SEGMENT_CALC_TIMEOUT", "SEGMENT_CALC_FAILED", "GA4_ERROR", "GA4_NOT_CONNECTED", "ATTRIBUTE_SOURCE", "ATTRIBUTE_TARGET"}
FATAL = {"CANCELLED", "LOGIN_REQUIRED", "APP_MISMATCH", "ORIGIN_MISMATCH", "LOGIN_CONFIG_REQUIRED", "LOGIN_FORM_UNVERIFIED",
         "AUTO_LOGIN_FAILED", "AUTO_LOGIN_ACTION_REQUIRED", "AUTO_LOGIN_LIMIT"}


class PanelError(Exception):
    def __init__(self, code: str, step: str | None = None, detail: str | None = None):
        super().__init__(code)
        self.code, self.step, self.detail = code, step, detail

    def message(self) -> str:
        text = ERRORS.get(self.code, self.code)
        if self.step:
            text = f"[{STEPS.get(self.step, self.step)}] {text}"
        if self.detail:
            text += f" ({self.detail})"
        return text


# Tag seçeneklerinin sonundaki kişi sayısı: "over_3.8.1_19EKIM(123242)" / "Ad (1.234)". Yalnız sayı kabul edilir.
COUNT_SUFFIX = r"(?:\s*\(\s*\d[\d.,\s]*\))?"


def without_count(text: str) -> str:
    """'ad(123242)' → 'ad'. Parantez içinde sayı yoksa metin olduğu gibi kalır."""
    return re.sub(r"\s*\(\s*\d[\d.,\s]*\)\s*$", "", (text or "").strip())


_BROWSER_REASONS = (
    ("strict mode violation", "birden çok eleman eşleşti"),
    ("not editable", "kutu yazılabilir değil"),
    ("not visible", "eleman görünür değil"),
    ("not attached", "eleman sayfadan kalktı"),
    ("intercepts pointer events", "üstte başka bir eleman var"),
    ("context was destroyed", "sayfa yeniden yüklendi"),
    ("target closed", "tarayıcı penceresi kapandı"),
)


def browser_reason(exc: Exception) -> str:
    """Playwright hatasının bilinen nedeni (sayfa metni aktarılmaz)."""
    text = str(exc).lower()
    for needle, reason in _BROWSER_REASONS:
        if needle in text:
            return reason
    return f"tarayıcı hatası: {type(exc).__name__}"


def parse_count(text) -> int:
    if not isinstance(text, str):
        raise PanelError("COUNT_INVALID")
    value = text.strip().replace(" ", " ").replace(" ", " ")
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    for separator in (".", ",", " "):
        if re.fullmatch(r"[1-9][0-9]{0,2}(?:" + re.escape(separator) + r"[0-9]{3})+", value):
            return int(value.replace(separator, ""))
    raise PanelError("COUNT_INVALID")


COUNT_PATTERN = re.compile(r"\d{1,3}(?:[.,   ]\d{3})+|\d{3,}")


def mask(text) -> str:
    """Teşhis metinlerinde sayıları ve e-postaları gizler (sürüm gibi kısa sayılar kalır)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\S+@\S+", "<e-posta>", text)
    return COUNT_PATTERN.sub("#", text)[:80]


def _cells(row) -> list[tuple[str, int]]:
    """Başlık hücresi: 'metin' veya [metin, colspan]."""
    return [(c[0].strip(), int(c[1] or 1)) if isinstance(c, (list, tuple)) else (c.strip(), 1) for c in row]


def header_summary(headers) -> str:
    return "başlıklar: " + " || ".join(" | ".join(mask(t) + (f"/{s}" if s > 1 else "") for t, s in _cells(row)) for row in headers)[:400]


def event_table_layout(headers, metric_label: str) -> tuple[int, int, int | None, list[str]]:
    """(event hücresi, metrik grubunun ilk hücresi, filtre hücresi, metrik grubunun alt sütun adları).

    Prod'da birden çok metrik grubu yan yana olabilir (2026-10-16 keşfi). Konumlar başlıkların
    colspan değerlerinden hesaplanır; alt satırda yalnız çok sütunlu grupların başlıkları bulunur.
    """
    if not headers:
        raise PanelError("METRIC_TABLE_MISMATCH", detail="tablo başlığı yok")
    top = _cells(headers[0])
    sub = [t for t, _ in _cells(headers[-1])] if len(headers) > 1 else []
    cell = sub_at = 0
    positions: dict[str, int] = {}
    metric = None
    for text, span in top:
        if metric_label in text:
            if metric is not None:
                raise PanelError("METRIC_TABLE_MISMATCH", detail=header_summary(headers))
            metric = (cell, sub[sub_at:sub_at + span] if span > 1 else [])
        positions.setdefault(text, cell)
        cell += span
        if span > 1:
            sub_at += span
    if metric is None:
        raise PanelError("METRIC_TABLE_MISMATCH", detail=header_summary(headers))
    if "Events" not in positions or positions["Events"] > metric[0]:
        raise PanelError("TABLE_SCHEMA_CHANGED", detail=header_summary(headers))
    return positions["Events"], metric[0], positions.get("Filter"), metric[1]


def extract_event_count(headers, rows: list[list[str]], event: str, metric_label: str, column: str = "All Total") -> int:
    """Event Insight tablosundan tek event satırının, istenen metrik grubundaki sütunu okur; platformları toplamaz."""
    event_at, start, _, names = event_table_layout(headers, metric_label)
    if names.count(column) != 1:
        raise PanelError("TABLE_SCHEMA_CHANGED", detail=f"'{column}' yok; " + header_summary(headers))
    populated = [r for r in rows if len(r) > event_at and r[event_at]]
    matching = [r for r in populated if r[event_at] == event]
    if len(populated) != 1 or len(matching) != 1 or len(matching[0]) < start + len(names):
        raise PanelError("RESULT_ROW_MISSING_OR_AMBIGUOUS",
                         detail=f"dolu satır {len(populated)}, eşleşen {len(matching)}, hücre {[len(r) for r in matching][:3]}")
    return parse_count(matching[0][start + names.index(column)])


def extract_named_count(headers: list[str], rows: list[list[str]], name: str) -> tuple[int, str | None]:
    """Tags/Segments tablosunda adı birebir eşleşen tek satırın kullanıcı sayısı."""
    count_index = None
    for pattern in (r"^user count$", r"^number of users$", r"user count", r"users"):
        found = [i for i, h in enumerate(headers) if re.search(pattern, h.strip(), re.I)]
        if found:
            count_index = found[0]
            break
    if count_index is None:
        raise PanelError("TABLE_SCHEMA_CHANGED")
    matches = [r for r in rows if any(cell.strip() == name for cell in r)]
    if len(matches) != 1 or len(matches[0]) <= count_index:
        raise PanelError("NAME_NOT_FOUND_OR_AMBIGUOUS")
    update = [i for i, h in enumerate(headers) if re.search(r"update", h, re.I)]
    note = matches[0][update[0]].strip() if update and len(matches[0]) > update[0] else None
    return parse_count(matches[0][count_index]), (f"Panel güncelleme: {note}" if note else None)


def extract_breakdown(headers, rows: list[list[str]], columns: list[str], first: date, last: date) -> dict:
    """Dimensions tablosu → {gün: {sütun: sayı}}.

    'Days of a Month' kırılımında ilk sütun ayın günüdür; gün numarası istenen aralıktaki tarihe
    çevrilir (aralık en çok 28 gün: aynı gün numarası iki kez geçemez). Kırılımsız tek satırlı özet
    yalnız tek günlük sorguda kabul edilir. Tabloda olmayan gün sonuçta yer almaz; sıfır uydurulmaz.
    """
    if len(headers) != 1:
        raise PanelError("TABLE_SCHEMA_CHANGED", detail=header_summary(headers))
    cells = _cells(headers[0])
    names = [name for name, _ in cells]
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    if names and names[0] not in COLUMNS:
        metrics = names[1:]
        if (names[0] != DAYS_BREAKDOWN or any(span != 1 for _, span in cells) or any(n not in COLUMNS for n in metrics)
                or len(set(metrics)) != len(metrics) or any(metrics.count(c) != 1 for c in columns)):
            raise PanelError("TABLE_SCHEMA_CHANGED", detail=header_summary(headers))
        if len(days) > 28:
            raise PanelError("TABLE_SCHEMA_CHANGED", detail="gün kırılımı en çok 28 günlük aralıkta okunur")
        by_number = {d.day: d for d in days}
        out: dict = {}
        for row in rows:
            if not any(value.strip() for value in row):
                continue
            label = row[0].strip() if row else ""
            if len(row) != len(names) or not label.isdigit() or int(label) not in by_number or by_number[int(label)] in out:
                raise PanelError("RESULT_ROW_MISSING_OR_AMBIGUOUS", detail=f"beklenmeyen gün satırı: {mask(label)}")
            out[by_number[int(label)]] = {c: parse_count(row[1 + metrics.index(c)]) for c in columns}
        return out
    if len(days) != 1:
        raise PanelError("TABLE_SCHEMA_CHANGED", detail="çok günlük sorguda günlük kırılım yok")
    return {first: {c: extract_session_count(headers, rows, c) for c in columns}}


def extract_session_count(headers, rows: list[list[str]], column: str) -> int:
    """Dimensions'ın kırılımsız, tek satırlı özeti. Event ana tablosu veya kırılımlar toplanmaz."""
    if len(headers) != 1:
        raise PanelError("TABLE_SCHEMA_CHANGED", detail=header_summary(headers))
    cells = _cells(headers[0])
    names = [name for name, _ in cells]
    if (not names or any(span != 1 or name not in COLUMNS for name, span in cells)
            or len(set(names)) != len(names) or names.count(column) != 1):
        raise PanelError("TABLE_SCHEMA_CHANGED", detail=header_summary(headers))
    populated = [row for row in rows if any(value.strip() for value in row)]
    if len(populated) != 1 or len(populated[0]) != len(names):
        raise PanelError("RESULT_ROW_MISSING_OR_AMBIGUOUS", detail="Dimensions özeti tek satır değil")
    return parse_count(populated[0][names.index(column)])


def _box_date(text: str) -> date | None:
    """Tarih kutusundaki değer (GG/AA/YYYY veya YYYY-AA-GG); boş/okunamazsa None."""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


class PanelReader:
    def __init__(self, page, expected_app: str, timeout_seconds: int = 45, log=print):
        self.page = page
        self.expected_app = expected_app
        self.timeout = timeout_seconds
        self.log = log
        self.cancel = None  # threading.Event; uzun segment beklemesini durdurmak için

    # --- ortak yardımcılar ---------------------------------------------------
    @contextmanager
    def step(self, code: str):
        self.log(f"   · {STEPS.get(code, code)}")
        try:
            yield
        except PanelError as exc:
            exc.step = exc.step or code
            raise
        except Exception as exc:  # Playwright hataları DOM metni içerebilir; yalnız tür ve bilinen nedenin adı aktarılır
            if type(exc).__name__ == "TimeoutError":
                raise PanelError("TIMEOUT", code) from None
            raise PanelError("STEP_FAILED", code, detail=browser_reason(exc)) from None

    def open_panel(self):
        with self.step("PANEL_OPEN"):
            self.page.goto(PANEL_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            try:
                self._wait_identity()
            except Exception:
                raise PanelError("LOGIN_REQUIRED") from None
        self.verify_app()

    def _wait_identity(self):
        # React oturumu geri yüklerken /login adresi form çizilmeden geçici olarak
        # görünebilir. Yalnız URL, oturumun sona erdiğinin kanıtı değildir.
        self.page.wait_for_function("""() => {
            const visible = s => [...document.querySelectorAll(s)].some(e=>e.getClientRects().length);
            const login = location.pathname.replace(/\\/+$/, '') === '/login';
            return login ? visible('form, input[type="password"], input[autocomplete="one-time-code"], iframe[title*="captcha" i]')
                         : visible('[class*="app-subtitle_"]');
        }""",
                                    timeout=self.timeout*1000)

    def recover(self):
        """Hatadan sonra açık kalan pencere/menüyü temizler: paneli baştan yükler.

        Böylece bir işin hatası sonraki işlerin menü tıklamalarını engellemez.
        """
        self.log("   ↺ panel sıfırlanıyor")
        try:
            for _ in range(2):
                self.page.keyboard.press("Escape")
        except Exception:
            pass
        self.open_panel()

    def snapshot(self) -> dict:
        """Teşhis: ekrandaki düğme, kutu, pencere ve tablo BAŞLIK adları. Hücre değeri okunmaz; sayılar maskelenir."""
        data = self.page.evaluate("""() => {
            const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
                               return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
            const q = (s, root = document) => [...root.querySelectorAll(s)].filter(vis);
            const t = e => (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').slice(0, 120);
            const box = i => ({tag: i.tagName.toLowerCase(), role: i.getAttribute('role'), type: i.getAttribute('type'),
                               placeholder: i.getAttribute('placeholder'), aria: i.getAttribute('aria-label')});
            const overlays = q('[role=dialog], .ant-modal, .ant-drawer-content, .ant-popover, .ant-dropdown, .ant-select-dropdown');
            return {
                path: location.pathname,
                overlays: overlays.map(d => ({role: d.getAttribute('role'), cls: String(d.className).slice(0, 100),
                    buttons: q('button, [role=button]', d).map(t), boxes: q('input, textarea, [role=combobox]', d).map(box),
                    options: q('.ant-select-item-option, .ant-dropdown-menu-item, [role=option], [role=menuitem]', d).map(t).slice(0, 40)})),
                buttons: q('button, [role=button]').map(t).slice(0, 250),
                boxes: q('input, textarea, [role=combobox]').map(box).slice(0, 80),
                tabpanels: q('[role=tabpanel]').map(p => p.getAttribute('aria-label') || p.getAttribute('aria-labelledby')),
                tables: q('table').map(tb => ({headers: [...tb.querySelectorAll('thead tr')].map(r => [...r.querySelectorAll('th')].map(t)),
                    body_rows: tb.querySelectorAll('tbody tr').length,
                    cells_per_row: [...tb.querySelectorAll('tbody tr')].slice(0, 3).map(r => r.querySelectorAll('td').length)})),
                people_counter_rows: q('li').filter(l => /people match/i.test(l.innerText)).map(t),
                headings: q('h1, h2, h3, h4, h5, h6, [role=heading]').map(t).slice(0, 30),
                dimension_sections: q('[class*="breakdownSection_"]').map(s => ({cls: String(s.className).slice(0, 100),
                    headings: q('h1, h2, h3, h4, h5, h6, [role=heading]', s).map(t),
                    radios: q('input[type=radio]', s).map(r => (r.value || '') + (r.checked ? '*' : '')),
                    selected_buttons: q('button.ant-btn-primary', s).map(t)})),
            };
        }""")

        def clean(value, dedup=False):
            if isinstance(value, str):
                return mask(value)
            if isinstance(value, list):
                items = [clean(v) for v in value]
                if not dedup:
                    return items
                seen, out = set(), []
                for item in items:
                    if repr(item) not in seen and item not in ("", None):
                        seen.add(repr(item))
                        out.append(item)
                return out
            if isinstance(value, dict):
                return {k: clean(v, dedup=k in ("buttons", "options")) for k, v in value.items()}
            return value
        return clean(data)

    def skeleton(self, selector: str, depth: int = 7) -> str:
        """Teşhis: bir bölgenin etiket/sınıf/rol iskeleti; metinler maskeli ve kısa."""
        text = self.page.evaluate("""([sel, depth]) => {
            const root = [...document.querySelectorAll(sel)].pop();
            if (!root) return '(yok)';
            const walk = (e, d) => {
                if (d > depth) return '';
                const cls = String(e.className || '').split(' ').filter(c => c && !c.startsWith('css-')).slice(0, 3).join('.');
                const role = e.getAttribute('role') ? `[${e.getAttribute('role')}]` : '';
                const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(' ').slice(0, 30);
                const kids = [...e.children].slice(0, 12).map(c => walk(c, d + 1)).filter(Boolean).join('');
                return `<${e.tagName.toLowerCase()}${cls ? '.' + cls : ''}${role}${own ? ' "' + own + '"' : ''}>${kids}</>`;
            };
            return walk(root, 0);
        }""", [selector, depth])
        return re.sub(r"[0-9]", "9", text)[:6000]

    def current_apps(self) -> list[str]:
        for attempt in range(3):
            try:
                return [t.strip() for t in self.page.locator(APP_TITLE).all_text_contents()]
            except Exception as exc:
                # Sayfa o anda yeniden yükleniyorsa okuma "context destroyed" hatası verir; yüklenmeyi bekle.
                if attempt == 2 or 'context was destroyed' not in str(exc).lower():
                    raise PanelError('STEP_FAILED', detail='sayfa yüklenirken uygulama adı okunamadı') from None
                try:
                    self.page.wait_for_load_state('domcontentloaded', timeout=self.timeout * 1000)
                except Exception:
                    pass
        return []

    def verify_app(self):
        url = urlparse(self.page.url)
        if url.scheme != "https" or url.netloc != "panel.netmera.com":
            raise PanelError("ORIGIN_MISMATCH")
        if url.path.rstrip('/') == '/login':
            raise PanelError('LOGIN_REQUIRED')
        labels = self.current_apps()
        if not labels:
            raise PanelError("LOGIN_REQUIRED")
        if labels != [self.expected_app]:
            raise PanelError("APP_MISMATCH", detail=f"panelde açık: {', '.join(labels)}")

    def _button(self, name, root=None):
        """Erişilebilir adı bu olan düğme. Prod'da düğmedeki simgenin adı başa eklenebilir
        ("plus Add Event Property", "searchFind"); küçük harfli tek simge kelimesine izin verilir.
        "Tag" aranırken "Add Tag" gibi başka düğmeler eşleşmez."""
        if isinstance(name, str):
            # Simge adı ile yazı arasında boşluk olmayabilir: "plusAdd Event Property".
            name = re.compile(r"^(?:[a-z][a-z0-9-]*\s*)?" + re.escape(name) + r"\s*$")
        return (root or self.page).get_by_role("button", name=name)

    def _option(self, label: str, with_count: bool = False):
        # Ant Design'ın sanal role=option öğeleri tıklanabilir öğe değildir.
        # Açılış animasyonunda liste kısa süre iki kopya olabilir; tek eşleşme kalana dek beklenir.
        # with_count: Tag listesi seçenekleri adın yanında kişi sayısı gösterir ("ad(123242)").
        suffix = COUNT_SUFFIX if with_count else ""
        matches = self.page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden):visible .ant-select-item-option").filter(
            has_text=re.compile(r"^\s*" + re.escape(label) + suffix + r"\s*$"))
        deadline, count = time.monotonic() + 10, 0
        while time.monotonic() < deadline:
            count = matches.count()
            if count == 1:
                self.page.wait_for_timeout(150)
                if matches.count() == 1:
                    matches.click()
                    return
            self.page.wait_for_timeout(150)
        raise PanelError("OPTION_MISSING_OR_AMBIGUOUS", detail=f"{label} ({'bulunamadı' if count == 0 else f'{count} eşleşme'})")

    def _goto(self, path: str):
        """Ekranı doğrudan adresiyle açar (menü üzerine gelme/tıklama gerektirmez).

        Adresler prod menüsündeki bağlantılardan okundu (2026-10-16). Her açılış boş bir sorgu formu verir.
        """
        self.page.goto(PANEL_URL.rstrip("/") + path, wait_until="domcontentloaded")
        try:
            self._wait_identity()
        except Exception:
            raise PanelError("LOGIN_REQUIRED") from None
        self.verify_app()
        if urlparse(self.page.url).path.rstrip("/") != path.rstrip("/"):
            raise PanelError("NAV_FAILED", detail=f"{path} yerine {urlparse(self.page.url).path}")
        # Ekran verisi (event listesi vb.) sayfa açıldıktan sonra yüklenir: ağ durulana dek bekle.
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        self._wait_idle()
        self.page.wait_for_timeout(500)

    def _wait_idle(self):
        self.page.wait_for_function("() => !document.querySelector('.ant-spin-spinning, .ant-btn-loading')",
                                    timeout=self.timeout * 1000)

    def _arm_refresh(self, table_root=None):
        """Tıklamadan önce çağrılır. Tazelik iki yolla anlaşılır: yükleme işareti görüldü, ya da
        tıklamadan sonra başlayan bir sunucu isteği (XHR/fetch) bitti. Yalnız istek türü ve
        zamanı izlenir; adres, gövde, başlık veya çerez okunmaz/kaydedilmez."""
        self._wait_idle()
        net = self._net = {"started": 0, "pending": 0, "last": time.monotonic()}

        def on_request(request):
            if request.resource_type in ("xhr", "fetch"):
                net["started"] += 1
                net["pending"] += 1
                net["last"] = time.monotonic()

        def on_done(request):
            if request.resource_type in ("xhr", "fetch"):
                net["pending"] = max(0, net["pending"] - 1)
                net["last"] = time.monotonic()

        self._net_handlers = (on_request, on_done)
        self.page.on("request", on_request)
        self.page.on("requestfinished", on_done)
        self.page.on("requestfailed", on_done)
        # Yükleme işaretinin (hızlı ekle/kaldır dahil) görüldüğünü izler; oturum/çerez okumaz.
        self.page.evaluate("""() => {
            window.__netmeraCountObserver?.disconnect();
            window.__netmeraCountFresh = false;
            const loading = /(?:^|\\s)(?:ant-spin-spinning|ant-btn-loading)(?:\\s|$)/;
            const isLoading = n => n?.nodeType === 1 &&
                (loading.test(n.getAttribute('class') || '') || n.querySelector('.ant-spin-spinning, .ant-btn-loading'));
            window.__netmeraCountObserver = new MutationObserver(records => {
                if (records.some(r => r.type === 'attributes'
                    ? loading.test(r.oldValue || '') || isLoading(r.target)
                    : [...r.addedNodes, ...r.removedNodes].some(isLoading))) {
                    window.__netmeraCountFresh = true;
                }
            });
            window.__netmeraCountObserver.observe(document.body,
                {subtree:true, childList:true, attributes:true, attributeFilter:['class'], attributeOldValue:true});
        }""")
        # Session seçimi istemcide de tabloyu değiştirebilir. Yalnız ilgili bölümdeki
        # tablo metni/satır değişimi sinyal sayılır; radio'nun değişmesi yeterli değildir.
        self._table_refresh = table_root is not None
        if table_root is not None:
            table_root.evaluate("""root => {
                window.__netmeraTableObserver?.disconnect();
                window.__netmeraTableFresh = false;
                window.__netmeraTableLast = Date.now();
                const inTable = node => (node.nodeType === 1 ? node : node.parentElement)?.closest('table');
                window.__netmeraTableObserver = new MutationObserver(records => {
                    if (records.some(r => inTable(r.target) || [...r.addedNodes, ...r.removedNodes].some(n =>
                        n.nodeType === 1 && (n.matches('table') || n.querySelector('table'))))) {
                        window.__netmeraTableFresh = true;
                        window.__netmeraTableLast = Date.now();
                    }
                });
                window.__netmeraTableObserver.observe(root, {subtree:true, childList:true, characterData:true});
            }""")

    def _finish_refresh(self, quiet: float = 1.0):
        """Hesap bitene dek bekler: (işaret görüldü VEYA istek başladı) ve yükleme yok ve ağ `quiet` sn sessiz."""
        net = self._net
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                marker = self.page.evaluate("() => window.__netmeraCountFresh === true")
                spinning = self.page.evaluate("() => !!document.querySelector('.ant-spin-spinning, .ant-btn-loading')")
                settled = net["pending"] == 0 and time.monotonic() - net["last"] >= quiet
                table_fresh, table_quiet = (False, True)
                if self._table_refresh:
                    table_fresh, elapsed = self.page.evaluate("() => [window.__netmeraTableFresh === true, Date.now() - window.__netmeraTableLast]")
                    table_quiet = elapsed >= quiet * 1000
                if (marker or net["started"] or table_fresh) and not spinning and settled and table_quiet:
                    return
                self.page.wait_for_timeout(100)
            raise PanelError("COUNT_REFRESH_UNVERIFIED")
        finally:
            self._end_refresh()

    def _end_refresh(self):
        self.page.evaluate("() => { window.__netmeraCountObserver?.disconnect(); window.__netmeraTableObserver?.disconnect(); }")
        on_request, on_done = self._net_handlers
        self.page.remove_listener("request", on_request)
        self.page.remove_listener("requestfinished", on_done)
        self.page.remove_listener("requestfailed", on_done)

    # --- işler --------------------------------------------------------------
    def fetch(self, job: dict, first: date, last: date):
        self.verify_app()
        if job["type"] == "event":
            return self._event(job, first, last), None
        if job["type"] == "people":
            return {None: self._people(job)}, None
        if job["type"] == "segment_run":
            value, note = self._segment_run(job, first, last)
            return {None: value}, note
        value, note = self._named_count(job)
        return {None: value}, note

    def _fresh_events(self):
        self._goto(SCREENS["event"])
        try:
            self._button("Add Event Property").wait_for(state="visible", timeout=self.timeout * 1000)
        except PanelError:
            raise
        except Exception:
            raise PanelError("STEP_FAILED", detail="Event Insight sayfasında 'Add Event Property' düğmesi görünmedi") from None

    def _set_dates(self, first: date, last: date):
        fmt = "%d/%m/%Y"
        start = self.page.get_by_placeholder("Start date", exact=True)
        end = self.page.get_by_placeholder("End date", exact=True)
        today = datetime.now(ZoneInfo(TIMEZONE)).date()
        if first == last == today - timedelta(days=1):
            self._button("Yesterday").click()  # prod'da gözlenen hazır seçim
        elif first == last == today:
            self._button("Today").click()
        else:
            # DENEYSEL: tarih kutusuna yazma. Sonuç aşağıda mutlaka doğrulanır.
            for box, value in ((start, first), (end, last)):
                box.click()
                box.press("Control+a")
                box.type(value.strftime(fmt))
                box.press("Enter")
            self.page.keyboard.press("Escape")
        if start.input_value() != first.strftime(fmt) or end.input_value() != last.strftime(fmt):
            raise PanelError("DATE_MISMATCH", detail=f"beklenen {first:%d.%m.%Y}–{last:%d.%m.%Y}")

    def _add_filter(self, item: dict):
        """Prod'daki 'Add Event Property' penceresi: property, operatör, değer kutusu.

        Değer kutusu otomatik tamamlamalıdır: Enter bir öneriyi seçebileceği için kutudan Tab ile çıkılır.
        In'de her değer, kutunun yanındaki 'Add' ile listeye eklenir; alttaki 'Add' pencereyi onaylar.
        """
        with self.step("FILTER_OPEN"):
            self._button("Add Event Property").click()
            dialog = self.page.get_by_role("dialog")
            dialog.wait_for(state="visible")
            body, footer = dialog.locator(".ant-modal-body"), dialog.locator(".ant-modal-footer")
        with self.step("FILTER_PROPERTY"):
            body.get_by_role("combobox").nth(0).fill(item["property"])
            self._option(item["property"])
        with self.step("FILTER_OPERATOR"):
            body.get_by_role("combobox").nth(1).click()
            self._option(item["operator"])
        if item["values"]:
            with self.step("FILTER_VALUE"):
                box = body.locator(".ant-select-auto-complete input").first
                inline_add = body.get_by_role("button", name="Add", exact=True)
                for value in item["values"]:
                    box.fill(value)
                    box.press("Tab")  # öneri listesini kapatır, yazılan değeri korur
                    if item["operator"] in MULTI_VALUE_OPERATORS:
                        inline_add.click()
                    elif box.input_value() != value:
                        raise PanelError("STEP_FAILED", detail="değer kutusu yazılanı korumadı")
        with self.step("FILTER_CONFIRM"):
            footer.get_by_role("button", name="Add", exact=True).click()
            dialog.wait_for(state="hidden")

    def _select_metric(self, metric_label: str):
        self._select_metrics({metric_label})

    def _select_metrics(self, labels: set):
        """Prod'da metrik düğmeleri açılıp kapanır; yalnız istenenler açık bırakılır (aynı tabloda yan yana gruplar).

        Panel tüm metriklerin kapanmasına izin vermez: önce istenenler açılır, sonra diğerleri kapatılır.
        """
        for label in sorted((METRICS[k] for k in ('total_event_count', 'unique_user_count')), key=lambda name: name not in labels):
            control = self._button(label)
            if not control.count():
                if label in labels:
                    raise PanelError("METRIC_SELECTION_UNVERIFIED", detail=f"'{label}' düğmesi yok")
                continue
            want = label in labels
            if ("ant-btn-primary" in (control.get_attribute("class") or "")) != want:
                control.click()
            if ("ant-btn-primary" in (control.get_attribute("class") or "")) != want:
                raise PanelError("METRIC_SELECTION_UNVERIFIED", detail=label)

    def _audience_root(self):
        # Canlı DOM: Target düğmeleri targetTypeRow_ içinde, seçici aynı card-body'de.
        root = self.page.locator('.ant-card-body:visible').filter(has=self.page.locator('[class*="targetTypeRow_"]'))
        if root.count() != 1:
            raise PanelError('AUDIENCE_UNVERIFIED', detail='tek hedef kitle alanı bulunamadı')
        return root

    def _verify_event_audience(self, job):
        root = self._audience_root()
        expected = 'Tag' if job['audience'] == 'tag' else 'All Users'
        selected = [label for label in ('All Users', 'Segment', 'Tag', 'Profile')
                    if 'ant-btn-primary' in (self._button(label, root).get_attribute('class') or '')]
        if selected != [expected]:
            raise PanelError('AUDIENCE_UNVERIFIED', detail='hedef türü iş tanımıyla eşleşmedi')
        if job['audience'] == 'tag':
            # Kartta event seçim kutusu da var; yalnız içinde bu Tag seçili olan kutuya bakılır
            # ve o kutuda başka bir Tag seçili olmamalı.
            selects = root.locator('.ant-select:visible')
            found = []
            for i in range(selects.count()):
                items = [without_count(x) for x in selects.nth(i).locator('.ant-select-selection-item').all_inner_texts()]
                if job['audience_name'] in items:
                    found.append(items)
            if found != [[job['audience_name']]]:
                raise PanelError('AUDIENCE_UNVERIFIED', detail='seçili Tag iş tanımıyla eşleşmedi')

    def _set_event_audience(self, job):
        root = self._audience_root()
        button = self._button('Tag' if job['audience'] == 'tag' else 'All Users', root)
        before = self._combobox_ids()
        if 'ant-btn-primary' not in (button.get_attribute('class') or ''):
            button.click()
        if job['audience'] == 'tag':
            box = self._tag_box(root, before)
            # Kullanıcının yaptığı gibi: kutuya tıkla (öneriler açılır), önerilere bakmadan adı klavyeyle yaz.
            self.page.locator('.ant-select').filter(has=box).first.click()
            box.press_sequentially(job['audience_name'], delay=40)
            self._option(job['audience_name'], with_count=True)
        self._verify_event_audience(job)

    def _combobox_ids(self) -> list[str]:
        return self.page.locator('input[role="combobox"]').evaluate_all(
            "es => es.filter(e => e.getClientRects().length).map(e => e.id || '')")

    def _tag_box(self, root, before: list[str]):
        """Tag'e basınca beliren arama kutusu. Kartta event kutusu da olduğundan yeni beliren kutu seçilir."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            new = [i for i in self._combobox_ids() if i and i not in before]
            if len(new) == 1:
                return self.page.locator(f'input[role="combobox"][id="{new[0]}"]')
            if len(new) > 1:
                raise PanelError('AUDIENCE_UNVERIFIED', detail='Tag kutusu tek değil')
            self.page.wait_for_timeout(150)
        # Tag zaten seçiliyse kutu önceden vardır: kartta event kutusu dışında tek kutu olmalı.
        boxes = root.locator('input[role="combobox"]:visible')
        event_box = self.page.get_by_role("combobox").first
        candidates = [boxes.nth(i) for i in range(boxes.count())
                      if boxes.nth(i).get_attribute('id') != event_box.get_attribute('id')]
        if len(candidates) != 1:
            raise PanelError('AUDIENCE_UNVERIFIED', detail='Tag arama kutusu bulunamadı')
        return candidates[0]

    # --- Event Insight -------------------------------------------------------
    def _query(self, job: dict, first: date, last: date, top_labels: set):
        """Sorguyu kurar ve Find'a basar; tarih, filtre ve hedef kitle doğrulanır. (tablo, başlıklar, satırlar, kayıt yok mu)"""
        event = job["event"]
        with self.step("NAV"):
            self._fresh_events()
        with self.step("DATE_SET"):
            self._set_dates(first, last)
        with self.step("EVENT_SELECT"):
            self.page.get_by_role("combobox").first.fill(event)
            self._option(event)
        for item in job["filters"]:
            self._add_filter(item)
        with self.step("AUDIENCE_CHECK"):
            self._set_event_audience(job)
        with self.step("METRIC_SELECT"):
            self._select_metrics(top_labels)
        table = self.page.locator("table").filter(has=self.page.get_by_role("columnheader", name="Events", exact=True))
        with self.step("QUERY_RUN"):
            # Panel başta varsayılan satırlar gösterir: bu Find'dan sonra biten bir hesap şart.
            self._arm_refresh()
            self._button(re.compile(r"Find$")).click()
            self._finish_refresh()
            table.wait_for(state="visible", timeout=self.timeout * 1000)
            self._wait_idle()
        self.verify_app()
        with self.step('AUDIENCE_CHECK'):
            self._verify_event_audience(job)
        with self.step("RESULT_READ"):
            headers = table.locator("thead tr").evaluate_all(
                "es => es.map(e => [...e.querySelectorAll('th')].map(c => [c.innerText.trim(), c.colSpan || 1]))")
            rows = table.locator("tbody tr").evaluate_all("es => es.map(e => [...e.querySelectorAll('td')].map(c => c.innerText.trim()))")
            if any(NO_RECORD_TEXT in cell for row in rows for cell in row):
                return table, headers, rows, True
            if job["filters"]:
                event_at, _, filter_at, _ = event_table_layout(headers, sorted(top_labels)[0])
                matching = [r for r in rows if len(r) > event_at and r[event_at] == event]
                if filter_at is None or len(matching) != 1 or len(matching[0]) <= filter_at:
                    raise PanelError("QUERY_FILTER_UNVERIFIED", detail=header_summary(headers))
                for item in job["filters"]:
                    if any(part not in matching[0][filter_at] for part in [item["property"], *item["values"]]):
                        raise PanelError("QUERY_FILTER_UNVERIFIED", detail="tablodaki filtre: " + mask(matching[0][filter_at]))
        return table, headers, rows, False

    def _dimensions(self, job: dict, first: date, last: date, table):
        """Find altındaki Dimensions bölümü. Kapalıysa ana tablodaki event satırının seçim radyosuyla açılır."""
        event = job["event"]
        sections = self.page.locator('[class*="breakdownSection_"]:visible').filter(
            has=self.page.locator('input[type="radio"][value="SESSION"]'))
        if sections.count() == 0:
            row = table.locator("tbody tr").filter(
                has=self.page.locator("td", has_text=re.compile(r"^\s*" + re.escape(event) + r"\s*$")))
            pick = row.locator('input[type="radio"]')
            if pick.count() != 1:
                raise PanelError('SESSION_SECTION_UNVERIFIED', detail='event satırının seçim kutusu tek değil')
            pick.check(force=True)
        sections.first.wait_for(state='visible', timeout=self.timeout * 1000)
        if sections.count() != 1:
            raise PanelError('SESSION_SECTION_UNVERIFIED')
        # Bölüm bu Find'dan sonra, bu event'in satırından açıldı; tarih Find öncesi kutularda doğrulandı.
        # Başlık varsa yalnız event adını taşıdığına bakılır (prod başlık biçimi tarih yazımında farklı olabilir).
        headings = [re.sub(r'\s+', ' ', h).strip() for h in sections.get_by_role('heading').all_inner_texts()]
        if headings and not any(event.casefold() in h.casefold() for h in headings):
            raise PanelError('SESSION_SECTION_UNVERIFIED', detail='bölüm başlığı: ' + ' | '.join(mask(h) for h in headings)[:160])
        return sections

    def _days_breakdown(self, section):
        """Varsa 'Days of a Month' kırılımını seçer. Düğme yoksa kırılımsız özet okunur."""
        button = self._text_button(DAYS_BREAKDOWN, section)
        if button.count() > 1:
            raise PanelError('SESSION_SECTION_UNVERIFIED', detail='gün kırılımı düğmesi tek değil')
        if button.count() == 1 and 'ant-btn-primary' not in (button.get_attribute('class') or ''):
            self._arm_refresh(section)
            try:
                button.click()
            except Exception:
                self._end_refresh()
                raise
            self._finish_refresh()
            if 'ant-btn-primary' not in (button.get_attribute('class') or ''):
                raise PanelError('METRIC_SELECTION_UNVERIFIED', detail='gün kırılımı seçilemedi')

    def _dimension_read(self, section, job: dict, first: date, last: date) -> dict:
        """Bölümde işin metrik radyosunu seçip tabloyu okur → {gün: {sütun: sayı}}."""
        radio = section.locator(f'input[type="radio"][value="{DIM_RADIO[job["metric"]]}"]')
        if radio.count() != 1:
            raise PanelError('METRIC_SELECTION_UNVERIFIED', detail=f'{DIM_RADIO[job["metric"]]} radyosu tek değil')
        # Bölüm bu Find'dan sonra açıldı: radyo zaten seçiliyse tablo tazedir, ek tıklama gerekmez.
        if not radio.is_checked():
            self._arm_refresh(section)
            try:
                radio.check()
            except Exception:
                self._end_refresh()
                raise
            self._finish_refresh()
        if not radio.is_checked() or section.locator('input[type="radio"][value]:checked').count() != 1:
            raise PanelError('METRIC_SELECTION_UNVERIFIED', detail='bölüm metriği doğrulanamadı')
        tables = section.locator('table:visible')
        if tables.count() != 1:
            raise PanelError('SESSION_SECTION_UNVERIFIED', detail='tek özet tablosu bulunamadı')
        headers = tables.locator('thead tr').evaluate_all(
            "es => es.map(e => [...e.querySelectorAll('th')].map(c => [c.innerText.trim(), c.colSpan || 1]))")
        rows = tables.locator('tbody tr').evaluate_all(
            "es => es.map(e => [...e.querySelectorAll('td')].map(c => c.innerText.trim()))")
        values = extract_breakdown(headers, rows, job['columns'], first, last)
        total = section.locator('.nm-pagination-total-item')
        if total.count() == 1 and total.inner_text().strip().isdigit() and int(total.inner_text().strip()) > len(values):
            raise PanelError('RESULT_ROW_MISSING_OR_AMBIGUOUS', detail='tablo birden çok sayfa; daha kısa aralık gerekir')
        return values

    def fetch_many(self, jobs: list[dict], first: date, last: date, per_day: bool = False) -> dict:
        """Aynı sorgunun (event, filtre, hedef kitle) işlerini tek Find ile okur.

        Total/Unique üst tablodan, By Session ve günlük değerler Dimensions bölümünden okunur.
        Dönüş: {iş id: değerler | PanelError}. per_day ise değerler {gün: {sütun: sayı} | PanelError}.
        """
        self.verify_app()
        base = jobs[0]
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        if per_day and len(days) > DAY_CHUNK:
            raise PanelError('STEP_FAILED', detail=f'tek sorgu en çok {DAY_CHUNK} gün okur')
        top = set() if per_day else {METRICS[j['metric']] for j in jobs if j['metric'] != 'by_session'}
        table, headers, rows, no_record = self._query(base, first, last, top or {METRICS['total_event_count']})
        out: dict = {}
        zero = lambda j: {c: 0 for c in j['columns']}  # noqa: E731
        if no_record:
            for j in jobs:
                if j.get('empty_is_zero'):
                    out[j['id']] = {d: zero(j) for d in days} if per_day else zero(j)
                else:
                    out[j['id']] = PanelError('NO_RECORD', 'RESULT_READ')
            return out
        dimension = []
        for j in jobs:
            if per_day or j['metric'] == 'by_session':
                dimension.append(j)
                continue
            try:
                out[j['id']] = {c: extract_event_count(headers, rows, base['event'], METRICS[j['metric']], c) for c in j['columns']}
            except PanelError as exc:
                exc.step = exc.step or 'RESULT_READ'
                out[j['id']] = exc
        if not dimension:
            return out
        try:
            with self.step('METRIC_SELECT'):
                section = self._dimensions(base, first, last, table)
                # Prod'da bölüm kırılımsız özetle açılır; tek gün için özet yeter, günlük kırılım yalnız çok günde seçilir.
                if len(days) > 1:
                    self._days_breakdown(section)
        except PanelError as exc:
            for j in dimension:
                out[j['id']] = exc
            return out
        for j in dimension:
            try:
                with self.step('RESULT_READ'):
                    by_day = self._dimension_read(section, j, first, last)
                if per_day:
                    out[j['id']] = {d: by_day.get(d) or (zero(j) if j.get('empty_is_zero') else PanelError('NO_RECORD', 'RESULT_READ'))
                                    for d in days}
                elif len(days) == 1:
                    if first in by_day:
                        out[j['id']] = by_day[first]
                    elif j.get('empty_is_zero'):
                        out[j['id']] = zero(j)
                    else:
                        raise PanelError('NO_RECORD', 'RESULT_READ')
                else:
                    # Çok günlük oturum toplamı: oturumlar günler arasında toplanabilir.
                    out[j['id']] = {c: sum(v[c] for v in by_day.values()) for c in j['columns']}
            except PanelError as exc:
                out[j['id']] = exc
        if any(not isinstance(v, PanelError) for v in out.values()):
            self.verify_app()
        return out

    def fetch_days(self, job: dict, first: date, last: date) -> dict:
        """Tek iş için günlük değerler: {gün: {sütun: sayı} | PanelError}."""
        result = self.fetch_many([job], first, last, per_day=True)[job['id']]
        if isinstance(result, PanelError):
            raise result
        return result

    def _session_values(self, job, first, last):
        """Mevcut sorgu ekranında By Session değerini okur (teşhis ve testler için)."""
        table = self.page.locator("table").filter(has=self.page.get_by_role("columnheader", name="Events", exact=True))
        with self.step('METRIC_SELECT'):
            section = self._dimensions(job, first, last, table)
            if last > first:
                self._days_breakdown(section)
        with self.step('RESULT_READ'):
            by_day = self._dimension_read(section, job, first, last)
        if first not in by_day:
            raise PanelError('NO_RECORD', 'RESULT_READ')
        return by_day[first]

    def _event(self, job: dict, first: date, last: date) -> dict:
        result = self.fetch_many([job], first, last)[job['id']]
        if isinstance(result, PanelError):
            raise result
        return result

    def _text_button(self, text: str, root=None):
        """Görünen yazısı tam bu olan düğme. Prod'da simgeler erişilebilir ada eklenir
        (ör. 'download App Installation', 'search Show results'), bu yüzden yazıya göre bulunur."""
        return (root or self.page).locator("button").filter(has_text=re.compile(r"^\s*" + re.escape(text) + r"\s*$"))

    def _popover_pick(self, text: str, search: str | None = None, *, detail: str | None = None):
        """Açık açılır pencerede (popover) yazısı `text` olan seçeneğe basar; gerekirse önce arar."""
        pop = self.page.locator(".ant-popover:visible")
        option = self._text_button(text, pop)
        box = pop.locator('input[placeholder^="Search"]')

        def visible(timeout):
            try:
                option.first.wait_for(state="visible", timeout=timeout)
                return True
            except Exception:
                return False

        found = visible(1500 if box.count() else 8000)
        for query in ([search] if search else []) + [text, text.split()[-1]]:
            if found or not box.count():
                break
            box.first.fill(query)
            found = visible(4000)
        if not found:
            raise PanelError("OPTION_MISSING_OR_AMBIGUOUS", detail=detail or text)
        option.first.click()

    def _add_people_rule(self, root, add_button, rule: dict):
        where = f"{rule['field']}: {rule['value']}"
        if any(s.casefold() in ("between dates", "date between") for s in rule.get("steps", [])) and not rule.get("date_range"):
            raise PanelError("PEOPLE_DATE_MISMATCH", step="PEOPLE_DATE", detail="başlangıç ve bitiş tarihi eksik")
        with self.step("PEOPLE_RULE"):
            add_button.click()
            self._popover_pick(rule["field"], detail=f"alan '{rule['field']}'")
        with self.step("PEOPLE_VALUE"):
            self._popover_pick(rule["value"], rule.get("search"), detail=where)
        if rule.get("operator"):
            with self.step("PEOPLE_OPERATOR"):
                wrapper = root.locator('[class*="condition-wrapper"]').filter(has=self._text_button(rule["value"])).filter(
                    has=self._text_button("Equals")).last
                self._text_button("Equals", wrapper).click()
                self._popover_pick(rule["operator"], detail=f"{where} operatör")
        for step in rule.get("steps", []):
            with self.step("PEOPLE_STEP"):
                self._choose_step(root, step, where, field=rule["field"])
        if rule.get("numbers"):
            with self.step("PEOPLE_NUMBER"):
                self._enter_numbers(root, rule["numbers"], where)
        if rule.get("date_range"):
            with self.step("PEOPLE_DATE"):
                self._enter_people_dates(root, rule["date_range"], where)

    def _choose_step(self, root, text: str, where: str, *, field: str):
        """Şemadaki seçenek ailesinin mevcut düğmesini açar (more than → exactly gibi).

        İstenen yeni değer, henüz satırın düğme yazısı olmayabilir. Alan şeması
        operatör/tarih/birim menülerini ayırır; yalnız son eklenen kural değiştirilir.
        """
        detail = f"{where} · adım '{text}'"
        families = [entry["secenekler"] for entry in PEOPLE_FIELD_INFO.get(field, {}).get("satir_secimleri", [])
                    if isinstance(entry.get("secenekler"), list) and text in entry["secenekler"]]
        if len(families) != 1:
            raise PanelError("PEOPLE_STEP_CONTROL_MISSING", detail=detail)
        family = re.compile(r"^\s*(?:" + "|".join(re.escape(v) for v in families[0]) + r")\s*$")
        exact = re.compile(r"^\s*" + re.escape(text) + r"\s*$")
        row = root.locator('[class*="condition-wrapper"]').last
        trigger = row.locator("button:visible").filter(has_text=family)
        try:
            trigger.first.wait_for(state="visible", timeout=6000)
        except Exception:
            raise PanelError("PEOPLE_STEP_CONTROL_MISSING", detail=detail) from None
        if trigger.count() != 1:
            raise PanelError("PEOPLE_STEP_CONTROL_MISSING", detail=detail)
        if trigger.inner_text().strip() == text:
            return  # Zaten seçili: menüyü gereksiz açma.
        self.page.keyboard.press("Escape")
        trigger.click()
        popup = self.page.locator(".ant-popover:visible, .ant-dropdown:visible, .ant-select-dropdown:visible")
        option = popup.locator("button:visible, .ant-select-item-option:visible, [role=menuitem]:visible, [role=option]:visible").filter(has_text=exact)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if option.count() == 1:
                self.page.wait_for_timeout(150)
                if option.count() == 1:
                    option.click()
                    break
            self.page.wait_for_timeout(100)
        else:
            raise PanelError("OPTION_MISSING_OR_AMBIGUOUS", detail=detail)
        selected = row.locator("button:visible").filter(has_text=exact)
        try:
            selected.first.wait_for(state="visible", timeout=6000)
        except Exception:
            raise PanelError("PEOPLE_STEP_UNVERIFIED", detail=detail) from None
        if selected.count() != 1:
            raise PanelError("PEOPLE_STEP_UNVERIFIED", detail=detail)

    def _enter_numbers(self, root, numbers: list, where: str):
        """Satırdaki sayı kutularına (ant-input-number, role=spinbutton) sırayla yazar; Tab ile onaylar."""
        boxes = root.locator('[class*="condition-wrapper"]').last.locator('input[role="spinbutton"]:visible')
        if boxes.count() < len(numbers):
            raise PanelError("STEP_FAILED", detail=f"{where} · {len(numbers)} sayı kutusu bekleniyordu, {boxes.count()} bulundu")
        for index, number in enumerate(numbers):
            box = boxes.nth(index)
            box.click()
            box.fill(str(number))
            box.press("Tab")
            self.page.wait_for_timeout(300)

    def _set_select(self, select, value: str):
        if select.locator(".ant-select-selection-item").inner_text().strip() != value:
            select.click()
            self._option(value)

    def _enter_people_dates(self, root, interval: dict, where: str, row=None):
        """Yalnız son (veya verilen) kuralın iki tarih kutusu. Takvim onayı sonrası değerleri tekrar okur."""
        row = row or root.locator('[class*="condition-wrapper"]').last
        boxes = row.locator(PEOPLE_DATE_INPUT).filter(visible=True)
        if boxes.count() != 2 or any(not boxes.nth(i).is_editable() for i in range(2)):
            raise PanelError("PEOPLE_DATE_CONTROL_MISSING", detail=where)
        values = [date.fromisoformat(interval[key]) for key in ("start", "end")]
        wanted = [v.isoformat() if boxes.nth(i).get_attribute("type") == "date" else v.strftime("%d/%m/%Y")
                  for i, v in enumerate(values)]
        # Takvimler birbirine bağlı: başlangıç mevcut bitişten sonra, bitiş mevcut başlangıçtan önce seçilemez.
        # Tarih ileri kayıyorsa önce bitiş yazılır (kullanıcı kaydı 2026-10-27), geri kayıyorsa önce başlangıç.
        current_end = _box_date(boxes.nth(1).input_value())
        order = (1, 0) if current_end and values[0] > current_end else (0, 1)
        for index in order:
            box, text = boxes.nth(index), wanted[index]
            box.click()
            box.fill(text)
            box.press("Enter")
            box.press("Tab")
            self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        if boxes.count() != 2 or [b.input_value() for b in boxes.all()] != wanted:
            raise PanelError("PEOPLE_DATE_MISMATCH", detail=where)

    def _people(self, job: dict) -> int:
        with self.step("NAV"):
            self._goto(SCREENS["people"])
            root = self.page.get_by_role("tabpanel", name="Filter people")
            root.wait_for(state="visible")
            if root.locator('[class*="condition-wrapper"]').count():
                raise PanelError("UNEXPECTED_EXISTING_FILTER")
        self._build_rules(root, job["items"], job["connector"])
        with self.step("PEOPLE_VERIFY"):
            self._verify_people(job)
        if job["count_from"] == "people_match":
            return self._people_match_counter(root)
        with self.step("PEOPLE_RESULTS"):
            # Kişi listesi açılır ama satırları OKUNMAZ; yalnız alttaki toplam alınır.
            total = self.page.locator(".nm-pagination-total-item")
            if total.count():
                raise PanelError("STEP_FAILED", detail="eski sonuç toplamı ekranda")
            self._arm_refresh()
            self._text_button("Show results", root).click()
            total.first.wait_for(state="visible", timeout=self.timeout * 1000)
            self._finish_refresh()
            if total.count() != 1:
                raise PanelError("COUNT_MISSING_OR_AMBIGUOUS", detail="alttaki toplam tek değil")
            value = self._stable_text(total.first)
            self.verify_app()
            return parse_count(value)

    def _build_rules(self, root, items: list, connector: str):
        """People ve segment formunda kuralları/grupları sırayla kurar (aynı Netmera bileşeni)."""
        top = root.locator("button:not(.ant-btn-ghost)")  # grubun kendi düğmeleri 'ghost' biçiminde (prod)
        top_select = root.locator('.ant-select:not([class^="group-container_"] .ant-select):not([class*=" group-container_"] .ant-select)')
        for index, item in enumerate(items):
            if item["kind"] == "group":
                with self.step("PEOPLE_GROUP"):
                    top.filter(has_text=re.compile(r"^\s*Add Filter Group\s*$")).click()
                    # Grup = kendi AND/OR seçicisi olan kap (aynı sınıf önekini taşıyan başka bir sarmalayıcı daha var).
                    group = root.locator(GROUP).filter(has=self.page.locator(".ant-select")).last
                    group.wait_for(state="visible")
                    self._set_select(group.locator(".ant-select").first, item["connector"])
                for rule in item["rules"]:
                    self._add_people_rule(root, self._text_button("Add Filter Rule", group).last, rule)
                    self._add_attributes(root, rule)
            else:
                self._add_people_rule(root, top.filter(has_text=re.compile(r"^\s*Add Filter Rule\s*$")).last, item)
                self._add_attributes(root, item)
            if index == 1:  # üst düzeyde ilk bağlaç ikinci öğeden sonra çıkar; sonrakiler onu kopyalar
                with self.step("PEOPLE_CONNECTOR"):
                    self._set_select(top_select.first, connector)

    # --- segment hesaplama (DENEYSEL: prod'da uçtan uca denenmedi) -----------------
    def _pick_any(self, text: str, detail: str):
        """Açılan menü popover ise _popover_pick, açılır liste ise seçenek; ikisi de birebir yazıyla."""
        menus = self.page.locator(".ant-popover:visible, .ant-select-dropdown:visible, .ant-dropdown:visible")
        try:
            menus.first.wait_for(state="visible", timeout=6000)
        except Exception:
            raise PanelError("OPTION_MISSING_OR_AMBIGUOUS", detail=f"{detail} (menü açılmadı)") from None
        if self.page.locator(".ant-popover:visible").count():
            return self._popover_pick(text, detail=detail)
        exact = re.compile(r"^\s*" + re.escape(text) + r"\s*$")
        option = menus.locator(".ant-select-item-option, [role=menuitem], [role=option]").filter(has_text=exact)
        if option.count() != 1:
            raise PanelError("OPTION_MISSING_OR_AMBIGUOUS", detail=detail)
        option.click()

    def _add_attributes(self, root, rule: dict):
        for item in rule.get("attributes", []):
            wrapper = root.locator('[class*="condition-wrapper"]').last
            self._add_attribute(wrapper, item, rule['field'] + ' attribute')

    def _add_attribute(self, wrapper, item: dict, where: str):
        with self.step("ATTRIBUTE_ADD"):
            rows = wrapper.locator(".ant-space-horizontal").filter(has=self.page.locator(ATTRIBUTE_VALUE))
            before = rows.count()
            add = self._text_button('+ attribute filter', wrapper)
            if add.count() != 1:
                raise PanelError('ATTRIBUTE_TARGET', detail='Bu kuralın + attribute filter düğmesi tekil bulunamadı.')
            add.click()
            # Bazı formlar önce boş/default attribute satırı ekler; bazıları doğrudan ad menüsü açar.
            menus = self.page.locator('.ant-popover:visible, .ant-select-dropdown:visible, .ant-dropdown:visible')
            deadline = time.monotonic() + min(self.timeout, 12)
            while rows.count() == before and not menus.count():
                if time.monotonic() > deadline:
                    raise PanelError('ATTRIBUTE_TARGET', detail='Ekle düğmesinden sonra yeni attribute satırı veya ad menüsü görünmedi.')
                self.page.wait_for_timeout(100)
            if rows.count() == before:
                with self.step('ATTRIBUTE_NAME'):
                    self._pick_any(item['name'], where + ' · attribute adı')
                while rows.count() == before:
                    if time.monotonic() > deadline:
                        raise PanelError('ATTRIBUTE_TARGET', detail='Ad seçiminden sonra yeni attribute satırı görünmedi.')
                    self.page.wait_for_timeout(100)
            if rows.count() != before + 1:
                raise PanelError('ATTRIBUTE_TARGET', detail='Eklenen attribute satırı tekil doğrulanamadı.')
            row = rows.nth(before)
            buttons = row.locator("button:visible")
            if buttons.count() < 2:
                raise PanelError('ATTRIBUTE_TARGET', detail='Yeni satırda attribute adı ve operatör düğmeleri bulunamadı.')
            def wait_label(index, label):
                until = time.monotonic() + min(self.timeout, 6)
                while buttons.nth(index).inner_text().strip() != label:
                    if self.cancel is not None and self.cancel.is_set():
                        raise PanelError('CANCELLED')
                    if time.monotonic() > until:
                        raise PanelError('ATTRIBUTE_UNVERIFIED', detail='Attribute adı veya operatörü seçimden sonra doğrulanamadı.')
                    self.page.wait_for_timeout(100)
            if buttons.nth(0).inner_text().strip() != item['name']:
                with self.step('ATTRIBUTE_NAME'):
                    buttons.nth(0).click()
                    self._pick_any(item['name'], where + ' · attribute adı')
                    wait_label(0, item['name'])
            if buttons.nth(1).inner_text().strip() != item["operator"]:
                with self.step('ATTRIBUTE_OPERATOR'):
                    buttons.nth(1).click()
                    self._pick_any(item["operator"], f"{where} operatör")
                    wait_label(1, item['operator'])
            with self.step('ATTRIBUTE_VALUES'):
                self._fill_attribute_values(row, item, where)

    def _fill_attribute_values(self, row, item, where):
        """Add Value: etiket + boş kutu veya eski değer + yeni boş kutu; her adımı geri okur."""
        total = len(item['values'])
        add = self._text_button('+ Add Value', row)
        if add.count() > 1 or (total > 1 and add.count() != 1):
            raise PanelError('ATTRIBUTE_UNVERIFIED', detail='+ Add Value düğmesi tekil bulunamadı.')
        commit = add.count() == 1
        boxes = row.locator(ATTRIBUTE_VALUE).filter(visible=True)
        if boxes.count() != 1:
            raise PanelError('ATTRIBUTE_UNVERIFIED', detail='Seçili attribute’ta tek değer giriş kutusu bekleniyor.')
        wrapper = row.locator('xpath=ancestor::*[contains(@class,"condition-wrapper")][1]')
        rule_index = wrapper.evaluate("r => [...document.querySelectorAll('[class*=\"condition-wrapper\"]')].indexOf(r)")
        nodes = [n for n in self.page.evaluate(PEOPLE_TREE_JS) if n['tur'] == 'kural']
        if rule_index < 0 or rule_index >= len(nodes):
            raise PanelError('ATTRIBUTE_TARGET', detail='Değer girilecek kuralın konumu doğrulanamadı.')
        node = nodes[rule_index]
        def state():
            # Değerler yalnız yerel doğrulamada kullanılır; teşhise/loga yazılmaz.
            current = row.evaluate("""r => {
                const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
                const boxes = [...r.querySelectorAll('input[placeholder="Enter value..."]')].filter(visible);
                const tags = [...r.querySelectorAll('.ant-tag')].filter(visible).map(e => e.innerText.trim()).filter(Boolean);
                return {boxes: boxes.length, empty: boxes.filter(e => !e.value.trim()).length,
                        empty_at: boxes.map((e,i) => !e.value.trim() ? i : -1).filter(i => i >= 0),
                        values: [...boxes.map(e => e.value.trim()).filter(Boolean), ...tags]};
            }""")
            local = wrapper.locator('[class*="condition-text"]:visible').all_inner_texts()
            # Yalnız açık formdaki güncel özet; listedeki eski kural özeti kullanılmaz.
            summary = attribute_values_from_summary(node, local or self._condition_texts(), item['name'])
            if summary is not None:
                current['values'] = summary
            return current
        if commit and (state()['values'] or boxes.first.input_value().strip()):
            raise PanelError('ATTRIBUTE_UNVERIFIED', detail='Değer girişinden önce eski attribute değerleri temizlenmemiş.')
        if total > 100:
            self.log(f'   · Attribute değerleri yazılıyor: 0/{total}')
        expected = set()
        for index, value in enumerate(item['values']):
            if self.cancel is not None and self.cancel.is_set():
                raise PanelError('CANCELLED')
            boxes = row.locator(ATTRIBUTE_VALUE).filter(visible=True)
            before = state()
            if commit and before['empty'] != 1:
                raise PanelError('ATTRIBUTE_UNVERIFIED', detail=f'{index + 1}/{total}: yeni değer için tek boş giriş kutusu bulunamadı.')
            box = boxes.nth(before['empty_at'][0]) if commit else boxes.first
            box.fill(value)
            if commit:
                add.click()
                expected.add(value)
                deadline = time.monotonic() + min(self.timeout, 10)
                while True:
                    if self.cancel is not None and self.cancel.is_set():
                        raise PanelError('CANCELLED')
                    current = state()
                    if (current['empty'] == 1 and len(current['values']) == index + 1
                            and set(current['values']) == expected):
                        break
                    if time.monotonic() > deadline:
                        raise PanelError('ATTRIBUTE_UNVERIFIED', detail=f'{index + 1}/{total}: Add Value sonrası değer listesi veya sonraki boş giriş doğrulanamadı (kutu: {current["boxes"]}, boş: {current["empty"]}, değer: {len(current["values"])}).')
                    self.page.wait_for_timeout(100)
            else:
                box.press('Tab')
            if total > 100 and (index == 0 or (index + 1) % 50 == 0 or index + 1 == total):
                self.log(f'   · Attribute değerleri yazılıyor: {index + 1}/{total}')
        entered = state()['values']
        if len(entered) != total or set(entered) != set(item['values']):
            raise PanelError('ATTRIBUTE_UNVERIFIED', detail='Girilen değerler attribute’un kaydedilmiş değer listesinde doğrulanamadı.')


    def _segment_list(self, name: str) -> list[dict]:
        """Segment listesinde adı birebir eşleşen satırlar: id, users (metin), status. Sayfa her çağrıda yeniden açılır."""
        with self.step("NAV"):
            self._goto(SCREENS["segment"])
            table = self.page.locator("table:visible").filter(has=self.page.get_by_role("columnheader", name="Name", exact=True))
            table.first.wait_for(state="visible", timeout=self.timeout * 1000)
            self._wait_idle()
        with self.step("LIST_SEARCH"):
            search = self.page.get_by_placeholder("Search by name", exact=True)
            search.last.fill(name)
            search.last.press("Enter")
            self.page.wait_for_timeout(800)
            self._wait_idle()
        self.verify_app()
        with self.step("RESULT_READ"):
            data = table.first.evaluate(SEGMENT_LIST_JS)
            headers = data["headers"]
            try:
                i_id, i_name, i_users, i_status = (headers.index(h) for h in ("ID", "Name", "Users", "Status"))
            except ValueError:
                raise PanelError("TABLE_SCHEMA_CHANGED", detail=header_summary(headers)) from None
            found = []
            for row in data["rows"]:
                cells = row["cells"]
                if len(cells) > max(i_id, i_name, i_users, i_status) and cells[i_name] == name:
                    found.append({"id": cells[i_id] or row["key"], "users": cells[i_users], "status": cells[i_status]})
            return found

    def _segment_open(self, job: dict) -> bool:
        """Segmenti bulup düzenleme formunu açar; yoksa ve izin verildiyse yeni form açar. Yeni mi döner."""
        found = self._segment_list(job["name"])
        if len(found) > 1 or (not found and not job.get("create_if_missing")):
            raise PanelError("NAME_NOT_FOUND_OR_AMBIGUOUS", step="LIST_SEARCH", detail=f"{len(found)} eşleşme")
        name_box = self.page.locator("#segmentName")
        self.segment_id = found[0]["id"] if found else None
        self.segment_users_before = found[0]["users"] if found else None  # kaydetmeden önceki Users yazısı
        # Kurallar karşılaştırılacaksa, In değerleri için listedeki kural özeti (Show details) şimdi okunur.
        self.segment_list_texts = self._expand_condition_texts(found[0]["id"]) if found and job.get("rebuild") else []
        if found:
            if not re.fullmatch(r"[0-9]+", found[0]["id"] or ""):
                raise PanelError("TABLE_SCHEMA_CHANGED", step="SEGMENT_OPEN", detail="segment kimliği okunamadı")
            with self.step("SEGMENT_OPEN"):
                self._goto(f"{SEGMENT_SAVE}/{found[0]['id']}")  # satırdaki Edit düğmesinin açtığı adres
                name_box.wait_for(state="visible", timeout=self.timeout * 1000)
                self._wait_idle()
                if name_box.input_value().strip() != job["name"]:
                    raise PanelError("SEGMENT_FORM_MISMATCH")
            return False
        with self.step("SEGMENT_CREATE"):
            self._button("Create Segment").click()
            name_box.wait_for(state="visible", timeout=self.timeout * 1000)
            if not urlparse(self.page.url).path.startswith(SEGMENT_SAVE):
                raise PanelError("NAV_FAILED", detail="Create Segment formu açılmadı")
            self._wait_idle()
            name_box.fill(job["name"])
        self.log(f"   ⓘ '{job['name']}' bulunamadı; yeni segment oluşturuluyor")
        return True

    def _segment_clear(self, root):
        with self.step("SEGMENT_CLEAR"):
            wrappers = root.locator('[class*="condition-wrapper"]')
            for _ in range(40):
                if not wrappers.count():
                    break
                wrappers.last.locator("button:has(.anticon-delete)").last.click()
                confirm = self.page.locator(".ant-popconfirm:visible .ant-btn-primary")
                if confirm.count():  # yalnız kaydedilmemiş formdaki kural silme onayı
                    confirm.first.click()
                self.page.wait_for_timeout(300)
            groups = root.locator(GROUP)
            for _ in range(10):
                if not groups.count():
                    break
                groups.last.locator("button:has(.anticon-delete)").last.click()
                self.page.wait_for_timeout(300)
            if wrappers.count() or groups.count():
                raise PanelError("SEGMENT_CLEAR_FAILED")

    def _segment_dates(self, root, first: date, last: date):
        """Tüm 'between dates' kurallarının iki tarih kutusuna aynı aralık yazılır ve ağaçtan geri okunur."""
        interval = {"start": first.isoformat(), "end": last.isoformat()}
        with self.step("SEGMENT_DATES"):
            rows = root.locator('[class*="condition-wrapper"]').filter(
                has=self.page.locator("button").filter(has_text=re.compile(r"^\s*(between dates|date between)\s*$", re.I)))
            if not rows.count():
                raise PanelError("SEGMENT_NO_DATE_RULE")
            for index in range(rows.count()):
                self._enter_people_dates(root, interval, f"{index + 1}. tarihli kural", row=rows.nth(index))
            wanted = {first.strftime("%d/%m/%Y"), first.isoformat()}, {last.strftime("%d/%m/%Y"), last.isoformat()}
            dated = [n for n in self.page.evaluate(PEOPLE_TREE_JS) if n["tur"] == "kural"
                     and any(b.casefold() in ("between dates", "date between") for b in n["dugmeler"])]
            if len(dated) != rows.count() or any(len(n["tarihler"]) != 2 or n["tarihler"][0]["value"] not in wanted[0]
                                                 or n["tarihler"][1]["value"] not in wanted[1] for n in dated):
                raise PanelError("PEOPLE_DATE_MISMATCH", detail="segment tarih kutuları seçilen aralıkla aynı değil")
        return rows.count()

    def _segment_save(self):
        with self.step("SEGMENT_SAVE"):
            save = self.page.get_by_test_id("save-segment")
            if save.count() != 1:
                raise PanelError("SEGMENT_SAVE_UNVERIFIED", detail="Save düğmesi tek değil")
            save.click()
            deadline = time.monotonic() + self.timeout
            while urlparse(self.page.url).path.rstrip("/") != SCREENS["segment"]:
                problem = self.page.locator(".ant-modal:visible, .ant-message-error:visible, .ant-form-item-explain-error:visible")
                if problem.count() or time.monotonic() > deadline:
                    detail = mask(problem.first.inner_text()[:160]) if problem.count() else "liste sayfasına dönülmedi"
                    raise PanelError("SEGMENT_SAVE_UNVERIFIED", detail=detail)
                self.page.wait_for_timeout(250)
            self.verify_app()

    def _segment_wait(self, job: dict) -> tuple[int, str]:
        """Liste her poll_seconds saniyede yeniden açılır. Calculating görüldükten sonra Ready gelince sayı alınır.
        Prod'da (kullanıcı kaydı 2026-10-27) kayıttan sonra liste doğrudan Ready gösterebiliyor: Users sayısı
        kayıttan öncekinden farklıysa hemen alınır; aynıysa 10 sn arayla en çok 3 kez daha bakılıp yine alınır."""
        started = time.monotonic()
        deadline = started + job["max_wait_minutes"] * 60
        before = getattr(self, "segment_users_before", None)
        seen_waiting, ready_checks, check = False, 0, 0
        while True:
            check += 1
            rows = self._segment_list(job["name"])
            if len(rows) != 1:
                raise PanelError("NAME_NOT_FOUND_OR_AMBIGUOUS", step="SEGMENT_WAIT", detail=f"{len(rows)} eşleşme")
            status = rows[0]["status"].strip()
            minutes = (time.monotonic() - started) / 60
            if status.casefold().startswith(SEGMENT_WAITING):
                seen_waiting = True
                self.log(f"   … {status} ({check}. kontrol, {minutes:.0f} dk); {job['poll_seconds']} sn sonra yenilenecek")
            elif status.casefold().startswith(SEGMENT_DONE):
                users = rows[0]["users"]
                changed = before is None or users != before
                if seen_waiting or changed or ready_checks >= 3:
                    value = parse_count(users)
                    reason = "" if seen_waiting else " (Calculating görülmedi; sayı değişti)" if changed else " (Calculating görülmedi; sayı aynı kaldı)"
                    self.log(f"   ✓ {status} · Users {users}")
                    return value, f"segment {status}; ~{max(1, round(minutes))} dk{reason}"
                ready_checks += 1
                self.log(f"   … {status}, Users henüz kayıttan önceki gibi ({users}); 10 sn sonra tekrar bakılacak ({ready_checks}/3)")
            else:
                raise PanelError("SEGMENT_CALC_FAILED", step="SEGMENT_WAIT", detail=mask(status[:60]))
            wait = min(10, job["poll_seconds"]) if ready_checks and not seen_waiting else job["poll_seconds"]
            if time.monotonic() + wait > deadline:
                raise PanelError("SEGMENT_CALC_TIMEOUT", step="SEGMENT_WAIT", detail=f"{job['max_wait_minutes']} dk")
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if self.cancel is not None and self.cancel.is_set():
                    raise PanelError("CANCELLED", step="SEGMENT_WAIT")
                self.page.wait_for_timeout(1000)

    def _segment_run(self, job: dict, first: date, last: date) -> tuple[int, str]:
        # Panel açılmadan önce bütün kaynaklar çözülür; hata halinde eski filtreyle devam edilmez.
        dynamic = filter_source.dynamic(job)
        if dynamic:
            try:
                job = filter_source.resolve_job(job, first, last, getattr(self, 'profile_id', None))
            except filter_source.FilterSourceError as exc:
                raise PanelError('ATTRIBUTE_SOURCE', detail=str(exc)) from None
            for rule in filter_source.rules(job):
                for attribute in rule.get('attributes', []):
                    if attribute.get('value_source'):
                        self.log(f"   ✓ Tarihe bağlı filtre kaynağı okundu: {len(attribute['values'])} ayrı değer")
        self.log(f"   · Segment '{job['name']}' → {first:%d.%m.%Y}–{last:%d.%m.%Y}")
        created = self._segment_open(job)
        root = self.page.locator('[class*="targeting-condition-list"]').first
        root.wait_for(state="visible", timeout=self.timeout * 1000)
        if dynamic and not created:
            self._replace_dynamic_values(root, job)
            self._segment_dates(root, first, last)
        elif job.get("rebuild") and not created and self._rules_unchanged(root, job):
            # Kurallar zaten aynı: silip baştan kurmak yerine yalnız tarihler değişir (kullanıcı isteği 2026-10-28).
            self.log("   ✓ Segmentin kuralları iş tanımıyla aynı; yeniden kurulmadı, yalnız tarihler değişiyor")
            count = self._segment_dates(root, first, last)
            self.log(f"   ✓ {count} kuralda tarih yazıldı")
        elif job.get("rebuild"):
            if not created:
                self._segment_clear(root)
            items = segment_rules_with_dates(job, first, last)
            self._build_rules(root, items, job["connector"])
            with self.step("PEOPLE_VERIFY"):
                self._verify_people({"items": items, "connector": job["connector"]})
        else:
            count = self._segment_dates(root, first, last)
            self.log(f"   ✓ {count} kuralda tarih yazıldı")
        if self.cancel is not None and self.cancel.is_set():
            raise PanelError('CANCELLED')
        self._segment_save()
        value, note = self._segment_wait(job)
        return value, f"{first:%d.%m.%Y}–{last:%d.%m.%Y} · {note}"

    def _replace_dynamic_values(self, root, job):
        """× değerleri temizlerse doldurur; satırı kaldırırsa yalnız o attribute'u yeniden kurar."""
        from copy import deepcopy
        with self.step('SEGMENT_READ'):
            tree = self.page.evaluate(PEOPLE_TREE_JS)
            fill_attribute_values(tree, self._condition_texts() or getattr(self, 'segment_list_texts', []), prefer_summary=True)
        try:
            actual = segment_items_from_tree(tree)
            expected = deepcopy(job)
            wanted_rules, actual_rules = filter_source.rules(expected), filter_source.rules(actual)
            if len(wanted_rules) != len(actual_rules):
                raise ValueError()
            for wanted, current in zip(wanted_rules, actual_rules):
                for attribute in wanted.get('attributes', []):
                    if attribute.get('value_source'):
                        matches = [a for a in current.get('attributes', []) if a['name'] == attribute['name']]
                        if len(matches) != 1:
                            raise ValueError()
                        attribute['values'] = matches[0]['values']
            if not same_segment_rules(expected, actual):
                raise ValueError()
        except (ValueError, KeyError):
            raise PanelError('ATTRIBUTE_TARGET', detail='Kuralları segmentten yeniden alın; diğer koşullar iş tanımıyla aynı olmalı.') from None
        wrappers = root.locator('[class*="condition-wrapper"]')
        if wrappers.count() != len(wanted_rules):
            raise PanelError('ATTRIBUTE_TARGET')
        for index, rule in enumerate(filter_source.rules(job)):
            wrapper = wrappers.nth(index)
            for attribute in rule.get('attributes', []):
                if not attribute.get('value_source'):
                    continue
                with self.step('ATTRIBUTE_CLEAR'):
                    rows = wrapper.locator('.ant-space-horizontal').filter(has=self.page.locator(ATTRIBUTE_VALUE))
                    row = rows.filter(has=self.page.get_by_role('button', name=attribute['name'], exact=True))
                    if row.count() != 1 or row.locator('button:has(.anticon-close)').count() != 1:
                        raise PanelError('ATTRIBUTE_TARGET', detail='Seçili attribute’un değer temizleme × düğmesi tekil bulunamadı.')
                    before_count = rows.count()
                    # Ham form ağacıyla karşılaştır: eski liste özetini kullanmak, başka bir
                    # attribute'un yanlışlıkla temizlendiğini gizleyebilir. Tarihler de korunmalı.
                    def remainder(tree):
                        result = deepcopy(tree)
                        nodes = [n for n in result if n['tur'] == 'kural']
                        if len(nodes) != len(wanted_rules):
                            return None
                        nodes[index]['ozellikler'] = [a for a in nodes[index]['ozellikler'] if a['name'] != attribute['name']]
                        for node in result:
                            node.pop('dugmeler', None)  # Seçili attribute'un düğmeleri de değişir.
                        return result
                    before_tree = remainder(self.page.evaluate(PEOPLE_TREE_JS))
                    row.locator('button:has(.anticon-close)').click()
                    deadline = time.monotonic() + min(self.timeout, 6)
                    stable_mode, stable_since = None, time.monotonic()
                    while True:
                        if self.cancel is not None and self.cancel.is_set():
                            raise PanelError('CANCELLED')
                        mode = None
                        if wrappers.count() == len(wanted_rules) and remainder(self.page.evaluate(PEOPLE_TREE_JS)) == before_tree:
                            if row.count() == 0 and rows.count() == before_count - 1:
                                mode = 'removed'
                            elif row.count() == 1 and rows.count() == before_count:
                                boxes, buttons = row.locator(ATTRIBUTE_VALUE), row.locator('button:visible')
                                if (boxes.count() == 1 and not boxes.first.input_value().strip()
                                        and not row.locator('.ant-tag:visible').count() and buttons.count() >= 2
                                        and buttons.nth(0).inner_text().strip() == attribute['name']
                                        and buttons.nth(1).inner_text().strip() == attribute['operator']):
                                    mode = 'cleared'
                        if mode != stable_mode:
                            stable_mode, stable_since = mode, time.monotonic()
                        if mode and time.monotonic() - stable_since >= 0.4:
                            break
                        if time.monotonic() > deadline:
                            raise PanelError('ATTRIBUTE_TARGET', detail='× sonrası yalnız seçili attribute’un temizlendiği/kaldırıldığı ve diğer kuralların korunduğu doğrulanamadı.')
                        self.page.wait_for_timeout(100)
                    if mode == 'removed':
                        self.log('   · × attribute satırını kaldırdı; aynı ad ve operatörle yeniden ekleniyor')
                    else:
                        self.log('   ✓ Eski attribute değerleri temizlendi; ad ve operatör korundu')
                if mode == 'removed':
                    self._add_attribute(wrapper, attribute, 'Tarihe bağlı attribute')
                else:
                    with self.step('ATTRIBUTE_VALUES'):
                        self._fill_attribute_values(row, attribute, 'Tarihe bağlı attribute')
                if (rows.count() != before_count or row.count() != 1
                        or remainder(self.page.evaluate(PEOPLE_TREE_JS)) != before_tree):
                    raise PanelError('ATTRIBUTE_TARGET', detail='Attribute yenilenirken diğer koşullar veya tarihler değişti; kaydedilmedi.')
        if not self._rules_unchanged(root, job, preserved_static=actual):
            raise PanelError('ATTRIBUTE_TARGET')

    def _rules_unchanged(self, root, job: dict, *, preserved_static=None) -> bool:
        """Formdaki kuralları okur ve işle karşılaştırır. Okunamazsa veya farklıysa False (yeniden kurulur)."""
        with self.step("SEGMENT_READ"):
            self._wait_idle()
            tree = self.page.evaluate(PEOPLE_TREE_JS)
            texts = self._condition_texts()
            if not filter_source.dynamic(job):
                texts = texts or getattr(self, 'segment_list_texts', [])
            fill_attribute_values(tree, texts, prefer_summary=True)
            if preserved_static is not None:
                # _replace_dynamic_values her adımda bu alanların ham formda değişmediğini
                # denetledi. Bazı sabit In değerleri yalnız listedeki eski özette görünür.
                for node, rule, before in zip((n for n in tree if n['tur'] == 'kural'),
                                              filter_source.rules(job), filter_source.rules(preserved_static)):
                    names = {a['name'] for a in rule.get('attributes', []) if not a.get('value_source')}
                    for attr in node.get('ozellikler', []):
                        matches = [a for a in before.get('attributes', []) if a['name'] == attr['name']]
                        if attr['name'] in names and len(matches) == 1:
                            attr['values'] = matches[0]['values']
        try:
            same = same_segment_rules(job, segment_items_from_tree(tree))
        except ValueError:
            return False
        if not same:
            self.log("   ⓘ Segmentin kuralları iş tanımından farklı; silinip yeniden kurulacak")
        return same

    def segment_rules(self, name: str) -> dict:
        """Segmentin kurallarını formdan okur (Edit ekranı açılır, hiçbir şey değiştirilmez/kaydedilmez)."""
        self._segment_open({"name": name})
        root = self.page.locator('[class*="targeting-condition-list"]').first
        with self.step("SEGMENT_READ"):
            root.wait_for(state="visible", timeout=self.timeout * 1000)
            self._wait_idle()
            self.page.wait_for_timeout(500)
            tree = self.page.evaluate(PEOPLE_TREE_JS)
        if any(a.get("name") for n in tree for a in n.get("ozellikler", [])):
            # In değerleri kutuda görünmeyebilir; Netmera'nın kural özetinde yazar. Önce formda, yoksa listedeki ayrıntıda aranır.
            with self.step("SEGMENT_READ"):
                texts = self._condition_texts() or self._condition_texts_from_list(name)
            fill_attribute_values(tree, texts, prefer_summary=True)
        try:
            return segment_items_from_tree(tree)
        except ValueError as exc:
            raise PanelError("SEGMENT_RULES_UNREADABLE", step="SEGMENT_READ", detail=str(exc)[:300]) from None

    def _condition_texts(self) -> list[str]:
        return [t.strip() for t in self.page.locator('[class*="condition-text"]:visible').all_inner_texts() if t.strip()]

    def _condition_texts_from_list(self, name: str) -> list[str]:
        """Segment listesinde satırın 'Show details' (+) ayrıntısındaki kural özetleri. Yalnız okunur."""
        found = self._segment_list(name)
        return self._expand_condition_texts(found[0]["id"]) if len(found) == 1 else []

    def _expand_condition_texts(self, segment_id: str) -> list[str]:
        """Açık segment listesinde bu satırın ayrıntısını açar ve kural özetlerini okur; açılamazsa boş."""
        row = self.page.locator(f'tr[data-row-key="{segment_id}"]')
        expand = row.locator('button[aria-label="Show details"]')
        if expand.count() != 1:
            return []
        expand.click()
        try:
            self.page.locator('[class*="condition-text"]:visible').first.wait_for(timeout=self.timeout * 1000)
        except Exception:
            return []
        self._wait_idle()
        return self._condition_texts()

    def _verify_people(self, job: dict):
        """Panelde kurulan kural ağacını okur ve iş tanımıyla karşılaştırır (yalnız adlar)."""
        built = self.page.evaluate(PEOPLE_TREE_JS)
        expected_rules, groups = [], []
        for item in job["items"]:
            if item["kind"] == "group":
                groups.append(item["connector"])
                expected_rules += [(1, r) for r in item["rules"]]
            else:
                expected_rules.append((0, item))
        got_rules = [n for n in built if n["tur"] == "kural"]
        problems = []
        if len(got_rules) != len(expected_rules):
            problems.append(f"{len(expected_rules)} kural beklendi, panelde {len(got_rules)}")
        for got, (depth, rule) in zip(got_rules, expected_rules):
            buttons = got.get("dugmeler", [])
            if got["derinlik"] != depth or got.get("alan") != rule["field"] or rule["value"] not in buttons                     or (rule.get("operator") and rule["operator"] not in buttons):
                problems.append(f"kural '{rule['field']} {rule.get('operator', '=')} {rule['value']}' farklı kuruldu")
            if any(step not in buttons for step in rule.get("steps", [])):
                problems.append(f"'{rule['field']}' ek seçenekleri aynı değil")
            if rule.get("numbers") and got.get("sayilar", [])[:len(rule["numbers"])] != [str(n) for n in rule["numbers"]]:
                problems.append(f"'{rule['field']}' kuralının sayı kutuları aynı değil")
            if rule.get("date_range"):
                actual = got.get("tarihler", [])
                expected = [date.fromisoformat(rule["date_range"][key]) for key in ("start", "end")]
                if len(actual) != 2 or any(item["value"] != (value.isoformat() if item["type"] == "date"
                                                           else value.strftime("%d/%m/%Y"))
                                          for item, value in zip(actual, expected)):
                    problems.append(f"'{rule['field']}' kuralının tarih kutuları aynı değil")
        if sum(n["tur"] == "grup" for n in built) != len(groups):
            problems.append(f"{len(groups)} grup beklendi")
        group_index, top_connectors = -1, 0
        for node in built:
            if node["tur"] == "grup":
                group_index += 1
            elif node["tur"] == "baglac":
                want = job["connector"] if node["derinlik"] == 0 else groups[group_index] if 0 <= group_index < len(groups) else None
                top_connectors += node["derinlik"] == 0
                if node["deger"] != want:
                    problems.append(f"bağlaç {want} yerine {node['deger']}")
        if len(job["items"]) > 1 and not top_connectors:
            problems.append("üst düzey bağlaç görülmedi")
        if problems:
            raise PanelError("PEOPLE_STRUCTURE_MISMATCH", detail="; ".join(dict.fromkeys(problems))[:300])

    def _stable_text(self, locator) -> str:
        """Yazı 1 sn boyunca değişmeden kalmalı (geç gelen güncellemeyi yakalamak için)."""
        deadline, previous = time.monotonic() + 8, None
        while True:
            current = locator.inner_text().strip()
            if current == previous:
                return current
            if time.monotonic() > deadline:
                raise PanelError("COUNT_REFRESH_UNVERIFIED", detail="sayı durulmadı")
            previous = current
            self.page.wait_for_timeout(1000)

    def _people_match_counter(self, root) -> int:
        with self.step("PEOPLE_COUNT"):
            counter = root.get_by_role("listitem").filter(has=self.page.get_by_text("people match", exact=True)).locator("span.ant-tag")
            if counter.count() != 1:
                raise PanelError("COUNT_MISSING_OR_AMBIGUOUS")
            self.page.wait_for_timeout(1500)
            value = self._stable_text(counter)
            self.verify_app()
            return parse_count(value)

    def _named_count(self, job: dict):
        with self.step("NAV"):
            self._goto(SCREENS[job["type"]])
            table = self.page.locator("table:visible").filter(has=self.page.get_by_role("columnheader", name="Name", exact=True))
            table.first.wait_for(state="visible", timeout=self.timeout * 1000)
            self._wait_idle()
        with self.step("LIST_SEARCH"):
            # Segments: 'Search by name'. Tags: sayfanın kendi 'Search' kutusu (üst çubuktaki genel aramadan sonra gelir).
            search = self.page.get_by_placeholder("Search by name", exact=True)
            if not search.count():
                search = self.page.get_by_placeholder("Search", exact=True)
            search.last.fill(job["name"])
            search.last.press("Enter")
            self.page.wait_for_timeout(800)
            self._wait_idle()
        self.verify_app()
        with self.step("RESULT_READ"):
            headers = table.first.locator("thead tr").last.evaluate("e => [...e.querySelectorAll('th')].map(c => c.innerText.trim())")
            rows = table.first.locator("tbody tr").evaluate_all("es => es.map(e => [...e.querySelectorAll('td')].map(c => c.innerText.trim()))")
            return extract_named_count(headers, [r for r in rows if any(r)], job["name"])

    def event_catalog(self) -> list[str]:
        """Event arama listesindeki adlar (yalnız ad; sayı/kişi verisi okunmaz). DENEYSEL."""
        with self.step("NAV"):
            self._fresh_events()
        names: list[str] = []
        with self.step("CATALOG"):
            self.page.get_by_role("combobox").first.click()
            options = self.page.locator(".ant-select-dropdown:visible .ant-select-item-option")
            options.first.wait_for(state="visible", timeout=self.timeout * 1000)
            holder = self.page.locator(".ant-select-dropdown:visible .rc-virtual-list-holder")
            unchanged = 0
            for _ in range(400):
                before = len(names)
                for text in options.all_inner_texts():
                    text = text.strip()
                    if text and text not in names:
                        names.append(text)
                unchanged = unchanged + 1 if len(names) == before else 0
                if unchanged >= 3 or not holder.count():
                    break
                holder.first.evaluate("h => { h.scrollTop += Math.max(h.clientHeight - 40, 60); }")
                self.page.wait_for_timeout(150)
            self.page.keyboard.press("Escape")
        return sorted(names, key=str.casefold)
