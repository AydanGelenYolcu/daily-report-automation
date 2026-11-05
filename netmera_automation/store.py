"""Profil dosyaları ve sonuç deposu (%LOCALAPPDATA%\\NetmeraOtomasyon\\profiller\\<id>)."""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
from copy import deepcopy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

from .jobs import JobError, normalize_profile, slug
from .paths import APP_ROOT, data_root

SEED_FILE = APP_ROOT / "config" / "varsayilan-isler.json"
_PROFILE_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


def profiles_root() -> Path:
    return data_root() / "profiller"


def profile_dir(profile_id: str) -> Path:
    if not isinstance(profile_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", profile_id):
        raise JobError("Profil kimliği geçersiz.")
    root = profiles_root().resolve()
    folder = root / profile_id
    if folder.resolve().parent != root:
        raise JobError("Profil klasörü veri konumunun dışında olamaz.")
    return folder


def write_json(path: Path, data) -> None:
    """Önce geçici dosyaya yazar, sonra değiştirir: yarım dosya kalmaz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def profile_revision(profile: dict) -> str:
    """Eski bir sekmenin yeni ayarları ezmesini önleyen içerik sürümü."""
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run_preview(run: dict, limit: int = 200, offset: int = 0) -> dict:
    """Gezinmede raporun tamamı taşınmaz; tam sonuç diskte kalır."""
    preview = {k: v for k, v in run.items() if k not in ('rows', 'day_rows', 'ga4')}
    rows, days = run.get('rows') or [], run.get('day_rows') or []
    selected = rows[offset:offset + limit]
    remaining = max(0, limit - len(selected))
    start = max(0, offset - len(rows))
    preview.update(rows=selected, day_rows=days[start:start + remaining])
    # GA4 tabloları sonuç/dışa aktarma ekranında ayrıca yüklenir.
    preview['ga4'] = []
    preview['ga4_count'] = len(run.get('ga4') or [])
    preview['preview_total'] = len(rows) + len(days)
    preview['preview_offset'] = offset
    preview['preview_truncated'] = preview['preview_total'] > limit
    return preview


def write_last_run(profile_id: str, run: dict) -> None:
    folder = profile_dir(profile_id)
    write_json(folder / 'son-calisma.json', run)
    try:
        write_json(folder / 'arayuz-son-calisma.json', {
            'source_stamp': (folder / 'son-calisma.json').stat().st_mtime_ns,
            'run': run_preview(run),
        })
    except OSError:
        pass  # Özet yazılamasa da asıl çalışma sonucu saklandı.


def last_run_preview(profile_id: str) -> dict | None:
    folder = profile_dir(profile_id)
    path = folder / 'son-calisma.json'
    try:
        stat = path.stat()
        cached = read_json(folder / 'arayuz-son-calisma.json')
        if cached and cached.get('source_stamp') == stat.st_mtime_ns:
            return cached['run']
        # Eski sürümün büyük sonuçları ilk açılışı da kilitlemesin.
        if stat.st_size > 512_000:
            return {'rows': [], 'day_rows': [], 'ga4': [], 'history_deferred': True}
        run = read_json(path)
        return run_preview(run) if isinstance(run, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def seed() -> dict:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def list_profiles() -> list[dict]:
    return [p for p in _scan() if "error" not in p]


def broken_profiles() -> list[dict]:
    """Açılamayan projeler (ör. eski sürümden kalan geçersiz ayar). Sessizce gizlenmez; arayüzde gösterilir."""
    return [p for p in _scan() if "error" in p]


def _scan() -> list[dict]:
    root = profiles_root()
    found = []
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or not (folder / "profil.json").is_file():
                continue
            data = None
            try:
                found.append(load_profile(folder.name))
            except (OSError, ValueError) as exc:
                try:
                    data = read_json(folder / 'profil.json')
                except (OSError, ValueError):
                    pass
                name = data.get("name") if isinstance(data, dict) else None
                error = str(exc) if isinstance(exc, JobError) else "Proje ayar dosyası okunamadı; dosya bozuk veya erişilemiyor."
                found.append({"id": folder.name, "name": str(name or folder.name), "error": error})
    return found


def load_profile(profile_id: str) -> dict:
    path = profile_dir(profile_id) / "profil.json"
    if not path.is_file():
        raise JobError(f"Profil bulunamadı: {profile_id}")
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        key = str(path)
        with _CACHE_LOCK:
            cached = _PROFILE_CACHE.get(key)
            if cached and cached[0] == stamp:
                _PROFILE_CACHE.move_to_end(key)
                return deepcopy(cached[1])
        data = read_json(path)
    except (OSError, ValueError):
        raise JobError("Proje ayar dosyası okunamadı; dosya bozuk veya erişilemiyor.") from None
    profile = normalize_profile(data, allow_incomplete_dates=True)
    with _CACHE_LOCK:
        if stat.st_size <= 2_000_000:
            _PROFILE_CACHE[key] = (stamp, deepcopy(profile))
            _PROFILE_CACHE.move_to_end(key)
            while len(_PROFILE_CACHE) > 128 or sum(entry[0][2] for entry in _PROFILE_CACHE.values()) > 16_000_000:
                _PROFILE_CACHE.popitem(last=False)
    return profile


def save_profile(raw: dict) -> dict:
    profile = normalize_profile(raw)
    write_json(profile_dir(profile["id"]) / "profil.json", profile)
    return profile


def append_excel_batch(profile: dict, batch: dict) -> None:
    """Eklemeli çıktının tekrar oynatılabilir çalışma kaydı; sonuç deposundan ayrıdır."""
    path = profile_dir(profile['id']) / 'excel-calismalar.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    if profile.get('excel_mode')=='mapped_cells':
        from .cell_mapping import mapping_signature
        batch={**batch,'mapping_signature':mapping_signature(profile)}
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({**batch, 'output_file':profile['output_file'],
                                 'output_target':profile.get('output_target','excel'),'sheets_id':profile.get('sheets_id',''),
                                 'excel_mode':profile.get('excel_mode','rebuild'),
                                 'excel_sheet':profile.get('excel_sheet','Netmera')}, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def excel_batches(profile: dict) -> list[dict]:
    path = profile_dir(profile['id']) / 'excel-calismalar.jsonl'
    if not path.exists():
        return []
    provider=profile.get('output_target','excel')
    target = profile.get('sheets_id','') if provider=='google_sheets' else os.path.normcase(str(Path(profile['output_file']).resolve()))
    batches = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        batch = json.loads(line)
        batch_target=batch.get('sheets_id','') if provider=='google_sheets' else os.path.normcase(str(Path(batch['output_file']).resolve()))
        if (batch.get('output_target','excel')==provider and batch_target == target
                and batch['excel_mode'] == profile.get('excel_mode','rebuild')
                and batch['excel_sheet'] == profile.get('excel_sheet','Netmera')):
            if profile.get('excel_mode')=='mapped_cells':
                from .cell_mapping import mapping_signature
                if batch.get('mapping_signature')!=mapping_signature(profile):continue
            batches.append(batch)
    return batches


def new_profile_id(name: str) -> str:
    base = slug(name, "profil").replace("_", "-")[:30].strip("-") or "profil"
    existing = {p.name for p in profiles_root().iterdir()} if profiles_root().is_dir() else set()
    candidate, n = base, 2
    while candidate in existing:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def create_profile(name: str, *, source: str = "netmera", copy_from: str | None = None) -> dict:
    jobs = load_profile(copy_from)["jobs"] if copy_from else seed()["jobs"]
    base = load_profile(copy_from) if copy_from else {}
    profile = {**base, "id": new_profile_id(name), "name": name, "source": source, "jobs": jobs,
               "schedule": {"times": [], "days": ["MON", "TUE", "WED", "THU", "FRI"]}}
    if copy_from:
        profile["output_file"] = ""  # iki profil aynı Excel'e yazmasın
        profile['sheets_id']=''  # kopya otomatik aynı Google dosyasına yazmasın
        if isinstance(profile.get('ga4'), dict) and isinstance(profile['ga4'].get('sheets'), dict):
            profile['ga4'] = {**profile['ga4'], 'sheets': {**profile['ga4']['sheets'], 'tablo': None}}  # GA4 sekmeleri de
    return save_profile(profile)


def ensure_defaults() -> None:
    """İlk açılışta bir prod profili ve bir demo profili oluşturur."""
    if list_profiles():
        return
    create_profile("Production")
    demo = create_profile("Demo (sahte veri)", source="demo")
    demo["output_file"] = str(profile_dir(demo["id"]) / "demo-sonuclar.xlsx")
    save_profile(demo)


def delete_profile(profile_id: str) -> None:
    folder = profile_dir(profile_id)
    if not (folder / "profil.json").is_file():
        raise JobError("Profil bulunamadı.")
    shutil.rmtree(folder)


# --- sonuç deposu -----------------------------------------------------------

RESULT_KEY_FIELDS = ("source", "job_id", "signature", "column", "period_start", "period_end", "snapshot_slot")


def record_key(row: dict) -> str:
    payload = {field: row.get(field) for field in RESULT_KEY_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_results(profile_id: str) -> dict[str, dict]:
    """Anahtar → son geçerli satır. Hatalı yeni sonuç, eski başarılı sonucu silmez."""
    path = profile_dir(profile_id) / "sonuclar.jsonl"
    stored: dict[str, dict] = {}
    if not path.is_file():
        return stored
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # yarım kalmış son satır
        key = record_key(row)
        previous = stored.get(key)
        if previous and previous["status"] == "OK" and row["status"] != "OK":
            continue
        stored[key] = row
    return stored


def append_results(profile_id: str, rows: list[dict]) -> None:
    path = profile_dir(profile_id) / "sonuclar.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# --- aynı profil için tek çalışma ---------------------------------------------

def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # Windows'ta os.kill(pid, 0) işlemi sonlandırır; bu yüzden yalnız sorgulanır.
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


class BusyError(RuntimeError):
    pass


@contextmanager
def profile_lock(profile_id: str):
    lock = profile_dir(profile_id) / "calisiyor.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                raise BusyError("Bu profil için başka bir çalışma sürüyor (arayüz veya zamanlanmış görev).")
            lock.unlink(missing_ok=True)
    else:
        raise BusyError("Çalışma kilidi alınamadı.")
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        lock.unlink(missing_ok=True)
