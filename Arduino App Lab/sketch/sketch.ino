#include <Arduino_RouterBridge.h>
#include "config.h"
#include "sensors.h"
#include "status_led.h"
#include "air_score.h"

namespace {

EnvironmentX6  x6Sensor;
DustSensor     dustSensor;
PressureSensor pressureSensor;
StatusLed      statusLed;

X6Reading   lastX6;
DustReading lastDust;
bool        dustAlarm       = false;
bool        coAlarm         = false;
uint32_t    frameSeq        = 0;
uint32_t    nextFrameMs     = 0;
uint32_t    nextInfoMs      = 0;
uint32_t    loopStartMs     = 0;
uint32_t    loopMaxMs       = 0;
uint32_t    loopMaxWindowMs = 0;
uint32_t    loopMaxReported = 0;

int pressureChipCode() {
  return pressureSensor.detected() ? 3 : 0;
}

int statusFlags() {
  int f = 0;
  if (lastX6.valid)                   f |= FLAG_X6;
  if (pressureSensor.reading().valid) f |= FLAG_PRESSURE;
  if (lastDust.valid)                 f |= FLAG_DUST;
  if (lastDust.timing_ok)             f |= FLAG_DUST_TIME;
  if (dustAlarm)                      f |= FLAG_FAILSAFE;
  if (coAlarm)                        f |= FLAG_CO_ALARM;
  if (statusLed.ready())              f |= FLAG_LED;
  return f;
}

void updateAlarmsAndLight() {
  if (lastX6.valid) {
    if (lastX6.co_ppm >= CO_ALERT_PPM) coAlarm = true;
    else if (lastX6.co_ppm <= CO_ALERT_CLEAR_PPM) coAlarm = false;
  }
  statusLed.setAlarm(dustAlarm || coAlarm);
  const int score = airScore(lastX6.valid, lastX6.iaq, lastX6.tvoc_ppm, lastX6.hcho_ppm, lastX6.co_ppm,
                             lastDust.valid, lastDust.density_ugm3);
  uint8_t r, g, b;
  scoreColor(score, r, g, b);
  statusLed.setColor(r, g, b);
}

void pushFrame(uint32_t now) {
  const PressureReading& p = pressureSensor.reading();
  Bridge.notify("atmosis_frame",
                (int)FRAME_PROTOCOL,
                (int)(++frameSeq),
                (int)(now / 1000UL),
                statusFlags(),
                lastX6.iaq,
                lastX6.tvoc_ppm,
                lastX6.hcho_ppm,
                lastX6.co_ppm,
                lastX6.temperature_c,
                lastX6.humidity_pct,
                lastDust.density_ugm3,
                (int)(lastDust.sensor_v * 1000.0f + 0.5f),
                (int)DUST_ZERO_MV,
                (int)lastDust.status,
                p.pressure_hpa,
                p.altitude_m,
                p.temperature_c,
                pressureChipCode(),
                (int)loopMaxReported);
}

void pushInfo() {
  Bridge.notify("atmosis_info",
                (int)FRAME_PROTOCOL,
                (int)FIRMWARE_CODE,
                (int)x6Sensor.framesOk(),
                (int)x6Sensor.framesBad(),
                (int)x6Sensor.status(),
                (int)pressureSensor.status(),
                pressureChipCode(),
                (int)pressureSensor.address(),
                (int)pressureSensor.idByte(),
                (int)pressureSensor.attempts(),
                (int)lastDust.status,
                (int)(lastDust.pin_mv * 10.0f + 0.5f),
                1,
                statusLed.ready() ? 1 : 0,
                (int)WS2812B_LED_COUNT);
}

void trackLoopTime(uint32_t now) {
  const uint32_t elapsed = now - loopStartMs;
  loopStartMs = now;
  if (elapsed > loopMaxMs) loopMaxMs = elapsed;
  if ((now - loopMaxWindowMs) >= 5000) {
    loopMaxReported = loopMaxMs;
    loopMaxMs = 0;
    loopMaxWindowMs = now;
  }
}

}

void setup() {
  Bridge.begin();
  analogReadResolution(ADC_RESOLUTION_BITS);
  x6Sensor.begin();
  dustSensor.begin();
  pressureSensor.begin();
  statusLed.begin();

  const uint32_t now = millis();
  loopStartMs     = now;
  loopMaxWindowMs = now;
  nextFrameMs     = now + FRAME_INTERVAL_MS;
  nextInfoMs      = now + INFO_FIRST_MS;
}

void loop() {
  const uint32_t now = millis();
  trackLoopTime(now);

  statusLed.update(now);
  x6Sensor.poll(now);
  lastX6 = x6Sensor.reading(now);
  dustSensor.poll(now);
  pressureSensor.update(now);

  DustReading dust;
  if (dustSensor.consume(dust)) {
    lastDust = dust;
    if (dust.valid) {
      if (dust.density_ugm3 >= DUST_ALERT_UGM3) dustAlarm = true;
      else if (dust.density_ugm3 <= DUST_ALERT_CLEAR_UGM3) dustAlarm = false;
    } else if (dust.status == DUST_STATUS_SATURATED) {
      dustAlarm = true;
    } else if (dust.status == DUST_STATUS_NO_SIGNAL) {
      dustAlarm = false;
    }
  }

  if ((int32_t)(now - nextFrameMs) >= 0) {
    nextFrameMs += FRAME_INTERVAL_MS;
    if ((int32_t)(now - nextFrameMs) >= 0) nextFrameMs = now + FRAME_INTERVAL_MS;
    updateAlarmsAndLight();
    pushFrame(now);
  }

  if ((int32_t)(now - nextInfoMs) >= 0) {
    nextInfoMs = now + INFO_INTERVAL_MS;
    pushInfo();
  }

  delay(LOOP_YIELD_MS);
}
