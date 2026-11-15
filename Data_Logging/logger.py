"""Buffered data logger running in its own thread (CSV + TDMS).

Acquisition/control loops only ever call the non-blocking enqueue methods. A
bounded queue decouples them from disk I/O; if the disk cannot keep up, items
are dropped and counted instead of stalling acquisition or growing memory.

Files per run (Results/<RUN_ID>/):
  slow_data.csv     long format, decimated: timestamp, channel id, name, raw, eng, unit, status...
  control.csv       one row per control period: flows, mixture ratio, valve commands, state, DIO
  events.csv        state changes, alarms, abort events, operator actions
  slow_full.tdms    all 50 slow channels at the full 1 kS/s (raw + engineering) + control data
  hf_raw.tdms       high-frequency channels at the full 50 kS/s (raw volts, float32)
  summary.json / TEST_SUMMARY.md   test summary report
"""
from __future__ import annotations

import csv
import json
import math
import os
import queue
import threading
import time
from datetime import datetime, timedelta

import numpy as np

from Acquisition.scaling import ChStatus

try:
    from nptdms import ChannelObject, GroupObject, RootObject, TdmsWriter
    HAVE_TDMS = True
except Exception:                                    # pragma: no cover
    HAVE_TDMS = False

CONTROL_FIELDS = ["timestamp", "t_s", "test_state", "sp_total_kg_s", "meas_total_kg_s",
                  "sp_mixture_ratio", "meas_mixture_ratio", "ox_flow_kg_s", "fuel_flow_kg_s",
                  "ox_valve_cmd_V", "fuel_valve_cmd_V", "ox_valve_pct", "fuel_valve_pct",
                  "err_total_kg_s", "err_mixture_ratio", "controller_status", "alarm_active",
                  "abort_latched", "do_bits_hex", "di_bits_hex"]
STATUS_NAMES = [s.name for s in ChStatus]


def _iso(t0, t):
    return (t0 + timedelta(seconds=t)).isoformat(timespec="microseconds")


def _bits_hex(bits):
    v = 0
    for i, b in enumerate(bits):
        if b:
            v |= 1 << i
    return f"0x{v:08X}"


class _Stats:
    def __init__(self):
        self.n, self.s, self.s2, self.mn, self.mx = 0, 0.0, 0.0, math.inf, -math.inf

    def add(self, v):
        if math.isfinite(v):
            self.n += 1
            self.s += v
            self.s2 += v * v
            self.mn, self.mx = min(self.mn, v), max(self.mx, v)

    def result(self):
        if not self.n:
            return None
        mean = self.s / self.n
        return {"mean": round(mean, 5), "min": round(self.mn, 5), "max": round(self.mx, 5),
                "std": round(math.sqrt(max(self.s2 / self.n - mean * mean, 0.0)), 5), "n": self.n}


class DataLogger:
    def __init__(self, cfg, root_dir):
        self.cfg = cfg
        lc = cfg["logging"]
        self.results_dir = lc["results_dir"] if os.path.isabs(lc["results_dir"]) \
            else os.path.join(root_dir, lc["results_dir"])
        self.fs_slow = cfg["acquisition"]["slow_rate_hz"]
        self.fs_hf = cfg["acquisition"]["hf_rate_hz"]
        self.decim = max(1, int(round(self.fs_slow / lc["csv_rate_hz"])))
        self.flush_interval = lc["flush_interval_s"]
        self.write_tdms = bool(lc["write_tdms"]) and HAVE_TDMS
        self.hf_mode = lc["hf_log_mode"]
        self.q = queue.Queue(maxsize=lc["queue_max_items"])
        self.active = False
        self.healthy = True
        self.last_error = None
        self.run_id = None
        self.run_dir = None
        self._thread = None
        self._ctl_lock = threading.Lock()             # serialises start_run / stop_run
        self._seq = 0
        self.stats = self._new_stats()
        self.last_summary = None

    @staticmethod
    def _new_stats():
        return {"slow_samples": 0, "hf_samples": 0, "csv_rows": 0, "control_rows": 0, "events": 0,
                "dropped_items": 0, "queue_high_water": 0, "bytes_written": 0}

    # ------------------------------------------------------------------ run control
    def start_run(self, t0_wall=None, hf_fft_index=0, name_time=None):
        with self._ctl_lock:
            if self.active:
                return self.run_id
            if self._thread is not None and self._thread.is_alive():
                self._thread.join()                   # previous run must be fully closed first
            self.t0 = t0_wall or datetime.now()
            self._seq += 1
            base = (name_time or self.t0).strftime("RUN_%Y%m%d_%H%M%S")
            self.run_id = f"{base}_{self._seq:03d}"
            self.run_dir = os.path.join(self.results_dir, self.run_id)
            n = 1
            while os.path.exists(self.run_dir):       # never overwrite an existing run
                n += 1
                self.run_dir = os.path.join(self.results_dir, f"{self.run_id}_{n}")
            os.makedirs(self.run_dir)
            self.stats = self._new_stats()
            self.healthy, self.last_error = True, None
            self.hf_fft_index = hf_fft_index
            self._sum = {"p_max": (-math.inf, None, None), "t_max": (-math.inf, None, None),
                         "flow_all": _Stats(), "mr_all": _Stats(), "flow_hold": _Stats(),
                         "mr_hold": _Stats(), "alarms": [], "aborts": [], "states": [],
                         "t_first": None, "t_last": None}
            while not self.q.empty():
                self.q.get_nowait()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, name="DataLogger", daemon=True)
            self.active = True
            self._thread.start()
            return self.run_id

    def stop_run(self, timeout=15.0):
        """Stop accepting data, drain the queue, close the files and write the summary."""
        with self._ctl_lock:
            if self.active:
                self.active = False
                self._stop.set()
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout)
            return self.last_summary

    # ------------------------------------------------------------------ non-blocking producers
    def _put(self, item):
        if not self.active:
            return
        try:
            self.q.put_nowait(item)
            d = self.q.qsize()
            if d > self.stats["queue_high_water"]:
                self.stats["queue_high_water"] = d
        except queue.Full:
            self.stats["dropped_items"] += 1

    def log_slow(self, n0, raw, eng, status, control):
        """n0: index of first sample; control: dict for this control period."""
        self._put(("slow", n0, raw, eng, status, control))

    def log_hf(self, n0, block):
        if self.hf_mode != "off":
            self._put(("hf", n0, block))

    def log_event(self, t, etype, message):
        self._put(("event", t, etype, message))

    # ------------------------------------------------------------------ writer thread
    def _run(self):
        try:
            self._open_files()
            last_flush = time.monotonic()
            while True:
                try:
                    item = self.q.get(timeout=0.1)
                except queue.Empty:
                    item = None
                    if self._stop.is_set():
                        break
                if item is not None:
                    kind = item[0]
                    if kind == "slow":
                        self._write_slow(*item[1:])
                    elif kind == "hf":
                        self._write_hf(*item[1:])
                    else:
                        self._write_event(*item[1:])
                if time.monotonic() - last_flush >= self.flush_interval:
                    self._flush()
                    last_flush = time.monotonic()
            self._flush()
        except Exception as e:                        # disk full, permission error, ...
            self.healthy = False
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            try:
                self._close_files()
            except Exception as e:                    # pragma: no cover
                self.healthy = False
                self.last_error = self.last_error or f"{type(e).__name__}: {e}"

    def _open_files(self):
        d = self.run_dir
        self._f_slow = open(os.path.join(d, "slow_data.csv"), "w", newline="", encoding="utf-8",
                            buffering=1 << 16)
        self._f_ctl = open(os.path.join(d, "control.csv"), "w", newline="", encoding="utf-8",
                           buffering=1 << 16)
        self._f_evt = open(os.path.join(d, "events.csv"), "w", newline="", encoding="utf-8")
        self._w_slow, self._w_ctl, self._w_evt = (csv.writer(f) for f in
                                                  (self._f_slow, self._f_ctl, self._f_evt))
        self._w_slow.writerow(["timestamp", "t_s", "channel_id", "sensor_name", "raw_V",
                               "engineering_value", "unit", "channel_status", "test_state",
                               "alarm_active"])
        self._w_ctl.writerow(CONTROL_FIELDS)
        self._w_evt.writerow(["timestamp", "t_s", "event_type", "message"])
        self._sc = self.cfg["slow_channels"]
        self._hc = self.cfg["hf_channels"]
        self._hf_idx = list(range(len(self._hc))) if self.hf_mode == "all" else [self.hf_fft_index]
        self._tdms_slow = self._tdms_hf = None
        self._slow_buf, self._ctl_buf, self._hf_buf = [], [], []
        self._slow_first = self._hf_first = True
        if self.write_tdms:
            self._tdms_slow = TdmsWriter(os.path.join(d, "slow_full.tdms"))
            self._tdms_slow.open()
            if self.hf_mode != "off":
                self._tdms_hf = TdmsWriter(os.path.join(d, "hf_raw.tdms"))
                self._tdms_hf.open()

    def _write_slow(self, n0, raw, eng, status, ctl):
        fs = self.fs_slow
        st_name = ctl["test_state"]
        alarm = int(ctl["alarm_active"])
        first = (-n0) % self.decim                    # rows on a fixed global sample grid
        rows = []
        for i in range(first, raw.shape[0], self.decim):
            t = (n0 + i) / fs
            ts = _iso(self.t0, t)
            for c in self._sc:
                k = c["index"]
                rows.append((ts, f"{t:.4f}", c["id"], c["name"], f"{raw[i, k]:.6f}",
                             f"{eng[i, k]:.6f}", c["unit"], STATUS_NAMES[status[k]], st_name, alarm))
        if rows:
            self._w_slow.writerows(rows)
            self.stats["csv_rows"] += len(rows)
        t_end = (n0 + raw.shape[0]) / fs
        self._w_ctl.writerow([_iso(self.t0, t_end), f"{t_end:.4f}"] +
                             [ctl[k] if not isinstance(ctl[k], float) else f"{ctl[k]:.6f}"
                              for k in CONTROL_FIELDS[2:]])
        self.stats["control_rows"] += 1
        self.stats["slow_samples"] += raw.size
        self._summarise(t_end, eng, ctl)
        if self._tdms_slow is not None:
            self._slow_buf.append((raw, eng))
            self._ctl_buf.append(ctl)

    def _summarise(self, t, eng, ctl):
        s = self._sum
        if s["t_first"] is None:
            s["t_first"] = t
        s["t_last"] = t
        mx = np.where(np.isfinite(eng), eng, -np.inf).max(axis=0)   # -inf where a channel is all invalid
        for key, stype in (("p_max", "pressure"), ("t_max", "temperature")):
            for c in self._sc:
                if c["sensor_type"] == stype:
                    v = mx[c["index"]]
                    if np.isfinite(v) and v > s[key][0]:
                        s[key] = (float(v), c["id"], t)
        if ctl["sp_total_kg_s"] > 0:
            s["flow_all"].add(ctl["meas_total_kg_s"])
            s["mr_all"].add(ctl["meas_mixture_ratio"])
            if ctl["test_state"] == "HOLD":
                s["flow_hold"].add(ctl["meas_total_kg_s"])
                s["mr_hold"].add(ctl["meas_mixture_ratio"])

    def _write_hf(self, n0, block):
        self.stats["hf_samples"] += block.size
        if self._tdms_hf is not None:
            self._hf_buf.append(block[:, self._hf_idx] if self.hf_mode != "all" else block)
            if len(self._hf_buf) >= 5:
                self._flush_hf()

    def _write_event(self, t, etype, message):
        self._w_evt.writerow([_iso(self.t0, t), f"{t:.4f}", etype, message])
        self.stats["events"] += 1
        s = self._sum
        if etype == "ALARM":
            s["alarms"].append({"t_s": round(t, 3), "message": message})
        elif etype == "ABORT":
            s["aborts"].append({"t_s": round(t, 3), "timestamp": _iso(self.t0, t), "reason": message})
            self._f_evt.flush()
            os.fsync(self._f_evt.fileno())            # abort record goes to disk immediately
        elif etype == "STATE":
            s["states"].append({"t_s": round(t, 3), "transition": message})

    def _flush_hf(self):
        if not self._hf_buf or self._tdms_hf is None:
            return
        data = np.concatenate(self._hf_buf, axis=0)
        self._hf_buf = []
        objs = []
        if self._hf_first:
            objs.append(RootObject(properties={"run_id": self.run_id, "note": "SIMULATED data"}))
            objs.append(GroupObject("HF_Raw_V", properties={"sample_rate_hz": self.fs_hf}))
        for j, k in enumerate(self._hf_idx):
            c = self._hc[k]
            props = {}
            if self._hf_first:
                props = {"wf_increment": 1.0 / self.fs_hf, "wf_start_time": np.datetime64(self.t0),
                         "unit_string": "V", "sensor_name": c["name"], "eng_unit": c["unit"],
                         "gain": float(c["gain"]), "offset": float(c["offset"])}
            objs.append(ChannelObject("HF_Raw_V", c["id"], np.ascontiguousarray(data[:, j]), props))
        self._tdms_hf.write_segment(objs)
        self._hf_first = False

    def _flush(self):
        self._f_slow.flush()
        self._f_ctl.flush()
        self._f_evt.flush()
        self._flush_hf()
        if self._tdms_slow is not None and self._slow_buf:
            raw = np.concatenate([b[0] for b in self._slow_buf], axis=0)
            eng = np.concatenate([b[1] for b in self._slow_buf], axis=0)
            ctl, self._slow_buf, self._ctl_buf = self._ctl_buf, [], []
            objs = []
            first = self._slow_first
            if first:
                objs.append(RootObject(properties={"run_id": self.run_id, "note": "SIMULATED data"}))
            for group, data, unit_key in (("Slow_Raw_V", raw, None), ("Slow_Eng", eng, "unit")):
                for c in self._sc:
                    props = {}
                    if first:
                        props = {"wf_increment": 1.0 / self.fs_slow,
                                 "wf_start_time": np.datetime64(self.t0),
                                 "unit_string": c[unit_key] if unit_key else "V",
                                 "sensor_name": c["name"], "sensor_type": c["sensor_type"],
                                 "gain": float(c["gain"]), "offset": float(c["offset"])}
                    objs.append(ChannelObject(group, c["id"], np.ascontiguousarray(data[:, c["index"]]),
                                              props))
            for key in ("t_s", "sp_total_kg_s", "meas_total_kg_s", "meas_mixture_ratio",
                        "ox_flow_kg_s", "fuel_flow_kg_s", "ox_valve_cmd_V", "fuel_valve_cmd_V"):
                objs.append(ChannelObject("Control", key,
                                          np.array([r[key] for r in ctl], dtype=np.float64)))
            self._tdms_slow.write_segment(objs)
            self._slow_first = False

    def _close_files(self):
        for f in (self._f_slow, self._f_ctl, self._f_evt):
            f.close()
        for w in (self._tdms_slow, self._tdms_hf):
            if w is not None:
                w.close()
        self.stats["bytes_written"] = sum(os.path.getsize(os.path.join(self.run_dir, f))
                                          for f in os.listdir(self.run_dir))
        self.last_summary = self._write_summary()

    # ------------------------------------------------------------------ summary report
    def _write_summary(self):
        s = self._sum
        t0, t1 = s["t_first"] or 0.0, s["t_last"] or 0.0

        def peak(v):
            return None if v[1] is None else {"value": round(v[0], 3), "channel": v[1],
                                              "t_s": round(v[2], 3)}
        summ = {
            "run_id": self.run_id, "note": "SIMULATED test - no real hardware involved",
            "log_start": _iso(self.t0, t0), "log_end": _iso(self.t0, t1),
            "duration_s": round(t1 - t0, 3),
            "max_pressure_bar": peak(s["p_max"]), "max_temperature_degC": peak(s["t_max"]),
            "total_flow_kg_s_controller_active": s["flow_all"].result(),
            "mixture_ratio_controller_active": s["mr_all"].result(),
            "total_flow_kg_s_hold": s["flow_hold"].result(),
            "mixture_ratio_hold": s["mr_hold"].result(),
            "aborted": bool(s["aborts"]), "abort_events": s["aborts"],
            "alarms": s["alarms"], "state_transitions": s["states"],
            "logger": dict(self.stats, healthy=self.healthy, last_error=self.last_error,
                           csv_rate_hz=self.fs_slow / self.decim, tdms=self.write_tdms,
                           hf_log_mode=self.hf_mode),
            "files": sorted(os.listdir(self.run_dir)) + ["summary.json", "TEST_SUMMARY.md"],
        }
        with open(os.path.join(self.run_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summ, f, indent=1)

        def stat_line(name, r, unit):
            if not r:
                return f"| {name} | n/a | | | |"
            return f"| {name} | {r['mean']:.4f} {unit} | {r['min']:.4f} | {r['max']:.4f} | {r['std']:.4f} |"
        pm, tm = summ["max_pressure_bar"], summ["max_temperature_degC"]
        md = [f"# Test summary - {self.run_id}", "",
              "**SIMULATED TEST - no real engine hardware was involved.**", "",
              f"- Log start: {summ['log_start']}", f"- Log end: {summ['log_end']}",
              f"- Total duration: {summ['duration_s']:.2f} s",
              f"- Maximum recorded pressure: " +
              (f"{pm['value']:.2f} bar on {pm['channel']} at t = {pm['t_s']:.2f} s" if pm else "n/a"),
              f"- Maximum recorded temperature: " +
              (f"{tm['value']:.1f} degC on {tm['channel']} at t = {tm['t_s']:.2f} s" if tm else "n/a"),
              f"- Abort status: {'ABORTED' if summ['aborted'] else 'no abort'}", "",
              "## Flow and mixture-ratio statistics", "",
              "| Quantity | Mean | Min | Max | Std |", "|---|---|---|---|---|",
              stat_line("Total flow (controller active)", summ["total_flow_kg_s_controller_active"], "kg/s"),
              stat_line("Total flow (HOLD)", summ["total_flow_kg_s_hold"], "kg/s"),
              stat_line("Mixture ratio (controller active)", summ["mixture_ratio_controller_active"], ""),
              stat_line("Mixture ratio (HOLD)", summ["mixture_ratio_hold"], ""), "",
              "## Abort events", ""]
        md += [f"- t = {a['t_s']:.3f} s ({a['timestamp']}): {a['reason']}" for a in s["aborts"]] or ["- none"]
        md += ["", "## Recorded alarms", ""]
        md += [f"- t = {a['t_s']:.3f} s: {a['message']}" for a in s["alarms"]] or ["- none"]
        md += ["", "## State transitions", ""]
        md += [f"- t = {a['t_s']:.3f} s: {a['transition']}" for a in s["states"]] or ["- none"]
        st = self.stats
        md += ["", "## Logger statistics", "",
               f"- Slow samples logged: {st['slow_samples']:,}",
               f"- High-frequency samples logged: {st['hf_samples']:,}",
               f"- CSV rows: {st['csv_rows']:,}  |  control rows: {st['control_rows']:,}  |  events: {st['events']}",
               f"- Dropped queue items: {st['dropped_items']}  |  queue high-water mark: {st['queue_high_water']}",
               f"- Bytes written: {st['bytes_written']:,}", ""]
        with open(os.path.join(self.run_dir, "TEST_SUMMARY.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        return summ
