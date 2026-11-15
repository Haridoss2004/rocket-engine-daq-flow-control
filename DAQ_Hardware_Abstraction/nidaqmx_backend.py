"""NI-DAQmx backend - FUTURE INTEGRATION OPTION, INPUTS ONLY.

What is implemented with real NI-DAQmx calls (through the `nidaqmx` Python API,
which wraps the same driver LabVIEW uses):
  * device discovery
  * AI task for the 50 slow channels (hardware-timed, continuous)
  * AI task for the 25 high-frequency channels (hardware-timed, continuous)
  * DI task for the 30 digital inputs (on-demand)
  * DAQmx error reporting (mapped to DaqError) and task cleanup

What is deliberately NOT implemented:
  * any DO or AO task. This class never creates an output task; the virtual
    outputs stay in memory. write_physical_outputs() always raises.

Safety interlocks:
  * the backend is never selected automatically (default backend = simulation)
  * it refuses to open non-simulated (physical) devices unless
    allow_physical_devices is explicitly set in the configuration
"""
from __future__ import annotations

import numpy as np

from .backend_base import DaqBackend, DaqError

try:
    import nidaqmx
    import nidaqmx.system
    from nidaqmx.constants import AcquisitionType, LineGrouping
    from nidaqmx.stream_readers import AnalogMultiChannelReader
    HAVE_NIDAQMX = True
except Exception as _e:                               # pragma: no cover
    HAVE_NIDAQMX = False
    _IMPORT_ERROR = _e


def discover_devices():
    """List DAQmx devices visible to the driver (same list NI MAX shows)."""
    if not HAVE_NIDAQMX:
        return []
    out = []
    for d in nidaqmx.system.System.local().devices:
        out.append({"name": d.name, "product_type": d.product_type, "simulated": bool(d.is_simulated),
                    "ai": len(d.ai_physical_chans), "ao": len(d.ao_physical_chans),
                    "di": len(d.di_lines), "do": len(d.do_lines),
                    "ai_max_rate": float(d.ai_max_single_chan_rate) if len(d.ai_physical_chans) else 0.0})
    return out


def driver_version():
    if not HAVE_NIDAQMX:
        return None
    v = nidaqmx.system.System.local().driver_version
    return f"{v.major_version}.{v.minor_version}.{v.update_version}"


def auto_map(n_slow, n_hf, n_di, hf_rate, simulated_only=True):
    """Pick physical channels for the three input groups from the discovered devices.
    High-rate capable modules (>= hf_rate, few channels) are used for the HF group."""
    devs = [d for d in discover_devices() if d["simulated"] or not simulated_only]
    hf_devs = [d for d in devs if 0 < d["ai"] <= 8 and d["ai_max_rate"] >= hf_rate]
    slow_devs = [d for d in devs if d["ai"] > 8]

    def take(dev_list, n, kind):
        chans = []
        for d in dev_list:
            for i in range(d[kind]):
                if len(chans) < n:
                    chans.append(f"{d['name']}/ai{i}" if kind == "ai" else f"{d['name']}/port0/line{i}")
        if len(chans) < n:
            raise DaqError(f"auto_map: only {len(chans)} of {n} required {kind} channels available")
        return chans
    return {"slow": take(slow_devs, n_slow, "ai"), "hf": take(hf_devs, n_hf, "ai"),
            "di": take([d for d in devs if d["di"] > 0], n_di, "di")}


class NiDaqmxInputBackend(DaqBackend):
    name = "nidaqmx (inputs only)"
    is_simulation = False

    def __init__(self, cfg, mapping=None, allow_physical_devices=False):
        if not HAVE_NIDAQMX:
            raise DaqError(f"nidaqmx Python package / NI-DAQmx driver not available: {_IMPORT_ERROR}")
        self.cfg = cfg
        acq = cfg["acquisition"]
        self.fs_slow, self.fs_hf = float(acq["slow_rate_hz"]), float(acq["hf_rate_hz"])
        self.n_slow, self.n_hf = len(cfg["slow_channels"]), len(cfg["hf_channels"])
        self.n_di = len(cfg["digital_inputs"])
        self.allow_physical = bool(allow_physical_devices)
        self.mapping = mapping or auto_map(self.n_slow, self.n_hf, self.n_di, self.fs_hf,
                                           simulated_only=not self.allow_physical)
        self._tasks = []
        self._slow = self._hf = self._di = None
        self.actual_slow_rate = self.actual_hf_rate = None
        self.all_simulated = False

    def discover_devices(self):
        return discover_devices()

    def _check_devices(self):
        sim = {d["name"]: d["simulated"] for d in discover_devices()}
        used = {c.split("/")[0] for grp in self.mapping.values() for c in grp}
        missing = sorted(u for u in used if u not in sim)
        if missing:
            raise DaqError(f"DAQmx devices not found: {', '.join(missing)}")
        physical = sorted(u for u in used if not sim[u])
        if physical and not self.allow_physical:
            raise DaqError("Refusing to open PHYSICAL devices (" + ", ".join(physical) + "). "
                           "Set allow_physical_devices only after an independent engineering review.")
        self.all_simulated = not physical

    def open(self):
        self._check_devices()
        try:
            self._slow = nidaqmx.Task("rocket_daq_slow_ai")
            self._tasks.append(self._slow)
            self._slow.ai_channels.add_ai_voltage_chan(",".join(self.mapping["slow"]),
                                                       min_val=-10.0, max_val=10.0)
            self._slow.timing.cfg_samp_clk_timing(self.fs_slow, sample_mode=AcquisitionType.CONTINUOUS,
                                                  samps_per_chan=int(self.fs_slow * 5))
            self._hf = nidaqmx.Task("rocket_daq_hf_ai")
            self._tasks.append(self._hf)
            self._hf.ai_channels.add_ai_voltage_chan(",".join(self.mapping["hf"]),
                                                     min_val=-5.0, max_val=5.0)
            self._hf.timing.cfg_samp_clk_timing(self.fs_hf, sample_mode=AcquisitionType.CONTINUOUS,
                                                samps_per_chan=int(self.fs_hf * 5))
            self._di = nidaqmx.Task("rocket_daq_di")
            self._tasks.append(self._di)
            self._di.di_channels.add_di_chan(",".join(self.mapping["di"]),
                                             line_grouping=LineGrouping.CHAN_PER_LINE)
            self.actual_slow_rate = float(self._slow.timing.samp_clk_rate)
            self.actual_hf_rate = float(self._hf.timing.samp_clk_rate)   # may be coerced by the module
            self._slow_reader = AnalogMultiChannelReader(self._slow.in_stream)
            self._hf_reader = AnalogMultiChannelReader(self._hf.in_stream)
            for t in self._tasks:
                t.start()
        except nidaqmx.DaqError as e:
            self.close()
            raise DaqError(f"DAQmx configuration failed: {e}", getattr(e, "error_code", None)) from e

    def close(self):
        """Stop and clear every task, even if some of them fail."""
        for t in self._tasks:
            for op in (t.stop, t.close):
                try:
                    op()
                except Exception:
                    pass
        self._tasks = []
        self._slow = self._hf = self._di = None

    def is_ready(self):
        return self._slow is not None

    def _read(self, reader, n_ch, n):
        buf = np.empty((n_ch, n), dtype=np.float64)
        try:
            reader.read_many_sample(buf, number_of_samples_per_channel=n, timeout=2.0)
        except nidaqmx.DaqError as e:
            raise DaqError(f"DAQmx read failed: {e}", getattr(e, "error_code", None)) from e
        return buf.T

    def read_slow(self, n_samples):
        return self._read(self._slow_reader, self.n_slow, n_samples)

    def read_hf(self, n_samples):
        return self._read(self._hf_reader, self.n_hf, n_samples).astype(np.float32)

    def read_di(self):
        try:
            return np.array(self._di.read(), dtype=bool)
        except nidaqmx.DaqError as e:
            raise DaqError(f"DAQmx DI read failed: {e}", getattr(e, "error_code", None)) from e

    def apply_virtual_outputs(self, do_states, ao_volts):
        """Intentionally a no-op: virtual outputs are never written to hardware."""
        return None
