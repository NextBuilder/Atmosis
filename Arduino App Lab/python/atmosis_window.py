from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence

AXIS_ORDER = (
    "iaq",
    "tvoc_ppm",
    "hcho_ppm",
    "co_ppm",
    "temp_c",
    "rh_pct",
    "dust_ugm3",
    "pressure_hpa",
    "altitude_m",
)

NORMALIZED_SUFFIX = "_n"


def base_axis_name(name: str) -> str:
    return name[:-len(NORMALIZED_SUFFIX)] if name.endswith(NORMALIZED_SUFFIX) else name


@dataclass
class Sample:
    iaq: float = 0.0
    tvoc_ppm: float = 0.0
    hcho_ppm: float = 0.0
    co_ppm: float = 0.0
    temp_c: float = 0.0
    rh_pct: float = 0.0
    dust_ugm3: float = 0.0
    pressure_hpa: float = 0.0
    altitude_m: float = 0.0
    timestamp: float = 0.0

    def as_axes(self) -> List[float]:
        return [getattr(self, name) for name in AXIS_ORDER]


class AtmosisWindow:
    def __init__(self, window_samples: int, axis_names: Optional[Sequence[str]] = None):
        if window_samples < 1:
            raise ValueError("window_samples must be at least 1")
        if axis_names is not None:
            actual = [base_axis_name(a) for a in axis_names]
            if actual != list(AXIS_ORDER):
                raise ValueError(
                    "Model axis order does not match this backend.\n"
                    f"  Model expects : {list(axis_names)}\n"
                    f"  Backend sends : {list(AXIS_ORDER)}"
                )
        self.window_samples = window_samples
        self.axis_count = len(AXIS_ORDER)
        self.feature_count = window_samples * self.axis_count
        self._lock = threading.Lock()
        self._buffer: Deque[Sample] = deque(maxlen=window_samples)

    def push(self, sample: Sample) -> None:
        if sample.timestamp == 0.0:
            sample.timestamp = time.time()
        with self._lock:
            self._buffer.append(sample)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    @property
    def fill_level(self) -> int:
        with self._lock:
            return len(self._buffer)

    def flatten(self) -> Optional[List[float]]:
        with self._lock:
            if len(self._buffer) < self.window_samples:
                return None
            snapshot = list(self._buffer)
        features: List[float] = []
        for sample in snapshot:
            features.extend(sample.as_axes())
        return features

    def timing_stats(self) -> dict:
        with self._lock:
            stamps = [s.timestamp for s in self._buffer]
        if len(stamps) < 2:
            return {"mean_interval_ms": 0.0, "jitter_ms": 0.0, "samples": len(stamps)}
        gaps = [(stamps[i] - stamps[i - 1]) * 1000.0 for i in range(1, len(stamps))]
        mean = sum(gaps) / len(gaps)
        return {
            "mean_interval_ms": round(mean, 1),
            "jitter_ms": round(max(abs(g - mean) for g in gaps), 1),
            "samples": len(stamps),
        }


class ClassificationSmoother:
    def __init__(self, required_votes: int = 3):
        self.required_votes = max(1, required_votes)
        self._candidate: Optional[str] = None
        self._votes = 0
        self._stable: Optional[str] = None

    def update(self, label: str) -> Optional[str]:
        if label == self._candidate:
            self._votes += 1
        else:
            self._candidate = label
            self._votes = 1
        if self._votes >= self.required_votes:
            self._stable = self._candidate
        return self._stable

    def reset(self) -> None:
        self._candidate = None
        self._votes = 0
        self._stable = None
