"""Demo okuyucu: Netmera'ya bağlanmaz, sahte ama tekrarlanabilir sayılar üretir."""
from __future__ import annotations

from datetime import date
import hashlib
import time

from .jobs import signature


class SyntheticReader:
    def __init__(self, log=print, delay: float = 0.3):
        self.log = log
        self.delay = delay

    def fetch(self, job: dict, first: date, last: date):
        time.sleep(self.delay)
        columns = job.get("columns") or [None]
        values = {}
        for column in columns:
            seed = f"{signature(job)}|{column}|{first}|{last}"
            number = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
            values[column] = 1_000 + number % 90_000
        note = {"segment": "sahte segment güncelleme zamanı",
                "segment_run": f"sahte segment hesaplaması {first:%d.%m.%Y}–{last:%d.%m.%Y}"}.get(job["type"])
        return values, note
