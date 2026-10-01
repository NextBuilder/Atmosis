# Atmosis

Premium indoor air intelligence for the **Arduino UNO Q** — a live glass-style dashboard, on-device pattern recognition, a Gemini health advisor, and polished Telegram alerts.

```
sketch/     Microcontroller firmware — sensors, light strip, dust alarm
python/     App — dashboard, AI advisor, Telegram, Edge Impulse model
scripts/    Firmware tests (simulator + Cortex-M33 build check)
```

---

## Components

| Part | Link |
|---|---|
| Arduino UNO Q | [Amazon](https://www.amazon.com/ABX00173-Dragonwing-microprocessor-STM32U585-Microcontroller/dp/B0GFN669S4/) |
| DPS310 pressure sensor | [Amazon](https://www.amazon.com/Industrial-Temperature-Supporting-Microcontrollers-Measurement/dp/B0H5NQJ7RR/) |
| Waveshare Environment X6 | [Waveshare](https://www.waveshare.com/environment-x6-sensor.htm?&aff_id=135301) |
| Waveshare Dust Sensor | [Waveshare](https://www.waveshare.com/dust-sensor.htm?&aff_id=135301) |
| Waveshare RGB COB strip (WS2812B) | [Waveshare](https://www.waveshare.com/rgb-27-5v-160d.htm?sku=34160?&aff_id=135301) |

## Wiring

| Waveshare X6 | UNO Q | | Dust sensor | UNO Q |
|---|---|---|---|---|
| VCC | 5V | | VCC | 5V |
| GND | GND | | GND | GND |
| TXD | D0 | | ILED | D4 |
| RXD | D1 | | AOUT | A0 |

| SmartElex DPS310 | UNO Q | | RGB strip | UNO Q |
|---|---|---|---|---|
| VIN | 3.3V | | DIN | D5 |
| GND | GND | | VCC | 5V |
| **SDI** | SDA | | GND | GND |
| **SCK** | SCL | | | |
| SDO, CS | not connected | | | |

The SmartElex board labels its I2C pins **SCK** (clock) and **SDI** (data); SDO and CS stay unconnected. The DPS310 is read with the **Adafruit DPS310 library** on the header SDA/SCL bus (`Wire`) — exactly the setup from the data-collection sketch: address 0x77 or 0x76, the Infineon temperature fix, 4 Hz × 16 samples, and `Wire.begin()` re-run on every retry (every 10 s) so a late or reconnected sensor is picked up.

**Light strip:** the firmware drives the first **30 LEDs** at low brightness. A full metre of this strip draws up to 12 W — far more than the UNO Q's 5V pin should supply — so keep it short, or power a longer strip from its own 5V supply (shared GND). Change `WS2812B_LED_COUNT` in `sketch/config.h` (max 60).

---

## Set up

1. **Keys** — fill in `python/.env`. An empty line switches that feature off.

   | Key | Where to get it |
   |---|---|
   | `GEMINI_API_KEY` | aistudio.google.com/apikey |
   | `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
   | `TELEGRAM_CHAT_ID` | Telegram → @userinfobot |

   Open your new bot in Telegram and press **Start**, or it cannot message you.

2. **Run** — import the folder into Arduino App Lab and press Run.
3. **Open** — `http://<board-ip>:7000` on any phone or laptop on the same Wi-Fi.

---

## How it works

The board reads every sensor and **pushes one frame per second** to Python with `Bridge.notify` — the same one-way pattern as Arduino's official climate-monitoring example. **Nothing is ever sent to the board**: it computes the air score, the light-strip colour and the dust / carbon-monoxide alarms itself, so the light keeps working even when the app is closed.

Why one-way: in the UNO Q Bridge library, every message sent *to* the board is handled on a 500-byte thread stack with no overflow protection. Earlier versions sent LED and heartbeat messages to the board, and the link failed after a few minutes. A board-side restart is not a fix either — after any reset the UNO Q waits for App Lab before it starts the sketch again.

| Sensor | What Atmosis shows | Source of the maths |
|---|---|---|
| Environment X6 | IAQ, VOCs, formaldehyde, CO, temperature, humidity | Waveshare protocol (0x70 query, verified against their example frame) |
| Dust sensor | PM2.5 | Waveshare's official method: LED on, sample at 280 µs, 11:1 divider, fixed 400 mV zero, 0.2 µg/m³ per mV, average of the last 10 readings, 0–500 µg/m³ |
| DPS310 | Pressure, altitude | Adafruit DPS310 library 1.1.6 (same as the data-collection sketch) |

**Air score** is the worst of the pollutant sub-scores (PM2.5, CO, VOCs, formaldehyde, IAQ), using WHO / EPA indoor breakpoints.

## Telegram

Every message is a card: headline, one-line summary, score bar, full **Air quality** and **Climate** tables with a status for each reading, one clear action, and a link to the dashboard.

| When | Message |
|---|---|
| App starts | Atmosis is online |
| Air score falls to 35 / recovers above 55 | Air quality needs attention / has recovered |
| PM2.5 over 150 µg/m³ | Very high dust level / back to safe |
| CO over 9 ppm | Carbon monoxide alert / back to safe |
| AI sees pollution or stale air | Pattern alert (at most every 30 min) |
| Air stays poor | Air still needs attention (at most once an hour) |

Air, pattern and reminder alerts are spaced at least 10 minutes apart, and dust alarms at least 15 minutes, so a reading hovering around a threshold never floods your chat.
| Board silent for 2 min | Sensors are not responding / reconnected |
| Every evening at 9 PM | Daily air report — averages, peaks and the worst time of day |

Air, dust, pattern and reminder alerts include a **✦ Gemini suggests** tip from Gemini 3.5 Flash. Carbon-monoxide alerts go out instantly, without waiting for Gemini. Set `ATMOSIS_NOTIFY_DAILY_HOUR=-1` in `.env` to turn the daily report off.

## Gemini

Everything in the Health advisor comes from Gemini 3.5 Flash. One recommendation card updates in place — no stacking, no repeats:

- It is written when the air starts needing attention, refreshed only if the air gets **worse** (at most every 5 minutes) or after 30 minutes, and Gemini is shown its previous advice so it never repeats itself. Near-identical text is not shown as new.
- When the air is healthy the card simply says **All clear**.
- Your questions are answered by Gemini. If Google is busy, the answer arrives in the same bubble a moment later (retried for up to 4 minutes).
- If Google is busy, Atmosis retries on its own after 30 s, 1, 2, then 5 minutes.

## Edge Impulse

Atmosis runs Edge Impulse models directly in Python — no compiling. Export your impulse as a **C++ library** with the EON Compiler on, copy `model_metadata.h`, `model_variables.h` and `tflite_learn_*_compiled.cpp` into `python/model/`, and restart the app.

At start-up Atmosis checks the model on a set of known situations (clean air, solvent fumes, a stuffy room, smoke). When the model tells them apart, it drives the air-pattern card, Telegram pattern alerts and the startup banner, and every one of them says **Edge Impulse**. Otherwise the Atmosis pattern engine (WHO / EPA thresholds) drives them. Models trained on raw sensor units or on 0–1 scaled columns (names ending in `_n`) both work.

## Settings (`python/.env`)

| Setting | Default | Meaning |
|---|---|---|
| `ATMOSIS_AI_PROVIDER` | `auto` | `off` disables the cloud advisor |
| `GEMINI_MODEL` | `gemini-3.5-flash` | The only model used, with minimal thinking for speed. Called only when your air needs attention, or when you ask a question |
| `ATMOSIS_NOTIFY_REMINDER_S` | `3600` | While air stays poor, a reminder with a fresh Gemini tip at most this often |
| `ATMOSIS_FAILSAFE_CO_PPM` | `9` | CO alarm level |
| `ATMOSIS_NOTIFY_DAILY_HOUR` | `21` | Hour for the daily report, `-1` to disable |
| `ATMOSIS_UTC_OFFSET_MIN` | `330` | Time zone for message times (India) |
| `ATMOSIS_DASHBOARD_URL` | automatic | Link used in Telegram messages |
| `ATMOSIS_SIMULATE` | `0` | `1` runs everything on realistic fake data |

## Verify

```bash
./scripts/check_firmware.sh            # firmware simulator + Cortex-M33 build check
cd python && python3 test_atmosis.py   # app tests
ATMOSIS_SIMULATE=1 python3 main.py     # full app without hardware
```

## Troubleshooting

| Dashboard shows | Check |
|---|---|
| Board offline | Press **Stop**, then **Run** in App Lab. Check the board still has power. |
| Dust: No signal | AOUT → A0, VCC → **5V** (3.3V is not enough for this sensor) |
| DPS310: Not found | SCK → SCL, SDI → SDA, VIN → 3.3V, GND. Unplug the board's power for 10 s (the DPS310 only picks I2C at power-up), then Run. Atmosis retries every 10 s. |
| Advisor chip says "reconnecting" | Google's Gemini service is busy (HTTP 503 or slow). Atmosis retries on its own and the chip turns green again |
| Gas sensor: No data | X6 TXD → D0 and RXD → D1 (crossed), VCC → 5V |
| No readings at all from the first second | A known issue on some UNO Q cores: `Serial1.begin()` (needed by the X6) can break the Bridge — see github.com/arduino-libraries/Arduino_RouterBridge/issues/76 |

Your API key and bot token live in `python/.env` — keep it out of anything you publish.

**Libraries** (`sketch/sketch.yaml`): Adafruit DPS310 1.1.6, Adafruit BusIO 1.17.3, Adafruit Unified Sensor 1.1.15, Adafruit NeoPixel 1.15.5. App Lab installs them automatically.
