"""Discrete PID controller with output saturation, anti-windup and rate limiting."""
from __future__ import annotations

import math


class PID:
    def __init__(self, kp, ki, kd, out_min=0.0, out_max=10.0, rate_limit=None,
                 d_filter_hz=10.0):
        self.kp, self.ki, self.kd = float(kp), float(ki), float(kd)
        self.out_min, self.out_max = float(out_min), float(out_max)
        self.rate_limit = rate_limit            # units of output per second, None = unlimited
        self.d_filter_hz = d_filter_hz
        self.reset()

    def reset(self, output=0.0):
        self.integral = float(output)           # bumpless: integral holds the current output
        self.output = float(output)
        self._prev_pv = None
        self._d_state = 0.0
        self.saturated = False
        self.rate_limited = False
        self.error = 0.0
        self.p_term = self.d_term = 0.0

    def step(self, setpoint, pv, dt):
        e = setpoint - pv
        self.error = e
        # derivative on measurement (no set-point kick), first-order filtered
        if self._prev_pv is None or self.kd == 0.0:
            d_raw = 0.0
        else:
            d_raw = -(pv - self._prev_pv) / dt
        self._prev_pv = pv
        alpha = 1.0 - math.exp(-2 * math.pi * self.d_filter_hz * dt)
        self._d_state += (d_raw - self._d_state) * alpha
        self.p_term = self.kp * e
        self.d_term = self.kd * self._d_state

        i_new = self.integral + self.ki * e * dt
        u = self.p_term + i_new + self.d_term
        u_sat = min(max(u, self.out_min), self.out_max)             # saturation
        u_out = u_sat
        if self.rate_limit is not None:                              # rate limiting
            step = self.rate_limit * dt
            u_out = self.output + min(max(u_sat - self.output, -step), step)
        self.saturated = u != u_sat
        self.rate_limited = abs(u_out - u_sat) > 1e-12
        # anti-windup (conditional integration): freeze the integrator while the output is
        # limited, unless the error would drive the output back out of the limit
        limited = self.saturated or self.rate_limited
        if (not limited) or (u > u_out and e < 0) or (u < u_out and e > 0):
            self.integral = min(max(i_new, self.out_min), self.out_max)
        self.output = u_out
        return u_out
