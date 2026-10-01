#pragma once
#include "Arduino.h"
#include "Wire.h"

typedef enum { DPS310_IDLE = 0, DPS310_CONT_PRESTEMP = 7 } dps310_mode_t;
typedef enum { DPS310_1HZ, DPS310_2HZ, DPS310_4HZ, DPS310_8HZ } dps310_rate_t;
typedef enum { DPS310_1SAMPLE, DPS310_2SAMPLES, DPS310_4SAMPLES, DPS310_8SAMPLES, DPS310_16SAMPLES } dps310_oversample_t;

struct sensors_event_t { float temperature; float pressure; };

namespace sim { extern int dps_begin_calls; }

class Adafruit_DPS310 {
public:
  bool begin_I2C(uint8_t addr, TwoWire* wire) {
    sim::dps_begin_calls++;
    auto it = wire->devices.find(addr);
    if (it == wire->devices.end() || it->second->readReg(0x0D) != 0x10) return false;
    dev_ = it->second;
    uint8_t c[18];
    for (int i = 0; i < 18; ++i) c[i] = dev_->readReg(0x10 + i);
    c0_  = sx(((uint32_t)c[0] << 4) | (c[1] >> 4), 12);
    c1_  = sx((((uint32_t)c[1] & 0x0F) << 8) | c[2], 12);
    c00_ = sx(((uint32_t)c[3] << 12) | ((uint32_t)c[4] << 4) | (c[5] >> 4), 20);
    c10_ = sx((((uint32_t)c[5] & 0x0F) << 16) | ((uint32_t)c[6] << 8) | c[7], 20);
    int32_t* rest[5] = {&c01_, &c11_, &c20_, &c21_, &c30_};
    for (int i = 0; i < 5; ++i) *rest[i] = sx(((uint32_t)c[8 + 2 * i] << 8) | c[9 + 2 * i], 16);
    return true;
  }
  void setMode(dps310_mode_t m) { if (dev_) dev_->writeReg(0x08, (uint8_t)m); }
  void configurePressure(dps310_rate_t, dps310_oversample_t) {}
  void configureTemperature(dps310_rate_t, dps310_oversample_t) {}
  bool getEvents(sensors_event_t* t, sensors_event_t* p) {
    if (!dev_) return false;
    int32_t praw = sx(((uint32_t)dev_->readReg(0) << 16) | ((uint32_t)dev_->readReg(1) << 8) | dev_->readReg(2), 24);
    int32_t traw = sx(((uint32_t)dev_->readReg(3) << 16) | ((uint32_t)dev_->readReg(4) << 8) | dev_->readReg(5), 24);
    double ts = traw / 253952.0, ps = praw / 253952.0;
    double temp = ts * c1_ + c0_ / 2.0;
    double pres = c00_ + ps * (c10_ + ps * (c20_ + ps * c30_)) + ts * (c01_ + ps * (c11_ + ps * c21_));
    if (t) t->temperature = (float)temp;
    if (p) p->pressure = (float)(pres / 100.0);
    return true;
  }
private:
  I2CDevice* dev_ = nullptr;
  int32_t c0_ = 0, c1_ = 0, c00_ = 0, c10_ = 0, c01_ = 0, c11_ = 0, c20_ = 0, c21_ = 0, c30_ = 0;
  static int32_t sx(uint32_t v, int bits) { uint32_t m = 1u << (bits - 1); return (int32_t)((v ^ m) - m); }
};
