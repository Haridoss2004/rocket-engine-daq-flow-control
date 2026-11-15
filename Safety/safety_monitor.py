"""Independent software safety monitor and central alarm manager (SIMULATION).

The monitor only looks at acquired data and reports trips. It has no knowledge
of the sequence logic, so a sequencer bug cannot mask an abort condition.

Abort conditions:
  A  pressure above the configured demonstration abort threshold
  B  pressure sensor failure: disconnected / out of range, NaN, Infinity, stale
  C  emergency-stop input active
  D  critical acquisition error, or acquisition watchdog time-out
All thresholds are DEMONSTRATION values, not real engine safety limits.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from Acquisition.scaling import ChStatus


@dataclass
class Trip:
    code: str          # OVERPRESSURE | SENSOR_FAILURE | ESTOP | DAQ_ERROR | WATCHDOG | OPERATOR | SEQUENCE
    reason: str
    always: bool       # True: aborts in every state. False: only while the test is armed.


_FAIL_TEXT = {ChStatus.OUT_OF_RANGE: "out of range / disconnected",
              ChStatus.INVALID: "invalid data (NaN or Infinity)",
              ChStatus.STALE: "stale (frozen) measurement"}


class SafetyMonitor:
    def __init__(self, cfg):
        s = cfg["safety"]
        self.channels = cfg["slow_channels"]
        self.debounce = max(1, int(s["pressure_abort_debounce_samples"]))
        self.watchdog_timeout = float(s["watchdog_timeout_s"])
        self.estop_index = next(d["index"] for d in cfg["digital_inputs"]
                                if d["id"] == s["estop_di_channel"])
        self.reload_channels(self.channels)
        self._over_run = np.zeros(len(self.channels), dtype=np.int64)
        self._acq_error = None
        self.last_block_time = None
        self.active_conditions = []        # conditions present in the most recent evaluation
        self.active_alarms = {}            # key -> text (all channels, incl. non-abort warnings)

    def reload_channels(self, channels):
        self.channels = channels
        self.pressure_idx = np.array([c["index"] for c in channels
                                      if c["sensor_type"] == "pressure" and c.get("enabled", True)],
                                     dtype=int)
        self.abort_hi = np.array([np.inf if c.get("abort_hi") is None else c["abort_hi"]
                                  for c in channels], dtype=float)

    # ------------------------------------------------------------------ inputs
    def report_acquisition_error(self, message):
        self._acq_error = str(message)

    def clear_acquisition_error(self):
        self._acq_error = None

    def note_block(self, wall_time):
        self.last_block_time = wall_time

    def check_watchdog(self, wall_time):
        """Called from an independent thread. Returns a Trip if acquisition has stalled."""
        if self.last_block_time is None:
            return None
        gap = wall_time - self.last_block_time
        if gap > self.watchdog_timeout:
            return Trip("WATCHDOG", f"Acquisition watchdog: no slow data for {gap:.2f} s "
                                    f"(limit {self.watchdog_timeout:.2f} s) - data is stale", True)
        return None

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, eng, status, di):
        """eng: (n, n_ch) block, status: (n_ch,), di: (n_di,). Returns list[Trip]."""
        trips = []
        ch = self.channels
        # A - overpressure with debounce (N consecutive samples above threshold)
        p = self.pressure_idx
        failed = np.isin(status[p], (ChStatus.OUT_OF_RANGE, ChStatus.INVALID, ChStatus.STALE))
        with np.errstate(invalid="ignore"):
            over = np.isfinite(eng[:, p]) & (eng[:, p] > self.abort_hi[p]) & ~failed[None, :]
        if over.any():
            for j, idx in enumerate(p):
                run, best = self._over_run[idx], 0
                col = over[:, j]
                if col.all():
                    run += col.size
                    best = run
                else:
                    for v in col:
                        run = run + 1 if v else 0
                        best = max(best, run)
                self._over_run[idx] = run
                if best >= self.debounce:
                    peak = float(eng[:, idx][over[:, j]].max())
                    trips.append(Trip("OVERPRESSURE",
                                      f"{ch[idx]['id']} {ch[idx]['name']}: {peak:.1f} {ch[idx]['unit']} "
                                      f"exceeds abort threshold {self.abort_hi[idx]:.1f}", False))
        else:
            self._over_run[p] = 0
        # B - pressure sensor failure
        for idx in p:
            st = ChStatus(int(status[idx]))
            if st in _FAIL_TEXT:
                trips.append(Trip("SENSOR_FAILURE",
                                  f"{ch[idx]['id']} {ch[idx]['name']}: pressure sensor failure - "
                                  f"{_FAIL_TEXT[st]}", False))
        # C - emergency stop (fail-safe input: healthy = 1)
        if not di[self.estop_index]:
            trips.append(Trip("ESTOP", "Emergency stop activated (SIMULATED E-STOP input)", True))
        # D - acquisition error
        if self._acq_error:
            trips.append(Trip("DAQ_ERROR", f"Critical acquisition error: {self._acq_error}", True))
        self.active_conditions = trips
        self._update_alarms(status, trips)
        return trips

    def _update_alarms(self, status, trips):
        alarms = {}
        for c in self.channels:
            st = ChStatus(int(status[c["index"]]))
            if st not in (ChStatus.OK, ChStatus.DISABLED):
                alarms[f"{c['id']}:{st.name}"] = f"{c['id']} {c['name']}: {st.name}"
        for t in trips:
            if t.code in ("ESTOP", "DAQ_ERROR"):
                alarms[t.code] = t.reason
        raised = [v for k, v in alarms.items() if k not in self.active_alarms]
        cleared = [v for k, v in self.active_alarms.items() if k not in alarms]
        self.active_alarms = alarms
        self.alarm_changes = (raised, cleared)

    def conditions_clear(self):
        return not self.active_conditions and not self._acq_error
