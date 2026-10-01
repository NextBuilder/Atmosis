from __future__ import annotations

import json
import logging
import math
import queue
import signal
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
import difflib

from ai_advisor import Advisor
from atmosis_notify import Messages, Telegram
from atmosis_ei import AtmosisInferenceError, load_classifier
from atmosis_sources import Snapshot, build_source
from atmosis_log import log
from atmosis_store import EventLog, History
from atmosis_window import AtmosisWindow, ClassificationSmoother, Sample

VERSION = "1.2"

try:
    from flask import Flask, Response, request, send_from_directory
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

try:
    from arduino.app_utils import App, Bridge
    HAVE_BRIDGE = True
except ImportError:
    HAVE_BRIDGE = False

    class _BridgeStub:
        pass

    class _AppStub:
        @staticmethod
        def run(user_loop=None):
            while user_loop is not None and not _shutdown.is_set():
                user_loop()

    Bridge = _BridgeStub()
    App = _AppStub()

HERE = Path(__file__).resolve().parent
DASHBOARD_FILE = "atmosis-dashboard.html"
_shutdown = threading.Event()

BAND_RANK = {"offline": -1, "excellent": 0, "good": 1, "moderate": 2, "poor": 3, "hazardous": 4}

BREAKPOINTS = {
    "pm":   ([0, 12, 35, 55, 150, 250], "pm25_ugm3", "dust_valid"),
    "tvoc": ([0, 0.3, 0.5, 1.0, 3.0, 5.0], "tvoc_ppm", "x6_valid"),
    "hcho": ([0, 0.03, 0.08, 0.3, 0.75, 1.0], "hcho_ppm", "x6_valid"),
    "co":   ([0, 2, 4.5, 9, 15, 35], "co_ppm", "x6_valid"),
    "iaq":  ([0, 50, 100, 150, 250, 400], "iaq", "x6_valid"),
}
SUBSCORES = [100, 85, 65, 45, 20, 0]


def sub_score(value: float, edges) -> float:
    if value <= edges[0]:
        return 100.0
    for i in range(1, len(edges)):
        if value <= edges[i]:
            frac = (value - edges[i - 1]) / (edges[i] - edges[i - 1])
            return SUBSCORES[i - 1] + (SUBSCORES[i] - SUBSCORES[i - 1]) * frac
    return 0.0


def environment_score(reading: dict) -> tuple:
    parts = {}
    for key, (edges, field, valid) in BREAKPOINTS.items():
        if reading.get(valid):
            parts[key] = round(sub_score(float(reading.get(field, 0.0)), edges))
    if not parts:
        return 0, parts
    return max(1, int(min(parts.values()))), parts


def score_band(score: int, live: bool) -> str:
    if not live:
        return "offline"
    if score >= 80:
        return "excellent"
    if score >= 60:
        return "good"
    if score >= 40:
        return "moderate"
    if score >= 20:
        return "poor"
    return "hazardous"


def sensitivity_level(profile: dict) -> str:
    risky = profile.get("asthma") or profile.get("heart") or profile.get("pregnant")
    return "heightened" if risky or profile.get("age", 0) >= 65 else "standard"


def clean_json(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    return value


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def dashboard_url(cfg) -> str:
    if cfg.DASHBOARD_URL:
        return cfg.DASHBOARD_URL
    ip = local_ip()
    parts = ip.split(".")
    private_docker = len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31
    if ip == "localhost" or ip.startswith("127.") or private_docker:
        return ""
    return f"http://{ip}:{cfg.HTTP_PORT}/"


class Debounce:
    def __init__(self, needed: int = 3):
        self.needed = needed
        self.value: Optional[bool] = None
        self._pending = None
        self._count = 0

    def update(self, raw: bool) -> Optional[bool]:
        if self.value is None:
            self.value = raw
            return None
        if raw == self.value:
            self._pending, self._count = None, 0
            return None
        if raw != self._pending:
            self._pending, self._count = raw, 0
        self._count += 1
        if self._count >= self.needed:
            self.value, self._pending, self._count = raw, None, 0
            return raw
        return None


class Atmosis:
    def __init__(self, classifier, model_report: dict, source, cfg=config):
        self.cfg = cfg
        self.classifier = classifier
        self.model_report = model_report
        self.source = source
        self.window = AtmosisWindow(classifier.window_samples, list(classifier.axis_names))
        self.smoother = ClassificationSmoother(cfg.CLASS_VOTE_WINDOWS)
        data = Path(cfg.DATA_DIR)
        self.history = History(data / "history.json", cfg.HISTORY_FINE_STEP_S, cfg.HISTORY_FINE_SPAN_S,
                               cfg.HISTORY_COARSE_STEP_S, cfg.HISTORY_COARSE_SPAN_S)
        self.events = EventLog(data / "events.json")
        self.advisor = Advisor(cfg)
        self.tg = Telegram(cfg)
        self.msg = Messages(cfg.UTC_OFFSET_MIN, dashboard_url(cfg))
        self.profile_path = data / "profile.json"
        self.profile = dict(cfg.DEFAULT_HEALTH_PROFILE)
        try:
            self.profile.update(json.loads(self.profile_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.jobs: "queue.Queue[tuple]" = queue.Queue(maxsize=32)
        self.alerts: "queue.Queue[tuple]" = queue.Queue(maxsize=32)
        self.lock = threading.RLock()
        self.started = time.time()
        self.advisory = {"text": "", "model": "", "at": 0.0, "pending": False, "needed": False}
        self.state: dict = {}
        self._held = {"dust": None, "pressure": None, "altitude": None}
        self._x6_fail = 0
        self._flags = {k: Debounce(3) for k in ("x6", "dust", "pressure", "mcu_fs")}
        self._link = Debounce(3)
        self._stable_label: Optional[str] = None
        self._adv_at = 0.0
        self._advised_rank: Optional[int] = None
        self._pending_class, self._pending_since = None, 0.0
        self._notified_class, self._last_class_notify = None, 0.0
        self._score_alerted, self._last_score_notify = False, 0.0
        self._co_alarm, self._last_alert_at = False, 0.0
        self._online_sent = not cfg.NOTIFY_SEND_STARTUP
        self._ever_live = False
        self._down_since = 0.0
        self._link_alerted = False
        self._daily_sent_on = ""
        self._announced = False
        self._last_reminder = 0.0
        self._last_attention_alert = 0.0
        self._last_dust_alert = 0.0
        self._dust_alert_open = False
        self._followups: list = []
        self.answers: deque = deque(maxlen=5)
        self._build_state(Snapshot(), None, 0, {}, None)

    @staticmethod
    def reading_dict(s: Snapshot) -> dict:
        return {
            "iaq": s.iaq, "tvoc_ppm": s.tvoc_ppm, "hcho_ppm": s.hcho_ppm, "co_ppm": s.co_ppm,
            "temperature_c": s.temperature_c, "humidity_pct": s.humidity_pct,
            "pm25_ugm3": s.pm25_ugm3, "pressure_hpa": s.pressure_hpa, "altitude_m": s.altitude_m,
            "sensor_temp_c": s.sensor_temp_c, "x6_valid": s.x6_valid, "dust_valid": s.dust_valid,
            "pressure_valid": s.pressure_valid,
        }

    def tick(self, snap: Optional[Snapshot] = None) -> None:
        snap = snap if snap is not None else self.source.read()
        now = time.time()
        if snap.dust_valid:
            self._held["dust"] = snap.pm25_ugm3
        if snap.pressure_valid:
            self._held["pressure"] = snap.pressure_hpa
            self._held["altitude"] = snap.altitude_m
        live = snap.link_up and snap.any_valid
        sample = None
        if snap.link_up and snap.x6_valid:
            self._x6_fail = 0
            sample = Sample(iaq=snap.iaq, tvoc_ppm=snap.tvoc_ppm, hcho_ppm=snap.hcho_ppm, co_ppm=snap.co_ppm,
                            temp_c=snap.temperature_c, rh_pct=snap.humidity_pct,
                            dust_ugm3=self._held["dust"] if self._held["dust"] is not None else 0.0,
                            pressure_hpa=self._held["pressure"] if self._held["pressure"] is not None else 1013.25,
                            altitude_m=self._held["altitude"] if self._held["altitude"] is not None else 0.0,
                            timestamp=now)
            self.window.push(sample)
        else:
            self._x6_fail += 1
            if self._x6_fail >= 3:
                self.window.clear()
                self.smoother.reset()
                self._stable_label = None

        score, parts = environment_score(self.reading_dict(snap))
        result = None
        features = self.window.flatten()
        if sample is not None and features is not None:
            try:
                result = self.classifier.classify(features)
                self._stable_label = self.smoother.update(result.label)
            except AtmosisInferenceError as exc:
                log("bad", f"Air-pattern engine error: {exc}")

        self._build_state(snap, result, score, parts, self._stable_label)
        if live:
            self.history.add({"score": score, "pm": snap.pm25_ugm3 if snap.dust_valid else None,
                              "iaq": snap.iaq if snap.x6_valid else None,
                              "tvoc": snap.tvoc_ppm if snap.x6_valid else None,
                              "hcho": snap.hcho_ppm if snap.x6_valid else None,
                              "co": snap.co_ppm if snap.x6_valid else None,
                              "temp": snap.temperature_c if snap.x6_valid else None,
                              "hum": snap.humidity_pct if snap.x6_valid else None,
                              "pres": snap.pressure_hpa if snap.pressure_valid else None,
                              "alt": snap.altitude_m if snap.pressure_valid else None}, now)
        self._transitions(snap)
        self._link_watch(snap)
        self._announce_stream(snap)
        self._schedule_advisory(snap, score)
        self._notify_class(self._stable_label, score)
        self._notify_score(score, snap)
        self._check_co(snap)
        self._daily_report(live)
        self._service_followups()

    def _build_state(self, snap, result, score, parts, label) -> None:
        stats = self.source.stats()
        fw = stats.get("firmware_fields", {})
        live = snap.link_up and snap.any_valid
        with self.lock:
            self.state = {
                "version": VERSION,
                "server_time": time.time(),
                "uptime_s": round(time.time() - self.started, 1),
                "live": live,
                "reading": self.reading_dict(snap),
                "score": score if live else 0,
                "band": score_band(score, live),
                "score_parts": parts if live else {},
                "classification": {
                    "label": label or "",
                    "display": (label or "").replace("_", " "),
                    "raw_label": result.label if result else "",
                    "confidence": round(result.confidence, 4) if result else 0.0,
                    "scores": {k: round(v, 4) for k, v in (result.scores if result else {}).items()},
                    "engine": self.classifier.engine,
                    "inference_us": result.classification_us if result else 0,
                    "fill": self.window.fill_level,
                    "required": self.window.window_samples,
                    "ready": result is not None,
                },
                "model": {"labels": list(self.model_report.get("labels", []))},
                "sensors": {
                    "link": {"up": snap.link_up, "source": stats.get("source"),
                             "delivery": stats.get("delivery"), "frames_ok": stats.get("frames_ok", 0),
                             "frames_lost": stats.get("frames_lost", 0),
                             "last_frame_age_s": stats.get("last_frame_age_s"),
                             "last_error": snap.error or stats.get("last_error", ""),
                             "mcu_uptime_s": stats.get("mcu_uptime_s", 0), "restarts": stats.get("mcu_restarts", 0),
                             "loop_max_ms": stats.get("loop_max_ms", 0),
                             "firmware": fw.get("fw", ""), "raw": stats.get("firmware", "")},
                    "x6": {"ok": snap.x6_valid, "status": fw.get("x6", "OK" if snap.x6_valid else "")},
                    "dust": {"ok": snap.dust_valid, "status": snap.dust_status, "sensor_v": snap.dust_sensor_v,
                             "baseline_v": snap.dust_baseline_v,
                             "timing_ok": snap.dust_timing_ok, "led_level": fw.get("dust_led", "")},
                    "pressure": {"ok": snap.pressure_valid,
                                 "chip": snap.pressure_chip if snap.pressure_valid else ("" if fw.get("p", "none") == "none" else fw.get("p")),
                                 "bus": fw.get("p_bus", ""), "address": fw.get("p_addr", ""),
                                 "status": fw.get("p_st", ""), "id": fw.get("p_id", ""), "tries": fw.get("p_tries", "")},
                    "led": {"ok": snap.led_ok and fw.get("led", "ok") == "ok", "count": fw.get("leds", "")},
                },
                "failsafe": {"co": self._co_alarm, "dust": snap.mcu_failsafe, "board_co": snap.co_alarm},
                "timing": self.window.timing_stats(),
                "sensitivity": sensitivity_level(self.profile),
            }

    def snapshot(self) -> dict:
        with self.lock:
            state = dict(self.state)
        state["advisory"] = dict(self.advisory)
        state["ai"] = self.advisor.snapshot()
        state["answers"] = list(self.answers)
        state["notify"] = self.tg.snapshot()
        return clean_json(state)

    def _transitions(self, snap: Snapshot) -> None:
        link = self._link.update(snap.link_up)
        if link is True:
            self.events.add("good", "Board connected", "Live readings are flowing.")
        elif link is False:
            self.events.add("bad", "Board link lost", snap.error or "No frame from the microcontroller.")
        if getattr(self.source, "restart_detected", False):
            self.events.add("warn", "Board restarted", "The microcontroller rebooted; readings resumed.")
        if not snap.link_up:
            return
        names = {"x6": ("Gas sensor", snap.x6_valid), "dust": ("Dust sensor", snap.dust_valid),
                 "pressure": ("Pressure sensor", snap.pressure_valid)}
        for key, (name, ok) in names.items():
            change = self._flags[key].update(ok)
            if change is True:
                self.events.add("good", f"{name} online")
            elif change is False:
                detail = ""
                if key == "dust":
                    detail = {"no_signal": "No signal on A0 — check the AOUT wire and the module's 5 V power.",
                              "saturated": "The reading is at the top of the range."}.get(snap.dust_status, "")
                self.events.add("warn", f"{name} offline", detail)
        fs = self._flags["mcu_fs"].update(snap.mcu_failsafe)
        if fs is True:
            self.events.add("bad", "Dust alarm", f"PM2.5 reached {snap.pm25_ugm3:.0f} µg/m³.")
            if time.time() - self._last_dust_alert >= self.cfg.NOTIFY_DUST_COOLDOWN_S:
                self._last_dust_alert = time.time()
                self._dust_alert_open = True
                self._alert("dust_high", "fine dust (PM2.5) has spiked above 150 µg/m³")
        elif fs is False:
            self.events.add("good", "Dust alarm cleared")
            if self._dust_alert_open:
                self._dust_alert_open = False
                self._notify(self.msg.dust_clear(self.notify_context()))

    def _announce_stream(self, snap: Snapshot) -> None:
        if self._announced or not (snap.link_up and snap.any_valid):
            return
        self._announced = True
        parts = [("Environment X6", snap.x6_valid), ("dust sensor", snap.dust_valid), ("DPS310", snap.pressure_valid)]
        live = ", ".join(n for n, ok in parts if ok)
        waiting = ", ".join(n for n, ok in parts if not ok)
        log("good", f"Sensors streaming — {live}" + (f" (still waiting for {waiting})" if waiting else ""))

    def _link_watch(self, snap: Snapshot) -> None:
        now = time.time()
        live = snap.link_up and snap.any_valid
        if live:
            if self._link_alerted:
                self._link_alerted = False
                self._notify(self.msg.link_restored(self.notify_context()))
            self._down_since = 0.0
            self._ever_live = True
            if not self._online_sent and self.window.fill_level >= 3:
                self._online_sent = True
                self._notify(self.msg.online(self.notify_context()))
            return
        if not self._ever_live:
            return
        if not self._down_since:
            self._down_since = now
        elif not self._link_alerted and now - self._down_since >= self.cfg.NOTIFY_LINK_LOST_S:
            self._link_alerted = True
            self._notify(self.msg.link_lost(max(1, round((now - self._down_since) / 60))))

    def advisory_context(self) -> dict:
        with self.lock:
            st = self.state
            return {"reading": dict(st.get("reading", {})), "score": st.get("score", 0),
                    "classification": st.get("classification", {}).get("label") or "unknown",
                    "sensitivity": sensitivity_level(self.profile)}

    def notify_context(self) -> dict:
        with self.lock:
            st = self.state
            return {"reading": dict(st.get("reading", {})), "score": st.get("score", 0),
                    "band": st.get("band", "offline"), "live": st.get("live", False),
                    "score_parts": dict(st.get("score_parts", {})),
                    "confidence": st.get("classification", {}).get("confidence", 0.0),
                    "engine": self.classifier.engine}

    def needs_attention(self, snap: Snapshot, score: int) -> bool:
        return (score < 60 or self._co_alarm or snap.mcu_failsafe
                or self._stable_label in ("Indoor_pollution", "Poor_ventilation"))

    def _schedule_advisory(self, snap: Snapshot, score: int) -> None:
        now = time.time()
        live = snap.link_up and snap.any_valid
        needed = live and self.window.fill_level >= 3 and self.needs_attention(snap, score)
        if needed != self.advisory["needed"]:
            self.advisory["needed"] = needed
            self._advised_rank = None
        if not needed or self.advisor.provider == "offline" or self.advisory["pending"] or self.advisor.cooling_down():
            return
        rank = BAND_RANK[score_band(score, True)]
        first = self._advised_rank is None
        worse = not first and rank > self._advised_rank and (now - self._adv_at) >= self.cfg.AI_ESCALATE_GAP_S
        stale = not first and (now - self._adv_at) >= self.cfg.AI_AUTO_INTERVAL_S
        if first or worse or stale:
            self._advised_rank = rank
            self._adv_at = now
            self.advisory["pending"] = True
            ctx = self.advisory_context()
            ctx["previous"] = self.advisory["text"] if not first else ""
            self._submit("advisory", ctx)

    def refresh_advisory(self) -> bool:
        if self.advisory["pending"] or not self.advisory["needed"] or self.advisor.provider == "offline":
            return False
        self._advised_rank = None
        return True

    def _submit(self, kind: str, payload) -> None:
        target = self.alerts if kind in ("telegram", "alert") else self.jobs
        try:
            target.put_nowait((kind, payload))
        except queue.Full:
            log("warn", f"Busy — skipped one {kind} update")

    def _notify(self, text: str) -> None:
        if text and self.tg.enabled:
            self._submit("telegram", text)

    def _alert(self, builder: str, situation: str, *extra) -> None:
        if not self.tg.enabled:
            return
        if builder in ("poor_air", "pattern", "still_poor"):
            now = time.time()
            if now - self._last_attention_alert < self.cfg.NOTIFY_MIN_GAP_S:
                return
            self._last_attention_alert = now
        self._submit("alert", (builder, situation, self.notify_context(), extra))

    def queue_followup(self, question: str) -> str:
        fid = f"q{int(time.time() * 1000)}"
        self._followups.append({"id": fid, "question": question, "created": time.time(), "next": time.time() + 20.0,
                                "tries": 0, "busy": False})
        return fid

    def _service_followups(self) -> None:
        now = time.time()
        for item in list(self._followups):
            if now - item["created"] > self.cfg.AI_FOLLOWUP_WINDOW_S and not item["busy"]:
                self._followups.remove(item)
                self.answers.append({"id": item["id"], "question": item["question"], "text": None,
                                     "failed": True, "at": now})
            elif not item["busy"] and now >= item["next"]:
                item["busy"] = True
                self._submit("followup", item)

    def worker(self, inbox: "queue.Queue[tuple]" = None) -> None:
        inbox = inbox if inbox is not None else self.jobs
        while not _shutdown.is_set():
            try:
                kind, payload = inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if kind == "advisory":
                    text, source = self.advisor.ask(payload)
                    if source == "gemini":
                        same = difflib.SequenceMatcher(None, text, self.advisory["text"]).ratio() > 0.85
                        if not same:
                            self.advisory.update(text=text, model=self.advisor.snapshot()["model"], at=time.time())
                    elif not self.advisory["text"]:
                        self._advised_rank = None
                    self.advisory["pending"] = False
                elif kind == "alert":
                    builder, situation, ctx, extra = payload
                    tip = None if builder == "co_alert" else self.advisor.tip(ctx, situation)
                    self.tg.send(getattr(self.msg, builder)(ctx, *extra, tip=tip))
                elif kind == "followup":
                    text, source = self.advisor.ask(self.advisory_context(), payload["question"])
                    if source == "off":
                        source = "busy"
                    payload["tries"] += 1
                    payload["busy"] = False
                    if source == "gemini":
                        self.answers.append({"id": payload["id"], "question": payload["question"],
                                             "text": text, "at": time.time()})
                        if payload in self._followups:
                            self._followups.remove(payload)
                    else:
                        payload["next"] = time.time() + 30.0 * payload["tries"]
                elif kind == "telegram":
                    self.tg.send(payload)
            except Exception as exc:
                log("bad", f"{kind.capitalize()} failed: {exc}")
                if kind == "advisory":
                    self.advisory["pending"] = False

    def _notify_class(self, label: Optional[str], score: int) -> None:
        if not label:
            return
        now = time.time()
        if label != self._pending_class:
            self._pending_class, self._pending_since = label, now
            return
        if label == self._notified_class or (now - self._pending_since) < self.cfg.NOTIFY_CLASS_DWELL_S:
            return
        self._notified_class = label
        self.events.add("good" if label == "Clean_air" else "warn", f"Air pattern: {label.replace('_', ' ')}",
                        f"Air score {score}.")
        if label != "Clean_air" and (now - self._last_class_notify) >= self.cfg.NOTIFY_CLASS_COOLDOWN_S:
            self._last_class_notify = now
            self._alert("pattern", f"an air pattern was detected: {label.replace('_', ' ').lower()}", label)

    def _notify_score(self, score: int, snap: Snapshot) -> None:
        if not (snap.link_up and snap.any_valid):
            return
        now = time.time()
        if self._score_alerted:
            if score >= self.cfg.NOTIFY_SCORE_RECOVERY:
                if (now - self._last_score_notify) >= self.cfg.NOTIFY_SCORE_RECOVERY_COOLDOWN_S:
                    self._score_alerted, self._last_score_notify = False, now
                    self.events.add("good", "Air quality recovered", f"Score {score}.")
                    self._notify(self.msg.recovered(self.notify_context()))
            elif (now - self._last_reminder) >= self.cfg.NOTIFY_REMINDER_S:
                self._last_reminder = now
                self._alert("still_poor", f"the air has needed attention for over an hour (score {score}/100)")
        elif score <= self.cfg.NOTIFY_SCORE_ALERT and (now - self._last_score_notify) >= self.cfg.NOTIFY_SCORE_COOLDOWN_S:
            self._score_alerted, self._last_score_notify = True, now
            self._last_reminder = now
            self.events.add("bad", "Poor air quality", f"Score dropped to {score}.")
            self._alert("poor_air", f"the air score dropped to {score}/100")

    def _check_co(self, snap: Snapshot) -> None:
        if not (snap.link_up and snap.x6_valid):
            return
        now = time.time()
        if snap.co_ppm > self.cfg.FAILSAFE_CO_PPM:
            if not self._co_alarm or (now - self._last_alert_at) > self.cfg.ALERT_COOLDOWN_S:
                self.events.add("bad", "Carbon monoxide alert", f"CO {snap.co_ppm:.1f} ppm")
                self._alert("co_alert", f"carbon monoxide is {snap.co_ppm:.1f} ppm, above the 9 ppm safety limit")
                self._last_alert_at = now
            self._co_alarm = True
        elif self._co_alarm and snap.co_ppm <= self.cfg.FAILSAFE_CO_PPM - 1.0:
            self._co_alarm = False
            self.events.add("good", "Carbon monoxide back to safe")
            self._notify(self.msg.co_clear(self.notify_context()))

    def daily_stats(self) -> dict:
        pts = self.history.query(86400)["points"]

        def col(key):
            return [p[key] for p in pts if isinstance(p.get(key), (int, float))]

        out = {}
        for key in ("score", "pm", "co", "tvoc", "temp", "hum", "pres"):
            vals = col(key)
            if vals:
                out[f"{key}_avg"] = sum(vals) / len(vals)
                out[f"{key}_max"] = max(vals)
                out[f"{key}_min"] = min(vals)
        scored = [p for p in pts if isinstance(p.get("score"), (int, float))]
        if scored:
            worst = min(scored, key=lambda p: p["score"])
            if worst["score"] < 60:
                out["worst_at"] = datetime.fromtimestamp(worst["t"], self.msg.tz).strftime("%I:%M %p").lstrip("0")
            step = self.history.coarse_step
            out["minutes_poor"] = int(sum(step for p in scored if p["score"] < 40) / 60)
        return out

    def _daily_report(self, live: bool) -> None:
        if self.cfg.NOTIFY_DAILY_HOUR < 0 or not self.tg.enabled or not live:
            return
        now = self.msg.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour != self.cfg.NOTIFY_DAILY_HOUR or self._daily_sent_on == today:
            return
        self._daily_sent_on = today
        if time.time() - self.started < 3600:
            return
        self._notify(self.msg.daily(self.daily_stats(), self.notify_context()))

    def run_sampler(self) -> None:
        while not _shutdown.is_set():
            try:
                self.tick(self.source.read())
            except Exception as exc:
                log("bad", f"Reading skipped: {type(exc).__name__}: {exc}")
                _shutdown.wait(1.0)

    def save_profile(self, body: dict) -> dict:
        updated = dict(self.profile)
        if "age" in body:
            updated["age"] = max(0, min(120, int(body["age"])))
        for key in ("asthma", "heart", "pregnant"):
            if key in body:
                updated[key] = bool(body[key])
        self.profile = updated
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            self.profile_path.write_text(json.dumps(updated), encoding="utf-8")
        except OSError:
            pass
        return dict(updated)

    def close(self) -> None:
        self.history.save()
        self.events.flush(force=True)
        self.source.close()


RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}


def build_flask_app(core: Atmosis):
    if not HAVE_FLASK:
        return None
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        import flask.cli
        flask.cli.show_server_banner = lambda *args, **kwargs: None
    except Exception:
        pass
    app = Flask(__name__, static_folder=None)

    def js(obj, status: int = 200):
        return Response(json.dumps(clean_json(obj), allow_nan=False), status=status, mimetype="application/json")

    @app.after_request
    def headers(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/")
    def index():
        if not (HERE / DASHBOARD_FILE).is_file():
            return f"<h1>Atmosis</h1><p>{DASHBOARD_FILE} is missing. The API is at /api/status.</p>"
        return send_from_directory(HERE, DASHBOARD_FILE)

    @app.route("/api/ping")
    def ping():
        return js({"ok": True, "version": VERSION, "uptime_s": round(time.time() - core.started, 1)})

    @app.route("/api/status")
    def status():
        return js(core.snapshot())

    @app.route("/api/history")
    def history():
        span = RANGES.get(request.args.get("range", "1h"), 3600)
        return js(core.history.query(span))

    @app.route("/api/events")
    def events():
        return js({"events": core.events.list(100)})

    @app.route("/api/profile", methods=["GET", "POST"])
    def profile():
        if request.method == "GET":
            return js(core.profile)
        try:
            return js(core.save_profile(request.get_json(silent=True) or {}))
        except (TypeError, ValueError):
            return js({"error": "age must be a whole number"}, 400)

    @app.route("/api/ask", methods=["POST", "OPTIONS"])
    def ask():
        if request.method == "OPTIONS":
            return js({})
        question = str((request.get_json(silent=True) or {}).get("question", "")).strip()
        if not question:
            return js({"error": "Type a question first."}, 400)
        if len(question) > 500:
            return js({"error": "Keep questions under 500 characters."}, 400)
        if core.advisor.provider == "offline":
            return js({"error": "Gemini isn't set up yet — add GEMINI_API_KEY to python/.env and restart the app."}, 503)
        answer, source = core.advisor.ask(core.advisory_context(), question)
        if source == "gemini":
            return js({"answer": answer, "source": "gemini", "followup": ""})
        return js({"answer": None, "source": "busy", "followup": core.queue_followup(question)})

    @app.route("/api/advisory/refresh", methods=["POST"])
    def refresh():
        return js({"queued": core.refresh_advisory()}, 202)

    @app.route("/api/test_notify", methods=["POST"])
    def test_notify():
        if not core.cfg.TELEGRAM_ENABLED:
            return js({"ok": False, "error": "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to python/.env, then restart."}, 400)
        ok = core.tg.send(core.msg.test(core.notify_context()))
        return js({"ok": ok, "error": core.tg.snapshot()["last_error"]}, 200 if ok else 502)

    return app


def run_flask(app) -> None:
    deadline = time.monotonic() + 120.0
    announced = False
    while not _shutdown.is_set():
        try:
            app.run(host=config.HTTP_HOST, port=config.HTTP_PORT, threaded=True, debug=False, use_reloader=False)
            return
        except OSError as exc:
            if time.monotonic() < deadline and ("in use" in str(exc).lower() or getattr(exc, "errno", 0) == 98):
                if not announced:
                    log("warn", f"Port {config.HTTP_PORT} is busy — retrying for 2 minutes")
                    announced = True
                _shutdown.wait(5.0)
                continue
            log("bad", f"Dashboard server stopped: {exc}")
            return


def engine_label(classifier) -> str:
    return "Edge Impulse model" if classifier.engine == "edge-impulse" else "Atmosis pattern engine"


def banner(core) -> None:
    cfg = core.cfg
    advisor = core.advisor.snapshot()
    rows = [
        ("Dashboard", f"http://localhost:{cfg.HTTP_PORT}/"),
        ("Sensors", "Environment X6, dust sensor, DPS310, RGB strip"),
        ("Air patterns", engine_label(core.classifier)),
        ("Advisor", "Gemini 3.5 Flash — recommends when your air needs attention" if advisor["enabled"]
         else "Gemini off (add GEMINI_API_KEY to python/.env)"),
        ("Alerts", "Telegram ready" if cfg.TELEGRAM_ENABLED else "Telegram off (add your bot token to python/.env)"),
    ]
    line = "  " + "─" * 58
    print(f"\n{line}\n   ATMOSIS {VERSION}   ·   Indoor air intelligence\n{line}", flush=True)
    for k, v in rows:
        print(f"   {k:<14}{v}", flush=True)
    print(f"{line}\n", flush=True)


def main() -> None:
    classifier, report = load_classifier(config.MODEL_DIRS, config.USE_UNTRAINED_MODEL)
    source = build_source(config, Bridge, HAVE_BRIDGE)
    core = Atmosis(classifier, report, source)
    banner(core)

    def on_signal(signum, frame):
        _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    threading.Thread(target=core.worker, args=(core.jobs,), name="advisor", daemon=True).start()
    threading.Thread(target=core.worker, args=(core.alerts,), name="alerts", daemon=True).start()
    threading.Thread(target=core.run_sampler, name="sampler", daemon=True).start()
    app = build_flask_app(core)
    if app is not None:
        threading.Thread(target=run_flask, args=(app,), name="http", daemon=True).start()
        log("good", f"Dashboard live at http://localhost:{config.HTTP_PORT}/")

    def idle():
        _shutdown.wait(1.0)

    try:
        App.run(user_loop=idle)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown.set()
        core.close()
        log("info", "Atmosis stopped")


if __name__ == "__main__":
    main()
