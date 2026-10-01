from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config

_TZ = timezone(timedelta(minutes=config.UTC_OFFSET_MIN))
ICONS = {"good": "✓", "info": "•", "warn": "▲", "bad": "✕", "ai": "✦"}


def log(level: str, text: str) -> None:
    print(f"  {datetime.now(_TZ).strftime('%H:%M:%S')}  {ICONS.get(level, '•')}  {text}", flush=True)
