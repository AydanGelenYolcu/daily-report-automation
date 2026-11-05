"""İş tanımlarını aktarır ve seçilen işleri yönetir; sonuç/rapor dosyalarını değiştirmez."""
from contextlib import ExitStack
from copy import deepcopy
import re
import uuid

from . import jobs as J, store


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,59}', value):
        raise J.JobError('Proje kimliği geçersiz.')
    return value


def _remove(profile, ids, clear=False):
    result = deepcopy(profile)
    removed = [j for j in result['jobs'] if j['id'] in ids]
    result['jobs'] = [j for j in result['jobs'] if j['id'] not in ids]
    result['excel_mappings'] = [m for m in result.get('excel_mappings', []) if m['job_id'] not in ids]
    if clear:
        result['ga4']['paketler'] = []  # eşitleme silinen işleri geri getirmesin
    else:
        for job in removed:
            package = job.get('ga4', {}).get('paket')
            if package:
                for entry in result.get('ga4', {}).get('paketler', []):
                    if entry['kimlik'] == package['kimlik']:
                        entry['haric'] = sorted(set(entry.get('haric', [])) | {package['rapor']})
    return J.normalize_profile(result)


def transfer(source_id, target_id, job_ids, mode):
    source_id, target_id = _id(source_id), _id(target_id)
    if source_id == target_id:
        raise J.JobError('Başka bir hedef proje seçin.')
    if mode not in ('copy', 'move'):
        raise J.JobError('Aktarım türü kopyala veya taşı olmalı.')
    if not isinstance(job_ids, list) or not job_ids or any(not isinstance(x, str) for x in job_ids) or len(job_ids) != len(set(job_ids)):
        raise J.JobError('Aktarılacak işleri seçin; seçim tekil iş kimliklerinden oluşmalı.')
    with ExitStack() as locks:
        for pid in sorted((source_id, target_id)):
            # Geçersiz/yok proje için klasör oluşturma.
            store.load_profile(pid)
            locks.enter_context(store.profile_lock(pid))
        original_source, original_target = store.load_profile(source_id), store.load_profile(target_id)
        selected = set(job_ids)
        if not selected <= {j['id'] for j in original_source['jobs']}:
            raise J.JobError('İş listesi değişti. Sayfayı yenileyip yeniden seçin.')
        moving = [deepcopy(j) for j in original_source['jobs'] if j['id'] in selected]
        for job in moving:
            missing = [v['job_id'] for v in job.get('variables', []) if v['kind'] == 'job' and v['job_id'] not in selected]
            if missing:
                raise J.JobError('Hesaplama işinin bağlı olduğu işleri de birlikte seçin.')
        if mode == 'move':
            for job in original_source['jobs']:
                if job['id'] not in selected and any(v['kind'] == 'job' and v['job_id'] in selected for v in job.get('variables', [])):
                    raise J.JobError(f"'{job['label']}' seçili işleri kullanıyor. Bu hesaplamayı da birlikte seçin.")
        target = deepcopy(original_target)
        ids = {j['id'] for j in target['jobs']}
        remap = {}
        for job in moving:
            new_id = 'aktar_' + uuid.uuid4().hex[:16]
            while new_id in ids:
                new_id = 'aktar_' + uuid.uuid4().hex[:16]
            ids.add(new_id); remap[job['id']] = new_id
        occupied = {J.column_label(j, c).casefold() for j in target['jobs'] for c in (j.get('columns') or [None])}
        names = {J.slug(j['label']) for j in target['jobs']}
        for job in moving:
            job['id'] = remap[job['id']]
            base, n = job['label'], 1
            while J.slug(job['label']) in names or any(J.column_label(job, c).casefold() in occupied for c in (job.get('columns') or [None])):
                n += 1; job['label'] = base[:40] + f' ({n})'  # slug 50 karakter: ayırt edici numara kesilmesin
            names.add(J.slug(job['label']))
            occupied.update(J.column_label(job, c).casefold() for c in (job.get('columns') or [None]))
            job['enabled'] = False  # hedef uygulama ve çıktı eşlemesi incelenene kadar zamanlanmış iş çalışmasın
            if job['type'] == 'ga4':
                job['ga4']['sheets'] = None
                job['ga4'].pop('paket', None)
            for var in job.get('variables', []):
                if var['kind'] == 'job':
                    var['job_id'] = remap[var['job_id']]
            target['jobs'].append(job)
        target = J.normalize_profile(target)
        source = _remove(original_source, selected) if mode == 'move' else original_source
        # Hedef doğrulanıp yazılmadan kaynak eksiltilmez. İkinci yazım hata verirse hedef geri alınır.
        store.write_json(store.profile_dir(target_id) / 'profil.json', target)
        if mode == 'move':
            try:
                store.write_json(store.profile_dir(source_id) / 'profil.json', source)
            except Exception:
                try:
                    store.write_json(store.profile_dir(target_id) / 'profil.json', original_target)
                except Exception:
                    raise J.JobError('Taşıma tamamlanamadı; kaynak işler korunuyor. Hedefte ek kopya kalmış olabilir, iki projeyi kontrol edin.') from None
                raise J.JobError('Taşıma kaydedilemedi; hedef geri alındı, kaynak işler korunuyor.') from None
        return {'count': len(moving), 'target_profile_id': target_id, 'job_ids': remap, 'mode': mode}


def bulk(profile_id, job_ids, action):
    profile_id = _id(profile_id)
    if action not in ('enable', 'disable', 'delete'):
        raise J.JobError('İşlem aç, kapat veya sil olmalı.')
    if not isinstance(job_ids, list) or not job_ids or any(not isinstance(x, str) for x in job_ids) or len(job_ids) != len(set(job_ids)):
        raise J.JobError('İşleri seçin; seçim tekil iş kimliklerinden oluşmalı.')
    store.load_profile(profile_id)
    with store.profile_lock(profile_id):
        profile = store.load_profile(profile_id)
        selected = set(job_ids)
        if not selected <= {j['id'] for j in profile['jobs']}:
            raise J.JobError('İş listesi değişti. Sayfayı yenileyip yeniden seçin.')
        if action == 'delete':
            for job in profile['jobs']:
                if job['id'] not in selected and any(v['kind'] == 'job' and v['job_id'] in selected for v in job.get('variables', [])):
                    raise J.JobError(f"'{job['label']}' seçili işleri kullanıyor. Bu hesaplamayı da birlikte seçin.")
            result = _remove(profile, selected)
        else:
            result = deepcopy(profile)
            for job in result['jobs']:
                if job['id'] in selected:
                    job['enabled'] = action == 'enable'
            result = J.normalize_profile(result)
        store.write_json(store.profile_dir(profile_id) / 'profil.json', result)
        return {'count': len(selected), 'action': action}


def delete_all(profile_id, expected_ids):
    profile_id = _id(profile_id)
    store.load_profile(profile_id)
    with store.profile_lock(profile_id):
        profile = store.load_profile(profile_id)
        if expected_ids != [j['id'] for j in profile['jobs']]:
            raise J.JobError('İş listesi değişti. Sayfayı yenileyip silmeyi tekrar onaylayın.')
        cleaned = _remove(profile, set(expected_ids), clear=True)
        store.write_json(store.profile_dir(profile_id) / 'profil.json', cleaned)
        return {'count': len(expected_ids)}
