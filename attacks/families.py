"""Category-A value fault families (synthetic perturbations of public weather data).

Each family transforms ONE window of ONE station. The functions are pure: the input
window is never mutated, a new array is returned, and all randomness comes from the
``numpy.random.Generator`` passed in, so a scenario is reproducible from its seed.

Window semantics follow the design spec section 5. Fault sizes are in units of
``sigma`` (the station's sigma_ref); precipitation families ignore ``sigma``.

``realistic_replace`` and ``cycle_shaped`` need per-station context (history / diurnal
shape) and have their own signatures; ``apply_fault`` reaches them through its ``context``
argument. ``network_bias`` is a plain per-station offset (the injector fans it to the
whole pool), so it reuses ``_offset``.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

# Family names are plain strings here so this module does not depend on attacks.schema.
Params = dict[str, float]
_Fault = Callable[[np.ndarray, Params, float, np.random.Generator, float], np.ndarray]

BLOCK_STEPS = 12  # intermittent_offset block length: 12 x 5 min = 1 h


def _offset(x, params, sigma, rng, prev):
    return x + params["k"] * sigma


def _drift(x, params, sigma, rng, prev):
    return x + np.linspace(0.0, params["k"] * sigma, len(x))


def _spike_positions(n_points: int, n_spikes: int, rng: np.random.Generator) -> np.ndarray:
    """n distinct positions, mutually non-adjacent whenever the window is long enough.

    Non-adjacent sampling is uniform: draw n sorted distinct slots from
    ``n_points - (n_spikes - 1)`` and shift the i-th by i.
    """
    slots = n_points - (n_spikes - 1)
    if slots >= n_spikes:
        base = np.sort(rng.choice(slots, size=n_spikes, replace=False))
        return base + np.arange(n_spikes)
    return rng.choice(n_points, size=n_spikes, replace=False)  # too short to isolate


def _spike(x, params, sigma, rng, prev):
    n_spikes = min(int(params["n"]), len(x))
    if n_spikes <= 0:
        return x
    positions = _spike_positions(len(x), n_spikes, rng)
    signs = np.where(rng.random(n_spikes) < 0.5, -1.0, 1.0)
    out = x.copy()
    out[positions] += signs * params["k"] * sigma
    return out


def _noise(x, params, sigma, rng, prev):
    return x + rng.normal(0.0, params["k"] * sigma, size=len(x))


def _flatline(x, params, sigma, rng, prev):
    window = max(int(params["w_steps"]), 1)
    smooth = pd.Series(x).rolling(window, center=True, min_periods=1).mean().to_numpy()
    return np.where(np.isnan(x), np.nan, smooth)  # a missing cell stays missing


def _stuck(x, params, sigma, rng, prev):
    if not np.isfinite(prev):
        raise ValueError("stuck needs a finite `prev` (last value before the window)")
    return np.full(len(x), prev, dtype=np.float64)


def _gain(x, params, sigma, rng, prev):
    centre = np.nanmean(x)
    return centre + params["g"] * (x - centre)


def _intermittent_offset(x, params, sigma, rng, prev):
    faulted = (np.arange(len(x)) // BLOCK_STEPS) % 2 == 0  # blocks 0, 2, 4, ...
    return x + faulted * (params["k"] * sigma)


def _rain_scale(x, params, sigma, rng, prev):
    return x * params["f"]


def _rain_suppress(x, params, sigma, rng, prev):
    return np.where(np.isnan(x), np.nan, 0.0)


def _triangle(duration: int, total_mm: float) -> np.ndarray:
    """Triangular hyetograph: strictly positive, symmetric, peaking mid-event, sums to total."""
    mid = duration / 2.0
    weights = 1.0 - np.abs(np.arange(duration) + 0.5 - mid) / mid  # min weight is 1/duration
    return weights * (total_mm / weights.sum())


def _rain_inject(x, params, sigma, rng, prev):
    duration = min(max(int(params["duration_steps"]), 1), len(x))
    start = int(rng.integers(0, len(x) - duration + 1))
    out = x.copy()
    out[start : start + duration] += _triangle(duration, params["total_mm"])
    return out


_FAULTS: dict[str, _Fault] = {
    "offset": _offset,
    "drift": _drift,
    "spike": _spike,
    "noise": _noise,
    "flatline": _flatline,
    "stuck": _stuck,
    "multi_offset": _offset,  # per-station effect is a plain offset; the injector fans out stations
    "gain": _gain,
    "intermittent_offset": _intermittent_offset,
    "rain_scale": _rain_scale,
    "rain_suppress": _rain_suppress,
    "rain_inject": _rain_inject,
    "network_bias": _offset,  # per-station offset; injector applies it to the whole pool
}

DAY_STEPS = 288  # 5-min bins per day


def realistic_replace(
    x: np.ndarray,
    params: Params,
    sigma: float,
    rng: np.random.Generator,
    *,
    history: np.ndarray,
    tod_index: np.ndarray,
) -> np.ndarray:
    """Replace the window with values drawn from the station's OWN recent history.

    Marginal/temporal statistics look right (so a purely single-station detector is
    fooled), but the series is generated in isolation and ignores what the region
    actually did in this window, so spatial/cross-source detectors still catch it.
    ``params``: ``k`` (bias in sigma units) and ``generator`` (0 = empirical resample by
    time-of-day, 1 = seasonal-mean + AR(1) residual). ``history`` is the station's clean
    values immediately before the window; ``tod_index`` the window's 5-min-of-day bins.
    """
    x = np.asarray(x, dtype=np.float64)
    history = np.asarray(history, dtype=np.float64)
    hist = history[np.isfinite(history)]
    bias = params["k"] * sigma
    if hist.size == 0:
        return np.full(len(x), bias, dtype=np.float64)
    # time-of-day bin of each history sample (history ends just before the window)
    hist_tod = (int(tod_index[0]) - len(history) + np.arange(len(history))) % DAY_STEPS
    hist_tod = hist_tod[np.isfinite(history)]
    if int(round(params.get("generator", 0.0))) == 1:
        out = _ar1_generate(hist, hist_tod, tod_index, rng)
    else:
        out = _empirical_generate(hist, hist_tod, tod_index, rng)
    return out + bias


def _empirical_generate(hist, hist_tod, tod_index, rng):
    by_tod = {t: hist[hist_tod == t] for t in np.unique(hist_tod)}
    out = np.empty(len(tod_index), dtype=np.float64)
    for i, t in enumerate(tod_index):
        pool = by_tod.get(int(t) % DAY_STEPS)
        pool = pool if pool is not None and pool.size else hist
        out[i] = pool[rng.integers(pool.size)]
    return out


def _ar1_generate(hist, hist_tod, tod_index, rng):
    tod_mean = {t: hist[hist_tod == t].mean() for t in np.unique(hist_tod)}
    mean_series = np.array([tod_mean.get(int(t) % DAY_STEPS, hist.mean()) for t in tod_index])
    resid = hist - np.array([tod_mean[t] for t in hist_tod])
    sd = resid.std()
    if resid.size > 1 and sd > 0:
        phi = float(np.clip(np.corrcoef(resid[:-1], resid[1:])[0, 1], -0.95, 0.95))
    else:
        phi = 0.0
    innov_sd = sd * np.sqrt(max(1.0 - phi * phi, 1e-6))
    e = np.empty(len(tod_index))
    prev = 0.0
    for i in range(len(tod_index)):
        prev = phi * prev + rng.normal(0.0, innov_sd)
        e[i] = prev
    return mean_series + e


def value_replay(
    x: np.ndarray,
    params: Params,
    sigma: float,
    rng: np.random.Generator,
    *,
    history: np.ndarray,
    tod_index: np.ndarray,
) -> np.ndarray:
    """Replace the window with a VERBATIM past window of the station, plus optional noise.

    A real past day's readings have perfect marginal and temporal statistics, so this is a
    strong evasion case (inspired by replay-with-noise attacks). ``params``: ``days_back`` (how
    many days ago to copy, keeping the time-of-day aligned) and ``noise`` (added N(0, (noise·σ)²),
    0 = exact replay). ``history`` is the station's clean values immediately before the window.
    """
    x = np.asarray(x, dtype=np.float64)
    history = np.asarray(history, dtype=np.float64)
    n = len(x)
    start = len(history) - int(round(params["days_back"])) * DAY_STEPS
    if start < 0 or start + n > len(history):  # not enough history: use the earliest aligned window
        start = max(0, len(history) - n)
    past = history[start:start + n]
    if len(past) < n:  # extremely short history: tile to length
        past = np.resize(past, n) if len(past) else np.zeros(n)
    noise = params.get("noise", 0.0) * sigma
    return past + (rng.normal(0.0, noise, n) if noise else 0.0)


def cycle_shaped(
    x: np.ndarray,
    params: Params,
    sigma: float,
    rng: np.random.Generator,
    *,
    diurnal: np.ndarray,
) -> np.ndarray:
    """Add a bias of ``k * sigma`` modulated by the station's diurnal shape.

    ``diurnal`` is the normalised (0..1) mean daily shape evaluated at the window's steps,
    so the injected change is largest when the real signal naturally swings most and rides
    inside the expected daily cycle. A flat (zero) diurnal shape leaves the window unchanged.
    """
    x = np.asarray(x, dtype=np.float64)
    return x + params["k"] * sigma * np.asarray(diurnal, dtype=np.float64)


_CONTEXT_FAULTS = {
    "realistic_replace": realistic_replace,
    "cycle_shaped": cycle_shaped,
    "value_replay": value_replay,
}
CONTEXT_FAMILIES = tuple(_CONTEXT_FAULTS)  # families that need a context dict
FAMILIES = tuple(_FAULTS) + CONTEXT_FAMILIES


def apply_fault(
    family: str,
    x: np.ndarray,
    params: Params,
    sigma: float,
    rng: np.random.Generator,
    prev: float,
    *,
    context: dict | None = None,
) -> np.ndarray:
    """Return the faulted copy of window ``x`` (same length; ``x`` is not modified).

    ``prev`` is the last clean value before the window; only ``stuck`` uses it.
    ``realistic_replace`` and ``cycle_shaped`` need extra per-station inputs, supplied in
    ``context`` (``history`` + ``tod_index``, or ``diurnal``). Physical clipping is a
    separate step (``clip_physical``).
    """
    x = np.asarray(x)
    if x.ndim != 1:
        raise ValueError(f"expected a 1-D window, got shape {x.shape}")
    out_dtype = np.result_type(x.dtype, np.float32)  # float32 stays float32
    if family in _CONTEXT_FAULTS:
        if context is None:
            raise ValueError(f"family {family!r} needs a context dict ({_CONTEXT_FAULTS[family].__name__})")
        out = _CONTEXT_FAULTS[family](x.astype(np.float64), params, float(sigma), rng, **context)
    else:
        try:
            fault = _FAULTS[family]
        except KeyError:
            raise ValueError(
                f"family {family!r} is not an implemented value family; choose from {FAMILIES}"
            ) from None
        out = fault(x.astype(np.float64), params, float(sigma), rng, float(prev))
    return np.asarray(out, dtype=out_dtype).copy()


def clip_physical(variable: str, x: np.ndarray) -> np.ndarray:
    """Clip to the physical range: RH to [0, 100], precipitation to >= 0, temperature as is."""
    if variable == "relative_humidity":
        return np.clip(x, 0, 100)
    if variable == "precipitation":
        return np.maximum(x, 0)
    if variable == "air_temperature":
        return np.array(x, copy=True)
    raise ValueError(f"unknown variable {variable!r}")


def delta_onset(family: str, injected_delta: np.ndarray, sigma: float) -> int:
    """Index of the first step where ``|delta| >= sigma``; only drift has a late onset.

    Returns 0 for every other family, and ``len(injected_delta)`` for a drift whose ramp
    never reaches 1 sigma inside the window (the caller should treat that as "no onset").
    """
    if family != "drift":
        return 0
    reached = np.flatnonzero(np.abs(injected_delta) >= sigma)
    return int(reached[0]) if len(reached) else len(injected_delta)
