"""Off-screen GUI exercise: runs a scripted test with the real threaded engine, clicks the
real buttons and saves one screenshot per tab. Verifies the GUI without needing a display."""
from __future__ import annotations

import os
import time

from PyQt6 import QtWidgets

from State_Machine.sequencer import State


def capture(engine, out_dir):
    from GUI.main_window import MainWindow
    os.makedirs(out_dir, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import tempfile
    engine.logger.results_dir = tempfile.mkdtemp(prefix="rocketdaq_gui_capture_")   # keep Results/ clean
    engine.cfg["sequence"]["hold_duration_s"] = 30.0     # keep HOLD long enough for all captures
    win = MainWindow(engine)
    win.resize(1600, 1000)
    win.show()
    engine.start()

    def pump(seconds, until=None):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)
            if until and until():
                return True
        return until is None

    def shot(tab, name):
        win.tabs.setCurrentIndex(tab)
        pump(0.5)
        win.refresh()
        app.processEvents()
        path = os.path.join(out_dir, name)
        win.grab().save(path)
        print("saved", path)

    log = []
    pump(1.0)
    win.b_start.click()
    log.append(("READY reached via START TEST button", pump(15, lambda: engine.state == State.READY)))
    shot(5, "06_test_sequence_ready.png")
    win.b_fire.click()
    log.append(("HOLD reached via START SEQUENCE button", pump(15, lambda: engine.state == State.HOLD)))
    pump(2.5)
    names = ("01_system_overview.png", "02_analog_acquisition.png", "03_high_frequency.png",
             "04_digital_io.png", "05_flow_control.png", "06_test_sequence_hold.png",
             "07_data_logging.png", "08_configuration.png", "09_fault_injection.png")
    for i, n in enumerate(names):
        shot(i, n)
    engine.inject_fault("overpressure")
    log.append(("ABORT on injected overpressure", pump(5, lambda: engine.state == State.ABORT)))
    shot(0, "10_abort_overview.png")
    shot(5, "11_abort_sequence.png")
    # with the valves shut the injected overpressure no longer exceeds the threshold, so use a
    # persistent sensor failure to show that reset is refused while an abort condition remains
    engine.inject_fault("slow", engine.slow_id["PT-07"], "disconnect")
    pump(0.5)
    win.b_reset.click()
    pump(0.3)
    log.append(("reset rejected while the fault is still active", engine.state == State.ABORT))
    win._clear_faults()
    pump(0.5)
    win.b_reset.click()
    log.append(("reset to IDLE after clearing the fault", pump(3, lambda: engine.state == State.IDLE)))
    pump(2.5)
    win._show_summary()
    shot(6, "12_logging_summary.png")
    # ---- remaining controls: configuration edits, parameters, set-points, FFT, logging, export
    i = engine.slow_id["PT-10"]
    win.cf_table.item(i, 2 + win.cf_cols.index("gain")).setText("25.0")
    win._cf_apply()
    pump(0.3)
    log.append(("channel gain edit applied (tab 8)", engine.slow_ch[i]["gain"] == 25.0
                and abs(engine.eng_mean[i] - (engine.raw_last[i] * 25.0 - 12.5)) < 0.5))
    win.cf_table.item(i, 2 + win.cf_cols.index("gain")).setText("abc")
    win._cf_apply()
    log.append(("invalid gain entry rejected", engine.slow_ch[i]["gain"] == 25.0))
    win.cf_table.item(i, 2 + win.cf_cols.index("gain")).setText("12.5")      # restore calibration
    win._cf_apply()
    r = win._param_keys.index(("sequence", "hold_duration_s"))
    win.cf_params.item(r, 2).setText("4.5")
    r2 = win._param_keys.index(("fft", "size"))
    win.cf_params.item(r2, 2).setText("4096")
    win._params_apply()
    log.append(("parameters applied (hold time, FFT size)",
                engine.cfg["sequence"]["hold_duration_s"] == 4.5 and engine.fft.size == 4096))
    win.fl_total.setValue(1.9)
    win.fl_mr.setValue(2.2)
    win.tabs.setCurrentIndex(4)
    for b in win.tabs.widget(4).findChildren(QtWidgets.QPushButton):
        b.click()
    log.append(("set-points and PID gains applied (tab 5)",
                engine.controller.target_total == 1.9 and engine.controller.target_mr == 2.2))
    win.hf_ch.setCurrentIndex(24)
    win.hf_size.setCurrentText("16384")
    pump(1.5)
    fr = engine.fft_result
    log.append(("FFT channel / size selection (tab 3): HF-25 reads 1 kHz",
                fr is not None and fr["channel"] == "HF-25" and fr["size"] == 16384
                and abs(fr["peak_freq_hz"] - 1000.0) < 2.0 and abs(fr["peak_amp"] - 1.0) < 0.05))
    win.tabs.setCurrentIndex(6)
    btn = {b.text(): b for b in win.tabs.widget(6).findChildren(QtWidgets.QPushButton)}
    btn["Start logging"].click()
    pump(1.0)
    active = engine.logger.active
    btn["Stop logging and write summary"].click()
    log.append(("manual logging start / stop (tab 7)", active and not engine.logger.active
                and "Test summary" in win.lg_summary.toPlainText()))
    btn["Export live trend buffer to CSV"].click()
    exp = os.path.join(engine.logger.results_dir, "exports")
    log.append(("trend export written", os.path.isdir(exp) and len(os.listdir(exp)) == 1))
    win.b_estop.click()
    log.append(("E-STOP button aborts from IDLE", pump(2, lambda: engine.state == State.ABORT)))
    win.b_estop.click()
    pump(0.3)
    win.b_reset.click()
    log.append(("E-STOP release + reset", pump(2, lambda: engine.state == State.IDLE)))
    win.b_engine_stop.click()
    pump(0.3)
    stopped = not engine.snapshot()["running"] and engine.outputs.is_safe()
    win.b_engine_start.click()
    log.append(("STOP / START SIMULATION buttons", stopped and pump(2, lambda: engine.loop_stats["slow_rate_meas"] > 0
                                                                   and engine.snapshot()["running"])))
    win.close()
    import shutil
    shutil.rmtree(engine.logger.results_dir, ignore_errors=True)
    for k, v in log:
        print(("PASS  " if v else "FAIL  ") + k)
    return 0 if all(v for _, v in log) else 1
