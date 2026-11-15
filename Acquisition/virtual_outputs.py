"""Virtual digital / analog outputs.

These are plain in-memory states. There is no hardware handle anywhere in this
class, by design: the project never drives a physical output.
"""
from __future__ import annotations

import numpy as np


class VirtualOutputs:
    def __init__(self, do_cfg, ao_cfg):
        self.do_cfg, self.ao_cfg = do_cfg, ao_cfg
        self.do = np.zeros(len(do_cfg), dtype=bool)
        self.ao = np.zeros(len(ao_cfg))
        self.do_safe = np.array([bool(d["safe_state"]) for d in do_cfg])
        self.ao_safe = np.array([a["safe_v"] for a in ao_cfg], dtype=float)
        self.ao_min = np.array([a["min_v"] for a in ao_cfg], dtype=float)
        self.ao_max = np.array([a["max_v"] for a in ao_cfg], dtype=float)
        self.ao_requested = np.zeros(len(ao_cfg))
        self.locked = False              # True after an abort: only safe states are accepted
        self.rejected_writes = 0

    def set_do_pattern(self, on_indices):
        """Energise exactly the listed outputs, de-energise all others."""
        if self.locked:
            self.rejected_writes += 1
            return False
        self.do[:] = False
        self.do[list(on_indices)] = True
        return True

    def set_ao(self, volts):
        """Command the virtual analog outputs; values are clamped to the 0-10 V range."""
        if self.locked:
            self.rejected_writes += 1
            return False
        self.ao_requested[:] = volts
        self.ao[:] = np.clip(volts, self.ao_min, self.ao_max)
        return True

    def apply_safe_state(self, lock=True):
        self.do[:] = self.do_safe
        self.ao[:] = self.ao_safe
        self.ao_requested[:] = self.ao_safe
        self.locked = self.locked or lock

    def unlock(self):
        self.locked = False

    def is_safe(self):
        return bool((self.do == self.do_safe).all() and np.allclose(self.ao, self.ao_safe))

    @property
    def ao_percent(self):
        return 100.0 * (self.ao - self.ao_min) / (self.ao_max - self.ao_min)

    @property
    def ao_saturated(self):
        """True when a command sits at (or was requested beyond) the 0-10 V limits."""
        return (self.ao_requested < self.ao_min - 1e-9) | (self.ao >= self.ao_max - 1e-9)
