"""Tests 1-6: channel counts, acquisition, digital I/O, analog outputs, scaling, configuration."""
import copy

import numpy as np
import pytest

from Acquisition.scaling import ChStatus, RingBuffer, SlowChannelProcessor, scale
from Acquisition.virtual_outputs import VirtualOutputs
from Configuration import config as config_mod
from Configuration.defaults import DO, default_config
from Signal_Processing.fft import amplitude_spectrum, find_peak
from State_Machine.sequencer import State
from Tests.conftest import goto


# ---------------------------------------------------------------- 1. slow analog inputs
def test_01_slow_channel_count_and_rate(engine):
    assert len(engine.slow_ch) == 50
    raw = engine.backend.read_slow(1000)
    assert raw.shape == (1000, 50)
    assert np.isfinite(raw).all()
    assert engine.backend.n_slow_read == 1000          # 1 kS/s per channel -> 1000 samples = 1 s
    assert 50 * engine.fs_slow == 50_000               # aggregate rate


def test_01_all_50_slow_channels_healthy_and_plausible(engine):
    engine.run_for(1.0)
    assert (engine.status == ChStatus.OK).all(), \
        [engine.slow_ch[i]["id"] for i in np.nonzero(engine.status != ChStatus.OK)[0]]
    eng = dict(zip([c["id"] for c in engine.slow_ch], engine.eng_mean))
    assert eng["PT-10"] == pytest.approx(8.0, abs=0.1)      # pneumatic supply
    assert eng["PT-07"] == pytest.approx(1.0, abs=0.1)      # chamber at ambient
    assert eng["TT-14"] == pytest.approx(22.0, abs=0.5)     # ambient temperature
    assert eng["VT-01"] == pytest.approx(24.0, abs=0.2)
    types = {c["sensor_type"] for c in engine.slow_ch}
    assert {"pressure", "temperature", "load_cell", "flow"} <= types


def test_01_every_slow_channel_responds_to_its_own_fault(engine):
    """Each of the 50 channels is individually wired through acquisition -> scaling -> health."""
    engine.run_for(0.1)
    for i in range(50):
        engine.backend.clear_faults()
        engine.backend.inject_slow_fault(i, "nan")
        engine.run_for(0.04)
        bad = np.nonzero(engine.status == ChStatus.INVALID)[0].tolist()
        assert bad == [i]


# ---------------------------------------------------------------- 2. high-frequency inputs
def test_02_hf_channel_count_and_rate(engine):
    assert len(engine.hf_ch) == 25
    blk = engine.backend.read_hf(50_000)
    assert blk.shape == (50_000, 25) and blk.dtype == np.float32
    assert np.isfinite(blk).all()
    assert 25 * engine.fs_hf == 1_250_000              # aggregate rate


def test_02_all_25_hf_channels_carry_their_configured_tone(engine):
    goto(engine, State.HOLD)
    engine.run_for(1.0)
    data = engine.hf_ring.latest(16384)
    for c in engine.hf_ch:
        f, m = amplitude_spectrum(data[:, c["index"]], engine.fs_hf)
        pf, pa = find_peak(f, m)
        tone_f, tone_a = max(c["sim"]["tones"], key=lambda t: t[1])
        assert pf == pytest.approx(tone_f, abs=2 * engine.fs_hf / 16384), c["id"]
        assert pa == pytest.approx(tone_a, rel=0.15), c["id"]


def test_02_hf_buffer_is_independent_and_bounded(engine):
    cap = engine.hf_ring.capacity
    engine.run_for(5.0)
    assert engine.hf_ring.buf.shape == (cap, 25)       # never grows
    assert engine.n_hf == pytest.approx(5.0 * engine.fs_hf, abs=engine.n_hf_block)


def test_ring_buffer_wraps_correctly():
    rb = RingBuffer(10, 1, np.float64)
    for i in range(7):
        rb.write(np.arange(i * 3, i * 3 + 3, dtype=float)[:, None])
    assert rb.latest(10, 0).tolist() == list(range(11, 21))
    assert rb.latest(4, 0).tolist() == [17, 18, 19, 20]


# ---------------------------------------------------------------- 3. digital inputs
def test_03_digital_inputs(engine):
    engine.run_for(0.2)
    assert engine.di.shape == (30,)
    di = {d["id"]: bool(engine.di[d["index"]]) for d in engine.cfg["digital_inputs"]}
    assert di["DI-00"] is True                         # e-stop loop healthy
    assert di["DI-07"] is True and di["DI-06"] is False    # OX main valve closed
    assert di["DI-10"] is True                         # OX vent open in IDLE
    engine.set_estop(True)
    engine.run_for(0.05)
    assert not engine.di[0]


def test_03_valve_feedback_follows_virtual_outputs(engine):
    goto(engine, State.HOLD)
    di = engine.di
    assert di[6] and not di[7] and di[8] and not di[9]     # both main valves report OPEN
    assert di[20] and di[21]                               # chamber pressure switch, flame detector


def test_03_every_digital_input_can_be_forced(engine):
    engine.run_for(0.1)
    base = engine.di.copy()
    for i in range(30):
        engine.backend.clear_faults()
        engine.backend.inject_di_fault(i, "invert")
        engine.backend.read_slow(1)
        now = engine.backend.read_di()
        assert np.nonzero(now != base)[0].tolist() == [i]


# ---------------------------------------------------------------- 4. virtual digital outputs
def test_04_digital_outputs(engine):
    o = engine.outputs
    assert o.do.shape == (30,)
    engine.run_for(0.1)
    assert o.do[DO["OX_VENT"]] and o.do[DO["FUEL_VENT"]] and o.do.sum() == 2      # IDLE pattern
    for i in range(30):                                # every output is individually addressable
        assert o.set_do_pattern([i])
        assert np.nonzero(o.do)[0].tolist() == [i]
    o.apply_safe_state()
    assert (o.do == o.do_safe).all() and o.locked
    assert not o.set_do_pattern([DO["IGNITER"]])       # rejected while locked
    assert not o.do[DO["IGNITER"]] and o.rejected_writes == 1


def test_04_outputs_are_virtual_only(engine):
    from DAQ_Hardware_Abstraction.backend_base import PhysicalOutputDisabledError
    assert engine.backend.physical_outputs_enabled is False
    with pytest.raises(PhysicalOutputDisabledError):
        engine.backend.write_physical_outputs([True], [5.0])


# ---------------------------------------------------------------- 5. virtual analog outputs
def test_05_analog_outputs_range_percent_saturation(cfg):
    o = VirtualOutputs(cfg["digital_outputs"], cfg["analog_outputs"])
    assert o.ao.shape == (2,)
    o.set_ao([2.5, 7.5])
    assert o.ao.tolist() == [2.5, 7.5] and o.ao_percent.tolist() == [25.0, 75.0]
    assert not o.ao_saturated.any()
    o.set_ao([-3.0, 14.0])                             # clamped to 0..10 V
    assert o.ao.tolist() == [0.0, 10.0]
    assert o.ao_saturated.tolist() == [True, True]
    o.apply_safe_state()
    assert o.ao.tolist() == [0.0, 0.0]
    assert not o.set_ao([5.0, 5.0]) and o.ao.tolist() == [0.0, 0.0]


# ---------------------------------------------------------------- 6. sensor scaling
def test_06_scaling_formula():
    raw = np.array([[1.0, 5.0, 9.0]])
    assert scale(raw, 12.5, -12.5).tolist() == [[0.0, 50.0, 100.0]]          # pressure, bar
    assert scale(raw, 100.0, -300.0).tolist() == [[-200.0, 200.0, 600.0]]    # temperature, degC
    assert scale(np.array([3.0]), 2.0, 1.5)[0] == 7.5


def test_06_all_sensor_types_scale_to_plant_value(engine):
    goto(engine, State.HOLD)
    engine.run_for(1.5)
    eng = dict(zip([c["id"] for c in engine.slow_ch], engine.eng_mean))
    p = engine.backend.plant
    assert eng["PT-07"] == pytest.approx(p.pc, abs=0.1)
    assert eng["TT-01"] == pytest.approx(p.t_chamber, abs=1.5)
    assert eng["LC-01"] * 3 == pytest.approx(158.0 * (p.pc - 1.0), rel=0.02)
    assert eng["FT-01"] + eng["FT-02"] == pytest.approx(p.flow_ox + p.flow_fuel, abs=0.01)
    assert eng["ZT-01"] == pytest.approx(100 * p.pos_ox, abs=0.5)


def test_06_editing_calibration_changes_engineering_value(engine):
    engine.run_for(0.2)
    i = engine.slow_id["PT-10"]
    before = engine.eng_mean[i]
    raw = engine.raw_last[i]
    engine.update_slow_channel(i, gain=14.5038 * 12.5, offset=-14.5038 * 12.5, unit="psi")
    engine.run_for(0.1)
    assert engine.eng_mean[i] == pytest.approx(before * 14.5038, rel=0.01)   # bar -> psi
    assert engine.eng_mean[i] == pytest.approx(raw * engine.slow_ch[i]["gain"]
                                               + engine.slow_ch[i]["offset"], rel=0.01)
    with pytest.raises(ValueError):
        engine.update_slow_channel(i, gain=0.0)


def test_06_health_classification(cfg):
    ch = cfg["slow_channels"][:1]
    ch[0].update(warn_lo=10.0, warn_hi=40.0, abort_hi=50.0)
    p = SlowChannelProcessor(ch, 1000.0, stale_time_s=0.05)
    rng = np.random.default_rng(0)
    v = lambda bar: ((bar + 12.5) / 12.5 + 1e-4 * rng.standard_normal((20, 1)))
    assert p.process(v(25))[1][0] == ChStatus.OK
    assert p.process(v(5))[1][0] == ChStatus.WARN_LOW
    assert p.process(v(45))[1][0] == ChStatus.WARN_HIGH
    assert p.process(v(55))[1][0] == ChStatus.ABORT_HIGH
    assert p.process(np.full((20, 1), 0.01) + 1e-4 * rng.standard_normal((20, 1)))[1][0] == ChStatus.OUT_OF_RANGE
    assert p.process(np.full((20, 1), np.nan))[1][0] == ChStatus.INVALID
    assert p.process(np.full((20, 1), np.inf))[1][0] == ChStatus.INVALID
    p.process(np.full((20, 1), 3.0))
    p.process(np.full((20, 1), 3.0))
    assert p.process(np.full((20, 1), 3.0))[1][0] == ChStatus.STALE


# ---------------------------------------------------------------- configuration
def test_config_counts_and_validation(tmp_path):
    cfg = default_config()
    assert [len(cfg[k]) for k in ("slow_channels", "hf_channels", "digital_inputs",
                                  "digital_outputs", "analog_outputs")] == [50, 25, 30, 30, 2]
    assert all(0.0 <= a["min_v"] and a["max_v"] <= 10.0 for a in cfg["analog_outputs"])
    bad = copy.deepcopy(cfg)
    bad["slow_channels"].pop()
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate(bad)
    bad = copy.deepcopy(cfg)
    bad["fft"]["size"] = 5000
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate(bad)
    bad = copy.deepcopy(cfg)
    bad["analog_outputs"][0]["max_v"] = 12.0
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate(bad)
    path = str(tmp_path / "cfg.json")
    cfg["slow_channels"][0]["gain"] = 99.0
    config_mod.save_config(cfg, path)
    assert config_mod.load_config(path)["slow_channels"][0]["gain"] == 99.0
