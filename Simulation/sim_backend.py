"""Software sensor/actuator simulation backend with fault injection.

Produces RAW VOLTAGES exactly as a DAQ device would, so everything downstream
(scaling, health checks, safety, control, logging, FFT) runs on the same data
path it would use with hardware.
"""
from __future__ import annotations

import threading

import numpy as np

from DAQ_Hardware_Abstraction.backend_base import DaqBackend, DaqError
from .plant import SOURCES, SRC_INDEX, Plant

SLOW_FAULTS = ("disconnect", "stuck", "nan", "inf", "offset", "noise")
HF_FAULTS = ("disconnect", "stuck", "nan")
DI_FAULTS = ("stuck_low", "stuck_high", "invert")


class SimulationBackend(DaqBackend):
    name = "simulation"
    is_simulation = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.sim = cfg["simulation"]
        acq = cfg["acquisition"]
        self.fs_slow = float(acq["slow_rate_hz"])
        self.fs_hf = float(acq["hf_rate_hz"])
        self.rng = np.random.default_rng(self.sim["seed"])
        self.rng_hf = np.random.default_rng(self.sim["seed"] + 1)
        self.plant = Plant(self.sim)
        self.lock = threading.Lock()
        self._open = False

        sc = cfg["slow_channels"]
        self.n_slow = len(sc)
        self._src = np.array([SRC_INDEX[c["sim"]["source"]] for c in sc])
        self._k = np.array([c["sim"]["k"] for c in sc])
        self._b = np.array([c["sim"]["b"] for c in sc])
        self._noise = np.array([c["sim"]["noise_v"] for c in sc])
        # The simulated sensor's TRUE transfer function is frozen at start-up, so
        # editing the calibration in the GUI changes the scaled value, as it would on a rig.
        self._true_gain = np.array([c["gain"] for c in sc], dtype=float)
        self._true_offset = np.array([c["offset"] for c in sc], dtype=float)
        self._last_raw = np.full(self.n_slow, 1.0)

        hc = cfg["hf_channels"]
        self.n_hf = len(hc)
        nt = max(len(c["sim"]["tones"]) for c in hc) + 1     # +1 slot: injected instability tone
        self._hf_f = np.zeros((self.n_hf, nt))
        self._hf_a = np.zeros((self.n_hf, nt))
        for i, c in enumerate(hc):
            for j, (f, a) in enumerate(c["sim"]["tones"]):
                self._hf_f[i, j], self._hf_a[i, j] = f, a
        self._hf_idle = np.array([c["sim"]["idle_gain"] for c in hc])
        self._hf_noise = np.array([c["sim"]["noise_v"] for c in hc])
        self._hf_phase = self.rng_hf.uniform(0, 2 * np.pi, size=(self.n_hf, nt))
        self._hf_last = np.zeros(self.n_hf, dtype=np.float32)
        self._hf_dyn = [i for i, c in enumerate(hc) if c["sensor_type"] != "reference"]

        self.di_cfg = cfg["digital_inputs"]
        self.n_di = len(self.di_cfg)
        self._di = np.zeros(self.n_di, dtype=bool)

        self._do = np.zeros(len(cfg["digital_outputs"]), dtype=bool)
        self._ao = np.zeros(2)
        self.n_slow_read = 0        # sample counters (the simulated sample clocks)
        self.n_hf_read = 0

        # ---- fault state (all operator-injectable)
        self.slow_faults = {}       # index -> {"type":..., "value":...}
        self.hf_faults = {}
        self.di_faults = {}         # index -> "stuck_low" | "stuck_high" | "invert"
        self.estop_active = False
        self.overpressure = False
        self._spike_until = -1
        self.acq_error = None       # None | "error" | "timeout"
        self.instability = False
        self._transient_at = None   # hf sample index of a pending/active burst

    # ------------------------------------------------------------------ lifecycle
    def open(self):
        self._open = True

    def close(self):
        self._open = False

    def is_ready(self):
        return self._open and self.acq_error is None

    def discover_devices(self):
        return [{"name": "SIM", "product_type": "Software simulation backend", "simulated": True,
                 "ai": self.n_slow + self.n_hf, "di": self.n_di, "do": len(self._do), "ao": 2}]

    # ------------------------------------------------------------------ fault API
    def inject_slow_fault(self, index, ftype, value=0.0):
        if ftype not in SLOW_FAULTS:
            raise ValueError(f"unknown slow fault type {ftype}")
        with self.lock:
            self.slow_faults[int(index)] = {"type": ftype, "value": float(value)}

    def inject_hf_fault(self, index, ftype):
        if ftype not in HF_FAULTS:
            raise ValueError(f"unknown HF fault type {ftype}")
        with self.lock:
            self.hf_faults[int(index)] = {"type": ftype}

    def inject_di_fault(self, index, ftype):
        if ftype not in DI_FAULTS:
            raise ValueError(f"unknown DI fault type {ftype}")
        with self.lock:
            self.di_faults[int(index)] = ftype

    def trigger_pressure_spike(self):
        with self.lock:
            self._spike_until = self.n_slow_read + int(self.sim["spike_duration_s"] * self.fs_slow)

    def trigger_hf_transient(self):
        with self.lock:
            self._transient_at = self.n_hf_read

    def clear_faults(self):
        with self.lock:
            self.slow_faults.clear()
            self.hf_faults.clear()
            self.di_faults.clear()
            self.estop_active = False
            self.overpressure = False
            self._spike_until = -1
            self.acq_error = None
            self.instability = False

    def active_faults(self):
        with self.lock:
            out = [f"slow[{i}]:{f['type']}" for i, f in sorted(self.slow_faults.items())]
            out += [f"hf[{i}]:{f['type']}" for i, f in sorted(self.hf_faults.items())]
            out += [f"di[{i}]:{f}" for i, f in sorted(self.di_faults.items())]
            for flag, label in ((self.estop_active, "E-STOP"), (self.overpressure, "overpressure"),
                                (self.instability, "hf instability tone"),
                                (self.acq_error, f"acquisition {self.acq_error}")):
                if flag:
                    out.append(label)
            return out

    # ------------------------------------------------------------------ outputs
    def apply_virtual_outputs(self, do_states, ao_volts):
        with self.lock:
            self._do[:] = do_states
            self._ao[:] = np.clip(ao_volts, 0.0, 10.0)

    # ------------------------------------------------------------------ slow AI
    def read_slow(self, n_samples):
        with self.lock:
            if self.acq_error == "error":
                raise DaqError("Simulated DAQ error: device reported a hardware fault", -50000)
            if self.acq_error == "timeout":
                raise DaqError("Simulated DAQ error: read timed out, samples not available", -200284)
            dt = 1.0 / self.fs_slow
            traj = np.empty((n_samples, len(SOURCES)))
            for i in range(n_samples):
                spike = (self.n_slow_read + i) < self._spike_until
                traj[i] = self.plant.step(dt, self._ao, self._do, self.overpressure, spike)
            self.n_slow_read += n_samples
            eng = traj[:, self._src] * self._k + self._b
            raw = (eng - self._true_offset) / self._true_gain
            raw += self.rng.standard_normal(raw.shape) * self._noise
            for idx, f in self.slow_faults.items():
                t = f["type"]
                if t == "disconnect":       # open circuit: input floats near 0 V
                    raw[:, idx] = 0.01 + 0.003 * self.rng.standard_normal(n_samples)
                elif t == "stuck":          # frozen value, no noise
                    raw[:, idx] = self._last_raw[idx]
                elif t == "nan":
                    raw[:, idx] = np.nan
                elif t == "inf":
                    raw[:, idx] = np.inf
                elif t == "offset":
                    raw[:, idx] += f["value"]
                elif t == "noise":
                    raw[:, idx] += self.rng.standard_normal(n_samples) * max(f["value"], 0.05)
            finite = np.isfinite(raw[-1])
            self._last_raw[finite] = raw[-1][finite]
            self._update_di(traj[-1])
            return raw

    def _update_di(self, last):
        L = self.plant.do_lag
        for i, d in enumerate(self.di_cfg):
            s = d["sim"]
            t = s["type"]
            if t == "const":
                v = bool(s["value"])
            elif t == "estop_ok":
                v = not self.estop_active
            elif t == "do_open":
                v = L[s["do"]] > 0.9
            elif t == "do_closed":
                v = L[s["do"]] < 0.1
            elif t == "pc_gt":
                v = last[SRC_INDEX["p_chamber"]] > s["value"]
            elif t == "burning":
                v = self.plant.burning
            elif t == "mass_lt":
                v = last[SRC_INDEX["mass_prop"]] < s["value"]
            else:
                v = False
            f = self.di_faults.get(i)
            if f == "stuck_low":
                v = False
            elif f == "stuck_high":
                v = True
            elif f == "invert":
                v = not v
            self._di[i] = v

    def read_di(self):
        with self.lock:
            return self._di.copy()

    # ------------------------------------------------------------------ HF AI
    def read_hf(self, n_samples):
        with self.lock:
            if self.acq_error is not None:
                raise DaqError(f"Simulated DAQ error on high-frequency task ({self.acq_error})", -50000)
            n0 = self.n_hf_read
            self.n_hf_read += n_samples
            level = self.plant.combustion_level
            faults = dict(self.hf_faults)
            instab = self.instability
            tr_at = self._transient_at
        fs = self.fs_hf
        k = np.arange(n_samples)
        amp = self._hf_a * (self._hf_idle + (1.0 - self._hf_idle) * level)[:, None]
        f = self._hf_f.copy()
        if instab:                                   # injected combustion-instability-like tone
            f[self._hf_dyn, -1] = self.sim["hf_instability_freq_hz"]
            amp[self._hf_dyn, -1] = self.sim["hf_instability_amp_v"]
        # phase kept modulo 1 cycle so precision does not degrade over long runs
        ph0 = (f * n0 / fs) % 1.0
        phase = 2 * np.pi * (ph0[:, :, None] + f[:, :, None] * k[None, None, :] / fs) \
            + self._hf_phase[:, :, None]
        sig = (amp[:, :, None] * np.sin(phase)).sum(axis=1)             # (n_hf, n)
        sig += self.rng_hf.standard_normal(sig.shape) * self._hf_noise[:, None]
        if tr_at is not None:                        # decaying burst (transient event)
            tau = self.sim["hf_transient_decay_s"]
            trel = (n0 + k - tr_at) / fs
            m = (trel >= 0) & (trel < 12 * tau)
            if m.any():
                burst = self.sim["hf_transient_amp_v"] * np.exp(-trel[m] / tau) * \
                    np.sin(2 * np.pi * self.sim["hf_transient_freq_hz"] * trel[m])
                sig[np.ix_(self._hf_dyn, np.nonzero(m)[0])] += burst
            elif trel[0] >= 12 * tau:
                with self.lock:
                    if self._transient_at == tr_at:
                        self._transient_at = None
        out = np.clip(sig, -10.0, 10.0).T.astype(np.float32)           # (n, n_hf)
        for idx, fl in faults.items():
            t = fl["type"]
            if t == "disconnect":
                out[:, idx] = (0.002 * self.rng_hf.standard_normal(n_samples)).astype(np.float32)
            elif t == "stuck":
                out[:, idx] = self._hf_last[idx]
            elif t == "nan":
                out[:, idx] = np.nan
        last = out[-1]
        ok = np.isfinite(last)
        self._hf_last[ok] = last[ok]
        return out
