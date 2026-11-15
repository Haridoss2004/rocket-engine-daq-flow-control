"""DAQ hardware abstraction layer (HAL) - common interface.

The application only ever talks to a DaqBackend. Two implementations exist:
  * SimulationBackend (Simulation/sim_backend.py)  - DEFAULT, no hardware needed.
  * NiDaqmxInputBackend (nidaqmx_backend.py)       - future option, INPUTS ONLY.

Outputs: the interface deliberately has no "write to hardware" method.
apply_virtual_outputs() hands the virtual DO/AO states to the backend so a
simulated plant can react to them; a hardware backend must ignore them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class DaqError(RuntimeError):
    """Critical acquisition error (maps to a DAQmx error in a hardware backend)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class PhysicalOutputDisabledError(RuntimeError):
    """Raised by any attempt to drive a physical output. Outputs are virtual by design."""


class DaqBackend(ABC):
    name = "abstract"
    is_simulation = True
    physical_outputs_enabled = False     # never True in this project

    @abstractmethod
    def open(self): ...

    @abstractmethod
    def close(self): ...

    @abstractmethod
    def is_ready(self) -> bool: ...

    @abstractmethod
    def read_slow(self, n_samples):
        """Return ndarray (n_samples, 50) of raw volts."""

    @abstractmethod
    def read_hf(self, n_samples):
        """Return ndarray (n_samples, 25) of raw volts."""

    @abstractmethod
    def read_di(self):
        """Return ndarray (30,) bool."""

    def apply_virtual_outputs(self, do_states, ao_volts):
        """Virtual outputs for a simulated plant. Hardware backends ignore this."""

    def discover_devices(self):
        return []

    def write_physical_outputs(self, *_, **__):
        raise PhysicalOutputDisabledError(
            "Physical output operations are disabled in this project. All DO/AO are virtual.")
