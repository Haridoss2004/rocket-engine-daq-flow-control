"""Headless scripted demonstration (python main.py --demo).

Runs the real threaded engine in real time, without the GUI:
  run 1  complete nominal sequence  IDLE -> ... -> COMPLETE
  run 2  sequence aborted by an injected pressure-sensor failure, then operator reset
Both runs are logged to Results/<RUN_ID>/ with a test summary.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from Configuration.config import load_config            # noqa: E402
from Core.engine import DaqEngine                        # noqa: E402
from State_Machine.sequencer import State                # noqa: E402


def _wait(e, pred, timeout, label):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        if e.state != last:
            last = e.state
            print(f"  t={e.t:7.2f} s  state -> {last.value}")
        if pred():
            return True
        time.sleep(0.05)
    print(f"  TIMEOUT waiting for {label}")
    return False


def run_demo():
    print("ROCKET ENGINE DAQ - headless demonstration (SIMULATION ONLY, no hardware)\n")
    e = DaqEngine(load_config(), root_dir=ROOT)
    ok = True
    e.start()
    try:
        time.sleep(1.0)
        print("RUN 1: nominal sequence")
        print(" ", e.cmd_start()[1])
        ok &= _wait(e, lambda: e.state == State.READY, 15, "READY")
        for name, passed, detail in e.sequencer.precheck_results:
            print(f"    pre-check {'PASS' if passed else 'FAIL'}  {name}: {detail}")
        print(" ", e.cmd_fire()[1])
        ok &= _wait(e, lambda: e.state == State.HOLD, 15, "HOLD")
        time.sleep(3.0)
        s = e.snapshot()
        r = e.fft_result
        print(f"    HOLD: total flow {s['meas_total']:.3f} kg/s (target {s['target_total']}), "
              f"mixture ratio {s['meas_mr']:.3f} (target {s['target_mr']}), "
              f"valves {s['ao'][0]:.2f} / {s['ao'][1]:.2f} V, controller {s['ctl_status']}")
        if r:
            print(f"    FFT {r['channel']}: dominant {r['peak_freq_hz']:.1f} Hz, amplitude "
                  f"{r['peak_amp']:.3f} {r['unit']}, resolution {r['resolution_hz']:.2f} Hz, "
                  f"Nyquist {r['nyquist_hz']:.0f} Hz")
        ok &= _wait(e, lambda: e.state == State.COMPLETE, 30, "COMPLETE")
        time.sleep(2.5)
        summ1 = e.logger.stop_run()
        print(f"    log: {e.logger.run_dir}")
        print(f"    max pressure {summ1['max_pressure_bar']}, dropped items "
              f"{summ1['logger']['dropped_items']}, HF samples {summ1['logger']['hf_samples']:,}")
        print(" ", e.cmd_reset()[1])

        print("\nRUN 2: abort on pressure-sensor failure")
        e.logger.hf_mode = "fft_channel"                 # keep the second sample log small
        print(" ", e.cmd_start()[1])
        ok &= _wait(e, lambda: e.state == State.READY, 15, "READY")
        e.cmd_fire()
        ok &= _wait(e, lambda: e.state == State.HOLD, 15, "HOLD")
        time.sleep(2.0)
        print(" ", e.inject_fault("slow", e.slow_id["PT-07"], "disconnect")[1], "(fault injected)")
        ok &= _wait(e, lambda: e.state == State.ABORT, 3, "ABORT")
        a = e.last_abort
        print(f"    ABORT reason: {a['reason']}\n    ABORT time:   {a['timestamp']}")
        print(f"    outputs safe: {e.outputs.is_safe()}, locked: {e.outputs.locked}, "
              f"AO = {e.outputs.ao.tolist()} V, controller: {e.controller.status}")
        print("  reset while fault active ->", e.cmd_reset()[1][:60])
        e.inject_fault("clear")
        time.sleep(0.5)
        print("  reset after clearing     ->", e.cmd_reset()[1])
        ok &= e.state == State.IDLE
        time.sleep(1.0)
        summ2 = e.logger.stop_run()
        ok &= bool(summ2 and summ2["aborted"]) and not summ1["aborted"]
        print(f"    log: {e.logger.run_dir}")
        lp = e.loop_stats
        print(f"\nmeasured rates: slow {lp['slow_rate_meas']:.0f} S/s/ch, HF {lp['hf_rate_meas']:.0f} S/s/ch, "
              f"overruns {lp['slow_overruns']}/{lp['hf_overruns']}")
    finally:
        e.stop()
    print("\nDEMO " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_demo())
