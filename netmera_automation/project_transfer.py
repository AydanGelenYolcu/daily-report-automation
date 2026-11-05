"""Proje tanımlarını JSON ile taşır. Oturum, sır, sonuç veya rapor dosyası taşımaz."""
from copy import deepcopy
from datetime import datetime
import json
import shutil
import threading
import uuid
from zoneinfo import ZoneInfo

from . import __version__, jobs as J, store
from .paths import TIMEZONE

FORMAT = "netmera-projects"
FORMAT_VERSION = 2
MAX_BYTES = 5_000_000
MAX_PROJECTS = 100
_LOCK = threading.Lock()


def _check_size(bundle):
    try:
        size = len(json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise J.JobError("Proje dosyasının JSON yapısı geçersiz.") from None
    if size > MAX_BYTES:
        raise J.JobError("Proje dosyası en çok 5 MB olabilir; daha az proje seçin.")


def _profiles(bundle):
    _check_size(bundle)
    if (not isinstance(bundle, dict) or bundle.get("format") != FORMAT
            or type(bundle.get("format_version")) is not int or bundle["format_version"] not in (1, FORMAT_VERSION)):
        raise J.JobError("Desteklenmeyen proje dosyası. Bu uygulamanın dışa aktardığı JSON dosyasını seçin.")
    if set(bundle) - {"format", "format_version", "app_version", "exported_at", "projects", "connections"}:
        raise J.JobError("Proje dosyasında desteklenmeyen alanlar var.")
    projects = bundle.get("projects")
    if not isinstance(projects, list) or not 1 <= len(projects) <= MAX_PROJECTS:
        raise J.JobError("Dosyada 1–100 proje bulunmalı.")
    normalized = []
    for index, raw in enumerate(projects, 1):
        try:
            _check_local_paths(raw)
            profile = J.normalize_profile(raw, allow_incomplete_dates=True)
            # Kimlikler değişirse hesaplama bağımlılıkları ve hücre eşlemeleri şaşabilir.
            ids = [j.get("id") for j in raw.get("jobs", [])]
            if any(not isinstance(key, str) or not J.JOB_ID.fullmatch(key) for key in ids) or len(ids) != len(set(ids)):
                raise J.JobError("İş kimlikleri geçerli ve tekil olmalı.")
            if ids != [j['id'] for j in profile['jobs']]:
                raise J.JobError('İş kimlikleri aktarımda değişmemeli.')
        except J.JobError as exc:
            raise J.JobError(f"{index}. proje geçersiz: {exc}") from None
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            raise J.JobError(f"{index}. projenin ayar yapısı geçersiz.") from None
        normalized.append(profile)
    _connections(bundle)
    return normalized


def _connections(bundle):
    """Yalnız paylaşılabilir mülk eşlemeleri. Oturum/istemci sırrı bu biçime alınmaz."""
    from .ga4 import ayarlar as settings
    raw = bundle.get('connections', {})
    if not isinstance(raw, dict) or set(raw) - {'ga4_properties'}:
        raise J.JobError('Bağlantı ayarları geçersiz.')
    properties = raw.get('ga4_properties', {})
    try:
        settings._dogrula({'propertyler': properties}, 'proje aktarımı')
    except settings.SettingsError as exc:
        raise J.JobError(str(exc)) from None
    return deepcopy(properties)


def _property_refs(profiles):
    return {j['ga4']['property_ref'] for p in profiles for j in p['jobs'] if j['type'] == 'ga4'}


def _property_plan(bundle, profiles):
    from .ga4 import ayarlar as settings
    used = _property_refs(profiles)
    incoming = {k:v for k,v in _connections(bundle).items() if k in used}
    if not incoming:
        return None, {}, []
    existing = settings._mevcut()
    merged, remap, added = deepcopy(existing), {}, []
    for ref, entry in incoming.items():
        target, number = ref, 2
        while target in merged['propertyler'] and merged['propertyler'][target]['property_id'] != entry['property_id']:
            suffix = f'_{number}'
            target, number = ref[:40-len(suffix)] + suffix, number + 1
        if target not in merged['propertyler']:
            merged['propertyler'][target] = deepcopy(entry)
            added.append(target)
        remap[ref] = target
    return merged, remap, added


def _check_local_paths(raw):
    """Dosyadan gelen UNC/aygıt yollarını Path.resolve veya önizlemeden önce reddet."""
    pending = [raw]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'output_file' or (key == 'file' and node.get('source') == 'excel'):
                    if isinstance(value, str) and value.strip().replace('/', '\\').startswith('\\\\'):
                        raise J.JobError('Ağ ve aygıt dosya yolları içe alınamaz; yerel Excel dosyası yolu kullanın.')
                if isinstance(value, (dict, list)):
                    pending.append(value)
        elif isinstance(node, list):
            pending.extend(node)


def export_projects(profile_ids):
    if (not isinstance(profile_ids, list) or not 1 <= len(profile_ids) <= MAX_PROJECTS
            or any(not isinstance(key, str) for key in profile_ids) or len(profile_ids) != len(set(profile_ids))):
        raise J.JobError("Dışa aktarmak için 1–100 farklı proje seçin.")
    # Yalnız normalize edilmiş profil alanları: yan dosyalar ve bilinmeyen sır alanları okunmaz.
    profiles = [store.load_profile(key) for key in profile_ids]
    from .ga4 import ayarlar as settings
    try:
        properties = settings.load()['propertyler']
    except settings.SettingsError:
        properties = {}
    used = _property_refs(profiles)
    properties = {k:deepcopy(v) for k,v in properties.items() if k in used}
    now = datetime.now(ZoneInfo(TIMEZONE))
    bundle = {"format": FORMAT, "format_version": FORMAT_VERSION, "app_version": __version__,
              "exported_at": now.isoformat(), "projects": profiles,
              "connections": {'ga4_properties': properties}}
    _profiles(bundle)  # Üretilen dosya aynı içe alma sözleşmesini karşılamalı.
    suffix = J.slug(profiles[0]["name"]) + "-" if len(profiles) == 1 else ""
    return {"bundle": bundle, "filename": f"netmera-projeler-{suffix}{now:%Y%m%d-%H%M%S}.json",
            'notes': ['Bazı GA4 mülk eşlemeleri bu bilgisayarda tanımlı değil; hedefte mülk seçilmeli.'] if used - properties.keys() else []}


def _new_name(name, occupied):
    candidate, number = name, 2
    while candidate.casefold() in occupied:
        suffix = f" ({number})"
        candidate = name[:60 - len(suffix)] + suffix
        number += 1
    occupied.add(candidate.casefold())
    return candidate


def _names():
    return {p["name"].casefold() for p in store.list_profiles() + store.broken_profiles()}


def preview_import(bundle):
    profiles, occupied = _profiles(bundle), _names()
    _, remap, added = _property_plan(bundle, profiles)
    return {"items": [{"index": i, "name": p["name"], "new_name": _new_name(p["name"], occupied),
                       "source": p["source"], "jobs": len(p["jobs"]), "mappings": len(p["excel_mappings"]),
                       "ga4": sum(j["type"] == "ga4" for j in p["jobs"])} for i, p in enumerate(profiles)],
            'connections': {'ga4_properties':len(added), 'remapped':sum(k != v for k,v in remap.items())}}


def import_projects(bundle, indices, *, keep_enabled=False):
    profiles = _profiles(bundle)  # Tüm dosya doğrulanmadan hiçbir klasör oluşturulmaz.
    if (not isinstance(indices, list) or not indices or any(type(i) is not int or not 0 <= i < len(profiles) for i in indices)
            or len(indices) != len(set(indices))):
        raise J.JobError("İçe alınacak projeleri seçin; seçim geçerli ve tekil olmalı.")
    if type(keep_enabled) is not bool:
        raise J.JobError("İşlerin etkinliğini koruma seçeneği geçersiz.")
    with _LOCK:
        occupied, staged = _names(), []
        connection_settings, refs, added = _property_plan(bundle, [profiles[i] for i in sorted(indices)])
        for index in sorted(indices):
            profile = deepcopy(profiles[index])
            profile["name"] = _new_name(profile["name"], occupied)
            profile["id"] = "aktar-" + uuid.uuid4().hex[:24]
            for job in profile['jobs']:
                if job['type'] == 'ga4':
                    ref = job['ga4']['property_ref']
                    job['ga4']['property_ref'] = refs.get(ref, ref)
            if not keep_enabled:
                for job in profile["jobs"]:
                    job["enabled"] = False
            staged.append(J.normalize_profile(profile, allow_incomplete_dates=True))
        created = []
        try:
            for profile in staged:
                while True:
                    folder = store.profile_dir(profile["id"])
                    try:
                        folder.mkdir(parents=True, exist_ok=False)
                        break
                    except FileExistsError:
                        profile["id"] = "aktar-" + uuid.uuid4().hex[:24]
                created.append(folder)
                store.write_json(folder / "profil.json", profile)
            if added:
                from .ga4 import ayarlar as settings
                store.write_json(settings.settings_path(), connection_settings)
        except Exception:
            complete = True
            root = store.profiles_root().resolve()
            for folder in reversed(created):
                try:
                    if folder.resolve().parent != root:
                        raise OSError()
                    shutil.rmtree(folder)
                except OSError:
                    complete = False
            message = ("İçe alma kaydedilemedi; oluşturulan projeler geri alındı." if complete else
                       "İçe alma tamamlanamadı; bazı yeni proje klasörleri kalmış olabilir. Projeler ekranını kontrol edin.")
            raise J.JobError(message) from None
    return {"count": len(staged), 'connections_added':len(added), "projects": [{"id": p["id"], "name": p["name"], "jobs": len(p["jobs"])} for p in staged],
            "message": f"{len(staged)} proje içe alındı. " + ("İşlerin etkinlik durumları korundu. " if keep_enabled else "İşler kapalı olarak eklendi. ")
                       + f"Tablo/dosya bağlantıları korundu; {len(added)} GA4 mülk eşlemesi eklendi. Google hesabına hedef bilgisayarda giriş yapın. Zamanlama otomatik kurulmadı."}
