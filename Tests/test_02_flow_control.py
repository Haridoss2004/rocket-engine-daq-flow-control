"""Tests 7-10: flow calculation, mixture ratio, PID behaviour, saturation / anti-windup."""
import math

import numpy as np
import pytest

from Flow_Control.flow_controller import mixture_ratio, split_setpoints, total_flow
from Flow_Control.pid import PID
from State_Machine.sequencer import State
from Tests.conftest import goto


# ---------------------------------------------------------------- 7. / 8. calculations
def test_07_total_flow():
    assert total_flow(1.6, 0.8) == pytest.approx(2.4)
    assert total_flow(0.0, 0.0) == 0.0


def test_08_mixture_ratio():
    assert mixture_ratio(1.6, 0.8) == pytest.approx(2.0)
    assert math.isnan(mixture_ratio(1.0, 0.0))            # no division by zero
    assert math.isnan(mixture_ratio(1.0, 0.001))          # below the minimum fuel flow
    assert math.isnan(mixture_ratio(float("nan"), 1.0))


@pytest.mark.parametrize("total,mr", [(2.5, 2.0), (1.0, 6.0), (3.3, 1.3)])
def test_08_setpoint_split_is_consistent(total, mr):
    ox, fuel = split_setpoints(total, mr)
    assert total_flow(ox, fuel) == pytest.approx(total)
    assert mixture_ratio(ox, fuel) == pytest.approx(mr)


def test_07_08_engine_reports_flow_and_ratio_from_acquired_channels(engine):
    goto(engine, State.HOLD)
    engine.run_for(1.5)
    c = engine.controller
    assert c.meas_total == pytest.approx(engine.ox_flow + engine.fuel_flow)
    assert c.meas_mr == pytest.approx(engine.ox_flow / engine.fuel_flow)


# ---------------------------------------------------------------- 9. PID behaviour
def _first_order(pid, sp, k=0.3, tau=0.15, dt=0.02, t_end=5.0):
    y, hist = 0.0, []
    for _ in range(int(t_end / dt)):
        u = pid.step(sp, y, dt)
        y += (k * u - y) * dt / tau
        hist.append((u, y))
    return np.array(hist)


def test_09_pid_step_response_converges_without_steady_state_error():
    h = _first_order(PID(2.5, 22.0, 0.0, 0.0, 10.0, rate_limit=20.0), sp=1.5)
    assert h[-1, 1] == pytest.approx(1.5, abs=1e-3)
    assert h[:, 1].max() < 1.5 * 1.10                    # overshoot below 10 %
    settled = np.nonzero(np.abs(h[:, 1] - 1.5) > 0.03)[0]
    assert (settled[-1] + 1) * 0.02 < 1.5                # inside 2 % band within 1.5 s


def test_09_pid_terms():
    p = PID(2.0, 0.0, 0.0, -100, 100)
    assert p.step(3.0, 1.0, 0.1) == pytest.approx(4.0)   # P only: Kp * error
    i = PID(0.0, 5.0, 0.0, -100, 100)
    for _ in range(10):
        out = i.step(1.0, 0.0, 0.1)
    assert out == pytest.approx(5.0)                     # I only: Ki * error * time
    d = PID(0.0, 0.0, 1.0, -100, 100, d_filter_hz=1e6)
    d.step(0.0, 0.0, 0.1)
    assert d.step(0.0, 0.5, 0.1) == pytest.approx(-5.0, rel=0.01)   # derivative on measurement


def test_09_closed_loop_tracks_targets_in_hold(engine):
    goto(engine, State.HOLD)
    engine.run_for(1.5)
    tot, mr = [], []
    for _ in range(25):
        engine.run_for(0.02)
        tot.append(engine.controller.meas_total)
        mr.append(engine.controller.meas_mr)
    assert np.mean(tot) == pytest.approx(2.5, abs=0.02)
    assert np.mean(mr) == pytest.approx(2.0, abs=0.02)
    assert np.std(tot) < 0.02 and np.std(mr) < 0.02      # stable
    assert engine.controller.status == "SETTLED"


def test_09_setpoint_change_during_hold(engine):
    engine.cfg["sequence"]["hold_duration_s"] = 8.0
    goto(engine, State.HOLD)
    engine.run_for(1.0)
    engine.set_targets(total=1.8, mr=2.4)
    engine.run_for(2.0)
    assert engine.controller.meas_total == pytest.approx(1.8, abs=0.03)
    assert engine.controller.meas_mr == pytest.approx(2.4, abs=0.04)
    with pytest.raises(ValueError):
        engine.set_targets(mr=0.0)


# ---------------------------------------------------------------- 10. saturation, anti-windup, rate limit
def test_10_output_saturates_between_0_and_10_volts():
    hi = PID(5.0, 50.0, 0.0, 0.0, 10.0)
    lo = PID(5.0, 50.0, 0.0, 0.0, 10.0)
    for _ in range(200):
        assert hi.step(100.0, 0.0, 0.02) <= 10.0
        assert lo.step(-100.0, 0.0, 0.02) >= 0.0
    assert hi.output == 10.0 and hi.saturated
    assert lo.output == 0.0 and lo.saturated


def test_10_anti_windup_bounds_integrator_and_recovers_quickly():
    pid = PID(1.0, 20.0, 0.0, 0.0, 10.0)
    for _ in range(500):                                 # 10 s of unreachable set-point
        pid.step(100.0, 0.0, 0.02)
    assert pid.integral <= 10.0                          # without anti-windup this would be ~20000
    steps = 0
    while pid.step(0.0, 1.0, 0.02) >= 10.0:              # error reverses
        steps += 1
        assert steps < 5, "controller stuck in saturation: integrator wound up"


def test_10_output_rate_limit():
    pid = PID(100.0, 0.0, 0.0, 0.0, 10.0, rate_limit=5.0)   # 5 V/s
    prev, outs = 0.0, []
    for _ in range(50):
        u = pid.step(10.0, 0.0, 0.02)
        assert u - prev <= 5.0 * 0.02 + 1e-9
        prev = u
        outs.append(u)
    assert outs[-1] == pytest.approx(5.0, abs=1e-6)      # after 1 s at 5 V/s


def test_10_engine_saturation_with_unreachable_target(engine):
    engine.cfg["sequence"]["hold_duration_s"] = 6.0
    engine.set_targets(total=9.0)                        # far beyond what the simulated valves pass
    goto(engine, State.HOLD)
    engine.run_for(3.0)
    assert engine.state == State.HOLD
    assert engine.outputs.ao.max() == pytest.approx(10.0)
    assert engine.outputs.ao.max() <= 10.0 and engine.outputs.ao.min() >= 0.0
    assert engine.controller.status == "SATURATED"
    assert any(engine.snapshot()["ao_sat"])
    assert engine.controller.pid_ox.integral <= 10.0 and engine.controller.pid_fuel.integral <= 10.0


def test_10_invalid_feedback_holds_output(engine):
    engine.cfg["sequence"]["hold_duration_s"] = 6.0
    goto(engine, State.HOLD)
    engine.run_for(1.0)
    before = engine.outputs.ao.copy()
    engine.inject_fault("slow", engine.slow_id["FT-01"], "nan")
    engine.run_for(0.5)
    assert engine.state == State.HOLD                    # a flow-meter fault is not a pressure abort
    assert np.allclose(engine.outputs.ao, before)
    assert "FEEDBACK INVALID" in engine.controller.status
