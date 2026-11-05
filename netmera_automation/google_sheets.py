"""Belirlenen Google Sheet'e yalnız eşlenen sayım hücrelerini yazar."""
from datetime import date,timedelta
import hashlib
import json
import math
from urllib.parse import quote,urlencode

from openpyxl import Workbook
from openpyxl.utils import get_column_letter,column_index_from_string

from . import jobs as J,store,google_auth as auth
from .cell_mapping import _plan,_preview_plan,mapping_signature,unsuccessful_mapping_note


def _range(sheet,cell):
    return "'"+sheet.replace("'","''")+"'!"+cell


def _base(profile):
    if profile['source']!='netmera':raise auth.GoogleError('Demo sayıları Google Sheets’e gönderilmez. Demo için yerel Excel kullanın.')
    if profile.get('output_target')!='google_sheets' or not profile.get('sheets_id') or not profile.get('excel_mappings'):
        raise auth.GoogleError('Önce Google Sheets hedefini ve hücre eşlemelerini tamamlayın.')
    return 'https://sheets.googleapis.com/v4/spreadsheets/'+quote(profile['sheets_id'],safe='')


def _protected(sheet,item):
    from openpyxl.utils.cell import coordinate_from_string
    col,row=coordinate_from_string(item['cell']);col=column_index_from_string(col)-1;row-=1
    for protected in sheet.get('protectedRanges',[]):
        area=protected.get('range')
        if not isinstance(area,dict):return True
        if (area.get('startRowIndex',0)<=row<area.get('endRowIndex',10**9)
                and area.get('startColumnIndex',0)<=col<area.get('endColumnIndex',10**9)):return True
    return False


def plan(profile,rows,*,day_rows=None):
    profile=J.normalize_profile(profile);base=_base(profile)
    token=auth.access_token(profile['id'])
    fields='spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),merges,protectedRanges(range,namedRangeId))'
    metadata=auth.request_json(base+'?'+urlencode({'fields':fields}),token=token)
    if metadata.get('spreadsheetId')!=profile['sheets_id'] or not isinstance(metadata.get('sheets'),list):
        raise auth.GoogleError('Google hedef dosya kimliği doğrulanamadı.')
    book=Workbook();book.remove(book.active)
    selected={}
    try:
        for mapping in profile['excel_mappings']:
            wanted=mapping['sheet'].casefold()
            matches=[s for s in metadata['sheets'] if s.get('properties',{}).get('title','').casefold()==wanted]
            if len(matches)!=1:raise auth.GoogleError('Hedef Google Sheets sayfası tekil olarak bulunamadı; adı kontrol edin.')
            entry=matches[0];props=entry['properties']
            if type(props.get('sheetId')) is not int or not isinstance(props.get('gridProperties'),dict):
                raise auth.GoogleError('Hedef normal bir hücre sayfası değil.')
            if wanted not in selected:
                sheet=book.create_sheet(props['title']);selected[wanted]=entry
                for area in entry.get('merges',[]):
                    try:
                        sheet.merge_cells(start_row=area.get('startRowIndex',0)+1,start_column=area.get('startColumnIndex',0)+1,
                                          end_row=area['endRowIndex'],end_column=area['endColumnIndex'])
                    except Exception:raise auth.GoogleError('Birleştirilmiş Google hücre yapısı okunamadı.') from None
        dates=sorted({(m['sheet'].casefold(),m['date_column']) for m in profile['excel_mappings'] if m['mode']=='date_row'})
        ranges=[]
        for name,col in dates:
            props=selected[name]['properties'];count=props['gridProperties'].get('rowCount',0)
            if not 0<count<=100000:raise auth.GoogleError('Tarih araması en çok 100.000 satırlı hedefi destekler.')
            ranges.append(_range(props['title'],f'{col}1:{col}{count}'))
        if ranges:
            query=urlencode([('ranges',r) for r in ranges]+[('valueRenderOption','UNFORMATTED_VALUE'),('dateTimeRenderOption','SERIAL_NUMBER')])
            data=auth.request_json(base+'/values:batchGet?'+query,token=token)
            values=data.get('valueRanges',[])
            if len(values)!=len(ranges):raise auth.GoogleError('Google tarih sütunları tam okunamadı.')
            for (name,col),value_range in zip(dates,values):
                sheet=book[selected[name]['properties']['title']]
                for i,entry in enumerate(value_range.get('values',[]),1):
                    if not entry:continue
                    value=entry[0]
                    if type(value) in (int,float):
                        if not math.isfinite(value):continue
                        # 1-31 gün numarası olarak kalır; büyük seri sayılar Google tarih hücresidir.
                        if 20000<=value<=2958465:value=date(1899,12,30)+timedelta(days=math.floor(value))
                    sheet.cell(i,column_index_from_string(col),value)
        # Başlık araması: başlık satırı ve ay kontrol hücresi Google'ın hesapladığı değerlerle okunur.
        headers=sorted({(m['sheet'].casefold(),f"{m['header_row']}:{m['header_row']}") for m in profile['excel_mappings'] if m['mode']=='header_lookup'}
                       |{(m['sheet'].casefold(),m['month_cell']) for m in profile['excel_mappings'] if m['mode']=='header_lookup' and m.get('month_cell')})
        for name,area in headers:
            props=selected[name]['properties']
            first=int(area.split(':')[0]) if ':' in area else int(''.join(ch for ch in area if ch.isdigit()))
            if first>props['gridProperties'].get('rowCount',0):raise auth.GoogleError('Başlık satırı veya ay kontrol hücresi Sheet sınırının dışında.')
        if headers:
            query=urlencode([('ranges',_range(selected[n]['properties']['title'],a)) for n,a in headers]+[('valueRenderOption','UNFORMATTED_VALUE'),('dateTimeRenderOption','SERIAL_NUMBER')])
            values=auth.request_json(base+'/values:batchGet?'+query,token=token).get('valueRanges',[])
            if len(values)!=len(headers):raise auth.GoogleError('Google başlık satırları tam okunamadı.')
            from openpyxl.utils.cell import coordinate_from_string
            for (name,area),value_range in zip(headers,values):
                sheet=book[selected[name]['properties']['title']]
                if ':' in area:start_row,start_col=int(area.split(':')[0]),1
                else:col,start_row=coordinate_from_string(area);start_col=column_index_from_string(col)
                for i,line in enumerate(value_range.get('values',[])):
                    for j,value in enumerate(line):
                        if value=='' or value is None:continue
                        if type(value) in (int,float) and math.isfinite(value):
                            # 1-31 gün numarasıdır; büyük seri sayılar Google tarih hücresidir.
                            if 20000<=value<=2958465:value=date(1899,12,30)+timedelta(days=math.floor(value))
                        sheet.cell(start_row+i,start_col+j,value)
        initial=_preview_plan(book,profile,rows,day_rows) if day_rows is not None else _plan(book,profile,rows)
        ready=[i for i in initial if i['status']=='ready']
        for item in ready:
            info=selected[item['sheet'].casefold()];grid=info['properties']['gridProperties']
            cell=book[item['sheet']][item['cell']]
            if cell.row>grid.get('rowCount',0) or cell.column>grid.get('columnCount',0):
                raise auth.GoogleError('Hedef hücre mevcut Sheet sınırının dışında. Google Sheets’te gerekli boş satır/sütunları ekleyin.')
            if _protected(info,item):raise auth.GoogleError('Hedef hücre Google Sheets’te korumalı; yazım yapılmadı.')
        if ready:
            query=urlencode([('ranges',_range(i['sheet'],i['cell'])) for i in ready]+[('valueRenderOption','FORMULA')])
            cells=auth.request_json(base+'/values:batchGet?'+query,token=token).get('valueRanges',[])
            if len(cells)!=len(ready):raise auth.GoogleError('Hedef hücreler tam okunamadı; yazım yapılmadı.')
            for item,data in zip(ready,cells):
                values=data.get('values',[])
                value=values[0][0] if values and values[0] else None
                # Formül korunur; dolu hücre bilgisi kaçan günün yalnız boş hücreye yazılması için gerekir.
                if value not in (None,''):
                    book[item['sheet']][item['cell']]=value
        final=_preview_plan(book,profile,rows,day_rows) if day_rows is not None else _plan(book,profile,rows)
        return {'items':final,'ready':sum(i['status']=='ready' for i in final),'errors':sum(i['status']=='error' for i in final),
                'written':False,'source':profile['source'],'provider':'google_sheets'},selected,token
    finally:book.close()


def preview(profile,rows,*,day_rows=None):return plan(profile,rows,day_rows=day_rows)[0]


def check(profile):
    """Yalnız okur: hesap, API, dosya erişimi ve eşlenen sayfa adları. Hiçbir hücreye yazmaz."""
    profile=J.normalize_profile(profile)
    if profile['source']!='netmera':raise auth.GoogleError('Demo projesi Google’a bağlanmaz. Veri kaynağını Netmera seçip kaydet.')
    if not profile.get('sheets_id'):raise auth.GoogleError('Önce Google Sheets dosya bağlantısını yapıştır.')
    base='https://sheets.googleapis.com/v4/spreadsheets/'+quote(profile['sheets_id'],safe='')
    token=auth.access_token(profile['id'])
    metadata=auth.request_json(base+'?'+urlencode({'fields':'spreadsheetId,properties(title),sheets(properties(title,sheetType))'}),token=token)
    if metadata.get('spreadsheetId')!=profile['sheets_id'] or not isinstance(metadata.get('sheets'),list):
        raise auth.GoogleError('Google hedef dosya kimliği doğrulanamadı.')
    titles=[s['properties']['title'][:100] for s in metadata['sheets']
            if isinstance(s,dict) and isinstance(s.get('properties'),dict) and isinstance(s['properties'].get('title'),str)
            and s['properties'].get('sheetType','GRID')=='GRID']
    title=(metadata.get('properties') or {}).get('title')
    known={t.casefold() for t in titles}
    wanted=sorted({m['sheet'] for m in profile.get('excel_mappings') or []},key=str.casefold)
    return {'ok':True,'sheets_id':profile['sheets_id'],'title':title[:200] if isinstance(title,str) else '','sheets':titles[:50],
            'missing_sheets':[w for w in wanted if w.casefold() not in known],'written':False}


def write(profile,batches,notes=None):
    profile=J.normalize_profile(profile);base=_base(profile)
    receipt_path=store.profile_dir(profile['id'])/'google-yazim-kaydi.json'
    receipt=store.read_json(receipt_path) or {'version':1,'written':[]}
    if (not isinstance(receipt,dict) or receipt.get('version')!=1 or not isinstance(receipt.get('written'),list)
            or not isinstance(receipt.get('skipped',[]),list)):
        raise auth.GoogleError('Google yazım kaydı okunamadı; tekrar yazım için kayıt otomatik sıfırlanmadı.')
    seen=set(receipt['written']);skipped=set(receipt.get('skipped',[]));count=0;fingerprint=mapping_signature(profile)
    def save_receipt():
        store.write_json(receipt_path,{**receipt,'version':1,'written':sorted(seen),'skipped':sorted(skipped)})
    for batch in sorted(batches,key=lambda b:b['planned_at']):
        identity={'id':batch['id'],'target':profile['sheets_id'],'map':fingerprint,'source':profile['source']}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        if key in seen or key in skipped:continue
        if batch['source']!=profile['source'] or batch.get('mapping_signature')!=fingerprint:
            raise auth.GoogleError('Bekleyen Sheets yazımının kaynağı veya eşlemesi değişmiş.')
        note=unsuccessful_mapping_note(profile,batch['rows'])
        if note is not None:
            if notes is not None:notes.append(note)
            skipped.add(key);save_receipt()
            continue
        result,sheets,token=plan(profile,batch['rows'])
        bad=[i for i in result['items'] if i['status']=='error']
        ready=[i for i in result['items'] if i['status']=='ready']
        if batch.get('catch_up'):
            from .cell_mapping import catch_up_notes
            if notes is not None:notes.extend(catch_up_notes(result['items']))
            if not ready:
                skipped.add(key);save_receipt()
                continue
        else:
            if bad:raise auth.GoogleError(bad[0]['job']+': '+bad[0]['message'])
            if not ready:raise auth.GoogleError('Eşlenen işler için başarılı sonuç yok; Google Sheets değiştirilmedi.')
        requests=[]
        from openpyxl.utils.cell import coordinate_from_string
        for item in ready:
            col,row=coordinate_from_string(item['cell']);props=sheets[item['sheet'].casefold()]['properties']
            requests.append({'updateCells':{'range':{'sheetId':props['sheetId'],'startRowIndex':row-1,'endRowIndex':row,
                                'startColumnIndex':column_index_from_string(col)-1,'endColumnIndex':column_index_from_string(col)},
                                'rows':[{'values':[{'userEnteredValue':{'numberValue':item['value']}}]}],'fields':'userEnteredValue'}})
        # Mutlak hücre adresleri ve tek atomik batch; belirsiz ağ hatasında kör tekrar yok.
        response=auth.request_json(base+':batchUpdate',method='POST',token=token,body={'requests':requests})
        if response.get('spreadsheetId')!=profile['sheets_id'] or not isinstance(response.get('replies'),list) or len(response['replies'])!=len(requests):
            raise auth.GoogleError('Google yazım onayı doğrulanamadı. Hedefi kontrol edin; yeniden deneme aynı rapor hücresini kullanır.')
        seen.add(key);save_receipt()
        count+=1
    return count
