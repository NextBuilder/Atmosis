#pragma once
#include "Arduino.h"
#define NEO_GRB 0x52
#define NEO_KHZ800 0
class Adafruit_NeoPixel { public: void updateType(uint16_t); void updateLength(uint16_t); uint16_t numPixels() const; void setPin(int16_t); void begin(); void setBrightness(uint8_t); static uint32_t Color(uint8_t,uint8_t,uint8_t); void setPixelColor(uint16_t,uint32_t); void show(); };
