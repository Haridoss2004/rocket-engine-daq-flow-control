# Design notes

Assumptions, design decisions and verification for the rocket engine test stand DAQ and flow-control
simulation.

## 1. Requirement coverage (LabVIEW program)

| Requirement | How it is met | Evidence |
| :-- | :-- | :-- |
| Acquire all channels and scale them | 50 slow channels generated as raw volts at 1 kS/s and scaled with `eng = volts × gain + offset`; 25 fast channels at 50 kS/s; 30 digital inputs; 30 virtual digital outputs; 2 virtual 0–10 V outputs | 4.200 V → 40.00 bar; 2.602 V → 20.02 bar |
| One control loop for total flow and mixture ratio | Targets split into one flow set-point per propellant; one PI loop per valve; 0–10 V clamp; anti-windup; 20 V/s rate limit | HOLD at 2.50 kg/s and 2.00; follows a change to 1.8 / 2.4; saturates at 10.00 V on an unreachable target and recovers |
| Test sequence | IDLE, PRE-CHECK, READY, START, RAMP, HOLD, SHUTDOWN, COMPLETE, ABORT | All states in order; start refused while a pre-check fails |
| Log data with timestamps | One row per 1 ms sample: time, state, abort code, set-point, flows, valve commands, 50 raw and 50 scaled values | 54,700 rows × 108 columns in the test run |
| Live slow data and FFT of one fast channel | Strip chart, channel arrays, waveform graph, FFT graph, dominant frequency | 1200 Hz at amplitude 1.001 (expected 1.0); injected 1850 Hz tone found at 3.000 |
| Abort if any pressure exceeds a limit | All 20 pressure channels are checked on every sample with a 3-sample debounce. Chamber channels use the operator-set limit; tank, line and injector channels use 50 bar | Chamber overpressure and tank overpressure both abort within one loop iteration; valve commands 0 V; reason shown as text |

Also implemented: abort on pressure-sensor failure (out of range, frozen), E-stop, failed ignition and
manual abort; reset refused while the cause is still present.

## 2. Assumptions

**Simulation.** Driver-level simulated DAQ devices return fixed test waveforms and do not respond to
output commands, so a control loop and an abort chain cannot be exercised against them. The simulator
here is a plant model (tanks, valves, flows, chamber pressure, temperature, thrust) that reacts to the
valve commands, plus a sensor layer that produces raw volts with noise. Simulating at the voltage level
means scaling, range checks and fault detection run on the same data path they would use on hardware.

**Sensors.** Voltage-output sensors on a 1–9 V "live zero" span. A healthy sensor never reads near 0 V,
so an open circuit shows up as an out-of-range reading. Scaling is linear.

**Slow channels.** 50 × 1 kS/s = 50 kS/s aggregate, within reach of a single multiplexed device.
Multiplexing adds an inter-channel delay, acceptable for signals that change over tens of milliseconds.
Data is read in blocks, not point by point.

**Fast channels.** 25 × 50 kS/s = 1.25 MS/s. These need simultaneous sampling with anti-alias filtering,
their own task and their own loop. Such modules offer fixed rates (a 50 kS/s request can be coerced to
51.2 kS/s), so the software takes the actual rate from the driver for the FFT axis. Nyquist is 25 kHz at
50 kS/s; frequency resolution is `fs / N`.

**Control.** Total flow and mixture ratio each depend on both valves. Converting the two targets into one
flow set-point per propellant, `ox = T·MR/(1+MR)` and `fuel = T/(1+MR)`, gives two single-input loops
that are simple to tune. Flow is assumed to be measured by a flow meter on each line. PI is sufficient
for the first-order valve and line dynamics assumed. Gains were tuned against the model.

**Sequence.** Tanks are pressurised during pre-check. Igniter before propellant valves. Start at 30 %
flow, confirm chamber pressure, then ramp. Shutdown ramps the commands down, closes the main valves, then
purges and vents. All timings are parameters.

**Pressure limits.** Each pressure channel has a limit suited to what it measures. The chamber channels
share the operator-set limit (28 bar by default, against 20 bar in HOLD). Tank, feed-line and injector
channels run near 40 bar, so they use a fixed 50 bar limit. One common limit would either trip on the
tanks or be useless for the chamber.

**Abort and safe state.** Safe state: main valves closed, igniter off, pressurisation closed, vents open,
purge on, both analog commands 0 V. An abort is latched and needs an operator reset, which is refused
while the cause is present. Pressure-sensor failures abort as well, because a pressure limit means
nothing if the measurement cannot be trusted. Pressure and sensor conditions abort only while the test is
armed; before that they fail the pre-check. E-stop aborts in any state.

**Software abort is not a safety system.** A program on a desktop operating system has no guaranteed
reaction time. A real stand needs a hardwired E-stop, valves that fail safe on loss of power or signal,
and limit checks on deterministic hardware. The PC then displays, logs and analyses.

## 3. LabVIEW program

One While Loop at 100 ms:

- **Formula Node**: 100 steps of 1 ms per iteration covering the operator command and sequence, virtual
  outputs, plant, 50 sensors, scaling, safety checks, pre-check, the two PI loops and one log row. It then
  generates 5000 samples for each of the 25 fast channels, their RMS, and the selected channel with a Hann
  window. Source: [`../LabVIEW/core_formula_node_source.txt`](../LabVIEW/core_formula_node_source.txt).
- **Feedback Node**: carries the state array to the next iteration.
- **Reshape Array → Write Delimited Spreadsheet**: appends 100 log rows per iteration.
- **Amplitude and Phase Spectrum**: FFT. **Array Max & Min** × df: dominant frequency.
- **Pick Line**: state number and abort code to text.

## 4. Python reference implementation

Same behaviour with the structure intended for a real stand:

| Thread | Period | Job |
| :-- | :-- | :-- |
| Slow acquisition | 20 ms | read → scale → safety → sequencer → PID → virtual outputs → enqueue |
| Fast acquisition | 100 ms | read → ring buffer → enqueue |
| Analysis | 100 ms | FFT of the selected channel, statistics of all 25 |
| Watchdog | 50 ms | abort if no slow data for 1 s |
| Logger | queue driven | CSV and TDMS, flush every second, summary on close |
| GUI | 100 ms | read a snapshot, draw, send commands |

Acquisition hands data to the logger through a bounded queue with a non-blocking put: if the disk cannot
keep up, items are dropped and counted, so acquisition is never blocked and memory cannot grow. A hardware
abstraction layer separates the simulator from an NI-DAQmx backend that reads inputs only.

## 5. Verification

**LabVIEW program** — three scripts drive the running VI through its front panel
(outputs in [`../LabVIEW/Results/`](../LabVIEW/Results/)). All checks pass:

- nominal sequence with every state in order, then reset
- control accuracy in HOLD, set-point change, saturation at 10 V and recovery
- scaling of pressure channels
- FFT on three channels and an injected tone, frequency and amplitude
- aborts: chamber overpressure, tank overpressure, lowered limit, sensor disconnected, sensor frozen, E-stop, manual
- reset refused while a fault is active; start blocked by a failed pre-check
- operator stop and cancel
- digital output states in HOLD and in ABORT
- state name and abort reason text
- log file size and shape

**Python implementation** — 106 automated tests, including a 10-second real-time run logging 1.25 MS/s
plus 50 kS/s with no dropped data. Sample output of a nominal run and an aborted run:
[`sample_run/`](sample_run/).

## 6. Limitations

- The LabVIEW program is a single loop; logging and display share it with control.
- Its outputs update every 100 ms, although the simulated control and abort logic run at 1 ms inside it.
- Fast-channel data is displayed and analysed but not logged by the LabVIEW program.
- Commands, faults and E-stop are entered as numbers; digital I/O is shown as a numeric array.
- The FFT graph axis is in bins (1 bin = 10 Hz); the dominant frequency is shown in Hz.
- The plant model is illustrative. Nothing here has been run on hardware.
