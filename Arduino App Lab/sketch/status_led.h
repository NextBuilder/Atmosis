#pragma once
#include <Arduino.h>
#include <Adafruit_NeoPixel.h>
#include "config.h"

class StatusLed {
public:
  bool begin() {
    pixels_.updateType(NEO_GRB + NEO_KHZ800);
    pixels_.updateLength(WS2812B_LED_COUNT);
    if (pixels_.numPixels() != WS2812B_LED_COUNT) return false;
    pixels_.setPin(PIN_WS2812B);
    pixels_.begin();
    pixels_.setBrightness(WS2812B_BRIGHTNESS);
    ready_ = true;
    target_[0] = 0; target_[1] = 110; target_[2] = 255;
    from_[0] = from_[1] = from_[2] = 0;
    fadeStartMs_ = millis();
    fading_ = true;
    return true;
  }

  void setColor(uint8_t r, uint8_t g, uint8_t b) {
    if (r == target_[0] && g == target_[1] && b == target_[2]) return;
    startFade(r, g, b);
  }

  void setAlarm(bool active) { alarm_ = active; }
  bool ready() const { return ready_; }

  void update(uint32_t now) {
    if (!ready_ || (now - lastFrameMs_) < LED_FRAME_MS) return;
    lastFrameMs_ = now;
    if (alarm_) {
      const float phase = (float)(now % LED_PULSE_PERIOD_MS) / (float)LED_PULSE_PERIOD_MS;
      const float tri   = phase < 0.5f ? phase * 2.0f : (1.0f - phase) * 2.0f;
      const float k     = 0.25f + 0.75f * tri * tri * (3.0f - 2.0f * tri);
      show((uint8_t)(255 * k), (uint8_t)(24 * k), (uint8_t)(16 * k));
      wasPulsing_ = true;
      return;
    }
    if (wasPulsing_) {
      wasPulsing_ = false;
      from_[0] = lastR_; from_[1] = lastG_; from_[2] = lastB_;
      fadeStartMs_ = now;
      fading_ = true;
    }
    if (!fading_) return;
    float t = (float)(now - fadeStartMs_) / (float)LED_FADE_MS;
    if (t >= 1.0f) { t = 1.0f; fading_ = false; }
    const float e = t * t * (3.0f - 2.0f * t);
    show(mix(from_[0], target_[0], e), mix(from_[1], target_[1], e), mix(from_[2], target_[2], e));
  }

private:
  Adafruit_NeoPixel pixels_;
  bool     ready_        = false;
  bool     alarm_        = false;
  bool     fading_       = false;
  bool     wasPulsing_   = false;
  uint8_t  target_[3]    = {0, 110, 255};
  uint8_t  from_[3]      = {0, 0, 0};
  uint8_t  lastR_ = 0, lastG_ = 0, lastB_ = 0;
  uint32_t fadeStartMs_  = 0;
  uint32_t lastFrameMs_  = 0;

  static uint8_t mix(uint8_t a, uint8_t b, float e) {
    return (uint8_t)((float)a + ((float)b - (float)a) * e + 0.5f);
  }

  void startFade(uint8_t r, uint8_t g, uint8_t b) {
    from_[0] = lastR_; from_[1] = lastG_; from_[2] = lastB_;
    target_[0] = r; target_[1] = g; target_[2] = b;
    fadeStartMs_ = millis();
    fading_ = true;
  }

  void show(uint8_t r, uint8_t g, uint8_t b) {
    if (r == lastR_ && g == lastG_ && b == lastB_) return;
    const uint32_t color = pixels_.Color(r, g, b);
    for (uint16_t i = 0; i < WS2812B_LED_COUNT; ++i) pixels_.setPixelColor(i, color);
    pixels_.show();
    lastR_ = r; lastG_ = g; lastB_ = b;
  }
};
