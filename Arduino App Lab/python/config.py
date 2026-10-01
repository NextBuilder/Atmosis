from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

GEMINI_API_KEY     = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID   = ""


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    env_path = Path(path) if path else HERE / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        print(f"[config] could not read {env_path}: {exc}")


load_dotenv()


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


_PLACEHOLDERS = {"", "AIza...", "YOUR_API_KEY", "YOUR_KEY_HERE", "PASTE_HERE",
                 "YOUR_BOT_TOKEN", "123456:ABC..."}


def _configured(value: str) -> bool:
    return bool(value) and value not in _PLACEHOLDERS


GEMINI_API_KEY     = _str("GEMINI_API_KEY", GEMINI_API_KEY)
TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
TELEGRAM_CHAT_ID   = _str("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

AI_PROVIDER = _str("ATMOSIS_AI_PROVIDER", "auto").lower()
GEMINI_MODEL = _str("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACK_MODELS = [m.strip() for m in _str(
    "GEMINI_FALLBACK_MODELS", "",
).split(",") if m.strip()]
AI_TIMEOUT_S = _float("ATMOSIS_AI_TIMEOUT_S", 45.0)
AI_QUESTION_BUDGET_S = _float("ATMOSIS_AI_QUESTION_BUDGET_S", 30.0)
AI_ATTEMPT_TIMEOUT_S = _float("ATMOSIS_AI_ATTEMPT_TIMEOUT_S", 18.0)
AI_TIP_BUDGET_S = _float("ATMOSIS_AI_TIP_BUDGET_S", 20.0)
NOTIFY_REMINDER_S = _float("ATMOSIS_NOTIFY_REMINDER_S", 3600.0)
NOTIFY_MIN_GAP_S = _float("ATMOSIS_NOTIFY_MIN_GAP_S", 600.0)
NOTIFY_DUST_COOLDOWN_S = _float("ATMOSIS_NOTIFY_DUST_COOLDOWN_S", 900.0)
AI_FOLLOWUP_WINDOW_S = _float("ATMOSIS_AI_FOLLOWUP_WINDOW_S", 240.0)
AI_AUTO_INTERVAL_S = _float("ATMOSIS_AI_AUTO_INTERVAL_S", 1800.0)
AI_ESCALATE_GAP_S = _float("ATMOSIS_AI_ESCALATE_GAP_S", 300.0)

GEMINI_ENABLED = _configured(GEMINI_API_KEY)
ACTIVE_AI = "gemini" if GEMINI_ENABLED and AI_PROVIDER != "off" else "offline"

TELEGRAM_ENABLED = _configured(TELEGRAM_BOT_TOKEN) and _configured(TELEGRAM_CHAT_ID)
TELEGRAM_TIMEOUT_S = _float("TELEGRAM_TIMEOUT_S", 8.0)

SIMULATE_SENSORS = _bool("ATMOSIS_SIMULATE", False)

HTTP_HOST = _str("ATMOSIS_HTTP_HOST", "0.0.0.0")
HTTP_PORT = _int("ATMOSIS_HTTP_PORT", 7000)

FAILSAFE_CO_PPM = _float("ATMOSIS_FAILSAFE_CO_PPM", 9.0)
ALERT_COOLDOWN_S = _float("ATMOSIS_ALERT_COOLDOWN_S", 900.0)

DASHBOARD_URL = _str("ATMOSIS_DASHBOARD_URL", "")
UTC_OFFSET_MIN = _int("ATMOSIS_UTC_OFFSET_MIN", 330)
NOTIFY_SEND_STARTUP = _bool("ATMOSIS_NOTIFY_STARTUP", True)
NOTIFY_LINK_LOST_S = _float("ATMOSIS_NOTIFY_LINK_LOST_S", 120.0)
NOTIFY_SCORE_ALERT = _float("ATMOSIS_NOTIFY_SCORE_ALERT", 35.0)
NOTIFY_SCORE_RECOVERY = _float("ATMOSIS_NOTIFY_SCORE_RECOVERY", 55.0)
NOTIFY_SCORE_COOLDOWN_S = _float("ATMOSIS_NOTIFY_SCORE_COOLDOWN_S", 1800.0)
NOTIFY_SCORE_RECOVERY_COOLDOWN_S = _float("ATMOSIS_NOTIFY_SCORE_RECOVERY_COOLDOWN_S", 300.0)
NOTIFY_CLASS_DWELL_S = _float("ATMOSIS_NOTIFY_CLASS_DWELL_S", 60.0)
NOTIFY_CLASS_COOLDOWN_S = _float("ATMOSIS_NOTIFY_CLASS_COOLDOWN_S", 1800.0)
NOTIFY_DAILY_HOUR = _int("ATMOSIS_NOTIFY_DAILY_HOUR", 21)

CLASS_VOTE_WINDOWS = _int("ATMOSIS_CLASS_VOTE_WINDOWS", 3)
MODEL_DIRS = [p for p in (
    _str("ATMOSIS_MODEL_DIR", ""),
    str(HERE / "model"),
) if p]
USE_UNTRAINED_MODEL = _bool("ATMOSIS_USE_UNTRAINED_MODEL", False)

DATA_DIR = Path(_str("ATMOSIS_DATA_DIR", str(HERE / "data")))
HISTORY_FINE_STEP_S = 2
HISTORY_FINE_SPAN_S = 3600
HISTORY_COARSE_STEP_S = 60
HISTORY_COARSE_SPAN_S = 86400

DEFAULT_HEALTH_PROFILE = {
    "age": _int("ATMOSIS_PROFILE_AGE", 34),
    "asthma": _bool("ATMOSIS_PROFILE_ASTHMA", False),
    "heart": _bool("ATMOSIS_PROFILE_HEART", False),
    "pregnant": _bool("ATMOSIS_PROFILE_PREGNANT", False),
}
