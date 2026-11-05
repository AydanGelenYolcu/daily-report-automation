"""Yalnız yerel istek süreleri. Gövde, sorgu, profil adı veya sonuç değeri kaydedilmez."""
from collections import deque
from datetime import datetime
import json
import threading

from .paths import data_root


class RequestMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.recent = deque(maxlen=100)

    def record(self, method, route, elapsed, status, size):
        if not route.startswith('/api/') or len(route) > 80:
            return
        entry = {'at': datetime.now().astimezone().isoformat(timespec='seconds'),
                 'method': method, 'route': route, 'ms': round(elapsed * 1000),
                 'status': status, 'bytes': size}
        with self.lock:
            self.recent.append(entry)
            if entry['ms'] < 1000 and status < 400:
                return
            try:
                root = data_root()
                root.mkdir(parents=True, exist_ok=True)
                path = root / 'arayuz-performans.jsonl'
                if path.exists() and path.stat().st_size > 256_000:
                    path.replace(root / 'arayuz-performans.onceki.jsonl')
                with path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
            except OSError:
                pass  # Ölçüm yazılamaması gezinmeyi engellemez.

    def snapshot(self):
        with self.lock:
            return list(self.recent)
