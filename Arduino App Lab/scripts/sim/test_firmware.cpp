#include "Arduino.h"
#include <cassert>
#include <sstream>

namespace sim {
uint64_t now_us = 0;
int pin_state[64] = {0};
int line_level[64] = {0};
std::function<int(uint8_t)> adc;
uint32_t adc_cost_us = 300;
int neo_shows = 0;
uint32_t neo_color = 0;
int dps_begin_calls = 0;
}
HardwareSerialSim Serial1;
TwoWire Wire, Wire1, Wire2;
BridgeSim Bridge;

#include "sketch.ino"

static int failures = 0;
#define CHECK(cond, msg) do { if (!(cond)) { std::printf("  FAIL: %s\n", msg); failures++; } else { std::printf("  ok:   %s\n", msg); } } while (0)

struct RegDevice : I2CDevice {
  uint8_t r[256] = {0};
  uint8_t readReg(uint8_t reg) override { return r[reg]; }
  void writeReg(uint8_t reg, uint8_t v) override { if (reg != 0x7E && reg != 0x0C) r[reg] = v; }
};

struct Dps310 : RegDevice {
  Dps310(int32_t c0, int32_t c1, int32_t c00, int32_t c10, int32_t c01, int32_t c11,
         int32_t c20, int32_t c21, int32_t c30, int32_t praw, int32_t traw) {
    r[0x0D] = 0x10; r[0x28] = 0x80;
    auto m = [](int32_t v, int bits) { return (uint32_t)v & ((1u << bits) - 1); };
    uint32_t C0 = m(c0, 12), C1 = m(c1, 12), C00 = m(c00, 20), C10 = m(c10, 20);
    r[0x10] = C0 >> 4; r[0x11] = ((C0 & 0x0F) << 4) | (C1 >> 8); r[0x12] = C1 & 0xFF;
    r[0x13] = C00 >> 12; r[0x14] = (C00 >> 4) & 0xFF; r[0x15] = ((C00 & 0x0F) << 4) | (C10 >> 16);
    r[0x16] = (C10 >> 8) & 0xFF; r[0x17] = C10 & 0xFF;
    int32_t rest[5] = {c01, c11, c20, c21, c30};
    for (int i = 0; i < 5; ++i) { uint32_t v = m(rest[i], 16); r[0x18 + 2 * i] = v >> 8; r[0x19 + 2 * i] = v & 0xFF; }
    uint32_t P = m(praw, 24), T = m(traw, 24);
    r[0x00] = P >> 16; r[0x01] = (P >> 8) & 0xFF; r[0x02] = P & 0xFF;
    r[0x03] = T >> 16; r[0x04] = (T >> 8) & 0xFF; r[0x05] = T & 0xFF;
  }
  uint8_t readReg(uint8_t reg) override { return reg == 0x08 ? (uint8_t)(0xF0 | (r[0x08] & 0x0F)) : r[reg]; }
};

static void be32(uint8_t* p, float f) { uint32_t b; std::memcpy(&b, &f, 4); p[0] = b >> 24; p[1] = b >> 16; p[2] = b >> 8; p[3] = b; }

static std::vector<uint8_t> x6Reply;
static int x6Queries = 0;

static void installX6Bytes(const std::vector<uint8_t>& frame) {
  x6Reply = frame;
  Serial1.on_write = [](HardwareSerialSim& s, const uint8_t* b, size_t n) {
    if (n != 2 || b[0] != 0x70 || b[1] != 0x90) return;
    x6Queries++;
    for (auto x : x6Reply) s.rx.push_back(x);
  };
}

static void installX6(float iaq, float tvoc, float hcho, float co, float t, float rh) {
  std::vector<uint8_t> f(22, 0);
  f[0] = 0x70;
  be32(&f[1], iaq); be32(&f[5], tvoc); be32(&f[9], hcho); be32(&f[13], co);
  int16_t T = (int16_t)std::lround(t * 100); uint16_t H = (uint16_t)std::lround(rh * 100);
  f[17] = (uint16_t)T >> 8; f[18] = (uint16_t)T & 0xFF; f[19] = H >> 8; f[20] = H & 0xFF;
  uint32_t sum = 0; for (int i = 0; i < 21; ++i) sum += f[i];
  f[21] = (uint8_t)((~(sum & 0xFF) + 1) & 0xFF);
  installX6Bytes(f);
}

static float dustVolts = 0.9f;
static bool moduleActiveHigh = true;
static bool dustConnected = true;

static void installDust() {
  sim::adc = [](uint8_t pin) -> int {
    if (pin != A0 || !dustConnected) return 0;
    bool lit = moduleActiveHigh ? sim::pin_state[PIN_DUST_ILED] == HIGH : sim::pin_state[PIN_DUST_ILED] == LOW;
    float pinV = lit ? dustVolts / DUST_VOLTAGE_GAIN : 0.002f;
    return (int)(pinV / ADC_VREF * ADC_MAX_COUNT + 0.5f);
  };
}

static std::vector<BridgeArg> last(const char* method) {
  for (auto it = Bridge.sent.rbegin(); it != Bridge.sent.rend(); ++it) if (it->first == method) return it->second;
  return {};
}
static int count(const char* method) {
  int n = 0; for (auto& m : Bridge.sent) if (m.first == method) n++; return n;
}
static double F(int i) { return last("atmosis_frame").at(i).num; }
static unsigned FLAGS() { return (unsigned)F(3); }

static uint64_t runFor(uint32_t ms) {
  uint64_t end = sim::now_us + (uint64_t)ms * 1000ULL, worst = 0;
  while (sim::now_us < end) {
    uint64_t a = sim::now_us;
    loop();
    if (sim::now_us - a > worst) worst = sim::now_us - a;
  }
  return worst;
}

static void resetWorld() {
  sim::now_us = 1000000;
  Wire.devices.clear(); Wire1.devices.clear(); Wire2.devices.clear();
  Wire.begin_calls = Wire1.begin_calls = 0; sim::dps_begin_calls = 0;
  Serial1.rx.clear(); Serial1.tx.clear(); Serial1.on_write = nullptr;
  Bridge.sent.clear();
  dustVolts = 0.9f; moduleActiveHigh = true; dustConnected = true; x6Queries = 0;
  x6Sensor = EnvironmentX6(); dustSensor = DustSensor(); pressureSensor = PressureSensor(); statusLed = StatusLed();
  lastX6 = X6Reading(); lastDust = DustReading();
  dustAlarm = false; coAlarm = false; frameSeq = 0; sim::neo_shows = 0;
}

int main() {
  const int32_t c0 = 200, c1 = -260, c00 = 51043, c10 = -50000, c01 = -2000, c11 = 1200, c20 = -8000, c21 = 150, c30 = -1500;
  const int32_t praw = -300000, traw = 76186;
  const double ps = praw / 253952.0, ts = traw / 253952.0;
  const double expP = (c00 + ps * (c10 + ps * (c20 + ps * c30)) + ts * (c01 + ps * (c11 + ps * c21))) / 100.0;
  const double expT = ts * c1 + c0 / 2.0;

  std::printf("== Test 1: Waveshare's official X6 example frame ==\n");
  resetWorld();
  installX6Bytes({0x70, 0x42,0x30,0x00,0x00, 0x3D,0x35,0x64,0xF0, 0x3D,0x53,0xD2,0x5B, 0x3D,0x08,0x19,0x55, 0x0A,0xC8, 0x27,0x0F, 0xE0});
  installDust();
  setup(); runFor(3500);
  CHECK(last("atmosis_frame").size() == 19, "frame carries 19 values");
  CHECK(F(0) == 5, "frame protocol 5");
  CHECK(FLAGS() & FLAG_X6, "official example frame accepted (checksum 0xE0 valid)");
  CHECK(std::fabs(F(4) - 44.0) < 1e-3, "IAQ 44");
  CHECK(std::fabs(F(5) - 0.044) < 5e-4, "TVOC 0.044 ppm");
  CHECK(std::fabs(F(6) - 0.052) < 5e-4, "HCHO 0.052 ppm");
  CHECK(std::fabs(F(7) - 0.033) < 5e-4, "CO 0.033 ppm");
  CHECK(std::fabs(F(8) - 27.60) < 1e-3, "temperature 27.60 C");
  CHECK(std::fabs(F(9) - 99.99) < 1e-3, "humidity 99.99 %");

  std::printf("== Test 2: DPS310 on SDA/SCL (Wire), your data-collection setup ==\n");
  resetWorld();
  Dps310 dps(c0, c1, c00, c10, c01, c11, c20, c21, c30, praw, traw);
  Wire.devices[0x77] = &dps;
  installX6(42.5f, 0.12f, 0.02f, 0.8f, 27.3f, 55.1f);
  installDust();
  setup();
  uint64_t worst = runFor(12000);
  CHECK(F(17) == 3, "DPS310 detected at 0x77 with the Adafruit driver");
  CHECK(std::fabs(F(14) - expP) < 0.05, "pressure matches the DPS310 datasheet formula");
  CHECK(std::fabs(F(16) - expT) < 0.05, "DPS310 temperature matches");
  CHECK(std::fabs(F(15) - 44330.0 * (1.0 - std::pow(F(14) / 1013.25, 0.1903))) < 0.5, "altitude matches the exact formula");
  for (float hpa : {300.0f, 700.0f, 1013.25f, 1100.0f}) {
    const double ex = 44330.0 * (1.0 - std::pow(hpa / 1013.25, 0.1903));
    CHECK(std::fabs(altitudeFromPressure(hpa) - ex) < 1.0, "altitude accurate across 300-1100 hPa");
  }
  CHECK(FLAGS() & FLAG_PRESSURE, "pressure flag set");
  CHECK(FLAGS() & FLAG_DUST, "dust detected");
  CHECK(std::fabs(F(10) - 100.0) < 1.5, "0.9 V gives 100 ug/m3 with Waveshare's 400 mV zero");
  CHECK(Wire1.begin_calls == 0, "only Wire is touched, like your data-collection sketch");
  CHECK(worst < 8000, "worst loop() iteration under 8 ms");
  std::printf("       DPS310 %.2f hPa / %.2f C, worst loop %.2f ms\n", F(14), F(16), worst / 1000.0);

  std::printf("== Test 3: DPS310 at 0x76 ==\n");
  resetWorld(); installDust();
  Dps310 dps76(c0, c1, c00, c10, c01, c11, c20, c21, c30, praw, traw);
  Wire.devices[0x76] = &dps76;
  setup(); runFor(4500);
  CHECK(F(17) == 3, "DPS310 detected at 0x76");
  CHECK(last("atmosis_info").at(7).num == 0x76, "info reports address 0x76");

  std::printf("== Test 4: one frame per second, X6 queried once per second ==\n");
  resetWorld(); installDust(); installX6(10, 0.01f, 0.01f, 0.2f, 25, 50); setup(); runFor(2000);
  int before = count("atmosis_frame"); int q0 = x6Queries;
  runFor(10000);
  int frames = count("atmosis_frame") - before;
  CHECK(frames >= 9 && frames <= 11, "about 10 frames in 10 s");
  CHECK(x6Queries - q0 >= 9 && x6Queries - q0 <= 11, "about 10 X6 queries in 10 s");
  CHECK(F(1) == count("atmosis_frame"), "sequence number counts every frame");

  std::printf("== Test 5: dust spike trips the alarm, recovery clears it ==\n");
  dustVolts = 1.6f; runFor(20000);
  CHECK(F(10) > 150, "PM2.5 above alert threshold");
  CHECK(FLAGS() & FLAG_FAILSAFE, "dust alarm active");
  dustVolts = 0.45f; runFor(30000);
  CHECK(!(FLAGS() & FLAG_FAILSAFE), "dust alarm cleared after recovery");

  std::printf("== Test 6: dust maths is exactly Waveshare's (400 mV zero, 0.2 ug/m3 per mV) ==\n");
  for (float mv : {300.0f, 400.0f, 550.0f, 900.0f, 1650.0f, 4000.0f}) {
    float ws = mv >= 400.0f ? (mv - 400.0f) * 0.2f : 0.0f;
    if (ws > 500.0f) ws = 500.0f;
    CHECK(std::fabs(dustDensityFromSensorMv(mv) - ws) < 1e-3, "matches Waveshare's formula");
  }
  dustVolts = 0.30f; runFor(15000);
  CHECK(F(10) == 0, "below the 400 mV zero reads 0 ug/m3");
  CHECK(F(12) == 400, "fixed clean-air zero reported as 400 mV");
  dustVolts = 0.55f; runFor(15000);
  CHECK(std::fabs(F(10) - 30.0) < 1.0, "0.55 V at the sensor reads 30 ug/m3, same as Waveshare's demo");

  std::printf("== Test 7: failed ADC reads are ignored, not averaged in ==\n");
  {
    auto good = sim::adc;
    int n = 0;
    sim::adc = [&n, good](uint8_t pin) { return (++n % 3 == 0) ? -5 : good(pin); };
    runFor(15000);
    CHECK(std::fabs(F(10) - 30.0) < 1.0, "reading unchanged when some ADC reads fail");
    sim::adc = good;
  }
  resetWorld(); moduleActiveHigh = false; installDust(); installX6(10, 0.01f, 0.01f, 0.2f, 25, 50);
  setup(); runFor(8000);
  CHECK(!(FLAGS() & FLAG_DUST), "wrong ILED wiring gives no reading instead of a fake one");
  CHECK(F(13) == DUST_STATUS_NO_SIGNAL, "reported as NO_SIGNAL");

  std::printf("== Test 8: disconnected dust sensor, no DPS310 ==\n");
  resetWorld(); dustConnected = false; installDust(); setup(); runFor(10000);
  CHECK(!(FLAGS() & FLAG_DUST), "dust invalid when unplugged");
  CHECK(F(13) == DUST_STATUS_NO_SIGNAL, "status NO_SIGNAL");
  CHECK(F(17) == 0, "no pressure chip");
  auto info = last("atmosis_info");
  CHECK(info.size() == 15, "info carries 15 numbers");
  bool allNumeric = true; for (auto& a : info) allNumeric = allNumeric && !a.is_text;
  CHECK(allNumeric, "info is numbers only (no strings over the Bridge)");
  CHECK(info.at(10).num == DUST_STATUS_NO_SIGNAL, "info explains dust state");
  CHECK(info.at(5).num == P_NOT_DETECTED, "info explains pressure state");

  std::printf("== Test 9: DPS310 plugged in later is picked up; every retry re-runs Wire.begin() ==\n");
  resetWorld(); installDust(); setup(); runFor(2000);
  int wb = Wire.begin_calls;
  Dps310 late(c0, c1, c00, c10, c01, c11, c20, c21, c30, praw, traw);
  Wire.devices[0x77] = &late;
  runFor(12000);
  CHECK(F(17) == 3, "DPS310 detected within 12 s of hot-plug");
  CHECK(Wire.begin_calls > wb, "retry re-applies Wire.begin() like your sketch");
  CHECK(pressureSensor.attempts() >= 2, "detection retried until the sensor appeared");

  std::printf("== Test 10: DPS310 unplugged while running is recovered ==\n");
  Wire.devices.erase(0x77);
  runFor(5000);
  CHECK(!(FLAGS() & FLAG_PRESSURE), "pressure invalid after the sensor disappears");
  Wire.devices[0x77] = &late;
  runFor(12000);
  CHECK(FLAGS() & FLAG_PRESSURE, "pressure back after it returns");

  std::printf("== Test 11: light strip set by the board itself ==\n");
  resetWorld(); dustVolts = 0.42f; installDust(); installX6(10, 0.01f, 0.01f, 0.2f, 25, 50); setup();
  runFor(3000);
  int s0 = sim::neo_shows; runFor(10000);
  CHECK(sim::neo_shows - s0 == 0, "no refreshes while the colour is steady");
  {
    uint32_t c = sim::neo_color;
    CHECK(((c >> 16) & 0xFF) == 0 && ((c >> 8) & 0xFF) > 200, "clean air shows green, computed on the board");
  }
  installX6(10, 0.01f, 0.01f, 12.0f, 25, 50);
  s0 = sim::neo_shows; runFor(3000);
  CHECK(FLAGS() & FLAG_CO_ALARM, "CO alarm raised on the board");
  CHECK(sim::neo_shows - s0 > 20, "strip pulses during the alarm");
  installX6(10, 0.01f, 0.01f, 0.5f, 25, 50);
  runFor(3000);
  CHECK(!(FLAGS() & FLAG_CO_ALARM), "CO alarm clears");

  std::printf("== Test 12: loop yields every pass ==\n");
  uint64_t a0 = sim::now_us; int loops = 0;
  while (sim::now_us - a0 < 1000000ULL) { loop(); loops++; }
  CHECK(loops < 260, "loop() sleeps every pass (Bridge thread gets CPU time)");

  std::printf("\n%s (%d failure%s)\n", failures ? "FIRMWARE TESTS FAILED" : "ALL FIRMWARE TESTS PASSED", failures, failures == 1 ? "" : "s");
  return failures ? 1 : 0;
}
