#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

g++ -std=c++17 -Wall -Wextra -Wno-unused-parameter -O1 -x c++ \
    -I"$ROOT/scripts/sim" -I"$ROOT/sketch" \
    "$ROOT/scripts/sim/test_firmware.cpp" -o "$OUT/fwtest"
"$OUT/fwtest"

if ! command -v arm-none-eabi-g++ >/dev/null; then
  echo "ARM check skipped (install gcc-arm-none-eabi to run it)"
  exit 0
fi
cp "$ROOT/sketch/sketch.ino" "$OUT/sketch.cpp"
arm-none-eabi-g++ -std=gnu++17 -mcpu=cortex-m33 -mthumb -mfloat-abi=hard -mfpu=fpv5-sp-d16 -Os \
    -fno-exceptions -fno-rtti -Wall -Wextra -Wno-unused-parameter -Wdouble-promotion -Werror \
    -I"$ROOT/scripts/arm" -I"$ROOT/sketch" -c "$OUT/sketch.cpp" -o "$OUT/sketch.o"
EXTRA="$(arm-none-eabi-nm -u "$OUT/sketch.o" | awk '{print $2}' | grep -vE '^_Z|^(millis|micros|delay|pinMode|digitalWrite|digitalRead|analogRead|analogReadResolution|Serial1|Wire|Bridge|memcpy|memset|strlen)$|^__cxa_|^__dso_handle$|^__aeabi_atexit$' || true)"
if [ -n "$EXTRA" ]; then
  echo "ARM CHECK FAILED — the UNO Q core may not provide:"; echo "$EXTRA"; exit 1
fi
echo "ARM CHECK PASSED — Cortex-M33 build needs no double-precision or libm helpers"
