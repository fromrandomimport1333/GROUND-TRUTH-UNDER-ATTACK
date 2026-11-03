"""Scoring of alarm tables against the labels of a standard-scenario suite (spec section 7).

Conventions that every function here shares:

* Label times (``start``, ``end``, ``onset``) and alarm indexes must be tz-aware (they are compared as
  UTC instants). A naive timestamp raises ``ValueError`` instead of silently matching nothing.
* ``end`` is **exclusive**: a faulted cell is a 5-min step ``t`` with ``start <= t < end``.
* An alarm table is a bool frame, index = 5-min UTC steps, columns = station ids (str).
* Event and point metrics and leakage are scored on 2024 only (``YEAR_2024``), 2023 being the
  fault-free calibration year; false alarms are counted on the fault-free ``clean2024`` suite.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

YEAR_2024 = slice("2024-01-01", "2024-12-31")  # label-based, like ``DataFrame.loc``
ONE_MINUTE = pd.Timedelta("1min")
ONE_DAY = pd.Timedelta("1D")

# ----------------------------------------------------------------------------- helpers


def _utc_times(frame: pd.DataFrame, column: str) -> pd.DatetimeIndex:
    """A labels time column as a UTC DatetimeIndex; naive timestamps are an error."""
    dtype = frame[column].dtype
    if not isinstance(dtype, pd.DatetimeTZDtype):
        raise ValueError(f"labels.{column} must be tz-aware (UTC) timestamps, got dtype {dtype}")
    return pd.DatetimeIndex(frame[column]).tz_convert("UTC")


def _onset_times(labels: pd.DataFrame, start: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Onset per label row. A missing column means onset = start; NaN/NaT stays NaT."""
    if "onset" not in labels.columns:
        return start
    column = labels["onset"]
    if column.isna().all():  # all-NaN float or object column carries no timezone
        return pd.DatetimeIndex([pd.NaT] * len(labels), tz="UTC")
    return _utc_times(labels, "onset")


class _Grid:
    """One variable's bool alarm table as a matrix plus time/station lookups."""

    def __init__(self, table: pd.DataFrame, name: str):
        index = table.index
        if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
            raise ValueError(f"alarm table for {name!r} must have a tz-aware DatetimeIndex")
        if not index.is_monotonic_increasing:
            raise ValueError(f"alarm table for {name!r} must have an increasing time index")
        self.name = name
        self.index = index.tz_convert("UTC")
        self.columns = pd.Index([str(c) for c in table.columns])
        values = table if table.dtypes.eq(bool).all() else table.astype("boolean").fillna(False)
        self.alarm = values.to_numpy(dtype=bool)
        self.step = _step(self.index)

    def stations(self, stations: pd.Series) -> np.ndarray:
        cols = self.columns.get_indexer(stations.astype(str))
        if (cols < 0).any():
            missing = sorted(set(stations.astype(str)[cols < 0]))
            raise ValueError(f"stations {missing} of variable {self.name!r} are missing from its alarm table")
        return cols

    def span(self, period: slice) -> tuple[int, int]:
        """Row range [lo, hi) of the index covered by ``period``."""
        sl = self.index.slice_indexer(period.start, period.stop)
        return int(sl.start), int(sl.stop)

    def rows(self, times: pd.DatetimeIndex) -> np.ndarray:
        """First row at or after each time."""
        return self.index.searchsorted(times, side="left")


def _step(index: pd.DatetimeIndex) -> pd.Timedelta:
    if len(index) < 2:
        return pd.Timedelta("5min")
    return pd.Series(index).diff().median()


def _grids(alarms: dict[str, pd.DataFrame], variables) -> dict[str, _Grid]:
    out = {}
    for var in variables:
        if var not in alarms:
            raise ValueError(f"no alarm table for variable {var!r}; got {sorted(alarms)}")
        out[var] = _Grid(alarms[var], var)
    return out


def _ratio(num: float, den: float) -> float:
    return float(num) / float(den) if den else float("nan")


# --------------------------------------------------------------------- event detection


def event_table(labels: pd.DataFrame, alarms: dict[str, pd.DataFrame], grace: str = "1h") -> pd.DataFrame:
    """Per label row: was the scenario caught, and how fast?

    A scenario is detected if any of its targeted (station, variable) cells alarms within
    ``[start, end + grace)`` (spec section 7). Returned columns, besides the labels:

    * ``station_detected`` / ``station_latency_min``: the same test restricted to the row's own station.
    * ``detected``: scenario level, True on every row of a scenario that any row's station caught.
    * ``latency_min``: first alarm in the window minus ``start`` (minutes; scenario level = earliest).
    * ``latency_from_onset_min``: first alarm at or after ``onset`` minus ``onset``. NaN when the
      onset is undefined (NaN/NaT: a drift that never reached 1 sigma) or no alarm follows it.
    """
    ev = labels.reset_index(drop=True).copy()
    n = len(ev)
    st_lat = np.full(n, np.nan)
    st_lat_onset = np.full(n, np.nan)
    if n:
        grace = pd.Timedelta(grace)
        start, end = _utc_times(ev, "start"), _utc_times(ev, "end")
        onset = _onset_times(ev, start)
        for var, rows in ev.groupby("variable").groups.items():
            grid = _grids(alarms, [var])[var]
            pos = np.asarray(rows)
            cols = grid.stations(ev.loc[pos, "station"])
            lo = grid.rows(start[pos])
            hi = grid.rows(end[pos] + grace)
            onset_pos = onset[pos]
            onset_row = grid.rows(onset_pos.where(onset_pos.notna(), start[pos]))
            for k, p in enumerate(pos):
                seg = grid.alarm[lo[k] : hi[k], cols[k]]
                if seg.any():
                    st_lat[p] = (grid.index[lo[k] + seg.argmax()] - start[p]) / ONE_MINUTE
                first_row = max(onset_row[k], lo[k])
                seg = grid.alarm[first_row : hi[k], cols[k]]
                if seg.any() and not pd.isna(onset[p]):
                    st_lat_onset[p] = (grid.index[first_row + seg.argmax()] - onset[p]) / ONE_MINUTE
    ev["station_detected"] = ~np.isnan(st_lat)
    ev["station_latency_min"] = st_lat
    key = ev["scenario_id"] if "scenario_id" in ev.columns else pd.Series(np.arange(n))
    ev["detected"] = ev.groupby(key)["station_detected"].transform("any").astype(bool) if n else pd.Series(dtype=bool)
    ev["latency_min"] = pd.Series(st_lat).groupby(key).transform("min") if n else pd.Series(dtype=float)
    ev["latency_from_onset_min"] = pd.Series(st_lat_onset).groupby(key).transform("min") if n else pd.Series(dtype=float)
    return ev


# ------------------------------------------------------------------------ point metrics


def point_metrics(labels: pd.DataFrame, alarms: dict[str, pd.DataFrame], period: slice = YEAR_2024):
    """Precision / recall / F1 per variable over (time, station) cells, no point adjustment.

    Cells are scored strictly: a faulted cell is a true positive only if it is itself alarmed, and an
    alarm on any cell outside every fault window (grace steps included) is a false positive. Only the
    steps inside ``period`` count. Undefined ratios (nothing faulted, nothing alarmed) are NaN.
    """
    lab = labels.reset_index(drop=True)
    start, end = _utc_times(lab, "start"), _utc_times(lab, "end")
    out: dict[str, dict[str, float]] = {}
    for var, table in alarms.items():
        grid = _Grid(table, var)
        lo, hi = grid.span(period)
        faulted = np.zeros((hi - lo, len(grid.columns)), dtype=bool)
        rows = np.flatnonzero((lab["variable"] == var).to_numpy())
        if len(rows):
            cols = grid.stations(lab.loc[rows, "station"])
            a = np.clip(grid.rows(start[rows]), lo, hi) - lo
            b = np.clip(grid.rows(end[rows]), lo, hi) - lo
            for k in range(len(rows)):
                faulted[a[k] : b[k], cols[k]] = True
        alarmed = grid.alarm[lo:hi]
        tp = int((alarmed & faulted).sum())
        fp = int((alarmed & ~faulted).sum())
        fn = int((~alarmed & faulted).sum())
        out[var] = {
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "f1": _ratio(2 * tp, 2 * tp + fp + fn),
            "tp": float(tp),
            "fp": float(fp),
            "fn": float(fn),
        }
    return out


# -------------------------------------------------------------------------- false alarms


def _episodes(alarmed: np.ndarray) -> int:
    """Number of contiguous alarm runs per station (rising edges), summed over stations."""
    previous = np.vstack([np.zeros((1, alarmed.shape[1]), dtype=bool), alarmed[:-1]])
    return int((alarmed & ~previous).sum())


def false_alarm_rate(
    alarms: dict[str, pd.DataFrame], flags: dict[str, pd.DataFrame], period: slice
) -> pd.DataFrame:
    """Alarm rates on fault-free data, one row per variable.

    * ``alarms_per_station_day``: alarmed 5-min cells per station-day of ``period``.
    * ``alarms_per_station_day_excl_gapfilled``: the same, counting only cells on measured values
      (provider flag 0). Both use the same denominator, so overall = measured + gap-filled share.
    * ``episodes_per_station_day[_excl_gapfilled]``: contiguous alarm runs instead of cells.
    * ``gapfilled_cell_share``: share of cells in ``period`` with flag > 0.
    """
    rows = {}
    for var, table in alarms.items():
        if var not in flags:
            raise ValueError(f"no gap-fill flag table for variable {var!r}")
        grid = _Grid(table, var)
        lo, hi = grid.span(period)
        alarmed = grid.alarm[lo:hi]
        flag = flags[var].reindex(index=grid.index[lo:hi], columns=grid.columns).to_numpy(dtype=float)
        filled = np.nan_to_num(flag, nan=0.0) > 0
        station_days = alarmed.shape[0] * (grid.step / ONE_DAY) * alarmed.shape[1]
        measured = alarmed & ~filled
        rows[var] = {
            "alarms_per_station_day": _ratio(alarmed.sum(), station_days),
            "alarms_per_station_day_excl_gapfilled": _ratio(measured.sum(), station_days),
            "episodes_per_station_day": _ratio(_episodes(alarmed), station_days),
            "episodes_per_station_day_excl_gapfilled": _ratio(_episodes(measured), station_days),
            "alarm_cells": int(alarmed.sum()),
            "alarm_cells_excl_gapfilled": int(measured.sum()),
            "gapfilled_cell_share": _ratio(filled.sum(), filled.size),
            "station_days": float(station_days),
        }
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("variable")


# ---------------------------------------------------------------- sensitivity and MDM90


def sensitivity_long(events: pd.DataFrame) -> pd.DataFrame:
    """Scenario counts and recall per (family, size). Each scenario counts once, however many
    stations it labels. A family without a size parameter has a single NaN-size group."""
    cols = [c for c in ("scenario_id", "family", "size", "detected") if c in events.columns]
    ev = events[cols]
    if "scenario_id" in ev.columns:
        ev = ev.drop_duplicates("scenario_id")
    ev = ev.assign(detected=ev["detected"].astype(bool))
    grouped = ev.groupby(["family", "size"], dropna=False)["detected"].agg(n="size", detected="sum")
    grouped["recall"] = grouped["detected"] / grouped["n"]
    return grouped.reset_index()


def sensitivity(events: pd.DataFrame) -> pd.DataFrame:
    """Recall by family (rows) x size (columns, ascending; NaN-size group last)."""
    table = sensitivity_long(events).set_index(["family", "size"])["recall"].unstack("size")
    sizes = [c for c in table.columns if not pd.isna(c)]
    return table[sorted(sizes) + [c for c in table.columns if pd.isna(c)]]


def mdm90(events: pd.DataFrame, threshold: float = 0.9) -> pd.Series:
    """Per family, the smallest size whose recall is >= ``threshold`` (NaN if none, or no size).

    "Smallest" is on the raw size parameter, so for ``rain_scale`` / ``gain`` (where 1.0 is the
    identity) it is the smallest factor, not the smallest departure from 1.
    """
    long = sensitivity_long(events)
    out = {}
    for family, group in long.groupby("family"):
        hit = group.dropna(subset=["size"]).query("recall >= @threshold - 1e-12")
        out[family] = float(hit["size"].min()) if len(hit) else float("nan")
    return pd.Series(out, name="mdm90", dtype=float).rename_axis("family")


# ----------------------------------------------------------------------- release leakage

TRUSTED = "trusted"


def _leak_by_label(labels, alarms, states, period):
    """Per label row: faulted cells, leaked cells and the sums needed for magnitudes/weights.

    A cell is a (5-min step, station) of one variable inside a label's ``[start, end)``. Where two
    labels overlap on a cell, the cell is counted once, for the later label row. A cell is contained
    when the detector's trust state is not ``trusted`` (variables with a state table), otherwise when
    the cell itself is alarmed. Late alarms do not recall a cell already released at its own step, so
    no grace applies (grace only matters for event latency).
    """
    lab = labels.reset_index(drop=True)
    n = len(lab)
    start, end = _utc_times(lab, "start"), _utc_times(lab, "end")
    magnitude = np.abs(lab["size_phys"].to_numpy(dtype=float)) if "size_phys" in lab.columns else np.full(n, np.nan)
    cols = ["faulted_cells", "leaked_cells", "leaked_mag_cells", "leaked_mag_sum", "step_hours"]
    counts = pd.DataFrame(0.0, index=lab.index, columns=cols)
    source = {}
    for var, rows in lab.groupby("variable").groups.items():
        grid = _grids(alarms, [var])[var]
        lo, hi = grid.span(period)
        pos = np.asarray(rows)
        station_col = grid.stations(lab.loc[pos, "station"])
        a = np.clip(grid.rows(start[pos]), lo, hi) - lo
        b = np.clip(grid.rows(end[pos]), lo, hi) - lo
        owner = np.full((hi - lo, len(grid.columns)), -1, dtype=np.int32)
        for k, p in enumerate(pos):
            owner[a[k] : b[k], station_col[k]] = p
        contained = grid.alarm[lo:hi]
        source[var] = "alarm"
        if states and var in states:
            state = states[var].reindex(index=grid.index[lo:hi], columns=grid.columns)
            contained = np.where(state.notna().to_numpy(), ~state.eq(TRUSTED).to_numpy(), contained)
            source[var] = "state"
        faulted = owner >= 0
        leaked = faulted & ~contained
        who = owner[leaked]
        finite = np.isfinite(magnitude)
        counts["faulted_cells"] += np.bincount(owner[faulted], minlength=n)
        counts["leaked_cells"] += np.bincount(who, minlength=n)
        counts["leaked_mag_cells"] += np.bincount(who, weights=finite[who].astype(float), minlength=n)
        counts["leaked_mag_sum"] += np.bincount(who, weights=np.nan_to_num(magnitude)[who], minlength=n)
        counts.loc[pos, "step_hours"] = grid.step / pd.Timedelta("1h")
    return lab, counts, source


def release_leakage(
    labels: pd.DataFrame,
    alarms: dict[str, pd.DataFrame],
    *,
    impact_weight: dict[str, float] | None = None,
    states: dict[str, pd.DataFrame] | None = None,
    by: str = "variable",
    period: slice = YEAR_2024,
) -> pd.DataFrame:
    """Share of faulted cells released as trusted: the corrupted observation that reached the analyst.

    One row per ``by`` group (default per variable) plus ``overall``, with ``faulted_cells``,
    ``leaked_cells``, ``leak_rate``, ``mean_leaked_magnitude`` (mean |size_phys| over leaked cells
    that have a magnitude), ``leaked_cell_hours`` (leaked cells x step length in hours) and, if
    ``impact_weight`` (variable -> downstream weight per unit of magnitude) is given,
    ``weighted_leak`` = weight x sum(|size_phys| over leaked cells) x step hours.

    ``states`` maps a variable to a table of trust states (trusted / suspicious / quarantined /
    imputed, same index/columns as the alarm table): for such a variable a cell is contained only when
    its state is not ``trusted``; without one the boolean alarm is used. The mean magnitude is NaN for a
    group that mixes variables (it would mix units), which includes ``overall`` in general.
    """
    lab, counts, _ = _leak_by_label(labels, alarms, states, period)
    if impact_weight is not None:
        missing = sorted(set(lab["variable"]) - set(impact_weight))
        if missing:
            raise ValueError(f"impact_weight is missing variables {missing}")
        counts["weighted_leak"] = (
            lab["variable"].map(impact_weight).to_numpy(dtype=float) * counts["leaked_mag_sum"] * counts["step_hours"]
        )
    counts["leaked_cell_hours"] = counts["leaked_cells"] * counts["step_hours"]
    counts["variable"] = lab["variable"]
    keys = lab[by] if by != "variable" else lab["variable"]
    sums = [c for c in counts.columns if c not in ("step_hours", "variable")]

    def summarise(frame: pd.DataFrame) -> dict[str, float]:
        row = {c: frame[c].sum() for c in sums}
        single_unit = frame["variable"].nunique() == 1
        row["mean_leaked_magnitude"] = _ratio(row["leaked_mag_sum"], row["leaked_mag_cells"]) if single_unit else np.nan
        row["leak_rate"] = _ratio(row["leaked_cells"], row["faulted_cells"])
        return row

    table = {name: summarise(frame) for name, frame in counts.groupby(keys, sort=True)}
    table["overall"] = summarise(counts)
    out = pd.DataFrame.from_dict(table, orient="index")
    order = ["faulted_cells", "leaked_cells", "leak_rate", "mean_leaked_magnitude", "leaked_cell_hours"]
    if impact_weight is not None:
        order.append("weighted_leak")
    out = out[order].rename_axis(by)
    out[["faulted_cells", "leaked_cells"]] = out[["faulted_cells", "leaked_cells"]].astype("int64")
    return out


# ------------------------------------------------------------------------------ records

OK_STATUS = "ok"
ACCEPTED_STATUSES = frozenset({"ok", "gap"})
# Faults that change a record where it sits: a correct verifier rejects it and its sensor's sequence
# keeps a hole, so the next record of that sensor legitimately reports ``gap`` (Task 8 hazard).
REJECTED_IN_PLACE = frozenset({"tamper_value", "shift_timestamp", "rewrite_stored", "unknown_sensor"})


def _find_followon(out: pd.DataFrame, pos: int, fault: str, taken: set[int]) -> int | None:
    """Row position of the gap that the rejected record at ``pos`` legitimately causes, or None.

    With sensor ids: the next unlabelled record of the same sensor (for ``unknown_sensor`` the sensor
    id is the fake one, so the real sensor is matched by ``seq`` = rejected seq + 1 when ``seq`` is
    known, else it is the next unlabelled gap). Without sensor ids: the next unlabelled record of
    the single assumed stream. A record can excuse only one gap.
    """
    sensor = out["sensor_id"] if "sensor_id" in out.columns else None
    seq = out["seq"] if "seq" in out.columns else None
    for q in range(pos + 1, len(out)):
        if pd.notna(out["fault"].iat[q]) or q in taken:
            continue
        if sensor is not None and fault == "unknown_sensor":
            if out["status"].iat[q] != "gap":
                continue
            if seq is not None and pd.notna(seq.iat[pos]) and seq.iat[q] != seq.iat[pos] + 1:
                continue
            return q
        if sensor is not None and sensor.iat[q] != sensor.iat[pos]:
            continue
        return q if out["status"].iat[q] == "gap" else None
    return None


def record_outcomes(labels: pd.DataFrame, statuses: pd.DataFrame) -> pd.DataFrame:
    """Per record: the verifier status next to the label, and how it was classified.

    ``statuses`` has ``idx, status`` and optionally ``sensor_id`` and ``seq`` (the position and the
    fields of the record in ``records.jsonl``). ``kind`` is ``labelled`` (has a label),
    ``ok`` (unlabelled, status ok), ``followon_gap`` (an excused gap, see ``score_records``) or
    ``false_positive`` (anything else on an unlabelled record). A labelled record without a status
    row gets status ``missing`` and never matches.
    """
    for name, frame in (("statuses", statuses), ("labels", labels)):
        if frame["idx"].duplicated().any():
            raise ValueError(f"duplicate idx in {name}")
    out = statuses.sort_values("idx").reset_index(drop=True)
    absent = labels.loc[~labels["idx"].isin(out["idx"]), "idx"]
    if len(absent):
        out = pd.concat([out, pd.DataFrame({"idx": absent.to_numpy(), "status": "missing"})], ignore_index=True)
        out = out.sort_values("idx").reset_index(drop=True)
    info = labels.set_index("idx")
    out["fault"] = out["idx"].map(info["fault"])
    out["expected_status"] = out["idx"].map(info["expected_status"])
    labelled = out["fault"].notna()
    out["matched"] = labelled & (out["status"] == out["expected_status"])
    out["kind"] = np.where(labelled, "labelled", np.where(out["status"] == OK_STATUS, "ok", "false_positive"))
    taken: set[int] = set()
    for pos in np.flatnonzero(labelled & out["fault"].isin(REJECTED_IN_PLACE) & ~out["status"].isin(ACCEPTED_STATUSES | {"missing"})):
        hit = _find_followon(out, int(pos), out["fault"].iat[pos], taken)
        if hit is not None:
            taken.add(hit)
            out.loc[hit, "kind"] = "followon_gap"
    return out


def score_records(labels: pd.DataFrame, statuses: pd.DataFrame) -> pd.DataFrame:
    """Verifier agreement with the labels: match rate per fault, false positives on the rest.

    One row per fault (``n`` labelled records, ``matched`` = status equals the expected status,
    ``match_rate``) and a final ``unlabelled`` row: ``n`` unlabelled records, ``false_positives`` =
    those with a non-ok status, ``followon_gaps`` = excused ones, ``matched`` = n - false_positives.

    Excused: a record rejected in place (tamper_value / shift_timestamp / rewrite_stored /
    unknown_sensor, and actually rejected by the verifier) leaves a sequence hole, so a correct
    verifier reports ``gap`` on that sensor's next unlabelled record. That gap is not a false positive;
    each rejected record excuses at most one. Pass ``sensor_id`` (and ``seq``) columns in ``statuses`` to
    locate "that sensor's next record"; without them one stream is assumed.
    """
    out = record_outcomes(labels, statuses)
    rows = []
    for fault, group in out[out["fault"].notna()].groupby("fault", sort=True):
        rows.append(
            {
                "fault": fault,
                "expected_status": "|".join(sorted(group["expected_status"].unique())),
                "n": len(group),
                "matched": int(group["matched"].sum()),
                "match_rate": group["matched"].mean(),
                "false_positives": 0,
                "followon_gaps": 0,
            }
        )
    unl = out[out["fault"].isna()]
    fps = int((unl["kind"] == "false_positive").sum())
    rows.append(
        {
            "fault": "unlabelled",
            "expected_status": OK_STATUS,
            "n": len(unl),
            "matched": len(unl) - fps,
            "match_rate": _ratio(len(unl) - fps, len(unl)),
            "false_positives": fps,
            "followon_gaps": int((unl["kind"] == "followon_gap").sum()),
        }
    )
    return pd.DataFrame(rows).set_index("fault")
