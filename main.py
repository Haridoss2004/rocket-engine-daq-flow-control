"""ROCKET ENGINE TEST STAND - DAQ acquisition and flow control SIMULATION. Entry point.

    python main.py                     GUI with the simulation backend (default)
    python main.py --demo              headless scripted demonstration (writes a log + summary)
    python main.py --backend nidaqmx   GUI reading INPUTS from NI-DAQmx *simulated* devices
    python main.py --discover          list NI-DAQmx devices

This is a Python application. It is NOT a LabVIEW project (see README.md).
All digital and analog outputs are virtual; nothing is ever written to hardware.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def build_engine(backend_name, config_path=None):
    from Configuration.config import load_config
    from Core.engine import DaqEngine
    cfg = load_config(config_path)
    backend = None
    if backend_name == "nidaqmx":
        from DAQ_Hardware_Abstraction.nidaqmx_backend import NiDaqmxInputBackend
        backend = NiDaqmxInputBackend(cfg, allow_physical_devices=False)
    return DaqEngine(cfg, root_dir=ROOT, backend=backend)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--backend", choices=["simulation", "nidaqmx"], default="simulation")
    ap.add_argument("--config", default=None, help="path to a configuration JSON")
    ap.add_argument("--demo", action="store_true", help="run the headless scripted demonstration")
    ap.add_argument("--discover", action="store_true", help="list NI-DAQmx devices and exit")
    ap.add_argument("--screenshots", default=None, metavar="DIR",
                    help="run a scripted demo off-screen and save one PNG per GUI tab")
    a = ap.parse_args(argv)

    if a.discover:
        from DAQ_Hardware_Abstraction import nidaqmx_backend as nb
        print("NI-DAQmx driver:", nb.driver_version() or "not available")
        for d in nb.discover_devices():
            print(f"  {d['name']:<12} {d['product_type']:<12} simulated={d['simulated']!s:<5} "
                  f"AI={d['ai']:<3} AO={d['ao']:<2} DI={d['di']:<3} DO={d['do']}")
        return 0
    if a.demo:
        from Tests.run_demo import run_demo
        return run_demo()
    if a.screenshots:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
        from Tests.gui_capture import capture
        return capture(build_engine(a.backend, a.config), a.screenshots)

    from PyQt6 import QtWidgets
    from GUI.main_window import MainWindow
    engine = build_engine(a.backend, a.config)
    app = QtWidgets.QApplication(sys.argv[:1])
    win = MainWindow(engine)
    engine.start()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
