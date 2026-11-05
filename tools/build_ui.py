"""Birleşik arayüzü üretir: netmera_automation/ui/src/{netmera,ga4}.html → netmera_automation/ui/index.html

Kabuk (kenar çubuğu, Ctrl+K komut paleti, açık/koyu tema, çekmece, tasarım belirteçleri) Analytics Veri Çekme'den,
Netmera sayfaları (Projeler, İşler, Çalıştır, Zamanlama, Ayarlar, Google Sheets kurulumu, Yardım) Netmera'dan gelir;
Google hesabı ortak bağlantı sayfasında yönetilir. Netmera stilleri `.nm` kapsamına, renk değişkenleri `--nm-*` adıyla tasarım
belirteçlerine bağlanır (koyu tema Netmera sayfalarında da çalışır). İki kod tek <script> içinde ayrı kapsamlardadır;
`window.NM` ve `window.GA` köprüleriyle konuşur. Her dönüşüm dayanağını birebir bulamazsa betik durur.

Kullanım: python tools/build_ui.py   (src dosyaları değişince yeniden çalıştırılır; testler index.html'i denetler)
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "netmera_automation" / "ui"


def between(text: str, start: str, end: str) -> str:
    i = text.index(start) + len(start)
    return text[i:text.index(end, i)]


def one(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"Dayanak {text.count(old)} kez bulundu (1 bekleniyordu): {old[:100]!r}")
    return text.replace(old, new)


def cut(text: str, start: str, end: str, new: str) -> str:
    """start'tan (dahil) end'e (hariç) kadar olan bölümü new ile değiştirir."""
    if text.count(start) != 1:
        raise SystemExit(f"Başlangıç dayanağı tek değil: {start[:80]!r}")
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + new + text[j:]


# ---------------------------------------------------------------------------------------------------------------
# Netmera: değişken adları, stil kapsamı, gövde, kod
# ---------------------------------------------------------------------------------------------------------------
NM_VARS = ["accent-ink", "ok-bg", "warn-bg", "err-bg", "demo-bg", "code-ink", "bg", "panel", "ink", "muted", "line", "soft",
           "accent", "ok", "warn", "err", "demo", "code", "radius", "shadow"]
NM_VAR = re.compile(r"--(" + "|".join(map(re.escape, NM_VARS)) + r")(?![\w-])")
NM_TOKENS = """:root {
  --nm-bg: var(--bg); --nm-panel: var(--surface); --nm-ink: var(--text); --nm-muted: var(--text-2); --nm-line: var(--line);
  --nm-soft: var(--surface-2); --nm-accent: var(--btn-primary); --nm-accent-ink: var(--on-accent);
  --nm-ok: var(--ok-text); --nm-ok-bg: var(--ok-soft); --nm-warn: var(--warn-text); --nm-warn-bg: var(--warn-soft);
  --nm-err: var(--err-text); --nm-err-bg: var(--err-soft); --nm-demo: var(--mor-text); --nm-demo-bg: var(--mor-soft);
  --nm-code: oklch(0.2 0.012 262); --nm-code-ink: oklch(0.9 0.01 262); --nm-radius: var(--r-lg); --nm-shadow: var(--shadow-card);
}
[hidden], .hidden { display: none !important; }
.nm .nm-ust { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
.nm .nm-ust #projTitle { font-size: 22px; font-weight: 600; letter-spacing: -0.011em; }
.nm .nm-ust .grow { flex: 1; }
.nm code { overflow-wrap: anywhere; }  /* uzun yollar dar ekranda taşmasın (eşaralıklı yazı tipi daha geniş) */
/* Dar ekran (telefon/küçük pencere): kenar çubuğu üstte yatay şerit olur, içerik tam genişlik (Netmera formları sığsın) */
@media (max-width: 760px) {
  .uygulama { grid-template-columns: minmax(0, 1fr); }
  .kenar { position: sticky; top: 0; z-index: 10; height: auto; min-width: 0; max-width: 100vw; flex-direction: row; align-items: center; gap: 6px; padding: 8px 10px;
    overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line); }
  .marka { padding: 0 2px 0 0; flex: none; } .proje-sec { flex: none; } .proje-sec .nav-baslik { display: none; } .proje-sec select { max-width: 140px; }
  .nav { flex-direction: row; gap: 2px; flex: none; } .nav-oge { padding: 0 8px; } .kenar-alt { margin: 0 0 0 auto; flex-direction: row; padding: 0; flex: none; }
}
"""
DROP = re.compile(r"^(?::root|\*|html|body|header|main|nav\.tabs)\b")


def _block_end(css: str, i: int) -> int:
    depth = 0
    for k in range(i, len(css)):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    raise SystemExit("CSS bloğu kapanmıyor")


def scope_css(css: str) -> str:
    out, i = [], 0
    while True:
        while i < len(css) and css[i] in " \t\r\n":
            i += 1
        if i >= len(css):
            break
        if css.startswith("/*", i):
            i = css.index("*/", i) + 2
            continue
        brace = css.index("{", i)
        head = css[i:brace].strip()
        end = _block_end(css, brace)
        body = css[brace + 1:end]
        i = end + 1
        if head.startswith("@media"):
            inner = scope_css(body)
            if inner.strip():
                out.append(f"{head}{{{inner}}}")
        elif head.startswith("@"):
            out.append(f"{head}{{{body}}}")
        else:
            sels = [s.strip() for s in head.split(",")]
            kept = [".nm " + s for s in sels if not DROP.match(s)]
            if kept:
                out.append(f"{','.join(kept)}{{{body}}}")
    return "\n".join(out)


def netmera(src: str) -> tuple[str, str, str, str]:
    css = scope_css(NM_VAR.sub(r"--nm-\1", between(src, "<style>", "</style>")))
    body = NM_VAR.sub(r"--nm-\1", between(src, "<body>", "<script>"))
    js = NM_VAR.sub(r"--nm-\1", between(src, "<script>", "</script>"))

    header = between(body, "<header>", "</header>")
    body = one(body, f"<header>{header}</header>", "")
    body = cut(body, '<nav class="tabs" role="tablist">', "</nav>", "")
    body = one(body, "</nav>\n", "\n")
    ust = ('<div class="nm-ust"><button class="btn small hidden" id="backBtn" type="button">← Projeler</button>'
           '<b id="projTitle" class="hidden"></b><span id="sourcePill" class="pill hidden"></span><span class="grow"></span>'
           '<span id="statusPill" class="pill">Hazır</span>'
           '<button class="btn small" id="quitBtn" type="button" title="Arayüzü kapat (zamanlanmış görevler çalışmaya devam eder)">Kapat</button></div>')
    main = between(body, "<main>", "</main>")
    rest = body[body.index("</main>") + len("</main>"):]
    main = one(main, '<div class="projects" id="projectCards"></div>', '<div id="ga4ImportNote" class="banner warn hidden" role="status"></div><div class="projects" id="projectCards"></div>')
    main = one(main, '<div class="row"><button class="btn primary" id="addJobBtn">+ Yeni iş</button></div>',
               '<div class="row"><button class="btn" id="ga4LibraryBtn" type="button" title="GA4 hazır rapor kütüphanesinden grup grup iş ekle">'
               'GA4 hazır raporları</button><button class="btn primary" id="addJobBtn">+ Yeni iş</button></div>')
    main = one(main, '<tbody id="resBody"></tbody></table></div>\n    </div>',
               '<tbody id="resBody"></tbody></table></div>\n    </div>\n'
               '    <div class="card hidden" id="ga4RunCard"><div class="row" style="justify-content:space-between">'
               '<div><h2>GA4 tabloları</h2><div class="hint">Bu çalışmadaki GA4 işlerinin tam tabloları (kırılım satırları, GA4 toplamı, uyarılar). '
               'Excel, CSV veya yeni bir Google tablosuna dışa aktarılabilir.</div></div>'
               '<a class="btn primary" href="#/ga4/calistir">Tabloları gör ve dışa aktar</a></div></div>')
    nm_main = f'<div id="nm" class="nm" hidden><div class="sayfa nm-sayfa">{ust}<div id="navFeedback" role="status" class="banner" hidden></div>{main}</div></div>'
    nm_rest = f'<div class="nm">{rest}</div>'

    js = one(js, "const TYPE_HINT = {", "const TYPE_HINT = {ga4:'GA4 metrikleri: kırılım, zaman, filtre (Analytics)', ")
    js = one(js, "const TYPE_NAME = {", "const TYPE_NAME = {ga4:'GA4', ")
    js = one(js, "  S = state; requestedProfile = S.profile.id;", "  S = state; requestedProfile = S.profile.id;\n  window.GA?.projeBildir(S.profile.id, S.profile.source);")
    js = one(js, """function showView(){
  const proj = view === 'project';""", """function showView(){
  const proj = view === 'project';
  // Kenar çubuğu: proje listesine dönülünce adres de değişir (GA4 sayfasındayken dokunulmaz)
  if (!proj && Object.values(NM_ROUTE).includes(location.hash) && location.hash !== '#/projeler') location.hash = '#/projeler';""")
    js = one(js, "function switchTab(name){\n", """const NM_ROUTE = {projects:'#/projeler', jobs:'#/isler', run:'#/calistir', schedule:'#/zamanlama', settings:'#/ayarlar', 'google-guide':'#/google', help:'#/yardim'};
// Sekme değişimi adresi değiştirir; kabuğun yönlendiricisi showTab'ı çağırır (kenar çubuğu doğru öğeyi işaretler).
function switchTab(name){
  const route = NM_ROUTE[name];
  if (route && location.hash !== route) { location.hash = route; return; }
  view = 'project'; showView(); showTab(name);
}
function showTab(name){
""")
    js = one(js, "  if (b.dataset.edit) return openJob(+b.dataset.edit);",
             "  if (b.dataset.edit) { const j = p.jobs[+b.dataset.edit]; return j.type === 'ga4' ? GA.isDuzenleAd(j.label) : openJob(+b.dataset.edit); }")
    js = one(js, "  if (b.dataset.dup) {", "  if (b.dataset.dup && p.jobs[+b.dataset.dup].type === 'ga4') return GA.isKopyala(p.jobs[+b.dataset.dup].label);  // Sheets sekmesi kopyalanmaz\n  if (b.dataset.dup) {")
    js = one(js, "<div class=\"meta\"><span>${p.source === 'demo' ? 'Sahte veri' : esc(p.app || 'Netmera uygulaması girilmedi')}</span>",
             "<div class=\"meta\"><span>${p.source === 'demo' ? 'Sahte veri' : esc(p.app || (p.ga4_only ? 'Yalnız GA4 (Netmera paneli kullanılmıyor)' : 'Netmera uygulaması girilmedi'))}</span>${p.ga4 ? `<span>${p.ga4} GA4 işi</span>` : ''}")
    js = one(js, "function renderProjects(){\n", """function renderProjects(){
  // Analytics Veri Çekme'den aktarım bir kez bildirilir (eski Windows görevi uyarısı siyah pencerede kalmasın)
  const imp = S.ga4 && S.ga4.import, note = $('#ga4ImportNote');
  let seen = null; try { seen = localStorage.getItem('ga4.import.seen'); } catch(e) {}
  const showNote = !!(imp && imp.projeler && imp.projeler.length && seen !== imp.zaman);
  note.classList.toggle('hidden', !showNote);
  if (showNote) note.innerHTML = `<b>Analytics Veri Çekme'den aktarıldı:</b> ${imp.projeler.map(x => esc(x.ad) + ' (' + x.is + ' GA4 işi)').join(', ')}.
    Eski uygulamaya ve dosyalarına dokunulmadı. Eski uygulamada Windows görevi kuruluysa, buradaki sonuçları doğruladıktan sonra o görevi kaldır
    (aksi hâlde aynı Sheets sekmesine iki uygulama yazar); zamanlamayı bu uygulamada projenin Zamanlama sayfasından kur.
    <button class="btn small" type="button" data-import-ok="${esc(imp.zaman)}">Anladım</button>`;
""")
    js = one(js, "  if (b.dataset.del) { const i = +b.dataset.del;",
             "  if (b.dataset.del && p.jobs[+b.dataset.del].type === 'ga4') return GA.isSil(p.jobs[+b.dataset.del].label);  // paketten silineni hatırlar\n"
             "  if (b.dataset.del) { const i = +b.dataset.del;")
    js = one(js, "$('#typeBtns').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; readEditor();",
             "$('#typeBtns').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return;\n"
             "  if (b.dataset.type === 'ga4') { $('#jobDlg').close(); return GA.isDuzenle(null); }  // GA4 işi Analytics çekmecesinde\n  readEditor();")
    js = one(js, "function renderLastRun(run){\n", """function renderLastRun(run){
  const ga = !!(run && ((run.ga4 && run.ga4.length) || run.ga4_count));
  $('#ga4RunCard').classList.toggle('hidden', !ga);
  if (ga && window.GA) GA.netmeraSonucu(run);
""")
    js = one(js, "guard(async () => { await load(savedProfile(), 'projects'); poll(); });", """$('#ga4LibraryBtn').onclick = () => GA.hazirRaporlar();
$('#ga4ImportNote').addEventListener('click', e => { const b = e.target.closest('[data-import-ok]'); if (!b) return;
  try { localStorage.setItem('ga4.import.seen', b.dataset.importOk); } catch(err) {} $('#ga4ImportNote').classList.add('hidden'); });
const ready = (async () => { try { await load(savedProfile(), 'projects'); } catch(e) { toast(e.message, true); } poll(); })();
// Kabukla köprü: kenar çubuğu/komut paleti Netmera sayfalarını bunlarla açar.
Object.defineProperty(window, 'S', { configurable: true, get: () => S });  // salt okunur: hata ayıklama ve arayüz testleri
window.NM = {
  ready,
  async show(name){ await ready; if (!S) return; if (name === 'projects') { view = 'projects'; showView(); return; } view = 'project'; rememberProfile(S.profile.id); showView(); showTab(name); if (name === 'settings') await refreshGoogleState(); },
  async select(id){ await ready; if (!S) return false; if (requestedProfile === id && S.profile.id === id) return true; return load(id); },
  refresh(){ return S && requestedProfile === S.profile.id && !pendingToggles.size && !$('#jobDlg').open && activeTab !== 'settings' ? load(S.profile.id) : null; },
  lastGa4(id){ return api('/api/ga4-last-run?profile=' + encodeURIComponent(id)); },
  async exportGoogleClient(){ await ready; return api('/api/google/team/save', {profile_id: S.profile.id}); },
  async refreshGoogle(){ await ready; await refreshGoogleState(); },
  state: () => S,
  newJob(){ return guard(async () => { await ready; openJob(null); }); },
  editJob(i){ return openJob(i); },
  newProject(){ $('#newProfileBtn').click(); },
};""")
    return css, nm_main, nm_rest, js


# ---------------------------------------------------------------------------------------------------------------
# Analytics (GA4): kabuk + GA4 sayfaları
# ---------------------------------------------------------------------------------------------------------------
GA_HASH = {"'#/isler'": "'#/ga4/isler'", "'#/calistir'": "'#/ga4/calistir'", "'#/zamanlama'": "'#/ga4/sheets'",
           "'#/baglanti'": "'#/ga4/baglanti'", "'#/yardim'": "'#/ga4/yardim'", "'#/genel'": "'#/ga4/isler'"}

ROUTER = """const NM_SAYFA = { projeler: 'projects', isler: 'jobs', calistir: 'run', zamanlama: 'schedule', ayarlar: 'settings', google: 'google-guide', yardim: 'help' };
const SAYFALAR = {
  projeler: ['Projeler', 'klasor'], isler: ['İşler', 'isler'], calistir: ['Çalıştır', 'calistir'], zamanlama: ['Zamanlama', 'saat'],
  ayarlar: ['Ayarlar', 'anahtar'], google: ['Google kurulum rehberi', 'tablo'], yardim: ['Yardım ve kılavuz', 'yardim'],
  'ga4/genel': ['GA4 genel bakış', 'genel', sayfaGenel], 'ga4/isler': ['GA4 işleri', 'grafik', sayfaIsler], 'ga4/calistir': ['GA4 sonuçları', 'calistir', sayfaCalistir],
  'ga4/sheets': ['GA4 Sheets', 'tablo', sayfaZamanlama], 'ga4/baglanti': ['Google bağlantısı', 'baglanti', sayfaBaglanti],
  'ga4/yardim': ['GA4 yardım', 'yardim', sayfaYardim],
};
function rotaAdi() { const p = location.hash.replace(/^#\\//, '').split('?')[0].split('/'); return p[0] === 'ga4' ? `ga4/${p[1] || 'isler'}` : (p[0] || 'projeler'); }
const gaRotada = () => rotaAdi().startsWith('ga4/');
function navCiz(aktifAd) {
  const oge = ad => { const [baslik, simge] = SAYFALAR[ad];
    return h('a', { class: 'nav-oge', href: `#/${ad}`, 'data-tab': NM_SAYFA[ad] && ad !== 'projeler' ? NM_SAYFA[ad] : null,
      'aria-current': ad === aktifAd ? 'page' : null }, ikon(simge, 16), h('span', { text: baslik })); };
  $('#nav').replaceChildren(oge('projeler'), h('div', { class: 'nav-baslik', text: 'Bu proje' }), oge('isler'), oge('calistir'), oge('zamanlama'), oge('ayarlar'),
    h('div', { class: 'nav-baslik', text: 'Bağlantı' }), oge('ga4/baglanti'),
    h('div', { class: 'nav-baslik', text: 'Google Analytics (GA4)' }), oge('ga4/genel'), oge('ga4/isler'), oge('ga4/calistir'), oge('ga4/sheets'),
    h('div', { class: 'nav-baslik', text: 'Yardım' }), oge('google'), oge('yardim'), oge('ga4/yardim'));
}
function yonlendir() {
  let ad = rotaAdi();
  if (!SAYFALAR[ad]) ad = 'projeler';
  if (ad !== 'ga4/baglanti') D.googleRouteActive = false;
  navCiz(ad);
  D.calistirKisayol = null;
  document.title = `${SAYFALAR[ad][0]} · Netmera Otomasyonu`;
  if (NM_SAYFA[ad]) {  // Netmera sayfası: kendi kodu çizer
    $('#ga-sayfa').hidden = true; $('#nm').hidden = false;
    if (window.NM) NM.show(NM_SAYFA[ad]).catch(e => bildir(e.message, 'hata'));
    return;
  }
  $('#nm').hidden = true; $('#ga-sayfa').hidden = false;
  if (ad === 'ga4/calistir' && D.pendingNetmeraRun && D.pendingNetmeraRun.proje === D.proje) {
    const pending = D.pendingNetmeraRun;
    D.pendingNetmeraRun = null;
    $('#ga-sayfa').replaceChildren(h('div', {class:'sayfa', role:'status', text:'Son GA4 tabloları yükleniyor…'}));
    fetchNetmeraResult(pending);
    return;
  }
  if (ad === 'ga4/baglanti' && !D.googleRouteActive) {
    D.googleRouteActive = true;
    $('#ga-sayfa').replaceChildren(h('div', { class: 'sayfa', role: 'status', text: 'Google bağlantısı yükleniyor…' }));
    api('GET', '/api/durum').then(r => {
      if (rotaAdi() !== 'ga4/baglanti') return;
      D.durum = r; hapGuncelle(); yonlendir();
    }).catch(e => {
      if (rotaAdi() === 'ga4/baglanti') $('#ga-sayfa').replaceChildren(h('div', { class: 'sayfa', role: 'alert', text: e.message }));
    });
    return;
  }
  const eski = D.durum && D.durum.surum !== ARAYUZ_SURUMU ? h('div', { class: 'sayfa', style: 'padding-bottom:0' },
    h('div', { class: 'uyari-kutu uyari', role: 'alert' }, ikon('uyari'), h('div', {}, h('b', { text: 'Uygulama güncellendi ama eski sürüm çalışıyor. ' }),
      `Siyah pencereyi kapatıp NetmeraOtomasyonu.cmd ile yeniden aç (çalışan: v${D.durum.surum}, yeni: v${ARAYUZ_SURUMU}).`))) : null;
  try { $('#ga-sayfa').replaceChildren(...(eski ? [eski] : []), SAYFALAR[ad][2]()); }
  catch (e) { $('#ga-sayfa').replaceChildren(h('div', { class: 'sayfa' }, h('div', { class: 'uyari-kutu hata' }, ikon('hata'), h('div', { text: `Sayfa çizilemedi: ${e.message}` })))); }
}

"""

PALET = """  const git = r => () => { location.hash = r; };
  const N = window.NM && NM.state();
  const komutlar = [
    { ad: 'Projeler', ikon: 'klasor', eylem: git('#/projeler') },
    { ad: 'Yeni proje', ikon: 'arti', eylem: () => NM.newProject() },
    { ad: 'İşler', ikon: 'isler', eylem: git('#/isler') },
    { ad: 'Yeni iş', ikon: 'arti', kisayol: 'N', eylem: () => { location.hash = '#/isler'; NM.newJob(); } },
    { ad: 'Yeni GA4 işi', ikon: 'grafik', eylem: () => isDuzenle(null) },
    { ad: 'GA4 hazır raporları çek', ikon: 'grafik', eylem: () => hazirRaporlarAc() },
    { ad: 'Çalıştır', ikon: 'calistir', eylem: git('#/calistir') },
    { ad: 'Zamanlama', ikon: 'saat', eylem: git('#/zamanlama') },
    { ad: 'Ayarlar', ikon: 'anahtar', eylem: git('#/ayarlar') },
    { ad: 'GA4 genel bakış', ikon: 'genel', eylem: git('#/ga4/genel') },
    { ad: 'GA4 işleri', ikon: 'grafik', eylem: git('#/ga4/isler') },
    { ad: 'GA4 sonuçları ve dışa aktarma', ikon: 'calistir', eylem: git('#/ga4/calistir') },
    { ad: 'GA4 Sheets sekmeleri', ikon: 'tablo', eylem: git('#/ga4/sheets') },
    { ad: 'Google bağlantısı (GA4 + Sheets, tek ayar)', ikon: 'baglanti', eylem: git('#/ga4/baglanti') },
    { ad: 'GA4 taslak dışa aktar', ikon: 'indir', eylem: () => taslakIndir() },
    { ad: 'Google Sheets kurulumu', ikon: 'tablo', eylem: git('#/google') },
    { ad: 'Yardım ve kılavuz', ikon: 'yardim', eylem: git('#/yardim') },
    { ad: 'GA4 yardım', ikon: 'yardim', eylem: git('#/ga4/yardim') },
    { ad: 'Temayı değiştir', ikon: 'ay', eylem: temaDegistir },
    ...((N && N.projects) || []).filter(p => p.id !== D.proje).map(p => ({ ad: `Projeye geç: ${p.name}`, ikon: 'klasor', eylem: () => projeDegistir(p.id) })),
    ...((N && N.profile.jobs) || []).map((j, i) => j.type === 'ga4' ? null : { ad: `İşi düzenle: ${j.label}`, ikon: 'kalem', eylem: () => { location.hash = '#/isler'; NM.editJob(i); } }).filter(Boolean),
    ...D.isler.map(j => ({ ad: `GA4 işini düzenle: ${j.ad}`, ikon: 'kalem', eylem: () => isDuzenle(j) })),
  ];
"""

BRIDGE = """
// Netmera kodu ile köprü: GA4 işleri Netmera İşler listesinden de açılır; proje seçimi iki tarafta aynı tutulur.
window.GA = {
  async jobsChanged(){ await durumYenile(); await islerYenile(); if (gaRotada()) yonlendir(); },
  isDuzenle: j => isDuzenle(j),
  async isKopyala(ad) { await islerYenile(); const j = D.isler.find(x => x.ad === ad); if (j) isDuzenle(j, true); },
  async isDuzenleAd(ad) { await islerYenile(); const j = D.isler.find(x => x.ad === ad); if (j) isDuzenle(j); else bildir('GA4 işi bulunamadı; sayfayı yenile.', 'hata'); },
  async isSil(ad) {
    await islerYenile();
    const j = D.isler.find(x => x.ad === ad);
    if (!j) return;
    if (!await onayla({ baslik: 'GA4 işi silinsin mi?', metin: `"${ad}" bu projeden silinir.` + (j.paket ? ' Paketten silinen rapor kendiliğinden geri gelmez; istersen "Hazır raporları çek" ile yeniden alırsın.' : ''), eylem: 'Sil', tehlike: true })) return;
    try { await api('POST', '/api/isler/sil', { ad }); await islerYenile(); bildir('İş silindi.', 'ok'); } catch (e) { bildir(e.message, 'hata'); }
  },
  hazirRaporlar: () => hazirRaporlarAc(),
  async projeBildir(id, kaynak) {
    D.netmeraKaynak = kaynak;
    if (id === D.proje && D.durum) { projeSeciciCiz(); return; }
    // Proje seçimini tek merkez yürütür. GA4 yüklemesi Netmera gezinmesini bekletmez.
    try { const loading = projeDegistir(id, null, true); projeSeciciCiz(); await loading; } catch (e) { bildir(e.message, 'hata'); }
  },
  netmeraSonucu(run) {
    if (run.ga4_count && !run.ga4.length) {
      if (D.netmeraSonuc?.calisma !== run.run_id) D.pendingNetmeraRun = {proje:run.profile_id, id:run.run_id};
      return;
    }
    const say = d => run.ga4.filter(t => t.durum === d).length;
    D.netmeraSonuc = { calisma: run.run_id, zaman: run.planned_at, kaynak: D.netmeraKaynak === 'demo' ? 'sentetik' : 'ga4', sonuclar: run.ga4,
                       tamam: say('tamam'), uyari: say('uyari'), hata: say('hata'), sure_ms: (run.seconds || 0) * 1000, proje: run.profile_id, netmera: true };
  },
};
async function fetchNetmeraResult(pending) {
  try {
    const r = await window.NM.lastGa4(pending.proje);
    if (pending.proje !== D.proje || rotaAdi() !== 'ga4/calistir') return;
    D.netmeraSonuc = r.result; D.sonuc = r.result; yonlendir();
  } catch(e) { if (pending.proje === D.proje) { bildir(e.message, 'hata'); yonlendir(); } }
}
"""


def ga4(src: str) -> tuple[str, str, str]:
    css = between(src, "<style>", "</style>")
    body = between(src, "<body>", "<script>")
    js = between(src, "<script>", "</script>")
    for old, new in GA_HASH.items():
        js = js.replace(old, new)
        js = js.replace(old.replace("'", '"'), new.replace("'", '"'))
    body = one(body, 'href="#/baglanti"', 'href="#/ga4/baglanti"')
    body = one(body, '<div class="marka-ad">Analytics Veri Çekme</div><div class="marka-surum">GA4 · v__SURUM__</div>',
               '<div class="marka-ad">Netmera Otomasyonu</div><div class="marka-surum">Netmera + GA4 · v__VERSION__</div>')

    js = one(js, "  let r;\n  const controller", "  yol = yol.replace(/^\\/api\\//, '/api/ga4/');\n  let r;\n  const controller")
    js = js.replace("AnalyticsVeriCekme.cmd", "NetmeraOtomasyonu.cmd")
    js = one(js, "  if (!r.ok) throw new Error((veri && veri.error) || `İstek başarısız (${r.status})`);\n  return veri;",
             "  if (!r.ok) throw new Error((veri && veri.error) || `İstek başarısız (${r.status})`);\n"
             "  if (yontem === 'POST' && /^\\/api\\/ga4\\/(isler|paketler|projeler\\/guncelle)/.test(yol) && window.NM) NM.refresh();  // Netmera İşler listesi tazelensin\n"
             "  if (yontem === 'POST' && /^\\/api\\/ga4\\/google\\/(istemci|baglan|cikis)$/.test(yol) && window.NM) await NM.refreshGoogle();\n"
             "  return veri;")
    js = js.replace("localStorage.getItem('proje')", "localStorage.getItem('netmera.profile')").replace("localStorage.setItem('proje',", "localStorage.setItem('netmera.profile',")
    js = one(js, "async function projeDegistir(id, hedef) {\n", """async function projeDegistir(id, hedef, nmden = false) {
  if (!nmden && window.NM) {
    try { const changed = await NM.select(id); if (changed && hedef) location.hash = hedef; }
    catch(e) { bildir(e.message, 'hata'); projeSeciciCiz(); }
    return;
  }
""")
    js = one(js, "async function yeniProje() {\n", "async function yeniProje() {\n  if (window.NM) return NM.newProject();  // proje Netmera Projeler'inde açılır\n")
    js = one(js, "  const liste = (D.durum && D.durum.projeler) || [];",
             "  const liste = window.NM?.state()?.projects.map(p => ({id:p.id, ad:p.name})) || (D.durum && D.durum.projeler) || [];")
    js = one(js, "if (location.hash !== '#/ga4/isler') location.hash = '#/ga4/isler'; else yonlendir();", "if (gaRotada()) yonlendir();  // Netmera İşler'den açıldıysa orada kalınır")
    js = one(js, "if ((location.hash || '#/ga4/isler').startsWith('#/ga4/isler')) yonlendir(); else location.hash = '#/ga4/isler';", "if (gaRotada()) yonlendir();")
    js = one(js, "    kap.replaceChildren(sheetsAyarKarti(z), isler, zaman);",
             "    kap.replaceChildren(sheetsAyarKarti(z), isler, h('section', { class: 'kart' },\n"
             "      h('div', { class: 'kart-ust' }, h('div', { class: 'kart-baslik' }, ikon('saat', 15), 'Otomatik çalışma')),\n"
             "      h('div', { class: 'kart-ic' }, h('p', { class: 'ipucu' }, 'GA4 Sheets sekmeleri projenin Windows göreviyle yazılır: ', h('a', { href: '#/zamanlama', text: 'Zamanlama' }),\n"
             "        ' sayfasından saat ve gün seç. Çalıştır → \"Çalıştır ve yaz\" da işaretli GA4 işlerini sekmelerine yazar; buradaki \"Şimdi yaz\" yalnız GA4 sekmelerini yazar.'))));")
    js = one(js, "sayfaUst('Sheets ve zamanlama', `${proje ? proje.ad + ' projesi · ' : ''}Google tablosu, Sheets'e yazılan işler ve otomatik çalışma saati.`)",
             "sayfaUst('GA4 Sheets sekmeleri', `${proje ? proje.ad + ' projesi · ' : ''}GA4 işlerinin kendi sekmelerine yazıldığı Google tablosu ve yazılacak işler.`)")
    js = one(js, "          h('div', { class: 'etiket', text: 'Kaynak' }), kaynakSeg.el,\n          kurulu ? null : h('p', { class: 'ipucu' }, 'GA4 için önce ', h('a', { href: '#/ga4/baglanti', text: 'bağlantı kur' }), '.')),",
             "          h('div', { class: 'etiket', text: 'Kaynak' }), h('p', { class: 'ipucu', text: D.netmeraKaynak === 'demo'\n"
             "            ? 'Demo projesi: sentetik kaynak (sayılar uydurmadır).' : 'GA4 (bu projede). Demo projelerinde sentetik kaynak kullanılır.' }),\n"
             "          kurulu || D.netmeraKaynak === 'demo' ? null : h('p', { class: 'ipucu' }, 'GA4 için önce ', h('a', { href: '#/ga4/baglanti', text: 'bağlantı kur' }), '.')),")
    js = one(js, "  sonuclariCiz();\n  D.calistirKisayol = calistir;",
             "  if (!D.sonuc && D.netmeraSonuc && D.netmeraSonuc.proje === D.proje) D.sonuc = D.netmeraSonuc;  // Netmera çalışmasındaki GA4 tabloları\n"
             "  sonuclariCiz();\n  D.calistirKisayol = calistir;")
    # GA4 sayfa başlıkları Netmera sayfalarıyla karışmasın
    for old, new in (("sayfaUst('İşler', `", "sayfaUst('GA4 işleri', `"), ("sayfaUst('Çalıştır', `", "sayfaUst('GA4 sonuçları ve dışa aktarma', `"),
                     (r"sayfaUst('Bağlantı', 'GA4\'e yalnız okuma izniyle bağlan. Önerilen: kendi Google hesabın; Google Sheets çıktısı da aynı girişle yazılacak.'",
                      "sayfaUst('Google bağlantısı (GA4 + Sheets)', 'Tek Google ayarı: buradaki OAuth JSON dosyası ve Google hesabı GA4 okuma, GA4 Sheets sekmeleri ve bütün projelerin Google Sheets hücre yazımı için ortaktır. En son seçilen JSON geçerlidir.'"), ("sayfaUst('Yardım', 'Kısa", "sayfaUst('GA4 yardım', 'Kısa"),
                     ("let sinif = 'uyari', metin = 'Sentetik mod';", "let sinif = 'uyari', metin = 'GA4 bağlı değil';")):
        js = one(js, old, new)
    js = one(js, "text: 'Arayüzün kaydettiği işler komut satırından da çalışır:' }",
             "text: 'Projenin bütün işleri (Netmera + GA4) komut satırından da çalışır; --test çıktıya yazmaz:' }")
    js = one(js, "text: `python -m analytics_cekme dene \"${D.durum.is_dosyasi}\"` }", "text: `python -m netmera_automation run --profile ${D.proje} --test` }")
    js = cut(js, "const SAYFALAR = {", "document.addEventListener('keydown', e => {", ROUTER)
    js = cut(js, "  const komutlar = [", "  let aktif = 0;", PALET)
    js = one(js, "e.key.toLowerCase() === 'n') { e.preventDefault(); isDuzenle(null); }",
             "e.key.toLowerCase() === 'n') { e.preventDefault(); gaRotada() ? isDuzenle(null) : NM.newJob(); }")
    basla = js[js.index("(async function basla() {"):]
    boot = basla.replace("$('#icerik').replaceChildren(", "$('#ga-sayfa').replaceChildren(").replace("navCiz('genel');", "navCiz('projeler');")
    boot = one(boot, "    await Promise.all([durumYenile(), islerYenile()]);",
               "    await NM.ready;\n    if (!NM.state()) throw new Error('Proje listesi yüklenemedi; sayfayı yenileyin.');")
    js = js.replace(basla, boot)
    js = js.rstrip() + "\n" + BRIDGE
    return css, body, js


def build() -> str:
    nm_css, nm_main, nm_rest, nm_js = netmera((UI / "src" / "netmera.html").read_text(encoding="utf-8"))
    ga_css, ga_body, ga_js = ga4((UI / "src" / "ga4.html").read_text(encoding="utf-8"))
    ga_body = one(ga_body, '<main class="icerik" id="icerik" tabindex="-1"></main>',
                  f'<main class="icerik" id="icerik" tabindex="-1"><div id="ga-sayfa"></div>{nm_main}</main>')
    favicon = between((UI / "src" / "ga4.html").read_text(encoding="utf-8"), '<link rel="icon" href="', '">')
    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="token" content="__TOKEN__">
<title>Netmera Otomasyonu</title>
<link rel="icon" href="{favicon}">
<!-- ÜRETİLMİŞ DOSYA: düzenleme ui/src/netmera.html ve ui/src/ga4.html üzerinde yapılır, sonra: python tools/build_ui.py -->
<style>
{ga_css}
/* ---------- Netmera sayfaları (kapsam: .nm; renkler tasarım belirteçlerinden) ---------- */
{NM_TOKENS}
{nm_css}
</style>
</head>
<body>
{ga_body}
{nm_rest}
<script>
(() => {{
{nm_js}
}})();
(() => {{
{ga_js}
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    out = UI / "index.html"
    out.write_text(build(), encoding="utf-8")
    print(f"yazıldı: {out} ({out.stat().st_size // 1024} KB)")
    sys.exit(0)
