"""FFT spectrum analysis for one high-frequency channel.

Key relations (fs = sampling frequency, N = FFT size):
    Nyquist frequency     = fs / 2            (50 kS/s -> 25 kHz)
    Frequency resolution  = fs / N            (50 kS/s, N = 8192 -> 6.10 Hz per bin)
A Hann window is applied to limit spectral leakage; the amplitude is corrected
by the window's coherent gain so a sine of amplitude A reads A at its peak.
"""
from __future__ import annotations

import numpy as np


def nyquist(fs):
    return fs / 2.0


def frequency_resolution(fs, n_fft):
    return fs / n_fft


def window_function(name, n):
    if name == "hann":
        return np.hanning(n + 1)[:-1]          # periodic Hann, preferred for spectral analysis
    if name in ("rect", "none"):
        return np.ones(n)
    raise ValueError(f"unsupported window '{name}'")


def amplitude_spectrum(x, fs, window="hann"):
    """Single-sided amplitude spectrum (peak units) of x. Returns (freqs, magnitude)."""
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    w = window_function(window, n)
    x = x - x.mean()                           # remove DC so it cannot mask the dominant tone
    spec = np.fft.rfft(x * w)
    mag = np.abs(spec) * (2.0 / w.sum())       # coherent-gain correction
    mag[0] *= 0.5
    return np.fft.rfftfreq(n, d=1.0 / fs), mag


def find_peak(freqs, mag, f_min=0.0):
    """Dominant frequency with parabolic interpolation between bins."""
    lo = int(np.searchsorted(freqs, f_min))
    lo = max(lo, 1)
    if mag.size <= lo + 1:
        return 0.0, 0.0
    k = lo + int(np.argmax(mag[lo:]))
    df = freqs[1] - freqs[0]
    if 0 < k < mag.size - 1:
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        den = a - 2 * b + c
        d = 0.5 * (a - c) / den if den != 0 else 0.0
        return float(freqs[k] + d * df), float(b - 0.25 * (a - c) * d)
    return float(freqs[k]), float(mag[k])


class FftAnalyzer:
    """Stateful analyzer with optional spectrum averaging."""

    def __init__(self, fs, size=8192, window="hann", averages=1):
        self.fs = float(fs)
        self.configure(size, window, averages)

    def configure(self, size=None, window=None, averages=None):
        if size is not None:
            if size < 16 or size & (size - 1):
                raise ValueError("FFT size must be a power of two >= 16")
            self.size = int(size)
        if window is not None:
            window_function(window, 8)
            self.window = window
        if averages is not None:
            self.averages = max(1, int(averages))
        self._avg = None
        self._count = 0

    @property
    def resolution_hz(self):
        return frequency_resolution(self.fs, self.size)

    @property
    def nyquist_hz(self):
        return nyquist(self.fs)

    def analyze(self, samples):
        """samples: 1-D array with at least `size` points (newest last). Returns dict or None."""
        if samples.size < self.size or not np.isfinite(samples[-self.size:]).all():
            return None
        x = samples[-self.size:]
        freqs, mag = amplitude_spectrum(x, self.fs, self.window)
        if self._avg is None or self._avg.size != mag.size or self.averages == 1:
            self._avg, self._count = mag, 1
        else:                                   # exponential (running) average
            self._count = min(self._count + 1, self.averages)
            self._avg = self._avg + (mag - self._avg) / self._count
        pf, pa = find_peak(freqs, self._avg)
        return {"freqs": freqs, "mag": self._avg, "peak_freq_hz": pf, "peak_amp": pa,
                "resolution_hz": self.resolution_hz, "nyquist_hz": self.nyquist_hz,
                "size": self.size, "window": self.window,
                "rms": float(np.sqrt(np.mean((x - x.mean()) ** 2)))}


def signal_stats(x):
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0 or not np.isfinite(x).all():
        return {"mean": float("nan"), "rms": float("nan"), "pk_pk": float("nan"),
                "min": float("nan"), "max": float("nan"), "crest": float("nan")}
    ac = x - x.mean()
    rms = float(np.sqrt(np.mean(ac ** 2)))
    return {"mean": float(x.mean()), "rms": rms, "pk_pk": float(x.max() - x.min()),
            "min": float(x.min()), "max": float(x.max()),
            "crest": float(np.abs(ac).max() / rms) if rms > 0 else 0.0}
