#pragma once
#include <Arduino.h>

#define ATMOSIS_VERSION "1.2"

constexpr uint32_t FRAME_INTERVAL_MS        = 1000;
constexpr uint32_t INFO_INTERVAL_MS         = 30000;
constexpr uint32_t INFO_FIRST_MS            = 3000;
constexpr uint32_t LOOP_YIELD_MS            = 4;
constexpr uint8_t  FRAME_PROTOCOL           = 5;
constexpr uint8_t  FIRMWARE_CODE            = 12;

#define X6_SERIAL Serial1
constexpr uint32_t X6_BAUD                  = 9600;
constexpr uint8_t  X6_CMD_CONCENTRATION     = 0x70;
constexpr uint8_t  X6_RESPONSE_LEN          = 22;
constexpr uint32_t X6_POLL_INTERVAL_MS      = 1000;
constexpr uint32_t X6_FRAME_TIMEOUT_MS      = 250;
constexpr uint32_t X6_STALE_AFTER_MS        = 3500;
constexpr float    X6_IAQ_MAX               = 500.0f;
constexpr float    X6_TVOC_MAX              = 10.0f;
constexpr float    X6_HCHO_MAX              = 2.0f;
constexpr float    X6_CO_MAX                = 200.0f;
constexpr float    X6_TEMP_MIN              = -40.0f;
constexpr float    X6_TEMP_MAX              = 85.0f;

constexpr uint8_t  PIN_DUST_ILED            = 4;
constexpr uint8_t  PIN_DUST_AOUT            = A0;
constexpr float    DUST_VOLTAGE_GAIN        = 11.0f;
constexpr float    DUST_ZERO_MV             = 400.0f;
constexpr float    DUST_UGM3_PER_MV         = 0.2f;
constexpr float    DUST_MAX_UGM3            = 500.0f;
constexpr uint32_t DUST_BATCH_INTERVAL_MS   = 1000;
constexpr uint8_t  DUST_SAMPLES             = 10;
constexpr uint8_t  DUST_AVERAGE_WINDOW      = 10;
constexpr uint32_t DUST_SAMPLE_DELAY_US     = 280;
constexpr uint32_t DUST_PULSE_MAX_US        = 1200;
constexpr uint32_t DUST_PULSE_PERIOD_US     = 10000;
constexpr float    DUST_NO_SIGNAL_MV        = 4.0f;

constexpr uint8_t  ADC_RESOLUTION_BITS      = 12;
constexpr uint32_t ADC_MAX_COUNT            = (1UL << ADC_RESOLUTION_BITS) - 1UL;
constexpr float    ADC_VREF                 = 3.3f;

#define DPS310_WIRE Wire
constexpr uint32_t I2C_CLOCK_HZ             = 100000;
constexpr uint32_t PRESSURE_POLL_MS         = 1000;
constexpr uint32_t PRESSURE_RETRY_MS        = 10000;
constexpr float    PRESSURE_MIN_HPA         = 300.0f;
constexpr float    PRESSURE_MAX_HPA         = 1200.0f;
constexpr float    SEA_LEVEL_PRESSURE_HPA   = 1013.25f;

constexpr uint8_t  PIN_WS2812B              = 5;
constexpr uint16_t WS2812B_LED_COUNT        = 30;
constexpr uint8_t  WS2812B_BRIGHTNESS       = 40;
constexpr uint32_t LED_FADE_MS              = 600;
constexpr uint32_t LED_FRAME_MS             = 40;
constexpr uint32_t LED_PULSE_PERIOD_MS      = 1600;

constexpr float    DUST_ALERT_UGM3          = 150.0f;
constexpr float    DUST_ALERT_CLEAR_UGM3    = 130.0f;
constexpr float    CO_ALERT_PPM             = 9.0f;
constexpr float    CO_ALERT_CLEAR_PPM       = 8.0f;

constexpr uint16_t FLAG_X6                  = 0x0001;
constexpr uint16_t FLAG_PRESSURE            = 0x0002;
constexpr uint16_t FLAG_DUST                = 0x0004;
constexpr uint16_t FLAG_DUST_TIME           = 0x0008;
constexpr uint16_t FLAG_FAILSAFE            = 0x0010;
constexpr uint16_t FLAG_CO_ALARM            = 0x0020;
constexpr uint16_t FLAG_LED                 = 0x0040;

static_assert(DUST_SAMPLES >= 5, "Need enough pulses per batch");
static_assert(DUST_ALERT_CLEAR_UGM3 < DUST_ALERT_UGM3, "Dust hysteresis must be positive");
static_assert(CO_ALERT_CLEAR_PPM < CO_ALERT_PPM, "CO hysteresis must be positive");
static_assert(DUST_SAMPLE_DELAY_US == 280, "Sharp/Waveshare sample point is 280 us after the LED turns on");
static_assert(WS2812B_LED_COUNT > 0 && WS2812B_LED_COUNT <= 60, "Keep the strip short for Bridge timing and USB power");
static_assert(PRESSURE_MIN_HPA < PRESSURE_MAX_HPA, "Pressure bounds ordered");
