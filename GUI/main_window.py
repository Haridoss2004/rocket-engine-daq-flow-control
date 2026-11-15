"""Operator GUI (PyQt6 + pyqtgraph). Dark industrial dashboard, 8 tabs + fault injection.

The GUI runs on its own timer and only reads engine.snapshot(); it never sits in
the acquisition path. Every control calls an engine command.
"""
from __future__ import annotations

import csv
import math
import os
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets as W

from Acquisition.scaling import ChStatus
from Core.engine import CTL_TREND
from Signal_Processing.fft import frequency_resolution, nyquist
from State_Machine.sequencer import State

BG, PANEL, EDGE, TXT, DIM = "#12161c", "#1b2129", "#2e3742", "#e6edf3", "#8b98a5"
GREEN, AMBER, RED, BLUE, CYAN, VIOLET = "#2ecc71", "#f5b041", "#ff4d4f", "#4aa3ff", "#35c9c3", "#b08cff"
STATE_COLOR = {State.IDLE: DIM, State.PRECHECK: BLUE, State.READY: CYAN, State.START: AMBER,
               State.RAMP: AMBER, State.HOLD: GREEN, State.SHUTDOWN: VIOLET, State.COMPLETE: GREEN,
               State.ABORT: RED}
STATUS_COLOR = {ChStatus.OK: GREEN, ChStatus.WARN_LOW: AMBER, ChStatus.WARN_HIGH: AMBER,
                ChStatus.ABORT_HIGH: RED, ChStatus.OUT_OF_RANGE: RED, ChStatus.INVALID: RED,
                ChStatus.STALE: RED, ChStatus.DISABLED: DIM}
QSS = f"""
* {{ font-family: 'Segoe UI'; font-size: 10pt; color: {TXT}; }}
QMainWindow, QWidget {{ background: {BG}; }}
QGroupBox {{ background: {PANEL}; border: 1px solid {EDGE}; border-radius: 6px; margin-top: 14px;
            padding: 8px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {DIM}; }}
QGroupBox QWidget {{ background: {PANEL}; }}
QTabWidget::pane {{ border: 1px solid {EDGE}; }}
QTabBar::tab {{ background: {PANEL}; padding: 8px 14px; border: 1px solid {EDGE}; border-bottom: none;
               color: {DIM}; font-weight: 600; }}
QTabBar::tab:selected {{ background: #263040; color: {TXT}; border-top: 2px solid {BLUE}; }}
QPushButton {{ background: #2a3340; border: 1px solid #3b4756; border-radius: 4px; padding: 7px 12px;
              font-weight: 600; }}
QPushButton:hover {{ background: #344050; }}
QPushButton:disabled {{ color: #5d6873; background: #1f252d; }}
QPushButton#go {{ background: #1e6b3c; border-color: #2ecc71; }}
QPushButton#warn {{ background: #7a5a12; border-color: #f5b041; }}
QPushButton#danger {{ background: #8a1f21; border-color: #ff4d4f; }}
QPushButton#estop {{ background: #b3171a; border: 3px solid #ffd21f; border-radius: 10px;
                    font-size: 14pt; padding: 14px; }}
QPushButton#estop:checked {{ background: #ff2a2e; color: #000; }}
QTableWidget, QListWidget, QPlainTextEdit {{ background: #0e1217; gridline-color: {EDGE};
    border: 1px solid {EDGE}; alternate-background-color: #141a21; }}
QHeaderView::section {{ background: #232b35; padding: 4px; border: 1px solid {EDGE}; font-weight: 600; }}
QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit {{ background: #0e1217; border: 1px solid #3b4756;
    border-radius: 3px; padding: 4px; }}
QComboBox QAbstractItemView {{ background: #0e1217; selection-background-color: #2f4a6b; }}
QProgressBar {{ background: #0e1217; border: 1px solid {EDGE}; border-radius: 3px; text-align: center; }}
QProgressBar::chunk {{ background: {BLUE}; }}
QLabel#big {{ font-size: 22pt; font-weight: 700; }}
QLabel#cap {{ color: {DIM}; font-size: 9pt; }}
"""
pg.setConfigOptions(antialias=False, background="#0e1217", foreground=DIM)


def fmt(v, nd=3):
    return "---" if v is None or not math.isfinite(v) else f"{v:.{nd}f}"


def led(text=""):
    lab = W.QLabel(text)
    lab.setMinimumHeight(24)
    return lab


def set_led(lab, on, on_color=GREEN, off_color="#39424d", text=None):
    c = on_color if on else off_color
    lab.setStyleSheet(f"background:{c}; color:{'#06110a' if on else TXT}; border-radius:4px; "
                      f"padding:2px 8px; font-weight:600;")
    if text is not None:
        lab.setText(text)


def group(title, layout):
    g = W.QGroupBox(title)
    g.setLayout(layout)
    return g


def big_value(caption):
    box = W.QVBoxLayout()
    cap = W.QLabel(caption)
    cap.setObjectName("cap")
    val = W.QLabel("---")
    val.setObjectName("big")
    box.addWidget(cap)
    box.addWidget(val)
    return box, val


def make_table(headers, rows, stretch_col=None):
    t = W.QTableWidget(rows, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(22)
    t.setAlternatingRowColors(True)
    t.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
    h = t.horizontalHeader()
    h.setSectionResizeMode(W.QHeaderView.ResizeMode.ResizeToContents)
    if stretch_col is not None:
        h.setSectionResizeMode(stretch_col, W.QHeaderView.ResizeMode.Stretch)
    return t


def cell(table, r, c, text, color=None, editable=False):
    it = table.item(r, c)
    if it is None:
        it = W.QTableWidgetItem()
        if not editable:
            it.setFlags(it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        table.setItem(r, c, it)
    if it.text() != text:
        it.setText(text)
    if color is not None:
        it.setForeground(QtGui.QColor(color))
    return it


def plot(title, ylabel, xlabel="time (s)"):
    p = pg.PlotWidget()
    p.setTitle(title, color=TXT, size="10pt")
    p.setLabel("left", ylabel)
    p.setLabel("bottom", xlabel)
    p.showGrid(x=True, y=True, alpha=0.25)
    p.addLegend(offset=(8, 8), labelTextSize="8pt")
    return p


class MainWindow(W.QMainWindow):
    def __init__(self, engine):
        super().__init__()
        self.e = engine
        self.cfg = engine.cfg
        self.setWindowTitle("Rocket Engine Test Stand - DAQ & Flow Control  [SIMULATION ONLY]")
        self.resize(1500, 950)
        self.setStyleSheet(QSS)
        central = W.QWidget()
        root = W.QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.addLayout(self._header())
        self.abort_banner = W.QLabel("")
        self.abort_banner.setWordWrap(True)
        self.abort_banner.setStyleSheet(f"background:{RED}; color:#fff; font-size:13pt; font-weight:700; "
                                        "padding:10px; border-radius:6px;")
        self.abort_banner.hide()
        root.addWidget(self.abort_banner)
        self.tabs = W.QTabWidget()
        for name, builder in (("1  SYSTEM OVERVIEW", self._tab_overview),
                              ("2  ANALOG ACQUISITION", self._tab_analog),
                              ("3  HIGH-FREQUENCY", self._tab_hf), ("4  DIGITAL I/O", self._tab_dio),
                              ("5  FLOW CONTROL", self._tab_flow), ("6  TEST SEQUENCE", self._tab_sequence),
                              ("7  DATA LOGGING", self._tab_logging), ("8  CONFIGURATION", self._tab_config),
                              ("FAULT INJECTION", self._tab_faults)):
            self.tabs.addTab(builder(), name)
        root.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.status = self.statusBar()
        self._n_events = -1
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(int(1000 / self.cfg["gui"]["refresh_hz"]))
        self.refresh()

    # ------------------------------------------------------------------ header
    def _header(self):
        h = W.QHBoxLayout()
        title = W.QLabel("ROCKET ENGINE TEST STAND  |  DAQ ACQUISITION & FLOW CONTROL")
        title.setStyleSheet("font-size:14pt; font-weight:700;")
        sim = W.QLabel("SIMULATION ONLY - NO REAL HARDWARE IS CONTROLLED")
        sim.setStyleSheet(f"background:{AMBER}; color:#1a1300; font-weight:700; padding:5px 12px; "
                          "border-radius:4px;")
        self.h_state = W.QLabel("IDLE")
        self.h_time = W.QLabel("t = 0.0 s")
        self.h_time.setStyleSheet(f"color:{DIM}; font-size:11pt;")
        self.h_alarm = led("NO ALARMS")
        h.addWidget(title)
        h.addStretch(1)
        h.addWidget(sim)
        h.addSpacing(12)
        h.addWidget(self.h_time)
        h.addWidget(self.h_state)
        h.addWidget(self.h_alarm)
        return h

    def _msg(self, result):
        ok, text = result if isinstance(result, tuple) else (True, str(result))
        self.status.showMessage(("OK: " if ok else "REJECTED: ") + str(text), 8000)

    # ------------------------------------------------------------------ TAB 1
    def _tab_overview(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        st = W.QGridLayout()
        self.ov = {}
        for r, (key, cap) in enumerate((("system", "System status"), ("backend", "Acquisition backend"),
                                        ("engine", "Simulation engine"), ("state", "Current test state"),
                                        ("ctl", "Flow controller"), ("log", "Data logger"),
                                        ("rates", "Measured sample rates"), ("outputs", "Virtual outputs"))):
            st.addWidget(W.QLabel(cap), r, 0)
            self.ov[key] = led("---")
            st.addWidget(self.ov[key], r, 1)
        st.setColumnStretch(1, 1)
        g.addWidget(group("STATUS", st), 0, 0)

        ctl = W.QGridLayout()
        self.b_engine_start = W.QPushButton("START SIMULATION")
        self.b_engine_stop = W.QPushButton("STOP SIMULATION")
        self.b_start = W.QPushButton("1. START TEST (PRE-CHECK)")
        self.b_start.setObjectName("go")
        self.b_fire = W.QPushButton("2. START SEQUENCE (SIMULATED)")
        self.b_fire.setObjectName("warn")
        self.b_stop = W.QPushButton("STOP (controlled shutdown)")
        self.b_abort = W.QPushButton("MANUAL ABORT")
        self.b_abort.setObjectName("danger")
        self.b_reset = W.QPushButton("OPERATOR RESET")
        self.b_estop = W.QPushButton("EMERGENCY STOP\n(simulated)")
        self.b_estop.setObjectName("estop")
        self.b_estop.setCheckable(True)
        self.b_engine_start.clicked.connect(lambda: (self.e.start(), self._msg("simulation started")))
        self.b_engine_stop.clicked.connect(lambda: (self.e.stop(close_backend=False),
                                                    self._msg("simulation stopped, outputs safe")))
        self.b_start.clicked.connect(lambda: self._msg(self.e.cmd_start()))
        self.b_fire.clicked.connect(lambda: self._msg(self.e.cmd_fire()))
        self.b_stop.clicked.connect(lambda: self._msg(self.e.cmd_stop()))
        self.b_abort.clicked.connect(lambda: self._msg(self.e.cmd_abort()))
        self.b_reset.clicked.connect(lambda: self._msg(self.e.cmd_reset()))
        self.b_estop.toggled.connect(lambda on: self.e.set_estop(on))
        for i, b in enumerate((self.b_engine_start, self.b_engine_stop, self.b_start, self.b_fire,
                               self.b_stop, self.b_abort, self.b_reset)):
            ctl.addWidget(b, i // 2, i % 2)
        ctl.addWidget(self.b_estop, 4, 0, 1, 2)
        g.addWidget(group("CONTROLS (all simulated)", ctl), 0, 1)

        vals = W.QGridLayout()
        self.ov_big = {}
        for i, (key, cap) in enumerate((("total", "TOTAL FLOW  kg/s  (simulated)"),
                                        ("mr", "MIXTURE RATIO  O/F  (simulated)"),
                                        ("pc", "CHAMBER PRESSURE  bar"), ("thrust", "THRUST  N"),
                                        ("ox", "OX VALVE CMD  V"), ("fuel", "FUEL VALVE CMD  V"))):
            box, lab = big_value(cap)
            self.ov_big[key] = lab
            vals.addLayout(box, i // 2, i % 2)
        g.addWidget(group("KEY MEASUREMENTS", vals), 0, 2)

        al = W.QVBoxLayout()
        self.alarm_list = W.QListWidget()
        al.addWidget(self.alarm_list)
        g.addWidget(group("ACTIVE ALARMS", al), 1, 0)
        self.ov_plot = plot("Chamber pressure and total flow", "bar  |  kg/s x10")
        self.ov_c_pc = self.ov_plot.plot(pen=pg.mkPen(AMBER, width=2), name="Chamber pressure (bar)")
        self.ov_c_fl = self.ov_plot.plot(pen=pg.mkPen(CYAN, width=2), name="Total flow x10 (kg/s)")
        g.addWidget(self.ov_plot, 1, 1, 1, 2)
        g.setRowStretch(1, 1)
        for c in range(3):
            g.setColumnStretch(c, 1)
        return w

    # ------------------------------------------------------------------ TAB 2
    def _tab_analog(self):
        w = W.QWidget()
        lay = W.QHBoxLayout(w)
        ch = self.cfg["slow_channels"]
        self.an_table = make_table(["ID", "Sensor name", "Type", "Raw (V)", "Engineering", "Unit",
                                    "Health", "Gain", "Offset"], len(ch), 1)
        for r, c in enumerate(ch):
            cell(self.an_table, r, 0, c["id"])
        self.an_table.itemSelectionChanged.connect(self._an_select)
        left = W.QVBoxLayout()
        left.addWidget(W.QLabel("50 slow analog inputs @ 1 kS/s per channel.  Engineering = Raw x Gain + Offset.  "
                                "Edit scaling on the CONFIGURATION tab.  Select rows to trend them."))
        left.addWidget(self.an_table)
        lay.addLayout(left, 3)
        self.an_plot = plot("Trend of selected channels", "engineering units")
        self.an_curves = {}
        lay.addWidget(self.an_plot, 2)
        self.an_table.selectRow(6)
        return w

    def _an_select(self):
        rows = sorted({i.row() for i in self.an_table.selectedIndexes()})[:6]
        for c in self.an_curves.values():
            self.an_plot.removeItem(c)
        self.an_curves = {}
        colors = [AMBER, CYAN, GREEN, VIOLET, BLUE, RED]
        for k, r in enumerate(rows):
            c = self.cfg["slow_channels"][r]
            self.an_curves[r] = self.an_plot.plot(pen=pg.mkPen(colors[k], width=2),
                                                  name=f"{c['id']} ({c['unit']})")

    # ------------------------------------------------------------------ TAB 3
    def _tab_hf(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        top = W.QHBoxLayout()
        self.hf_ch = W.QComboBox()
        self.hf_ch.addItems([f"{c['id']}  {c['name']}" for c in self.cfg["hf_channels"]])
        self.hf_ch.setCurrentIndex(self.e.fft_channel)
        self.hf_size = W.QComboBox()
        self.hf_size.addItems([str(s) for s in self.cfg["fft"]["allowed_sizes"]])
        self.hf_size.setCurrentText(str(self.cfg["fft"]["size"]))
        self.hf_avg = W.QSpinBox()
        self.hf_avg.setRange(1, 64)
        self.hf_avg.setValue(self.cfg["fft"]["averages"])
        self.hf_log = W.QCheckBox("log magnitude")
        self.hf_log.setChecked(True)
        self.hf_ch.currentIndexChanged.connect(self._hf_apply)
        self.hf_size.currentIndexChanged.connect(self._hf_apply)
        self.hf_avg.valueChanged.connect(self._hf_apply)
        self.hf_log.toggled.connect(lambda on: self.hf_spec.setLogMode(False, on))
        b_tr = W.QPushButton("Inject transient burst")
        b_in = W.QPushButton("Toggle instability tone")
        b_tr.clicked.connect(lambda: self._msg(self.e.inject_fault("hf_transient")))
        b_in.clicked.connect(lambda: self._msg(self.e.inject_fault("hf_instability")))
        for lab, wid in (("FFT / waveform channel", self.hf_ch), ("FFT size", self.hf_size),
                         ("Averages", self.hf_avg)):
            top.addWidget(W.QLabel(lab))
            top.addWidget(wid)
        top.addWidget(W.QLabel("Window: Hann"))
        top.addWidget(self.hf_log)
        top.addStretch(1)
        top.addWidget(b_tr)
        top.addWidget(b_in)
        g.addLayout(top, 0, 0, 1, 2)
        self.hf_wave = plot("Live waveform (last 20 ms)", "engineering units", "time (ms)")
        self.hf_wave_c = self.hf_wave.plot(pen=pg.mkPen(CYAN, width=1))
        self.hf_spec = plot("FFT magnitude spectrum (Hann window)", "amplitude (peak)", "frequency (Hz)")
        self.hf_spec_c = self.hf_spec.plot(pen=pg.mkPen(AMBER, width=1))
        self.hf_spec.setLogMode(False, True)
        self.hf_peak = pg.InfiniteLine(angle=90, pen=pg.mkPen(RED, width=1, style=QtCore.Qt.PenStyle.DashLine))
        self.hf_spec.addItem(self.hf_peak)
        g.addWidget(self.hf_wave, 1, 0)
        g.addWidget(self.hf_spec, 2, 0)
        info = W.QGridLayout()
        self.hf_info = {}
        for r, (key, cap) in enumerate((("peak", "DOMINANT FREQUENCY"), ("amp", "Peak amplitude"),
                                        ("res", "Frequency resolution = fs / N"),
                                        ("nyq", "Nyquist frequency = fs / 2"), ("fs", "Sampling rate"),
                                        ("rms", "RMS (AC)"), ("pkpk", "Peak-to-peak"),
                                        ("crest", "Crest factor"), ("mean", "Mean"))):
            info.addWidget(W.QLabel(cap), r, 0)
            v = W.QLabel("---")
            v.setStyleSheet("font-weight:700;" + ("font-size:16pt; color:%s;" % AMBER if key == "peak" else ""))
            self.hf_info[key] = v
            info.addWidget(v, r, 1)
        right = W.QVBoxLayout()
        right.addWidget(group("SPECTRUM / SIGNAL STATISTICS", info))
        self.hf_table = make_table(["ID", "Name", "RMS", "Pk-Pk", "Unit", "Health"],
                                   len(self.cfg["hf_channels"]), 1)
        for r, c in enumerate(self.cfg["hf_channels"]):
            cell(self.hf_table, r, 0, c["id"])
            cell(self.hf_table, r, 1, c["name"])
            cell(self.hf_table, r, 4, c["unit"])
        right.addWidget(group("25 HIGH-FREQUENCY CHANNELS @ 50 kS/s", self._wrap(self.hf_table)), 1)
        g.addLayout(right, 1, 1, 2, 1)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 2)
        return w

    @staticmethod
    def _wrap(widget):
        lay = W.QVBoxLayout()
        lay.addWidget(widget)
        return lay

    def _hf_apply(self, *_):
        cid = self.cfg["hf_channels"][self.hf_ch.currentIndex()]["id"]
        self.e.set_fft(cid, int(self.hf_size.currentText()), self.hf_avg.value())

    # ------------------------------------------------------------------ TAB 4
    def _tab_dio(self):
        w = W.QWidget()
        lay = W.QHBoxLayout(w)
        self.di_leds, self.do_leds = [], []
        for title, key, store in (("30 DIGITAL INPUTS", "digital_inputs", self.di_leds),
                                  ("30 VIRTUAL DIGITAL OUTPUTS (never drive hardware)", "digital_outputs",
                                   self.do_leds)):
            grid = W.QGridLayout()
            for i, d in enumerate(self.cfg[key]):
                lab = led()
                store.append(lab)
                r, c = i % 15, (i // 15) * 3
                idl = W.QLabel(d["id"])
                idl.setStyleSheet(f"color:{DIM};")
                grid.addWidget(idl, r, c)
                grid.addWidget(W.QLabel(d["name"]), r, c + 1)
                grid.addWidget(lab, r, c + 2)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(4, 1)
            lay.addWidget(group(title, grid))
        return w

    # ------------------------------------------------------------------ TAB 5
    def _tab_flow(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        fc = self.cfg["flow_control"]
        sp = W.QGridLayout()
        self.fl_total = W.QDoubleSpinBox()
        self.fl_total.setRange(0.0, 10.0)
        self.fl_total.setDecimals(3)
        self.fl_total.setSingleStep(0.1)
        self.fl_total.setValue(fc["target_total_flow_kg_s"])
        self.fl_mr = W.QDoubleSpinBox()
        self.fl_mr.setRange(0.2, 10.0)
        self.fl_mr.setDecimals(3)
        self.fl_mr.setSingleStep(0.1)
        self.fl_mr.setValue(fc["target_mixture_ratio"])
        b = W.QPushButton("Apply set-points")
        b.clicked.connect(lambda: (self.e.set_targets(self.fl_total.value(), self.fl_mr.value()),
                                   self._msg("set-points applied")))
        sp.addWidget(W.QLabel("Target total flow (kg/s)"), 0, 0)
        sp.addWidget(self.fl_total, 0, 1)
        sp.addWidget(W.QLabel("Target mixture ratio (O/F)"), 1, 0)
        sp.addWidget(self.fl_mr, 1, 1)
        sp.addWidget(b, 2, 0, 1, 2)
        self.fl_gain = {}
        for r, which in enumerate(("ox", "fuel")):
            sp.addWidget(W.QLabel(f"PID {which}:  Kp / Ki / Kd"), 3 + r, 0)
            row = W.QHBoxLayout()
            for k in ("kp", "ki", "kd"):
                s = W.QDoubleSpinBox()
                s.setRange(0, 500)
                s.setDecimals(3)
                s.setValue(fc[f"pid_{which}"][k])
                self.fl_gain[(which, k)] = s
                row.addWidget(s)
            sp.addLayout(row, 3 + r, 1)
        b2 = W.QPushButton("Apply PID gains")
        b2.clicked.connect(self._apply_gains)
        sp.addWidget(b2, 5, 0, 1, 2)
        note = W.QLabel("Demonstration set-points only. Flow feedback is SIMULATED (FT-01 / FT-02).\n"
                        "The controller acts during START / RAMP / HOLD of the test sequence.")
        note.setStyleSheet(f"color:{AMBER};")
        note.setWordWrap(True)
        sp.addWidget(note, 6, 0, 1, 2)
        g.addWidget(group("SET-POINTS AND TUNING", sp), 0, 0)

        rd = W.QGridLayout()
        self.fl = {}
        rows = (("status", "Controller status"), ("sp_total", "Target total flow (active set-point)"),
                ("meas_total", "Measured total flow (simulated)"), ("err_total", "Total flow error"),
                ("target_mr", "Target mixture ratio"), ("meas_mr", "Measured mixture ratio (simulated)"),
                ("err_mr", "Mixture ratio error"), ("ox_flow", "Oxidizer flow  FT-01"),
                ("fuel_flow", "Fuel flow  FT-02"))
        for r, (k, cap) in enumerate(rows):
            rd.addWidget(W.QLabel(cap), r, 0)
            v = W.QLabel("---")
            v.setStyleSheet("font-weight:700; font-size:11pt;")
            self.fl[k] = v
            rd.addWidget(v, r, 1)
        g.addWidget(group("FEEDBACK", rd), 0, 1)

        vo = W.QGridLayout()
        self.fl_bar, self.fl_v, self.fl_sat = [], [], []
        for r, name in enumerate(("AO-00  OXIDIZER valve command (virtual)", "AO-01  FUEL valve command (virtual)")):
            vo.addWidget(W.QLabel(name), r * 2, 0, 1, 3)
            bar = W.QProgressBar()
            bar.setRange(0, 1000)
            bar.setFormat("%p %  open")
            v = W.QLabel("0.000 V")
            v.setStyleSheet("font-weight:700; font-size:12pt;")
            s = led("IN RANGE")
            self.fl_bar.append(bar)
            self.fl_v.append(v)
            self.fl_sat.append(s)
            vo.addWidget(bar, r * 2 + 1, 0)
            vo.addWidget(v, r * 2 + 1, 1)
            vo.addWidget(s, r * 2 + 1, 2)
        vo.setColumnStretch(0, 1)
        self.fl_ctl = led("---")
        vo.addWidget(W.QLabel("Control status"), 4, 0)
        vo.addWidget(self.fl_ctl, 4, 1, 1, 2)
        g.addWidget(group("VIRTUAL ANALOG OUTPUTS  0-10 V", vo), 0, 2)

        self.fl_p1 = plot("Total flow", "kg/s")
        self.fl_c = {"sp_total": self.fl_p1.plot(pen=pg.mkPen(DIM, width=2, style=QtCore.Qt.PenStyle.DashLine),
                                                 name="set-point"),
                     "meas_total": self.fl_p1.plot(pen=pg.mkPen(CYAN, width=2), name="measured"),
                     "ox_flow": self.fl_p1.plot(pen=pg.mkPen(BLUE, width=1), name="oxidizer"),
                     "fuel_flow": self.fl_p1.plot(pen=pg.mkPen(AMBER, width=1), name="fuel")}
        self.fl_p2 = plot("Mixture ratio", "O/F")
        self.fl_c["sp_mr"] = self.fl_p2.plot(pen=pg.mkPen(DIM, width=2, style=QtCore.Qt.PenStyle.DashLine),
                                             name="target")
        self.fl_c["meas_mr"] = self.fl_p2.plot(pen=pg.mkPen(GREEN, width=2), name="measured")
        self.fl_p3 = plot("Valve commands and control error", "V  |  kg/s")
        self.fl_c["ox_cmd"] = self.fl_p3.plot(pen=pg.mkPen(BLUE, width=2), name="ox command (V)")
        self.fl_c["fuel_cmd"] = self.fl_p3.plot(pen=pg.mkPen(AMBER, width=2), name="fuel command (V)")
        self.fl_c["err_total"] = self.fl_p3.plot(pen=pg.mkPen(RED, width=1), name="total flow error (kg/s)")
        g.addWidget(self.fl_p1, 1, 0)
        g.addWidget(self.fl_p2, 1, 1)
        g.addWidget(self.fl_p3, 1, 2)
        g.setRowStretch(1, 1)
        return w

    def _apply_gains(self):
        for which in ("ox", "fuel"):
            self.e.set_gains(which, *(self.fl_gain[(which, k)].value() for k in ("kp", "ki", "kd")))
        self._msg("PID gains applied")

    # ------------------------------------------------------------------ TAB 6
    def _tab_sequence(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        row = W.QHBoxLayout()
        self.sq_boxes = {}
        for i, s in enumerate(State):
            lab = W.QLabel(s.value)
            lab.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            lab.setMinimumHeight(46)
            lab.setWordWrap(True)
            self.sq_boxes[s] = lab
            row.addWidget(lab, 1)
            if i < len(State) - 2:
                arrow = W.QLabel(">")
                arrow.setStyleSheet(f"color:{DIM}; font-size:14pt;")
                row.addWidget(arrow)
        top = W.QVBoxLayout()
        top.addLayout(row)
        pr = W.QHBoxLayout()
        self.sq_prog = W.QProgressBar()
        self.sq_prog.setRange(0, 1000)
        self.sq_info = W.QLabel("")
        pr.addWidget(W.QLabel("Sequence progress"))
        pr.addWidget(self.sq_prog, 1)
        pr.addWidget(self.sq_info)
        top.addLayout(pr)
        warn = W.QLabel("State machine drives VIRTUAL outputs only. ABORT is reachable from every state.")
        warn.setStyleSheet(f"color:{AMBER};")
        top.addWidget(warn)
        g.addWidget(group("STATE MACHINE", top), 0, 0, 1, 2)
        self.sq_checks = make_table(["Pre-check", "Result", "Detail"], 8, 2)
        g.addWidget(group("PRE-CHECK RESULTS", self._wrap(self.sq_checks)), 1, 0)
        ab = W.QVBoxLayout()
        self.sq_abort = W.QLabel("No abort recorded.")
        self.sq_abort.setWordWrap(True)
        self.sq_abort.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        ab.addWidget(self.sq_abort)
        ab.addStretch(1)
        g.addWidget(group("ABORT INFORMATION", ab), 1, 1)
        self.sq_events = make_table(["Wall time", "t (s)", "Type", "Message"], 0, 3)
        g.addWidget(group("EVENT HISTORY (newest first)", self._wrap(self.sq_events)), 2, 0, 1, 2)
        g.setRowStretch(2, 1)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 2)
        return w

    # ------------------------------------------------------------------ TAB 7
    def _tab_logging(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        st = W.QGridLayout()
        self.lg = {}
        for r, (k, cap) in enumerate((("active", "Log status"), ("run_id", "Test-run ID"),
                                      ("run_dir", "Current log folder"), ("slow_samples", "Slow samples logged"),
                                      ("hf_samples", "High-frequency samples logged"),
                                      ("csv_rows", "CSV rows written"), ("events", "Events logged"),
                                      ("queue_depth", "Logger queue depth / high-water"),
                                      ("dropped_items", "Dropped items (queue overflow)"),
                                      ("last_error", "Last logger error"))):
            st.addWidget(W.QLabel(cap), r, 0)
            v = W.QLabel("---")
            v.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setStyleSheet("font-weight:600;")
            self.lg[k] = v
            st.addWidget(v, r, 1)
        st.setColumnStretch(1, 1)
        g.addWidget(group("LOG STATUS  (CSV + TDMS, organised by test-run ID)", st), 0, 0)
        bt = W.QVBoxLayout()
        for text, fn in (("Start logging", lambda: self._msg(f"logging to {self.e.start_logging()}")),
                         ("Stop logging and write summary", self._stop_logging),
                         ("Show last test summary", self._show_summary),
                         ("Export live trend buffer to CSV", self._export_trend),
                         ("Open results folder", self._open_results)):
            b = W.QPushButton(text)
            b.clicked.connect(fn)
            bt.addWidget(b)
        bt.addStretch(1)
        g.addWidget(group("LOGGING CONTROLS", bt), 0, 1)
        self.lg_summary = W.QPlainTextEdit()
        self.lg_summary.setReadOnly(True)
        self.lg_summary.setStyleSheet("font-family: Consolas; font-size: 9.5pt;")
        self.lg_summary.setPlainText("The test summary appears here after a run is stopped.\n"
                                     "Logging starts automatically with the pre-check and stops 2 s after "
                                     "COMPLETE or ABORT.")
        g.addWidget(group("TEST SUMMARY REPORT", self._wrap(self.lg_summary)), 1, 0, 1, 2)
        g.setRowStretch(1, 1)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 1)
        return w

    def _stop_logging(self):
        self.e.stop_logging()
        self._show_summary()

    def _show_summary(self):
        d = self.e.logger.run_dir
        p = os.path.join(d, "TEST_SUMMARY.md") if d else None
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                self.lg_summary.setPlainText(f"{p}\n\n" + f.read())
        else:
            self._msg((False, "no summary yet - stop the current log first"))

    def _export_trend(self):
        d = os.path.join(self.e.logger.results_dir, "exports")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, datetime.now().strftime("trend_export_%Y%m%d_%H%M%S.csv"))
        with self.e.lock:
            data = self.e.trend.latest(self.e.trend.capacity)
        with open(p, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["t_s"] + [f"{c['id']} [{c['unit']}]" for c in self.cfg["slow_channels"]])
            wr.writerows([[f"{v:.6g}" for v in row] for row in data])
        self._msg(f"exported {data.shape[0]} rows to {p}")

    def _open_results(self):
        d = self.e.logger.results_dir
        os.makedirs(d, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(d))

    # ------------------------------------------------------------------ TAB 8
    def _tab_config(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        ch = self.cfg["slow_channels"]
        self.cf_cols = ["name", "unit", "gain", "offset", "warn_lo", "warn_hi", "abort_hi", "enabled"]
        self.cf_table = make_table(["ID", "Type"] + [c.replace("_", " ") for c in self.cf_cols], len(ch), 2)
        self.cf_table.setEditTriggers(W.QAbstractItemView.EditTrigger.AllEditTriggers)
        self._cf_fill()
        left = W.QVBoxLayout()
        note = W.QLabel("DEMONSTRATION calibration values and limits - not real sensor calibrations or engine "
                        "safety limits. Edit cells, then press Apply.")
        note.setStyleSheet(f"color:{AMBER};")
        left.addWidget(note)
        left.addWidget(self.cf_table)
        row = W.QHBoxLayout()
        for text, fn in (("Apply channel configuration", self._cf_apply),
                         ("Save configuration to file", lambda: self._msg(f"saved {self.e.save_config()}")),
                         ("Discard edits", self._cf_fill)):
            b = W.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        left.addLayout(row)
        g.addWidget(group("CHANNEL CONFIGURATION, SCALING AND ALARM THRESHOLDS (50 slow channels)", left), 0, 0)
        self.cf_params = make_table(["Section", "Parameter", "Value"], 0, 1)
        self.cf_params.setEditTriggers(W.QAbstractItemView.EditTrigger.AllEditTriggers)
        self._param_keys = []
        for sec in ("sequence", "safety", "simulation", "flow_control", "fft", "logging"):
            for k, v in self.cfg[sec].items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    self._param_keys.append((sec, k))
        self.cf_params.setRowCount(len(self._param_keys))
        self._params_fill()
        right = W.QVBoxLayout()
        right.addWidget(self.cf_params)
        b = W.QPushButton("Apply parameters")
        b.clicked.connect(self._params_apply)
        right.addWidget(b)
        g.addWidget(group("SEQUENCE / SAFETY / SIMULATION / FFT PARAMETERS", right), 0, 1)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 2)
        return w

    def _cf_fill(self):
        for r, c in enumerate(self.cfg["slow_channels"]):
            cell(self.cf_table, r, 0, c["id"])
            cell(self.cf_table, r, 1, c["sensor_type"])
            for k, key in enumerate(self.cf_cols):
                v = c.get(key)
                cell(self.cf_table, r, 2 + k, "" if v is None else str(v), editable=True)

    def _cf_apply(self):
        n = 0
        try:
            for r, c in enumerate(self.cfg["slow_channels"]):
                new = {}
                for k, key in enumerate(self.cf_cols):
                    txt = self.cf_table.item(r, 2 + k).text().strip()
                    if key in ("name", "unit"):
                        val = txt
                    elif key == "enabled":
                        val = txt.lower() in ("true", "1", "yes")
                    elif key in ("gain", "offset"):
                        val = float(txt)
                    else:
                        val = None if txt == "" or txt.lower() == "none" else float(txt)
                    if c.get(key) != val:
                        new[key] = val
                if new:
                    self.e.update_slow_channel(r, **new)
                    n += 1
            self._msg(f"{n} channel(s) updated")
        except ValueError as ex:
            self._msg((False, f"invalid entry: {ex}"))
        self._cf_fill()

    def _params_fill(self):
        for r, (sec, k) in enumerate(self._param_keys):
            cell(self.cf_params, r, 0, sec)
            cell(self.cf_params, r, 1, k)
            cell(self.cf_params, r, 2, str(self.cfg[sec][k]), editable=True)

    def _params_apply(self):
        try:
            with self.e.lock:
                for r, (sec, k) in enumerate(self._param_keys):
                    old = self.cfg[sec][k]
                    new = type(old)(float(self.cf_params.item(r, 2).text()))
                    if new != old:
                        self.cfg[sec][k] = new
                        self.e._event("OPERATOR", f"Parameter {sec}.{k}: {old} -> {new}")
                self.e.apply_parameters()
            self.hf_size.setCurrentText(str(self.cfg["fft"]["size"]))
            self._msg("parameters applied")
        except Exception as ex:
            self._msg((False, f"invalid parameter: {ex}"))
        self._params_fill()

    # ------------------------------------------------------------------ fault injection
    def _tab_faults(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        q = W.QGridLayout()
        quick = (("Pressure sensor failure (PT-07 disconnected)", lambda: self.e.inject_fault("slow", self.e.slow_id["PT-07"], "disconnect")),
                 ("Temperature sensor failure (TT-01 disconnected)", lambda: self.e.inject_fault("slow", self.e.slow_id["TT-01"], "disconnect")),
                 ("Stuck pressure sensor (PT-05 frozen)", lambda: self.e.inject_fault("slow", self.e.slow_id["PT-05"], "stuck")),
                 ("NaN on pressure sensor (PT-03)", lambda: self.e.inject_fault("slow", self.e.slow_id["PT-03"], "nan")),
                 ("Chamber OVERPRESSURE", lambda: self.e.inject_fault("overpressure")),
                 ("Chamber pressure spike (50 ms)", lambda: self.e.inject_fault("spike")),
                 ("Digital input failure (DI-03 interlock stuck low)", lambda: self.e.inject_fault("di", 3, "stuck_low")),
                 ("Emergency-stop activation", lambda: self.b_estop.setChecked(True)),
                 ("Critical DAQ error", lambda: self.e.inject_fault("acq_error")),
                 ("DAQ read time-out", lambda: self.e.inject_fault("acq_timeout")))
        for i, (text, fn) in enumerate(quick):
            b = W.QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: self._msg(f() or "E-STOP pressed"))
            q.addWidget(b, i // 2, i % 2)
        bc = W.QPushButton("CLEAR ALL FAULTS")
        bc.setObjectName("go")
        bc.clicked.connect(self._clear_faults)
        q.addWidget(bc, 5, 0, 1, 2)
        g.addWidget(group("QUICK FAULTS", q), 0, 0)

        c = W.QGridLayout()
        self.ft_ch = W.QComboBox()
        self.ft_ch.addItems([f"{x['id']}  {x['name']}" for x in self.cfg["slow_channels"]])
        self.ft_type = W.QComboBox()
        self.ft_type.addItems(["disconnect", "stuck", "nan", "inf", "offset", "noise"])
        self.ft_val = W.QDoubleSpinBox()
        self.ft_val.setRange(-10, 10)
        self.ft_val.setDecimals(3)
        self.ft_val.setValue(1.0)
        b = W.QPushButton("Inject on slow channel")
        b.clicked.connect(lambda: self._msg(self.e.inject_fault(
            "slow", self.ft_ch.currentIndex(), self.ft_type.currentText(), self.ft_val.value())))
        self.ft_hf = W.QComboBox()
        self.ft_hf.addItems([f"{x['id']}  {x['name']}" for x in self.cfg["hf_channels"]])
        self.ft_hft = W.QComboBox()
        self.ft_hft.addItems(["disconnect", "stuck", "nan"])
        b2 = W.QPushButton("Inject on high-frequency channel")
        b2.clicked.connect(lambda: self._msg(self.e.inject_fault(
            "hf", self.ft_hf.currentIndex(), self.ft_hft.currentText())))
        self.ft_di = W.QComboBox()
        self.ft_di.addItems([f"{x['id']}  {x['name']}" for x in self.cfg["digital_inputs"]])
        self.ft_dit = W.QComboBox()
        self.ft_dit.addItems(["stuck_low", "stuck_high", "invert"])
        b3 = W.QPushButton("Inject on digital input")
        b3.clicked.connect(lambda: self._msg(self.e.inject_fault(
            "di", self.ft_di.currentIndex(), self.ft_dit.currentText())))
        for r, row in enumerate(((W.QLabel("Slow channel"), self.ft_ch, self.ft_type, self.ft_val, b),
                                 (W.QLabel("HF channel"), self.ft_hf, self.ft_hft, None, b2),
                                 (W.QLabel("Digital input"), self.ft_di, self.ft_dit, None, b3))):
            for col, wid in enumerate(row):
                if wid is not None:
                    c.addWidget(wid, r, col)
        c.addWidget(W.QLabel("Value = offset in volts (offset) or noise RMS in volts (noise). Overpressure and "
                             "spike magnitudes are set on the CONFIGURATION tab (simulation section)."), 3, 0, 1, 5)
        g.addWidget(group("CUSTOM FAULT", c), 0, 1)
        self.ft_list = W.QListWidget()
        g.addWidget(group("ACTIVE INJECTED FAULTS", self._wrap(self.ft_list)), 1, 0, 1, 2)
        g.setRowStretch(1, 1)
        return w

    def _clear_faults(self):
        self.b_estop.setChecked(False)
        self._msg(self.e.inject_fault("clear"))

    # ================================================================== periodic refresh
    def refresh(self):
        s = self.e.snapshot()
        st = s["state"]
        col = STATE_COLOR[st]
        self.h_state.setText(st.value)
        self.h_state.setStyleSheet(f"background:{col}; color:#0b0f14; font-size:13pt; font-weight:700; "
                                   "padding:5px 14px; border-radius:4px;")
        self.h_time.setText(f"t = {s['t']:.1f} s")
        n_al = len(s["alarms"])
        set_led(self.h_alarm, n_al > 0 or st == State.ABORT, RED, "#1e6b3c",
                "ABORT" if st == State.ABORT else (f"{n_al} ALARM(S)" if n_al else "NO ALARMS"))
        if s["abort"]:
            a = s["abort"]
            self.abort_banner.setText(f"ABORT  -  {a['reason']}\nEvent time: {a['timestamp']}  (t = {a['t_s']} s)"
                                      "   |   All virtual outputs in SAFE state, control inhibited.   "
                                      "Clear the cause, then press OPERATOR RESET.")
            self.abort_banner.show()
        else:
            self.abort_banner.hide()
        if self.b_estop.isChecked() != ("E-STOP" in s["faults"]):
            self.b_estop.blockSignals(True)
            self.b_estop.setChecked("E-STOP" in s["faults"])
            self.b_estop.blockSignals(False)
        self.b_start.setEnabled(st == State.IDLE and s["running"])
        self.b_fire.setEnabled(st == State.READY)
        self.b_stop.setEnabled(st in (State.PRECHECK, State.READY, State.START, State.RAMP, State.HOLD))
        self.b_reset.setEnabled(st in (State.ABORT, State.COMPLETE))
        self.b_abort.setEnabled(st != State.ABORT)
        self.b_engine_start.setEnabled(not s["running"])
        self.b_engine_stop.setEnabled(s["running"])
        i = self.tabs.currentIndex()
        (self._r_overview, self._r_analog, self._r_hf, self._r_dio, self._r_flow, self._r_sequence,
         self._r_logging, lambda s: None, self._r_faults)[i](s)

    def _trend(self, n=1500):
        with self.e.lock:
            return self.e.trend.latest(n), self.e.ctl_trend.latest(n)

    def _r_overview(self, s):
        st = s["state"]
        ok = s["running"] and st != State.ABORT and not s["alarms"]
        set_led(self.ov["system"], ok, GREEN, RED if st == State.ABORT else AMBER,
                "NOMINAL" if ok else ("ABORTED" if st == State.ABORT else
                                      ("ALARM" if s["alarms"] else "ENGINE STOPPED")))
        set_led(self.ov["backend"], True, AMBER if s["simulation"] else BLUE,
                text=f"{s['backend'].upper()}" + ("  (no hardware)" if s["simulation"] else ""))
        set_led(self.ov["engine"], s["running"], GREEN, "#39424d", "RUNNING" if s["running"] else "STOPPED")
        set_led(self.ov["state"], True, STATE_COLOR[st], text=f"{st.value}   ({s['state_elapsed']:.1f} s)")
        set_led(self.ov["ctl"], s["ctl_enabled"], GREEN, "#39424d", s["ctl_status"])
        lg = s["logger"]
        set_led(self.ov["log"], lg["active"], GREEN if lg["healthy"] else RED, "#39424d",
                f"LOGGING  {lg['run_id']}" if lg["active"] else "IDLE")
        lp = s["loops"]
        set_led(self.ov["rates"], True, "#263040",
                text=f"slow {lp['slow_rate_meas']:,.0f} S/s/ch   |   HF {lp['hf_rate_meas']:,.0f} S/s/ch")
        self.ov["rates"].setStyleSheet(f"background:#263040; color:{TXT}; border-radius:4px; padding:2px 8px;")
        set_led(self.ov["outputs"], s["outputs_locked"], RED, "#263040",
                "LOCKED IN SAFE STATE" if s["outputs_locked"] else "virtual - sequencer controlled")
        if not s["outputs_locked"]:
            self.ov["outputs"].setStyleSheet(f"background:#263040; color:{TXT}; border-radius:4px; padding:2px 8px;")
        e = s["eng"]
        thrust = sum(e[self.e.slow_id[k]] for k in ("LC-01", "LC-02", "LC-03"))
        for k, v, nd in (("total", s["meas_total"], 3), ("mr", s["meas_mr"], 3), ("pc", e[self.e.i_pc], 2),
                         ("thrust", thrust, 0), ("ox", s["ao"][0], 3), ("fuel", s["ao"][1], 3)):
            self.ov_big[k].setText(fmt(v, nd))
        self.ov_big["pc"].setStyleSheet(f"color:{STATUS_COLOR[ChStatus(int(s['status'][self.e.i_pc]))]};")
        items = s["alarms"] or ["(no active alarms)"]
        if [self.alarm_list.item(i).text() for i in range(self.alarm_list.count())] != items:
            self.alarm_list.clear()
            self.alarm_list.addItems(items)
            for i in range(self.alarm_list.count()):
                self.alarm_list.item(i).setForeground(QtGui.QColor(RED if s["alarms"] else DIM))
        tr, ct = self._trend()
        if tr.shape[0]:
            self.ov_c_pc.setData(tr[:, 0], tr[:, 1 + self.e.i_pc])
            self.ov_c_fl.setData(ct[:, 0], np.nan_to_num(ct[:, 2]) * 10.0)

    def _r_analog(self, s):
        t = self.an_table
        for r, c in enumerate(self.cfg["slow_channels"]):
            stt = ChStatus(int(s["status"][r]))
            cell(t, r, 1, c["name"])
            cell(t, r, 2, c["sensor_type"])
            cell(t, r, 3, fmt(float(s["raw"][r]), 4))
            cell(t, r, 4, fmt(float(s["eng"][r]), 3), TXT)
            cell(t, r, 5, c["unit"])
            cell(t, r, 6, stt.name, STATUS_COLOR[stt])
            cell(t, r, 7, f"{c['gain']:g}")
            cell(t, r, 8, f"{c['offset']:g}")
        tr, _ = self._trend()
        if tr.shape[0]:
            for r, curve in self.an_curves.items():
                curve.setData(tr[:, 0], tr[:, 1 + r])

    def _r_hf(self, s):
        fs = self.e.fs_hf
        ch = self.e.fft_channel
        n = int(0.02 * fs)
        x = self.e.hf_waveform(ch, n)
        if x.size:
            self.hf_wave_c.setData(np.arange(x.size) * 1e3 / fs, x)
        unit = self.cfg["hf_channels"][ch]["unit"]
        self.hf_wave.setLabel("left", unit)
        r = self.e.fft_result
        size = self.e.fft.size
        self.hf_info["res"].setText(f"{frequency_resolution(fs, size):.3f} Hz   ({fs:,.0f} / {size})")
        self.hf_info["nyq"].setText(f"{nyquist(fs):,.0f} Hz")
        self.hf_info["fs"].setText(f"{fs:,.0f} S/s per channel")
        if r is not None and r["channel"] == self.cfg["hf_channels"][ch]["id"]:
            self.hf_spec_c.setData(r["freqs"][1:], np.maximum(r["mag"][1:], 1e-6))
            self.hf_peak.setValue(r["peak_freq_hz"])
            self.hf_info["peak"].setText(f"{r['peak_freq_hz']:,.1f} Hz")
            self.hf_info["amp"].setText(f"{r['peak_amp']:.4f} {unit}")
            stt = r["stats"]
            for k, key in (("rms", "rms"), ("pkpk", "pk_pk"), ("crest", "crest"), ("mean", "mean")):
                self.hf_info[k].setText(f"{fmt(stt[key], 4)} {unit if k != 'crest' else ''}")
        else:
            self.hf_info["peak"].setText("no valid data")
        hs = self.e.hf_stats
        if hs is not None:
            for i in range(len(self.cfg["hf_channels"])):
                cell(self.hf_table, i, 2, fmt(float(hs["rms"][i]), 4))
                cell(self.hf_table, i, 3, fmt(float(hs["pk_pk"][i]), 4))
                okv = bool(hs["valid"][i])
                flat = okv and float(hs["pk_pk"][i]) == 0.0
                cell(self.hf_table, i, 5, "INVALID" if not okv else ("STALE" if flat else "OK"),
                     RED if (not okv or flat) else GREEN)

    def _r_dio(self, s):
        for i, lab in enumerate(self.di_leds):
            on = bool(s["di"][i])
            set_led(lab, on, GREEN, "#39424d", "HIGH" if on else "LOW")
        for i, lab in enumerate(self.do_leds):
            on = bool(s["do"][i])
            set_led(lab, on, AMBER, "#39424d", "ON (virtual)" if on else "OFF")

    def _r_flow(self, s):
        self.fl["status"].setText(s["ctl_status"])
        for k in ("sp_total", "meas_total", "err_total", "ox_flow", "fuel_flow"):
            self.fl[k].setText(f"{fmt(s[k], 4)} kg/s")
        for k in ("target_mr", "meas_mr", "err_mr"):
            self.fl[k].setText(fmt(s[k], 4))
        for i in range(2):
            self.fl_bar[i].setValue(int(round(s["ao_pct"][i] * 10)))
            self.fl_v[i].setText(f"{s['ao'][i]:.3f} V   ({s['ao_pct'][i]:.1f} %)")
            sat = bool(s["ao_sat"][i]) or bool(s["ctl_sat"][i])
            set_led(self.fl_sat[i], sat, RED, "#1e6b3c", "SATURATED" if sat else "IN RANGE")
        set_led(self.fl_ctl, s["ctl_enabled"], GREEN, RED if s["outputs_locked"] else "#39424d", s["ctl_status"])
        _, ct = self._trend()
        if ct.shape[0]:
            t = ct[:, 0]
            for name, curve in self.fl_c.items():
                curve.setData(t, np.nan_to_num(ct[:, CTL_TREND.index(name)]))

    def _r_sequence(self, s):
        st = s["state"]
        for k, lab in self.sq_boxes.items():
            active = k == st
            c = STATE_COLOR[k]
            lab.setStyleSheet(f"background:{c if active else PANEL}; color:{'#0b0f14' if active else DIM}; "
                              f"border:2px solid {c}; border-radius:6px; font-weight:700; padding:4px;")
        self.sq_prog.setValue(int(s["progress"] * 1000))
        self.sq_info.setText(f"{st.value}: {s['state_elapsed']:.1f} s in state   |   "
                             f"completed runs: {s['runs_completed']}")
        pc = s["prechecks"]
        self.sq_checks.setRowCount(max(len(pc), 1))
        if not pc:
            cell(self.sq_checks, 0, 0, "(pre-check has not run yet)")
        for r, (name, ok, detail) in enumerate(pc):
            cell(self.sq_checks, r, 0, name)
            cell(self.sq_checks, r, 1, "PASS" if ok else "FAIL", GREEN if ok else RED)
            cell(self.sq_checks, r, 2, detail)
        a = s["last_abort"]
        if a:
            self.sq_abort.setText(f"<b style='color:{RED}'>{'ACTIVE' if s['abort'] else 'LAST'} ABORT</b><br>"
                                  f"<b>Reason:</b> {a['reason']}<br><b>Timestamp:</b> {a['timestamp']}<br>"
                                  f"<b>Sequence time:</b> {a['t_s']} s<br><b>Outputs:</b> "
                                  f"{'locked in safe state' if s['outputs_locked'] else 'released after reset'}")
        ev = s["events"]
        if len(ev) and (len(ev), ev[-1]) != self._n_events:
            self._n_events = (len(ev), ev[-1])
            colors = {"ABORT": RED, "ALARM": AMBER, "STATE": CYAN, "FAULT": VIOLET, "ERROR": RED,
                      "OPERATOR": BLUE}
            self.sq_events.setRowCount(len(ev))
            for r, (wall, t, typ, msg) in enumerate(reversed(ev)):
                col = colors.get(typ, TXT)
                cell(self.sq_events, r, 0, wall)
                cell(self.sq_events, r, 1, f"{t:.2f}")
                cell(self.sq_events, r, 2, typ, col)
                cell(self.sq_events, r, 3, msg, col)

    def _r_logging(self, s):
        lg = s["logger"]
        self.lg["active"].setText(("LOGGING" if lg["active"] else "STOPPED") +
                                  ("" if lg["healthy"] else "   (ERROR)"))
        self.lg["active"].setStyleSheet(f"font-weight:700; color:{GREEN if lg['active'] else DIM};")
        self.lg["run_id"].setText(str(lg["run_id"] or "---"))
        self.lg["run_dir"].setText(str(lg["run_dir"] or "---"))
        for k in ("slow_samples", "hf_samples", "csv_rows", "events", "dropped_items"):
            self.lg[k].setText(f"{lg[k]:,}")
        self.lg["queue_depth"].setText(f"{lg['queue_depth']} / {lg['queue_high_water']}")
        self.lg["last_error"].setText(str(lg["last_error"] or "none"))

    def _r_faults(self, s):
        items = s["faults"] or ["(no faults injected)"]
        if [self.ft_list.item(i).text() for i in range(self.ft_list.count())] != items:
            self.ft_list.clear()
            self.ft_list.addItems(items)

    def closeEvent(self, ev):
        self.timer.stop()
        self.e.stop()
        ev.accept()
