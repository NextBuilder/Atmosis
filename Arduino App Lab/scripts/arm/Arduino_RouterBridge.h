#pragma once
#include "Arduino.h"
struct BridgeClass {
  bool begin();
  template <typename... A> bool notify(const char*, A...);
};
extern BridgeClass Bridge;
