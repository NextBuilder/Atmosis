#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_DPS310.h>
#include <math.h>
#include <string.h>

namespace config {

constexpr uint32_t kSerialBaud = 115200;
constexpr uint32_t kSerialWaitMs = 5000;
constexpr uint32_t kPollIntervalMs = 200;
constexpr uint32_t kStaleAfterMs = 3000;
constexpr uint32_t kDiagnosticsIntervalMs = 1000;
constexpr bool kDiagnosticsMode = false;

namespace adc {
constexpr uint8_t kResolutionBits = 12;
constexpr int kMaxCount = (1 << kResolutionBits) - 1;
constexpr float kReferenceVolts = 3.3f;
}

namespace x6 {
constexpr uint32_t kBaud = 9600;
constexpr uint32_t kResponseTimeoutMs = 500;
constexpr float kMinTemperatureC = -40.0f;
constexpr float kMaxTemperatureC = 85.0f;
}

namespace dust {
const uint8_t kLedPin = 4;
const uint8_t kOutputPin = A0;
constexpr auto kLedOn = HIGH;
constexpr auto kLedOff = LOW;
constexpr uint8_t kSamplesPerRead = 20;
constexpr uint8_t kTrimPerSide = 4;
constexpr float kFilterAlpha = 0.25f;
constexpr uint32_t kFilterResetMs = 10000;
constexpr float kModuleGain = 11.0f;
constexpr float kZeroDustVolts = 0.6f;
constexpr float kUgm3PerVolt = 200.0f;
constexpr float kMaxSensorVolts = 5.5f;
constexpr uint32_t kSampleDelayUs = 280;
constexpr uint32_t kPulseWidthUs = 320;
constexpr uint32_t kPulseToleranceUs = 20;
constexpr uint32_t kPulsePeriodUs = 10000;
constexpr int kRailMarginCounts = 2;
}

namespace dps310 {
constexpr uint32_t kI2cClockHz = 100000;
constexpr uint32_t kRetryIntervalMs = 10000;
constexpr uint32_t kReadTimeoutMs = 300;
constexpr uint32_t kWarmupMs = 300;
constexpr uint8_t kMaxConsecutiveFailures = 3;
constexpr float kSeaLevelHpa = 1013.25f;
constexpr float kMinPressureHpa = 300.0f;
constexpr float kMaxPressureHpa = 1200.0f;
constexpr float kMinTemperatureC = -40.0f;
constexpr float kMaxTemperatureC = 85.0f;
}

static_assert(dust::kSamplesPerRead > 2 * dust::kTrimPerSide, "Dust trim removes every sample");
static_assert(dust::kFilterAlpha > 0.0f && dust::kFilterAlpha <= 1.0f, "Dust filter alpha must be in (0, 1]");
static_assert(dust::kSampleDelayUs < dust::kPulseWidthUs, "Dust sample must fall inside the LED pulse");
static_assert(dust::kPulseWidthUs < dust::kPulsePeriodUs, "Dust pulse must fit inside its period");
static_assert(dps310::kSeaLevelHpa >= 800.0f && dps310::kSeaLevelHpa <= 1200.0f, "Sea-level pressure out of range");

}

inline uint32_t nowMs() { return static_cast<uint32_t>(millis()); }
inline uint32_t nowUs() { return static_cast<uint32_t>(micros()); }

struct EnvironmentReading {
  float iaq;
  float tvocPpm;
  float hchoPpm;
  float coPpm;
  float temperatureC;
  float humidityPct;
};

struct DustReading {
  float densityUgm3;
};

struct PressureReading {
  float pressureHpa;
  float temperatureC;
  float altitudeM;
};

template <typename T>
class LiveValue {
 public:
  void update(const T& value) {
    value_ = value;
    updatedAtMs_ = nowMs();
    ++updateCount_;
    hasValue_ = true;
  }

  const T& value() const { return value_; }
  bool hasValue() const { return hasValue_; }
  uint32_t ageMs() const { return hasValue_ ? nowMs() - updatedAtMs_ : 0; }
  bool isFresh() const { return hasValue_ && ageMs() <= config::kStaleAfterMs; }

  uint32_t takeUpdateCount() {
    const uint32_t count = updateCount_;
    updateCount_ = 0;
    return count;
  }

 private:
  T value_{};
  uint32_t updatedAtMs_ = 0;
  uint32_t updateCount_ = 0;
  bool hasValue_ = false;
};

class EnvironmentX6 {
 public:
  explicit EnvironmentX6(HardwareSerial& port) : port_(port) {}

  void begin() { port_.begin(config::x6::kBaud); }

  bool read(EnvironmentReading& out) {
    discardInput();
    sendQuery();

    uint8_t frame[kFrameLength];
    if (!receiveFrame(frame)) return fail();

    const EnvironmentReading reading = decode(frame);
    if (!isPlausible(reading)) return fail();

    out = reading;
    return true;
  }

  uint32_t errorCount() const { return errorCount_; }

 private:
  static constexpr uint8_t kHeader = 0x70;
  static constexpr size_t kFrameLength = 22;

  HardwareSerial& port_;
  uint32_t errorCount_ = 0;

  bool fail() {
    ++errorCount_;
    return false;
  }

  void discardInput() {
    while (port_.available() > 0) port_.read();
  }

  void sendQuery() {
    uint8_t query[2] = {kHeader, 0};
    query[1] = checksum(query, 1);
    port_.write(query, sizeof(query));
  }

  bool receiveFrame(uint8_t* frame) {
    const uint32_t start = nowMs();
    size_t received = 0;

    while (nowMs() - start < config::x6::kResponseTimeoutMs) {
      if (port_.available() <= 0) {
        delay(1);
        continue;
      }

      const uint8_t byte = static_cast<uint8_t>(port_.read());
      if (received == 0 && byte != kHeader) continue;

      frame[received++] = byte;
      if (received < kFrameLength) continue;
      if (checksum(frame, kFrameLength) == 0) return true;

      received = resync(frame);
    }
    return false;
  }

  static size_t resync(uint8_t* frame) {
    size_t next = 1;
    while (next < kFrameLength && frame[next] != kHeader) ++next;
    const size_t kept = kFrameLength - next;
    memmove(frame, frame + next, kept);
    return kept;
  }

  static uint8_t checksum(const uint8_t* data, size_t length) {
    uint8_t sum = 0;
    for (size_t i = 0; i < length; ++i) sum += data[i];
    return static_cast<uint8_t>(0u - sum);
  }

  static uint16_t readUint16(const uint8_t* bytes) {
    return static_cast<uint16_t>((bytes[0] << 8) | bytes[1]);
  }

  static float readFloat(const uint8_t* bytes) {
    const uint32_t raw = (static_cast<uint32_t>(bytes[0]) << 24) |
                         (static_cast<uint32_t>(bytes[1]) << 16) |
                         (static_cast<uint32_t>(bytes[2]) << 8) |
                         static_cast<uint32_t>(bytes[3]);
    float value;
    memcpy(&value, &raw, sizeof(value));
    return value;
  }

  static EnvironmentReading decode(const uint8_t* frame) {
    EnvironmentReading reading;
    reading.iaq = readFloat(frame + 1);
    reading.tvocPpm = readFloat(frame + 5);
    reading.hchoPpm = readFloat(frame + 9);
    reading.coPpm = readFloat(frame + 13);
    reading.temperatureC = static_cast<int16_t>(readUint16(frame + 17)) / 100.0f;
    reading.humidityPct = readUint16(frame + 19) / 100.0f;
    return reading;
  }

  static bool isPlausible(const EnvironmentReading& reading) {
    const float gases[] = {reading.iaq, reading.tvocPpm, reading.hchoPpm, reading.coPpm};
    for (const float gas : gases) {
      if (!isfinite(gas) || gas < 0.0f) return false;
    }
    return reading.temperatureC >= config::x6::kMinTemperatureC &&
           reading.temperatureC <= config::x6::kMaxTemperatureC &&
           reading.humidityPct >= 0.0f && reading.humidityPct <= 100.0f;
  }
};

class DustSensor {
 public:
  DustSensor(uint8_t ledPin, uint8_t outputPin) : ledPin_(ledPin), outputPin_(outputPin) {}

  void begin() {
    pinMode(ledPin_, OUTPUT);
    digitalWrite(ledPin_, config::dust::kLedOff);
    pinMode(outputPin_, INPUT);
    analogRead(outputPin_);
  }

  bool read(DustReading& out) {
    int samples[config::dust::kSamplesPerRead];
    if (!acquire(samples)) return fail();

    const float sensorVolts = toSensorVolts(trimmedMean(samples));
    if (!isfinite(sensorVolts) || sensorVolts > config::dust::kMaxSensorVolts) return fail();

    out.densityUgm3 = smooth(toDensity(sensorVolts));
    return true;
  }

  uint32_t errorCount() const { return errorCount_; }

 private:
  const uint8_t ledPin_;
  const uint8_t outputPin_;
  float estimate_ = 0.0f;
  bool filterReady_ = false;
  uint32_t lastValidMs_ = 0;
  uint32_t errorCount_ = 0;

  bool fail() {
    filterReady_ = false;
    ++errorCount_;
    return false;
  }

  bool acquire(int* samples) {
    using namespace config::dust;
    bool valid = true;

    for (uint8_t i = 0; i < kSamplesPerRead; ++i) {
      digitalWrite(ledPin_, kLedOn);
      const uint32_t pulseStart = nowUs();
      waitUntil(pulseStart, kSampleDelayUs);
      samples[i] = analogRead(outputPin_);
      waitUntil(pulseStart, kPulseWidthUs);
      digitalWrite(ledPin_, kLedOff);

      const uint32_t pulseUs = nowUs() - pulseStart;
      valid = valid && isPulseOnTime(pulseUs) && isAwayFromRails(samples[i]);

      const uint32_t elapsedUs = nowUs() - pulseStart;
      if (elapsedUs < kPulsePeriodUs) delayMicroseconds(kPulsePeriodUs - elapsedUs);
    }
    return valid;
  }

  static void waitUntil(uint32_t start, uint32_t offsetUs) {
    while (nowUs() - start < offsetUs) {
    }
  }

  static bool isPulseOnTime(uint32_t pulseUs) {
    using namespace config::dust;
    return pulseUs >= kPulseWidthUs - kPulseToleranceUs &&
           pulseUs <= kPulseWidthUs + kPulseToleranceUs;
  }

  static bool isAwayFromRails(int raw) {
    using namespace config;
    return raw > dust::kRailMarginCounts && raw < adc::kMaxCount - dust::kRailMarginCounts;
  }

  static float trimmedMean(int* samples) {
    using namespace config::dust;
    for (uint8_t i = 1; i < kSamplesPerRead; ++i) {
      const int value = samples[i];
      uint8_t j = i;
      while (j > 0 && samples[j - 1] > value) {
        samples[j] = samples[j - 1];
        --j;
      }
      samples[j] = value;
    }

    float sum = 0.0f;
    for (uint8_t i = kTrimPerSide; i < kSamplesPerRead - kTrimPerSide; ++i) sum += samples[i];
    return sum / (kSamplesPerRead - 2 * kTrimPerSide);
  }

  static float toSensorVolts(float rawAverage) {
    using namespace config;
    return rawAverage * adc::kReferenceVolts / adc::kMaxCount * dust::kModuleGain;
  }

  static float toDensity(float sensorVolts) {
    using namespace config::dust;
    const float density = (sensorVolts - kZeroDustVolts) * kUgm3PerVolt;
    return density > 0.0f ? density : 0.0f;
  }

  float smooth(float instant) {
    using namespace config::dust;
    const uint32_t now = nowMs();
    if (!filterReady_ || now - lastValidMs_ > kFilterResetMs) {
      estimate_ = instant;
      filterReady_ = true;
    } else {
      estimate_ += kFilterAlpha * (instant - estimate_);
    }
    lastValidMs_ = now;
    return estimate_;
  }
};

float altitudeFromPressure(float pressureHpa, float seaLevelHpa) {
  constexpr float kExponent = 0.1903f;
  constexpr float kScaleMeters = 44330.0f;
  constexpr int kSeriesTerms = 64;

  const float x = pressureHpa / seaLevelHpa - 1.0f;
  float term = 1.0f;
  float ratioPowerMinusOne = 0.0f;
  for (int n = 1; n <= kSeriesTerms; ++n) {
    term *= (kExponent - (n - 1)) * x / n;
    ratioPowerMinusOne += term;
  }
  return -kScaleMeters * ratioPowerMinusOne;
}

class PressureSensorDps310 {
 public:
  explicit PressureSensorDps310(TwoWire& wire) : wire_(wire) {}

  bool begin() {
    active_ = nullptr;
    address_ = 0;
    lastAttemptMs_ = nowMs();

    wire_.begin();
    wire_.setClock(config::dps310::kI2cClockHz);

    Adafruit_DPS310* const drivers[] = {&driverAt77_, &driverAt76_};
    const uint8_t addresses[] = {0x77, 0x76};

    for (uint8_t i = 0; i < 2; ++i) {
      if (!configure(*drivers[i], addresses[i])) continue;
      active_ = drivers[i];
      address_ = addresses[i];
      failures_ = 0;
      return true;
    }
    return false;
  }

  bool read(PressureReading& out) {
    if (!active_ && !reconnect()) return false;
    if (!waitForData()) return fail();

    sensors_event_t temperature = {};
    sensors_event_t pressure = {};
    if (!active_->getEvents(&temperature, &pressure)) return fail();

    PressureReading reading;
    reading.pressureHpa = pressure.pressure;
    reading.temperatureC = temperature.temperature;
    if (!isPlausible(reading)) return fail();

    reading.altitudeM = altitudeFromPressure(reading.pressureHpa, config::dps310::kSeaLevelHpa);
    failures_ = 0;
    out = reading;
    return true;
  }

  uint32_t errorCount() const { return errorCount_; }

 private:
  static constexpr uint8_t kRegMeasCfg = 0x08;
  static constexpr uint8_t kRegProductId = 0x0D;
  static constexpr uint8_t kProductId = 0x10;
  static constexpr uint8_t kDataReadyMask = 0x30;

  TwoWire& wire_;
  Adafruit_DPS310 driverAt77_;
  Adafruit_DPS310 driverAt76_;
  Adafruit_DPS310* active_ = nullptr;
  uint8_t address_ = 0;
  uint8_t failures_ = 0;
  uint32_t lastAttemptMs_ = 0;
  uint32_t errorCount_ = 0;

  bool configure(Adafruit_DPS310& driver, uint8_t address) {
    uint8_t productId = 0;
    if (!readRegister(address, kRegProductId, productId) || productId != kProductId) return false;
    if (!driver.begin_I2C(address, &wire_)) return false;

    driver.setMode(DPS310_IDLE);
    if (!applyTemperatureFix(address)) return false;
    driver.configurePressure(DPS310_4HZ, DPS310_16SAMPLES);
    driver.configureTemperature(DPS310_4HZ, DPS310_16SAMPLES);
    driver.setMode(DPS310_CONT_PRESTEMP);

    sensors_event_t temperature = {};
    sensors_event_t pressure = {};
    driver.getEvents(&temperature, &pressure);
    delay(config::dps310::kWarmupMs);
    return true;
  }

  bool reconnect() {
    if (nowMs() - lastAttemptMs_ < config::dps310::kRetryIntervalMs) return false;
    return begin();
  }

  bool waitForData() {
    const uint32_t start = nowMs();
    while (true) {
      uint8_t status = 0;
      if (!readRegister(address_, kRegMeasCfg, status)) return false;
      if ((status & kDataReadyMask) == kDataReadyMask) return true;
      if (nowMs() - start >= config::dps310::kReadTimeoutMs) return false;
      delay(2);
    }
  }

  bool applyTemperatureFix(uint8_t address) {
    const uint8_t sequence[][2] = {{0x0E, 0xA5}, {0x0F, 0x96}, {0x62, 0x02}, {0x0E, 0x00}, {0x0F, 0x00}};
    bool ok = true;
    for (const auto& step : sequence) ok = writeRegister(address, step[0], step[1]) && ok;
    return ok;
  }

  bool readRegister(uint8_t address, uint8_t reg, uint8_t& value) {
    wire_.beginTransmission(address);
    wire_.write(reg);
    if (wire_.endTransmission() != 0) return false;
    if (wire_.requestFrom(address, static_cast<size_t>(1)) != 1) return false;
    value = static_cast<uint8_t>(wire_.read());
    return true;
  }

  bool writeRegister(uint8_t address, uint8_t reg, uint8_t value) {
    wire_.beginTransmission(address);
    wire_.write(reg);
    wire_.write(value);
    return wire_.endTransmission() == 0;
  }

  static bool isPlausible(const PressureReading& reading) {
    using namespace config::dps310;
    return isfinite(reading.pressureHpa) && isfinite(reading.temperatureC) &&
           reading.pressureHpa >= kMinPressureHpa && reading.pressureHpa <= kMaxPressureHpa &&
           reading.temperatureC >= kMinTemperatureC && reading.temperatureC <= kMaxTemperatureC;
  }

  bool fail() {
    ++errorCount_;
    if (++failures_ >= config::dps310::kMaxConsecutiveFailures) {
      active_ = nullptr;
      lastAttemptMs_ = nowMs();
    }
    return false;
  }
};

EnvironmentX6 environmentSensor(Serial1);
DustSensor dustSensor(config::dust::kLedPin, config::dust::kOutputPin);
PressureSensorDps310 pressureSensor(Wire);

LiveValue<EnvironmentReading> environment;
LiveValue<DustReading> dust;
LiveValue<PressureReading> pressure;

template <typename Sensor, typename Reading>
void poll(Sensor& sensor, LiveValue<Reading>& channel) {
  Reading reading{};
  if (sensor.read(reading)) channel.update(reading);
}

void pollSensors() {
  poll(environmentSensor, environment);
  poll(dustSensor, dust);
  poll(pressureSensor, pressure);
}

struct CsvField {
  float value;
  uint8_t decimals;
};

void printCsvRecord() {
  const EnvironmentReading& env = environment.value();
  const CsvField fields[] = {
      {env.iaq, 2},
      {env.tvocPpm, 3},
      {env.hchoPpm, 3},
      {env.coPpm, 3},
      {env.temperatureC, 2},
      {env.humidityPct, 2},
      {dust.value().densityUgm3, 1},
      {pressure.value().pressureHpa, 2},
      {pressure.value().altitudeM, 1},
  };

  for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); ++i) {
    if (i > 0) Serial.print(',');
    Serial.print(fields[i].value, fields[i].decimals);
  }
  Serial.println();
}

void printMetric(const char* label, float value, uint8_t decimals, const char* unit) {
  Serial.print(label);
  Serial.print(' ');
  Serial.print(value, decimals);
  Serial.print(unit);
  Serial.print("  ");
}

void printValues(const EnvironmentReading& r) {
  printMetric("IAQ", r.iaq, 2, "");
  printMetric("TVOC", r.tvocPpm, 3, " ppm");
  printMetric("HCHO", r.hchoPpm, 3, " ppm");
  printMetric("CO", r.coPpm, 3, " ppm");
  printMetric("T", r.temperatureC, 2, " C");
  printMetric("RH", r.humidityPct, 2, " %");
}

void printValues(const DustReading& r) {
  printMetric("PM", r.densityUgm3, 1, " ug/m3");
}

void printValues(const PressureReading& r) {
  printMetric("P", r.pressureHpa, 2, " hPa");
  printMetric("T", r.temperatureC, 2, " C");
  printMetric("ALT", r.altitudeM, 1, " m");
}

template <typename T>
void printChannel(const char* name, LiveValue<T>& channel, uint32_t errors, uint32_t windowMs) {
  const char* state = !channel.hasValue() ? "WAIT " : channel.isFresh() ? "LIVE " : "STALE";
  const float rateHz = channel.takeUpdateCount() * 1000.0f / windowMs;

  Serial.print(name);
  Serial.print("  ");
  Serial.print(state);
  Serial.print("  age ");
  Serial.print(static_cast<unsigned long>(channel.ageMs()));
  Serial.print(" ms  rate ");
  Serial.print(rateHz, 1);
  Serial.print(" Hz  err ");
  Serial.print(static_cast<unsigned long>(errors));
  Serial.print("  | ");
  printValues(channel.value());
  Serial.println();
}

void reportDiagnostics() {
  static uint32_t lastReportMs = nowMs();
  const uint32_t now = nowMs();
  const uint32_t windowMs = now - lastReportMs;
  if (windowMs < config::kDiagnosticsIntervalMs) return;
  lastReportMs = now;

  Serial.print("--- Atmosis @ ");
  Serial.print(now / 1000.0f, 1);
  Serial.println(" s ---");
  printChannel("X6    ", environment, environmentSensor.errorCount(), windowMs);
  printChannel("DUST  ", dust, dustSensor.errorCount(), windowMs);
  printChannel("DPS310", pressure, pressureSensor.errorCount(), windowMs);
}

void setup() {
  Serial.begin(config::kSerialBaud);
  const uint32_t start = nowMs();
  while (!Serial && nowMs() - start < config::kSerialWaitMs) delay(10);

  analogReadResolution(config::adc::kResolutionBits);
  environmentSensor.begin();
  dustSensor.begin();
  pressureSensor.begin();
}

void loop() {
  static uint32_t lastPollMs = 0;
  const uint32_t now = nowMs();
  if (now - lastPollMs < config::kPollIntervalMs) {
    delay(1);
    return;
  }
  lastPollMs = now;

  pollSensors();

  if (config::kDiagnosticsMode) {
    reportDiagnostics();
  } else {
    printCsvRecord();
  }
}
