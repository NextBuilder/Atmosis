# Atmosis

**Indoor air quality you can see, understand and act on — running on the Arduino UNO Q.**

Most homes have a smoke alarm for the worst day and nothing for every other day. Atmosis watches the air in a room every second — fine dust, carbon monoxide, VOCs and formaldehyde — turns it into a single air score, recognises what is happening with an Edge Impulse model, and tells you what to do about it.

- **Real-time sensing** — nine readings per second from three sensors
- **On-board safety** — the air score, light strip and dust / CO alarms run on the microcontroller, independent of the app and the network
- **Edge Impulse** — recognises seven air conditions, from clean air and poor ventilation to traffic pollution and biomass smoke
- **Gemini 3.5 Flash advisor** — short, specific advice, only when the air needs attention
- **Telegram alerts** — structured cards with rate limiting, plus a daily report
- **Live dashboard** — on any phone or laptop on the same network

---

## Architecture

```
 Environment X6 ─┐
 Dust sensor    ─┼─►  STM32U585 · Zephyr  ──── Bridge.notify, 1 Hz ────►  QRB2210 · Linux
 DPS310         ─┘    air score · alarms                                   Edge Impulse inference
                      WS2812B light strip                                  Gemini advisor · Telegram
                                                                           Dashboard on :7000
```

```
sketch/    Firmware — sensor drivers, air score, light strip, alarms
python/    App — dashboard, Edge Impulse inference, Gemini advisor, Telegram
scripts/   Firmware simulator tests and Cortex-M33 build check
```

---

## Hardware

| Part | Link |
|---|---|
| Arduino UNO Q | [Amazon](https://www.amazon.com/ABX00173-Dragonwing-microprocessor-STM32U585-Microcontroller/dp/B0GFN669S4/) |
| DPS310 pressure sensor | [Amazon](https://www.amazon.com/Industrial-Temperature-Supporting-Microcontrollers-Measurement/dp/B0H5NQJ7RR/) |
| Waveshare Environment X6 | [Waveshare](https://www.waveshare.com/environment-x6-sensor.htm?&aff_id=135301) |
| Waveshare Dust Sensor | [Waveshare](https://www.waveshare.com/dust-sensor.htm?&aff_id=135301) |
| Waveshare RGB COB strip (WS2812B) | [Waveshare](https://www.waveshare.com/rgb-27-5v-160d.htm?sku=34160?&aff_id=135301) |

---

## Getting started

**1. Add your keys** to `python/.env`. Any feature left on its placeholder stays switched off.

| Key | Where to get it |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | Telegram → @userinfobot |

Open your new bot in Telegram and press **Start**, otherwise it cannot message you.

**2. Run** — import this folder into Arduino App Lab and press **Run**.

**3. Open** — `http://<board-ip>:7000` from any device on the same Wi-Fi.

---

## How it works

### Sensing

| Sensor | Readings | Method |
|---|---|---|
| Environment X6 | IAQ, VOCs, formaldehyde, CO, temperature, humidity | Waveshare UART protocol (`0x70` query, checksum-verified 22-byte frame) |
| Dust sensor | PM2.5 | Waveshare reference method — sample 280 µs after the LED pulse, 11:1 divider, 400 mV zero, 0.2 µg/m³ per mV, 10-reading average |
| DPS310 | Pressure, altitude | Adafruit DPS310 library, 4 Hz × 16 oversampling, automatic retry every 10 s |

The **air score** (1–100) is the lowest of the pollutant sub-scores — PM2.5, CO, VOCs, formaldehyde and IAQ — using WHO / EPA indoor breakpoints.

### Board ↔ Linux link

The microcontroller pushes one frame per second to Python with `Bridge.notify`, and nothing is ever sent back. On the UNO Q, messages addressed to the board are handled on a small thread stack; keeping the link one-way removes that failure mode entirely and keeps the board fully self-sufficient — the light strip and alarms keep working even when the app is stopped.

### Edge Impulse

Atmosis uses an Edge Impulse classifier trained on real data recorded with this exact sensor setup. A dedicated data-collection sketch streamed all nine readings as CSV at 5 Hz, and each class was captured under genuine, safe conditions.

| Class | Condition |
|---|---|
| `Clean_air` | Normal, relatively clean indoor air, recorded at different times and locations to set the baseline |
| `Traffic_pollution` | Short recordings near a busy road with passing traffic |
| `Construction_dust` | Genuine construction or dusty environments, recorded only when safely available |
| `Indoor_pollution` | Brief, controlled household sources such as cooking or incense |
| `Poor_ventilation` | Closed rooms with limited airflow, where air quality degrades gradually |
| `Biomass_burning` | Naturally occurring burning or smoke, captured only when safe |
| `Unusual_event` | Genuine anomalies that don't fit the other classes |

**Impulse:** nine time-series axes (`iaq`, `tvoc_ppm`, `hcho_ppm`, `co_ppm`, `temp_c`, `rh_pct`, `dust_ugm3`, `pressure_hpa`, `altitude_m`) → Raw Data block → dense neural-network classifier → int8, EON Compiler.

**On the board:** Atmosis reads the exported C++ library directly in Python — no compile step. A rolling window of live readings is classified once per frame, and a new label must win three windows in a row and hold for 60 seconds before anything changes. The model is verified against reference situations at start-up; if the files are missing or fail the check, a threshold-based pattern engine takes over.

To update the model, copy `model_metadata.h`, `model_variables.h` and `tflite_learn_*_compiled.cpp` into `python/model/` and restart.

### Gemini advisor

The Health advisor is written by Gemini 3.5 Flash when the air starts needing attention, refreshed only if it gets worse (at most every 5 minutes) or after 30 minutes. Gemini sees its previous advice so it doesn't repeat itself; when the air is healthy the card simply reads **All clear**. If the service is busy, Atmosis backs off and retries on its own.

### Telegram

| Trigger | Message |
|---|---|
| App starts | Atmosis is online |
| Air score ≤ 35 / recovers above 55 | Air quality needs attention / has recovered |
| PM2.5 above 150 µg/m³ | Very high dust level / back to safe |
| CO above 9 ppm | Carbon monoxide alert — sent immediately |
| Edge Impulse detects indoor pollution or poor ventilation | Pattern alert, at most every 30 minutes |
| Air stays poor | Reminder, at most once an hour |
| Board silent for 2 minutes | Sensors not responding / reconnected |
| Daily at 9 PM | Air report — averages, peaks and the worst time of day |

Alerts are spaced at least 10 minutes apart (dust: 15), so a reading hovering near a threshold never floods the chat.

---

## Configuration

Optional settings in `python/.env`:

| Setting | Default | Description |
|---|---|---|
| `ATMOSIS_AI_PROVIDER` | `auto` | `off` disables the Gemini advisor |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Model used for advice and questions |
| `ATMOSIS_FAILSAFE_CO_PPM` | `9` | Carbon-monoxide alert level |
| `ATMOSIS_NOTIFY_REMINDER_S` | `3600` | Minimum gap between "still poor" reminders |
| `ATMOSIS_NOTIFY_DAILY_HOUR` | `21` | Hour of the daily report (`-1` to disable) |
| `ATMOSIS_UTC_OFFSET_MIN` | `330` | Time zone for message timestamps |
| `ATMOSIS_DASHBOARD_URL` | auto | Dashboard link used in Telegram messages |
| `ATMOSIS_SIMULATE` | `0` | `1` runs the full app on simulated data |

---

## Testing

```bash
./scripts/check_firmware.sh             # firmware simulator + Cortex-M33 build check
cd python && python3 test_atmosis.py    # application tests
ATMOSIS_SIMULATE=1 python3 main.py      # full app without hardware
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Board offline | Press **Stop**, then **Run** in App Lab, and check the board has power |
| Gas sensor: no data | X6 `TXD → D0`, `RXD → D1` (crossed), `VCC → 5V` |
| Dust: no signal | `AOUT → A0`, `VCC → 5V` — 3.3V is not enough for this sensor |
| DPS310 not found | Check `SCK → SCL`, `SDI → SDA`, `VIN → 3.3V`; power-cycle the board for 10 s, then Run |
| Advisor shows "reconnecting" | Gemini is busy or slow — Atmosis retries automatically |
| No readings from the first second | Known issue on some UNO Q cores where `Serial1.begin()` affects the Bridge — see [Arduino_RouterBridge#76](https://github.com/arduino-libraries/Arduino_RouterBridge/issues/76) |

---

## Dependencies

Installed automatically by App Lab from `sketch/sketch.yaml`:
Adafruit DPS310 1.1.6 · Adafruit BusIO 1.17.3 · Adafruit Unified Sensor 1.1.15 · Adafruit NeoPixel 1.15.5
