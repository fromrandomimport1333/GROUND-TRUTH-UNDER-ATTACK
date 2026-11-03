"""Project constants and small helpers (configuration, hashing, JSON, library versions)."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]   # repository root (core/detection/utils.py -> ../..)
# Variable order of every [time, site, variable] array: 0 temperature, 1 humidity, 2 rain.
VARIABLES = ("temperature_c", "relative_humidity_pct", "precip_mm_30min")
UNITS = ("degC", "percent", "mm/30min")
ARTIFACTS = ROOT / "artifacts"   # trained models and frozen policy (git-ignored)
OUTPUTS = ROOT / "outputs"       # results of a new run (git-ignored); reported results are in results/


def config(section=None):
    """Load ``config/detector.yaml`` (or one of its sections)."""
    cfg = yaml.safe_load((ROOT / "config/detector.yaml").read_text())
    return cfg[section] if section else cfg


def sha256(path):
    """Hex SHA-256 of a file, read in 8 MiB blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_safe(value):
    """Convert NumPy values, Paths and non-finite floats (-> None) into JSON-safe values."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [json_safe(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


def write_json(path, value):
    """Write strict, indented JSON, creating parent folders."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(value), indent=2, allow_nan=False) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text())


def versions():
    """Python/platform and key library versions, stored with every saved model."""
    return {"python": platform.python_version(), "platform": platform.platform(), **{
        p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scikit-learn", "scipy")}}


def legacy_module_aliases():
    """Let artifacts pickled by the original ``layer3_detector`` package (module ``detector``) load.

    Pickles store the defining module's name; this package was renamed ``detector`` -> ``core.detection``.
    Only use for trusted local artifacts: their SHA-256 hashes are checked before loading.
    """
    import importlib
    import sys
    package = importlib.import_module("core.detection")
    sys.modules.setdefault("detector", package)
    for name in ("utils", "data", "attacks", "features", "model", "metrics"):
        sys.modules.setdefault(f"detector.{name}", importlib.import_module(f"core.detection.{name}"))
