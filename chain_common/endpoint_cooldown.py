"""Shared 429 deadlines for Python lanes and short-lived SDK processes.

Only SHA-256 endpoint identifiers are persisted. Immutable deadline markers
avoid read/modify/write races between processes (a shorter delay cannot win).
"""
import hashlib
import math
from pathlib import Path
import time


class EndpointCooldowns:
    def __init__(self, directory):
        self.directory = Path(directory)

    def folder(self, url):
        return self.directory / hashlib.sha256(url.encode()).hexdigest()

    def get(self, url, default=0):
        now = time.time()*1000
        deadline = now
        folder = self.folder(url)
        if folder.exists():
            for path in folder.glob('*.limit'):
                try:
                    stamp = int(path.stem)
                except ValueError:
                    continue
                if stamp <= now:
                    path.unlink(missing_ok=True)
                else:
                    deadline = max(deadline, stamp)
        return time.monotonic()+(deadline-now)/1000 if deadline > now else default

    def __contains__(self, url):
        return self.get(url) > time.monotonic()

    def __setitem__(self, url, deadline):
        delay = deadline-time.monotonic()
        if not math.isfinite(delay):
            delay = 120 if delay > 0 else 0
        if delay <= 0:
            return
        folder = self.folder(url)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = math.ceil(time.time()*1000+min(delay,120)*1000)
        (folder / (str(stamp)+'.limit')).touch(exist_ok=True)

    def clear(self):
        for path in self.directory.glob('*/*.limit'):
            path.unlink(missing_ok=True)
