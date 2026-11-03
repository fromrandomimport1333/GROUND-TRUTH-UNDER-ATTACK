from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from attacks.schema import REQUIRED_PARAMS, Scenario, Suite, dump_suite, load_suite

START = datetime(2024, 7, 1, 12, 0, tzinfo=timezone.utc)


def make_scenario(**overrides) -> Scenario:
    fields = dict(
        id="dev-offset-ai-0000",
        family="offset",
        variable="air_temperature",
        stations=("77",),
        start=START,
        end=START + timedelta(hours=6),
        params={"k": 2.0},
        split="dev",
    )
    fields.update(overrides)
    return Scenario(**fields)


def test_end_must_follow_start():
    with pytest.raises(ValidationError):
        make_scenario(end=START - timedelta(hours=1))
    with pytest.raises(ValidationError):
        make_scenario(end=START)


def test_rain_family_requires_precipitation():
    # rain_* on a non-precipitation variable is rejected
    with pytest.raises(ValidationError):
        make_scenario(
            family="rain_scale", variable="air_temperature", params={"f": 2.0}
        )
    # and the reverse: a value family on precipitation is rejected
    with pytest.raises(ValidationError):
        make_scenario(family="offset", variable="precipitation", params={"k": 2.0})
    # the valid combination is accepted
    ok = make_scenario(
        family="rain_scale", variable="precipitation", params={"f": 2.0}
    )
    assert ok.family == "rain_scale"


def test_missing_param_rejected():
    with pytest.raises(ValidationError):
        make_scenario(family="offset", params={})
    with pytest.raises(ValidationError):
        make_scenario(family="spike", params={"k": 4.0})  # n missing
    with pytest.raises(ValidationError):
        make_scenario(family="offset", params={"k": 2.0, "n": 3.0})  # extra key
    with pytest.raises(ValidationError):
        make_scenario(family="stuck", params={"k": 1.0})  # stuck takes none


def test_required_params_table():
    assert REQUIRED_PARAMS == {
        "offset": {"k"},
        "drift": {"k"},
        "spike": {"k", "n"},
        "noise": {"k"},
        "flatline": {"w_steps"},
        "stuck": set(),
        "multi_offset": {"k", "n"},
        "gain": {"g"},
        "intermittent_offset": {"k"},
        "rain_scale": {"f"},
        "rain_suppress": set(),
        "rain_inject": {"total_mm", "duration_steps"},
        "realistic_replace": {"k", "generator"},
        "cycle_shaped": {"k"},
        "network_bias": {"k"},
        "value_replay": {"days_back", "noise"},
    }


def test_roundtrip_yaml_identical(tmp_path: Path):
    suite = Suite(
        name="unit",
        seed=7,
        scenarios=(
            make_scenario(),
            make_scenario(
                id="dev-multi_offset-ai-0001",
                family="multi_offset",
                stations=("77", "78", "79"),
                params={"k": 3.0, "n": 3.0},
            ),
            make_scenario(
                id="heldout-rain_inject-pr-0002",
                family="rain_inject",
                variable="precipitation",
                params={"total_mm": 7.0, "duration_steps": 24.0},
                split="heldout",
            ),
            make_scenario(
                id="dev-stuck-re-0003",
                family="stuck",
                variable="relative_humidity",
                params={},
            ),
        ),
    )
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    dump_suite(suite, a)
    dump_suite(suite, b)
    assert a.read_bytes() == b.read_bytes()

    loaded = load_suite(a)
    assert loaded == suite

    # times are ISO-8601 with a Z suffix
    assert "2024-07-01T12:00:00Z" in a.read_text()

    # dump -> load -> dump is a fixed point
    c = tmp_path / "c.yaml"
    dump_suite(loaded, c)
    assert c.read_bytes() == a.read_bytes()
