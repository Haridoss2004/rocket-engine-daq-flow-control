"""DAQ engine: wires backend, scaling, safety, control, sequencing, logging and FFT.

Loop structure (each loop is its own thread when start() is used):
  slow loop      producer: reads 50 ch @ 1 kS/s in blocks, then scaling -> safety ->
                 sequencer -> flow control -> virtual outputs -> logger queue
  hf loop        producer: reads 25 ch @ 50 kS/s in blocks -> ring buffer + logger queue
  analysis loop  consumer: FFT + signal statistics from the ring buffer
  watchdog loop  independent stale-data monitor
  logger thread  consumer: drains the bounded queue to disk (Data_Logging/logger.py)
  GUI            polls snapshot() on its own timer; it never blocks acquisition

The same step functions can be called synchronously (no threads, no sleeping),
which is how the automated tests run faster than real time and deterministically.
"""
from __future__ import annotations

import collections
import math
import threading
import time
from datetime import datetime, timedelta

import numpy as np

from Acquisition.scaling import ChStatus, RingBuffer, SlowChannelProcessor
from Acquisition.virtual_outputs import VirtualOutputs
from Configuration import config as config_mod
from DAQ_Hardware_Abstraction.backend_base import DaqError
from Data_Logging.logger import DataLogger, _bits_hex
from Flow_Control.flow_controller import FlowController
from Safety.safety_monitor import SafetyMonitor, Trip
from Signal_Processing.fft import FftAnalyzer, signal_stats
from Simulation.sim_backend import SimulationBackend
from State_Machine.sequencer import ACTIVE_STATES, ARMED_STATES, Sequencer, State

CTL_TREND = ["t", "sp_total", "meas_total", "sp_mr", "meas_mr", "ox_flow", "fuel_flow",
             "ox_cmd", "fuel_cmd", "err_total"]


class DaqEngine:
    def __init__(self, cfg=None, root_dir=None, backend=None):
        self.cfg = cfg or config_mod.load_config()
        self.root_dir = root_dir or config_mod.ROOT
        acq = self.cfg["acquisition"]
        self.fs_slow, self.fs_hf = acq["slow_rate_hz"], acq["hf_rate_hz"]
        self.n_slow_block, self.n_hf_block = acq["slow_block_samples"], acq["hf_block_samples"]
        self.dt_block = self.n_slow_block / self.fs_slow
        self.lock = threading.RLock()
        self.hf_lock = threading.Lock()

        self.backend = backend or SimulationBackend(self.cfg)
        self.backend.open()
        # a hardware module may coerce the requested rate (e.g. NI 9234: 51.2 kS/s)
        self.fs_hf = getattr(self.backend, "actual_hf_rate", None) or self.fs_hf
        self.slow_ch, self.hf_ch = self.cfg["slow_channels"], self.cfg["hf_channels"]
        self.slow_id = {c["id"]: c["index"] for c in self.slow_ch}
        self.hf_id = {c["id"]: c["index"] for c in self.hf_ch}
        s = self.cfg["safety"]
        self.proc = SlowChannelProcessor(self.slow_ch, self.fs_slow, s["stale_time_s"],
                                         s["stale_epsilon_v"])
        self.outputs = VirtualOutputs(self.cfg["digital_outputs"], self.cfg["analog_outputs"])
        self.controller = FlowController(self.cfg["flow_control"])
        self.safety = SafetyMonitor(self.cfg)
        self.logger = DataLogger(self.cfg, self.root_dir)
        self.events = collections.deque(maxlen=500)
        self.sequencer = Sequencer(self.cfg["sequence"], self.outputs, self.controller,
                                   self._prechecks, self._event)
        self.fft = FftAnalyzer(self.fs_hf, self.cfg["fft"]["size"], self.cfg["fft"]["window"],
                               self.cfg["fft"]["averages"])
        self.fft_channel = self.hf_id[self.cfg["fft"]["channel"]]
        self.fft_result = None
        self.hf_stats = None
        self.hf_ring = RingBuffer(int(acq["hf_buffer_s"] * self.fs_hf), len(self.hf_ch), np.float32)
        n_tr = int(acq["trend_history_s"] / self.dt_block)
        self.trend = RingBuffer(n_tr, len(self.slow_ch) + 1, np.float64)      # col 0 = time
        self.ctl_trend = RingBuffer(n_tr, len(CTL_TREND), np.float64)

        fc = self.cfg["flow_control"]
        self.i_ox, self.i_fuel = self.slow_id[fc["ox_flow_channel"]], self.slow_id[fc["fuel_flow_channel"]]
        self.i_pc = self.slow_id["PT-07"] if "PT-07" in self.slow_id else 0
        self.n_slow = 0                    # slow samples processed  (t = n_slow / fs)
        self.n_hf = 0
        self.t0_wall = datetime.now()
        self.raw_last = np.zeros(len(self.slow_ch))
        self.eng_mean = np.zeros(len(self.slow_ch))
        self.status = np.zeros(len(self.slow_ch), dtype=np.int8)
        self.di = np.zeros(len(self.cfg["digital_inputs"]), dtype=bool)
        self.ox_flow = self.fuel_flow = 0.0
        self.last_abort = None
        self._acq_err_active = False
        self._hf_err_active = False
        self._log_auto = False
        self._log_stop_at = None
        self._threads = []
        self._running = threading.Event()
        self.loop_stats = {"slow_blocks": 0, "hf_blocks": 0, "slow_overruns": 0, "hf_overruns": 0,
                           "slow_proc_ms_max": 0.0, "hf_proc_ms_max": 0.0, "slow_rate_meas": 0.0,
                           "hf_rate_meas": 0.0}
        self.backend.apply_virtual_outputs(self.outputs.do, self.outputs.ao)

    # ================================================================== helpers
    @property
    def t(self):
        return self.n_slow / self.fs_slow

    @property
    def state(self):
        return self.sequencer.state

    def _event(self, etype, message):
        self.events.append((datetime.now().strftime("%H:%M:%S.%f")[:-3], round(self.t, 3), etype, message))
        self.logger.log_event(self.t, etype, message)

    def _abort(self, trip):
        ts = datetime.now().isoformat(timespec="milliseconds")
        reason = f"[{trip.code}] {trip.reason}"
        if self.sequencer.force_abort(reason, ts):
            self.last_abort = {"reason": reason, "timestamp": ts, "t_s": round(self.t, 3),
                               "code": trip.code}
            self.backend.apply_virtual_outputs(self.outputs.do, self.outputs.ao)
            self._event("ABORT", reason)
            self._event("INFO", "All virtual outputs forced to SAFE state; control commands inhibited; "
                                "operator reset required")
            if self._log_auto:
                self._log_stop_at = self.t + 2.0

    def _handle_trips(self, trips):
        for trip in trips:
            if self.state != State.ABORT and (trip.always or self.state in ARMED_STATES):
                self._abort(trip)
                break

    # ================================================================== pre-checks
    def _prechecks(self):
        ch, st = self.slow_ch, self.status
        res = []
        res.append(("Simulation backend available", self.backend.is_ready(),
                    f"backend '{self.backend.name}' " + ("ready" if self.backend.is_ready() else "NOT ready")))
        fresh = self.loop_stats["slow_blocks"] > 0 and self.loop_stats["hf_blocks"] > 0
        res.append(("Sensor availability (slow + high-frequency data arriving)", fresh,
                    f"{self.loop_stats['slow_blocks']} slow / {self.loop_stats['hf_blocks']} HF blocks"))
        bad = [c["id"] for c in ch if st[c["index"]] in
               (ChStatus.INVALID, ChStatus.OUT_OF_RANGE, ChStatus.STALE)]
        res.append(("Valid measurements on all channels", not bad,
                    "all valid" if not bad else "failed: " + ", ".join(bad)))
        viol = []
        for cid, (lo, hi) in self.cfg["sequence"]["precheck_pressure_windows_bar"].items():
            v = self.eng_mean[self.slow_id[cid]]
            if not (math.isfinite(v) and lo <= v <= hi):
                viol.append(f"{cid}={v:.1f} (need {lo:g}..{hi:g})")
        for c in ch:
            if c["sensor_type"] == "pressure" and st[c["index"]] in (ChStatus.WARN_HIGH, ChStatus.ABORT_HIGH):
                viol.append(f"{c['id']} above limit")
        res.append(("Pressures within demonstration limits", not viol,
                    "within limits" if not viol else "; ".join(viol)))
        wrong = [d["id"] for d in self.cfg["digital_inputs"]
                 if d["expected_precheck"] is not None and d["kind"] != "estop"
                 and bool(self.di[d["index"]]) != bool(d["expected_precheck"])]
        res.append(("Digital input status as expected", not wrong,
                    "all as expected" if not wrong else "unexpected: " + ", ".join(wrong)))
        estop_ok = bool(self.di[self.safety.estop_index])
        res.append(("Emergency stop healthy", estop_ok, "released" if estop_ok else "E-STOP ACTIVE"))
        log_ok = self.logger.active and self.logger.healthy
        res.append(("Data logger available", log_ok,
                    f"run {self.logger.run_id}" if log_ok else
                    (self.logger.last_error or "logger not running")))
        res.append(("Virtual analog outputs at 0 V", bool(np.allclose(self.outputs.ao, 0.0, atol=1e-6)),
                    f"AO = {self.outputs.ao[0]:.2f} / {self.outputs.ao[1]:.2f} V"))
        return res

    # ================================================================== slow path
    def process_slow_block(self):
        n = self.n_slow_block
        n0 = self.n_slow
        try:
            raw = self.backend.read_slow(n)
        except DaqError as e:
            with self.lock:
                self.n_slow += n
                self.safety.report_acquisition_error(str(e))
                if not self._acq_err_active:
                    self._acq_err_active = True
                    self._event("ERROR", f"Acquisition error (slow task): {e}")
                self._handle_trips([Trip("DAQ_ERROR", f"Critical acquisition error: {e}", True)])
                self.sequencer.step(self.t, float("nan"))
                self.backend.apply_virtual_outputs(self.outputs.do, self.outputs.ao)
            return False
        with self.lock:
            self.n_slow += n
            t = self.t
            if self._acq_err_active:
                self._acq_err_active = False
                self.safety.clear_acquisition_error()
                self._event("INFO", "Acquisition recovered")
            self.safety.note_block(time.monotonic())
            eng, status = self.proc.process(raw)
            di = self.backend.read_di()
            self.raw_last, self.status, self.di = raw[-1], status, di
            with np.errstate(invalid="ignore"):
                self.eng_mean = eng.mean(axis=0)

            # ---- independent safety evaluation, then abort handling
            trips = self.safety.evaluate(eng, status, di)
            raised, cleared = self.safety.alarm_changes
            for msg in raised:
                self._event("ALARM", msg)
            for msg in cleared:
                self._event("INFO", f"Alarm cleared: {msg}")
            self._handle_trips(trips)

            # ---- sequencer, then flow control on the SIMULATED flow feedback
            reason = self.sequencer.step(t, float(self.eng_mean[self.i_pc]))
            if reason:
                self._abort(Trip("SEQUENCE", reason, True))
            ok = lambda i: status[i] in (ChStatus.OK, ChStatus.WARN_LOW, ChStatus.WARN_HIGH)
            self.ox_flow = float(self.eng_mean[self.i_ox]) if ok(self.i_ox) else float("nan")
            self.fuel_flow = float(self.eng_mean[self.i_fuel]) if ok(self.i_fuel) else float("nan")
            ao = self.controller.step(self.ox_flow, self.fuel_flow, self.dt_block)
            self.outputs.set_ao(ao)
            self.backend.apply_virtual_outputs(self.outputs.do, self.outputs.ao)

            # ---- history for the GUI, record for the logger
            c = self.controller
            self.trend.write(np.concatenate(([t], self.eng_mean))[None, :])
            self.ctl_trend.write(np.array([[t, c.sp_total, c.meas_total, c.target_mr, c.meas_mr,
                                            self.ox_flow, self.fuel_flow, self.outputs.ao[0],
                                            self.outputs.ao[1], c.err_total]]))
            if self.logger.active:
                pct = self.outputs.ao_percent
                self.logger.log_slow(n0, raw, eng, status, {
                    "t_s": t, "test_state": self.state.value, "sp_total_kg_s": float(c.sp_total),
                    "meas_total_kg_s": float(c.meas_total), "sp_mixture_ratio": float(c.target_mr),
                    "meas_mixture_ratio": float(c.meas_mr), "ox_flow_kg_s": self.ox_flow,
                    "fuel_flow_kg_s": self.fuel_flow, "ox_valve_cmd_V": float(self.outputs.ao[0]),
                    "fuel_valve_cmd_V": float(self.outputs.ao[1]), "ox_valve_pct": float(pct[0]),
                    "fuel_valve_pct": float(pct[1]), "err_total_kg_s": float(c.err_total),
                    "err_mixture_ratio": float(c.err_mr), "controller_status": c.status,
                    "alarm_active": int(bool(self.safety.active_alarms)),
                    "abort_latched": int(self.state == State.ABORT),
                    "do_bits_hex": _bits_hex(self.outputs.do), "di_bits_hex": _bits_hex(di)})
            if self._log_auto and self._log_stop_at is None and self.state == State.COMPLETE:
                self._log_stop_at = t + 2.0
            if self._log_stop_at is not None and t >= self._log_stop_at:
                self._log_stop_at = None
                self._log_auto = False
                threading.Thread(target=self.logger.stop_run, daemon=True).start()
            self.loop_stats["slow_blocks"] += 1
        return True

    # ================================================================== high-frequency path
    def process_hf_block(self):
        n0 = self.n_hf
        try:
            block = self.backend.read_hf(self.n_hf_block)
        except DaqError as e:
            self.n_hf += self.n_hf_block
            with self.lock:
                if not self._hf_err_active:
                    self._hf_err_active = True
                    self.safety.report_acquisition_error(str(e))
                    self._event("ERROR", f"Acquisition error (high-frequency task): {e}")
            return False
        self._hf_err_active = False
        self.n_hf += self.n_hf_block
        with self.hf_lock:
            self.hf_ring.write(block)
        self.logger.log_hf(n0, block)
        self.loop_stats["hf_blocks"] += 1
        return True

    def analysis_step(self):
        """FFT + statistics. Works on a copy of the ring buffer, never blocks acquisition."""
        with self.hf_lock:
            ch = self.fft_channel
            x = self.hf_ring.latest(self.fft.size, ch).astype(np.float64)
            recent = self.hf_ring.latest(self.n_hf_block)
        c = self.hf_ch[ch]
        res = self.fft.analyze(x * c["gain"] + c["offset"])
        if res is not None:
            res = dict(res, channel=c["id"], unit=c["unit"],
                       stats=signal_stats(x[-self.n_hf_block:] * c["gain"] + c["offset"]))
        self.fft_result = res
        if recent.shape[0]:
            g = np.array([h["gain"] for h in self.hf_ch])
            o = np.array([h["offset"] for h in self.hf_ch])
            e = recent.astype(np.float64) * g + o
            with np.errstate(invalid="ignore"):
                ac = e - e.mean(axis=0)
                self.hf_stats = {"rms": np.sqrt((ac ** 2).mean(axis=0)),
                                 "pk_pk": e.max(axis=0) - e.min(axis=0), "mean": e.mean(axis=0),
                                 "valid": np.isfinite(e).all(axis=0)}
        return res

    def hf_waveform(self, channel_index, n):
        with self.hf_lock:
            x = self.hf_ring.latest(n, channel_index).astype(np.float64)
        c = self.hf_ch[channel_index]
        return x * c["gain"] + c["offset"]

    # ================================================================== synchronous stepping
    def run_for(self, seconds, analysis=False):
        """Advance simulated time without threads (tests / headless demo)."""
        blocks = int(round(seconds / self.dt_block))
        hf_per = self.n_hf_block / self.fs_hf
        for _ in range(blocks):
            self.process_slow_block()
            while (self.n_hf + self.n_hf_block) / self.fs_hf <= self.t + 1e-9:
                self.process_hf_block()
                if analysis:
                    self.analysis_step()
        return self.t

    def run_until(self, predicate, timeout_s):
        end = self.t + timeout_s
        while self.t < end:
            self.run_for(self.dt_block)
            if predicate():
                return True
        return False

    # ================================================================== threads
    def _paced_loop(self, fn, period, key):
        nxt = time.perf_counter()
        last_rate_t, last_count = nxt, 0
        while self._running.is_set():
            t0 = time.perf_counter()
            try:
                fn()
            except Exception as e:                    # never let a loop die silently
                with self.lock:
                    self.safety.report_acquisition_error(f"{key} loop exception: {e!r}")
                    self._event("ERROR", f"{key} loop exception: {e!r}")
            now = time.perf_counter()
            ms = (now - t0) * 1e3
            if ms > self.loop_stats[f"{key}_proc_ms_max"]:
                self.loop_stats[f"{key}_proc_ms_max"] = ms
            if now - last_rate_t >= 1.0:
                cnt = self.loop_stats[f"{key}_blocks"]
                per = self.n_slow_block if key == "slow" else self.n_hf_block
                self.loop_stats[f"{key}_rate_meas"] = (cnt - last_count) * per / (now - last_rate_t)
                last_rate_t, last_count = now, cnt
            nxt += period
            delay = nxt - now
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:                        # badly behind: resynchronise, count it
                self.loop_stats[f"{key}_overruns"] += 1
                nxt = now

    def _analysis_loop(self):
        while self._running.is_set():
            try:
                self.analysis_step()
            except Exception as e:
                self._event("ERROR", f"analysis loop exception: {e!r}")
            time.sleep(0.1)

    def _watchdog_loop(self):
        while self._running.is_set():
            trip = self.safety.check_watchdog(time.monotonic())
            if trip is not None:
                with self.lock:
                    if self.state != State.ABORT:
                        self._abort(trip)
            time.sleep(0.05)

    def start(self):
        if self._running.is_set():
            return
        if not self.backend.is_ready() and not getattr(self.backend, "acq_error", None):
            self.backend.open()
        self.safety.last_block_time = None            # restart the watchdog cleanly
        try:                                          # 1 ms timer resolution for this process
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
            self._timer_res = True
        except Exception:
            self._timer_res = False
        self._running.set()
        specs = [("SlowAcq", lambda: self._paced_loop(self.process_slow_block, self.dt_block, "slow")),
                 ("HfAcq", lambda: self._paced_loop(self.process_hf_block,
                                                    self.n_hf_block / self.fs_hf, "hf")),
                 ("Analysis", self._analysis_loop), ("Watchdog", self._watchdog_loop)]
        self._threads = [threading.Thread(target=f, name=n, daemon=True) for n, f in specs]
        for th in self._threads:
            th.start()
        self._event("INFO", "Engine started (SIMULATION backend)" if self.backend.is_simulation
                    else f"Engine started ({self.backend.name} backend, inputs only)")

    def stop(self, close_backend=True):
        """Graceful shutdown: stop loops, force safe outputs, close log and backend."""
        was_running = self._running.is_set()
        self._running.clear()
        for th in self._threads:
            th.join(timeout=3.0)
        self._threads = []
        with self.lock:
            self.controller.disable()
            self.outputs.apply_safe_state(lock=False)
            self.backend.apply_virtual_outputs(self.outputs.do, self.outputs.ao)
            if was_running:
                self._event("INFO", "Engine stopped; virtual outputs in safe state")
        self.logger.stop_run()
        self._log_auto, self._log_stop_at = False, None
        if close_backend:
            self.backend.close()
        if getattr(self, "_timer_res", False):
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
            self._timer_res = False

    # ================================================================== operator commands
    def start_logging(self, auto=False):
        with self.lock:
            if not self.logger.active:
                now = datetime.now()
                # t0 is chosen so that t0 + engine time = wall time for every logged sample
                rid = self.logger.start_run(now - timedelta(seconds=self.t), self.fft_channel,
                                            name_time=now)
                self._log_auto = auto
                self._log_stop_at = None
                self._event("INFO", f"Logging started: {rid}")
            return self.logger.run_id

    def stop_logging(self):
        with self.lock:
            self._log_auto = False
            self._log_stop_at = None
            if self.logger.active:
                self._event("INFO", f"Logging stopped: {self.logger.run_id}")
        return self.logger.stop_run()

    def cmd_start(self):
        with self.lock:
            if self.state != State.IDLE:
                return False, f"Start rejected: state is {self.state.value}, must be IDLE"
            if self.cfg["logging"]["auto_start_on_precheck"]:
                if self._log_auto:                    # log of the previous test still in post-roll
                    self._log_auto, self._log_stop_at = False, None
                    self.logger.stop_run()
                if not self.logger.active:
                    self.start_logging(auto=True)
            self._event("OPERATOR", "Start test sequence (pre-check)")
            return self.sequencer.request_precheck()

    def cmd_fire(self):
        with self.lock:
            self._event("OPERATOR", "Start sequence command (SIMULATED)")
            return self.sequencer.request_fire()

    def cmd_stop(self):
        with self.lock:
            self._event("OPERATOR", "Stop command")
            return self.sequencer.request_stop()

    def cmd_abort(self, reason="Manual abort by operator"):
        with self.lock:
            if self.state == State.ABORT:
                return False, "Already in ABORT"
            self._abort(Trip("OPERATOR", reason, True))
            return True, "Abort executed"

    def cmd_reset(self):
        with self.lock:
            self._event("OPERATOR", "Reset command")
            clear = self.safety.conditions_clear()
            ok, msg = self.sequencer.request_reset(clear)
            if not ok and self.state == State.ABORT:
                msg += ": " + "; ".join(t.reason for t in self.safety.active_conditions)
            if ok and hasattr(self.backend, "plant"):
                self.backend.plant.refill()
            self._event("INFO" if ok else "ALARM", msg)
            return ok, msg

    def set_estop(self, active):
        with self.lock:
            self.backend.estop_active = bool(active)
            self._event("OPERATOR", f"Simulated E-STOP {'PRESSED' if active else 'released'}")

    def inject_fault(self, kind, index=0, ftype=None, value=0.0):
        """kind: slow | hf | di | estop | overpressure | spike | acq_error | acq_timeout |
        hf_transient | hf_instability | clear"""
        b = self.backend
        if not b.is_simulation or not hasattr(b, "inject_slow_fault"):
            return False, "Fault injection is only available with the simulation backend"
        with self.lock:
            if kind == "slow":
                b.inject_slow_fault(index, ftype, value)
                desc = f"{self.slow_ch[index]['id']} {ftype}"
            elif kind == "hf":
                b.inject_hf_fault(index, ftype)
                desc = f"{self.hf_ch[index]['id']} {ftype}"
            elif kind == "di":
                b.inject_di_fault(index, ftype)
                desc = f"{self.cfg['digital_inputs'][index]['id']} {ftype}"
            elif kind == "estop":
                b.estop_active = True
                desc = "emergency stop"
            elif kind == "overpressure":
                b.overpressure = True
                desc = f"chamber overpressure (+{self.cfg['simulation']['overpressure_fault_bar']} bar)"
            elif kind == "spike":
                b.trigger_pressure_spike()
                desc = "chamber pressure spike"
            elif kind in ("acq_error", "acq_timeout"):
                b.acq_error = "error" if kind == "acq_error" else "timeout"
                desc = f"DAQ {b.acq_error}"
            elif kind == "hf_transient":
                b.trigger_hf_transient()
                desc = "high-frequency transient burst"
            elif kind == "hf_instability":
                b.instability = not b.instability
                desc = f"high-frequency instability tone {'ON' if b.instability else 'OFF'}"
            elif kind == "clear":
                b.clear_faults()
                desc = "ALL FAULTS CLEARED"
            else:
                return False, f"unknown fault kind {kind}"
            self._event("FAULT", f"Fault injection: {desc}")
            return True, desc

    def set_targets(self, total=None, mr=None):
        with self.lock:
            self.controller.set_targets(total, mr)
            fc = self.cfg["flow_control"]
            fc["target_total_flow_kg_s"] = self.controller.target_total
            fc["target_mixture_ratio"] = self.controller.target_mr
            self._event("OPERATOR", f"Targets: total flow {self.controller.target_total:.3f} kg/s, "
                                    f"mixture ratio {self.controller.target_mr:.3f}")

    def set_gains(self, which, kp, ki, kd):
        with self.lock:
            self.controller.set_gains(which, kp, ki, kd)
            self.cfg["flow_control"]["pid_ox" if which == "ox" else "pid_fuel"] = \
                {"kp": float(kp), "ki": float(ki), "kd": float(kd)}
            self._event("OPERATOR", f"PID {which}: Kp={kp} Ki={ki} Kd={kd}")

    def set_fft(self, channel_id=None, size=None, averages=None):
        with self.hf_lock:
            if channel_id is not None:
                self.fft_channel = self.hf_id[channel_id]
                self.cfg["fft"]["channel"] = channel_id
            self.fft.configure(size=size, averages=averages)
            if size is not None:
                self.cfg["fft"]["size"] = int(size)
            if averages is not None:
                self.cfg["fft"]["averages"] = int(averages)

    def update_slow_channel(self, index, **fields):
        """Edit calibration / limits / name of one slow channel at run time."""
        with self.lock:
            c = self.slow_ch[index]
            trial = dict(c, **fields)
            if not math.isfinite(float(trial["gain"])) or float(trial["gain"]) == 0.0:
                raise ValueError("gain must be finite and non-zero")
            c.update(fields)
            self.proc.load_channels(self.slow_ch)
            self.safety.reload_channels(self.slow_ch)
            self._event("OPERATOR", f"Channel {c['id']} configuration changed: {fields}")

    def apply_parameters(self):
        """Re-read numeric parameters after the configuration dict was edited."""
        with self.lock:
            sf, fc = self.cfg["safety"], self.cfg["flow_control"]
            self.safety.debounce = max(1, int(sf["pressure_abort_debounce_samples"]))
            self.safety.watchdog_timeout = float(sf["watchdog_timeout_s"])
            self.proc.stale_samples = max(2, int(sf["stale_time_s"] * self.fs_slow))
            self.controller.set_targets(fc["target_total_flow_kg_s"], fc["target_mixture_ratio"])
            for pid in (self.controller.pid_ox, self.controller.pid_fuel):
                pid.rate_limit = fc["rate_limit_v_per_s"]
                pid.out_min, pid.out_max = fc["output_min_v"], fc["output_max_v"]
                pid.d_filter_hz = fc["derivative_filter_hz"]
            lc = self.cfg["logging"]
            self.logger.decim = max(1, int(round(self.fs_slow / lc["csv_rate_hz"])))
            self.logger.flush_interval = lc["flush_interval_s"]
        self.set_fft(size=self.cfg["fft"]["size"], averages=self.cfg["fft"]["averages"])

    def save_config(self, path=None):
        with self.lock:
            p = config_mod.save_config(self.cfg, path)
            self._event("INFO", f"Configuration saved: {p}")
            return p

    # ================================================================== GUI snapshot
    def snapshot(self):
        with self.lock:
            c, o, s = self.controller, self.outputs, self.sequencer
            return {
                "t": self.t, "state": s.state, "state_elapsed": s.elapsed, "progress": s.progress,
                "raw": self.raw_last.copy(), "eng": self.eng_mean.copy(), "status": self.status.copy(),
                "di": self.di.copy(), "do": o.do.copy(), "ao": o.ao.copy(),
                "ao_pct": o.ao_percent.copy(), "ao_sat": o.ao_saturated.copy(),
                "outputs_locked": o.locked, "outputs_safe": o.is_safe(),
                "ox_flow": self.ox_flow, "fuel_flow": self.fuel_flow,
                "sp_total": c.sp_total, "meas_total": c.meas_total, "target_total": c.target_total,
                "target_mr": c.target_mr, "meas_mr": c.meas_mr, "err_total": c.err_total,
                "err_mr": c.err_mr, "ctl_status": c.status, "ctl_enabled": c.enabled,
                "ctl_sat": c.saturation, "sp_ox": c.sp_ox, "sp_fuel": c.sp_fuel,
                "alarms": list(self.safety.active_alarms.values()),
                "abort": self.last_abort if s.state == State.ABORT else None,
                "last_abort": self.last_abort,
                "prechecks": list(s.precheck_results), "history": list(s.history[-30:]),
                "runs_completed": s.runs_completed,
                "events": list(self.events),
                "logger": dict(self.logger.stats, active=self.logger.active,
                               healthy=self.logger.healthy, run_id=self.logger.run_id,
                               run_dir=self.logger.run_dir, last_error=self.logger.last_error,
                               queue_depth=self.logger.q.qsize()),
                "loops": dict(self.loop_stats), "n_slow": self.n_slow, "n_hf": self.n_hf,
                "faults": self.backend.active_faults() if hasattr(self.backend, "active_faults") else [],
                "backend": self.backend.name, "simulation": self.backend.is_simulation,
                "running": self._running.is_set(),
                "test_active": s.state in ACTIVE_STATES,
            }
