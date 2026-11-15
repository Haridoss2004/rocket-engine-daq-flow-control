"""Test-sequence state machine (SIMULATED sequence - controls no real hardware)."""
from __future__ import annotations

from enum import Enum

from Configuration.defaults import DO


class State(str, Enum):
    IDLE = "IDLE"
    PRECHECK = "PRE-CHECK"
    READY = "READY"
    START = "START SEQUENCE (SIMULATED)"
    RAMP = "RAMP"
    HOLD = "HOLD"
    SHUTDOWN = "SHUTDOWN (SIMULATED)"
    COMPLETE = "COMPLETE"
    ABORT = "ABORT"


S = State
VALID_TRANSITIONS = {
    S.IDLE: {S.PRECHECK, S.ABORT},
    S.PRECHECK: {S.READY, S.IDLE, S.ABORT},
    S.READY: {S.START, S.IDLE, S.ABORT},
    S.START: {S.RAMP, S.SHUTDOWN, S.ABORT},
    S.RAMP: {S.HOLD, S.SHUTDOWN, S.ABORT},
    S.HOLD: {S.SHUTDOWN, S.ABORT},
    S.SHUTDOWN: {S.COMPLETE, S.ABORT},
    S.COMPLETE: {S.IDLE, S.ABORT},
    S.ABORT: {S.IDLE},
}
# States in which pressure / sensor-failure conditions cause an abort
ARMED_STATES = {S.READY, S.START, S.RAMP, S.HOLD, S.SHUTDOWN}
ACTIVE_STATES = {S.PRECHECK, S.READY, S.START, S.RAMP, S.HOLD, S.SHUTDOWN}

D = DO
_IDLE = (D["OX_VENT"], D["FUEL_VENT"])
_PRESSURISED = (D["OX_PRESS"], D["FUEL_PRESS"], D["ARMED_LAMP"])
_ARMED = _PRESSURISED + (D["BEACON"],)
_FIRING = _ARMED + (D["ACTIVE_LAMP"], D["SIREN"], D["CAMERA"])


class InvalidTransition(RuntimeError):
    pass


class Sequencer:
    def __init__(self, seq_cfg, outputs, controller, precheck_fn, event_fn):
        self.cfg = seq_cfg
        self.outputs = outputs
        self.controller = controller
        self.precheck_fn = precheck_fn           # () -> list[(name, ok, detail)]
        self.event = event_fn                    # (type, message)
        self.state = S.IDLE
        self.t_enter = 0.0
        self.now = 0.0
        self.precheck_results = []
        self._ok_since = None
        self._fire_requested = False
        self.abort_reason = None
        self.abort_time = None
        self.history = [(0.0, S.IDLE)]
        self.runs_completed = 0
        self.outputs.set_do_pattern(_IDLE)

    # ------------------------------------------------------------------ transitions
    def _go(self, new):
        if new not in VALID_TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state.value} -> {new.value} is not a valid transition")
        old, self.state, self.t_enter = self.state, new, self.now
        self.history.append((self.now, new))
        del self.history[:-200]
        self.event("STATE", f"{old.value} -> {new.value}")

    @property
    def elapsed(self):
        return self.now - self.t_enter

    # ------------------------------------------------------------------ operator requests
    def request_precheck(self):
        if self.state != S.IDLE:
            return False, f"Start rejected: state is {self.state.value}, must be IDLE"
        self._ok_since = None
        self._fire_requested = False
        self._go(S.PRECHECK)
        return True, "Pre-check started"

    def request_fire(self):
        if self.state != S.READY:
            return False, f"Start sequence rejected: state is {self.state.value}, must be READY"
        self._fire_requested = True
        return True, "Start sequence requested"

    def request_stop(self):
        if self.state in (S.PRECHECK, S.READY):
            self._go(S.IDLE)
            return True, "Sequence cancelled, returned to IDLE"
        if self.state in (S.START, S.RAMP, S.HOLD):
            self._go(S.SHUTDOWN)
            return True, "Operator stop: controlled shutdown started"
        return False, f"Stop ignored in state {self.state.value}"

    def request_reset(self, conditions_clear=True):
        if self.state == S.COMPLETE:
            self._go(S.IDLE)
            return True, "Reset to IDLE"
        if self.state == S.ABORT:
            if not conditions_clear:
                return False, "Reset rejected: an abort condition is still active"
            self.outputs.unlock()
            self.controller.clear_inhibit()
            self.abort_reason = self.abort_time = None
            self._go(S.IDLE)
            return True, "Abort acknowledged, reset to IDLE"
        return False, f"Reset ignored in state {self.state.value}"

    def force_abort(self, reason, timestamp):
        """Called by the safety system. Immediate, from any state."""
        if self.state == S.ABORT:
            return False
        self.controller.inhibit()                 # 2. no further control commands
        self.outputs.apply_safe_state(lock=True)  # 3. all virtual outputs to safe state
        self.abort_reason, self.abort_time = reason, timestamp   # 4./5. reason + time
        self._go(S.ABORT)                         # 1. state
        return True

    # ------------------------------------------------------------------ periodic step
    def step(self, now, chamber_pressure_bar):
        """Run one sequencer period. Returns an abort reason string or None."""
        self.now = now
        c, el, st = self.cfg, self.elapsed, self.state
        out, ctl = self.outputs, self.controller

        if st == S.IDLE:
            ctl.disable()
            out.set_do_pattern(_IDLE)
        elif st == S.PRECHECK:
            ctl.disable()
            out.set_do_pattern(_PRESSURISED)
            self.precheck_results = self.precheck_fn()
            if all(ok for _, ok, _ in self.precheck_results):
                if self._ok_since is None:
                    self._ok_since = now
                elif now - self._ok_since >= c["precheck_stable_s"]:
                    self.event("INFO", "Pre-check PASSED: all checks OK")
                    self._go(S.READY)
            else:
                self._ok_since = None
                if el >= c["precheck_timeout_s"]:
                    failed = "; ".join(f"{n} ({d})" for n, ok, d in self.precheck_results if not ok)
                    self.event("ALARM", f"Pre-check FAILED: {failed}")
                    self._go(S.IDLE)
        elif st == S.READY:
            ctl.disable()
            out.set_do_pattern(_ARMED)
            self.precheck_results = self.precheck_fn()
            if self._fire_requested or c["auto_start_from_ready"]:
                self._fire_requested = False
                bad = [n for n, ok, _ in self.precheck_results if not ok]
                if bad:     # START is never entered unless every check passes
                    self.event("ALARM", "Start sequence blocked, checks failing: " + ", ".join(bad))
                else:
                    self._go(S.START)
        elif st == S.START:
            on = list(_FIRING)
            if el < c["start_purge_off_s"]:
                on.append(D["PURGE"])
            if el >= c["start_igniter_on_s"]:
                on.append(D["IGNITER"])
            if el >= c["start_valves_open_s"]:
                on += [D["OX_MAIN"], D["FUEL_MAIN"]]
                ctl.enable(c["start_flow_fraction"])
            out.set_do_pattern(on)
            if el >= c["start_duration_s"]:
                if chamber_pressure_bar < c["ignition_confirm_bar"]:
                    return (f"Ignition not confirmed (SIMULATED): chamber pressure "
                            f"{chamber_pressure_bar:.1f} bar < {c['ignition_confirm_bar']:.1f} bar")
                self._go(S.RAMP)
        elif st == S.RAMP:
            out.set_do_pattern(_FIRING + (D["OX_MAIN"], D["FUEL_MAIN"]))
            f0 = c["start_flow_fraction"]
            ctl.enable(min(1.0, f0 + (1.0 - f0) * el / max(c["ramp_duration_s"], 1e-6)))
            if el >= c["ramp_duration_s"]:
                self._go(S.HOLD)
        elif st == S.HOLD:
            out.set_do_pattern(_FIRING + (D["OX_MAIN"], D["FUEL_MAIN"]))
            ctl.enable(1.0)
            if el >= c["hold_duration_s"]:
                self._go(S.SHUTDOWN)
        elif st == S.SHUTDOWN:
            ctl.disable()                          # commands ramp to 0 V at the rate limit
            on = [D["BEACON"], D["PURGE"], D["ACTIVE_LAMP"]]
            if el < c["shutdown_valves_close_s"]:
                on += [D["OX_MAIN"], D["FUEL_MAIN"]]
            if el >= c["shutdown_vent_open_s"]:
                on += [D["OX_VENT"], D["FUEL_VENT"]]
            out.set_do_pattern(on)
            if el >= c["shutdown_duration_s"]:
                self.runs_completed += 1
                self._go(S.COMPLETE)
        elif st == S.COMPLETE:
            ctl.disable()
            out.set_do_pattern(_IDLE)
        # ABORT: outputs stay locked in the safe state until the operator resets
        return None

    @property
    def progress(self):
        """0..1 progress through START..SHUTDOWN."""
        c = self.cfg
        dur = [c["start_duration_s"], c["ramp_duration_s"], c["hold_duration_s"],
               c["shutdown_duration_s"]]
        order = [S.START, S.RAMP, S.HOLD, S.SHUTDOWN]
        if self.state == S.COMPLETE:
            return 1.0
        if self.state not in order:
            return 0.0
        i = order.index(self.state)
        return (sum(dur[:i]) + min(self.elapsed, dur[i])) / sum(dur)
