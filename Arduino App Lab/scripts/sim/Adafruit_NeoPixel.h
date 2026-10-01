#pragma once
#include "Arduino.h"
#define NEO_GRB 0x52
#define NEO_KHZ800 0x0000
namespace sim { extern int neo_shows; extern uint32_t neo_color; }
class Adafruit_NeoPixel {
public:
  void updateType(uint16_t) {}
  void updateLength(uint16_t n) { n_ = n; }
  uint16_t numPixels() const { return n_; }
  void setPin(int16_t) {}
  void begin() {}
  void setBrightness(uint8_t) {}
  static uint32_t Color(uint8_t r, uint8_t g, uint8_t b) { return ((uint32_t)r << 16) | ((uint32_t)g << 8) | b; }
  void setPixelColor(uint16_t, uint32_t c) { sim::neo_color = c; }
  void show() { sim::neo_shows++; sim::advance(300); }
private:
  uint16_t n_ = 0;
};
