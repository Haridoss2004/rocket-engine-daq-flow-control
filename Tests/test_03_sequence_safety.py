"""Tests 11-14: state transitions, pressure threshold, sensor failure, emergency stop, DAQ errors."""
import numpy as np
import pytest

from Configuration.defaults import DO
from State_Machine.sequencer import (ARMED_STATES, VALID_TRANSITIONS, InvalidTransition, State)
from Tests.conftest import goto


def assert_abort(e, code):
    """The eight required abort actions."""
    assert e.state == State.ABORT                                         # 1 state
    assert e.controller.inhibited and not e.controller.enabled            # 2 control disabled
    assert (e.outputs.do == e.outputs.do_safe).all()                      # 3 safe outputs
    assert e.outputs.ao.tolist() == [0.0, 0.0] and e.outputs.locked
    assert not e.outputs.do[DO["OX_MAIN"]] and not e.outputs.do[DO["FUEL_MAIN"]]
    assert not e.outputs.do[DO["IGNITER"]]
    assert e.outputs.do[DO["OX_VENT"]] and e.outputs.do[DO["PURGE"]]
    a = e.last_abort
    assert a["code"] == code and a["reason"]                              # 4 reason
    assert a["timestamp"] and a["t_s"] > 0                                # 5 timestamp
    assert e.snapshot()["abort"] is not None                              # 6 prominent alarm data
    assert any(ev[2] == "ABORT" for ev in e.events)
    assert e.logger.active and e.logger.healthy                           # 7 log preserved
    before = e.outputs.ao.copy()                                          # 8 needs explicit reset
    assert e.cmd_start()[0] is False and e.cmd_fire()[0] is False
    e.run_for(0.5)
    assert e.state == State.ABORT and np.array_equal(e.outputs.ao, before)


# ---------------------------------------------------------------- 11. state transitions
def test_11_full_sequence_visits_every_state_in_order(engine):
    seen = [engine.state]
    engine.run_for(0.2)
    assert engine.cmd_start()[0]
    fired = False
    while engine.state != State.COMPLETE and engine.t < 40:
        engine.run_for(engine.dt_block)
        if engine.state != seen[-1]:
            seen.append(engine.state)
        if engine.state == State.READY and not fired:
            engine.run_for(0.3)
            assert engine.cmd_fire()[0]
            fired = True
    assert seen == [State.IDLE, State.PRECHECK, State.READY, State.START, State.RAMP, State.HOLD,
                    State.SHUTDOWN, State.COMPLETE]
    assert engine.outputs.ao.tolist() == [0.0, 0.0]
    assert not engine.outputs.do[DO["OX_MAIN"]] and not engine.outputs.do[DO["IGNITER"]]
    assert engine.cmd_reset()[0] and engine.state == State.IDLE


def test_11_transition_table():
    assert set(VALID_TRANSITIONS) == set(State) and len(State) == 9
    for s in State:
        if s != State.ABORT:
            assert State.ABORT in VALID_TRANSITIONS[s]       # abort reachable from everywhere
    assert VALID_TRANSITIONS[State.ABORT] == {State.IDLE}    # only an operator reset leaves ABORT
    assert State.START not in VALID_TRANSITIONS[State.IDLE]
    assert State.START not in VALID_TRANSITIONS[State.PRECHECK]
    assert State.HOLD not in VALID_TRANSITIONS[State.START]


def test_11_invalid_requests_are_rejected(engine):
    engine.run_for(0.1)
    assert engine.cmd_fire()[0] is False                     # not READY
    assert engine.cmd_stop()[0] is False
    assert engine.cmd_reset()[0] is False
    with pytest.raises(InvalidTransition):
        engine.sequencer._go(State.HOLD)
    assert engine.state == State.IDLE
    goto(engine, State.HOLD)
    assert engine.cmd_start()[0] is False and engine.cmd_fire()[0] is False


def test_11_operator_stop(engine):
    goto(engine, State.READY)
    assert engine.cmd_stop()[0] and engine.state == State.IDLE
    goto(engine, State.RAMP)
    assert engine.cmd_stop()[0] and engine.state == State.SHUTDOWN
    goto(engine, State.COMPLETE)


def test_11_states_drive_expected_virtual_outputs(engine):
    o = engine.outputs.do
    goto(engine, State.READY)
    assert o[DO["OX_PRESS"]] and o[DO["FUEL_PRESS"]] and not o[DO["OX_VENT"]] and not o[DO["OX_MAIN"]]
    goto(engine, State.START)
    engine.run_for(0.6)
    assert o[DO["IGNITER"]] and not o[DO["OX_MAIN"]]         # igniter before propellant valves
    goto(engine, State.HOLD)
    assert o[DO["OX_MAIN"]] and o[DO["FUEL_MAIN"]] and not o[DO["IGNITER"]]
    goto(engine, State.SHUTDOWN)
    engine.run_for(1.5)
    assert not o[DO["OX_MAIN"]] and not o[DO["FUEL_MAIN"]] and o[DO["PURGE"]]
    assert engine.outputs.ao.max() < 0.01


@pytest.mark.parametrize("fault,check", [
    (("slow", "PT-02", "disconnect"), "Valid measurements"),
    (("slow", "TT-03", "nan"), "Valid measurements"),
    (("di", 3, "stuck_low"), "Digital input"),
    (("overpressure",), "Pressures within"),
])
def test_11_precheck_failure_blocks_start(engine, fault, check):
    engine.cfg["sequence"]["precheck_timeout_s"] = 4.0
    engine.run_for(0.2)
    if fault[0] == "slow":
        engine.inject_fault("slow", engine.slow_id[fault[1]], fault[2])
    else:
        engine.inject_fault(*fault)
    assert engine.cmd_start()[0]
    states = set()
    while engine.t < 8 and (engine.state != State.IDLE or len(states) < 1):
        engine.run_for(engine.dt_block)
        states.add(engine.state)
    assert engine.state == State.IDLE
    assert states <= {State.PRECHECK, State.IDLE}            # READY / START never entered
    failed = [n for n, ok, _ in engine.sequencer.precheck_results if not ok]
    assert any(check in n for n in failed), failed
    assert any(ev[2] == "ALARM" and "Pre-check FAILED" in ev[3] for ev in engine.events)


def test_11_start_blocked_if_check_fails_while_ready(engine):
    goto(engine, State.READY)
    engine.cfg["logging"]["auto_start_on_precheck"] = False
    engine.logger.stop_run()                                 # data logger no longer available
    engine.cmd_fire()
    engine.run_for(0.5)
    assert engine.state == State.READY                       # START refused
    assert any("Start sequence blocked" in ev[3] for ev in engine.events)


def test_11_no_ignition_aborts(engine):
    engine.cfg["simulation"]["kmax_fuel_kg_s"] = 0.0         # no fuel flow -> no combustion
    goto(engine, State.ABORT)
    assert engine.last_abort["code"] == "SEQUENCE" and "Ignition not confirmed" in engine.last_abort["reason"]


# ---------------------------------------------------------------- 12. pressure threshold
def test_12_overpressure_aborts(engine):
    goto(engine, State.HOLD)
    engine.run_for(1.0)
    t_inject = engine.t
    engine.inject_fault("overpressure")
    engine.run_until(lambda: engine.state == State.ABORT, 2.0)
    assert_abort(engine, "OVERPRESSURE")
    assert "exceeds abort threshold" in engine.last_abort["reason"]
    assert engine.last_abort["t_s"] - t_inject < 0.1         # detected within 100 ms


def test_12_pressure_spike_aborts(engine):
    goto(engine, State.HOLD)
    engine.inject_fault("spike")
    engine.run_until(lambda: engine.state == State.ABORT, 1.0)
    assert_abort(engine, "OVERPRESSURE")


def test_12_threshold_is_configurable(engine):
    goto(engine, State.HOLD)
    engine.run_for(1.0)
    assert engine.state == State.HOLD                        # 20 bar < 28 bar default threshold
    engine.update_slow_channel(engine.slow_id["PT-07"], abort_hi=15.0)
    engine.run_for(0.1)
    assert_abort(engine, "OVERPRESSURE")
    assert "abort threshold 15.0" in engine.last_abort["reason"]


def test_12_debounce_ignores_single_sample_glitch(cfg):
    from Safety.safety_monitor import SafetyMonitor
    cfg["safety"]["pressure_abort_debounce_samples"] = 3
    mon = SafetyMonitor(cfg)
    i = 6                                                    # PT-07, abort_hi 28 bar
    eng = np.full((20, 50), 1.0)
    st = np.zeros(50, dtype=np.int8)
    di = np.ones(30, dtype=bool)
    eng[5, i] = 99.0                                         # 1 sample
    assert mon.evaluate(eng, st, di) == []
    eng[5:7, i] = 99.0                                       # 2 samples
    assert mon.evaluate(eng, st, di) == []
    eng[5:8, i] = 99.0                                       # 3 consecutive samples
    assert [t.code for t in mon.evaluate(eng, st, di)] == ["OVERPRESSURE"]
    eng[:] = 1.0
    eng[-2:, i] = 99.0                                       # run continues across block boundary
    assert mon.evaluate(eng, st, di) == []
    eng[:] = 1.0
    eng[0, i] = 99.0
    assert [t.code for t in mon.evaluate(eng, st, di)] == ["OVERPRESSURE"]


# ---------------------------------------------------------------- 13. pressure sensor failure
@pytest.mark.parametrize("ftype,value,text", [
    ("disconnect", 0.0, "out of range"), ("nan", 0.0, "NaN"), ("inf", 0.0, "Infinity"),
    ("stuck", 0.0, "stale"), ("offset", 8.5, "out of range"),
])
@pytest.mark.parametrize("channel", ["PT-07", "PT-01"])
def test_13_pressure_sensor_failure_aborts(engine, ftype, value, text, channel):
    goto(engine, State.HOLD)
    engine.inject_fault("slow", engine.slow_id[channel], ftype, value)
    engine.run_until(lambda: engine.state == State.ABORT, 2.0)
    assert_abort(engine, "SENSOR_FAILURE")
    assert channel in engine.last_abort["reason"] and text in engine.last_abort["reason"]


@pytest.mark.parametrize("state", sorted(ARMED_STATES, key=lambda s: list(State).index(s)))
def test_13_sensor_failure_aborts_in_every_armed_state(engine, state):
    goto(engine, state)
    engine.inject_fault("slow", engine.slow_id["PT-05"], "nan")
    engine.run_for(0.1)
    assert_abort(engine, "SENSOR_FAILURE")


def test_13_temperature_sensor_failure_alarms_without_abort(engine):
    engine.cfg["sequence"]["hold_duration_s"] = 5.0
    goto(engine, State.HOLD)
    engine.inject_fault("slow", engine.slow_id["TT-01"], "disconnect")
    engine.run_for(1.0)
    assert engine.state == State.HOLD
    assert any("TT-01" in a for a in engine.snapshot()["alarms"])


def test_13_fault_in_idle_alarms_but_does_not_abort(engine):
    engine.run_for(0.2)
    engine.inject_fault("slow", engine.slow_id["PT-07"], "nan")
    engine.run_for(0.5)
    assert engine.state == State.IDLE
    assert any("PT-07" in a for a in engine.snapshot()["alarms"])


# ---------------------------------------------------------------- 14. emergency stop
@pytest.mark.parametrize("state", [State.IDLE, State.PRECHECK, State.READY, State.START, State.RAMP,
                                   State.HOLD, State.SHUTDOWN, State.COMPLETE])
def test_14_emergency_stop_aborts_from_every_state(engine, state):
    if state != State.IDLE:
        goto(engine, state)
    else:
        engine.run_for(0.2)
        engine.start_logging()
    engine.set_estop(True)
    engine.run_for(0.06)
    assert_abort(engine, "ESTOP")


def test_14_reset_requires_cause_cleared_and_operator_action(engine):
    goto(engine, State.HOLD)
    engine.set_estop(True)
    engine.run_for(0.1)
    ok, msg = engine.cmd_reset()
    assert not ok and "still active" in msg and engine.state == State.ABORT
    engine.set_estop(False)
    engine.run_for(2.0)
    assert engine.state == State.ABORT                       # never resets by itself
    assert engine.cmd_reset()[0] and engine.state == State.IDLE
    assert not engine.outputs.locked and not engine.controller.inhibited
    goto(engine, State.COMPLETE)                             # a new test can run afterwards


def test_14_manual_abort(engine):
    goto(engine, State.RAMP)
    assert engine.cmd_abort()[0]
    assert_abort(engine, "OPERATOR")


# ---------------------------------------------------------------- D. acquisition errors
@pytest.mark.parametrize("kind,text", [("acq_error", "hardware fault"), ("acq_timeout", "timed out")])
def test_daq_error_aborts(engine, kind, text):
    goto(engine, State.HOLD)
    engine.inject_fault(kind)
    engine.run_for(0.1)
    assert_abort(engine, "DAQ_ERROR")
    assert text in engine.last_abort["reason"]
    assert not engine.cmd_reset()[0]                         # error still present
    engine.inject_fault("clear")
    engine.run_for(0.2)
    assert engine.cmd_reset()[0]


def test_watchdog_detects_stalled_acquisition(engine):
    import time
    goto(engine, State.HOLD)
    mon = engine.safety
    assert mon.check_watchdog(time.monotonic()) is None
    trip = mon.check_watchdog(time.monotonic() + 5.0)        # 5 s without a slow block
    assert trip is not None and trip.code == "WATCHDOG" and trip.always
    engine._abort(trip)
    assert_abort(engine, "WATCHDOG")
