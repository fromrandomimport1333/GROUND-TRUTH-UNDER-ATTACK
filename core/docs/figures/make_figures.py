"""Regenerate the detector README figures from core/results/ (python core/docs/figures/make_figures.py)."""
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from pathlib import Path

FIGURES = Path(__file__).resolve().parent
RESULTS = FIGURES.parents[1] / "results"   # core/results

BLUE, ORANGE, GREY = "#2a78d6", "#eb6834", "#b4b2a9"
INK, INK2, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#e6e5e0"
plt.rcParams.update({"font.size": 10, "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "axes.edgecolor": GRID, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "font.family": "DejaVu Sans"})

def clean(ax, axis="x"):
    ax.grid(axis=axis, color=GRID, linewidth=.8); ax.set_axisbelow(True)
    for s in ("top", "right", "left" if axis == "x" else "bottom"):
        ax.spines[s].set_visible(False)

# 1. Model comparison (F1 on Nov-Dec 2023, same false-alert budget)
mc = pd.read_csv(RESULTS / "model_comparison_2023.csv").sort_values("f1")
labels = {"extra_trees": "Extra Trees (selected)", "boost_deep": "Gradient boosting (large)", "boost_small": "Gradient boosting (small)",
          "spatial": "Spatial consistency score", "rule_ensemble": "Rule ensemble (4 families)", "spatial_local": "Spatial score, drift-corrected",
          "gru_forecast": "GRU forecaster", "tcn_forecast": "TCN forecaster", "sequence_autoencoder": "Sequence autoencoder",
          "logistic": "Logistic regression", "isolation": "Isolation Forest"}
fig, ax = plt.subplots(figsize=(7.6, 4.2))
colors = [BLUE if m == "extra_trees" else GREY for m in mc.model]
ax.barh([labels[m] for m in mc.model], mc.f1, color=colors, height=.6, edgecolor=SURFACE, linewidth=2)
for y, v in enumerate(mc.f1):
    ax.text(v + .008, y, f"{v:.3f}", va="center", fontsize=9, color=INK)
ax.set_xlim(0, .7); ax.set_xlabel("F1 on Nov–Dec 2023 (≤ 0.05 false-alert episodes / stream-day, ≥ 95 % coverage)")
ax.set_title("Candidate detectors under the same false-alarm budget", loc="left", fontsize=11, color=INK)
clean(ax); fig.tight_layout(); fig.savefig(FIGURES / "model_comparison.png", dpi=160); plt.close(fig)

# 2. Detector vs rule ensemble, 2024 (seed 91000): rates in one panel, false alarms in its own panel (one axis each)
cmp = pd.read_csv(RESULTS / "detector_vs_rule_ensemble_2024.csv").query("suite == 'seed_91000'")
rule, ours = cmp.iloc[0], cmp.iloc[1]
metrics = [("precision", "Precision"), ("recall", "Recall"), ("f1", "F1"), ("event_recall", "Events detected")]
fig, (a, b) = plt.subplots(1, 2, figsize=(9, 3.6), gridspec_kw={"width_ratios": [3, 1.2]})
x = range(len(metrics)); w = .36
a.bar([i - w/2 for i in x], [rule[k] for k, _ in metrics], w, color=GREY, label="Rule ensemble", edgecolor=SURFACE, linewidth=2)
a.bar([i + w/2 for i in x], [ours[k] for k, _ in metrics], w, color=BLUE, label="This detector", edgecolor=SURFACE, linewidth=2)
for i, (k, _) in enumerate(metrics):
    a.text(i - w/2, rule[k] + .015, f"{rule[k]:.0%}" if k != "f1" else f"{rule[k]:.2f}", ha="center", fontsize=8, color=INK2)
    a.text(i + w/2, ours[k] + .015, f"{ours[k]:.0%}" if k != "f1" else f"{ours[k]:.2f}", ha="center", fontsize=8, color=INK)
a.set_xticks(list(x), [n for _, n in metrics]); a.set_ylim(0, 1); a.set_yticks([0, .25, .5, .75, 1], ["0", "25 %", "50 %", "75 %", "100 %"])
a.legend(frameon=False, loc="upper left"); a.set_title("2024, attack seed 91000", loc="left", fontsize=11); clean(a, "y")
fa = [rule.nominal_false_episodes_per_stream_day, ours.nominal_false_episodes_per_stream_day]
b.bar(["Rule\nensemble", "This\ndetector"], fa, color=[GREY, BLUE], width=.55, edgecolor=SURFACE, linewidth=2)
for i, v in enumerate(fa):
    b.text(i, v + .001, f"{v:.4f}", ha="center", fontsize=8, color=INK)
b.set_title("False-alert episodes\nper stream-day (lower is better)", loc="left", fontsize=10); b.set_ylim(0, .055); clean(b, "y")
fig.tight_layout(); fig.savefig(FIGURES / "detector_vs_rule_ensemble.png", dpi=160); plt.close(fig)

# 3. Recall per attack family, 2024 seed 91000
fam = pd.read_csv(RESULTS / "families_2024.csv").query("suite == 'seed_91000'").sort_values("point_recall")
withheld = {"pattern", "mimicry", "multivariate"}
names = [f"{f}  (withheld)" if f in withheld else f for f in fam.family]
fig, ax = plt.subplots(figsize=(7.6, 4.6))
y = range(len(fam)); h = .36
ax.barh([i + h/2 for i in y], fam.event_recall, h, color=ORANGE, label="Events detected", edgecolor=SURFACE, linewidth=2)
ax.barh([i - h/2 for i in y], fam.point_recall, h, color=BLUE, label="Attacked points flagged", edgecolor=SURFACE, linewidth=2)
ax.set_yticks(list(y), names); ax.set_xlim(0, 1); ax.set_xticks([0, .25, .5, .75, 1], ["0", "25 %", "50 %", "75 %", "100 %"])
ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.4, -0.08), ncol=2); ax.set_title("Recall per attack family (2024, seed 91000)", loc="left", fontsize=11)
clean(ax); fig.tight_layout(); fig.savefig(FIGURES / "family_recall.png", dpi=160); plt.close(fig)
print(f"Figures written to {FIGURES}")
