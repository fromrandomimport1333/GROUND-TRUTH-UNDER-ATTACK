"""The detector: Extra Trees attack probability -> calibrated tail value -> per-variable alert rule.

* ``training_rows``: labelled rows from nominal and synthetically attacked Jan-Aug 2023 episodes.
* ``train``: one Extra Trees classifier per variable.
* ``score``: attack probability for every valid point of an episode.
* ``calibration_tails`` / ``p_values``: empirical upper-tail value against nominal Sep-Oct 2023
  scores, ``p = (1 + #nominal >= score) / (n + 1)``. Small p = unusual. It is not the probability of an attack.
* ``alerts``: per-variable rule, alert when ``p <= alpha``, optionally confirmed two-of-three.
* ``Detector``: loads the frozen artifacts and returns p-values and alerts for new episodes.
"""
from __future__ import annotations

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits

from core.detection.attacks import scenarios
from core.detection.data import episodes
from core.detection.features import FeatureExtractor
from core.detection.utils import ARTIFACTS, config, read_json, sha256


def estimator(seed, threads):
    """Untrained classifier: missing-value indicators + 120-tree Extra Trees."""
    return make_pipeline(SimpleImputer(add_indicator=True), ExtraTreesClassifier(
        n_estimators=120, max_depth=18, min_samples_leaf=8, max_features=.8, n_jobs=threads, random_state=seed))


def training_rows(extractor, data, log=print):
    """Sample labelled feature rows per variable from nominal and attacked training episodes.

    Per episode and variable: up to 1,800 attacked (positive) rows and 1,800 nominal (negative)
    rows, of which a quarter are *hard* negatives (largest spatial residuals). Families in
    ``holdout_families`` are never used for training. Returns ``[(X, y) for each variable]``.
    """
    cfg = config("model")
    rng = np.random.default_rng(cfg["seed"])
    x_rows, y_rows = [[], [], []], [[], [], []]
    for eid, episode in episodes(data):
        log(f"Training rows: episode {eid}")
        nominal = extractor.model_inputs(episode)
        # Reserved draws (see config/detector.yaml, model.reserved_rng_draws): they keep the random stream, and
        # therefore the sampled rows, identical to the run reported in results/.
        clean = nominal["quality"].all(axis=-1) & nominal["valid"].all(axis=-1)
        for split, quota in cfg["reserved_rng_draws"].items():
            time_mask = episode.starts < np.datetime64("2023-08-01") if split == "train" else episode.starts >= np.datetime64("2023-08-02")
            count = int((clean & time_mask[:, None]).sum())
            if count:
                rng.choice(count, min(quota, count), replace=False)
        for replica in range(cfg["training_replicas"]):
            injected = scenarios(episode, extractor, "validation", eid, cfg["train_seed"] + 1000*replica, training=True)
            attacked = extractor.model_inputs(episode.with_values(injected.observed))
            for v in range(3):
                # All nominal points, including difficult weather, remain valid negatives.
                for source, label in ((nominal, np.zeros_like(injected.changed)), (attacked, injected.changed)):
                    usable = source["valid"][:, :, v] & source["quality"][:, :, v]
                    positive = np.flatnonzero(usable & label[:, :, v])
                    negative = np.flatnonzero(usable & ~label[:, :, v])
                    pos = rng.choice(positive, min(len(positive), cfg["positive_rows_per_episode_variable"]), replace=False)
                    num = min(len(negative), cfg["negative_rows_per_episode_variable"])
                    random_neg = rng.choice(negative, num*3//4, replace=False)
                    hardness = np.nan_to_num(source["spatial"][:, :, v].ravel()[negative], nan=0)
                    hard_neg = negative[np.argsort(hardness)[-max(1, num//4):]] if num else np.array([], dtype=int)
                    selected = np.unique(np.r_[pos, random_neg, hard_neg])
                    x_rows[v].append(source["features"][:, :, v].reshape(-1, source["features"].shape[-1])[selected])
                    y_rows[v].append(label[:, :, v].ravel()[selected])
    return [(np.concatenate(x_rows[v]), np.concatenate(y_rows[v])) for v in range(3)]


def train(rows, log=print):
    """Fit one Extra Trees classifier per variable on the sampled rows."""
    cfg = config("model")
    models = []
    for v, (x, y) in enumerate(rows):
        log(f"Fit Extra Trees, variable {v}: {len(y):,} rows, {int(y.sum()):,} attacked")
        model = estimator(cfg["seed"] + v, cfg["threads"])
        with threadpool_limits(limits=cfg["threads"]):
            model.fit(x, y)
        models.append(model)
    return models


def score(models, extractor, episode):
    """Attack probability [time, site, variable] (NaN where the point cannot be scored) and context.

    Context: ``quality`` (clean point) and ``eligible`` (outside the 48-step warm-up).
    """
    inputs = extractor.model_inputs(episode)
    result = np.full(inputs["valid"].shape, np.nan, dtype="float32")
    with threadpool_limits(limits=config("model")["threads"]):
        for v, model in enumerate(models):
            valid = inputs["valid"][..., v]
            if valid.any():
                result[..., v][valid] = model.predict_proba(inputs["features"][..., v, :][valid])[:, 1]
    eligible = np.broadcast_to((np.arange(len(episode.times)) >= 48)[:, None, None], episode.values.shape).copy()
    result[~eligible] = np.nan
    return result, {"quality": inputs["quality"], "eligible": eligible}


def calibration_tails(models, extractor, data, log=print):
    """Sorted nominal scores per variable from clean, eligible calibration points."""
    chunks = [[], [], []]
    for eid, episode in episodes(data):
        log(f"Calibration: episode {eid}")
        s, ctx = score(models, extractor, episode)
        for v in range(3):
            good = ctx["quality"][..., v] & ctx["eligible"][..., v] & np.isfinite(s[..., v])
            chunks[v].append(s[..., v][good])
    return [np.sort(np.concatenate(c)).astype("float32") for c in chunks]


def p_values(s, tails):
    """Conservative empirical upper-tail value per point (NaN where unscored), as float32."""
    out = np.full(s.shape, np.nan, dtype="float64")
    for v, tail in enumerate(tails):
        mask = np.isfinite(s[..., v])
        if len(tail):
            out[..., v][mask] = (1+len(tail)-np.searchsorted(tail, s[..., v][mask], side="left"))/(len(tail)+1)
    return out.astype("float32")


def confirmed(alert, mode):
    """``direct``: alert on every candidate. ``two_of_three``: the current point and at least one of
    the two previous points must be candidates (an alert is never back-dated)."""
    alert = np.asarray(alert, dtype=bool)
    if mode == "direct":
        return alert.copy()
    if mode != "two_of_three":
        raise ValueError("Unknown confirmation mode")
    previous = np.zeros_like(alert)
    previous2 = np.zeros_like(alert)
    previous[1:] = alert[:-1]
    previous2[2:] = alert[:-2]
    return alert & (previous | previous2)


def alert_code(p, rule):
    """Apply one rule ``{"alpha", "confirmation"}``: 0 = alert, 1 = no alert, NaN = unscored."""
    alert = confirmed(np.isfinite(p) & (p <= rule["alpha"]), rule["confirmation"])
    return np.where(np.isfinite(p), np.where(alert, 0., 1.), np.nan)


def alerts(p, policy):
    """Apply the per-variable policy (list of three rules) to p-values [time, site, variable]."""
    out = np.full(p.shape, np.nan, dtype="float32")
    for v, rule in enumerate(policy):
        out[..., v] = alert_code(p[..., v], rule)
    return out


class Detector:
    """The frozen detector, loaded from ``artifacts/``; verifies every artifact hash first."""

    def __init__(self, folder=ARTIFACTS):
        frozen = read_json(folder / "policy.json")
        for name, digest in frozen["artifact_sha256"].items():
            if sha256(folder / name) != digest:
                raise ValueError(f"Frozen artifact changed: {name}")
        self.policy = frozen["policy"]
        self.extractor = FeatureExtractor.load(folder / "feature_extractor.joblib")
        self.models = joblib.load(folder / "extra_trees.joblib")
        with np.load(folder / "calibration.npz") as z:
            self.tails = [z[str(v)] for v in range(3)]

    def __call__(self, episode):
        """Return (p-values, alerts) for one episode; ``alerts`` is True where an alert is raised."""
        s, ctx = score(self.models, self.extractor, episode)
        p = p_values(s, self.tails)
        return p, (alerts(p, self.policy) == 0) & ctx["eligible"]
