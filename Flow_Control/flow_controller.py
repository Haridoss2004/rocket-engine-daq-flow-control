"""Two-valve virtual flow controller: holds total flow and mixture ratio.

    Total Flow    = Oxidizer Flow + Fuel Flow
    Mixture Ratio = Oxidizer Flow / Fuel Flow

The two targets are converted to one flow set-point per propellant:
    ox_sp   = total x MR / (1 + MR)        fuel_sp = total / (1 + MR)
and each valve is driven by its own PID loop. Outputs are VIRTUAL 0-10 V
commands; nothing here can reach a physical analog output.
"""
from __future__ import annotations

import math

from .pid import PID


def total_flow(ox, fuel):
    return ox + fuel


def mixture_ratio(ox, fuel, min_fuel=0.02):
    """O/F ratio. NaN when the fuel flow is too small for the ratio to mean anything."""
    if not (math.isfinite(ox) and math.isfinite(fuel)) or fuel < min_fuel:
        return float("nan")
    return ox / fuel


def split_setpoints(total, mr):
    return total * mr / (1.0 + mr), total / (1.0 + mr)


class FlowController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.target_total = cfg["target_total_flow_kg_s"]
        self.target_mr = cfg["target_mixture_ratio"]
        lo, hi, rl = cfg["output_min_v"], cfg["output_max_v"], cfg["rate_limit_v_per_s"]
        fh = cfg["derivative_filter_hz"]
        self.pid_ox = PID(**cfg["pid_ox"], out_min=lo, out_max=hi, rate_limit=rl, d_filter_hz=fh)
        self.pid_fuel = PID(**cfg["pid_fuel"], out_min=lo, out_max=hi, rate_limit=rl, d_filter_hz=fh)
        self.enabled = False
        self.demand_fraction = 1.0       # sequencer scales the total-flow set-point (start/ramp)
        self.inhibited = False           # latched by the safety system on abort
        self.status = "DISABLED"
        self.sp_total = self.sp_ox = self.sp_fuel = 0.0
        self.meas_total = self.meas_mr = float("nan")
        self.err_total = self.err_mr = 0.0
        self.out = [0.0, 0.0]

    # ---- configuration
    def set_targets(self, total=None, mr=None):
        if total is not None:
            if total < 0:
                raise ValueError("total flow target must be >= 0")
            self.target_total = float(total)
        if mr is not None:
            if mr <= 0:
                raise ValueError("mixture ratio target must be > 0")
            self.target_mr = float(mr)

    def set_gains(self, which, kp, ki, kd):
        pid = self.pid_ox if which == "ox" else self.pid_fuel
        pid.kp, pid.ki, pid.kd = float(kp), float(ki), float(kd)

    # ---- mode
    def enable(self, demand_fraction=1.0):
        if self.inhibited:
            return False
        if not self.enabled:
            self.pid_ox.reset(self.out[0])
            self.pid_fuel.reset(self.out[1])
        self.enabled = True
        self.demand_fraction = demand_fraction
        return True

    def disable(self):
        self.enabled = False

    def inhibit(self):
        """Abort: no further control commands until the operator resets."""
        self.enabled = False
        self.inhibited = True
        self.out = [0.0, 0.0]
        self.pid_ox.reset(0.0)
        self.pid_fuel.reset(0.0)
        self.status = "INHIBITED (ABORT)"

    def clear_inhibit(self):
        self.inhibited = False
        self.status = "DISABLED"

    # ---- one control period
    def step(self, ox_meas, fuel_meas, dt):
        c = self.cfg
        self.meas_total = total_flow(ox_meas, fuel_meas)
        self.meas_mr = mixture_ratio(ox_meas, fuel_meas, c["min_fuel_flow_for_mr_kg_s"])
        valid = math.isfinite(ox_meas) and math.isfinite(fuel_meas)
        if self.inhibited:
            self.out = [0.0, 0.0]
            self.sp_total = self.sp_ox = self.sp_fuel = 0.0
            return self.out
        if not self.enabled:
            # ramp the virtual commands to zero at the configured rate limit
            step = c["rate_limit_v_per_s"] * dt
            self.out = [max(0.0, v - step) for v in self.out]
            self.sp_total = self.sp_ox = self.sp_fuel = 0.0
            self.err_total = self.err_mr = 0.0
            self.status = "DISABLED"
            return self.out
        self.sp_total = self.target_total * self.demand_fraction
        self.sp_ox, self.sp_fuel = split_setpoints(self.sp_total, self.target_mr)
        if not valid:
            self.status = "FEEDBACK INVALID - holding output"
            return self.out
        self.out = [self.pid_ox.step(self.sp_ox, ox_meas, dt),
                    self.pid_fuel.step(self.sp_fuel, fuel_meas, dt)]
        self.err_total = self.sp_total - self.meas_total
        self.err_mr = (self.target_mr - self.meas_mr) if math.isfinite(self.meas_mr) else float("nan")
        band = c["settle_band_pct"] / 100.0
        if self.pid_ox.saturated or self.pid_fuel.saturated:
            self.status = "SATURATED"
        elif (self.sp_total > 0 and abs(self.err_total) <= band * self.sp_total
              and math.isfinite(self.err_mr) and abs(self.err_mr) <= band * self.target_mr):
            self.status = "SETTLED"
        else:
            self.status = "TRACKING"
        return self.out

    @property
    def saturation(self):
        """Per-valve saturation flags [ox, fuel]."""
        if not self.enabled:
            return [False, False]
        return [bool(self.pid_ox.saturated), bool(self.pid_fuel.saturated)]
