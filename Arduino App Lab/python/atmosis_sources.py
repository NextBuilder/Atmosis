from __future__ import annotations

import math
import queue
import random
import threading
import time
from dataclasses import dataclass, fields
from typing import Optional

from atmosis_log import log

FLAG_X6        = 0x01
FLAG_PRESSURE  = 0x02
FLAG_DUST      = 0x04
FLAG_DUST_TIME = 0x08
FLAG_FAILSAFE  = 0x10
FLAG_CO_ALARM  = 0x20
FLAG_LED       = 0x40

FRAME_PROTOCOL = 5
FRAME_LENGTH = 19
INFO_LENGTH = 15
PRESSURE_CHIPS = {0: "", 1: "BMP388", 2: "BMP390", 3: "DPS310"}
DUST_STATUS = {0: "starting", 1: "ok", 2: "no_signal", 3: "saturated"}
X6_STATUS = {0: "STARTING", 1: "OK", 2: "TIMEOUT", 3: "CHECKSUM", 4: "OUT_OF_RANGE"}
PRESSURE_STATUS = {0: "NOT_DETECTED", 1: "OK", 2: "INIT_FAILED", 3: "UNKNOWN_CHIP", 4: "LOST"}

VALID_RANGES = {
    "iaq": (0.0, 500.0),
    "tvoc_ppm": (0.0, 10.0),
    "hcho_ppm": (0.0, 2.0),
    "co_ppm": (0.0, 200.0),
    "temperature_c": (-40.0, 85.0),
    "humidity_pct": (0.0, 100.0),
}


@dataclass
class Snapshot:
    iaq: float = 0.0
    tvoc_ppm: float = 0.0
    hcho_ppm: float = 0.0
    co_ppm: float = 0.0
    temperature_c: float = 0.0
    humidity_pct: float = 0.0
    pm25_ugm3: float = 0.0
    pressure_hpa: float = 0.0
    altitude_m: float = 0.0
    sensor_temp_c: float = 0.0
    x6_valid: bool = False
    pressure_valid: bool = False
    dust_valid: bool = False
    dust_timing_ok: bool = True
    mcu_failsafe: bool = False
    co_alarm: bool = False
    led_ok: bool = True
    dust_sensor_v: float = 0.0
    dust_baseline_v: float = 0.0
    dust_status: str = ""
    pressure_chip: str = ""
    loop_max_ms: int = 0
    seq: int = 0
    link_up: bool = False
    mcu_uptime_s: int = 0
    timestamp: float = 0.0
    error: str = ""

    @property
    def any_valid(self) -> bool:
        return self.x6_valid or self.pressure_valid or self.dust_valid

    def carry(self) -> "Snapshot":
        keep = {"iaq", "tvoc_ppm", "hcho_ppm", "co_ppm", "temperature_c", "humidity_pct",
                "pm25_ugm3", "pressure_hpa", "altitude_m", "sensor_temp_c", "pressure_chip",
                "dust_sensor_v", "dust_baseline_v", "mcu_uptime_s", "seq"}
        return Snapshot(**{f.name: getattr(self, f.name) for f in fields(self) if f.name in keep})


def parse_frame(params) -> Snapshot:
    p = list(params)
    if len(p) != FRAME_LENGTH:
        raise ValueError(f"frame needs {FRAME_LENGTH} values, got {len(p)}")
    if int(p[0]) != FRAME_PROTOCOL:
        raise ValueError(f"frame protocol {p[0]} is not {FRAME_PROTOCOL} — re-upload the sketch")
    values = [float(v) for v in p[4:11]] + [float(v) for v in p[14:17]]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("frame contained a non-finite value")
    flags = int(p[3])
    snap = Snapshot(
        seq=int(p[1]), mcu_uptime_s=int(p[2]),
        iaq=float(p[4]), tvoc_ppm=float(p[5]), hcho_ppm=float(p[6]), co_ppm=float(p[7]),
        temperature_c=float(p[8]), humidity_pct=float(p[9]), pm25_ugm3=float(p[10]),
        dust_sensor_v=int(p[11]) / 1000.0, dust_baseline_v=int(p[12]) / 1000.0,
        dust_status=DUST_STATUS.get(int(p[13]), "unknown"),
        pressure_hpa=float(p[14]), altitude_m=float(p[15]), sensor_temp_c=float(p[16]),
        pressure_chip=PRESSURE_CHIPS.get(int(p[17]), ""), loop_max_ms=int(p[18]),
        x6_valid=bool(flags & FLAG_X6), pressure_valid=bool(flags & FLAG_PRESSURE),
        dust_valid=bool(flags & FLAG_DUST), dust_timing_ok=bool(flags & FLAG_DUST_TIME),
        mcu_failsafe=bool(flags & FLAG_FAILSAFE), co_alarm=bool(flags & FLAG_CO_ALARM),
        led_ok=bool(flags & FLAG_LED),
        link_up=True, timestamp=time.time(),
    )
    for name, (low, high) in VALID_RANGES.items():
        value = getattr(snap, name)
        if snap.x6_valid and not (low <= value <= high):
            snap.x6_valid = False
            snap.error = f"{name}={value:.3f} outside {low}..{high}"
    return snap


def parse_info(params) -> dict:
    p = [int(v) for v in params]
    if len(p) != INFO_LENGTH or p[0] != FRAME_PROTOCOL:
        raise ValueError("info message does not match this app — re-upload the sketch")
    return {
        "fw": f"{p[1] // 10}.{p[1] % 10}",
        "x6_ok": str(p[2]), "x6_bad": str(p[3]), "x6": X6_STATUS.get(p[4], "?"),
        "p_st": PRESSURE_STATUS.get(p[5], "?"), "p": PRESSURE_CHIPS.get(p[6], "") or "none",
        "p_bus": "Wire (SDA/SCL)", "p_addr": f"0x{p[7]:02X}" if p[7] else "-",
        "p_id": f"0x{p[8]:02X}", "p_tries": str(p[9]),
        "dust": DUST_STATUS.get(p[10], "?").upper(), "dust_on_mv": f"{p[11] / 10:.1f}",
        "dust_led": "HIGH" if p[12] else "LOW", "led": "ok" if p[13] else "fail", "leds": str(p[14]),
    }


class McuSource:
    name = "board"

    def __init__(self, bridge, available: bool = True, frame_timeout_s: float = 3.5):
        self._bridge = bridge
        self.available = bool(available)
        self.frame_timeout_s = frame_timeout_s
        self._frames: "queue.Queue[Snapshot]" = queue.Queue(maxsize=16)
        self._lock = threading.Lock()
        self.registered = False
        self.last = Snapshot()
        self.last_frame_at = 0.0
        self.frames_ok = 0
        self.frames_bad = 0
        self.frames_lost = 0
        self.mcu_restarts = 0
        self.restart_detected = False
        self.last_error = "" if available else "Bridge not available"
        self.info: dict = {}
        self._pending_restart = False
        if self.available:
            threading.Thread(target=self._register, name="bridge-register", daemon=True).start()

    def _register(self) -> None:
        pending = {"atmosis_frame": self._on_frame, "atmosis_info": self._on_info}
        delay = 1.0
        while pending:
            for name, handler in list(pending.items()):
                try:
                    self._bridge.provide(name, handler)
                    pending.pop(name)
                except Exception as exc:
                    self.last_error = f"register {name} failed: {exc}"
                    log("warn", f"Waiting for the board router ({exc})")
            if pending:
                time.sleep(delay)
                delay = min(delay * 2, 15.0)
        self.registered = True
        log("good", "Board link ready — waiting for sensor data")

    def _on_frame(self, *params) -> None:
        try:
            snap = parse_frame(params)
        except Exception as exc:
            with self._lock:
                self.frames_bad += 1
                self.last_error = str(exc)
            return
        with self._lock:
            if self.last.seq and snap.seq > self.last.seq + 1:
                self.frames_lost += snap.seq - self.last.seq - 1
            if 0 < snap.mcu_uptime_s < self.last.mcu_uptime_s:
                self.mcu_restarts += 1
                self._pending_restart = True
            self.last = snap
            self.last_frame_at = time.monotonic()
            self.frames_ok += 1
            self.last_error = ""
        try:
            self._frames.put_nowait(snap)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait(snap)

    def _on_info(self, *params) -> None:
        try:
            info = parse_info(params)
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
            return
        with self._lock:
            self.info = info

    def read(self) -> Snapshot:
        try:
            snap = self._frames.get(timeout=1.0)
        except queue.Empty:
            snap = None
        now = time.monotonic()
        with self._lock:
            self.restart_detected = self._pending_restart
            self._pending_restart = False
            fresh = self.last_frame_at and (now - self.last_frame_at) < self.frame_timeout_s
        if snap is not None:
            return snap
        stale = self.last.carry()
        stale.link_up = bool(fresh)
        if not fresh:
            if not self.available:
                stale.error = "Bridge not available"
            elif not self.last_frame_at:
                stale.error = "waiting for the first frame from the board"
            else:
                stale.error = f"no frame for {now - self.last_frame_at:.0f} s"
        return stale

    def close(self) -> None:
        pass

    def info_text(self) -> str:
        return ";".join(f"{k}={v}" for k, v in self.info.items())

    def stats(self) -> dict:
        with self._lock:
            total = self.frames_ok + self.frames_lost
            age = time.monotonic() - self.last_frame_at if self.last_frame_at else None
            return {
                "source": self.name, "available": self.available, "registered": self.registered,
                "link_up": bool(age is not None and age < self.frame_timeout_s),
                "frames_ok": self.frames_ok, "frames_lost": self.frames_lost, "frames_bad": self.frames_bad,
                "delivery": round(self.frames_ok / total * 100, 1) if total else 0.0,
                "last_frame_age_s": round(age, 1) if age is not None else None,
                "last_error": self.last_error, "firmware": self.info_text(), "firmware_fields": dict(self.info),
                "mcu_uptime_s": self.last.mcu_uptime_s, "mcu_restarts": self.mcu_restarts,
                "loop_max_ms": self.last.loop_max_ms,
            }


class SimulatedSource:
    name = "simulated"

    def __init__(self, seed: Optional[int] = None, interval_s: float = 1.0):
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self.interval_s = interval_s
        self.frames_ok = 0
        self.restart_detected = False

    def _event(self, t: float) -> float:
        cycle = t % 300.0
        if 150.0 <= cycle <= 230.0:
            return math.sin((cycle - 150.0) / 80.0 * math.pi)
        return 0.0

    def read(self) -> Snapshot:
        if self.interval_s:
            time.sleep(self.interval_s)
        t = time.monotonic() - self._t0
        e = self._event(t)
        n = self._rng.uniform
        self.frames_ok += 1

        def drift(amp, period, offset):
            return amp * math.sin(t / period + offset)

        dust_v = 0.40 + 0.08 + drift(0.02, 40, 1) + e * 0.8 + n(-0.008, 0.008)
        pressure = 1004.6 + drift(0.6, 400, 0) + n(-0.05, 0.05)
        return Snapshot(
            iaq=max(0.0, 38 + drift(10, 60, 0) + e * 190 + n(-2, 2)),
            tvoc_ppm=max(0.0, 0.14 + drift(0.04, 70, 1) + e * 1.3 + n(-0.01, 0.01)),
            hcho_ppm=max(0.0, 0.018 + drift(0.006, 90, 2) + e * 0.09 + n(-0.002, 0.002)),
            co_ppm=max(0.0, 0.6 + drift(0.2, 80, 0.5) + e * 3.8 + n(-0.05, 0.05)),
            temperature_c=27.5 + drift(0.8, 200, 0) + e * 0.6 + n(-0.03, 0.03),
            humidity_pct=min(100.0, max(0.0, 56 + drift(5, 150, 1.5) + e * 4 + n(-0.4, 0.4))),
            pm25_ugm3=max(0.0, (dust_v - 0.40) * 200.0),
            pressure_hpa=pressure,
            altitude_m=44330.0 * (1.0 - (pressure / 1013.25) ** 0.1903),
            sensor_temp_c=29.0 + drift(0.6, 200, 0.4),
            x6_valid=True, pressure_valid=True, dust_valid=True, dust_timing_ok=True, led_ok=True,
            dust_sensor_v=dust_v, dust_baseline_v=0.40, dust_status="ok",
            pressure_chip="DPS310", link_up=True, mcu_uptime_s=int(t), loop_max_ms=5,
            seq=self.frames_ok, timestamp=time.time(),
        )

    def close(self) -> None:
        pass

    def stats(self) -> dict:
        return {"source": self.name, "available": True, "registered": True, "link_up": True,
                "frames_ok": self.frames_ok, "frames_lost": 0, "frames_bad": 0, "delivery": 100.0,
                "last_frame_age_s": 0.0, "last_error": "",
                "firmware": "fw=1.2;x6=OK;p=DPS310;p_st=OK;p_bus=Wire (SDA/SCL);p_addr=0x77;dust=OK;leds=30",
                "firmware_fields": {"fw": "1.2", "x6": "OK", "p": "DPS310", "p_bus": "Wire (SDA/SCL)", "p_addr": "0x77",
                                    "p_st": "OK", "p_id": "0x10", "p_tries": "1",
                                    "dust": "OK", "dust_led": "HIGH", "leds": "30", "led": "ok"},
                "mcu_uptime_s": int(time.monotonic() - self._t0), "mcu_restarts": 0, "loop_max_ms": 5}


def build_source(cfg, bridge, bridge_available: bool):
    if cfg.SIMULATE_SENSORS:
        log("info", "Simulation mode — using realistic demo data")
        return SimulatedSource()
    return McuSource(bridge, bridge_available)
