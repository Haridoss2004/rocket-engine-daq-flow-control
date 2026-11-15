"""Configuration loading, validation and saving (JSON)."""
from __future__ import annotations

import copy
import json
import math
import os

from .defaults import N_AO, N_DI, N_DO, N_HF, N_SLOW, default_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "Configuration", "system_config.json")


class ConfigError(ValueError):
    pass


def _merge(base, over):
    """Recursive dict merge; lists are replaced as a whole."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate(cfg):
    counts = (("slow_channels", N_SLOW), ("hf_channels", N_HF), ("digital_inputs", N_DI),
              ("digital_outputs", N_DO), ("analog_outputs", N_AO))
    for key, n in counts:
        if len(cfg[key]) != n:
            raise ConfigError(f"{key}: expected {n} channels, found {len(cfg[key])}")
        ids = [c["id"] for c in cfg[key]]
        if len(set(ids)) != n:
            raise ConfigError(f"{key}: channel ids are not unique")
        for i, c in enumerate(cfg[key]):
            if c["index"] != i:
                raise ConfigError(f"{key}[{i}]: index field mismatch")
    for c in cfg["slow_channels"] + cfg["hf_channels"]:
        for f in ("gain", "offset"):
            if not isinstance(c[f], (int, float)) or not math.isfinite(c[f]):
                raise ConfigError(f"{c['id']}: {f} must be a finite number")
        if c["gain"] == 0:
            raise ConfigError(f"{c['id']}: gain must not be zero")
    for ao in cfg["analog_outputs"]:
        if not (0.0 <= ao["min_v"] < ao["max_v"] <= 10.0):
            raise ConfigError(f"{ao['id']}: range must lie within 0..10 V")
    fc = cfg["flow_control"]
    if fc["target_total_flow_kg_s"] < 0 or fc["target_mixture_ratio"] <= 0:
        raise ConfigError("flow_control: targets must be positive")
    slow_ids = {c["id"] for c in cfg["slow_channels"]}
    for k in ("ox_flow_channel", "fuel_flow_channel"):
        if fc[k] not in slow_ids:
            raise ConfigError(f"flow_control.{k}: unknown channel {fc[k]}")
    n = cfg["fft"]["size"]
    if n < 16 or n & (n - 1):
        raise ConfigError("fft.size must be a power of two >= 16")
    if cfg["fft"]["channel"] not in {c["id"] for c in cfg["hf_channels"]}:
        raise ConfigError("fft.channel: unknown high-frequency channel")
    return cfg


def load_config(path=None, create=True):
    """Load configuration, layering the JSON file over the built-in defaults."""
    path = path or DEFAULT_PATH
    cfg = default_config()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    elif create:
        save_config(cfg, path)
    return validate(cfg)


def save_config(cfg, path=None):
    path = path or DEFAULT_PATH
    validate(cfg)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, path)
    return path
