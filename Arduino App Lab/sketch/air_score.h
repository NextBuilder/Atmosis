#pragma once
#include <Arduino.h>

struct ScoreEdges { float e[6]; };

constexpr ScoreEdges EDGES_PM   = {{0, 12, 35, 55, 150, 250}};
constexpr ScoreEdges EDGES_TVOC = {{0, 0.3f, 0.5f, 1.0f, 3.0f, 5.0f}};
constexpr ScoreEdges EDGES_HCHO = {{0, 0.03f, 0.08f, 0.3f, 0.75f, 1.0f}};
constexpr ScoreEdges EDGES_CO   = {{0, 2, 4.5f, 9, 15, 35}};
constexpr ScoreEdges EDGES_IAQ  = {{0, 50, 100, 150, 250, 400}};
constexpr float SUBSCORES[6]    = {100, 85, 65, 45, 20, 0};

inline float lower(float a, float b) { return a < b ? a : b; }

inline float subScore(float v, const ScoreEdges& edges) {
  if (v <= edges.e[0]) return 100.0f;
  for (int i = 1; i < 6; ++i) {
    if (v <= edges.e[i]) {
      const float f = (v - edges.e[i - 1]) / (edges.e[i] - edges.e[i - 1]);
      return SUBSCORES[i - 1] + (SUBSCORES[i] - SUBSCORES[i - 1]) * f;
    }
  }
  return 0.0f;
}

inline int airScore(bool gasValid, float iaq, float tvoc, float hcho, float co, bool dustValid, float pm) {
  float worst = 101.0f;
  if (dustValid) worst = lower(worst, subScore(pm, EDGES_PM));
  if (gasValid) {
    worst = lower(worst, subScore(tvoc, EDGES_TVOC));
    worst = lower(worst, subScore(hcho, EDGES_HCHO));
    worst = lower(worst, subScore(co, EDGES_CO));
    worst = lower(worst, subScore(iaq, EDGES_IAQ));
  }
  if (worst > 100.0f) return -1;
  const int s = (int)(worst + 0.5f);
  return s < 1 ? 1 : s;
}

inline void scoreColor(int score, uint8_t& r, uint8_t& g, uint8_t& b) {
  static const int      pos[6]    = {0, 20, 40, 60, 80, 100};
  static const uint8_t  col[6][3] = {{208, 0, 75}, {255, 40, 40}, {255, 145, 0}, {200, 200, 0}, {0, 200, 90}, {0, 220, 120}};
  if (score < 0) { r = 0; g = 110; b = 255; return; }
  if (score > 100) score = 100;
  for (int i = 1; i < 6; ++i) {
    if (score <= pos[i]) {
      const float t = (float)(score - pos[i - 1]) / (float)(pos[i] - pos[i - 1]);
      r = (uint8_t)(col[i - 1][0] + (col[i][0] - col[i - 1][0]) * t + 0.5f);
      g = (uint8_t)(col[i - 1][1] + (col[i][1] - col[i - 1][1]) * t + 0.5f);
      b = (uint8_t)(col[i - 1][2] + (col[i][2] - col[i - 1][2]) * t + 0.5f);
      return;
    }
  }
}
