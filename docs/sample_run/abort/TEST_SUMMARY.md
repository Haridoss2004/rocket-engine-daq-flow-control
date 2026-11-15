# Test summary - RUN_20261115_163609_002

**SIMULATED TEST - no real engine hardware was involved.**

- Log start: 2026-11-15T16:36:09.334764
- Log end: 2026-11-15T16:36:20.194764
- Total duration: 10.86 s
- Maximum recorded pressure: 41.20 bar on PT-12 at t = 27.84 s
- Maximum recorded temperature: 407.4 degC on TT-01 at t = 31.32 s
- Abort status: ABORTED

## Flow and mixture-ratio statistics

| Quantity | Mean | Min | Max | Std |
|---|---|---|---|---|
| Total flow (controller active) | 1.6769 kg/s | -0.0006 | 2.5027 | 0.7456 |
| Total flow (HOLD) | 2.4892 kg/s | 2.3814 | 2.5027 | 0.0236 |
| Mixture ratio (controller active) | 1.9989  | 1.9834 | 2.0193 | 0.0045 |
| Mixture ratio (HOLD) | 1.9994  | 1.9925 | 2.0043 | 0.0021 |

## Abort events

- t = 31.260 s (2026-11-15T16:36:18.654764): [SENSOR_FAILURE] PT-07 Chamber pressure A: pressure sensor failure - out of range / disconnected

## Recorded alarms

- t = 31.260 s: PT-07 Chamber pressure A: OUT_OF_RANGE
- t = 31.300 s: Reset rejected: an abort condition is still active: PT-07 Chamber pressure A: pressure sensor failure - out of range / disconnected

## State transitions

- t = 21.920 s: IDLE -> PRE-CHECK
- t = 24.180 s: PRE-CHECK -> READY
- t = 24.200 s: READY -> START SEQUENCE (SIMULATED)
- t = 26.200 s: START SEQUENCE (SIMULATED) -> RAMP
- t = 29.200 s: RAMP -> HOLD
- t = 31.260 s: HOLD -> ABORT
- t = 31.800 s: ABORT -> IDLE

## Logger statistics

- Slow samples logged: 544,000
- High-frequency samples logged: 13,625,000
- CSV rows: 5,400  |  control rows: 544  |  events: 21
- Dropped queue items: 0  |  queue high-water mark: 5
- Bytes written: 11,673,881
