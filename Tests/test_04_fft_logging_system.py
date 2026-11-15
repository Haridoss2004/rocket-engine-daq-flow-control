"""Tests 15-18 + stress: FFT, data logging, graceful shutdown, repeated runs, load tests,
NI-DAQmx input backend (MAX simulated devices), GUI."""
import csv
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np
import pytest
from nptdms import TdmsFile

from Core.engine import DaqEngine
from Signal_Processing.fft import (FftAnalyzer, amplitude_spectrum, find_peak, frequency_resolution,
                                   nyquist, window_function)
from State_Machine.sequencer import State
from Tests.conftest import ROOT, fast_config, goto

FS = 50_000.0


# ---------------------------------------------------------------- 15. FFT
def test_15_nyquist_and_resolution():
    assert nyquist(FS) == 25_000.0
    assert frequency_resolution(FS, 8192) == pytest.approx(6.103515625)
    assert frequency_resolution(FS, 1024) == pytest.approx(48.828125)
    a = FftAnalyzer(FS, 4096)
    assert a.resolution_hz == pytest.approx(FS / 4096) and a.nyquist_hz == 25_000.0


def test_15_hann_window():
    w = window_function("hann", 1024)
    assert w[0] == 0.0 and w.max() == pytest.approx(1.0) and w.sum() == pytest.approx(512.0)


@pytest.mark.parametrize("n", [1024, 4096, 8192, 32768])
def test_15_bin_centred_sine_amplitude_and_frequency(n):
    k = n // 10
    f0 = k * FS / n
    t = np.arange(n) / FS
    f, m = amplitude_spectrum(2.5 * np.sin(2 * np.pi * f0 * t) + 1.0, FS)     # + DC offset
    assert f.size == n // 2 + 1 and f[-1] == pytest.approx(25_000.0) and f[1] == pytest.approx(FS / n)
    pf, pa = find_peak(f, m)
    assert pf == pytest.approx(f0, abs=1e-6)
    assert pa == pytest.approx(2.5, rel=1e-3)


@pytest.mark.parametrize("f0", [777.7, 1234.5, 9999.0, 21003.3])
def test_15_off_bin_sine_interpolated_peak(f0):
    n = 8192
    t = np.arange(n) / FS
    rng = np.random.default_rng(1)
    f, m = amplitude_spectrum(1.0 * np.sin(2 * np.pi * f0 * t) + 0.05 * rng.standard_normal(n), FS)
    pf, pa = find_peak(f, m)
    assert pf == pytest.approx(f0, abs=FS / n / 4)           # better than a quarter of a bin
    assert pa == pytest.approx(1.0, rel=0.08)


def test_15_multi_tone_dominant_and_secondary():
    n = 8192
    t = np.arange(n) / FS
    x = 1.0 * np.sin(2 * np.pi * 1200 * t) + 0.4 * np.sin(2 * np.pi * 5000 * t)
    f, m = amplitude_spectrum(x, FS)
    assert find_peak(f, m)[0] == pytest.approx(1200, abs=2)
    assert find_peak(f, m, f_min=3000)[0] == pytest.approx(5000, abs=2)


def test_15_analyzer_configuration_and_invalid_data():
    a = FftAnalyzer(FS, 2048, averages=4)
    assert a.analyze(np.zeros(100)) is None                  # not enough samples yet
    bad = np.zeros(2048)
    bad[5] = np.nan
    assert a.analyze(bad) is None
    with pytest.raises(ValueError):
        a.configure(size=3000)
    a.configure(size=4096)
    r = a.analyze(np.sin(2 * np.pi * 2000 * np.arange(4096) / FS))
    assert r["size"] == 4096 and r["freqs"].size == 2049 and r["window"] == "hann"


def test_15_engine_live_fft_on_selected_channel(engine):
    goto(engine, State.HOLD)
    engine.run_for(1.0, analysis=True)
    r = engine.fft_result
    assert r["channel"] == "DP-01" and r["peak_freq_hz"] == pytest.approx(1200.0, abs=3.0)
    engine.set_fft("VIB-03", 16384, 1)
    engine.run_for(0.5, analysis=True)
    r = engine.fft_result
    assert r["channel"] == "VIB-03" and r["size"] == 16384
    assert r["resolution_hz"] == pytest.approx(50_000 / 16384)
    assert r["peak_freq_hz"] == pytest.approx(840.0, abs=2.0)
    engine.inject_fault("hf_instability")                    # injected 1850 Hz tone dominates
    engine.run_for(0.5, analysis=True)
    assert engine.fft_result["peak_freq_hz"] == pytest.approx(1850.0, abs=2.0)


def test_15_fft_does_not_run_in_acquisition_path(engine):
    """Acquisition keeps its block count while a slow analysis thread is running."""
    stop = threading.Event()

    def analysis():
        while not stop.is_set():
            engine.analysis_step()
    engine.set_fft(size=32768)
    engine.run_for(1.0)
    th = threading.Thread(target=analysis, daemon=True)
    th.start()
    n0 = engine.n_hf
    engine.run_for(2.0)
    stop.set()
    th.join()
    assert engine.n_hf - n0 == 2.0 * FS


# ---------------------------------------------------------------- 16. data logging
def _full_run(e):
    goto(e, State.COMPLETE, timeout=60)
    e.run_for(2.5)                                           # post-roll, then the log closes itself
    time.sleep(0.05)                                         # let the auto-stop thread start
    return e.logger.run_dir, e.logger.stop_run()             # waits until the files are closed


def test_16_logging_files_and_content(cfg, tmpdir_results):
    cfg["logging"]["hf_log_mode"] = "all"
    e = DaqEngine(cfg, root_dir=tmpdir_results)
    run_dir, summ = _full_run(e)
    t_log = summ["duration_s"]
    assert os.path.basename(run_dir).startswith("RUN_") and summ["run_id"] in run_dir
    assert set(os.listdir(run_dir)) == {"slow_data.csv", "control.csv", "events.csv", "slow_full.tdms",
                                        "hf_raw.tdms", "summary.json", "TEST_SUMMARY.md"}
    # ---- slow CSV: required columns, all 50 channels, timestamps
    with open(os.path.join(run_dir, "slow_data.csv"), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["timestamp", "t_s", "channel_id", "sensor_name", "raw_V",
                             "engineering_value", "unit", "channel_status", "test_state", "alarm_active"]
    assert len({r["channel_id"] for r in rows}) == 50
    assert len(rows) == summ["logger"]["csv_rows"] == pytest.approx(t_log * 10 * 50, rel=0.02)
    r = next(r for r in rows if r["channel_id"] == "PT-07" and r["test_state"] == "HOLD")
    assert float(r["engineering_value"]) == pytest.approx(float(r["raw_V"]) * 12.5 - 12.5, abs=1e-3)
    assert r["unit"] == "bar" and r["channel_status"] == "OK" and "T" in r["timestamp"]
    states = {r["test_state"] for r in rows}
    assert {"PRE-CHECK", "RAMP", "HOLD", "COMPLETE"} <= states   # READY lasts < 1 CSV period here
    # ---- control CSV
    with open(os.path.join(run_dir, "control.csv"), newline="", encoding="utf-8") as f:
        ctl = list(csv.DictReader(f))
    assert [s.value for s in State if s not in (State.ABORT, State.IDLE)] == list(dict.fromkeys(
        c["test_state"] for c in ctl if c["test_state"] != "IDLE"))          # every state, in order
    hold = [c for c in ctl if c["test_state"] == "HOLD"]
    assert float(hold[-1]["meas_total_kg_s"]) == pytest.approx(2.5, abs=0.03)
    assert float(hold[-1]["meas_mixture_ratio"]) == pytest.approx(2.0, abs=0.03)
    assert 0 < float(hold[-1]["ox_valve_cmd_V"]) < 10 and hold[-1]["do_bits_hex"].startswith("0x")
    # ---- events
    with open(os.path.join(run_dir, "events.csv"), newline="", encoding="utf-8") as f:
        ev = list(csv.DictReader(f))
    assert sum(1 for x in ev if x["event_type"] == "STATE") == 7
    # ---- TDMS: full-rate data, every channel
    with TdmsFile.open(os.path.join(run_dir, "slow_full.tdms")) as td:
        assert len(td["Slow_Raw_V"].channels()) == 50 and len(td["Slow_Eng"].channels()) == 50
        ch = td["Slow_Eng"]["PT-07"]
        assert len(ch) == summ["logger"]["slow_samples"] // 50 == pytest.approx(t_log * 1000, rel=0.01)
        assert ch.properties["unit_string"] == "bar" and ch.properties["wf_increment"] == 0.001
        assert ch[:].max() == pytest.approx(summ["max_pressure_bar"]["value"], abs=25)
    with TdmsFile.open(os.path.join(run_dir, "hf_raw.tdms")) as td:
        chans = td["HF_Raw_V"].channels()
        assert len(chans) == 25
        n = len(chans[0])
        assert all(len(c) == n for c in chans)
        assert n == summ["logger"]["hf_samples"] // 25 == pytest.approx(t_log * 50_000, rel=0.02)
        assert chans[0].properties["wf_increment"] == pytest.approx(1 / 50_000)
    # ---- summary report
    for key in ("log_start", "log_end", "duration_s", "max_pressure_bar", "max_temperature_degC",
                "total_flow_kg_s_hold", "mixture_ratio_hold", "aborted", "alarms"):
        assert key in summ
    assert summ["aborted"] is False and summ["logger"]["dropped_items"] == 0
    assert summ["total_flow_kg_s_hold"]["mean"] == pytest.approx(2.5, abs=0.05)
    assert summ["mixture_ratio_hold"]["mean"] == pytest.approx(2.0, abs=0.02)
    assert summ["max_temperature_degC"]["value"] > 100
    assert json.load(open(os.path.join(run_dir, "summary.json")))["run_id"] == summ["run_id"]
    assert "Maximum recorded pressure" in open(os.path.join(run_dir, "TEST_SUMMARY.md")).read()
    e.stop()


def test_16_abort_is_recorded_and_log_preserved(engine):
    goto(engine, State.HOLD)
    engine.inject_fault("overpressure")
    engine.run_until(lambda: engine.state == State.ABORT, 2.0)
    engine.run_for(2.5)
    time.sleep(0.05)
    summ = engine.logger.stop_run()
    assert summ["aborted"] and "OVERPRESSURE" in summ["abort_events"][0]["reason"]
    assert summ["abort_events"][0]["timestamp"]
    assert summ["max_pressure_bar"]["value"] > 28.0
    with open(os.path.join(engine.logger.run_dir, "control.csv"), newline="") as f:
        ctl = list(csv.DictReader(f))
    assert ctl[-1]["test_state"] == "ABORT" and ctl[-1]["abort_latched"] == "1"
    assert float(ctl[-1]["ox_valve_cmd_V"]) == 0.0
    assert "ABORTED" in open(os.path.join(engine.logger.run_dir, "TEST_SUMMARY.md")).read()


def test_16_logger_queue_is_bounded_and_never_blocks(cfg, tmpdir_results):
    cfg["logging"]["queue_max_items"] = 5
    e = DaqEngine(cfg, root_dir=tmpdir_results)
    e.logger.active = True                                   # accept data but no writer thread
    t0 = time.perf_counter()
    e.run_for(1.0)
    assert time.perf_counter() - t0 < 5.0                    # producers were never blocked
    assert e.logger.q.qsize() == 5 and e.logger.stats["dropped_items"] > 0
    e.logger.active = False
    e.stop()


def test_16_manual_logging_start_stop(engine):
    engine.run_for(0.5)
    rid = engine.start_logging()
    engine.run_for(1.0)
    summ = engine.stop_logging()
    assert summ["run_id"] == rid and summ["duration_s"] == pytest.approx(1.0, abs=0.05)
    assert summ["logger"]["slow_samples"] == 50 * 1000


# ---------------------------------------------------------------- 17. graceful shutdown
def test_17_graceful_shutdown_threaded(cfg, tmpdir_results):
    e = DaqEngine(cfg, root_dir=tmpdir_results)
    before = threading.active_count()
    e.start()
    e.cmd_start()
    deadline = time.time() + 15
    while e.state != State.READY and time.time() < deadline:
        time.sleep(0.05)
    assert e.state == State.READY
    e.cmd_fire()
    time.sleep(3.5)
    assert e.state in (State.RAMP, State.HOLD) and e.outputs.ao.max() > 0.5
    e.stop()                                                 # stop in the middle of a firing
    assert threading.active_count() == before
    assert e.outputs.is_safe() and e.outputs.ao.tolist() == [0.0, 0.0]
    assert not e.logger.active and e.logger.healthy and e.logger.last_error is None
    assert not e.backend.is_ready()
    run_dir = e.logger.run_dir
    assert os.path.exists(os.path.join(run_dir, "summary.json"))
    with TdmsFile.open(os.path.join(run_dir, "slow_full.tdms")) as td:      # file closed + readable
        assert len(td["Slow_Eng"]["PT-01"]) > 1000
    os.rename(run_dir, run_dir + "_moved")                   # no file handle left open
    e.stop()                                                 # idempotent


def test_17_stop_start_cycle(cfg, tmpdir_results):
    e = DaqEngine(cfg, root_dir=tmpdir_results)
    for _ in range(3):
        e.start()
        time.sleep(0.6)
        assert e.state == State.IDLE
        e.stop(close_backend=False)
    assert e.state == State.IDLE and e.loop_stats["slow_blocks"] > 60
    e.stop()


# ---------------------------------------------------------------- 18. repeated runs
def test_18_repeated_test_runs(engine):
    ids = []
    for i in range(4):
        run_dir, summ = _full_run(engine)
        assert summ["aborted"] is False
        assert summ["total_flow_kg_s_hold"]["mean"] == pytest.approx(2.5, abs=0.05)
        ids.append(summ["run_id"])
        assert engine.cmd_reset()[0] and engine.state == State.IDLE
    assert len(set(ids)) == 4 and engine.sequencer.runs_completed == 4


def test_18_run_abort_reset_run(engine):
    goto(engine, State.HOLD)
    engine.inject_fault("slow", engine.slow_id["PT-07"], "nan")
    engine.run_for(0.1)
    assert engine.state == State.ABORT
    engine.inject_fault("clear")
    engine.run_for(3.0)
    assert engine.cmd_reset()[0]
    time.sleep(0.3)
    run_dir, summ = _full_run(engine)
    assert summ["aborted"] is False and engine.state == State.COMPLETE


# ---------------------------------------------------------------- stress
def test_stress_realtime_acquisition_with_full_logging(tmpdir_results):
    """Threaded, real-time: 50 kS/s slow aggregate + 1.25 MS/s HF aggregate, all logged."""
    cfg = fast_config(tmpdir_results)
    cfg["logging"]["hf_log_mode"] = "all"
    e = DaqEngine(cfg, root_dir=tmpdir_results)
    e.start()
    e.start_logging()
    t0 = time.perf_counter()
    time.sleep(10.0)
    wall = time.perf_counter() - t0
    n_slow, n_hf = e.n_slow, e.n_hf
    stats, loops = dict(e.logger.stats), dict(e.loop_stats)
    qdepth = e.logger.q.qsize()
    e.stop()
    print(f"\n[stress] wall {wall:.2f} s  slow {n_slow / wall:.1f} S/s/ch  hf {n_hf / wall:.0f} S/s/ch  "
          f"slow proc max {loops['slow_proc_ms_max']:.2f} ms  hf proc max {loops['hf_proc_ms_max']:.2f} ms  "
          f"queue hw {stats['queue_high_water']}  dropped {stats['dropped_items']}  "
          f"overruns {loops['slow_overruns']}/{loops['hf_overruns']}")
    assert n_slow / wall == pytest.approx(1000.0, rel=0.03)
    assert n_hf / wall == pytest.approx(50_000.0, rel=0.03)
    assert stats["dropped_items"] == 0
    assert stats["queue_high_water"] < cfg["logging"]["queue_max_items"] // 4 and qdepth < 50
    assert loops["slow_overruns"] == 0 and loops["hf_overruns"] == 0
    summ = e.logger.last_summary
    assert summ["logger"]["hf_samples"] == pytest.approx(wall * 1_250_000, rel=0.05)
    assert e.logger.healthy


def test_stress_long_simulated_run_memory_is_bounded(cfg, tmpdir_results):
    import tracemalloc
    cfg["logging"]["auto_start_on_precheck"] = False
    e = DaqEngine(cfg, root_dir=tmpdir_results)
    e.run_for(20.0, analysis=True)
    tracemalloc.start()
    a = tracemalloc.take_snapshot()
    e.run_for(120.0, analysis=True)                          # 2 simulated minutes
    b = tracemalloc.take_snapshot()
    tracemalloc.stop()
    growth = sum(s.size_diff for s in b.compare_to(a, "filename"))
    print(f"\n[stress] heap growth over 120 simulated s: {growth / 1024:.0f} kB")
    assert growth < 5e6
    assert len(e.events) <= 500 and e.n_hf == 140 * 50_000
    e.stop()


# ---------------------------------------------------------------- NI-DAQmx (inputs only)
def _sim_devices():
    try:
        from DAQ_Hardware_Abstraction import nidaqmx_backend as nb
        return [d for d in nb.discover_devices() if d["simulated"]]
    except Exception:
        return []


needs_daqmx = pytest.mark.skipif(len(_sim_devices()) < 3,
                                 reason="NI-DAQmx simulated devices not configured in NI MAX")


@needs_daqmx
def test_nidaqmx_input_backend_on_max_simulated_devices(cfg, tmpdir_results):
    from DAQ_Hardware_Abstraction import nidaqmx_backend as nb
    from DAQ_Hardware_Abstraction.backend_base import PhysicalOutputDisabledError
    b = nb.NiDaqmxInputBackend(cfg)
    e = DaqEngine(cfg, root_dir=tmpdir_results, backend=b)
    try:
        assert b.all_simulated and b.actual_slow_rate == 1000.0
        assert e.fs_hf == b.actual_hf_rate >= 50_000.0       # module may coerce to 51.2 kS/s
        e.run_for(1.0, analysis=True)
        assert e.raw_last.shape == (50,) and np.isfinite(e.raw_last).all()
        assert e.hf_ring.total >= 25_000 and e.di.shape == (30,)
        assert e.fft_result is not None and e.fft_result["nyquist_hz"] == e.fs_hf / 2
        assert not any(t.name.lower().find("ao") >= 0 for t in b._tasks)
        with pytest.raises(PhysicalOutputDisabledError):
            b.write_physical_outputs([1], [5.0])
        assert e.inject_fault("overpressure")[0] is False    # fault injection is simulation-only
    finally:
        e.stop()
    assert b._tasks == [] and not b.is_ready()               # tasks cleared


@needs_daqmx
def test_nidaqmx_backend_refuses_physical_devices(cfg, monkeypatch):
    from DAQ_Hardware_Abstraction import nidaqmx_backend as nb
    from DAQ_Hardware_Abstraction.backend_base import DaqError
    real = nb.discover_devices()
    b = nb.NiDaqmxInputBackend(cfg)
    monkeypatch.setattr(nb, "discover_devices", lambda: [dict(d, simulated=False) for d in real])
    with pytest.raises(DaqError, match="Refusing to open PHYSICAL devices"):
        b.open()
    assert b._tasks == []


def test_default_backend_is_simulation(engine):
    assert engine.cfg["acquisition"]["backend"] == "simulation"
    assert engine.backend.is_simulation and engine.backend.name == "simulation"


# ---------------------------------------------------------------- GUI (off-screen, real threads)
def test_gui_offscreen_scripted_demo(tmpdir_results):
    out = os.path.join(tmpdir_results, "shots")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), "--screenshots", out],
                       capture_output=True, text=True, timeout=180, env=env, cwd=tmpdir_results)
    print(r.stdout[-1500:], r.stderr[-1500:])
    assert r.returncode == 0
    assert r.stdout.count("PASS") == 15 and "FAIL" not in r.stdout
    assert len([f for f in os.listdir(out) if f.endswith(".png")]) == 13
