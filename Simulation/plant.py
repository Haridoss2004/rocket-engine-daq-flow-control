"""Simplified, illustrative test-stand plant model (SIMULATION ONLY).

A lumped first-order model that produces plausible-looking pressures, flows,
temperatures and thrust in response to the VIRTUAL valve commands. It is a
demonstration model, not a validated engine model, and its parameters are not
real engine data.
"""
from __future__ import annotations

import math

import numpy as np

from Configuration.defaults import DO, N_DO

SOURCES = ["p_ox_tank", "p_fuel_tank", "p_ox_line", "p_fuel_line", "p_ox_inj", "p_fuel_inj",
           "p_chamber", "p_purge", "p_pneu", "p_amb", "t_chamber", "t_nozzle", "t_ox", "t_fuel",
           "t_amb", "t_coolant", "thrust", "mass_prop", "flow_ox", "flow_fuel", "pos_ox",
           "pos_fuel", "bus_v", "bus_i", "ign_v", "humidity", "lel", "spare_v"]
SRC_INDEX = {n: i for i, n in enumerate(SOURCES)}


class Plant:
    def __init__(self, sim_cfg):
        self.c = sim_cfg
        self.reset()

    def reset(self):
        c = self.c
        self.p_ox_tank = self.p_fuel_tank = c["p_amb_bar"]
        self.pos_ox = self.pos_fuel = 0.0          # control valve positions 0..1
        self.flow_ox = self.flow_fuel = 0.0        # kg/s
        self.pc = c["p_amb_bar"]
        self.p_purge = c["p_amb_bar"]
        self.burning = False
        self.t_chamber = self.t_nozzle = self.t_ox = self.t_fuel = self.t_coolant = c["t_amb_c"]
        self.mass_prop = c["initial_prop_mass_kg"]
        self.do_lag = np.zeros(N_DO)               # solenoid travel 0..1
        self.extra_pc = 0.0                        # injected overpressure / spike (bar)
        self.time_s = 0.0

    def refill(self):
        self.mass_prop = self.c["initial_prop_mass_kg"]

    def step(self, dt, ao, do, overpressure=False, spike=False):
        """Advance by dt seconds. ao: 2 volts, do: 30 bools. Returns source vector."""
        c = self.c
        amb = c["p_amb_bar"]
        self.time_s += dt
        self.do_lag += (np.asarray(do, dtype=float) - self.do_lag) * min(1.0, dt / 0.03)
        L = self.do_lag

        # --- tanks: pressurise / vent / hold
        for attr, press, vent in (("p_ox_tank", DO["OX_PRESS"], DO["OX_VENT"]),
                                  ("p_fuel_tank", DO["FUEL_PRESS"], DO["FUEL_VENT"])):
            p = getattr(self, attr)
            if L[vent] > 0.5:
                p += (amb - p) * dt / c["tau_vent_s"]
            elif L[press] > 0.5:
                p += (c["tank_nominal_bar"] - p) * dt / c["tau_pressurise_s"]
            setattr(self, attr, p)

        # --- control valves follow the virtual 0-10 V command
        a = dt / c["tau_valve_s"]
        self.pos_ox += (min(max(ao[0], 0.0), 10.0) / 10.0 - self.pos_ox) * a
        self.pos_fuel += (min(max(ao[1], 0.0), 10.0) / 10.0 - self.pos_fuel) * a

        # --- flows through control valve + isolation (main) valve
        p_ox_eff = self.p_ox_tank - c["tank_droop_bar_per_kg_s"] * self.flow_ox
        p_fu_eff = self.p_fuel_tank - c["tank_droop_bar_per_kg_s"] * self.flow_fuel
        has_prop = 1.0 if self.mass_prop > 0.0 else 0.0
        fo_t = (c["kmax_ox_kg_s"] * self.pos_ox * L[DO["OX_MAIN"]] * has_prop
                * math.sqrt(max(p_ox_eff - self.pc, 0.0) / c["dp_nominal_bar"]))
        ff_t = (c["kmax_fuel_kg_s"] * self.pos_fuel * L[DO["FUEL_MAIN"]] * has_prop
                * math.sqrt(max(p_fu_eff - self.pc, 0.0) / c["dp_nominal_bar"]))
        b = dt / c["tau_flow_s"]
        self.flow_ox += (fo_t - self.flow_ox) * b
        self.flow_fuel += (ff_t - self.flow_fuel) * b
        self.mass_prop = max(0.0, self.mass_prop - (self.flow_ox + self.flow_fuel) * dt)

        # --- combustion
        if L[DO["IGNITER"]] > 0.5 and self.flow_ox > 0.10 and self.flow_fuel > 0.05:
            self.burning = True
        if self.burning and (self.flow_ox < 0.05 or self.flow_fuel < 0.03):
            self.burning = False
        k = c["pc_hot_bar_per_kg_s"] if self.burning else c["pc_cold_bar_per_kg_s"]
        pc_t = amb + k * (self.flow_ox + self.flow_fuel)
        self.pc += (pc_t - self.pc) * dt / c["tau_chamber_s"]

        # --- injected disturbances (fault injection)
        extra_t = (c["overpressure_fault_bar"] if overpressure else 0.0) + \
                  (c["spike_fault_bar"] if spike else 0.0)
        self.extra_pc += (extra_t - self.extra_pc) * min(1.0, dt / 0.01)
        pc_out = self.pc + self.extra_pc

        self.p_purge += ((15.0 if L[DO["PURGE"]] > 0.5 else amb) - self.p_purge) * dt / 0.2
        p_ox_line = p_ox_eff - 0.6 * self.flow_ox
        p_fu_line = p_fu_eff - 0.6 * self.flow_fuel
        p_ox_inj = pc_out + 0.25 * max(p_ox_line - pc_out, 0.0) * L[DO["OX_MAIN"]] * self.pos_ox
        p_fu_inj = pc_out + 0.25 * max(p_fu_line - pc_out, 0.0) * L[DO["FUEL_MAIN"]] * self.pos_fuel

        # --- thermal (slow first-order lags), thrust
        ta = c["t_amb_c"]
        hot = self.burning
        tc_t = ta + (26.0 * (self.pc - amb) if hot else 0.0)
        self.t_chamber += (tc_t - self.t_chamber) * dt / (3.0 if tc_t > self.t_chamber else 9.0)
        tn_t = ta + (20.0 * (self.pc - amb) if hot else 0.0)
        self.t_nozzle += (tn_t - self.t_nozzle) * dt / (4.0 if tn_t > self.t_nozzle else 10.0)
        to_t = -150.0 if self.flow_ox > 0.05 else ta
        self.t_ox += (to_t - self.t_ox) * dt / (2.5 if to_t < self.t_ox else 25.0)
        self.t_fuel += (ta + 3.0 * self.flow_fuel - self.t_fuel) * dt / 5.0
        self.t_coolant += (ta + 3.5 * (self.pc - amb) - self.t_coolant) * dt / 4.0
        thrust = c["thrust_n_per_bar"] * max(pc_out - amb, 0.0) * (1.0 if hot else 0.05)

        n_on = float(np.count_nonzero(np.asarray(do)))
        return (self.p_ox_tank, self.p_fuel_tank, p_ox_line, p_fu_line, p_ox_inj, p_fu_inj,
                pc_out, self.p_purge, 8.0, amb, self.t_chamber, self.t_nozzle, self.t_ox,
                self.t_fuel, ta, self.t_coolant, thrust, self.mass_prop, self.flow_ox,
                self.flow_fuel, self.pos_ox, self.pos_fuel, 24.0 - 0.02 * n_on, 1.2 + 0.25 * n_on,
                24.0 * L[DO["IGNITER"]] + 1.0, 45.0, 2.0, 5.0)

    @property
    def combustion_level(self):
        """0..~1.3 intensity used to scale high-frequency signal amplitudes."""
        if not self.burning:
            return 0.0
        return max(0.0, (self.pc + self.extra_pc - self.c["p_amb_bar"]) / 19.0)
