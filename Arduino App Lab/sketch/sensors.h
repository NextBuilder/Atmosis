#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_DPS310.h>
#include <string.h>
#include "config.h"

enum X6Status : uint8_t { X6_STARTING = 0, X6_OK = 1, X6_TIMEOUT = 2, X6_CHECKSUM = 3, X6_OUT_OF_RANGE = 4 };
enum PressureStatus : uint8_t { P_NOT_DETECTED = 0, P_OK = 1, P_INIT_FAILED = 2, P_UNKNOWN_CHIP = 3, P_LOST = 4 };

struct X6Reading {
  float iaq           = 0;
  float tvoc_ppm      = 0;
  float hcho_ppm      = 0;
  float co_ppm        = 0;
  float temperature_c = 0;
  float humidity_pct  = 0;
  bool  valid         = false;
};

class EnvironmentX6 {
public:
  void begin() {
    X6_SERIAL.begin(X6_BAUD);
    drain();
    state_ = State::Idle;
  }

  void poll(uint32_t now) {
    if (state_ == State::AwaitingFrame) {
      collect();
      if (state_ == State::AwaitingFrame && (now - queryAtMs_) > X6_FRAME_TIMEOUT_MS) {
        state_ = State::Idle;
        timeouts_++;
        status_ = X6_TIMEOUT;
      }
      return;
    }
    if ((now - lastQueryMs_) < X6_POLL_INTERVAL_MS) return;
    sendQuery(now);
  }

  X6Reading reading(uint32_t now) const {
    X6Reading r = last_;
    if (r.valid && (now - lastGoodMs_) > X6_STALE_AFTER_MS) r.valid = false;
    return r;
  }

  uint8_t     status()    const { return status_; }
  uint32_t    framesOk()  const { return framesOk_; }
  uint32_t    framesBad() const { return framesBad_ + timeouts_; }

private:
  enum class State : uint8_t { Idle, AwaitingFrame };

  State       state_       = State::Idle;
  uint8_t     buffer_[X6_RESPONSE_LEN] = {0};
  uint8_t     filled_      = 0;
  uint32_t    lastQueryMs_ = 0;
  uint32_t    queryAtMs_   = 0;
  uint32_t    lastGoodMs_  = 0;
  uint32_t    framesOk_    = 0;
  uint32_t    framesBad_   = 0;
  uint32_t    timeouts_    = 0;
  uint8_t     status_      = X6_STARTING;
  X6Reading   last_;

  void drain() {
    while (X6_SERIAL.available() > 0) X6_SERIAL.read();
  }

  static uint8_t checksum(const uint8_t* data, size_t len) {
    uint32_t sum = 0;
    for (size_t i = 0; i < len; ++i) sum += data[i];
    return (uint8_t)((~(sum & 0xFF) + 1) & 0xFF);
  }

  static float bigEndianFloat(const uint8_t* p) {
    const uint32_t bits = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
                          ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
    float value;
    memcpy(&value, &bits, sizeof(value));
    return value;
  }

  void sendQuery(uint32_t now) {
    drain();
    const uint8_t command[2] = { X6_CMD_CONCENTRATION, checksum(&X6_CMD_CONCENTRATION, 1) };
    X6_SERIAL.write(command, sizeof(command));
    filled_      = 0;
    queryAtMs_   = now;
    lastQueryMs_ = now;
    state_       = State::AwaitingFrame;
  }

  void collect() {
    while (X6_SERIAL.available() > 0) {
      const int byteIn = X6_SERIAL.read();
      if (byteIn < 0) break;
      if (filled_ == 0 && (uint8_t)byteIn != X6_CMD_CONCENTRATION) continue;
      buffer_[filled_++] = (uint8_t)byteIn;
      if (filled_ < X6_RESPONSE_LEN) continue;
      filled_ = 0;
      state_  = State::Idle;
      decode();
      return;
    }
  }

  void decode() {
    uint32_t sum = 0;
    for (uint8_t i = 0; i < X6_RESPONSE_LEN; ++i) sum += buffer_[i];
    if ((sum & 0xFF) != 0) {
      framesBad_++;
      status_ = X6_CHECKSUM;
      return;
    }
    X6Reading r;
    r.iaq           = bigEndianFloat(&buffer_[1]);
    r.tvoc_ppm      = bigEndianFloat(&buffer_[5]);
    r.hcho_ppm      = bigEndianFloat(&buffer_[9]);
    r.co_ppm        = bigEndianFloat(&buffer_[13]);
    r.temperature_c = (float)(int16_t)(((uint16_t)buffer_[17] << 8) | buffer_[18]) / 100.0f;
    r.humidity_pct  = (float)(((uint16_t)buffer_[19] << 8) | buffer_[20]) / 100.0f;
    if (!plausible(r)) {
      framesBad_++;
      status_ = X6_OUT_OF_RANGE;
      return;
    }
    r.valid     = true;
    last_       = r;
    lastGoodMs_ = millis();
    framesOk_++;
    status_     = X6_OK;
  }

  static bool plausible(const X6Reading& r) {
    if (!isfinite(r.iaq) || !isfinite(r.tvoc_ppm) || !isfinite(r.hcho_ppm) || !isfinite(r.co_ppm)) return false;
    if (r.iaq < 0 || r.iaq > X6_IAQ_MAX) return false;
    if (r.tvoc_ppm < 0 || r.tvoc_ppm > X6_TVOC_MAX) return false;
    if (r.hcho_ppm < 0 || r.hcho_ppm > X6_HCHO_MAX) return false;
    if (r.co_ppm < 0 || r.co_ppm > X6_CO_MAX) return false;
    if (r.temperature_c < X6_TEMP_MIN || r.temperature_c > X6_TEMP_MAX) return false;
    if (r.humidity_pct < 0 || r.humidity_pct > 100) return false;
    return true;
  }
};

enum DustStatus : uint8_t {
  DUST_STATUS_STARTING  = 0,
  DUST_STATUS_OK        = 1,
  DUST_STATUS_NO_SIGNAL = 2,
  DUST_STATUS_SATURATED = 3,
};

struct DustReading {
  float   density_ugm3 = 0;
  float   sensor_v     = 0;
  float   pin_mv       = 0;
  uint8_t status       = DUST_STATUS_STARTING;
  bool    valid        = false;
  bool    timing_ok    = true;
};

inline float dustDensityFromSensorMv(float sensorMv) {
  if (sensorMv <= DUST_ZERO_MV) return 0.0f;
  const float d = (sensorMv - DUST_ZERO_MV) * DUST_UGM3_PER_MV;
  return d > DUST_MAX_UGM3 ? DUST_MAX_UGM3 : d;
}

class DustSensor {
public:
  void begin() {
    pinMode(PIN_DUST_ILED, OUTPUT);
    digitalWrite(PIN_DUST_ILED, LOW);
    analogRead(PIN_DUST_AOUT);
    nextBatchAtMs_ = millis() + 500;
  }

  void poll(uint32_t nowMs) {
    if (!sampling_) {
      if ((int32_t)(nowMs - nextBatchAtMs_) < 0) return;
      count_ = 0; saturated_ = 0; failed_ = 0; timingOk_ = true; sampling_ = true;
    }
    if ((count_ + failed_) > 0 && (uint32_t)(micros() - lastPulseUs_) < DUST_PULSE_PERIOD_US) return;
    pulse();
    if ((count_ + failed_) >= DUST_SAMPLES) {
      finish();
      sampling_      = false;
      nextBatchAtMs_ = nowMs + DUST_BATCH_INTERVAL_MS;
    }
  }

  bool consume(DustReading& out) {
    if (!fresh_) return false;
    fresh_ = false;
    out = last_;
    return true;
  }

  static const char* statusName(uint8_t s) {
    switch (s) {
      case DUST_STATUS_OK:        return "OK";
      case DUST_STATUS_NO_SIGNAL: return "NO_SIGNAL";
      case DUST_STATUS_SATURATED: return "SATURATED";
      default:                    return "STARTING";
    }
  }

private:
  bool        sampling_      = false;
  bool        fresh_         = false;
  bool        timingOk_      = true;
  int         samples_[DUST_SAMPLES];
  uint8_t     count_         = 0;
  uint8_t     failed_        = 0;
  uint8_t     saturated_     = 0;
  float       history_[DUST_AVERAGE_WINDOW];
  uint8_t     historyCount_  = 0;
  uint8_t     historyNext_   = 0;
  uint32_t    lastPulseUs_   = 0;
  uint32_t    nextBatchAtMs_ = 0;
  DustReading last_;

  static void waitUntil(uint32_t start, uint32_t offset) {
    while ((uint32_t)(micros() - start) < offset) {}
  }

  void pulse() {
    digitalWrite(PIN_DUST_ILED, HIGH);
    const uint32_t start = micros();
    waitUntil(start, DUST_SAMPLE_DELAY_US);
    const int raw = analogRead(PIN_DUST_AOUT);
    digitalWrite(PIN_DUST_ILED, LOW);
    lastPulseUs_ = start;
    if ((uint32_t)(micros() - start) > DUST_PULSE_MAX_US) timingOk_ = false;
    if (raw < 0) { failed_++; return; }
    if (raw >= (int)ADC_MAX_COUNT - 2) saturated_++;
    samples_[count_++] = raw;
  }

  static float countsToMv(float counts) {
    return counts * ADC_VREF * 1000.0f / (float)ADC_MAX_COUNT;
  }

  void finish() {
    DustReading r;
    r.timing_ok = timingOk_;
    if (count_ < DUST_SAMPLES / 2) {
      r.status = DUST_STATUS_NO_SIGNAL;
      publish(r);
      return;
    }
    sortSamples();
    const uint8_t trim = count_ / 5;
    float sum = 0;
    for (uint8_t i = trim; i < count_ - trim; ++i) sum += samples_[i];
    r.pin_mv   = countsToMv(sum / (float)(count_ - 2 * trim));
    r.sensor_v = r.pin_mv / 1000.0f * DUST_VOLTAGE_GAIN;
    if (saturated_ > count_ / 4) {
      r.status = DUST_STATUS_SATURATED;
      r.density_ugm3 = DUST_MAX_UGM3;
    } else if (r.pin_mv < DUST_NO_SIGNAL_MV) {
      r.status = DUST_STATUS_NO_SIGNAL;
      historyCount_ = 0;
    } else {
      history_[historyNext_] = dustDensityFromSensorMv(r.sensor_v * 1000.0f);
      historyNext_ = (uint8_t)((historyNext_ + 1) % DUST_AVERAGE_WINDOW);
      if (historyCount_ < DUST_AVERAGE_WINDOW) historyCount_++;
      float total = 0;
      for (uint8_t i = 0; i < historyCount_; ++i) total += history_[i];
      r.density_ugm3 = total / (float)historyCount_;
      r.status = DUST_STATUS_OK;
      r.valid  = true;
    }
    publish(r);
  }

  void publish(const DustReading& r) {
    last_  = r;
    fresh_ = true;
  }

  void sortSamples() {
    for (uint8_t i = 1; i < count_; ++i) {
      const int value = samples_[i];
      int j = i;
      while (j > 0 && samples_[j - 1] > value) {
        samples_[j] = samples_[j - 1];
        --j;
      }
      samples_[j] = value;
    }
  }
};

struct PressureReading {
  float pressure_hpa  = 0;
  float temperature_c = 0;
  float altitude_m    = 0;
  bool  valid         = false;
};

inline float altitudeFromPressure(float hpa) {
  const float x = hpa / SEA_LEVEL_PRESSURE_HPA - 1.0f;
  float term = 1.0f, ratio = 1.0f;
  for (int n = 1; n <= 40; ++n) {
    term *= (0.1903f - (float)(n - 1)) * x / (float)n;
    ratio += term;
  }
  return 44330.0f * (1.0f - ratio);
}

class PressureSensor {
public:
  bool begin() {
    active_        = nullptr;
    address_       = 0;
    lastAttemptMs_ = millis();
    attempts_++;
    DPS310_WIRE.begin();
    DPS310_WIRE.setClock(I2C_CLOCK_HZ);
    const uint8_t addresses[2] = {0x77, 0x76};
    Adafruit_DPS310* drivers[2] = {&at77_, &at76_};
    status_ = P_NOT_DETECTED;
    for (uint8_t i = 0; i < 2; ++i) {
      uint8_t id = 0;
      if (!readRegister(addresses[i], 0x0D, id)) continue;
      idByte_ = id;
      if (id != 0x10) { status_ = P_UNKNOWN_CHIP; continue; }
      if (!drivers[i]->begin_I2C(addresses[i], &DPS310_WIRE)) { status_ = P_INIT_FAILED; continue; }
      drivers[i]->setMode(DPS310_IDLE);
      if (!correctTemperature(addresses[i])) { status_ = P_INIT_FAILED; return false; }
      drivers[i]->configurePressure(DPS310_4HZ, DPS310_16SAMPLES);
      drivers[i]->configureTemperature(DPS310_4HZ, DPS310_16SAMPLES);
      drivers[i]->setMode(DPS310_CONT_PRESTEMP);
      sensors_event_t discardT = {}, discardP = {};
      drivers[i]->getEvents(&discardT, &discardP);
      delay(300);
      active_   = drivers[i];
      address_  = addresses[i];
      failures_ = 0;
      status_   = P_OK;
      return true;
    }
    return false;
  }

  void update(uint32_t now) {
    if (active_ == nullptr) {
      if ((now - lastAttemptMs_) >= PRESSURE_RETRY_MS) begin();
      return;
    }
    if ((now - lastPollMs_) < PRESSURE_POLL_MS) return;
    uint8_t flags = 0;
    if (!readRegister(address_, 0x08, flags)) { fail(); return; }
    if ((flags & 0x30) != 0x30) return;
    lastPollMs_ = now;
    sensors_event_t t = {}, p = {};
    if (!active_->getEvents(&t, &p)) { fail(); return; }
    const float hpa = p.pressure, tc = t.temperature;
    if (!isfinite(hpa) || !isfinite(tc) || hpa < PRESSURE_MIN_HPA || hpa > PRESSURE_MAX_HPA || tc < -40.0f || tc > 85.0f) {
      fail();
      return;
    }
    reading_.pressure_hpa  = hpa;
    reading_.temperature_c = tc;
    reading_.altitude_m    = altitudeFromPressure(hpa);
    reading_.valid         = true;
    failures_ = 0;
  }

  const PressureReading& reading() const { return reading_; }
  bool     detected() const { return active_ != nullptr; }
  uint8_t  status()   const { return status_; }
  uint8_t  address()  const { return address_; }
  uint8_t  idByte()   const { return idByte_; }
  uint16_t attempts() const { return attempts_; }

private:
  Adafruit_DPS310  at77_, at76_;
  Adafruit_DPS310* active_        = nullptr;
  uint8_t          address_       = 0;
  uint8_t          idByte_        = 0xFF;
  uint8_t          failures_      = 0;
  uint8_t          status_        = P_NOT_DETECTED;
  uint16_t         attempts_      = 0;
  uint32_t         lastAttemptMs_ = 0;
  uint32_t         lastPollMs_    = 0;
  PressureReading  reading_;

  static bool readRegister(uint8_t address, uint8_t reg, uint8_t& value) {
    DPS310_WIRE.beginTransmission(address);
    DPS310_WIRE.write(reg);
    if (DPS310_WIRE.endTransmission() != 0) return false;
    if (DPS310_WIRE.requestFrom(address, (size_t)1) != 1) return false;
    value = (uint8_t)DPS310_WIRE.read();
    return true;
  }

  static bool writeRegister(uint8_t address, uint8_t reg, uint8_t value) {
    DPS310_WIRE.beginTransmission(address);
    DPS310_WIRE.write(reg);
    DPS310_WIRE.write(value);
    return DPS310_WIRE.endTransmission() == 0;
  }

  static bool correctTemperature(uint8_t address) {
    bool ok = writeRegister(address, 0x0E, 0xA5);
    ok = writeRegister(address, 0x0F, 0x96) && ok;
    ok = writeRegister(address, 0x62, 0x02) && ok;
    ok = writeRegister(address, 0x0E, 0x00) && ok;
    ok = writeRegister(address, 0x0F, 0x00) && ok;
    return ok;
  }

  void fail() {
    reading_.valid = false;
    if (++failures_ >= 3) {
      active_        = nullptr;
      status_        = P_LOST;
      lastAttemptMs_ = millis();
    }
  }
};
