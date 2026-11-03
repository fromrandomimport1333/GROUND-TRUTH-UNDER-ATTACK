"""Suite builder: a rebuild is byte-identical (reproducible standard scenario)."""

import hashlib

import numpy as np
import pandas as pd

from attacks.build_suites import build_suite


def _write_fixture(clean_dir):
    """A tiny clean set (10 stations, Dec 2023 - Jun 2024) that the smoke grid can build on."""
    clean_dir.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range("2023-12-20", "2024-07-01", freq="5min", tz="UTC", inclusive="left", name="time")
    stations = [str(i) for i in range(1, 11)]
    n, m = len(idx), len(stations)
    rng = np.random.default_rng(0)
    tod = (np.arange(n) % 288)[:, None]
    lon = np.linspace(15.8, 15.9, m)
    for var, base, amp, noise in [
        ("air_temperature", 10.0, 6.0, 0.3),
        ("relative_humidity", 65.0, 15.0, 1.0),
        ("precipitation", 0.0, 0.0, 0.0),
    ]:
        if var == "precipitation":
            vals = np.where(rng.random((n, m)) < 0.3, rng.exponential(0.2, (n, m)), 0.0)
        else:
            vals = base + amp * np.sin(2 * np.pi * tod / 288) + rng.normal(0, noise, (n, m))
        pd.DataFrame(vals.astype(np.float32), index=idx, columns=stations).to_parquet(
            clean_dir / f"{var}.parquet"
        )
        pd.DataFrame(np.zeros((n, m), np.int8), index=idx, columns=stations).to_parquet(
            clean_dir / f"{var}_flag.parquet"
        )
    pd.DataFrame(
        {"lon": lon, "lat": np.full(m, 47.0), "alt_m": np.full(m, 300.0)},
        index=pd.Index(stations, name="station"),
    ).to_csv(clean_dir / "stations.csv")
    pd.DataFrame(
        {"air_temperature": np.full(m, 0.5), "relative_humidity": np.full(m, 2.0)},
        index=pd.Index(stations, name="station"),
    ).to_csv(clean_dir / "sigma_ref.csv")


def _hashes(out_dir, name):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((out_dir / name).iterdir())
        if p.name != "manifest.json"  # manifest embeds the same hashes
    }


def test_build_is_byte_identical(tmp_path):
    clean = tmp_path / "clean"
    _write_fixture(clean)
    a, b = tmp_path / "a", tmp_path / "b"
    build_suite("smoke", clean_dir=clean, out_dir=a)
    build_suite("smoke", clean_dir=clean, out_dir=b)
    assert _hashes(a, "smoke") == _hashes(b, "smoke")
    assert (a / "smoke" / "manifest.json").exists()


def test_clean2024_has_no_labels(tmp_path):
    clean = tmp_path / "clean"
    _write_fixture(clean)
    out = tmp_path / "out"
    build_suite("clean2024", clean_dir=clean, out_dir=out)
    labels = pd.read_parquet(out / "clean2024" / "labels.parquet")
    assert len(labels) == 0
    values = pd.read_parquet(out / "clean2024" / "air_temperature.parquet")
    assert values.index.equals(pd.read_parquet(clean / "air_temperature.parquet").index)
