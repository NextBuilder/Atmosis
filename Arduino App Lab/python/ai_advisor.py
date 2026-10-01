from __future__ import annotations

import re
import threading
import time

from atmosis_log import log
from typing import Optional, Tuple

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    requests = None
    HAVE_REQUESTS = False

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = (
    "You are Atmosis, the calm indoor-air advisor inside a home air-quality monitor in India. "
    "Speak plainly to a non-expert. Use only the live readings given. Reply in 2 to 4 short "
    "sentences, then one line that starts with 'Do this:' giving one practical action. No markdown, "
    "no lists, no diagnosis, no mention of medical conditions."
)


def describe_context(ctx: dict) -> str:
    r = ctx.get("reading", {})
    lines = [f"Overall air score: {ctx.get('score', '?')}/100 (higher is better)"]
    if r.get("x6_valid"):
        lines += [
            f"IAQ index: {r.get('iaq', 0):.0f} (0 best, 500 worst)",
            f"TVOC: {r.get('tvoc_ppm', 0):.3f} ppm, formaldehyde {r.get('hcho_ppm', 0):.3f} ppm, "
            f"carbon monoxide {r.get('co_ppm', 0):.2f} ppm",
            f"Temperature: {r.get('temperature_c', 0):.1f} C, humidity {r.get('humidity_pct', 0):.0f}%",
        ]
    else:
        lines.append("Gas sensor: offline")
    lines.append(f"PM2.5 dust: {r.get('pm25_ugm3', 0):.0f} ug/m3" if r.get("dust_valid") else "Dust sensor: offline")
    if r.get("pressure_valid"):
        lines.append(f"Air pressure: {r.get('pressure_hpa', 0):.1f} hPa")
    lines.append(f"Pattern detected: {str(ctx.get('classification', 'unknown')).replace('_', ' ')}")
    lines.append(f"User sensitivity: {ctx.get('sensitivity', 'standard')}")
    if ctx.get("previous"):
        lines.append(f"Your previous advice was: \"{ctx['previous'][:400]}\" — do not repeat it; build on it or add something new.")
    return "\n".join(lines)


def _clean(text: str) -> str:
    text = re.sub(r"\*\*?|__|`", "", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


THINKING_LEVELS = ("minimal", "low", None)


class _Retryable(RuntimeError):
    pass


class _Retired(RuntimeError):
    pass


class Advisor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.provider = cfg.ACTIVE_AI if HAVE_REQUESTS else "offline"
        chain = [cfg.GEMINI_MODEL] + list(cfg.GEMINI_FALLBACK_MODELS)
        self._models = list(dict.fromkeys(m for m in chain if m))
        self._retired: set = set()
        self._thinking: dict = {}
        self._good: Optional[str] = None
        self._cooldown_until = 0.0
        self._failures = 0
        self._lock = threading.Lock()
        self.status = {
            "provider": self.provider,
            "model": self._model_name(),
            "enabled": self.provider != "offline",
            "ok": None,
            "last_error": "" if HAVE_REQUESTS else "the 'requests' package is not installed",
            "last_ok_at": 0.0,
            "latency_ms": 0,
            "calls": 0,
        }

    def _model_name(self) -> str:
        if self.provider != "gemini":
            return "offline"
        live = [m for m in self._models if m not in self._retired]
        return self._good or (live[0] if live else self._models[0])

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.status)

    def cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _record(self, ok: bool, error: str = "", latency_ms: int = 0) -> None:
        with self._lock:
            self.status["calls"] += 1
            self.status["ok"] = ok
            self.status["model"] = self._model_name()
            if ok:
                self.status["last_error"] = ""
                self.status["last_ok_at"] = time.time()
                self.status["latency_ms"] = latency_ms
            else:
                self.status["last_error"] = error

    def _succeeded(self, t0: float) -> None:
        self._failures = 0
        self._cooldown_until = 0.0
        self._record(True, latency_ms=int((time.monotonic() - t0) * 1000))

    def _failed(self, exc: Exception) -> None:
        self._failures += 1
        self._cooldown_until = time.monotonic() + min(300.0, 30.0 * (2 ** (self._failures - 1)))
        self._record(False, str(exc))
        log("warn", "Gemini is busy — retrying shortly")

    def ask(self, ctx: dict, question: Optional[str] = None) -> Tuple[Optional[str], str]:
        if self.provider == "offline":
            return None, "off"
        if not question and self.cooling_down():
            return None, "busy"
        prompt = describe_context(ctx)
        if question:
            prompt += f"\n\nThe user asks: {question.strip()[:500]}\nAnswer that question directly using the readings."
        else:
            prompt += "\n\nGive a short status update and advice for right now."
        budget = self.cfg.AI_QUESTION_BUDGET_S if question else self.cfg.AI_TIMEOUT_S
        t0 = time.monotonic()
        try:
            text = self._gemini(prompt, t0 + budget)
            self._succeeded(t0)
            return _clean(text), "gemini"
        except Exception as exc:
            self._failed(exc)
            return None, "busy"

    def tip(self, ctx: dict, situation: str) -> Optional[str]:
        if self.provider == "offline" or self.cooling_down():
            return None
        prompt = (describe_context(ctx) + f"\n\nSituation: {situation}.\n"
                  "Reply with ONE practical recommendation in at most two short sentences. "
                  "No greeting, no 'Do this:' prefix, no markdown.")
        t0 = time.monotonic()
        try:
            text = self._gemini(prompt, t0 + self.cfg.AI_TIP_BUDGET_S)
            self._succeeded(t0)
            return _clean(text).replace("\n", " ")[:320]
        except Exception as exc:
            self._failed(exc)
            return None

    def _gemini(self, prompt: str, deadline: float) -> str:
        live = [m for m in self._models if m not in self._retired]
        order = ([self._good] if self._good in live else []) + [m for m in live if m != self._good]
        errors = []
        for model in order:
            for attempt in range(3):
                remaining = deadline - time.monotonic()
                if remaining < 3.0:
                    raise RuntimeError("; ".join(errors[-2:]) or "time budget used up")
                try:
                    text = self._gemini_call(model, prompt, min(remaining, self.cfg.AI_ATTEMPT_TIMEOUT_S))
                    self._good = model
                    return text
                except _Retired as exc:
                    self._retired.add(model)
                    errors.append(f"{model}: {exc}")
                    break
                except _Retryable as exc:
                    errors.append(f"{model}: {exc}")
                    if attempt < 2 and "busy" in str(exc) and deadline - time.monotonic() > 8.0:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    break
                except requests.exceptions.RequestException as exc:
                    timed_out = isinstance(exc, requests.exceptions.Timeout)
                    errors.append(f"{model}: {'timed out' if timed_out else 'network error'}")
                    if attempt < 2 and deadline - time.monotonic() > 10.0:
                        continue
                    break
        raise RuntimeError("; ".join(errors[-2:]) or "no Gemini model is available")

    def _gemini_call(self, model: str, prompt: str, timeout: float) -> str:
        levels = [self._thinking[model]] if model in self._thinking else list(THINKING_LEVELS)
        for level in levels:
            body = {
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            }
            if level:
                body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": level}}
            resp = requests.post(GEMINI_URL.format(model=model),
                                 headers={"x-goog-api-key": self.cfg.GEMINI_API_KEY, "Content-Type": "application/json"},
                                 json=body, timeout=timeout)
            if resp.status_code == 400 and level and "thinking" in resp.text.lower():
                continue
            self._thinking[model] = level
            return self._parse(resp)
        raise _Retryable("rejected every thinking setting")

    @staticmethod
    def _parse(resp) -> str:
        code, detail = resp.status_code, _error_text(resp)
        low = detail.lower()
        if code in (400, 401, 403) and "api key" in low:
            raise RuntimeError("Gemini rejected the API key — check GEMINI_API_KEY in python/.env")
        if code == 404 or "no longer available" in low:
            raise _Retired("retired or not found")
        if code in (429, 500, 503, 504):
            raise _Retryable(f"busy (HTTP {code})")
        if code != 200:
            raise _Retryable(f"HTTP {code} {detail[:80]}")
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise _Retryable(f"empty reply ({reason})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not text:
            raise _Retryable(f"empty text (finish: {cands[0].get('finishReason', '?')})")
        return text


def _error_text(resp) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            return str(err.get("message", ""))[:160]
        return str(err)[:160]
    except Exception:
        return resp.text[:160]
