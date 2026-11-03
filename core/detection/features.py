"""Causal feature extraction: the input of the Extra Trees detector.

Every feature uses only the current and *past* frames, so it can be computed on a live stream.
Arrays are shaped ``[time, site, variable]`` (variables: temperature, humidity, rain).

1. Signal helpers: rolling statistics, harmonic calendar terms, persistence/daily forecast,
   CUSUM, neighbour references and physical-plausibility rules.
2. ``FeatureExtractor``: learns frozen normalisation parameters on nominal Jan-Aug 2023 data and
   turns an episode into 37 features per point (``model_inputs``): 23 residual/history/neighbour
   features, three rule-family scores and 11 residual-history features.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist

from core.detection.data import episodes
from core.detection.utils import legacy_module_aliases, read_json, sha256, versions, write_json

FEATURE_NAMES = (
    "z_forecast", "z_anchor", "z_spatial_near", "z_spatial_wider",
    "delta_1", "history_median_6", "history_median_48", "history_mad_6", "history_mad_48",
    "observed_std_6", "observed_std_48", "observed_slope_6", "unchanged_run",
    "near_count", "wider_count", "near_spread", "wider_spread",
    "hour_sin", "hour_cos", "year_sin", "year_cos", "rain_occurrence", "rain_log_amount",
)


def transform(values):
    """Return a float copy of ``values`` with rain mapped to log1p(max(rain, 0)); temperature/humidity are unchanged."""
    out = np.asarray(values, dtype=np.float64).copy()
    # Keep invalid negative raw rain for physics; log features never invent a value.
    out[..., 2] = np.log1p(np.maximum(out[..., 2], 0))
    return out


def calendar(times):
    """Build a design matrix [1, sin/cos hour-of-day, sin/cos day-of-year] for harmonic seasonal references (leap-year aware)."""
    ts = pd.DatetimeIndex(times)
    year_start = pd.to_datetime(ts.year.astype(str) + "-01-01")
    next_start = pd.to_datetime((ts.year + 1).astype(str) + "-01-01")
    year = np.asarray((ts - year_start) / (next_start - year_start)) * 2 * np.pi
    hour = np.asarray(ts.hour + ts.minute / 60) * (2 * np.pi / 24)
    return np.column_stack([np.ones(len(ts)), np.sin(hour), np.cos(hour), np.sin(year), np.cos(year)])


def lag(values, steps):
    """Shift an array ``steps`` frames forward in time, padding the first frames with NaN (value at t = original value at t-steps)."""
    out = np.full_like(values, np.nan)
    if len(values) > steps:
        out[steps:] = values[:-steps]
    return out


def rolling(values, window, strict=False):
    """Rolling median, MAD, standard deviation and least-squares slope over ``window`` frames.

    With ``strict=True`` the window ends at t-1 (excludes the current value), which is
    used for history baselines that the current, possibly attacked, value must not influence.
    """
    source = lag(values, 1) if strict else values
    n, sites, variables = source.shape
    median = np.full_like(source, np.nan)
    mad = np.full_like(source, np.nan)
    std = np.full_like(source, np.nan)
    slope = np.full_like(source, np.nan)
    if n < window:
        return median, mad, std, slope
    # Zero-copy [time-window+1, site, var, window] view of every trailing window.
    view = np.lib.stride_tricks.sliding_window_view(source, window, axis=0)
    med = np.median(view, axis=-1)
    median[window-1:] = med
    mad[window-1:] = np.median(np.abs(view - med[..., None]), axis=-1)
    std[window-1:] = np.std(view, axis=-1)
    # Least-squares slope with a centred time index.
    x = np.arange(window) - (window - 1) / 2
    slope[window-1:] = np.sum(view * x, axis=-1) / np.sum(x*x)
    return median, mad, std, slope


def history_complete(values, window=48):
    """True where the previous ``window`` frames are all finite (the declared 48-step history requirement)."""
    valid = np.isfinite(values).astype(np.int32)
    # Prefix sums give the number of finite values in each trailing window in O(1).
    cumulative = np.concatenate([np.zeros_like(valid[:1]), np.cumsum(valid, axis=0)], axis=0)
    out = np.zeros_like(valid, dtype=bool)
    if len(values) > window:
        out[window:] = (cumulative[window:-1] - cumulative[:-window-1]) == window
    return out


def unchanged_runs(values, tolerance):
    """Length of the current run of consecutive values that changed by at most ``tolerance`` (freeze/stuck evidence)."""
    runs = np.zeros_like(values)
    runs[0] = np.isfinite(values[0])
    for t in range(1, len(values)):
        same = np.isfinite(values[t]) & np.isfinite(values[t-1]) & (np.abs(values[t]-values[t-1]) <= tolerance)
        runs[t] = np.where(same, runs[t-1]+1, np.isfinite(values[t]).astype(float))
    return runs


def forecast(values, blend):
    """Forecast x[t] as ``blend * x[t-1] + (1 - blend) * x[t-48]`` (last value vs. same time yesterday)."""
    previous, daily = lag(values, 1), lag(values, 48)
    # Even lambda=1 uses the declared 48-step history mode for eligibility.
    return blend * previous + (1-blend) * daily


def cusum(z, valid, k=0.5, cap=50):
    """Two-sided CUSUM over standardised residuals ``z``.

    Accumulates evidence of a sustained positive or negative shift (allowance ``k``,
    capped at ``cap``); the statistic resets to 0 wherever history is invalid.
    """
    plus, minus = np.zeros_like(z[0]), np.zeros_like(z[0])
    output = np.full_like(z, np.nan)
    for t in range(len(z)):
        # Upward and downward accumulators; reset to 0 whenever the point is not valid.
        plus = np.where(valid[t], np.clip(plus + np.nan_to_num(z[t]) - k, 0, cap), 0)
        minus = np.where(valid[t], np.clip(minus - np.nan_to_num(z[t]) - k, 0, cap), 0)
        output[t] = np.where(valid[t], np.maximum(plus, minus), np.nan)
    return output


def build_graph(sites, cfg):
    """Pre-compute, for every site, the indices and distances of its near and wider neighbour rings."""
    xy = sites[["easting_m", "northing_m"]].to_numpy()
    distances = cdist(xy, xy)
    # A site is never its own neighbour.
    np.fill_diagonal(distances, np.inf)
    order = np.argsort(distances, axis=1, kind="stable")
    k, rank = cfg["neighbors_per_ring"], cfg["wider_ring_start_rank"]
    if len(sites) <= rank + k:
        raise ValueError("Not enough sites for disjoint spatial rings")
    # Near ring = k closest sites; wider ring = k sites starting at a farther rank (disjoint).
    near, wider = order[:, :k], order[:, rank:rank+k]
    return {"near": near, "wider": wider,
            "near_distance": np.take_along_axis(distances, near, axis=1),
            "wider_distance": np.take_along_axis(distances, wider, axis=1)}


def capped_weights(distances, finite, cap):
    """Inverse-distance weights over valid neighbours, capped at ``cap`` so no single neighbour dominates (excess mass is redistributed)."""
    weights = np.broadcast_to(1 / np.maximum(distances, 1), finite.shape).copy()
    weights[~finite] = 0
    total = weights.sum(axis=-1, keepdims=True)
    weights /= np.where(total > 0, total, 1)
    # Water-filling redistributes excess only among valid uncapped references.
    for _ in range(weights.shape[-1]):
        excess = np.maximum(weights-cap, 0).sum(axis=-1, keepdims=True)
        weights = np.minimum(weights, cap)
        free = finite & (weights < cap-1e-12)
        remaining = np.where(free, weights, 0)
        mass = remaining.sum(axis=-1, keepdims=True)
        weights += np.where(mass > 0, excess * remaining / np.where(mass > 0, mass, 1), 0)
    return weights


def references(values, flags, graph, cfg):
    """Robust weighted-median reference value, neighbour count and weighted spread per ring for every [time, site, variable]."""
    result = {}
    for ring in ("near", "wider"):
        # [time, site, variable, neighbor]
        neighbors = np.moveaxis(values[:, graph[ring], :], 2, -1)
        quality = np.moveaxis(flags[:, graph[ring], :], 2, -1)
        # Only finite, unflagged neighbours can act as references.
        finite = np.isfinite(neighbors) & (quality == 0)
        weights = capped_weights(graph[ring+"_distance"][None, :, None, :], finite, cfg["max_reference_weight"])
        # Weighted median: sort the values and take the first one whose cumulative weight reaches half.
        ordered = np.argsort(np.where(finite, neighbors, np.inf), axis=-1, kind="stable")
        sorted_values = np.take_along_axis(neighbors, ordered, axis=-1)
        sorted_weights = np.take_along_axis(weights, ordered, axis=-1)
        threshold = sorted_weights.sum(axis=-1, keepdims=True) / 2
        idx = np.argmax(np.cumsum(sorted_weights, axis=-1) >= threshold, axis=-1)
        estimate = np.take_along_axis(sorted_values, idx[..., None], axis=-1)[..., 0]
        count = finite.sum(axis=-1)
        # Abstain when too few valid neighbours support the reference.
        estimate[count < cfg["min_neighbors"]] = np.nan
        spread = np.sqrt(np.sum(np.where(finite, (neighbors-estimate[..., None])**2, 0) * weights, axis=-1))
        result[ring] = estimate
        result[ring+"_count"] = count
        result[ring+"_spread"] = spread
    return result


def rule_scores(raw, delta, runs, std6, params):
    """Return (score, severity) for every point.

    ``severity`` measures hard physical-range violations (RH outside [0, 100], negative rain).
    ``score`` is the maximum of the range, rate, freeze and short-term-variability scores,
    each normalised by scales learned from nominal training data.
    """
    finite = np.isfinite(raw)
    severity = np.zeros_like(raw)
    eps = params["tolerance_raw"]
    # Humidity outside [0, 100] % (beyond numeric tolerance) is a hard violation.
    severity[..., 1] = np.maximum.reduce([np.zeros_like(raw[..., 1]), -raw[..., 1]-eps[1], raw[..., 1]-100-eps[1]])
    # Negative rain is a hard violation; temperature has no hard limit here.
    severity[..., 2] = np.maximum(0, -raw[..., 2]-eps[2])
    # A hard violation is inspectable regardless of tail resolution.
    range_score = severity / params["raw_scale_floor"]
    # Implausibly fast change, too-long unchanged runs and bursts of short-term variability.
    rate = np.abs(delta) / params["rate_scale"]
    freeze = runs / params["run_scale"]
    freeze[..., 2] = 0  # Dry rain runs alone are never freeze evidence.
    # Saturated humidity (~100 %) legitimately stays constant: not freeze evidence.
    freeze[..., 1] = np.where(raw[..., 1] >= 100-eps[1], 0, freeze[..., 1])
    variability = std6 / params["std_scale"]
    score = np.maximum.reduce([range_score, np.nan_to_num(rate), np.nan_to_num(freeze), np.nan_to_num(variability)])
    score[~finite] = np.nan
    return score, severity


def robust(values, floor, axis=0):
    """Robust centre/scale: median and 1.4826 x MAD along ``axis``, with the scale floored at ``floor``."""
    median = np.nanmedian(values, axis=axis)
    scale = 1.4826 * np.nanmedian(np.abs(values - np.expand_dims(median, axis)), axis=axis)
    return median, np.maximum(np.nan_to_num(scale), floor)


def past_mean(x, window):
    """Mean over the previous ``window`` frames, excluding the current one."""
    source = lag(x, 1)
    out = np.full_like(x, np.nan)
    if len(x) >= window:
        out[window-1:] = np.mean(np.lib.stride_tricks.sliding_window_view(source, window, axis=0), axis=-1)
    return out


# All 37 model inputs, in column order.
FEATURE_NAMES_ALL = list(FEATURE_NAMES) + [
    "raw_temporal", "raw_spatial", "raw_physics", "spatial_shift_6", "spatial_shift_48",
    "spatial_residual_mean_6", "spatial_residual_mean_48", "neighbor_change", "target_minus_neighbor_change",
    "relative_spatial_error", "local_volatility_ratio", "range_excess", "current_transformed", "source_flag_fraction"]


class FeatureExtractor:
    """Frozen, causal feature extractor fitted on nominal training data."""

    def __init__(self, cfg, sites):
        """``cfg`` is the ``features`` section of config/detector.yaml; ``sites`` has site_id/easting_m/northing_m."""
        if cfg["warmup_steps"] < 48 or cfg["episode_steps"] <= cfg["warmup_steps"]:
            raise ValueError("Features need at least 48 warm-up steps inside each episode")
        if cfg["min_neighbors"] < 3 or cfg["max_reference_weight"] < 1/cfg["min_neighbors"]:
            raise ValueError("Invalid spatial weight/minimum configuration")
        self.cfg = cfg
        self.sites = sites.copy().reset_index(drop=True)
        self.graph = build_graph(self.sites, cfg)
        self.params = {}
        self.fit_report = {}

    def fit(self, data):
        """Learn every frozen parameter from the nominal training partition (Jan-Aug 2023 only).

        Steps: numeric tolerances, forecast blend per variable (chosen on the last quarter of
        training), robust forecast/seasonal/spatial residual scales, rate/variability scales,
        CUSUM and freeze-run normalisation, and training statistics used by the attack generator.
        """
        if np.any(data.starts >= np.datetime64("2023-09-01")):
            raise ValueError("Fitting is restricted to the declared Jan-Aug 2023 training partition")
        # Fit only on finite, unflagged (quality == 0) training values.
        x = transform(data.values)
        quality = np.isfinite(x) & (data.flags == 0)
        delta = x - lag(x, 1)
        floors, raw_tol = [], []
        # Per-variable numeric tolerance from the smallest non-zero changes (data quantisation).
        for v in range(3):
            increments = np.abs(delta[..., v][quality[..., v] & np.isfinite(delta[..., v])])
            positive = increments[increments > 0]
            # Quantization-informed and robust to extremely small interpolated differences.
            floor = max(float(np.quantile(positive, .01)) if len(positive) else 0, 1e-6)
            floors.append(floor)
            raw = np.abs(data.values[1:, :, v]-data.values[:-1, :, v])
            raw = raw[np.isfinite(raw) & (raw > 0)]
            raw_tol.append(max(float(np.quantile(raw, .001))/2 if len(raw) else 0, 1e-7))
        floors = np.asarray(floors)
        self.params.update(scale_floor=floors, raw_scale_floor=np.asarray(raw_tol)*2,
                           tolerance_raw=np.asarray(raw_tol), tolerance=floors/2)
        # Last quarter of train chooses a bounded persistence blend; no calibration/test targets.
        split = int(len(x)*.75)
        loss, candidates = [], self.cfg["forecast_candidates"]
        for blend in candidates:
            err = np.abs(x - forecast(x, blend))
            loss.append(np.nanmean(np.where(quality & history_complete(x), err, np.nan)[split:], axis=(0, 1)))
        blend = np.asarray(candidates)[np.argmin(loss, axis=0)]
        self.params["blend"] = blend
        residual = x - forecast(x, blend)
        center, scale = robust(np.where(quality & history_complete(x), residual, np.nan), floors)
        self.params.update(forecast_center=center, forecast_scale=scale)
        # Harmonic seasonal anchor per site/variable: ridge regression on hour-of-day and day-of-year terms.
        design = calendar(data.times)
        coef = np.zeros((design.shape[1], len(self.sites), 3))
        ridge = np.eye(design.shape[1])*self.cfg["seasonal_ridge"]
        ridge[0, 0] = 0
        for site in range(len(self.sites)):
            for v in range(3):
                mask = quality[:, site, v]
                d, y = design[mask], x[mask, site, v]
                coef[:, site, v] = np.linalg.solve(d.T@d + ridge, d.T@y)
        anchor = np.einsum("tf,fsv->tsv", design, coef)
        center, scale = robust(np.where(quality, x-anchor, np.nan), floors)
        self.params.update(seasonal_coef=coef, anchor_center=center, anchor_scale=scale)
        # Per-site offset/scale of the difference to each neighbour ring (sites are not identical).
        refs = references(x, data.flags, self.graph, self.cfg)
        for ring in ("near", "wider"):
            center, scale = robust(np.where(quality, x-refs[ring], np.nan), floors)
            self.params[ring+"_offset"] = center
            self.params[ring+"_scale"] = scale
        self.params["rate_scale"] = np.maximum(np.nanquantile(np.where(quality, np.abs(delta), np.nan), .99, axis=0), floors)
        _, _, std6, _ = rolling(x, 6)
        self.params["std_scale"] = np.maximum(np.nanquantile(np.where(quality, std6, np.nan), .99, axis=0), floors)
        # Normalise CUSUM and unchanged-run lengths by their nominal 95th/99th percentiles (episode replay, like inference).
        run_chunks, cusum_chunks = [], []
        for _, episode in episodes(data, self.cfg["episode_steps"], self.cfg["warmup_steps"]):
            ex = transform(episode.values)
            valid = history_complete(ex) & np.isfinite(ex)
            eanchor = np.einsum("tf,fsv->tsv", calendar(episode.times), coef)
            z = (ex-eanchor-self.params["anchor_center"])/self.params["anchor_scale"]
            c = cusum(z, valid, self.cfg["cusum_k"], self.cfg["cusum_cap"])
            c[episode.flags != 0] = np.nan
            cusum_chunks.append(c)
            runs = unchanged_runs(ex, self.params["tolerance"])
            runs[episode.flags != 0] = np.nan
            run_chunks.append(runs)
        self.params["cusum_scale"] = np.maximum(np.nanquantile(np.concatenate(cusum_chunks), .95, axis=0), 1)
        self.params["run_scale"] = np.maximum(np.nanquantile(np.concatenate(run_chunks), .99, axis=0), 2)
        # Training statistics used by the attack generator (gain centre, mimicry, multivariate slope).
        raw_eligible = np.where(quality, data.values, np.nan)
        paired = quality[..., 0] & quality[..., 1]
        tx, rh = data.values[..., 0][paired], data.values[..., 1][paired]
        self.fit_report = {"fit_steps": len(x), "sites": len(self.sites), "selected_blend": blend,
                           "forecast_candidates": candidates, "inner_block_mae": loss,
                           "raw_mean": np.nanmean(raw_eligible, axis=(0, 1)).tolist(),
                           "raw_std": np.nanstd(raw_eligible, axis=(0, 1)).tolist(),
                           "temperature_humidity_slope": float(np.cov(tx, rh)[0, 1]/max(float(np.var(tx)), 1e-6))}
        return self

    def base_features(self, data):
        """The 23 baseline features and the three rule-family scores for one episode.

        Returns ``scores`` [time, site, var, (temporal, spatial, physics)], ``features``
        [time, site, var, 23], quality/history masks, neighbour references and residuals.
        """
        if list(data.sites.site_id) != list(self.sites.site_id):
            raise ValueError("Site ordering differs from model manifest")
        if len(data.times) == 0:
            raise ValueError("Empty episode")
        if len(data.times) > 1 and np.any(np.diff(data.times) != np.timedelta64(30, "m")):
            raise ValueError("Replay gap: split/reset and warm a new episode")
        x = transform(data.values)
        hist = history_complete(x)
        valid = hist & np.isfinite(x)
        quality = (data.flags == 0) & history_complete(np.where(data.flags == 0, x, np.nan))
        # Temporal family: forecast residual and seasonal-anchor CUSUM, both standardised.
        pred = forecast(x, self.params["blend"])
        anchor = np.einsum("tf,fsv->tsv", calendar(data.times), self.params["seasonal_coef"])
        z_forecast = (x-pred-self.params["forecast_center"])/self.params["forecast_scale"]
        z_anchor = (x-anchor-self.params["anchor_center"])/self.params["anchor_scale"]
        # Spatial family: standardised difference to the near and wider neighbour references.
        refs = references(x, data.flags, self.graph, self.cfg)
        z_near = (x-refs["near"]-self.params["near_offset"])/self.params["near_scale"]
        z_wider = (x-refs["wider"]-self.params["wider_offset"])/self.params["wider_scale"]
        accumulated = cusum(z_anchor, valid, self.cfg["cusum_k"], self.cfg["cusum_cap"])
        temporal = np.maximum(np.abs(z_forecast), accumulated/self.params["cusum_scale"])
        temporal[~valid] = np.nan
        spatial = np.maximum(np.abs(z_near), np.abs(z_wider))
        med6, mad6, _, _ = rolling(x, 6, strict=True)
        med48, mad48, _, _ = rolling(x, 48, strict=True)
        _, _, std6, slope6 = rolling(x, 6)
        _, _, std48, _ = rolling(x, 48)
        runs = unchanged_runs(x, self.params["tolerance"])
        delta = x-lag(x, 1)
        # Physics family uses raw (untransformed) values for physical ranges.
        rules, violations = rule_scores(data.values, delta, runs, std6, self.params)
        cal = calendar(data.times)
        broadcast = lambda a: np.broadcast_to(a[:, None, None], x.shape)
        # Assemble the 23 features in FEATURE_NAMES order; points without valid history are NaN.
        features = np.stack([z_forecast, z_anchor, z_near, z_wider, delta, med6, med48, mad6, mad48,
                             std6, std48, slope6, runs, refs["near_count"], refs["wider_count"],
                             refs["near_spread"], refs["wider_spread"],
                             *(broadcast(cal[:, j]) for j in range(1, 5)),
                             np.broadcast_to((data.values[..., 2] > 0)[..., None], x.shape),
                             np.broadcast_to(x[..., 2, None], x.shape)], axis=-1).astype("float32")
        features[~valid] = np.nan
        scores = np.stack([temporal, spatial, rules], axis=-1)
        return {"scores": scores, "features": features, "high_quality": quality, "references": refs,
                "violations": violations, "valid_history": valid, "std6": std6, "delta": delta}

    def model_inputs(self, episode):
        """The 37 model features per point plus masks for one episode.

        Returns ``features`` [t, site, var, 37], ``valid`` (finite target with complete own
        history, never in the first 48 frames), ``quality`` (unflagged with clean history) and
        ``spatial`` (raw spatial score, used to pick hard nominal training examples).
        """
        base = self.base_features(episode)
        f = base["features"]
        x = transform(episode.values)
        near = base["references"]["near"]
        # Standardised near-neighbour residual and its strictly-past history (step change vs. persistent offset).
        z = (x-near-self.params["near_offset"])/self.params["near_scale"]
        m6, _, _, _ = rolling(z, 6, strict=True)
        m48, _, _, _ = rolling(z, 48, strict=True)
        neighbor_change = near-lag(near, 1)
        spread = base["references"]["near_spread"]
        relative = (x-near-self.params["near_offset"])/np.maximum(spread, self.params["near_scale"])
        volatility = base["std6"]/np.maximum(self.params["std_scale"], 1e-6)
        extra = [*np.moveaxis(base["scores"], -1, 0), z-m6, z-m48,
                 past_mean(z, 6), past_mean(z, 48), neighbor_change, base["delta"]-neighbor_change,
                 relative, volatility, base["violations"], x, episode.flags/100]
        features = np.concatenate([f, np.stack(extra, axis=-1)], axis=-1).astype("float32")
        features[~np.isfinite(features)] = np.nan
        # Finite target and complete own history; absent neighbours remain NaN features.
        valid = base["valid_history"] & np.isfinite(episode.values)
        valid[:48] = False
        return {"features": features, "valid": valid, "quality": base["high_quality"],
                "spatial": base["scores"][..., 1]}

    def save(self, path):
        """Persist with joblib plus a manifest (hash, feature names, library versions)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path, compress=3)
        write_json(path.with_suffix(".json"), {"sha256": sha256(path), "feature_names": FEATURE_NAMES_ALL,
                                               "versions": versions()})

    @classmethod
    def load(cls, path):
        """Load a trusted, locally produced extractor; refuse other numpy/scikit-learn versions."""
        path = Path(path)
        manifest = read_json(path.with_suffix(".json"))
        if manifest["sha256"] != sha256(path):
            raise ValueError("Feature extractor file changed after saving")
        current = versions()
        if any(manifest["versions"][p] != current[p] for p in ("numpy", "scikit-learn")):
            raise ValueError("Library versions differ from the saved extractor; use requirements.txt")
        legacy_module_aliases()
        return joblib.load(path)
