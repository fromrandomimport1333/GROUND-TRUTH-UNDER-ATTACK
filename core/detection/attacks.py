"""Synthetic false-data-injection attacks used to train, select and benchmark the detector.

``generate_scenarios`` draws a reproducible set of attacks for one episode (family, window,
target sites, variable(s), amplitude) from the ``attacks`` section of ``config/detector.yaml``.
``inject`` applies them to a copy of the observations and returns the attacked values plus
point-level ``changed`` labels. Only the evaluator sees labels; the detector only sees values.
``scenarios`` is the single entry point used by the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from core.detection.utils import VARIABLES, config


@dataclass
class Injection:
    """Result of an injection: attacked ``observed`` values, ``changed`` (actually modified) and ``attempted`` masks, and event records."""
    observed: np.ndarray
    changed: np.ndarray
    attempted: np.ndarray
    events: list[dict]


def inject(values, scenarios, seed):
    """Copy-on-write injection. Invalid samples remain missing; quality is untouched."""
    nominal = np.asarray(values)
    observed = nominal.copy()
    attempted = np.zeros(nominal.shape, dtype=bool)
    events = []
    rng = np.random.default_rng(seed)
    for number, spec in enumerate(scenarios):
        spec = dict(spec)
        start, end = int(spec["start"]), int(spec["end"])
        sites = np.asarray(spec["sites"], dtype=int)
        variables = np.asarray(spec.get("variables", [spec.get("variable", 0)]), dtype=int)
        if start < 1 or end <= start or end > len(values):
            raise ValueError("Attack requires a past sample and must fit the episode")
        if not len(sites) or not len(variables) or np.any(sites < 0) or np.any(sites >= values.shape[1]) or np.any(variables < 0) or np.any(variables >= 3):
            raise ValueError("Invalid attack targets")
        # Block of points targeted by this attack: [time window] x [sites] x [variables].
        ix = np.ix_(np.arange(start, end), sites, variables)
        if attempted[ix].any():
            raise ValueError("Overlapping attacks need a separately declared composition policy")
        original = nominal[ix].copy()
        amount = spec.get("amplitude", 1.0)
        family = spec["family"]
        # Each branch computes the manipulated values for one attack family.
        if family in ("bias", "offset", "spike", "patch", "multi_offset", "invalid_range", "network_bias"):
            changed = original + amount
        elif family == "multivariate":
            changed = original + np.asarray(spec["amplitudes"])[None, None, :]
        elif family == "drift":
            changed = original + amount*np.linspace(0, 1, end-start)[:, None, None]
        elif family == "gain":
            center = np.asarray(spec.get("center", 0.0))
            changed = center + float(spec.get("factor", 1.5)) * (original-center)
        elif family == "intermittent_offset":
            block = max(1, int(spec.get("block_steps", 6)))
            active = ((np.arange(end-start) // block) % 2 == 0)
            changed = original + active[:, None, None] * amount
        elif family in ("freeze", "stuck"):
            changed = np.broadcast_to(observed[start-1][np.ix_(sites, variables)], original.shape).copy()
        elif family == "noise":
            changed = original + amount*rng.normal(size=original.shape)
        elif family in ("flatten", "flatline"):
            changed = original.copy()
            for offset, t in enumerate(range(start, end)):
                history = observed[max(0, t-6):t][:, sites][:, :, variables]
                center = np.mean(history, axis=0)
                changed[offset] = center + spec.get("rho", .25)*(original[offset]-center)
                observed[np.ix_([t], sites, variables)] = changed[offset][None]
        elif family == "rain_scale":
            changed = original * spec.get("factor", 2.)
        elif family == "rain_suppress":
            changed = original * float(spec.get("factor", 0.0))
        elif family == "rain_inject":
            changed = original + float(spec.get("amount", 1.0))
        elif family in ("pattern", "cycle_shaped"):
            phase = 2*np.pi*np.arange(end-start)/48 + spec.get("phase", 0)
            changed = original + amount*(1+.5*np.sin(phase[:, None, None]))
        elif family in ("mimicry", "realistic_replace"):
            mean = np.asarray(spec["training_mean"])
            std = np.asarray(spec["training_std"])
            changed = np.empty_like(original)
            previous = observed[start-1][np.ix_(sites, variables)].copy()
            for t in range(end-start):
                previous = mean + .95*(previous-mean) + .15*std*rng.normal(size=previous.shape)
                changed[t] = previous
        else:
            raise ValueError(f"Unknown attack family: {family}")
        # Keep attacked values physically plausible (except the deliberate invalid-range attack).
        if family != "invalid_range":
            for j, v in enumerate(variables):
                if v == 1:
                    changed[..., j] = np.clip(changed[..., j], 0, 100)
                elif v == 2:
                    changed[..., j] = np.maximum(changed[..., j], 0)
        changed[~np.isfinite(original)] = original[~np.isfinite(original)]
        observed[ix] = changed
        attempted[ix] = True
        # Labels count only points whose value really changed (e.g. scaling zero rain changes nothing).
        actual = np.isfinite(original) & np.isfinite(changed) & (changed != original)
        difference = np.abs(changed-original)
        spec.update(id=spec.get("id", f"{seed}-{number}"), seed=seed,
                    variables=variables.tolist(), variable_names=[VARIABLES[v] for v in variables],
                    actual_changed_points=int(actual.sum()), attempted_points=int(actual.size),
                    zero_impact=not bool(actual.any()), affected_sites=len(sites),
                    mean_absolute_change=float(np.nanmean(difference)), max_absolute_change=float(np.nanmax(difference)),
                    absolute_error_hours=float(np.nansum(difference)*.5),
                    clipping="RH [0,100]; rain >=0" if family != "invalid_range" else "none",
                    knowledge="current value and causal past; no future values; training summaries for mimicry",
                    quality_policy="unchanged", related_temperature_channels="excluded")
        spec["impact_by_variable"] = {
            VARIABLES[v]: {"mean_absolute_change": float(np.nanmean(difference[..., j])),
                           "max_absolute_change": float(np.nanmax(difference[..., j])),
                           "absolute_error_hours": float(np.nansum(difference[..., j])*.5),
                           "training_std_normalized_mean_change": float(np.nanmean(difference[..., j])/max(spec.get("training_scales", [1.]*len(variables))[j], 1e-9))}
            for j, v in enumerate(variables)}
        spec["pooled_impact_units"] = "mixed raw units; use impact_by_variable" if len(variables) > 1 else VARIABLES[variables[0]]
        if 2 in variables:
            j = list(variables).index(2)
            spec["rain_total_distortion_mm"] = float(np.nansum(changed[..., j]-original[..., j]))
        events.append(spec)
    actual = np.isfinite(nominal) & np.isfinite(observed) & (observed != nominal)
    return Injection(observed, actual, attempted, events)


def generate_scenarios(data, engine, cfg, partition, episode_id):
    """Draw one non-overlapping attack per configured family for an episode.

    The seed depends on the partition (development vs. test seed) and episode id, so the
    same scenarios are regenerated exactly. Rain families are placed on wet (or, for
    ``rain_inject``, dry) intervals; patch attacks cover all sites within a radius.
    """
    seed_base = cfg["test_seed"] if partition == "test" else cfg["development_seed"]
    seed = seed_base + episode_id
    rng = np.random.default_rng(seed)
    specs, occupied = [], np.zeros(data.values.shape, dtype=bool)
    warmup = engine.cfg["warmup_steps"]
    label = "test" if partition == "test" else "dev"
    xy = data.sites[["easting_m", "northing_m"]].to_numpy()
    if len(data.times) <= warmup+2:
        return specs, seed
    families = list(cfg["families"]) + list(cfg.get("extra_families", []))
    # Reserve broad footprints first; otherwise a network-wide event can be
    # crowded out by local events in the event-overlap guard.
    families.sort(key=lambda f: 0 if f == "network_bias" else 1)
    for index, family in enumerate(families):
        v = 2 if family in ("rain_scale", "rain_suppress", "rain_inject") else (1 if family == "invalid_range" else int(rng.integers(0, 2)))
        if family == "multivariate":
            v = 0
        variables = [0, 1] if family == "multivariate" else [v]
        duration = min(int(rng.choice(cfg["duration_steps"])), len(data.times)-warmup-1)
        if family == "spike":
            duration = int(rng.integers(1, 5))
        amplitude = float(rng.choice(cfg[f"{'temperature' if v == 0 else 'humidity'}_amplitudes_{label}"])) if v != 2 else 1.
        amplitude *= int(rng.choice([-1, 1]))
        if family == "network_bias":
            sites = np.arange(len(data.sites)).tolist()
            radius = 0
        else:
            sites = None
        # Try up to 100 random placements that do not overlap an earlier attack.
        for _ in range(100):
            site = int(rng.integers(len(data.sites)))
            start = int(rng.integers(warmup, len(data.times)-duration+1))
            radius = int(rng.choice(cfg["patch_radii_m"])) if family in ("patch", "multi_offset") else 0
            if family != "network_bias":
                sites = np.flatnonzero(np.linalg.norm(xy-xy[site], axis=1) <= radius).tolist()
            if family in ("rain_scale", "rain_suppress", "rain_inject"):
                window = data.values[warmup:len(data.times)-duration+1, :, 2]
                candidates = np.argwhere(window > 0) if family != "rain_inject" else np.argwhere(window == 0)
                if len(candidates):
                    onset, site = candidates[rng.integers(len(candidates))]
                    start, sites = int(onset+warmup), [int(site)]
            ix = np.ix_(np.arange(start, start+duration), sites, variables)
            if not occupied[ix].any():
                occupied[ix] = True
                break
        else:
            continue
        spec = {"id": f"{partition}-{episode_id:02d}-{family}", "family": family, "start": start,
                "end": start+duration, "sites": sites, "variables": variables, "amplitude": amplitude,
                "partition": partition, "episode": episode_id, "patch_radius_m": radius,
                "onset_utc": str(data.times[start]), "end_utc": str(data.times[start+duration-1]),
                "factor": float(rng.choice(cfg.get("rain_scale_factors", [0., .5, 2.]))), "rho": float(rng.choice([0., .25, .5])),
                "phase": float(rng.uniform(0, 2*np.pi))}
        if family == "gain":
            spec["factor"] = float(rng.choice(cfg.get("gain_factors", [0.5, 1.5, 2.0])))
            spec["center"] = float(engine.fit_report["raw_mean"][v])
        if family == "intermittent_offset":
            spec["block_steps"] = int(rng.choice(cfg.get("intermittent_block_steps", [4, 8, 12])))
        if family == "network_bias":
            spec["amplitude"] = float(rng.choice(cfg.get("network_bias_amplitudes", [0.5, 1.0, 2.0])))
        if family == "rain_suppress":
            spec["factor"] = float(rng.choice(cfg.get("rain_suppress_factors", [0.0, 0.1, 0.25])))
        if family == "rain_inject":
            spec["amount"] = float(rng.choice(cfg.get("rain_inject_amounts", [0.5, 1.0, 2.0])))
        spec["training_scales"] = [engine.fit_report["raw_std"][j] for j in variables]
        if family == "invalid_range":
            spec["amplitude"] = 110.
        if family == "multivariate":
            # Sign/magnitude relationship estimated only from nominal training residuals.
            spec["amplitudes"] = [amplitude, amplitude*engine.fit_report["temperature_humidity_slope"]]
        if family in ("mimicry", "realistic_replace"):
            spec["training_mean"] = [engine.fit_report["raw_mean"][j] for j in variables]
            spec["training_std"] = [engine.fit_report["raw_std"][j] for j in variables]
        specs.append(spec)
    return specs, seed


def scenarios(episode, extractor, partition, episode_id, seed=None, training=False):
    """Generate and inject the attacks for one episode.

    ``seed`` overrides the development (or, for ``partition="test"``, the test) seed;
    ``training=True`` removes the ``holdout_families`` so they stay unseen during training.
    """
    cfg = config("attacks")
    if seed is not None:
        cfg["test_seed" if partition == "test" else "development_seed"] = seed
    if training:
        cfg["families"] = [f for f in cfg["families"] if f not in config("model")["holdout_families"]]
    specs, used_seed = generate_scenarios(episode, extractor, cfg, partition, episode_id)
    return inject(episode.values, specs, used_seed)
