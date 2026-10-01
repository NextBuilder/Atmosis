#pragma once
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <vector>
#include <deque>

#ifndef PI
#define PI 3.1415926535897932384626433832795
#endif
#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define INPUT 0
#define INPUT_PULLDOWN 3
#define A0 14

using std::isfinite;

namespace sim {
extern uint64_t now_us;
extern int pin_state[64];
extern int line_level[64];
extern std::function<int(uint8_t)> adc;
extern uint32_t adc_cost_us;
extern std::vector<std::string> monitor_lines;
inline void advance(uint64_t us) { now_us += us; }
}

inline uint32_t millis() { sim::advance(3); return (uint32_t)(sim::now_us / 1000ULL); }
inline uint32_t micros() { sim::advance(1); return (uint32_t)sim::now_us; }
inline void delay(uint32_t ms) { sim::advance((uint64_t)ms * 1000ULL); }
inline void delayMicroseconds(uint32_t us) { sim::advance(us); }
inline void pinMode(uint8_t, uint8_t) {}
inline void digitalWrite(uint8_t pin, uint8_t v) { sim::pin_state[pin] = v; }
inline int digitalRead(uint8_t pin) { return sim::line_level[pin]; }
inline int analogRead(uint8_t pin) { sim::advance(sim::adc_cost_us); return sim::adc ? sim::adc(pin) : 0; }
inline void analogReadResolution(uint8_t) {}

class String {
public:
  String() = default;
  String(const char* s) : d_(s ? s : "") {}
  const char* c_str() const { return d_.c_str(); }
  size_t length() const { return d_.size(); }
  std::string str() const { return d_; }
private:
  std::string d_;
};

struct HardwareSerialSim {
  std::deque<uint8_t> rx;
  std::vector<uint8_t> tx;
  std::function<void(HardwareSerialSim&, const uint8_t*, size_t)> on_write;
  void begin(uint32_t) {}
  int available() { return (int)rx.size(); }
  int read() { if (rx.empty()) return -1; uint8_t b = rx.front(); rx.pop_front(); return b; }
  size_t write(const uint8_t* b, size_t n) { tx.insert(tx.end(), b, b + n); if (on_write) on_write(*this, b, n); return n; }
};
extern HardwareSerialSim Serial1;

struct I2CDevice {
  virtual ~I2CDevice() = default;
  virtual uint8_t readReg(uint8_t reg) = 0;
  virtual void writeReg(uint8_t reg, uint8_t v) = 0;
};

class TwoWire {
public:
  std::map<uint8_t, I2CDevice*> devices;
  bool begun = false;
  int begin_calls = 0;
  void begin() { begun = true; begin_calls++; }
  void setClock(uint32_t) {}
  void beginTransmission(uint8_t a) { addr_ = a; txbuf_.clear(); }
  size_t write(uint8_t v) { txbuf_.push_back(v); return 1; }
  uint8_t endTransmission() {
    sim::advance(100);
    auto it = devices.find(addr_);
    if (it == devices.end()) return 2;
    if (txbuf_.size() >= 1) ptr_ = txbuf_[0];
    for (size_t i = 1; i < txbuf_.size(); ++i) it->second->writeReg((uint8_t)(txbuf_[0] + i - 1), txbuf_[i]);
    return 0;
  }
  size_t requestFrom(uint8_t a, size_t n) {
    rxbuf_.clear();
    auto it = devices.find(a);
    if (it == devices.end()) return 0;
    for (size_t i = 0; i < n; ++i) rxbuf_.push_back(it->second->readReg((uint8_t)(ptr_ + i)));
    return n;
  }
  int available() { return (int)rxbuf_.size(); }
  int read() { if (rxbuf_.empty()) return -1; uint8_t b = rxbuf_.front(); rxbuf_.pop_front(); return b; }
private:
  uint8_t addr_ = 0, ptr_ = 0;
  std::vector<uint8_t> txbuf_;
  std::deque<uint8_t> rxbuf_;
};
extern TwoWire Wire;
extern TwoWire Wire1;
extern TwoWire Wire2;

struct BridgeArg {
  bool is_text = false;
  double num = 0;
  std::string text;
};

struct BridgeSim {
  std::vector<std::pair<std::string, std::vector<BridgeArg>>> sent;
  bool begin() { return true; }
  static BridgeArg pack(int v) { BridgeArg a; a.num = v; return a; }
  static BridgeArg pack(float v) { BridgeArg a; a.num = v; return a; }
  template <typename... A>
  bool notify(const char* method, A... args) {
    sent.push_back({method, {pack(args)...}});
    return true;
  }
};
extern BridgeSim Bridge;
