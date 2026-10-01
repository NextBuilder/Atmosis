#pragma once
#include <stdint.h>
#include <stddef.h>
#include <math.h>
#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define INPUT 0
#define INPUT_PULLDOWN 3
#define A0 14
#define PI 3.1415926535897932384626433832795
extern "C" uint32_t millis(void);
extern "C" uint32_t micros(void);
extern "C" void delay(uint32_t);
extern "C" void pinMode(uint8_t, uint8_t);
extern "C" void digitalWrite(uint8_t, uint8_t);
extern "C" int digitalRead(uint8_t);
extern "C" int analogRead(uint8_t);
extern "C" void analogReadResolution(uint8_t);
class String { public: String(const char*); };
struct HardwareSerial { void begin(uint32_t); int available(); int read(); size_t write(const uint8_t*, size_t); };
extern HardwareSerial Serial1;
class TwoWire { public: void begin(); void setClock(uint32_t); void beginTransmission(uint8_t); size_t write(uint8_t); uint8_t endTransmission(); size_t requestFrom(uint8_t, size_t); int available(); int read(); };
extern TwoWire Wire; extern TwoWire Wire1; extern TwoWire Wire2;
