from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

from atmosis_log import log

METRICS = ("score", "pm", "iaq", "tvoc", "hcho", "co", "temp", "hum", "pres", "alt")


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log("warn", f"Could not save {path.name}: {exc}")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class _Bucket:
    def __init__(self, start: float):
        self.start = start
        self.sums: Dict[str, float] = {}
        self.counts: Dict[str, int] = {}

    def add(self, values: dict) -> None:
        for k in METRICS:
            v = values.get(k)
            if _finite(v):
                self.sums[k] = self.sums.get(k, 0.0) + v
                self.counts[k] = self.counts.get(k, 0) + 1

    def point(self) -> dict:
        p = {"t": round(self.start, 1)}
        for k in METRICS:
            n = self.counts.get(k)
            p[k] = round(self.sums[k] / n, 4) if n else None
        return p


class History:
    def __init__(self, path: Path, fine_step=2, fine_span=3600, coarse_step=60, coarse_span=86400):
        self.path = path
        self.fine_step, self.coarse_step = fine_step, coarse_step
        self.fine: deque = deque(maxlen=fine_span // fine_step)
        self.coarse: deque = deque(maxlen=coarse_span // coarse_step)
        self._fine_bucket: Optional[_Bucket] = None
        self._coarse_bucket: Optional[_Bucket] = None
        self._lock = threading.Lock()
        self._last_save = time.time()
        cutoff = time.time() - coarse_span
        for p in _read_json(path, []):
            if isinstance(p, dict) and _finite(p.get("t")) and p["t"] >= cutoff:
                self.coarse.append(p)

    def add(self, values: dict, now: Optional[float] = None) -> None:
        now = now or time.time()
        with self._lock:
            self._fine_bucket = self._roll(self._fine_bucket, self.fine, self.fine_step, now)
            self._coarse_bucket = self._roll(self._coarse_bucket, self.coarse, self.coarse_step, now)
            self._fine_bucket.add(values)
            self._coarse_bucket.add(values)
        if now - self._last_save > 300:
            self.save()

    @staticmethod
    def _roll(bucket, store, step, now):
        start = now - (now % step)
        if bucket is None:
            return _Bucket(start)
        if bucket.start != start:
            store.append(bucket.point())
            return _Bucket(start)
        return bucket

    def query(self, span_s: float) -> dict:
        cutoff = time.time() - span_s
        with self._lock:
            use_fine = span_s <= 3600
            points = [p for p in (self.fine if use_fine else self.coarse) if p["t"] >= cutoff]
            current = self._fine_bucket if use_fine else self._coarse_bucket
            if current is not None and current.counts:
                points.append(current.point())
        return {"step_s": self.fine_step if span_s <= 3600 else self.coarse_step, "points": points}

    def save(self) -> None:
        with self._lock:
            data = list(self.coarse)
            self._last_save = time.time()
        _write_json(self.path, data)


class EventLog:
    def __init__(self, path: Path, limit: int = 200):
        self.path = path
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=limit)
        self._dirty = False
        self._last_save = 0.0
        for e in _read_json(path, [])[-limit:]:
            if isinstance(e, dict) and "t" in e:
                self._events.append(e)

    def add(self, level: str, title: str, detail: str = "") -> dict:
        event = {"t": round(time.time(), 1), "level": level, "title": title, "detail": detail}
        with self._lock:
            self._events.append(event)
            self._dirty = True
        log(level, title + (f" — {detail}" if detail else ""))
        self.flush()
        return event

    def list(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return list(self._events)[-limit:][::-1]

    def flush(self, force: bool = False) -> None:
        if not self._dirty or (not force and time.time() - self._last_save < 10):
            return
        with self._lock:
            data = list(self._events)
            self._dirty = False
            self._last_save = time.time()
        _write_json(self.path, data)
