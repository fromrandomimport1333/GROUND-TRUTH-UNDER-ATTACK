"""Scoring tests on hand-built tables (one or two stations, a few hours of 5-min data)."""

import json

import numpy as np
import pandas as pd
import pytest

from eval.scoring import (
    event_table,
    false_alarm_rate,
    mdm90,
    point_metrics,
    release_leakage,
    score_records,
    sensitivity,
)

STEP = pd.Timedelta("5min")
T0 = pd.Timestamp("2024-06-01 00:00", tz="UTC")
INDEX = pd.date_range(T0, periods=288 * 3, freq="5min", tz="UTC")  # three days
WIN_START = T0 + pd.Timedelta("2h")
WIN_STEPS = 12  # one hour -> end (exclusive) = 03:00
WIN_END = WIN_START + WIN_STEPS * STEP
VAR = "air_temperature"


def make_alarms(*times, stations=("77",), index=INDEX) -> pd.DataFrame:
    """Bool alarm table; each time is (timestamp) -> station 77, or (timestamp, station)."""
    table = pd.DataFrame(False, index=index, columns=list(stations))
    for item in times:
        ts, station = item if isinstance(item, tuple) else (item, stations[0])
        table.loc[ts, station] = True
    return table


def make_label(**over) -> dict:
    row = {
        "scenario_id": "dev-offset-ai-0001",
        "suite": "dev",
        "family": "offset",
        "category": "A",
        "variable": VAR,
        "station": "77",
        "start": WIN_START,
        "end": WIN_END,
        "onset": WIN_START,
        "size": 2.0,
        "size_phys": 1.2,
        "unit": "degC",
        "duration_h": 1.0,
        "n_stations": 1,
        "split": "dev",
    }
    row.update(over)
    return row


def make_labels(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows) or [make_label()])


def run_events(alarm_table, label_rows=None, **kw) -> pd.DataFrame:
    return event_table(make_labels(*(label_rows or [])), {VAR: alarm_table}, **kw)


# ------------------------------------------------------------------ event table


def test_detected_when_alarm_inside_window():
    ev = run_events(make_alarms(WIN_START + pd.Timedelta("30min")))
    assert len(ev) == 1
    assert bool(ev.loc[0, "detected"]) is True
    # original label columns are kept
    assert {"scenario_id", "family", "size", "start", "end"} <= set(ev.columns)


def test_no_alarm_means_not_detected_and_nan_latency():
    ev = run_events(make_alarms())
    assert bool(ev.loc[0, "detected"]) is False
    assert np.isnan(ev.loc[0, "latency_min"])
    assert np.isnan(ev.loc[0, "latency_from_onset_min"])


def test_alarm_in_grace_counts():
    # first step after the exclusive end, and 59 min after it: both inside end + 1 h
    for delay in ("0min", "30min", "55min"):
        ev = run_events(make_alarms(WIN_END + pd.Timedelta(delay)))
        assert bool(ev.loc[0, "detected"]) is True, delay


def test_alarm_after_grace_does_not_count():
    # [start, end + grace) is half-open: end + 1 h itself is outside
    for delay in ("60min", "65min", "3h"):
        ev = run_events(make_alarms(WIN_END + pd.Timedelta(delay)))
        assert bool(ev.loc[0, "detected"]) is False, delay
    # a custom grace is honoured
    ev = run_events(make_alarms(WIN_END + pd.Timedelta("90min")), grace="2h")
    assert bool(ev.loc[0, "detected"]) is True


def test_alarm_before_start_does_not_count():
    ev = run_events(make_alarms(WIN_START - STEP))
    assert bool(ev.loc[0, "detected"]) is False


def test_alarm_on_other_station_or_variable_does_not_count():
    other_station = make_alarms((WIN_START + STEP, "78"), stations=("77", "78"))
    assert bool(run_events(other_station).loc[0, "detected"]) is False
    other_var = event_table(make_labels(), {VAR: make_alarms(), "relative_humidity": make_alarms(WIN_START)})
    assert bool(other_var.loc[0, "detected"]) is False


def test_latency_minutes():
    alarms = make_alarms(WIN_START + pd.Timedelta("25min"), WIN_START + pd.Timedelta("40min"))
    onset = WIN_START + pd.Timedelta("10min")
    ev = run_events(alarms, [make_label(onset=onset)])
    assert ev.loc[0, "latency_min"] == pytest.approx(25.0)  # first alarm - start
    assert ev.loc[0, "latency_from_onset_min"] == pytest.approx(15.0)  # first alarm - onset


def test_latency_from_onset_ignores_alarms_before_onset_and_nan_onset_is_nan():
    alarms = make_alarms(WIN_START + pd.Timedelta("5min"), WIN_START + pd.Timedelta("35min"))
    ev = run_events(alarms, [make_label(onset=WIN_START + pd.Timedelta("20min"))])
    assert ev.loc[0, "latency_min"] == pytest.approx(5.0)
    assert ev.loc[0, "latency_from_onset_min"] == pytest.approx(15.0)  # next alarm at/after onset
    ev = run_events(alarms, [make_label(onset=pd.NaT)])
    assert bool(ev.loc[0, "detected"]) is True
    assert np.isnan(ev.loc[0, "latency_from_onset_min"])
    ev = run_events(alarms, [make_label(onset=np.nan)])
    assert np.isnan(ev.loc[0, "latency_from_onset_min"])


def test_multi_station_scenario_detected_if_any_station_alarms():
    stations = ("77", "78", "79")
    rows = [make_label(station=s, family="multi_offset", n_stations=3) for s in stations]
    alarms = make_alarms((WIN_START + pd.Timedelta("20min"), "79"), stations=stations)
    ev = run_events(alarms, rows)
    assert len(ev) == 3  # one row per label row
    assert ev["detected"].all()  # scenario level, spec section 7
    assert (ev["latency_min"] == 20.0).all()
    assert ev["station_detected"].tolist() == [False, False, True]


def test_naive_label_times_are_rejected():
    naive = make_label(start=WIN_START.tz_localize(None), end=WIN_END.tz_localize(None))
    with pytest.raises(ValueError, match="tz-aware"):
        run_events(make_alarms(WIN_START), [naive])
    with pytest.raises(ValueError, match="tz-aware"):
        run_events(make_alarms(WIN_START).tz_localize(None))


def test_label_station_missing_from_alarm_table_raises():
    with pytest.raises(ValueError, match="99"):
        run_events(make_alarms(), [make_label(station="99")])


# --------------------------------------------------------------- point metrics


def test_point_metrics_no_point_adjust():
    # one alarm inside a 12-point window: recall is 1/12, not 1 (no point adjustment)
    pm = point_metrics(make_labels(), {VAR: make_alarms(WIN_START + pd.Timedelta("20min"))})
    m = pm[VAR]
    assert m["recall"] == pytest.approx(1 / 12)
    assert m["precision"] == pytest.approx(1.0)
    assert m["f1"] == pytest.approx(2 * 1.0 * (1 / 12) / (1.0 + 1 / 12))
    # an alarm elsewhere is a false positive: precision drops, recall unchanged
    pm = point_metrics(
        make_labels(), {VAR: make_alarms(WIN_START + pd.Timedelta("20min"), T0 + pd.Timedelta("20h"))}
    )
    assert pm[VAR]["precision"] == pytest.approx(0.5)
    assert pm[VAR]["recall"] == pytest.approx(1 / 12)


def test_point_metrics_end_exclusive_and_no_grace():
    # the alarm on the first step after the (exclusive) end is a false positive, not a hit
    pm = point_metrics(make_labels(), {VAR: make_alarms(WIN_END)})
    assert pm[VAR]["recall"] == pytest.approx(0.0)
    assert pm[VAR]["precision"] == pytest.approx(0.0)
    assert pm[VAR]["f1"] == pytest.approx(0.0)


def test_point_metrics_only_score_2024():
    idx = pd.date_range("2023-06-01", periods=288 * 2, freq="5min", tz="UTC")
    label = make_label(start=pd.Timestamp("2023-06-01 02:00", tz="UTC"), end=pd.Timestamp("2023-06-01 03:00", tz="UTC"))
    alarms = make_alarms(pd.Timestamp("2023-06-01 02:00", tz="UTC"), index=idx)
    m = point_metrics(make_labels(label), {VAR: alarms})[VAR]
    assert np.isnan(m["recall"]) and np.isnan(m["precision"])  # nothing scored outside 2024


# ----------------------------------------------------------------- false alarms


def test_far_excludes_gap_filled_variant():
    # one day, one station, three alarmed cells, one of which sits on a gap-filled value
    day = INDEX[:288]
    alarms = {VAR: make_alarms(day[10], day[50], day[51], index=day)}
    flags = pd.DataFrame(0, index=INDEX, columns=["77"], dtype="int8")
    flags.loc[day[50], "77"] = 2
    far = false_alarm_rate(alarms, {VAR: flags}, slice("2024-06-01", "2024-06-01"))
    assert far.loc[VAR, "alarms_per_station_day"] == pytest.approx(3.0)
    assert far.loc[VAR, "alarms_per_station_day_excl_gapfilled"] == pytest.approx(2.0)


def test_far_period_slice_and_station_day_normalisation():
    # two stations, alarms on day 1 (2) and day 2 (1); scoring only day 1 -> 2 / (2 stations * 1 day)
    alarms = {VAR: make_alarms((INDEX[3], "77"), (INDEX[4], "78"), (INDEX[300], "77"), stations=("77", "78"))}
    flags = {VAR: pd.DataFrame(0, index=INDEX, columns=["77", "78"], dtype="int8")}
    far = false_alarm_rate(alarms, flags, slice("2024-06-01", "2024-06-01"))
    assert far.loc[VAR, "alarms_per_station_day"] == pytest.approx(1.0)
    far = false_alarm_rate(alarms, flags, slice("2024-06-01", "2024-06-03"))
    assert far.loc[VAR, "alarms_per_station_day"] == pytest.approx(3 / (2 * 3))


def test_far_counts_episodes_as_well_as_cells():
    day = INDEX[:288]
    alarms = {VAR: make_alarms(day[10], day[11], day[12], day[100], index=day)}  # 4 cells, 2 episodes
    flags = {VAR: pd.DataFrame(0, index=INDEX, columns=["77"], dtype="int8")}
    far = false_alarm_rate(alarms, flags, slice("2024-06-01", "2024-06-01"))
    assert far.loc[VAR, "alarms_per_station_day"] == pytest.approx(4.0)
    assert far.loc[VAR, "episodes_per_station_day"] == pytest.approx(2.0)


# ------------------------------------------------------------ sensitivity, MDM90


def events_fixture() -> pd.DataFrame:
    """offset: size 1 -> 1/4 detected, size 2 -> 4/5, size 3 -> 9/10 (both counted per scenario)."""
    rows = []
    for size, n_det, n in [(1.0, 1, 4), (2.0, 4, 5), (3.0, 9, 10), (5.0, 10, 10)]:
        for i in range(n):
            rows.append({"scenario_id": f"o-{size}-{i}", "family": "offset", "size": size, "detected": i < n_det})
    for i in range(4):  # a family that never reaches 0.9
        rows.append({"scenario_id": f"n-{i}", "family": "noise", "size": 1.0, "detected": i < 2})
    for i in range(3):  # a family without a size parameter
        rows.append({"scenario_id": f"s-{i}", "family": "stuck", "size": np.nan, "detected": True})
    return pd.DataFrame(rows)


def test_sensitivity_recall_by_family_and_size():
    sens = sensitivity(events_fixture())
    assert sens.loc["offset", 1.0] == pytest.approx(0.25)
    assert sens.loc["offset", 2.0] == pytest.approx(0.8)
    assert sens.loc["offset", 3.0] == pytest.approx(0.9)
    assert sens.loc["noise", 1.0] == pytest.approx(0.5)
    assert pd.isna(sens.loc["noise", 5.0])
    assert list(sens.columns[:4]) == [1.0, 2.0, 3.0, 5.0]  # ascending sizes


def test_sensitivity_counts_each_scenario_once():
    # a multi-station scenario has several label rows but is one scenario
    ev = pd.DataFrame(
        {
            "scenario_id": ["a", "a", "a", "b"],
            "family": ["multi_offset"] * 4,
            "size": [2.0] * 4,
            "detected": [True, True, True, False],
        }
    )
    assert sensitivity(ev).loc["multi_offset", 2.0] == pytest.approx(0.5)


def test_mdm90_smallest_size_reaching_threshold():
    out = mdm90(events_fixture())
    assert out["offset"] == 3.0  # 0.25, 0.8, 0.9 -> first size with recall >= 0.9
    assert pd.isna(out["noise"])  # never reaches 0.9
    assert pd.isna(out["stuck"])  # no size parameter
    assert isinstance(out, pd.Series)


# ---------------------------------------------------------------------- leakage


def test_leakage_counts_unalarmed_faulted_cells():
    # 6-cell fault, 2 alarmed -> 4 of 6 cells released
    label = make_label(end=WIN_START + 6 * STEP, size_phys=1.5)
    alarms = make_alarms(WIN_START + STEP, WIN_START + 3 * STEP)
    out = release_leakage(make_labels(label), {VAR: alarms})
    row = out.loc[VAR]
    assert row["faulted_cells"] == 6
    assert row["leaked_cells"] == 4
    assert row["leak_rate"] == pytest.approx(4 / 6)
    assert row["mean_leaked_magnitude"] == pytest.approx(1.5)
    assert row["leaked_cell_hours"] == pytest.approx(4 * 5 / 60)
    assert out.loc["overall", "leak_rate"] == pytest.approx(4 / 6)
    assert out.loc["overall", "leaked_cells"] == 4


def test_leakage_ignores_alarms_outside_the_faulted_cells():
    label = make_label(end=WIN_START + 6 * STEP)
    # alarms on the grace steps after the window and elsewhere do not contain any faulted cell
    alarms = make_alarms(WIN_START + 6 * STEP, WIN_START + 7 * STEP, T0)
    out = release_leakage(make_labels(label), {VAR: alarms})
    assert out.loc[VAR, "leak_rate"] == pytest.approx(1.0)


def test_leakage_per_variable_mean_magnitude_and_weighting():
    t = make_label(end=WIN_START + 4 * STEP, size_phys=2.0)
    r = make_label(
        scenario_id="dev-rain_scale-pr-0001",
        family="rain_scale",
        variable="precipitation",
        end=WIN_START + 2 * STEP,
        size_phys=3.0,
    )
    alarms = {VAR: make_alarms(WIN_START), "precipitation": make_alarms()}
    out = release_leakage(make_labels(t, r), alarms, impact_weight={VAR: 10.0, "precipitation": 100.0})
    assert out.loc[VAR, "leaked_cells"] == 3
    assert out.loc["precipitation", "leaked_cells"] == 2
    assert out.loc["overall", "faulted_cells"] == 6
    assert out.loc["overall", "leaked_cells"] == 5
    # weighted leak = weight * sum(|size_phys|) over leaked cells * hours per step
    assert out.loc[VAR, "weighted_leak"] == pytest.approx(10.0 * 3 * 2.0 / 12)
    assert out.loc["precipitation", "weighted_leak"] == pytest.approx(100.0 * 2 * 3.0 / 12)
    assert out.loc["overall", "weighted_leak"] == pytest.approx(10.0 * 3 * 2.0 / 12 + 100.0 * 2 * 3.0 / 12)
    # mean magnitude mixes units across variables, so it is only reported per variable
    assert np.isnan(out.loc["overall", "mean_leaked_magnitude"])
    assert "weighted_leak" not in release_leakage(make_labels(t, r), alarms).columns


def test_leakage_uses_trust_state_when_present():
    label = make_label(end=WIN_START + 4 * STEP)
    # cell 0 alarmed AND trusted -> still leaked; cell 1 not alarmed but quarantined -> contained
    alarms = make_alarms(WIN_START)
    state = pd.DataFrame("trusted", index=INDEX, columns=["77"])
    state.loc[WIN_START + STEP, "77"] = "quarantined"
    state.loc[WIN_START + 2 * STEP, "77"] = "imputed"
    state.loc[WIN_START + 3 * STEP, "77"] = "suspicious"
    with_state = release_leakage(make_labels(label), {VAR: alarms}, states={VAR: state})
    assert with_state.loc[VAR, "leaked_cells"] == 1  # only the alarmed-but-trusted cell 0
    without_state = release_leakage(make_labels(label), {VAR: alarms})
    assert without_state.loc[VAR, "leaked_cells"] == 3  # boolean-alarm approximation


def test_leakage_state_only_applies_to_variables_that_have_one():
    t = make_label(end=WIN_START + 2 * STEP)
    rh = make_label(scenario_id="dev-offset-re-0001", variable="relative_humidity", end=WIN_START + 2 * STEP)
    alarms = {VAR: make_alarms(WIN_START), "relative_humidity": make_alarms(WIN_START)}
    state = pd.DataFrame("trusted", index=INDEX, columns=["77"])
    out = release_leakage(make_labels(t, rh), alarms, states={VAR: state})
    assert out.loc[VAR, "leaked_cells"] == 2  # state says everything is trusted
    assert out.loc["relative_humidity", "leaked_cells"] == 1  # falls back to the alarm


def test_leakage_by_family_and_overlapping_labels_counted_once():
    a = make_label(end=WIN_START + 4 * STEP)
    b = make_label(scenario_id="dev-noise-ai-0002", family="noise", start=WIN_START + 2 * STEP, end=WIN_START + 6 * STEP)
    out = release_leakage(make_labels(a, b), {VAR: make_alarms()})
    assert out.loc[VAR, "faulted_cells"] == 6  # cells 0..5, the overlap (2, 3) once
    by_family = release_leakage(make_labels(a, b), {VAR: make_alarms()}, by="family")
    assert by_family.drop(index="overall")["faulted_cells"].sum() == 6
    assert by_family.loc["overall", "faulted_cells"] == 6
    assert set(by_family.index) == {"offset", "noise", "overall"}


# --------------------------------------------------------------------- records


def stat(*pairs, **extra) -> pd.DataFrame:
    return pd.DataFrame({"idx": [p[0] for p in pairs], "status": [p[1] for p in pairs], **extra})


def rec_labels(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["idx", "fault", "expected_status", "stage"])


def test_score_records_counts_false_positives():
    labels = rec_labels(
        (2, "tamper_value", "bad_signature", "transit"),
        (5, "replay", "replay", "transit"),
        (7, "replay", "replay", "transit"),
    )
    statuses = stat(
        (0, "ok"), (1, "ok"), (2, "bad_signature"), (3, "ok"),
        (4, "bad_signature"),  # unlabelled but flagged -> false positive
        (5, "replay"), (6, "ok"),
        (7, "ok"),  # labelled replay missed
        (8, "replay"),  # unlabelled but flagged -> false positive
    )
    out = score_records(labels, statuses)
    assert out.loc["tamper_value", "n"] == 1 and out.loc["tamper_value", "match_rate"] == 1.0
    assert out.loc["replay", "n"] == 2
    assert out.loc["replay", "matched"] == 1
    assert out.loc["replay", "match_rate"] == pytest.approx(0.5)
    assert out.loc["unlabelled", "n"] == 6
    assert out.loc["unlabelled", "false_positives"] == 2
    assert out.loc["unlabelled", "followon_gaps"] == 0


def test_score_records_wrong_status_is_not_a_match_and_missing_status_counts_as_miss():
    labels = rec_labels((1, "drop", "gap", "transit"), (3, "tamper_value", "bad_signature", "transit"))
    out = score_records(labels, stat((0, "ok"), (1, "bad_signature"), (2, "ok")))  # idx 3 never reported
    assert out.loc["drop", "matched"] == 0
    assert out.loc["tamper_value", "matched"] == 0 and out.loc["tamper_value", "n"] == 1
    assert out.loc["unlabelled", "false_positives"] == 0


def test_followon_gap_after_rejected_record_is_not_false_positive():
    # 2 sensors interleaved: even idx = sensor a, odd idx = sensor b. Record 2 (a) is tampered and
    # rejected in place, so a's next record (idx 4) legitimately reports gap.
    sensors = ["a", "b"] * 5
    statuses = ["ok", "ok", "bad_signature", "ok", "gap", "ok", "ok", "ok", "ok", "ok"]
    st = stat(*enumerate(statuses), sensor_id=sensors)
    labels = rec_labels((2, "tamper_value", "bad_signature", "transit"))
    out = score_records(labels, st)
    assert out.loc["unlabelled", "false_positives"] == 0
    assert out.loc["unlabelled", "followon_gaps"] == 1
    assert out.loc["tamper_value", "match_rate"] == 1.0


@pytest.mark.parametrize("fault", ["tamper_value", "shift_timestamp", "rewrite_stored"])
def test_followon_gap_excused_for_every_rejected_in_place_fault(fault):
    st = stat((0, "ok"), (1, "bad_signature"), (2, "gap"), (3, "ok"), sensor_id=["a"] * 4)
    out = score_records(rec_labels((1, fault, "bad_signature", "transit")), st)
    assert out.loc["unlabelled", "false_positives"] == 0


def test_followon_gap_after_unknown_sensor_is_excused_via_seq():
    # idx 2 is presented under a fake id; the real sensor a misses seq 1 and shows a gap at seq 2.
    st = stat(
        (0, "ok"), (1, "ok"), (2, "unknown_sensor"), (3, "ok"), (4, "gap"), (5, "ok"),
        sensor_id=["a", "b", "ghost", "b", "a", "b"],
        seq=[0, 0, 1, 1, 2, 2],
    )
    # a gap on the other sensor at an unrelated seq is still a false positive
    st2 = st.copy()
    st2.loc[3, "status"] = "gap"
    st2.loc[4, "status"] = "ok"
    labels = rec_labels((2, "unknown_sensor", "unknown_sensor", "transit"))
    assert score_records(labels, st).loc["unlabelled", "false_positives"] == 0
    assert score_records(labels, st2).loc["unlabelled", "false_positives"] == 1


def test_followon_exemption_is_narrow():
    labels = rec_labels((1, "tamper_value", "bad_signature", "transit"))
    base = {"sensor_id": ["a", "a", "a", "a", "b"]}
    # a gap on the sensor's *second* following record is a real false positive
    two_later = stat((0, "ok"), (1, "bad_signature"), (2, "ok"), (3, "gap"), (4, "ok"), **base)
    assert score_records(labels, two_later).loc["unlabelled", "false_positives"] == 1
    # a gap on a different sensor is a real false positive
    other = stat((0, "ok"), (1, "bad_signature"), (2, "ok"), (3, "ok"), (4, "gap"), **base)
    assert score_records(labels, other).loc["unlabelled", "false_positives"] == 1
    # one rejected record excuses at most one gap
    twice = stat((0, "ok"), (1, "bad_signature"), (2, "gap"), (3, "gap"), (4, "ok"), **base)
    out = score_records(labels, twice).loc["unlabelled"]
    assert out["false_positives"] == 1 and out["followon_gaps"] == 1
    # no rejection at all (the verifier accepted the tampered record) -> a later gap is not excused
    accepted = stat((0, "ok"), (1, "ok"), (2, "gap"), (3, "ok"), (4, "ok"), **base)
    assert score_records(labels, accepted).loc["unlabelled", "false_positives"] == 1
    # replays and drops leave no hole, so they excuse nothing
    replay_labels = rec_labels((1, "replay", "replay", "transit"))
    replayed = stat((0, "ok"), (1, "replay"), (2, "gap"), (3, "ok"), (4, "ok"), **base)
    assert score_records(replay_labels, replayed).loc["unlabelled", "false_positives"] == 1


def test_followon_gap_without_sensor_ids_assumes_single_stream():
    st = stat((0, "ok"), (1, "bad_signature"), (2, "gap"), (3, "ok"))
    out = score_records(rec_labels((1, "tamper_value", "bad_signature", "transit")), st)
    assert out.loc["unlabelled", "false_positives"] == 0


def test_score_records_rejects_duplicate_status_rows():
    with pytest.raises(ValueError, match="duplicate"):
        score_records(rec_labels(), stat((0, "ok"), (0, "ok")))


# ------------------------------------------------------------------------- CLI


def write_suite(tmp_path, labels, alarm_tables, flags=None):
    suites, clean, alarms = tmp_path / "suites", tmp_path / "clean", tmp_path / "alarms" / "run1" / "dev"
    (suites / "dev").mkdir(parents=True)
    clean.mkdir()
    alarms.mkdir(parents=True)
    labels.to_parquet(suites / "dev" / "labels.parquet")
    for var, table in alarm_tables.items():
        table.to_parquet(alarms / f"{var}.parquet")
        flag = (flags or {}).get(var, pd.DataFrame(0, index=INDEX, columns=table.columns, dtype="int8"))
        flag.to_parquet(clean / f"{var}_flag.parquet")
    return suites, clean, alarms


def test_cli_writes_events_summary_and_tables(tmp_path):
    from eval.run import main

    labels = make_labels(make_label(end=WIN_START + 6 * STEP))
    alarms = make_alarms(WIN_START + 2 * STEP, WIN_START + 3 * STEP)
    suites, clean, alarm_dir = write_suite(tmp_path, labels, {VAR: alarms})
    out = tmp_path / "results" / "run1" / "dev"
    rc = main(
        ["--suite", "dev", "--alarms", str(alarm_dir), "--out", str(out),
         "--suites-dir", str(suites), "--clean-dir", str(clean)]
    )
    assert rc == 0
    events = pd.read_parquet(out / "events.parquet")
    assert len(events) == 1 and bool(events.loc[0, "detected"])
    summary = json.loads((out / "summary.json").read_text())
    assert summary["suite"] == "dev"
    assert summary["leakage"]["overall"]["leak_rate"] == pytest.approx(4 / 6)
    assert summary["leakage"]["state_source"][VAR] == "alarm"
    assert summary["detection"]["overall"]["recall"] == 1.0
    assert "offset" in (out / "tables.md").read_text()


def test_cli_uses_state_file_when_present(tmp_path):
    from eval.run import main

    labels = make_labels(make_label(end=WIN_START + 4 * STEP))
    suites, clean, alarm_dir = write_suite(tmp_path, labels, {VAR: make_alarms(WIN_START)})
    pd.DataFrame("trusted", index=INDEX, columns=["77"]).to_parquet(alarm_dir / f"{VAR}_state.parquet")
    out = tmp_path / "out"
    main(["--suite", "dev", "--alarms", str(alarm_dir), "--out", str(out),
          "--suites-dir", str(suites), "--clean-dir", str(clean)])
    summary = json.loads((out / "summary.json").read_text())
    assert summary["leakage"]["state_source"][VAR] == "state"
    assert summary["leakage"]["overall"]["leaked_cells"] == 4


def test_cli_clean2024_reports_false_alarms(tmp_path):
    from eval.run import main

    suites, clean, alarm_dir = write_suite(tmp_path, make_labels().iloc[0:0], {VAR: make_alarms(INDEX[5], INDEX[40])})
    (suites / "clean2024").mkdir()
    make_labels().iloc[0:0].to_parquet(suites / "clean2024" / "labels.parquet")
    out = tmp_path / "out"
    assert main(["--suite", "clean2024", "--alarms", str(alarm_dir), "--out", str(out),
                 "--suites-dir", str(suites), "--clean-dir", str(clean)]) == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["false_alarms"][VAR]["alarms_per_station_day"] == pytest.approx(2 / 3)


def test_cli_records_bc(tmp_path):
    from eval.run import main

    suite = tmp_path / "suites" / "records_bc"
    suite.mkdir(parents=True)
    rec_labels((1, "tamper_value", "bad_signature", "transit")).to_parquet(suite / "labels.parquet")
    (suite / "records.jsonl").write_text(
        "".join(json.dumps({"sensor_id": "a", "seq": i}) + "\n" for i in range(4))
    )
    alarm_dir = tmp_path / "alarms" / "run1" / "records_bc"
    alarm_dir.mkdir(parents=True)
    # record 2 is a's next record after the rejected one -> legitimate gap, not a false positive
    (alarm_dir / "statuses.jsonl").write_text(
        "".join(json.dumps({"idx": i, "status": s}) + "\n" for i, s in enumerate(["ok", "bad_signature", "gap", "ok"]))
    )
    out = tmp_path / "out"
    assert main(["--suite", "records_bc", "--alarms", str(alarm_dir), "--out", str(out),
                 "--suites-dir", str(tmp_path / "suites")]) == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["records"]["false_positives"] == 0
    assert summary["records"]["followon_gaps"] == 1
    assert summary["records"]["by_fault"]["tamper_value"]["match_rate"] == 1.0
    assert (out / "events.parquet").exists() and (out / "tables.md").exists()
