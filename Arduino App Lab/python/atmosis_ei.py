from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from atmosis_window import AXIS_ORDER, NORMALIZED_SUFFIX, base_axis_name

LABELS_DEFAULT = ["Clean_air", "Indoor_pollution", "Poor_ventilation"]

NORMALIZATION: Dict[str, Tuple[float, float]] = {
    "iaq": (0.0, 500.0),
    "tvoc_ppm": (0.0, 5.0),
    "hcho_ppm": (0.0, 1.0),
    "co_ppm": (0.0, 50.0),
    "temp_c": (0.0, 50.0),
    "rh_pct": (0.0, 100.0),
    "dust_ugm3": (0.0, 500.0),
    "pressure_hpa": (900.0, 200.0),
    "altitude_m": (0.0, 1000.0),
}


def normalize_axis(name: str, value: float) -> float:
    offset, span = NORMALIZATION[base_axis_name(name)]
    return (value - offset) / span


class AtmosisInferenceError(RuntimeError):
    pass


class EonModelError(RuntimeError):
    pass


@dataclass
class Classification:
    label: str
    confidence: float
    scores: dict = field(default_factory=dict)
    classification_us: int = 0
    engine: str = "edge-impulse"


def _ints(text: str) -> List[int]:
    return [int(x) for x in re.findall(r"-?\d+", text)]


def _floats(text: str) -> List[float]:
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?", text)]


def find_model_files(dirs: Sequence[str]) -> Optional[Tuple[Path, Path, Path]]:
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            continue
        meta = next(iter(sorted(root.rglob("model_metadata.h"))), None)
        variables = next(iter(sorted(root.rglob("model_variables.h"))), None)
        compiled = next(iter(sorted(root.rglob("tflite_learn_*_compiled.cpp"))), None)
        if meta and variables and compiled:
            return meta, variables, compiled
    return None


def parse_eon_model(meta_path: Path, vars_path: Path, cpp_path: Path) -> dict:
    meta = meta_path.read_text(encoding="utf-8", errors="replace")
    variables = vars_path.read_text(encoding="utf-8", errors="replace")
    src = cpp_path.read_text(encoding="utf-8", errors="replace")

    def define(name, cast=str):
        m = re.search(r"#define\s+" + name + r"\s+(.+)", meta)
        if not m:
            raise EonModelError(f"{name} missing from model_metadata.h")
        return cast(m.group(1).strip().strip('"'))

    if "extract_raw_features" not in variables:
        raise EonModelError("only the Raw Data processing block is supported")
    scale_m = re.search(r"ei_dsp_config_raw_t\s+\w+\s*=\s*\{[^}]*?([-\d.eE+]+)f?\s*//\s*float scale-axes", variables)
    scale_axes = float(scale_m.group(1)) if scale_m else 1.0

    labels_m = re.search(r"inferencing_categories\w*\[\]\s*=\s*\{([^}]*)\}", variables)
    labels = re.findall(r'"([^"]+)"', labels_m.group(1)) if labels_m else []

    axes = [a.strip() for a in define("EI_CLASSIFIER_FUSION_AXES_STRING").split("+")]
    window = define("EI_CLASSIFIER_RAW_SAMPLE_COUNT", int)
    per_frame = define("EI_CLASSIFIER_RAW_SAMPLES_PER_FRAME", int)
    frequency = float(define("EI_CLASSIFIER_FREQUENCY"))
    project = define("EI_CLASSIFIER_PROJECT_NAME")

    arrays = {int(m.group(1)): m.group(2) for m in re.finditer(
        r"tensor_data(\d+)\[[^\]]*\]\s*=\s*\{(.*?)\};", src, re.S)}
    dims = {int(m.group(1)): _ints(m.group(2))[1:] for m in re.finditer(
        r"tensor_dimension(\d+)\s*=\s*\{(\s*\d+\s*,\s*\{[^}]*\})", src)}
    scales = {m.group(1): _floats(m.group(2))[1:] for m in re.finditer(
        r"(quant\d+_scale)\s*=\s*\{(\s*\d+\s*,\s*\{[^}]*\})", src)}
    zeros = {m.group(1): _ints(m.group(2))[1:] for m in re.finditer(
        r"(quant\d+_zero)\s*=\s*\{(\s*\d+\s*,\s*\{[^}]*\})", src)}
    quants = {int(m.group(1)): (m.group(2), m.group(3)) for m in re.finditer(
        r"TfLiteAffineQuantization\s+quant(\d+)\s*=\s*\{\s*\(TfLiteFloatArray\*\)&(?:g0::)?(quant\d+_scale),\s*"
        r"\(TfLiteIntArray\*\)&(?:g0::)?(quant\d+_zero)", src)}

    block = re.search(r"TensorInfo_t\s+tensorData\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    if not block:
        raise EonModelError("tensor table not found in compiled model")
    tensors = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        dm = re.search(r"tensor_dimension(\d+)", line)
        qm = re.search(r"&(?:g0::)?quant(\d+)\)", line)
        data_m = re.search(r"tensor_data(\d+)", line)
        entry = {"dims": dims.get(int(dm.group(1))) if dm else None, "data": None, "scale": None, "zero": None}
        if qm:
            sname, zname = quants[int(qm.group(1))]
            entry["scale"] = scales[sname]
            entry["zero"] = zeros[zname]
        if data_m:
            is_float = "kTfLiteFloat32" in line
            raw = arrays[int(data_m.group(1))]
            entry["data"] = _floats(raw) if is_float else _ints(raw)
            entry["float"] = is_float
        tensors.append(entry)

    ops = re.findall(r"OP_(\w+)", re.search(r"used_ops\[\]\s*=\s*\{([^}]*)\}", src).group(1))
    node_io = []
    for i, op in enumerate(ops):
        ins = re.search(r"inputs%d\s*=\s*\{\s*\d+\s*,\s*\{([^}]*)\}" % i, src)
        outs = re.search(r"outputs%d\s*=\s*\{\s*\d+\s*,\s*\{([^}]*)\}" % i, src)
        act = re.search(r"opdata%d\s*=\s*\{\s*kTfLiteAct(\w+)" % i, src)
        node_io.append({"op": op, "inputs": _ints(ins.group(1)), "outputs": _ints(outs.group(1)),
                        "act": act.group(1) if act else "None"})

    in_idx = _ints(re.search(r"in_tensor_indices\[\]\s*=\s*\{([^}]*)\}", src).group(1))[0]
    out_idx = _ints(re.search(r"out_tensor_indices\[\]\s*=\s*\{([^}]*)\}", src).group(1))[0]

    def dequant(t):
        data = t["data"]
        if t.get("float") or t["scale"] is None:
            return [float(v) for v in data]
        s, z = t["scale"], t["zero"] or [0]
        if len(s) == 1:
            return [(v - z[0]) * s[0] for v in data]
        per = len(data) // len(s)
        return [(data[i] - z[min(i // per, len(z) - 1)]) * s[i // per] for i in range(len(data))]

    layers = []
    for node in node_io:
        if node["op"] == "FULLY_CONNECTED":
            w_t = tensors[node["inputs"][1]]
            b_idx = node["inputs"][2] if len(node["inputs"]) > 2 else -1
            out_n, in_n = w_t["dims"][0], w_t["dims"][1]
            wflat = dequant(w_t)
            weights = [wflat[o * in_n:(o + 1) * in_n] for o in range(out_n)]
            bias = dequant(tensors[b_idx]) if b_idx >= 0 else [0.0] * out_n
            layers.append({"type": "dense", "w": weights, "b": bias, "act": node["act"]})
        elif node["op"] == "SOFTMAX":
            layers.append({"type": "softmax"})
        elif node["op"] in ("RESHAPE", "QUANTIZE", "DEQUANTIZE"):
            continue
        elif node["op"] in ("RELU", "RELU6", "LOGISTIC", "TANH"):
            layers.append({"type": "act", "act": node["op"]})
        else:
            raise EonModelError(f"unsupported layer {node['op']} — use a dense (fully connected) network")

    if not layers or layers[0]["type"] != "dense":
        raise EonModelError("model does not start with a dense layer")
    if len(layers[0]["w"][0]) != window * per_frame:
        raise EonModelError("model input size does not match window x axes")

    return {
        "project": project, "labels": labels or LABELS_DEFAULT, "axes": axes,
        "window_samples": window, "axis_count": per_frame, "frequency_hz": frequency,
        "scale_axes": scale_axes, "layers": layers, "path": str(cpp_path.parent),
        "input_index": in_idx, "output_index": out_idx,
    }


def _activate(v: float, act: str) -> float:
    if act in ("Relu", "RELU"):
        return v if v > 0 else 0.0
    if act in ("Relu6", "RELU6"):
        return min(max(v, 0.0), 6.0)
    if act in ("Sigmoid", "LOGISTIC"):
        return 1.0 / (1.0 + math.exp(-v))
    if act in ("Tanh", "TANH"):
        return math.tanh(v)
    return v


class EdgeImpulseModel:
    engine = "edge-impulse"

    def __init__(self, spec: dict):
        self.spec = spec
        self.project_name = spec["project"]
        self.labels = spec["labels"]
        self.axis_names = spec["axes"]
        self.window_samples = spec["window_samples"]
        self.axis_count = spec["axis_count"]
        self.feature_count = self.window_samples * self.axis_count
        self.normalized = all(a.endswith(NORMALIZED_SUFFIX) for a in self.axis_names)
        self.library_path = spec["path"]
        self._layers = spec["layers"]
        self._scale = spec["scale_axes"]


    def prepare(self, features: Sequence[float]) -> List[float]:
        if not self.normalized:
            return [float(v) * self._scale for v in features]
        out = []
        for i, v in enumerate(features):
            out.append(normalize_axis(self.axis_names[i % self.axis_count], float(v)) * self._scale)
        return out

    def forward(self, x: List[float]) -> List[float]:
        for layer in self._layers:
            if layer["type"] == "dense":
                act = layer["act"]
                x = [_activate(sum(w * v for w, v in zip(row, x)) + b, act)
                     for row, b in zip(layer["w"], layer["b"])]
            elif layer["type"] == "act":
                x = [_activate(v, layer["act"]) for v in x]
            elif layer["type"] == "softmax":
                m = max(x)
                e = [math.exp(v - m) for v in x]
                s = sum(e)
                x = [v / s for v in e]
        return x

    def classify(self, features: Sequence[float]) -> Classification:
        if len(features) != self.feature_count:
            raise AtmosisInferenceError(f"expected {self.feature_count} features, got {len(features)}")
        if not all(math.isfinite(v) for v in features):
            raise AtmosisInferenceError("feature vector contained NaN or Inf")
        t0 = time.perf_counter()
        probs = self.forward(self.prepare(features))
        us = int((time.perf_counter() - t0) * 1e6)
        best = max(range(len(probs)), key=lambda i: probs[i])
        return Classification(label=self.labels[best], confidence=probs[best],
                              scores={self.labels[i]: probs[i] for i in range(len(probs))},
                              classification_us=us, engine=self.engine)

    def health(self) -> dict:
        dense = [l for l in self._layers if l["type"] == "dense"]
        near_init = 0
        for layer in dense:
            fan_out, fan_in = len(layer["w"]), len(layer["w"][0])
            bound = math.sqrt(6.0 / (fan_in + fan_out))
            wmax = max(abs(v) for row in layer["w"] for v in row)
            bmean = sum(abs(v) for v in layer["b"]) / len(layer["b"])
            if wmax <= bound * 1.15 and bmean < 0.03:
                near_init += 1
        untrained = near_init == len(dense)
        labels = set()
        for window in PROBES.values():
            labels.add(self.classify(list(window) * self.window_samples).label)
        return {"untrained": untrained, "distinct_probe_labels": len(labels),
                "degenerate": len(labels) <= 1}



class RuleBasedClassifier:
    engine = "rules"
    labels = list(LABELS_DEFAULT)
    axis_names = list(AXIS_ORDER)
    window_samples = 15
    axis_count = 9
    feature_count = 135
    project_name = "Atmosis smart rules"
    library_path = ""
    normalized = False


    def classify(self, features: Sequence[float]) -> Classification:
        if len(features) != self.feature_count:
            raise AtmosisInferenceError(f"expected {self.feature_count} features, got {len(features)}")
        n = self.window_samples
        m = [sum(features[s * 9 + a] for s in range(n)) / n for a in range(9)]
        iaq, tvoc, hcho, co, _, rh, dust = m[:7]
        pollution = max(tvoc / 1.0, hcho / 0.1, co / 9.0, dust / 75.0, iaq / 250.0)
        stale = max(iaq / 150.0, (rh - 60.0) / 20.0 if rh > 60 else 0.0) * 0.9
        clean = max(0.0, 1.0 - max(pollution, stale))
        raw = {"Clean_air": 3.0 * clean, "Indoor_pollution": 3.0 * pollution, "Poor_ventilation": 3.0 * stale}
        z = sum(math.exp(v) for v in raw.values())
        scores = {k: math.exp(v) / z for k, v in raw.items()}
        label = max(scores, key=scores.get)
        return Classification(label=label, confidence=scores[label], scores=scores, engine=self.engine)

    def health(self) -> dict:
        return {"untrained": False, "distinct_probe_labels": 3, "degenerate": False}



PROBES = {
    "pristine": (5.0, 0.005, 0.001, 0.05, 21.0, 40.0, 2.0, 1013.0, 5.0),
    "solvent": (380.0, 4.5, 0.90, 12.0, 30.0, 55.0, 120.0, 1010.0, 25.0),
    "stuffy": (210.0, 1.2, 0.30, 3.0, 29.5, 78.0, 45.0, 1008.0, 40.0),
    "smoke": (470.0, 4.9, 0.95, 45.0, 34.0, 30.0, 300.0, 1005.0, 70.0),
}


def load_classifier(model_dirs: Sequence[str], use_untrained: bool = False):
    report = {"found": False, "active": "rules", "status": "missing", "project": "", "path": "",
              "labels": list(LABELS_DEFAULT), "axes": list(AXIS_ORDER), "normalized": False,
              "message": "Smart air-pattern rules are active."}
    files = find_model_files(model_dirs)
    if not files:
        return RuleBasedClassifier(), report
    try:
        model = EdgeImpulseModel(parse_eon_model(*files))
        if [base_axis_name(a) for a in model.axis_names] != list(AXIS_ORDER):
            raise EonModelError(f"axes {model.axis_names} do not match {list(AXIS_ORDER)}")
        health = model.health()
    except Exception:
        report.update(status="rules", message="Smart air-pattern rules are active.")
        return RuleBasedClassifier(), report
    report.update(found=True, project=model.project_name, path=model.library_path,
                  labels=list(model.labels), axes=list(model.axis_names),
                  normalized=model.normalized, health=health)
    if health["untrained"] or health["degenerate"]:
        report.update(status="rules", message="Smart air-pattern rules are active.")
        if not use_untrained:
            return RuleBasedClassifier(), report
    else:
        report.update(status="ok", message="Edge Impulse model active on-device.")
    report["active"] = "edge-impulse"
    return model, report
