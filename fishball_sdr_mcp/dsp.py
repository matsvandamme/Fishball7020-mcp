"""Spectrum analysis for captured IQ.

numpy is used when it is importable and a pure-Python radix-2 FFT is used
otherwise, so the server runs with only the MCP SDK installed. At the transform
sizes here (1k-16k) the fallback costs a fraction of a second, which is nothing
next to the capture itself.
"""

from __future__ import annotations

import cmath
import math
from typing import Sequence

try:                                            # pragma: no cover - env dependent
    import numpy as _np
except ImportError:                             # pragma: no cover
    _np = None

HAVE_NUMPY = _np is not None


def _fft_python(x: list[complex]) -> list[complex]:
    """Iterative radix-2 Cooley-Tukey. len(x) must be a power of two."""
    n = len(x)
    x = list(x)
    j = 0
    for i in range(1, n):                       # bit-reversal permutation
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    length = 2
    while length <= n:
        step = cmath.exp(-2j * math.pi / length)
        half = length // 2
        for start in range(0, n, length):
            w = 1 + 0j
            for k in range(start, start + half):
                u, v = x[k], x[k + half] * w
                x[k], x[k + half] = u + v, u - v
                w *= step
        length <<= 1
    return x


def interleaved_to_complex(values: Sequence[int]) -> list[complex]:
    """int16 I,Q,I,Q,... -> complex samples."""
    return [complex(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]


def _hann(n: int) -> list[float]:
    if n < 2:
        return [1.0] * n
    return [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]


def spectrum(samples: Sequence[complex], sample_rate: float, center_hz: float,
             ) -> tuple[list[float], list[float]]:
    """Return (frequencies_hz, magnitudes_db) sorted by frequency.

    Magnitudes are dB relative to full scale for a 12-bit converter, corrected
    for the analysis window's coherent gain so a tone reads at its true level.
    """
    n = 1 << max(1, len(samples).bit_length() - 1)     # largest power of two
    block = list(samples[:n])
    window = _hann(n)
    coherent_gain = sum(window) / n
    full_scale = 2047.0                                # AD9361 is 12-bit

    if HAVE_NUMPY:
        arr = _np.asarray(block, dtype=_np.complex128) * _np.asarray(window)
        spec = _np.fft.fftshift(_np.fft.fft(arr))
        freqs = _np.fft.fftshift(_np.fft.fftfreq(n, 1.0 / sample_rate)) + center_hz
        mags = 20.0 * _np.log10(
            _np.abs(spec) / (n * coherent_gain * full_scale) + 1e-15)
        return freqs.tolist(), mags.tolist()

    spec = _fft_python([block[i] * window[i] for i in range(n)])
    out: list[tuple[float, float]] = []
    for k in range(n):
        f = (k if k < n // 2 else k - n) * sample_rate / n
        mag = abs(spec[k]) / (n * coherent_gain * full_scale)
        out.append((center_hz + f, 20.0 * math.log10(mag + 1e-15)))
    out.sort(key=lambda t: t[0])
    return [f for f, _ in out], [d for _, d in out]


def find_peaks(freqs: Sequence[float], mags: Sequence[float], limit: int = 10,
               min_separation_hz: float = 50e3) -> list[tuple[float, float]]:
    """Strongest bins, thinned so one strong signal cannot fill the whole list."""
    order = sorted(range(len(mags)), key=lambda i: -mags[i])
    picked: list[tuple[float, float]] = []
    for i in order:
        f = freqs[i]
        if any(abs(f - pf) < min_separation_hz for pf, _ in picked):
            continue
        picked.append((f, mags[i]))
        if len(picked) >= limit:
            break
    return picked


def noise_floor_db(mags: Sequence[float]) -> float:
    """Median bin level - a robust stand-in for the noise floor."""
    ordered = sorted(mags)
    mid = len(ordered) // 2
    if not ordered:
        return float("nan")
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def iq_statistics(samples: Sequence[complex]) -> dict:
    """Level and health figures for a capture, cheap enough to always include."""
    if not samples:
        return {"samples": 0}
    n = len(samples)
    mean_i = sum(s.real for s in samples) / n
    mean_q = sum(s.imag for s in samples) / n
    power = sum((s.real - mean_i) ** 2 + (s.imag - mean_q) ** 2 for s in samples) / n
    peak = max(max(abs(s.real), abs(s.imag)) for s in samples)
    full_scale = 2047.0
    rms = math.sqrt(power)
    # The AD9361 is 12-bit, so |sample| can reach 2047 before the converter
    # clips. Counting samples at the rail is the honest way to report it.
    clipped = sum(1 for s in samples
                  if abs(s.real) >= full_scale or abs(s.imag) >= full_scale)
    return {
        "samples": n,
        "rms": round(rms, 2),
        "rms_dbfs": round(20 * math.log10(rms / full_scale + 1e-15), 2),
        "peak": int(peak),
        "peak_dbfs": round(20 * math.log10(peak / full_scale + 1e-15), 2),
        "dc_offset_i": round(mean_i, 2),
        "dc_offset_q": round(mean_q, 2),
        "clipped_samples": clipped,
    }


def ascii_spectrum(freqs: Sequence[float], mags: Sequence[float],
                   width: int = 64, height: int = 12) -> str:
    """A compact plot. Bounded output, so it is safe to inline in a response."""
    if not freqs:
        return "(no data)"
    buckets: list[float] = []
    per = max(1, len(mags) // width)
    for i in range(0, len(mags), per):
        buckets.append(max(mags[i:i + per]))
    buckets = buckets[:width]
    top, bottom = max(buckets), min(buckets)
    span = max(top - bottom, 1e-9)
    rows = []
    for r in range(height):
        threshold = top - (r + 0.5) * span / height
        rows.append("".join("#" if b >= threshold else " " for b in buckets))
    label_l = f"{freqs[0] / 1e6:.3f} MHz"
    label_r = f"{freqs[-1] / 1e6:.3f} MHz"
    axis = label_l + " " * max(1, width - len(label_l) - len(label_r)) + label_r
    return (f"{top:7.1f} dBFS |" + rows[0] + "\n"
            + "\n".join(" " * 13 + "|" + r for r in rows[1:])
            + f"\n{bottom:7.1f} dBFS +" + "-" * width + "\n" + " " * 14 + axis)
