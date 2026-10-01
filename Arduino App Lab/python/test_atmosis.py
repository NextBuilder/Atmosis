import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ATMOSIS_DATA_DIR"] = tempfile.mkdtemp()
os.environ["ATMOSIS_AI_PROVIDER"] = "off"
os.environ["TELEGRAM_BOT_TOKEN"] = ""

import config
import ai_advisor
import atmosis_ei
import atmosis_notify
import main
from atmosis_sources import McuSource, Snapshot, SimulatedSource, parse_frame
from atmosis_store import EventLog, History
from atmosis_window import AXIS_ORDER

def frame(**kw):
    base = dict(proto=5, seq=1, uptime=10, flags=0x4F, iaq=42.5, tvoc=0.12, hcho=0.02, co=0.8, temp=27.3, rh=55.1,
                pm=60.8, dust_mv=900, base_mv=400, dust_status=1, pres=1004.8, alt=70.5, ptemp=29.1, chip=3, loop=5)
    base.update(kw)
    return [base[k] for k in ("proto", "seq", "uptime", "flags", "iaq", "tvoc", "hcho", "co", "temp", "rh", "pm",
                              "dust_mv", "base_mv", "dust_status", "pres", "alt", "ptemp", "chip", "loop")]


class FakeBridge:
    def __init__(self, fail_register=0):
        self.handlers = {}
        self.notified = []
        self.fail_register = fail_register

    def provide(self, name, handler):
        if self.fail_register > 0:
            self.fail_register -= 1
            raise RuntimeError("router not ready")
        self.handlers[name] = handler

    def notify(self, name, *args):
        self.notified.append((name, args))

    def push(self, params):
        self.handlers["atmosis_frame"](*params)


def wait_registered(src, timeout=5.0):
    t0 = time.time()
    while not src.registered and time.time() - t0 < timeout:
        time.sleep(0.01)
    return src.registered


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class ScriptedSource:
    name = "scripted"

    def __init__(self):
        self.snap = Snapshot()
        self.restart_detected = False

    def set(self, **kw):
        base = dict(iaq=40, tvoc_ppm=0.1, hcho_ppm=0.01, co_ppm=0.5, temperature_c=26, humidity_pct=50,
                    pm25_ugm3=10, pressure_hpa=1005, altitude_m=70, sensor_temp_c=29, x6_valid=True, dust_valid=True,
                    pressure_valid=True, link_up=True, dust_status="ok", pressure_chip="DPS310")
        base.update(kw)
        self.snap = Snapshot(**base)

    def read(self):
        return self.snap

    def close(self):
        pass

    def stats(self):
        return {"source": self.name, "delivery": 100.0, "firmware_fields": {"leds": "30", "led": "ok"}, "mcu_uptime_s": 5}


def make_core(cfg_overrides=None):
    cfg = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    cfg.DATA_DIR = Path(tempfile.mkdtemp())
    cfg.TELEGRAM_ENABLED = True
    cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID = "1:abc", "42"
    cfg.NOTIFY_SEND_STARTUP = True
    cfg.ACTIVE_AI = "offline"
    for k, v in (cfg_overrides or {}).items():
        setattr(cfg, k, v)
    clf, report = atmosis_ei.load_classifier(config.MODEL_DIRS)
    src = ScriptedSource()
    core = main.Atmosis(clf, report, src, cfg)
    return core, src


def core_hour():
    return atmosis_notify.Messages(config.UTC_OFFSET_MIN).now().hour


def queued_messages(core):
    out = []
    while not core.alerts.empty():
        kind, payload = core.alerts.get_nowait()
        if kind == "telegram":
            out.append(payload)
        elif kind == "alert":
            builder, _, ctx, extra = payload
            out.append(getattr(core.msg, builder)(ctx, *extra, tip=None))
    return out


class FrameTests(unittest.TestCase):
    def test_parses_every_field(self):
        s = parse_frame(frame())
        self.assertTrue(s.x6_valid and s.pressure_valid and s.dust_valid)
        self.assertAlmostEqual(s.pressure_hpa, 1004.8)
        self.assertAlmostEqual(s.altitude_m, 70.5)
        self.assertAlmostEqual(s.sensor_temp_c, 29.1)
        self.assertEqual(s.pressure_chip, "DPS310")
        self.assertEqual(s.dust_status, "ok")
        self.assertAlmostEqual(s.dust_sensor_v, 0.9)

    def test_bad_frames_rejected(self):
        for bad in ([], frame()[:-1], frame(proto=2), frame(iaq=float("nan"))):
            with self.assertRaises(ValueError):
                parse_frame(bad)

    def test_out_of_range_gas_marks_x6_invalid(self):
        self.assertFalse(parse_frame(frame(rh=140.0)).x6_valid)


class BoardLinkTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.src = McuSource(self.bridge, True, frame_timeout_s=0.5)
        self.assertTrue(wait_registered(self.src))

    def test_registers_retrying_until_router_ready(self):
        bridge = FakeBridge(fail_register=2)
        with mock.patch("atmosis_sources.time.sleep"):
            src = McuSource(bridge, True)
            self.assertTrue(wait_registered(src))
        self.assertIn("atmosis_frame", bridge.handlers)
        self.assertIn("atmosis_info", bridge.handlers)

    def test_pushed_frame_is_read_and_nothing_is_sent_to_the_board(self):
        self.bridge.push(frame(seq=1))
        snap = self.src.read()
        self.assertTrue(snap.link_up and snap.x6_valid)
        for _ in range(3):
            self.src.read()
        self.assertEqual(self.bridge.notified, [])

    def test_link_down_after_silence(self):
        self.bridge.push(frame(seq=1))
        self.src.read()
        time.sleep(0.6)
        snap = self.src.read()
        self.assertFalse(snap.link_up)
        self.assertAlmostEqual(snap.iaq, 42.5)

    def test_counts_lost_frames_and_restarts(self):
        self.bridge.push(frame(seq=1, uptime=100))
        self.bridge.push(frame(seq=4, uptime=103))
        self.bridge.push(frame(seq=1, uptime=2))
        self.src.read()
        self.assertEqual(self.src.frames_lost, 2)
        self.assertEqual(self.src.mcu_restarts, 1)
        self.assertTrue(self.src.restart_detected)

    def test_bad_frame_is_counted_not_raised(self):
        self.bridge.handlers["atmosis_frame"](*frame(proto=9))
        self.assertEqual(self.src.frames_bad, 1)
        self.assertIn("re-upload", self.src.last_error)

    def test_info_is_parsed(self):
        self.bridge.handlers["atmosis_info"](5, 12, 300, 2, 1, 1, 3, 0x77, 0x10, 1, 1, 631, 1, 1, 30)
        f = self.src.stats()["firmware_fields"]
        self.assertEqual(f["fw"], "1.2")
        self.assertEqual(f["p_st"], "OK")
        self.assertEqual((f["p"], f["p_addr"], f["p_id"]), ("DPS310", "0x77", "0x10"))
        self.assertEqual(f["leds"], "30")
        self.assertIn("p_st=OK", self.src.stats()["firmware"])

    def test_bad_info_is_ignored(self):
        self.bridge.handlers["atmosis_info"](4, 12)
        self.assertEqual(self.src.stats()["firmware_fields"], {})


class ModelTests(unittest.TestCase):
    def test_model_that_cannot_separate_situations_is_not_used(self):
        clf, report = atmosis_ei.load_classifier(config.MODEL_DIRS)
        self.assertEqual(clf.engine, "rules")
        self.assertTrue(report["health"]["degenerate"])
        self.assertNotIn("retrain", report["message"].lower())

    def test_parser_reads_architecture(self):
        spec = atmosis_ei.parse_eon_model(*atmosis_ei.find_model_files(config.MODEL_DIRS))
        dense = [l for l in spec["layers"] if l["type"] == "dense"]
        self.assertEqual([len(l["w"]) for l in dense], [20, 10, 3])
        self.assertEqual(spec["labels"], ["Clean_air", "Indoor_pollution", "Poor_ventilation"])
        self.assertEqual(spec["window_samples"], 15)

    def test_trained_normalized_model_is_used(self):
        axes = [a + "_n" for a in AXIS_ORDER]
        w = [[0.0] * 135 for _ in range(3)]
        for s in range(15):
            w[1][s * 9 + 6] = 4.0
            w[0][s * 9 + 6] = -4.0
        spec = {"project": "t", "labels": atmosis_ei.LABELS_DEFAULT, "axes": axes, "window_samples": 15,
                "axis_count": 9, "frequency_hz": 5.0, "scale_axes": 1.0, "path": "",
                "layers": [{"type": "dense", "w": w, "b": [2.0, 0.0, 0.0], "act": "None"}, {"type": "softmax"}]}
        m = atmosis_ei.EdgeImpulseModel(spec)
        self.assertTrue(m.normalized)
        clean = [40, .1, .01, .5, 26, 50, 5, 1005, 70] * 15
        smoky = [40, .1, .01, .5, 26, 50, 300, 1005, 70] * 15
        self.assertEqual(m.classify(clean).label, "Clean_air")
        self.assertEqual(m.classify(smoky).label, "Indoor_pollution")
        self.assertFalse(m.health()["untrained"])

    def test_rules_separate_probe_situations(self):
        r = atmosis_ei.RuleBasedClassifier()
        self.assertEqual(r.classify(list(atmosis_ei.PROBES["pristine"]) * 15).label, "Clean_air")
        self.assertEqual(r.classify(list(atmosis_ei.PROBES["smoke"]) * 15).label, "Indoor_pollution")
        self.assertEqual(r.classify([140, .3, .03, 1, 31, 82, 30, 1004, 72] * 15).label, "Poor_ventilation")


class ScoreTests(unittest.TestCase):
    def test_score_uses_worst_live_pollutant(self):
        score, parts = main.environment_score({"x6_valid": True, "dust_valid": True, "pm25_ugm3": 160,
                                               "tvoc_ppm": 0.1, "hcho_ppm": 0.01, "co_ppm": 0.5, "iaq": 40})
        self.assertLess(score, 20)
        self.assertEqual(min(parts, key=parts.get), "pm")

    def test_dead_sensor_never_counts_as_clean(self):
        score, parts = main.environment_score({"x6_valid": False, "dust_valid": False})
        self.assertEqual((score, parts), (0, {}))

    def test_clean_json_removes_nan(self):
        out = main.clean_json({"a": float("nan"), "b": [float("inf"), 1.5]})
        json.dumps(out, allow_nan=False)
        self.assertIsNone(out["a"])


class StoreTests(unittest.TestCase):
    def test_history_buckets_and_persistence(self):
        path = Path(tempfile.mkdtemp()) / "h.json"
        h = History(path, 2, 3600, 60, 86400)
        t0 = time.time() - 300
        for i in range(300):
            h.add({"score": 50 + (i % 10), "pm": 10.0}, t0 + i)
        pts = h.query(900)["points"]
        self.assertGreater(len(pts), 100)
        self.assertTrue(all(p["pm"] == 10.0 for p in pts))
        h.save()
        self.assertGreaterEqual(len(History(path).coarse), 4)

    def test_event_log_newest_first(self):
        log = EventLog(Path(tempfile.mkdtemp()) / "e.json")
        log.add("good", "one")
        log.add("warn", "two")
        self.assertEqual(log.list()[0]["title"], "two")


class AdvisorTests(unittest.TestCase):
    ctx = {"reading": {"x6_valid": True, "dust_valid": True, "pm25_ugm3": 20, "co_ppm": 0.5, "tvoc_ppm": 0.1,
                       "hcho_ppm": 0.01, "temperature_c": 30, "humidity_pct": 60, "iaq": 40},
           "score": 72, "classification": "Clean_air"}
    ok_body = {"candidates": [{"content": {"parts": [{"text": "Fine."}]}}]}

    def advisor(self):
        cfg = types.SimpleNamespace(ACTIVE_AI="gemini", GEMINI_MODEL="gemini-3.6-flash",
                                    GEMINI_FALLBACK_MODELS=["gemini-3.5-flash", "gemini-3.1-flash-lite"],
                                    GEMINI_API_KEY="k", AI_TIMEOUT_S=45, AI_QUESTION_BUDGET_S=30,
                                    AI_ATTEMPT_TIMEOUT_S=18, AI_TIP_BUDGET_S=20)
        return ai_advisor.Advisor(cfg)

    def test_success_uses_minimal_thinking_and_strips_markdown(self):
        body = {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True},
                                                      {"text": "**Air is fine.**\nDo this: relax."}]}}]}
        with mock.patch.object(ai_advisor.requests, "post", return_value=Resp(200, body)) as post:
            text, source = self.advisor().ask(self.ctx, "ok?")
        self.assertEqual((text, source), ("Air is fine.\nDo this: relax.", "gemini"))
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["generationConfig"]["thinkingConfig"]["thinkingLevel"], "minimal")
        self.assertIn("PM2.5 dust: 20", sent["contents"][0]["parts"][0]["text"])

    def test_timeout_is_retried_on_the_same_model(self):
        replies = [ai_advisor.requests.exceptions.ReadTimeout("slow"), Resp(200, self.ok_body)]
        with mock.patch.object(ai_advisor.requests, "post", side_effect=replies) as post:
            text, source = self.advisor().ask(self.ctx)
        self.assertEqual((text, source), ("Fine.", "gemini"))
        self.assertTrue(all("gemini-3.6-flash" in c.args[0] for c in post.call_args_list))
        self.assertLessEqual(post.call_args_list[0].kwargs["timeout"], 18)

    def test_busy_twice_then_answers(self):
        busy = Resp(503, {"error": {"message": "high demand"}})
        with mock.patch.object(ai_advisor.requests, "post", side_effect=[busy, busy, Resp(200, self.ok_body)]), \
                mock.patch.object(ai_advisor.time, "sleep"):
            self.assertEqual(self.advisor().ask(self.ctx)[1], "gemini")

    def test_busy_model_is_retried_once(self):
        replies = [Resp(503, {"error": {"message": "high demand"}}), Resp(200, self.ok_body)]
        with mock.patch.object(ai_advisor.requests, "post", side_effect=replies) as post, \
                mock.patch.object(ai_advisor.time, "sleep"):
            self.assertEqual(self.advisor().ask(self.ctx)[1], "gemini")
        self.assertTrue(all("gemini-3.6-flash" in c.args[0] for c in post.call_args_list))

    def test_retired_model_is_dropped_for_the_session(self):
        adv = self.advisor()
        gone = Resp(404, {"error": {"message": "models/gemini-3.6-flash is no longer available to new users"}})
        with mock.patch.object(ai_advisor.requests, "post", side_effect=[gone, Resp(200, self.ok_body)]):
            adv.ask(self.ctx)
        with mock.patch.object(ai_advisor.requests, "post", return_value=Resp(200, self.ok_body)) as post:
            adv.ask(self.ctx)
        self.assertTrue(all("gemini-3.6-flash" not in c.args[0] for c in post.call_args_list))

    def test_unsupported_thinking_level_falls_back_and_is_remembered(self):
        adv = self.advisor()
        bad = Resp(400, {"error": {"message": "thinking_level MINIMAL is not supported"}})
        with mock.patch.object(ai_advisor.requests, "post", side_effect=[bad, Resp(200, self.ok_body)]) as post:
            adv.ask(self.ctx)
        self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")
        with mock.patch.object(ai_advisor.requests, "post", return_value=Resp(200, self.ok_body)) as post:
            adv.ask(self.ctx)
        self.assertEqual(post.call_count, 1)

    def test_total_failure_returns_nothing_then_backs_off(self):
        adv = self.advisor()
        with mock.patch.object(ai_advisor.requests, "post",
                               side_effect=ai_advisor.requests.exceptions.ConnectionError("down")):
            text, source = adv.ask(self.ctx, "Should I open the windows?")
        self.assertEqual((text, source), (None, "busy"))
        self.assertTrue(adv.cooling_down())
        self.assertLessEqual(adv._cooldown_until - ai_advisor.time.monotonic(), 30.5)
        with mock.patch.object(ai_advisor.requests, "post") as post:
            self.assertEqual(adv.ask(self.ctx), (None, "busy"))
            self.assertIsNone(adv.tip(self.ctx, "test"))
        post.assert_not_called()
        with mock.patch.object(ai_advisor.requests, "post", return_value=Resp(200, self.ok_body)):
            self.assertEqual(adv.ask(self.ctx, "Can I exercise?")[1], "gemini")
        self.assertFalse(adv.cooling_down())

    def test_backoff_grows_and_caps(self):
        adv = self.advisor()
        for n in range(8):
            adv._failed(RuntimeError("x"))
        self.assertLessEqual(adv._cooldown_until - ai_advisor.time.monotonic(), 300.5)

    def test_tip_is_short_and_plain(self):
        body = {"candidates": [{"content": {"parts": [{"text": "**Open two windows** for ten minutes.\nThen close them."}]}}]}
        with mock.patch.object(ai_advisor.requests, "post", return_value=Resp(200, body)) as post:
            tip = self.advisor().tip(self.ctx, "dust spike")
        self.assertEqual(tip, "Open two windows for ten minutes. Then close them.")
        self.assertIn("Situation: dust spike", post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"])

    def test_bad_key_reports_clear_error(self):
        adv = self.advisor()
        with mock.patch.object(ai_advisor.requests, "post", return_value=Resp(400, {"error": {"message": "API key not valid"}})):
            self.assertEqual(adv.ask(self.ctx), (None, "busy"))
        self.assertIn("GEMINI_API_KEY", adv.snapshot()["last_error"])

    def test_no_heat_stroke_content_anywhere(self):
        prompt = ai_advisor.describe_context(self.ctx)
        for word in ("heat index", "feels like", "heat stroke"):
            self.assertNotIn(word, prompt.lower())

    def test_refresh_prompt_asks_gemini_not_to_repeat_itself(self):
        prompt = ai_advisor.describe_context(dict(self.ctx, previous="Open two windows."))
        self.assertIn("do not repeat it", prompt)
        self.assertIn("Open two windows.", prompt)


class TelegramTests(unittest.TestCase):
    def test_messages_are_html_safe_and_complete(self):
        m = atmosis_notify.Messages(330, "http://192.168.1.5:7000/")
        ctx = {"reading": {"x6_valid": True, "dust_valid": True, "pressure_valid": True, "pm25_ugm3": 180,
                           "co_ppm": 12, "tvoc_ppm": .6, "hcho_ppm": .05, "iaq": 180, "temperature_c": 27.4,
                           "humidity_pct": 58, "pressure_hpa": 1004.6, "altitude_m": 71},
               "score": 18, "band": "hazardous", "live": True, "score_parts": {"pm": 18}, "confidence": 0.9}
        texts = [m.online(ctx), m.poor_air(ctx), m.recovered(ctx), m.dust_high(ctx), m.dust_clear(ctx),
                 m.co_alert(ctx), m.co_clear(ctx), m.pattern(ctx, "Indoor_pollution"),
                 m.pattern(ctx, "Poor_ventilation"), m.link_lost(3), m.link_restored(ctx), m.test(ctx),
                 m.daily({"score_avg": 71.2, "pm_avg": 22, "pm_max": 88, "co_avg": .5, "co_max": 1.2,
                          "tvoc_avg": .1, "tvoc_max": .4, "temp_min": 25.1, "temp_max": 29.8, "hum_avg": 57,
                          "pres_avg": 1004.2, "worst_at": "8:10 PM", "minutes_poor": 12}, ctx)]
        for t in texts:
            for tag in ("b", "i", "pre", "code", "a"):
                self.assertEqual(t.count(f"<{tag}>") + t.count(f"<{tag} "), t.count(f"</{tag}>"), (tag, t))
            self.assertIn("Atmosis ·", t)
            self.assertNotIn("None", t)
            self.assertNotIn("nan", t.lower())
            self.assertNotIn("heat", t.lower().replace("heaters", ""))
        full = m.online(ctx)
        for label in ("PM2.5", "CO", "VOCs", "HCHO", "IAQ", "Temperature", "Humidity", "Pressure", "Altitude"):
            self.assertIn(label, full)
        self.assertIn("▰", full)
        self.assertIn("fine dust", m.poor_air(ctx))
        self.assertEqual(m.pattern(ctx, "Clean_air"), "")

    def test_plain_text_fallback_on_bad_markup(self):
        cfg = types.SimpleNamespace(TELEGRAM_ENABLED=True, TELEGRAM_BOT_TOKEN="1:a", TELEGRAM_CHAT_ID="9", TELEGRAM_TIMEOUT_S=3)
        tg = atmosis_notify.Telegram(cfg)
        replies = [Resp(400, {"description": "Bad Request: can't parse entities"}), Resp(200, {"ok": True})]
        with mock.patch.object(atmosis_notify.requests, "post", side_effect=replies) as post:
            self.assertTrue(tg.send("<b>broken"))
        self.assertNotIn("parse_mode", post.call_args.kwargs["json"])
        self.assertEqual(post.call_args.kwargs["json"]["text"], "broken")

    def test_send_uses_html_mode(self):
        cfg = types.SimpleNamespace(TELEGRAM_ENABLED=True, TELEGRAM_BOT_TOKEN="1:a", TELEGRAM_CHAT_ID="9", TELEGRAM_TIMEOUT_S=3)
        tg = atmosis_notify.Telegram(cfg)
        with mock.patch.object(atmosis_notify.requests, "post", return_value=Resp(200, {"ok": True})) as post:
            self.assertTrue(tg.send("<b>hi</b>"))
        self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "HTML")
        with mock.patch.object(atmosis_notify.requests, "post", return_value=Resp(400, {"description": "Bad Request: chat not found"})):
            tg._last_sent = 0
            self.assertFalse(tg.send("x"))
        self.assertIn("press Start", tg.snapshot()["last_error"])


class SystemTests(unittest.TestCase):
    def run_ticks(self, core, n):
        for _ in range(n):
            core.tick()

    def test_online_message_sent_once_after_first_readings(self):
        core, src = make_core()
        src.set()
        self.run_ticks(core, 20)
        msgs = queued_messages(core)
        self.assertEqual(sum("Atmosis is online" in m for m in msgs), 1)
        self.run_ticks(core, 20)
        self.assertFalse(any("online" in m for m in queued_messages(core)))

    def test_dust_failsafe_and_recovery(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False})
        src.set()
        self.run_ticks(core, 5)
        src.set(pm25_ugm3=190, mcu_failsafe=True)
        self.run_ticks(core, 5)
        msgs = queued_messages(core)
        self.assertTrue(any("Very high dust level" in m for m in msgs))
        self.assertTrue(any("Air quality needs attention" in m for m in msgs))
        src.set(pm25_ugm3=12, mcu_failsafe=False)
        self.run_ticks(core, 5)
        self.assertTrue(any("Dust level back to safe" in m for m in queued_messages(core)))
        titles = [e["title"] for e in core.events.list()]
        self.assertIn("Dust alarm", titles)

    def test_co_alert_and_clear(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False})
        src.set(co_ppm=14)
        self.run_ticks(core, 3)
        self.assertTrue(any("Carbon monoxide alert" in m for m in queued_messages(core)))
        self.assertTrue(core.snapshot()["failsafe"]["co"])
        src.set(co_ppm=1.0)
        self.run_ticks(core, 3)
        self.assertTrue(any("Carbon monoxide back to safe" in m for m in queued_messages(core)))
        self.assertFalse(core.snapshot()["failsafe"]["co"])

    def test_no_heat_alerts_even_in_hot_humid_air(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False})
        src.set(temperature_c=38, humidity_pct=85)
        self.run_ticks(core, 10)
        st = core.snapshot()
        self.assertNotIn("heat_index_c", st)
        self.assertFalse(st["failsafe"]["co"] or st["failsafe"]["dust"])
        self.assertEqual(queued_messages(core), [])

    def test_daily_report(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False, "NOTIFY_DAILY_HOUR": core_hour()})
        src.set()
        core.started -= 7200
        t0 = time.time() - 3000
        for i in range(50):
            core.history.add({"score": 70 + i % 5, "pm": 20.0, "co": 0.5, "tvoc": 0.1, "temp": 27.0,
                              "hum": 55.0, "pres": 1004.0}, t0 + i * 60)
        self.run_ticks(core, 2)
        msgs = queued_messages(core)
        self.assertEqual(sum("Daily air report" in m for m in msgs), 1)
        self.run_ticks(core, 2)
        self.assertFalse(any("Daily air report" in m for m in queued_messages(core)))

    def test_link_lost_and_restored(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False, "NOTIFY_LINK_LOST_S": 0.0})
        src.set()
        self.run_ticks(core, 3)
        src.snap = Snapshot(link_up=False, error="TimeoutError")
        self.run_ticks(core, 12)
        self.assertTrue(any("Sensors are not responding" in m for m in queued_messages(core)))
        src.set()
        self.run_ticks(core, 12)
        self.assertTrue(any("Sensors reconnected" in m for m in queued_messages(core)))

    def test_all_sensor_values_reach_the_dashboard_state(self):
        core, src = make_core()
        src.set(altitude_m=71.2, sensor_temp_c=29.4, pressure_hpa=1004.6)
        self.run_ticks(core, 5)
        r = core.snapshot()["reading"]
        for key in ("iaq", "tvoc_ppm", "hcho_ppm", "co_ppm", "temperature_c", "humidity_pct", "pm25_ugm3",
                    "pressure_hpa", "altitude_m", "sensor_temp_c"):
            self.assertIn(key, r)
        self.assertAlmostEqual(r["altitude_m"], 71.2)
        self.assertAlmostEqual(r["sensor_temp_c"], 29.4)

    def test_state_is_strict_json_and_complete(self):
        core, src = make_core()
        src.set()
        self.run_ticks(core, 20)
        st = core.snapshot()
        json.dumps(st, allow_nan=False)
        for key in ("reading", "score", "band", "classification", "advisory", "ai", "sensors", "failsafe"):
            self.assertIn(key, st)
        self.assertTrue(st["classification"]["ready"])

    def test_history_keeps_altitude(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False})
        src.set(altitude_m=123.0)
        self.run_ticks(core, 12)
        pts = core.history.query(900)["points"]
        self.assertTrue(any(p.get("alt") == 123.0 for p in pts))

    def test_board_restart_is_logged(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False})
        src.set()
        self.run_ticks(core, 3)
        src.restart_detected = True
        core.tick()
        src.restart_detected = False
        self.assertIn("Board restarted", [e["title"] for e in core.events.list()])

    def test_simulated_source_runs_clean(self):
        clf, report = atmosis_ei.load_classifier(config.MODEL_DIRS)
        cfg = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
        cfg.DATA_DIR = Path(tempfile.mkdtemp())
        core = main.Atmosis(clf, report, SimulatedSource(seed=1, interval_s=0), cfg)
        self.run_ticks(core, 30)
        self.assertEqual(core.snapshot()["band"] in ("excellent", "good", "moderate"), True)


class PresentationTests(unittest.TestCase):
    def test_banner_shows_localhost_dashboard_and_truthful_engine(self):
        import io
        from contextlib import redirect_stdout
        core, _ = make_core()
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.banner(core)
        out = buf.getvalue()
        self.assertIn("http://localhost:7000/", out)
        self.assertIn("Atmosis pattern engine", out)
        self.assertNotIn("Edge Impulse", out)
        self.assertNotIn("on-device", out.lower())
        self.assertNotIn("retrain", out.lower())

    def test_alerts_carry_gemini_tip_and_co_never_waits(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False})
        src.set(co_ppm=14)
        core.tick()
        jobs = []
        while not core.alerts.empty():
            jobs.append(core.alerts.get_nowait())
        co_jobs = [p for k, p in jobs if k == "alert" and p[0] == "co_alert"]
        self.assertEqual(len(co_jobs), 1)
        builder, situation, ctx, extra = co_jobs[0]
        core.alerts.put(("alert", (builder, situation, ctx, extra)))
        core.advisor.tip = mock.Mock(return_value="should not be used")
        sent = []
        core.tg.send = lambda text: sent.append(text) or True
        runner = threading.Thread(target=core.worker, args=(core.alerts,), daemon=True)
        runner.start()
        t0 = time.time()
        while not sent and time.time() - t0 < 3:
            time.sleep(0.02)
        main._shutdown.set(); runner.join(2); main._shutdown.clear()
        core.advisor.tip.assert_not_called()
        self.assertIn("Carbon monoxide alert", sent[0])
        m = atmosis_notify.Messages()
        card = m.poor_air(ctx, tip="Open <two> windows & run a purifier.")
        self.assertIn("✦ Gemini suggests", card)
        self.assertIn("<blockquote>Open &lt;two&gt; windows &amp; run a purifier.</blockquote>", card)

    def test_hourly_reminder_while_air_stays_poor(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False, "NOTIFY_REMINDER_S": 0.0, "NOTIFY_MIN_GAP_S": 0.0})
        src.set(pm25_ugm3=200)
        for _ in range(4):
            core.tick()
        builders = []
        while not core.alerts.empty():
            kind, payload = core.alerts.get_nowait()
            if kind == "alert":
                builders.append(payload[0])
        self.assertIn("poor_air", builders)
        self.assertIn("still_poor", builders)

    def test_worker_survives_many_mixed_jobs(self):
        core, _ = make_core({"TELEGRAM_ENABLED": False})
        core.advisor.ask = mock.Mock(side_effect=[("Alpha advice", "gemini"), (None, "busy"),
                                                  ("Completely different guidance", "gemini"), ("D", "gemini")])
        for _ in range(3):
            core._submit("advisory", core.advisory_context())
        core._submit("followup", {"id": "x", "question": "q", "created": time.time(), "next": 0, "tries": 0, "busy": True})
        runner = threading.Thread(target=core.worker, args=(core.jobs,), daemon=True)
        runner.start()
        t0 = time.time()
        while (core.advisor.ask.call_count < 4 or not core.jobs.empty()) and time.time() - t0 < 3:
            time.sleep(0.02)
        alive = runner.is_alive()
        main._shutdown.set(); runner.join(2); main._shutdown.clear()
        self.assertTrue(alive)
        self.assertEqual(core.advisor.ask.call_count, 4)
        self.assertEqual(core.advisory["text"], "Completely different guidance")
        self.assertEqual(core.answers[-1]["text"], "D")

    def test_question_gets_a_gemini_follow_up(self):
        core, _ = make_core({"TELEGRAM_ENABLED": False})
        core.advisor.provider = "gemini"
        client = main.build_flask_app(core).test_client()
        core.advisor.ask = mock.Mock(return_value=(None, "busy"))
        r = client.post("/api/ask", json={"question": "Can I exercise?"}).get_json()
        self.assertTrue(r["followup"])
        core.advisor.ask = mock.Mock(return_value=("Gemini answer.", "gemini"))
        item = core._followups[0]
        item["next"] = 0
        core._service_followups()
        kind, payload = core.jobs.get_nowait()
        self.assertEqual(kind, "followup")
        core.jobs.put((kind, payload))
        runner = threading.Thread(target=core.worker, args=(core.jobs,), daemon=True)
        runner.start()
        t0 = time.time()
        while not core.answers and time.time() - t0 < 3:
            time.sleep(0.02)
        main._shutdown.set(); runner.join(2); main._shutdown.clear()
        self.assertEqual(core.snapshot()["answers"][0]["id"], r["followup"])
        self.assertEqual(core.snapshot()["answers"][0]["text"], "Gemini answer.")

    def test_edge_impulse_named_everywhere_once_active(self):
        core, _ = make_core()
        core.classifier = types.SimpleNamespace(engine="edge-impulse")
        self.assertIn("Edge Impulse", main.engine_label(core.classifier))
        m = atmosis_notify.Messages()
        self.assertIn("Edge Impulse", m.online({"engine": "edge-impulse", "reading": {}, "live": False}))
        self.assertNotIn("Edge Impulse", m.online({"engine": "rules", "reading": {}, "live": False}))

    def test_status_never_mentions_retraining(self):
        core, src = make_core()
        src.set()
        for _ in range(5):
            core.tick()
        text = json.dumps(core.snapshot()).lower()
        self.assertNotIn("retrain", text)
        self.assertNotIn("untrained", text)

    def test_gemini_is_called_only_when_air_needs_attention(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False, "TELEGRAM_ENABLED": False})
        core.advisor.provider = "gemini"
        src.set(pm25_ugm3=5, co_ppm=0.3, tvoc_ppm=0.05, hcho_ppm=0.01, iaq=20, humidity_pct=45)
        for _ in range(8):
            core.tick()
        jobs = [k for k, _ in list(core.jobs.queue)]
        self.assertNotIn("advisory", jobs)
        self.assertEqual(core.advisory["text"], "")
        self.assertFalse(core.advisory["needed"])
        src.set(pm25_ugm3=120, co_ppm=0.3, tvoc_ppm=0.05, hcho_ppm=0.01, iaq=20, humidity_pct=45)
        core.tick()
        self.assertIn("advisory", [k for k, _ in list(core.jobs.queue)])
        self.assertTrue(core.advisory["needed"])

    def test_no_repeat_requests_while_air_wobbles_or_improves(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False, "TELEGRAM_ENABLED": False})
        core.advisor.provider = "gemini"
        def advisory_jobs():
            n = 0
            while not core.jobs.empty():
                if core.jobs.get_nowait()[0] == "advisory":
                    n += 1
            return n
        src.set(pm25_ugm3=60)
        for _ in range(5):
            core.tick()
        self.assertEqual(advisory_jobs(), 1)
        core.advisory["pending"] = False
        for pm in (61, 59, 62, 58, 45, 50, 60) * 3:
            src.set(pm25_ugm3=pm)
            core.tick()
        self.assertEqual(advisory_jobs(), 0)
        src.set(pm25_ugm3=200)
        core.tick()
        self.assertEqual(advisory_jobs(), 0)
        core._adv_at -= core.cfg.AI_ESCALATE_GAP_S
        core.tick()
        self.assertEqual(advisory_jobs(), 1)

    def test_near_identical_gemini_text_is_not_shown_as_new(self):
        core, _ = make_core({"TELEGRAM_ENABLED": False})
        core.advisory.update(text="Open two windows on opposite sides for ten minutes.", at=123.0)
        core.advisor.ask = mock.Mock(return_value=("Open two windows on opposite sides for 10 minutes.", "gemini"))
        core._submit("advisory", core.advisory_context())
        runner = threading.Thread(target=core.worker, args=(core.jobs,), daemon=True)
        runner.start()
        t0 = time.time()
        while core.advisor.ask.call_count == 0 and time.time() - t0 < 3:
            time.sleep(0.02)
        time.sleep(0.1)
        main._shutdown.set(); runner.join(2); main._shutdown.clear()
        self.assertEqual(core.advisory["at"], 123.0)

    def test_unanswered_question_resolves_gracefully(self):
        core, _ = make_core({"TELEGRAM_ENABLED": False, "AI_FOLLOWUP_WINDOW_S": 0.0})
        fid = core.queue_followup("Can I exercise?")
        core._service_followups()
        self.assertEqual(core.snapshot()["answers"][-1]["id"], fid)
        self.assertTrue(core.snapshot()["answers"][-1]["failed"])

    def test_dust_and_attention_alerts_are_spaced_out(self):
        core, src = make_core({"NOTIFY_SEND_STARTUP": False})
        for _ in range(3):
            src.set(pm25_ugm3=10); [core.tick() for _ in range(4)]
            src.set(pm25_ugm3=200, mcu_failsafe=True); [core.tick() for _ in range(4)]
            src.set(pm25_ugm3=10, mcu_failsafe=False); [core.tick() for _ in range(4)]
        msgs = queued_messages(core)
        self.assertEqual(sum("Very high dust level" in m for m in msgs), 1)
        self.assertEqual(sum("Dust level back to safe" in m for m in msgs), 1)
        self.assertLessEqual(sum("Air quality needs attention" in m for m in msgs), 1)

    def test_only_gemini_35_flash_is_configured(self):
        adv = ai_advisor.Advisor(config)
        self.assertEqual(adv._models, ["gemini-3.5-flash"])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core, cls.src = make_core({"TELEGRAM_ENABLED": False})
        cls.src.set()
        for _ in range(20):
            cls.core.tick()
        cls.client = main.build_flask_app(cls.core).test_client()

    def test_endpoints(self):
        for path in ("/", "/api/ping", "/api/status", "/api/history?range=15m", "/api/history?range=24h",
                     "/api/events", "/api/profile"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            if path.startswith("/api"):
                json.loads(r.data, parse_constant=lambda c: self.fail(f"non-JSON constant {c} in {path}"))

    def test_ask(self):
        self.assertEqual(self.client.post("/api/ask", json={"question": ""}).status_code, 400)
        r = self.client.post("/api/ask", json={"question": "Can I exercise?"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("GEMINI_API_KEY", r.get_json()["error"])
        self.core.advisor.provider = "gemini"
        with mock.patch.object(self.core.advisor, "ask", return_value=("Yes, gently.", "gemini")):
            r = self.client.post("/api/ask", json={"question": "Can I exercise?"}).get_json()
        self.core.advisor.provider = "offline"
        self.assertEqual((r["answer"], r["source"]), ("Yes, gently.", "gemini"))

    def test_profile_round_trip(self):
        r = self.client.post("/api/profile", json={"age": 70, "asthma": True})
        self.assertEqual(r.get_json()["age"], 70)
        self.assertEqual(self.client.post("/api/profile", json={"age": "old"}).status_code, 400)

    def test_test_notify_explains_missing_setup(self):
        r = self.client.post("/api/test_notify")
        self.assertEqual(r.status_code, 400)
        self.assertIn(".env", r.get_json()["error"])

    def test_dashboard_render_calls_only_defined_functions(self):
        import re
        html = (HERE / "atmosis-dashboard.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        defined = set(re.findall(r"function\s+(\w+)\s*\(", js))
        body = re.search(r"function render\(\)\{(.*?)\n\}", js, re.S).group(1)
        called = set(re.findall(r"\b(render\w+)\(", body))
        self.assertTrue(called)
        self.assertEqual(called - defined, set())
        for view in ("view-model", "view-device", "data-view=\"model\"", "data-view=\"device\""):
            self.assertNotIn(view, html)

    def test_dashboard_needs_no_internet(self):
        html = (HERE / "atmosis-dashboard.html").read_text()
        self.assertNotRegex(html, r'(src|href)="https?://')


if __name__ == "__main__":
    unittest.main(verbosity=1)
