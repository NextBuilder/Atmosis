#pragma once
#include "Arduino.h"
typedef enum { DPS310_IDLE = 0, DPS310_CONT_PRESTEMP = 7 } dps310_mode_t;
typedef enum { DPS310_1HZ, DPS310_2HZ, DPS310_4HZ } dps310_rate_t;
typedef enum { DPS310_1SAMPLE, DPS310_2SAMPLES, DPS310_4SAMPLES, DPS310_8SAMPLES, DPS310_16SAMPLES } dps310_oversample_t;
typedef struct { int32_t version; int32_t sensor_id; int32_t type; int32_t reserved0; int32_t timestamp; union { float data[4]; float temperature; float pressure; }; } sensors_event_t;
class Adafruit_DPS310 {
public:
  Adafruit_DPS310();
  ~Adafruit_DPS310();
  bool begin_I2C(uint8_t i2c_addr, TwoWire* wire);
  void setMode(dps310_mode_t mode);
  void configurePressure(dps310_rate_t rate, dps310_oversample_t os);
  void configureTemperature(dps310_rate_t rate, dps310_oversample_t os);
  bool getEvents(sensors_event_t* temp_event, sensors_event_t* pressure_event);
};
