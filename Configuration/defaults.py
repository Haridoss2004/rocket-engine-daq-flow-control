"""Default (demonstration) configuration for the Rocket engine DAQ simulation.

EVERY numeric value in this file is a DEMONSTRATION value chosen so the
simulation behaves plausibly. None of them is a real sensor calibration, a real
engine operating point or a real safety limit. All of them are editable through
Configuration/system_config.json or the GUI Configuration tab.

Sensor convention used by the demo: a 1..9 V "live zero" span. A healthy sensor
never outputs less than about 1 V, so a reading near 0 V means an open circuit.
    Engineering Value = Measured Voltage x Calibration Gain + Offset
"""
from __future__ import annotations

N_SLOW, N_HF, N_DI, N_DO, N_AO = 50, 25, 30, 30, 2


def _slow(idx, cid, name, stype, unit, gain, offset, src, k=1.0, b=0.0,
          warn_lo=None, warn_hi=None, abort_hi=None, noise_v=0.002):
    return {
        "index": idx, "id": cid, "name": name, "sensor_type": stype, "unit": unit,
        "gain": gain, "offset": offset,            # demo calibration (editable)
        "raw_valid_min_v": 0.5, "raw_valid_max_v": 9.8,
        "warn_lo": warn_lo, "warn_hi": warn_hi, "abort_hi": abort_hi,
        "enabled": True,
        "sim": {"source": src, "k": k, "b": b, "noise_v": noise_v},
    }


def slow_channels():
    ch = []
    # ---- 20 pressure transducers: 1..9 V -> 0..100 bar  (gain 12.5, offset -12.5)
    P = dict(stype="pressure", unit="bar", gain=12.5, offset=-12.5)
    pt = [
        ("OX tank pressure", "p_ox_tank", 1.0, 0.0, None, 46.0, 50.0),
        ("Fuel tank pressure", "p_fuel_tank", 1.0, 0.0, None, 46.0, 50.0),
        ("OX feed line pressure", "p_ox_line", 1.0, 0.0, None, 46.0, 50.0),
        ("Fuel feed line pressure", "p_fuel_line", 1.0, 0.0, None, 46.0, 50.0),
        ("OX injector manifold pressure", "p_ox_inj", 1.0, 0.0, None, 32.0, 38.0),
        ("Fuel injector manifold pressure", "p_fuel_inj", 1.0, 0.0, None, 32.0, 38.0),
        ("Chamber pressure A", "p_chamber", 1.0, 0.0, None, 24.0, 28.0),
        ("Chamber pressure B (redundant)", "p_chamber", 1.0, 0.0, None, 24.0, 28.0),
        ("N2 purge supply pressure", "p_purge", 1.0, 0.0, None, 20.0, 25.0),
        ("Pneumatic supply pressure", "p_pneu", 1.0, 0.0, 6.0, 10.0, 12.0),
        ("OX pressurant regulator outlet", "p_ox_tank", 1.02, 0.3, None, 47.0, 52.0),
        ("Fuel pressurant regulator outlet", "p_fuel_tank", 1.02, 0.3, None, 47.0, 52.0),
        ("OX venturi upstream pressure", "p_ox_line", 0.99, 0.0, None, 46.0, 50.0),
        ("Fuel venturi upstream pressure", "p_fuel_line", 0.99, 0.0, None, 46.0, 50.0),
        ("Igniter cavity pressure", "p_chamber", 0.95, 0.0, None, 24.0, 28.0),
        ("Nozzle wall static pressure", "p_chamber", 0.55, 0.45, None, 15.0, 18.0),
        ("Coolant inlet pressure", "p_pneu", 0.6, 0.0, None, 8.0, 10.0),
        ("Coolant outlet pressure", "p_pneu", 0.45, 0.0, None, 8.0, 10.0),
        ("Test cell ambient pressure", "p_amb", 1.0, 0.0, None, 1.5, 3.0),
        ("Spare pressure channel", "p_amb", 1.0, 0.0, None, 1.5, 3.0),
    ]
    for i, (name, src, k, b, wl, wh, ah) in enumerate(pt):
        ch.append(_slow(len(ch), f"PT-{i+1:02d}", name, P["stype"], P["unit"], P["gain"],
                        P["offset"], src, k, b, wl, wh, ah))
    # ---- 16 temperatures: 1..9 V -> -200..600 degC (gain 100, offset -300)
    tt = [
        ("Chamber wall temperature 1", "t_chamber", 1.0, 0.0, 550.0),
        ("Chamber wall temperature 2", "t_chamber", 0.93, 2.0, 550.0),
        ("Nozzle throat temperature", "t_nozzle", 1.0, 0.0, 550.0),
        ("Nozzle exit temperature", "t_nozzle", 0.7, 6.0, 550.0),
        ("OX feed line temperature", "t_ox", 1.0, 0.0, 60.0),
        ("OX tank temperature", "t_ox", 0.2, 16.0, 60.0),
        ("Fuel feed line temperature", "t_fuel", 1.0, 0.0, 60.0),
        ("Fuel tank temperature", "t_amb", 1.0, 0.5, 60.0),
        ("Coolant inlet temperature", "t_amb", 1.0, -2.0, 60.0),
        ("Coolant outlet temperature", "t_coolant", 1.0, 0.0, 120.0),
        ("Injector face temperature", "t_chamber", 0.35, 14.0, 300.0),
        ("Igniter body temperature", "t_chamber", 0.25, 16.0, 300.0),
        ("Thrust stand temperature", "t_amb", 1.0, 1.0, 60.0),
        ("Test cell ambient temperature", "t_amb", 1.0, 0.0, 60.0),
        ("DAQ cabinet temperature", "t_amb", 1.0, 6.0, 60.0),
        ("Spare thermocouple", "t_amb", 1.0, 0.0, 60.0),
    ]
    for i, (name, src, k, b, wh) in enumerate(tt):
        ch.append(_slow(len(ch), f"TT-{i+1:02d}", name, "temperature", "degC", 100.0, -300.0,
                        src, k, b, None, wh, None, noise_v=0.003))
    # ---- 4 load cells
    for i in range(3):   # 1..9 V -> 0..2000 N
        ch.append(_slow(len(ch), f"LC-{i+1:02d}", f"Thrust load cell {i+1}", "load_cell", "N",
                        250.0, -250.0, "thrust", 1 / 3, 0.0, None, 1500.0, None, noise_v=0.004))
    ch.append(_slow(len(ch), "LC-04", "Propellant tank mass (run tank)", "load_cell", "kg",
                    12.5, -12.5, "mass_prop", 1.0, 0.0, 5.0, None, None, noise_v=0.003))
    # ---- 2 flow meters: 1..9 V -> 0..4 kg/s  (SIMULATED feedback for the controller)
    ch.append(_slow(len(ch), "FT-01", "Oxidizer mass flow (SIMULATED)", "flow", "kg/s",
                    0.5, -0.5, "flow_ox", 1.0, 0.0, None, 3.5, None, noise_v=0.006))
    ch.append(_slow(len(ch), "FT-02", "Fuel mass flow (SIMULATED)", "flow", "kg/s",
                    0.5, -0.5, "flow_fuel", 1.0, 0.0, None, 3.5, None, noise_v=0.006))
    # ---- 8 other slow measurements
    other = [
        ("ZT-01", "OX control valve position feedback", "position", "%", 12.5, -12.5, "pos_ox", 100.0, 0.0),
        ("ZT-02", "Fuel control valve position feedback", "position", "%", 12.5, -12.5, "pos_fuel", 100.0, 0.0),
        ("VT-01", "24 V bus voltage", "voltage", "V", 4.0, -4.0, "bus_v", 1.0, 0.0),
        ("IT-01", "24 V bus current", "current", "A", 1.25, -1.25, "bus_i", 1.0, 0.0),
        ("VT-02", "Igniter supply voltage", "voltage", "V", 4.0, -4.0, "ign_v", 1.0, 0.0),
        ("RH-01", "Test cell relative humidity", "humidity", "%RH", 12.5, -12.5, "humidity", 1.0, 0.0),
        ("GD-01", "Gas detector (LEL)", "gas", "%LEL", 12.5, -12.5, "lel", 1.0, 0.0),
        ("AX-01", "Spare slow analog input", "other", "V", 1.0, 0.0, "spare_v", 1.0, 0.0),
    ]
    for cid, name, stype, unit, g, o, src, k, b in other:
        ch.append(_slow(len(ch), cid, name, stype, unit, g, o, src, k, b))
    assert len(ch) == N_SLOW
    return ch


def hf_channels():
    """25 high-frequency channels. Bipolar +-10 V signals.
    tones: [frequency_Hz, amplitude_V_at_full_combustion]."""
    ch = []
    for i in range(10):   # dynamic pressure, demo sensitivity 0.5 bar/V
        ch.append({
            "index": len(ch), "id": f"DP-{i+1:02d}", "name": f"Dynamic pressure {i+1}",
            "sensor_type": "dynamic_pressure", "unit": "bar", "gain": 0.5, "offset": 0.0,
            "raw_valid_min_v": -10.0, "raw_valid_max_v": 10.0, "enabled": True,
            "sim": {"tones": [[1200.0 + 35.0 * i, 1.2], [2400.0 + 70.0 * i, 0.5], [5100.0, 0.2]],
                    "idle_gain": 0.02, "noise_v": 0.05},
        })
    for i in range(10):   # accelerometers, demo sensitivity 10 g/V (100 mV/g)
        ch.append({
            "index": len(ch), "id": f"VIB-{i+1:02d}", "name": f"Accelerometer {i+1}",
            "sensor_type": "vibration", "unit": "g", "gain": 10.0, "offset": 0.0,
            "raw_valid_min_v": -10.0, "raw_valid_max_v": 10.0, "enabled": True,
            "sim": {"tones": [[800.0 + 20.0 * i, 0.8], [3200.0 + 50.0 * i, 0.6], [12000.0, 0.25]],
                    "idle_gain": 0.03, "noise_v": 0.04},
        })
    aux = [("AE-01", "Acoustic emission 1", "acoustic", "Pa", 100.0, [[6000.0, 0.7], [18000.0, 0.3]]),
           ("AE-02", "Acoustic emission 2", "acoustic", "Pa", 100.0, [[6500.0, 0.7], [20000.0, 0.3]]),
           ("SG-01", "Dynamic strain gauge 1", "strain", "ustrain", 200.0, [[450.0, 1.0], [900.0, 0.4]]),
           ("SG-02", "Dynamic strain gauge 2", "strain", "ustrain", 200.0, [[470.0, 1.0], [940.0, 0.4]]),
           ("HF-25", "Reference tone 1 kHz (always on)", "reference", "V", 1.0, [[1000.0, 1.0]])]
    for cid, name, stype, unit, g, tones in aux:
        ch.append({
            "index": len(ch), "id": cid, "name": name, "sensor_type": stype, "unit": unit,
            "gain": g, "offset": 0.0, "raw_valid_min_v": -10.0, "raw_valid_max_v": 10.0,
            "enabled": True,
            "sim": {"tones": tones, "idle_gain": 1.0 if stype == "reference" else 0.03,
                    "noise_v": 0.01 if stype == "reference" else 0.04},
        })
    assert len(ch) == N_HF
    return ch


# Digital output indices used by the sequencer (all VIRTUAL)
DO = dict(OX_MAIN=0, FUEL_MAIN=1, OX_VENT=2, FUEL_VENT=3, PURGE=4, OX_PRESS=5, FUEL_PRESS=6,
          IGNITER=7, DELUGE=8, BEACON=9, SIREN=10, ARMED_LAMP=11, ACTIVE_LAMP=12,
          ABORT_LAMP=13, CAMERA=14)


def digital_outputs():
    names = [
        ("OX main valve (solenoid)", "valve", 0), ("Fuel main valve (solenoid)", "valve", 0),
        ("OX tank vent valve", "valve", 1), ("Fuel tank vent valve", "valve", 1),
        ("N2 purge valve", "purge", 1), ("OX pressurisation valve", "valve", 0),
        ("Fuel pressurisation valve", "valve", 0), ("Igniter command (SIMULATED)", "igniter", 0),
        ("Water deluge valve", "valve", 0), ("Warning beacon", "status", 1),
        ("Siren", "status", 1), ("DAQ armed lamp", "status", 0),
        ("Test active lamp", "status", 0), ("Abort lamp", "status", 1),
        ("High-speed camera trigger", "trigger", 0),
    ]
    out = []
    for i in range(N_DO):
        if i < len(names):
            n, kind, safe = names[i]
        else:
            n, kind, safe = f"Spare digital output {i}", "spare", 0
        out.append({"index": i, "id": f"DO-{i:02d}", "name": n, "kind": kind, "safe_state": safe})
    return out


def digital_inputs():
    """sim: how the simulator derives the line.  expected_precheck: required value or null."""
    d = [
        ("Emergency-stop loop healthy", "estop", {"type": "estop_ok"}, 1),
        ("24 V supply OK", "status", {"type": "const", "value": 1}, 1),
        ("Pneumatic supply OK", "status", {"type": "const", "value": 1}, 1),
        ("Test cell door interlock closed", "status", {"type": "const", "value": 1}, 1),
        ("Igniter continuity OK", "status", {"type": "const", "value": 1}, 1),
        ("Remote enable key ON", "status", {"type": "const", "value": 1}, 1),
        ("OX main valve OPEN limit switch", "valve_fb", {"type": "do_open", "do": 0}, None),
        ("OX main valve CLOSED limit switch", "valve_fb", {"type": "do_closed", "do": 0}, 1),
        ("Fuel main valve OPEN limit switch", "valve_fb", {"type": "do_open", "do": 1}, None),
        ("Fuel main valve CLOSED limit switch", "valve_fb", {"type": "do_closed", "do": 1}, 1),
        ("OX vent OPEN limit switch", "valve_fb", {"type": "do_open", "do": 2}, None),
        ("OX vent CLOSED limit switch", "valve_fb", {"type": "do_closed", "do": 2}, None),
        ("Fuel vent OPEN limit switch", "valve_fb", {"type": "do_open", "do": 3}, None),
        ("Fuel vent CLOSED limit switch", "valve_fb", {"type": "do_closed", "do": 3}, None),
        ("Purge valve OPEN limit switch", "valve_fb", {"type": "do_open", "do": 4}, None),
        ("Purge valve CLOSED limit switch", "valve_fb", {"type": "do_closed", "do": 4}, None),
        ("OX pressurisation OPEN limit switch", "valve_fb", {"type": "do_open", "do": 5}, None),
        ("OX pressurisation CLOSED limit switch", "valve_fb", {"type": "do_closed", "do": 5}, None),
        ("Fuel pressurisation OPEN limit switch", "valve_fb", {"type": "do_open", "do": 6}, None),
        ("Fuel pressurisation CLOSED limit switch", "valve_fb", {"type": "do_closed", "do": 6}, None),
        ("Chamber pressure switch (>3 bar)", "limit", {"type": "pc_gt", "value": 3.0}, 0),
        ("Flame detector", "status", {"type": "burning"}, 0),
        ("Deluge flow switch", "limit", {"type": "do_open", "do": 8}, None),
        ("OX tank level LOW", "limit", {"type": "mass_lt", "value": 10.0}, 0),
        ("Fuel tank level LOW", "limit", {"type": "mass_lt", "value": 10.0}, 0),
        ("Fire alarm panel OK", "status", {"type": "const", "value": 1}, 1),
        ("Gas detection system OK", "status", {"type": "const", "value": 1}, 1),
        ("UPS on mains", "status", {"type": "const", "value": 1}, None),
        ("Igniter command feedback", "status", {"type": "do_open", "do": 7}, 0),
        ("Spare digital input", "spare", {"type": "const", "value": 0}, None),
    ]
    assert len(d) == N_DI
    return [{"index": i, "id": f"DI-{i:02d}", "name": n, "kind": k, "sim": s,
             "expected_precheck": e} for i, (n, k, s, e) in enumerate(d)]


def default_config():
    return {
        "meta": {
            "project": "ROCKET ENGINE TEST STAND - DAQ acquisition and flow control SIMULATION",
            "config_version": 1,
            "disclaimer": "SIMULATION ONLY. All calibrations, set-points and limits are "
                          "DEMONSTRATION values. Not validated for any real engine or hardware.",
        },
        "acquisition": {
            "backend": "simulation",
            "slow_rate_hz": 1000.0, "slow_block_samples": 20,
            "hf_rate_hz": 50000.0, "hf_block_samples": 5000,
            "hf_buffer_s": 2.0, "trend_history_s": 120.0,
        },
        "slow_channels": slow_channels(),
        "hf_channels": hf_channels(),
        "digital_inputs": digital_inputs(),
        "digital_outputs": digital_outputs(),
        "analog_outputs": [
            {"index": 0, "id": "AO-00", "name": "Oxidizer control valve command (VIRTUAL)",
             "min_v": 0.0, "max_v": 10.0, "safe_v": 0.0},
            {"index": 1, "id": "AO-01", "name": "Fuel control valve command (VIRTUAL)",
             "min_v": 0.0, "max_v": 10.0, "safe_v": 0.0},
        ],
        "flow_control": {
            "target_total_flow_kg_s": 2.5, "target_mixture_ratio": 2.0,
            "ox_flow_channel": "FT-01", "fuel_flow_channel": "FT-02",
            "pid_ox": {"kp": 2.5, "ki": 22.0, "kd": 0.0},
            "pid_fuel": {"kp": 5.0, "ki": 44.0, "kd": 0.0},
            "derivative_filter_hz": 10.0,
            "output_min_v": 0.0, "output_max_v": 10.0,
            "rate_limit_v_per_s": 20.0,
            "settle_band_pct": 3.0, "min_fuel_flow_for_mr_kg_s": 0.02,
        },
        "sequence": {
            "precheck_timeout_s": 10.0, "precheck_stable_s": 1.0,
            "auto_start_from_ready": False,
            "start_duration_s": 2.0, "start_purge_off_s": 0.3, "start_igniter_on_s": 0.4,
            "start_valves_open_s": 0.8, "start_flow_fraction": 0.3,
            "ignition_confirm_bar": 3.0,
            "ramp_duration_s": 3.0, "hold_duration_s": 8.0,
            "shutdown_duration_s": 3.0, "shutdown_valves_close_s": 1.0, "shutdown_vent_open_s": 2.0,
            "precheck_pressure_windows_bar": {"PT-01": [35.0, 45.0], "PT-02": [35.0, 45.0],
                                              "PT-07": [0.0, 2.5], "PT-08": [0.0, 2.5]},
        },
        "safety": {
            "pressure_abort_debounce_samples": 3,
            "stale_time_s": 0.5, "stale_epsilon_v": 1e-9,
            "watchdog_timeout_s": 1.0,
            "estop_di_channel": "DI-00",
        },
        "simulation": {
            "seed": 12345, "p_amb_bar": 1.0, "t_amb_c": 22.0,
            "tank_nominal_bar": 40.0, "tau_pressurise_s": 0.6, "tau_vent_s": 1.2,
            "tank_droop_bar_per_kg_s": 0.8,
            "kmax_ox_kg_s": 3.2, "kmax_fuel_kg_s": 1.6, "dp_nominal_bar": 20.0,
            "tau_valve_s": 0.12, "tau_flow_s": 0.05, "tau_chamber_s": 0.03,
            "pc_hot_bar_per_kg_s": 7.6, "pc_cold_bar_per_kg_s": 0.6,
            "thrust_n_per_bar": 158.0, "initial_prop_mass_kg": 80.0,
            "overpressure_fault_bar": 15.0, "spike_fault_bar": 12.0, "spike_duration_s": 0.05,
            "hf_transient_amp_v": 4.0, "hf_transient_freq_hz": 7000.0, "hf_transient_decay_s": 0.004,
            "hf_instability_freq_hz": 1850.0, "hf_instability_amp_v": 3.0,
        },
        "fft": {"channel": "DP-01", "size": 8192, "window": "hann", "averages": 4,
                "allowed_sizes": [1024, 2048, 4096, 8192, 16384, 32768]},
        "logging": {
            "results_dir": "Results", "csv_rate_hz": 10.0, "write_tdms": True,
            "hf_log_mode": "all",            # all | fft_channel | off
            "flush_interval_s": 1.0, "queue_max_items": 2000, "auto_start_on_precheck": True,
        },
        "gui": {"refresh_hz": 10.0},
    }
