"""Engineering-unit scaling and per-channel health evaluation.

    Engineering Value = Measured Voltage x Calibration Gain + Offset
"""
from __future__ import annotations

from enum import IntEnum

import numpy as np


class ChStatus(IntEnum):
    OK = 0
    WARN_LOW = 1
    WARN_HIGH = 2
    ABORT_HIGH = 3      # above the configured demonstration abort threshold
    OUT_OF_RANGE = 4    # raw voltage outside the valid sensor span (e.g. disconnected)
    INVALID = 5         # NaN or Infinity
    STALE = 6           # value frozen for longer than stale_time_s
    DISABLED = 7


FAILED_STATES = (ChStatus.OUT_OF_RANGE, ChStatus.INVALID, ChStatus.STALE)


def scale(raw, gain, offset):
    """Vectorised linear scaling. raw: (..., n_ch)."""
    return raw * gain + offset


class SlowChannelProcessor:
    """Scales a block of slow samples and classifies the health of every channel."""

    def __init__(self, channels, fs, stale_time_s=0.5, stale_eps=1e-9):
        self.fs = fs
        self.n = len(channels)
        self.stale_samples = max(2, int(stale_time_s * fs))
        self.stale_eps = stale_eps
        self._prev = np.full(self.n, np.nan)
        self._run = np.zeros(self.n, dtype=np.int64)     # consecutive identical samples
        self.load_channels(channels)

    def load_channels(self, channels):
        """(Re)load calibration and limits - called when the operator edits the config."""
        f = lambda key, default: np.array(
            [default if c.get(key) is None else c[key] for c in channels], dtype=float)
        self.gain = f("gain", 1.0)
        self.offset = f("offset", 0.0)
        self.raw_min = f("raw_valid_min_v", -np.inf)
        self.raw_max = f("raw_valid_max_v", np.inf)
        self.warn_lo = f("warn_lo", -np.inf)
        self.warn_hi = f("warn_hi", np.inf)
        self.abort_hi = f("abort_hi", np.inf)
        self.enabled = np.array([c.get("enabled", True) for c in channels], dtype=bool)

    def process(self, raw):
        """raw: (n_samples, n_ch) volts -> (eng block, status vector for the block)."""
        eng = scale(raw, self.gain, self.offset)
        finite = np.isfinite(raw)
        invalid = ~finite.all(axis=0)
        with np.errstate(invalid="ignore"):
            oor = ((raw < self.raw_min) | (raw > self.raw_max)).any(axis=0)
            mx = np.where(finite, eng, -np.inf).max(axis=0)
            mn = np.where(finite, eng, np.inf).min(axis=0)
            # stale detection: count consecutive samples that did not change at all
            ext = np.vstack([self._prev[None, :], raw])
            same = np.abs(np.diff(ext, axis=0)) <= self.stale_eps        # NaN compares False
        all_same = same.all(axis=0)
        trailing = np.where(all_same, raw.shape[0],
                            np.argmax(~same[::-1], axis=0))              # trailing identical run
        self._run = np.where(all_same, self._run + raw.shape[0], trailing)
        self._prev = raw[-1].copy()
        stale = self._run >= self.stale_samples

        st = np.full(self.n, ChStatus.OK, dtype=np.int8)
        st[mn < self.warn_lo] = ChStatus.WARN_LOW
        st[mx > self.warn_hi] = ChStatus.WARN_HIGH
        st[mx > self.abort_hi] = ChStatus.ABORT_HIGH
        st[stale] = ChStatus.STALE
        st[oor] = ChStatus.OUT_OF_RANGE
        st[invalid] = ChStatus.INVALID
        st[~self.enabled] = ChStatus.DISABLED
        return eng, st


class RingBuffer:
    """Fixed-size multi-channel ring buffer (bounded memory, single writer)."""

    def __init__(self, capacity, n_ch, dtype=np.float32):
        self.capacity = int(capacity)
        self.buf = np.zeros((self.capacity, n_ch), dtype=dtype)
        self.pos = 0
        self.total = 0

    def write(self, block):
        n = block.shape[0]
        if n >= self.capacity:
            self.buf[:] = block[-self.capacity:]
            self.pos = 0
        else:
            end = self.pos + n
            if end <= self.capacity:
                self.buf[self.pos:end] = block
            else:
                k = self.capacity - self.pos
                self.buf[self.pos:] = block[:k]
                self.buf[:end - self.capacity] = block[k:]
            self.pos = end % self.capacity
        self.total += n

    def latest(self, n, channel=None):
        """Copy of the newest n samples (oldest first). Fewer if not yet filled."""
        n = min(n, self.capacity, self.total)
        idx = (self.pos - n + np.arange(n)) % self.capacity
        return self.buf[idx] if channel is None else self.buf[idx, channel]
