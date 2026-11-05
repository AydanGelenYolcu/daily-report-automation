"""Ortak sonuç → hücre planı. İlk hedef: mevcut yerel .xlsx dosyası."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from . import jobs as J, dateparse
from .excel import ExcelLockedError, LEDGER_MARKER, LEDGER_SHEET, MARKERS, _excel_lock, _day
from .paths import check_local_path


def normalize_mappings(raw, jobs):
    if not isinstance(raw, list) or len(raw) > 128:
        raise J.JobError('Hücre eşlemeleri en çok 128 satır olmalı.')
    lookup = {j['id']:j for j in jobs}
    mappings = []
    BASES = ('data_day','run_day','previous_day')
    def col(value):
        if not isinstance(value,str) or not re.fullmatch('[A-Za-z]{1,3}',value):
            raise J.JobError('Sütun A, B gibi yazılmalı.')
        value=value.upper()
        if column_index_from_string(value)>16384:
            raise J.JobError('Excel sütunu XFD sınırını aşıyor.')
        return value
    for m in raw:
        allowed={'job_id','column','sheet','mode','cell','date_column','value_column','date_basis','base_date','base_cell','row_step','column_step',
                 'header_row','value_row','month_cell','month_first'}
        if not isinstance(m,dict) or set(m)-allowed:
            raise J.JobError('Hücre eşleme alanları geçersiz.')
        job=lookup.get(m.get('job_id'))
        if job is None or m.get('column') not in (job.get('columns') or [None]):
            raise J.JobError('Hücre eşlemesindeki iş/sütun bulunamadı. İşi silmeden veya sütunlarını değiştirmeden eşlemesini düzenleyin.')
        # Aynı sonuç birden çok hedefe yazılabilir (ör. iki sayfadaki aynı metrik); hücre çakışması ayrıca denetlenir.
        sheet=J._text(m.get('sheet'),'Hedef sayfa',max_len=31)
        if re.search(r'[\\/*?:\[\]]',sheet) or sheet.startswith("'") or sheet.endswith("'") or sheet.casefold()==LEDGER_SHEET.casefold():
            raise J.JobError('Hedef sayfa adı geçersiz veya ayrılmış.')
        out={'job_id':job['id'],'column':m.get('column'),'sheet':sheet,'mode':m.get('mode')}
        if out['mode']=='fixed_cell':
            cell=m.get('cell','')
            matched=re.fullmatch(r'([A-Za-z]{1,3})([1-9][0-9]{0,6})',cell) if isinstance(cell,str) else None
            if not matched or int(matched[2])>1048576:
                raise J.JobError('Hedef hücre B4 gibi yazılmalı.')
            out['cell']=col(matched[1])+matched[2]
        elif out['mode']=='daily_shift':
            base=m.get('base_cell','')
            match=re.fullmatch(r'([A-Za-z]{1,3})([1-9][0-9]{0,6})',base) if isinstance(base,str) else None
            if not match or int(match[2])>1048576:raise J.JobError('Başlangıç hücresi B4 gibi olmalı.')
            base_date=m.get('base_date','')
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',base_date):raise ValueError()
                date.fromisoformat(base_date)
            except (ValueError,TypeError):raise J.JobError('Başlangıç tarihini seçin.') from None
            rs,cs=m.get('row_step'),m.get('column_step')
            if type(rs) is not int or type(cs) is not int or rs not in (0,1) or cs not in (0,1) or not rs+cs:
                raise J.JobError('Kayma aşağı, sağa veya ikisi birlikte olmalı.')
            basis=m.get('date_basis','data_day')
            if basis not in BASES:raise J.JobError('Kayma tarih türü geçersiz.')
            out.update(base_cell=col(match[1])+match[2],base_date=base_date,row_step=rs,column_step=cs,date_basis=basis)
        elif out['mode']=='date_row':
            out.update(date_column=col(m.get('date_column')),value_column=col(m.get('value_column')),
                       date_basis=m.get('date_basis','data_day'))
            if out['date_column']==out['value_column'] or out['date_basis'] not in BASES:
                raise J.JobError('Tarih/sonuç sütunları farklı ve tarih seçimi geçerli olmalı.')
            if m.get('month_first'):out['month_first']=True
        elif out['mode']=='header_lookup':
            hr,vr=m.get('header_row'),m.get('value_row')
            if type(hr) is not int or type(vr) is not int or not 1<=hr<=1048576 or not 1<=vr<=1048576:
                raise J.JobError('Başlık satırı ve yazılacak satır 1, 2, 3 gibi satır numarası olmalı.')
            if hr==vr:raise J.JobError('Başlık satırı ile yazılacak satır farklı olmalı.')
            basis=m.get('date_basis','data_day')
            if basis not in BASES:raise J.JobError('Tarih seçimi geçersiz.')
            month=m.get('month_cell') or None
            if month is not None:
                matched=re.fullmatch(r'([A-Za-z]{1,3})([1-9][0-9]{0,6})',month) if isinstance(month,str) else None
                if not matched or int(matched[2])>1048576:raise J.JobError('Ay adı kontrol hücresi A82 gibi yazılmalı veya boş bırakılmalı.')
                month=col(matched[1])+matched[2]
            out.update(header_row=hr,value_row=vr,date_basis=basis,month_cell=month)
            if m.get('month_first'):out['month_first']=True
        else:
            raise J.JobError('Hücre yerleştirme biçimi geçersiz.')
        mappings.append(out)
    for i,a in enumerate(mappings):
        for b in mappings[i+1:]:
            if a['sheet'].casefold()!=b['sheet'].casefold():continue
            if a==b:raise J.JobError('Aynı eşleme iki kez eklenmiş.')
            if a['mode']==b['mode']=='header_lookup' and (a['value_row']==b['header_row'] or b['value_row']==a['header_row']):
                raise J.JobError('Yazılacak satır başka eşlemenin başlık satırı olamaz.')
            if a['mode']==b['mode']=='header_lookup' and a['value_row']==b['value_row'] and a['header_row']==b['header_row']:
                raise J.JobError('İki sonuç aynı başlık ve satıra yazamaz.')
            if a['mode']==b['mode']=='fixed_cell' and a['cell']==b['cell']:
                raise J.JobError('İki sonuç aynı hücreye yazamaz.')
            if a['mode']==b['mode']=='date_row' and a['value_column']==b['value_column']:
                raise J.JobError('İki tarihli sonuç aynı hedef sütununu paylaşamaz.')
            if {a['mode'],b['mode']}=={'fixed_cell','date_row'}:
                fixed,dated=(a,b) if a['mode']=='fixed_cell' else (b,a)
                if re.match('[A-Z]+',fixed['cell'])[0] in (dated['date_column'],dated['value_column']):
                    raise J.JobError('Sabit hücre, tarihli eşlemenin tarih/sonuç sütunuyla çakışıyor.')
            if a['mode']==b['mode']=='date_row' and (a['value_column']==b['date_column'] or b['value_column']==a['date_column']):
                raise J.JobError('Sonuç sütunu başka eşlemenin tarih sütununu değiştiremez.')
    return mappings


def catch_up_notes(plan):
    """Kaçan gün yazımında atlanan/hatalı hedefler için kısa notlar."""
    return [f"Kaçan gün · {i['job']}: {i['message']}" for i in plan
            if i['status']=='error' or (i['status']=='skipped' and i.get('cell'))]


def unsuccessful_mapping_note(profile, rows):
    """Başarısız/ilgisiz eski sonuçlar yeni, başarılı yazımları bloke etmesin."""
    jobs = {j['id']: j for j in profile['jobs']}
    keys = {(m['job_id'], m['column']) for m in profile.get('excel_mappings', []) if jobs[m['job_id']]['enabled']}
    matched = [r for r in rows if (r.get('job_id'), r.get('column')) in keys]
    if any(r.get('status') == 'OK' for r in matched):
        return None  # Sayı, kaynak, imza, hedef ve koruma kontrolleri yine _plan içinde yapılır.
    if matched:
        codes = sorted({r.get('status') if re.fullmatch('[A-Z_]+', str(r.get('status', ''))) else 'UNKNOWN' for r in matched})
        return 'Eşlenen işe ait başarılı sonuç bulunmayan yazım kaydı atlandı (' + ', '.join(codes) + ').'
    return 'Eşlenen etkin işe ait sonuç içermeyen yazım kaydı atlandı.'


def mapping_signature(profile):
    jobs={j['id']:j for j in profile['jobs']}
    payload=[{**m,'query':J.signature(jobs[m['job_id']]),'enabled':jobs[m['job_id']]['enabled']}
             for m in profile.get('excel_mappings',[])]
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _target_day(row,basis):
    if row.get('catch_up'):basis='data_day'  # kaçan gün daima kendi veri gününe yazılır
    if basis=='run_day':return _day(row['planned_at'])
    # Anlık sayılar (People/Tag/Segment) sabah çekilip bir önceki günün sütununa yazılır.
    if basis=='previous_day':return _day(row['planned_at'])-timedelta(days=1)
    if row['job_type'] not in J.DATED_TYPES and not row.get('period_start'):return _day(row['captured_at'])
    first,end=_day(row['period_start']),_day(row['period_end'])
    if first is None or end is None or end-first!=timedelta(days=1):
        raise ValueError('Verinin günü için tek günlük event dönemi gerekir; çok günlük toplamda çalışma gününü seçin.')
    return first


def shifted_cell(mapping,row):
    day=_target_day(row,mapping['date_basis'])
    days=(day-date.fromisoformat(mapping['base_date'])).days
    if days<0:raise ValueError('Sonuç günü başlangıç tarihinden önce; geriye kaydırılıp yazılmaz.')
    match=re.fullmatch(r'([A-Z]+)(\d+)',mapping['base_cell'])
    r=int(match[2])+days*mapping['row_step']
    c=column_index_from_string(match[1])+days*mapping['column_step']
    if r>1048576 or c>16384:raise ValueError('Günlük kayma hedef sınırını aşıyor.')
    return get_column_letter(c)+str(r)


TR_MONTHS=['ocak','şubat','mart','nisan','mayıs','haziran','temmuz','ağustos','eylül','ekim','kasım','aralık']


def header_column(read,max_col,mapping,day):
    """Başlık satırında hedef günün sütunu. read(satır, sütun) hesaplanmış değeri verir."""
    if max_col>16384:raise ValueError('Başlık araması en çok 16.384 sütunu destekler.')
    cells=[(c,read(mapping['header_row'],c)) for c in range(1,max_col+1)]
    hits,overflow=dateparse.find(cells,day,mapping.get('month_first',False),f'Başlık satırı {mapping["header_row"]}')
    # 30 günlük ayın 31. sütunu gibi taşan tek günlere yazılmaz: ay, başlıkta çoğunlukla bulunmalı.
    if overflow:
        raise ValueError(f'{day:%d.%m.%Y} başlıkta yalnız taşan gün olarak var; o ayın başlık tarihini güncelleyin.')
    if len(hits)!=1:
        raise ValueError(f'{day:%d.%m.%Y} başlık satırı {mapping["header_row"]} içinde tek bir sütunda bulunamadı; '
                         'ayın başlık tarihini güncelleyin veya tekrarları düzeltin.')
    if mapping.get('month_cell'):
        match=re.fullmatch(r'([A-Z]+)(\d+)',mapping['month_cell'])
        label=str(read(int(match[2]),column_index_from_string(match[1])) or '')
        wanted=TR_MONTHS[day.month-1]
        if wanted not in label.replace('İ','i').replace('I','ı').lower():
            raise ValueError(f'Ay kontrol hücresi {mapping["month_cell"]} “{wanted.title()}” değil; ay satırlarını kaydırıp adını güncelleyin.')
    return hits[0]


def _values(path,profile):
    """Başlık araması varsa formüllerin Excel'in kaydettiği sonuçlarını da okur."""
    if not any(m['mode']=='header_lookup' for m in profile.get('excel_mappings',[])):return None
    try:return load_workbook(path,data_only=True)
    except PermissionError:raise ExcelLockedError('Excel dosyası açık veya kilitli.') from None
    except Exception:raise ValueError('Mevcut Excel okunamadı; dosya değiştirilmedi.') from None


def _ledger(book,source):
    if book.properties.identifier in MARKERS.values() and book.properties.identifier!=MARKERS[source]:
        raise ValueError('Demo ve prod sonuçları aynı Excel dosyasında karışamaz.')
    ledger=next((s for s in book if s.title.casefold()==LEDGER_SHEET.casefold()),None)
    if ledger is not None:
        if ledger['A1'].value!=LEDGER_MARKER:
            raise ValueError('Uygulama kayıt sayfası adı başka içerik tarafından kullanılıyor.')
        if any(r[1]!=source for r in ledger.iter_rows(min_row=3,max_col=2,values_only=True) if r[0]):
            raise ValueError('Demo ve prod sonuçları aynı Excel dosyasında karışamaz.')
    return ledger


def _open(path,source):
    path=check_local_path(Path(path))
    if path.suffix.lower()!='.xlsx' or not path.is_file():
        raise ValueError('Hücre eşleme için mevcut bir .xlsx dosyası seçin; dosya/sayfa kendiliğinden oluşturulmaz.')
    try:book=load_workbook(path,data_only=False,keep_links=True)
    except PermissionError:raise ExcelLockedError('Excel dosyası açık veya kilitli.') from None
    except Exception:raise ValueError('Mevcut Excel okunamadı; dosya değiştirilmedi.') from None
    try:_ledger(book,source)
    except Exception:book.close();raise
    return book


def _plan(book,profile,rows,values=None):
    """values: formüllerin kayıtlı sonuçlarını taşıyan kitap (yerel Excel); yoksa book'un kendi değerleri."""
    jobs={j['id']:j for j in profile['jobs']}
    plan=[]
    for m in profile.get('excel_mappings',[]):
        job=jobs[m['job_id']]
        item={'job':J.column_label(job,m['column']),'sheet':m['sheet'],'cell':None,'value':None,'status':'skipped','message':''}
        candidates=[r for r in rows if r.get('job_id')==m['job_id'] and r.get('column')==m['column']]
        try:
            if not job['enabled']:
                item['message']='İş kapalı; mevcut hücre korunur.'
            elif not candidates:
                item['message']='Son çalışmada bu sonuç yok; mevcut hücre korunur.'
            elif len(candidates)!=1:
                raise ValueError('Sonuç tekil değil; yeni bir test çalıştırın.')
            else:
                row=candidates[0]
                if row.get('source')!=profile['source'] or row.get('signature')!=J.signature(job):
                    raise ValueError('Kaynak veya sorgu değişmiş; yeni bir test çalıştırın.')
                if row.get('status')!='OK':
                    item['message']='Sorgu hatalı; mevcut hücre korunur.'
                elif row.get('catch_up') and (m['mode']=='fixed_cell' or m.get('date_basis')=='run_day'):
                    item['message']='Kaçan gün sabit hücreye veya çalışma gününe göre yazılmaz; atlandı.'
                else:
                    value=row.get('value')
                    if row.get('job_type') in ('formula','ga4'):  # hesaplama ve GA4 (oran, süre) ondalıklı olabilir
                        if type(value) not in (int,float) or not math.isfinite(value) or abs(value)>9007199254740991:
                            raise ValueError('Sonuç geçerli bir sayı değil.')
                    elif type(value) is not int or not 0<=value<=9007199254740991:
                        raise ValueError('Sonuç geçerli bir sayım değil.')
                    sheet=next((s for s in book if s.title.casefold()==m['sheet'].casefold()),None)
                    if sheet is None or sheet.protection.sheet:
                        raise ValueError('Hedef sayfa bulunamadı veya korumalı.')
                    if m['mode']=='fixed_cell':cell=m['cell']
                    elif m['mode']=='daily_shift':cell=shifted_cell(m,row)
                    elif m['mode']=='header_lookup':
                        shown=values[sheet.title] if values is not None else sheet
                        def read(r,c,shown=shown,sheet=sheet):
                            value=shown.cell(r,c).value
                            formula=sheet.cell(r,c).value
                            if value is None and isinstance(formula,str) and formula.startswith('='):
                                raise ValueError('Başlık formüllerinin hesaplanmış değeri dosyada yok; dosyayı Excel’de açıp kaydedin, sonra yeniden deneyin.')
                            return value
                        column=header_column(read,sheet.max_column,m,_target_day(row,m['date_basis']))
                        cell=get_column_letter(column)+str(m['value_row'])
                    else:
                        if sheet.max_row>100000:
                            raise ValueError('Tarih araması en çok 100.000 satırlı sayfayı destekler.')
                        day=_target_day(row,m['date_basis'])
                        date_col=column_index_from_string(m['date_column'])
                        found,_=dateparse.find([(i,sheet.cell(i,date_col).value) for i in range(1,sheet.max_row+1)],day,
                                               m.get('month_first',False),f'Tarih sütunu {m["date_column"]}')
                        if len(found)!=1:raise ValueError('Hedef tarih tek bir satırda bulunamadı; tarih ekleyin veya tekrarları düzeltin.')
                        cell=m['value_column']+str(found[0])
                    target=sheet[cell]
                    if row.get('catch_up') and target.value not in (None,''):
                        item.update(sheet=sheet.title,cell=cell,status='skipped',
                                    message=f'{_target_day(row,m["date_basis"]):%d.%m.%Y}: hücre dolu; kaçan gün yazılmadı (elle girilmiş olabilir).')
                        plan.append(item);continue
                    if any(cell in area for area in sheet.merged_cells.ranges):
                        raise ValueError('Hedef hücre birleştirilmiş; dosya değiştirilmedi.')
                    if target.data_type=='f':
                        raise ValueError('Hedef hücre formül içeriyor; formülün üzerine yazılmaz.')
                    item.update(sheet=sheet.title,cell=cell,value=row['value'],status='ready',message='Yazılabilir')
        except (ValueError,KeyError,TypeError) as exc:
            item.update(status='error',message=str(exc) if isinstance(exc,ValueError) else 'Sonuç tarihi/yapısı okunamadı.')
        plan.append(item)
    destinations=[(i['sheet'].casefold(),i['cell']) for i in plan if i['status']=='ready']
    if len(destinations)!=len(set(destinations)):
        raise ValueError('Çözülen hedef hücreler çakışıyor; eşlemeleri düzeltin.')
    for item in (i for i in plan if i['status']=='ready'):
        for m in profile.get('excel_mappings',[]):
            if m['mode']=='date_row' and item['sheet'].casefold()==m['sheet'].casefold() and re.match('[A-Z]+',item['cell'])[0]==m['date_column']:
                raise ValueError('Hedef hücre bir tarih arama sütununa yazıyor; eşlemeyi düzeltin.')
            if m['mode']=='header_lookup' and item['sheet'].casefold()==m['sheet'].casefold() and int(re.search(r'\d+',item['cell'])[0])==m['header_row']:
                raise ValueError('Hedef hücre bir başlık satırına yazıyor; eşlemeyi düzeltin.')
    return plan


def _preview_plan(book, profile, rows, day_rows=None, values=None):
    """Ana sonuçlar ve her günlük grubu, yazıcıdaki gibi ayrı ayrı eşler; hiçbir hücreyi değiştirmez."""
    groups = {}
    for row in day_rows or []:
        groups.setdefault((row.get('period_start'), row.get('period_end')), []).append(row)
    batches = ([(rows, False)] if rows else []) + [(group, True) for group in groups.values()]
    result, seen = [], set()
    mappings = profile.get('excel_mappings', [])
    for batch, daily in batches:
        plan = _plan(book, profile, batch, values)
        keys = {(r.get('job_id'), r.get('column')) for r in batch}
        for index, item in enumerate(plan):
            mapping = mappings[index]
            if (mapping['job_id'], mapping['column']) not in keys:
                continue
            if daily:
                start = batch[0].get('period_start')
                item['period'] = start[:10] if isinstance(start, str) else None
            result.append(item)
            seen.add(index)
    if len(seen) < len(mappings):
        missing = _plan(book, profile, [], values)
        result.extend(item for index, item in enumerate(missing) if index not in seen)
    targets = [(i['sheet'].casefold(), i['cell']) for i in result if i['status'] == 'ready']
    if len(targets) != len(set(targets)):
        raise ValueError('Çözülen hedef hücreler çakışıyor; eşlemeleri düzeltin.')
    return result


def preview(profile,rows,*,day_rows=None):
    profile=J.normalize_profile(profile)
    if profile['excel_mode']!='mapped_cells' or not profile['excel_mappings']:
        raise ValueError('Önce Belirli hücrelere yaz modunu ve en az bir eşlemeyi seçin.')
    path=check_local_path(Path(profile['output_file']))
    with _excel_lock(path):
        book=_open(path,profile['source']);values=_values(path,profile)
        try:plan=_preview_plan(book,profile,rows,day_rows,values)
        finally:
            book.close()
            if values is not None:values.close()
    return {'items':plan,'ready':sum(i['status']=='ready' for i in plan),'errors':sum(i['status']=='error' for i in plan),
            'written':False,'source':profile['source']}


def write_mapped_workbook(profile,batches,notes=None):
    profile=J.normalize_profile(profile)
    path=check_local_path(Path(profile['output_file']))
    if not batches:return 0
    with _excel_lock(path):
        before=(path.stat().st_mtime_ns,path.stat().st_size) if path.exists() else None
        book=_open(path,profile['source']);values=_values(path,profile)
        temporary=backup_temp=None
        try:
            ledger=_ledger(book,profile['source'])
            seen={r[0] for r in ledger.iter_rows(min_row=3,max_col=1,values_only=True) if r[0]} if ledger is not None else set()
            fingerprint=mapping_signature(profile)
            pending=[];written=0
            for batch in sorted(batches,key=lambda b:b['planned_at']):
                key='m:'+batch['id']+':'+fingerprint
                skipped_key='s:'+batch['id']+':'+fingerprint
                if key in seen or skipped_key in seen:continue
                if batch['source']!=profile['source'] or batch.get('mapping_signature')!=fingerprint:
                    raise ValueError('Bekleyen hücre yazımının kaynağı veya eşlemesi değişmiş; dosya değiştirilmedi.')
                note=unsuccessful_mapping_note(profile,batch['rows'])
                if note is not None:
                    if notes is not None:notes.append(note)
                    pending.append(skipped_key);seen.add(skipped_key)
                    continue
                plan=_plan(book,profile,batch['rows'],values)
                errors=[i for i in plan if i['status']=='error']
                ready=[i for i in plan if i['status']=='ready']
                if batch.get('catch_up'):
                    # Kaçan günün sorunu (ör. ay başlığı değişmiş) bugünkü yazımı durdurmaz.
                    if notes is not None:notes.extend(catch_up_notes(plan))
                else:
                    if errors:raise ValueError(errors[0]['job']+': '+errors[0]['message'])
                    if not ready:raise ValueError('Eşlenen işler için başarılı sonuç yok; dosya değiştirilmedi.')
                for i in ready:book[i['sheet']][i['cell']]=i['value']
                if ready:written+=1
                pending.append(key);seen.add(key)
            if not pending:return 0
            if ledger is None:
                ledger=book.create_sheet(LEDGER_SHEET);ledger['A1']=LEDGER_MARKER
                ledger.append(['Çalışma kimliği','Kaynak'])
            for key in pending:ledger.append([key,profile['source']])
            ledger.sheet_state='veryHidden'
            with tempfile.NamedTemporaryFile(prefix='.netmera-hucre-',suffix='.xlsx',dir=path.parent,delete=False) as h:
                temporary=Path(h.name)
            book.save(temporary)
            if (path.stat().st_mtime_ns,path.stat().st_size)!=before:
                raise ExcelLockedError('Excel yazım sırasında değişti; tekrar deneyin.')
            backup=path.with_name(path.stem+'.netmera-yedek.xlsx')
            with tempfile.NamedTemporaryFile(prefix='.netmera-yedek-',suffix='.xlsx',dir=path.parent,delete=False) as h:
                backup_temp=Path(h.name)
            shutil.copy2(path,backup_temp);os.replace(backup_temp,backup)
            try:os.replace(temporary,path)
            except PermissionError:raise ExcelLockedError('Excel dosyası açık veya kilitli.') from None
            return written
        finally:
            book.close()
            if values is not None:values.close()
            for temp in (temporary,backup_temp):
                if temp is not None:temp.unlink(missing_ok=True)
