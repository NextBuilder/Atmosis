from __future__ import annotations

import html
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from atmosis_log import log

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    requests = None
    HAVE_REQUESTS = False

BAND_WORD = {"excellent": "Excellent", "good": "Good", "moderate": "Moderate",
             "poor": "Poor", "hazardous": "Hazardous", "offline": "Offline"}
BAND_DOT = {"excellent": "🟢", "good": "🟢", "moderate": "🟡", "poor": "🟠", "hazardous": "🔴", "offline": "⚪"}
CAUSE = {"pm": "fine dust (PM2.5)", "tvoc": "chemical vapours (VOCs)", "hcho": "formaldehyde",
         "co": "carbon monoxide", "iaq": "overall air load"}
LEVELS = ("Good", "Fair", "Elevated", "High", "Very high")
EDGES = {"pm": (12, 35, 55, 150), "co": (2, 4.5, 9, 15), "tvoc": (0.3, 0.5, 1, 3),
         "hcho": (0.03, 0.08, 0.3, 0.75), "iaq": (50, 100, 150, 250)}


def _e(value) -> str:
    return html.escape(str(value), quote=False)


def level(key: str, value: float) -> str:
    i = 0
    for edge in EDGES[key]:
        if value > edge:
            i += 1
    return LEVELS[i]


def comfort_temp(t: float) -> str:
    return "Cool" if t < 18 else "Comfortable" if t <= 27 else "Warm" if t <= 32 else "Hot" if t <= 38 else "Very hot"


def comfort_rh(rh: float) -> str:
    return "Dry" if rh < 30 else "Ideal" if rh <= 60 else "Humid" if rh <= 75 else "Very humid"


def pressure_word(hpa: float) -> str:
    return "Low" if hpa < 985 else "Normal" if hpa < 1025 else "High"


def altitude_word(m: float) -> str:
    return "Low" if m < 300 else "Moderate" if m < 1500 else "High" if m < 2500 else "Very high"


def score_bar(score: int) -> str:
    filled = max(0, min(10, round(score / 10)))
    return "▰" * filled + "▱" * (10 - filled)


def _table(rows) -> str:
    rows = [r for r in rows if r]
    if not rows:
        return ""
    w0 = max(len(r[0]) for r in rows) + 2
    w1 = max(len(r[1]) for r in rows) + 2
    return "<pre>" + "\n".join(_e((r[0].ljust(w0) + r[1].ljust(w1) + r[2]).rstrip()) for r in rows) + "</pre>"


class Messages:
    def __init__(self, utc_offset_min: int = 330, dashboard_url: str = ""):
        self.tz = timezone(timedelta(minutes=utc_offset_min))
        self.dashboard_url = dashboard_url

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _stamp(self) -> str:
        n = self.now()
        return f"{n.day} {n.strftime('%b')} · {n.strftime('%I:%M %p').lstrip('0')}"

    def _footer(self) -> str:
        link = f"  ·  <a href=\"{_e(self.dashboard_url)}\">Open dashboard</a>" if self.dashboard_url else ""
        return f"\n<i>Atmosis · {self._stamp()}</i>{link}"

    @staticmethod
    def _score_line(ctx: dict) -> str:
        if not ctx.get("live", True):
            return ""
        score = int(ctx.get("score", 0))
        band = ctx.get("band", "")
        return f"\n{BAND_DOT.get(band, '⚪')} Air score <b>{score}</b>/100 · {_e(BAND_WORD.get(band, ''))}\n<code>{score_bar(score)}</code>\n"

    @staticmethod
    def air_table(r: dict) -> str:
        rows = []
        if r.get("dust_valid"):
            v = r.get("pm25_ugm3", 0)
            rows.append(("PM2.5", f"{v:.0f} µg/m³", level("pm", v)))
        if r.get("x6_valid"):
            rows += [
                ("CO", f"{r.get('co_ppm', 0):.2f} ppm", level("co", r.get("co_ppm", 0))),
                ("VOCs", f"{r.get('tvoc_ppm', 0):.3f} ppm", level("tvoc", r.get("tvoc_ppm", 0))),
                ("HCHO", f"{r.get('hcho_ppm', 0):.3f} ppm", level("hcho", r.get("hcho_ppm", 0))),
                ("IAQ", f"{r.get('iaq', 0):.0f}", level("iaq", r.get("iaq", 0))),
            ]
        return _table(rows)

    @staticmethod
    def climate_table(r: dict) -> str:
        rows = []
        if r.get("x6_valid"):
            t, rh = r.get("temperature_c", 0), r.get("humidity_pct", 0)
            rows += [("Temperature", f"{t:.1f} °C", comfort_temp(t)), ("Humidity", f"{rh:.0f} %", comfort_rh(rh))]
        if r.get("pressure_valid"):
            p, a = r.get("pressure_hpa", 0), r.get("altitude_m", 0)
            rows += [("Pressure", f"{p:.1f} hPa", pressure_word(p)), ("Altitude", f"{a:.0f} m", altitude_word(a))]
        return _table(rows)

    def readings(self, ctx: dict) -> str:
        r = ctx.get("reading", {})
        parts = []
        air, climate = self.air_table(r), self.climate_table(r)
        if air:
            parts.append(f"<b>Air quality</b>\n{air}")
        if climate:
            parts.append(f"<b>Climate</b>\n{climate}")
        return "\n".join(parts)

    @staticmethod
    def main_cause(ctx: dict) -> str:
        parts = ctx.get("score_parts") or {}
        if not parts:
            return ""
        key, value = min(parts.items(), key=lambda kv: kv[1])
        return CAUSE.get(key, "") if value < 85 else ""

    def _card(self, icon: str, title: str, summary: str, ctx: dict, action: str = "", score: bool = True,
              tip: str | None = None) -> str:
        body = f"{icon} <b>{_e(title)}</b>\n{_e(summary)}\n"
        if score:
            body += self._score_line(ctx)
        readings = self.readings(ctx)
        if readings:
            body += f"\n{readings}\n"
        if action:
            body += f"\n<b>What to do</b>\n{_e(action)}\n"
        if tip:
            body += f"\n<b>✦ Gemini suggests</b>\n<blockquote>{_e(tip)}</blockquote>\n"
        return body + self._footer()

    @staticmethod
    def engine_line(ctx: dict) -> str:
        return ("Air patterns are recognised by the Edge Impulse model."
                if ctx.get("engine") == "edge-impulse" else "Air patterns are tracked by the Atmosis engine.")

    def online(self, ctx: dict) -> str:
        return self._card("✨", "Atmosis is online",
                          f"Monitoring has started. {self.engine_line(ctx)} You'll hear from me only when your air needs attention.", ctx)

    def poor_air(self, ctx: dict, tip: str | None = None) -> str:
        cause = self.main_cause(ctx)
        lead = f"The main cause is {cause}." if cause else "Several pollutants have risen together."
        return self._card("🟠", "Air quality needs attention", lead, ctx,
                          "Open a window if the air outside is cleaner, or run an air purifier until the score is back above 55.",
                          tip=tip)

    def still_poor(self, ctx: dict, tip: str | None = None) -> str:
        cause = self.main_cause(ctx)
        lead = "The air has needed attention for a while now." + (f" The main cause is still {cause}." if cause else "")
        return self._card("⏳", "Air still needs attention", lead, ctx,
                          "Keep ventilating or run a purifier — this reminder repeats at most once an hour.", tip=tip)

    def recovered(self, ctx: dict) -> str:
        return self._card("✅", "Air quality has recovered", "Everything is back in the healthy range. No action needed.", ctx)

    def dust_high(self, ctx: dict, tip: str | None = None) -> str:
        pm = ctx.get("reading", {}).get("pm25_ugm3", 0)
        return self._card("🚨", "Very high dust level",
                          f"PM2.5 reached {pm:.0f} µg/m³ — ten times the WHO daily guideline. The light strip is pulsing red.", ctx,
                          "Stop the source (smoke, incense, frying), ventilate, and wear an N95 mask if you must stay in the room.",
                          tip=tip)

    def dust_clear(self, ctx: dict) -> str:
        pm = ctx.get("reading", {}).get("pm25_ugm3", 0)
        return self._card("✅", "Dust level back to safe", f"PM2.5 has fallen to {pm:.0f} µg/m³ and the dust alarm is off.", ctx)

    def co_alert(self, ctx: dict, tip: str | None = None) -> str:
        co = ctx.get("reading", {}).get("co_ppm", 0)
        return self._card("🚨", "Carbon monoxide alert",
                          f"CO is {co:.1f} ppm, above the 9 ppm safety limit. Carbon monoxide has no smell.", ctx,
                          "Turn off gas stoves and heaters, open windows and doors, and leave the room. Get medical help if anyone has a headache, dizziness or nausea.",
                          tip=tip)

    def co_clear(self, ctx: dict) -> str:
        co = ctx.get("reading", {}).get("co_ppm", 0)
        return self._card("✅", "Carbon monoxide back to safe", f"CO has fallen to {co:.1f} ppm.", ctx)

    def pattern(self, ctx: dict, label: str, tip: str | None = None) -> str:
        text = {
            "Indoor_pollution": ("🟠", "Indoor pollution detected",
                                 "A pollution pattern is building — cooking fumes, smoke or chemical vapours.",
                                 "Ventilate the room and find the source."),
            "Poor_ventilation": ("🟡", "This room needs fresh air",
                                 "The air is turning stale and poorly ventilated.",
                                 "Open a window or switch on a fan for ten minutes."),
        }.get(label)
        if not text:
            return ""
        icon, title, summary, action = text
        conf = ctx.get("confidence")
        if isinstance(conf, (int, float)) and conf:
            by = "Edge Impulse model" if ctx.get("engine") == "edge-impulse" else "Atmosis engine"
            summary += f" {by} confidence {conf * 100:.0f}%."
        return self._card(icon, title, summary, ctx, action, tip=tip)

    def link_lost(self, minutes: int) -> str:
        return (f"⚠️ <b>Sensors are not responding</b>\n"
                f"No readings from the sensor board for {minutes} minute{'s' if minutes != 1 else ''}. "
                f"Alerts are paused until it reconnects.\n\n"
                f"<b>If this continues</b>\nIn App Lab press Stop, then Run. Check the board still has power.\n"
                f"{self._footer()}")

    def link_restored(self, ctx: dict) -> str:
        return self._card("🟢", "Sensors reconnected", "Live monitoring has resumed.", ctx)

    def test(self, ctx: dict) -> str:
        if not ctx.get("live"):
            return self._card("🔔", "Test notification", "Alerts will arrive here. The sensors are not reporting yet.", ctx, score=False)
        return self._card("🔔", "Test notification", "Alerts from Atmosis will arrive in this chat. Here's your air right now.", ctx)

    def daily(self, stats: dict, ctx: dict) -> str:
        n = self.now()
        head = f"🌙 <b>Daily air report</b>\n{_e(n.strftime('%A'))}, {n.day} {_e(n.strftime('%B'))}\n"
        avg = stats.get("score_avg")
        if avg is not None:
            band = ("excellent" if avg >= 80 else "good" if avg >= 60 else "moderate" if avg >= 40
                    else "poor" if avg >= 20 else "hazardous")
            head += f"\n{BAND_DOT[band]} Average air score <b>{avg:.0f}</b>/100 · {BAND_WORD[band]}\n<code>{score_bar(int(avg))}</code>\n"
        rows = []
        if stats.get("pm_avg") is not None:
            rows.append(("PM2.5", f"{stats['pm_avg']:.0f} avg", f"{stats['pm_max']:.0f} peak µg/m³"))
        if stats.get("co_max") is not None:
            rows.append(("CO", f"{stats['co_avg']:.2f} avg", f"{stats['co_max']:.2f} peak ppm"))
        if stats.get("tvoc_max") is not None:
            rows.append(("VOCs", f"{stats['tvoc_avg']:.3f} avg", f"{stats['tvoc_max']:.3f} peak ppm"))
        if stats.get("temp_min") is not None:
            rows.append(("Temperature", f"{stats['temp_min']:.1f}–{stats['temp_max']:.1f}", "°C"))
        if stats.get("hum_avg") is not None:
            rows.append(("Humidity", f"{stats['hum_avg']:.0f} avg", "%"))
        if stats.get("pres_avg") is not None:
            rows.append(("Pressure", f"{stats['pres_avg']:.1f} avg", "hPa"))
        body = head
        if rows:
            body += f"\n<b>Last 24 hours</b>\n{_table(rows)}\n"
        if stats.get("worst_at"):
            body += f"\nAir was at its worst around <b>{_e(stats['worst_at'])}</b>.\n"
        if stats.get("minutes_poor"):
            body += f"Air was poor for about {stats['minutes_poor']} minutes today.\n"
        elif avg is not None:
            body += "Air stayed healthy all day.\n"
        return body + self._footer()


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class Telegram:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = bool(cfg.TELEGRAM_ENABLED)
        self._lock = threading.Lock()
        self._last_sent = 0.0
        self.status = {"enabled": self.enabled, "sent": 0, "last_ok": 0.0, "last_error": ""}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.status)

    def _fail(self, message: str) -> bool:
        with self._lock:
            self.status["last_error"] = message
        log("warn", f"Telegram: {message}")
        return False

    def _post(self, payload: dict):
        return requests.post(f"https://api.telegram.org/bot{self.cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                             json=payload, timeout=self.cfg.TELEGRAM_TIMEOUT_S)

    def send(self, text: str) -> bool:
        if not text:
            return False
        if not self.enabled:
            return self._fail("not configured")
        if not HAVE_REQUESTS:
            return self._fail("the 'requests' package is not installed")
        wait = 1.1 - (time.monotonic() - self._last_sent)
        if wait > 0:
            time.sleep(wait)
        self._last_sent = time.monotonic()
        payload = {"chat_id": self.cfg.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        try:
            resp = self._post(payload)
            if resp.status_code == 400 and "parse" in resp.text.lower():
                resp = self._post({"chat_id": self.cfg.TELEGRAM_CHAT_ID, "text": strip_html(text)})
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")
        if resp.status_code != 200:
            try:
                desc = resp.json().get("description", resp.text[:120])
            except ValueError:
                desc = resp.text[:120]
            if resp.status_code == 401:
                desc += " — the bot token was rejected"
            elif "chat not found" in str(desc).lower():
                desc += " — open your bot in Telegram and press Start"
            return self._fail(f"HTTP {resp.status_code}: {desc}")
        with self._lock:
            self.status.update(sent=self.status["sent"] + 1, last_ok=time.time(), last_error="")
        return True
