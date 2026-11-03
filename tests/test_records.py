import ast
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from attacks.records import (
    GENESIS_HASH,
    RECORD_KEYS,
    apply_faults,
    build_signed_stream,
    drop,
    record_hash,
    reorder,
    replay,
    rewrite_stored,
    shift_timestamp,
    tamper_value,
    unknown_sensor,
)
from tests.helpers import reference_statuses, sign, verify

SENSORS = ("s0", "s1", "s2")
STEPS = 12
T0 = datetime(2024, 6, 15, tzinfo=timezone.utc)
LABEL_KEYS = {"idx", "fault", "expected_status", "stage"}


def make_readings() -> list[dict]:
    """3 sensors x 12 steps, time-major: idx = step * 3 + sensor_number."""
    readings = []
    for step in range(STEPS):
        ts = (T0 + timedelta(minutes=5 * step)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for n, sid in enumerate(SENSORS):
            readings.append(
                {"sensor_id": sid, "timestamp": ts, "value": 10.0 + n + 0.25 * step, "unit": "degC"}
            )
    return readings


@pytest.fixture
def clean() -> list[dict]:
    return build_signed_stream(make_readings(), sign)


def sensor_of(idx: int) -> str:
    return SENSORS[idx % len(SENSORS)]


def only(labels: list[dict]) -> dict:
    assert len(labels) == 1
    assert LABEL_KEYS <= set(labels[0])
    return labels[0]


# ---------------------------------------------------------------- clean stream


def test_clean_stream_has_no_labels(clean):
    n = len(SENSORS) * STEPS
    assert len(clean) == n
    # every record has exactly the L1 message keys and a valid signature
    assert all(set(r) == set(RECORD_KEYS) for r in clean)
    assert all(verify(r) for r in clean)
    # per-sensor contiguous seq starting at 0 and a hash chain over the previous record
    for sid in SENSORS:
        mine = [r for r in clean if r["sensor_id"] == sid]
        assert [r["seq"] for r in mine] == list(range(STEPS))
        assert mine[0]["prev_hash"] == GENESIS_HASH
        for prev, cur in zip(mine, mine[1:]):
            assert cur["prev_hash"] == record_hash(prev)
    # the reference verifier accepts all of it, and no faults means no labels
    assert reference_statuses(clean, set(SENSORS)) == ["ok"] * n
    out, labels = apply_faults(clean, [])
    assert labels == []
    assert out == clean and out is not clean


def test_build_signed_stream_does_not_mutate_readings_and_checks_signer():
    readings = make_readings()
    snapshot = copy.deepcopy(readings)
    build_signed_stream(readings, sign)
    assert readings == snapshot

    def broken_signer(record, prev_hash):
        return {k: v for k, v in sign(record, prev_hash).items() if k != "signature"}

    with pytest.raises(ValueError):
        build_signed_stream(readings, broken_signer)


# ------------------------------------------------------------------ transforms


def test_tamper_value_changes_value_and_expects_bad_signature(clean):
    snapshot = copy.deepcopy(clean)
    out, labels = tamper_value(clean, 4, 2.5)
    assert clean == snapshot  # input untouched
    assert len(out) == len(clean)
    assert out[4]["value"] == pytest.approx(clean[4]["value"] + 2.5)
    assert out[4]["signature"] == clean[4]["signature"]  # not re-signed
    assert not verify(out[4])
    assert [r for k, r in enumerate(out) if k != 4] == [r for k, r in enumerate(clean) if k != 4]
    assert only(labels) == {
        "idx": 4, "fault": "tamper_value", "expected_status": "bad_signature", "stage": "transit",
    }


def test_replay_inserts_old_valid_record_and_expects_replay(clean):
    snapshot = copy.deepcopy(clean)
    out, labels = replay(clean, 2, 9)
    assert clean == snapshot
    assert len(out) == len(clean) + 1
    assert out[9] == clean[2] and out[9] is not out[2]
    assert verify(out[9])  # a replay carries a perfectly valid signature
    assert out[:9] == clean[:9] and out[10:] == clean[9:]
    assert only(labels) == {
        "idx": 9, "fault": "replay", "expected_status": "replay", "stage": "transit",
    }


def test_replay_must_land_after_its_source(clean):
    with pytest.raises(ValueError):
        replay(clean, 9, 2)
    with pytest.raises(ValueError):
        replay(clean, 5, 5)


def test_drop_removes_records_and_expects_gap_on_next_kept_record(clean):
    snapshot = copy.deepcopy(clean)
    out, labels = drop(clean, [3])  # s0 step 1
    assert clean == snapshot
    assert len(out) == len(clean) - 1
    assert out == clean[:3] + clean[4:]
    # next kept record of the same sensor is original idx 6, now idx 5
    assert out[5] is not clean[6] and out[5] == clean[6] and out[5]["sensor_id"] == "s0"
    assert only(labels) == {"idx": 5, "fault": "drop", "expected_status": "gap", "stage": "transit"}


def test_drop_run_of_one_sensor_gives_one_gap_label(clean):
    out, labels = drop(clean, [3, 6])  # s0 steps 1 and 2 -> single seq jump on step 3
    assert len(out) == len(clean) - 2
    assert only(labels)["idx"] == 9 - 2
    assert out[7] == clean[9]


def test_drop_of_different_sensors_gives_one_gap_label_each(clean):
    out, labels = drop(clean, [3, 4])  # s0 step 1 and s1 step 1
    assert [(l["idx"], l["expected_status"]) for l in labels] == [(4, "gap"), (5, "gap")]
    assert out[4] == clean[6] and out[5] == clean[7]


def test_drop_non_consecutive_records_of_one_sensor_gives_two_gaps(clean):
    out, labels = drop(clean, [3, 9])  # s0 steps 1 and 3, step 2 kept
    assert [l["idx"] for l in labels] == [5, 10]
    assert out[5] == clean[6] and out[10] == clean[12]


def test_drop_at_stream_tail_has_no_next_record_and_no_label(clean):
    out, labels = drop(clean, [len(clean) - 1])
    assert len(out) == len(clean) - 1
    assert labels == []


def test_reorder_swaps_consecutive_records_of_one_sensor(clean):
    snapshot = copy.deepcopy(clean)
    out, labels = reorder(clean, 4, 7)  # s1 steps 1 and 2
    assert clean == snapshot
    assert len(out) == len(clean)
    assert out[4] == clean[7] and out[7] == clean[4]
    assert verify(out[4]) and verify(out[7])  # signatures stay valid; only the order is wrong
    assert [r for k, r in enumerate(out) if k not in (4, 7)] == [
        r for k, r in enumerate(clean) if k not in (4, 7)
    ]
    # the early arrival of the newer record jumps ahead (gap); the late older one is a replay
    assert [(l["idx"], l["expected_status"]) for l in labels] == [(4, "gap"), (7, "replay")]
    assert all(l["fault"] == "reorder" and l["stage"] == "transit" for l in labels)
    assert {l["expected_status"] for l in labels} <= {"replay", "gap"}


def test_reorder_is_symmetric_in_its_arguments(clean):
    assert reorder(clean, 7, 4) == reorder(clean, 4, 7)


def test_reorder_rejects_swaps_that_are_not_a_per_sensor_inversion(clean):
    with pytest.raises(ValueError):
        reorder(clean, 4, 5)  # different sensors: per-sensor order is unchanged
    with pytest.raises(ValueError):
        reorder(clean, 1, 7)  # same sensor but step 1 lies between them
    with pytest.raises(ValueError):
        reorder(clean, 4, 4)


def test_shift_timestamp_moves_time_and_expects_bad_signature(clean):
    snapshot = copy.deepcopy(clean)
    out, labels = shift_timestamp(clean, 5, 90)
    assert clean == snapshot
    was = datetime.fromisoformat(clean[5]["timestamp"])
    now = datetime.fromisoformat(out[5]["timestamp"])
    assert now - was == timedelta(seconds=90)
    assert out[5]["timestamp"].endswith("Z")
    assert out[5]["value"] == clean[5]["value"] and out[5]["signature"] == clean[5]["signature"]
    assert not verify(out[5])
    assert only(labels) == {
        "idx": 5, "fault": "shift_timestamp", "expected_status": "bad_signature", "stage": "transit",
    }


def test_rewrite_stored_changes_value_and_is_labelled_storage(clean):
    snapshot = copy.deepcopy(clean)
    out, labels = rewrite_stored(clean, 8, -3.0)
    assert clean == snapshot
    assert out[8]["value"] == pytest.approx(clean[8]["value"] - 3.0)
    assert out[8]["signature"] == clean[8]["signature"]
    assert not verify(out[8])
    assert only(labels) == {
        "idx": 8, "fault": "rewrite_stored", "expected_status": "bad_signature", "stage": "storage",
    }


def test_unknown_sensor_swaps_the_id_and_expects_unknown_sensor(clean):
    snapshot = copy.deepcopy(clean)
    out, labels = unknown_sensor(clean, 6, "ghost-sensor")
    assert clean == snapshot
    assert out[6]["sensor_id"] == "ghost-sensor"
    assert {k: v for k, v in out[6].items() if k != "sensor_id"} == {
        k: v for k, v in clean[6].items() if k != "sensor_id"
    }
    assert only(labels) == {
        "idx": 6, "fault": "unknown_sensor", "expected_status": "unknown_sensor", "stage": "transit",
    }


def test_unknown_sensor_rejects_an_id_that_is_already_in_the_stream(clean):
    with pytest.raises(ValueError):
        unknown_sensor(clean, 6, "s1")


@pytest.mark.parametrize(
    "call",
    [
        lambda r: tamper_value(r, 4, 0.0),
        lambda r: shift_timestamp(r, 4, 0),
        lambda r: rewrite_stored(r, 4, 0.0),
    ],
)
def test_noop_transformations_are_rejected(clean, call):
    with pytest.raises(ValueError):
        call(clean)  # a "fault" that changes nothing would be a wrong label


def test_out_of_range_indices_are_rejected(clean):
    n = len(clean)
    with pytest.raises(IndexError):
        tamper_value(clean, n, 1.0)
    with pytest.raises(IndexError):
        drop(clean, [-1])
    with pytest.raises(IndexError):
        replay(clean, 0, n + 1)


# ----------------------------------------------- labels against a reference L1

# In-place faults sit on the last record of a sensor so that the (known, see below) follow-on gap
# does not appear; drop/replay/reorder can sit mid-stream.
ORACLE_CASES = [
    (tamper_value, (35, 2.5)),
    (shift_timestamp, (35, 90)),
    (rewrite_stored, (35, -1.0)),
    (unknown_sensor, (35, "ghost-sensor")),
    (replay, (2, 9)),
    (drop, ([3, 6],)),
    (reorder, (4, 7)),
]


@pytest.mark.parametrize("fn,args", ORACLE_CASES, ids=[fn.__name__ for fn, _ in ORACLE_CASES])
def test_labels_agree_with_reference_verifier(clean, fn, args):
    out, labels = fn(clean, *args)
    statuses = reference_statuses(out, set(SENSORS))
    expected = ["ok"] * len(out)
    for row in labels:
        expected[row["idx"]] = row["expected_status"]
    assert statuses == expected


@pytest.mark.parametrize("fn,args", ORACLE_CASES, ids=[fn.__name__ for fn, _ in ORACLE_CASES])
def test_transforms_never_mutate_their_input(clean, fn, args):
    snapshot = copy.deepcopy(clean)
    out, _ = fn(clean, *args)
    assert clean == snapshot
    assert all(a is not b for a in out for b in clean)  # no shared record objects


def test_rejected_record_leaves_a_seq_hole_that_shows_as_gap_on_the_next_one(clean):
    # Documented consequence of in-place faults: a record rejected for its signature does not
    # advance the sensor's sequence, so the sensor's next record reads as a gap. Labels mark only
    # the faulted record; consumers must not count this follow-on as a false positive.
    out, labels = tamper_value(clean, 4, 2.5)
    statuses = reference_statuses(out, set(SENSORS))
    assert statuses[4] == "bad_signature"
    assert statuses[7] == "gap"  # same sensor, next step
    assert [s for k, s in enumerate(statuses) if k not in (4, 7)] == ["ok"] * (len(out) - 2)
    assert [l["idx"] for l in labels] == [4]


# ------------------------------------------------------------------ composition


def test_apply_faults_keeps_labels_aligned_with_a_shifted_stream(clean):
    plan = [  # strictly descending stream position
        ("rewrite_stored", {"idx": 33, "delta": 1.0}),
        ("replay", {"src_idx": 2, "at_idx": 27}),
        ("drop", {"idxs": [14]}),
        ("reorder", {"i": 4, "j": 7}),
    ]
    snapshot = copy.deepcopy(clean)
    out, labels = apply_faults(clean, plan)
    assert clean == snapshot
    assert len(out) == len(clean)  # +1 replay, -1 drop
    assert [l["idx"] for l in labels] == sorted(l["idx"] for l in labels)
    assert {l["fault"] for l in labels} == {"rewrite_stored", "replay", "drop", "reorder"}
    expected = ["ok"] * len(out)
    for row in labels:
        expected[row["idx"]] = row["expected_status"]
    assert reference_statuses(out, set(SENSORS)) == expected


def test_apply_faults_rejects_plans_not_in_descending_position_order(clean):
    plan = [("drop", {"idxs": [3]}), ("replay", {"src_idx": 2, "at_idx": 20})]
    with pytest.raises(ValueError):
        apply_faults(clean, plan)


# ---------------------------------------------------------------- hard rule


def test_helpers_not_imported_by_production_code():
    root = Path(__file__).resolve().parents[1]
    for name in ("attacks/records.py", "attacks/build_records.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""] + [a.name for a in node.names]
            else:
                continue
            assert not any("helpers" in m or m.split(".")[0] == "tests" for m in modules), name
