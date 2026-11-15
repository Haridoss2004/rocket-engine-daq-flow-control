# Test summary - RUN_20261115_163548_001

**SIMULATED TEST - no real engine hardware was involved.**

- Log start: 2026-11-15T16:35:48.430681
- Log end: 2026-11-15T16:36:08.810681
- Total duration: 20.38 s
- Maximum recorded pressure: 41.19 bar on PT-11 at t = 12.56 s
- Maximum recorded temperature: 498.6 degC on TT-01 at t = 16.54 s
- Abort status: no abort

## Flow and mixture-ratio statistics

| Quantity | Mean | Min | Max | Std |
|---|---|---|---|---|
| Total flow (controller active) | 2.0797 kg/s | 0.0005 | 2.5026 | 0.6731 |
| Total flow (HOLD) | 2.4975 kg/s | 2.3930 | 2.5026 | 0.0115 |
| Mixture ratio (controller active) | 1.9993  | 1.9554 | 2.0123 | 0.0037 |
| Mixture ratio (HOLD) | 1.9998  | 1.9941 | 2.0051 | 0.0021 |

## Abort events

- none

## Recorded alarms

- none

## State transitions

- t = 1.000 s: IDLE -> PRE-CHECK
- t = 3.300 s: PRE-CHECK -> READY
- t = 3.360 s: READY -> START SEQUENCE (SIMULATED)
- t = 5.360 s: START SEQUENCE (SIMULATED) -> RAMP
- t = 8.380 s: RAMP -> HOLD
- t = 16.400 s: HOLD -> SHUTDOWN (SIMULATED)
- t = 19.400 s: SHUTDOWN (SIMULATED) -> COMPLETE

## Logger statistics

- Slow samples logged: 1,020,000
- High-frequency samples logged: 25,500,000
- CSV rows: 10,200  |  control rows: 1,020  |  events: 11
- Dropped queue items: 0  |  queue high-water mark: 4
- Bytes written: 119,839,945
