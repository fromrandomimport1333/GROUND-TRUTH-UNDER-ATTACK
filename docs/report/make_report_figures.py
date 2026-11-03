"""Figures sized for the IEEE two-column report (python docs/report/make_report_figures.py).

Drawn at the printed width of one column (3.5 in) so that 8 pt text stays 8 pt on the page.
Data: core/results/detector_vs_rule_ensemble_2024.csv (seed 91000).
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parents[1] / "core" / "results"
BLUE, GREY, INK, INK2, GRID = "#2a78d6", "#a8a69e", "#0b0b0b", "#3d3c39", "#dddcd6"
plt.rcParams.update({"font.size": 8, "font.family": "DejaVu Sans", "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "legend.fontsize": 7.5})


def clean(ax):
    ax.grid(axis="y", color=GRID, linewidth=.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


cmp = pd.read_csv(RESULTS / "detector_vs_rule_ensemble_2024.csv").query("suite == 'seed_91000'")
rule, ours = cmp.iloc[0], cmp.iloc[1]
metrics = [("precision", "Precision"), ("recall", "Recall\n(readings)"), ("f1", "F1"), ("event_recall", "Attacks\ndetected")]

fig, (a, b) = plt.subplots(1, 2, figsize=(3.5, 2.05), gridspec_kw={"width_ratios": [3.1, 1.15]})
w = .38
for i, (key, _) in enumerate(metrics):
    for dx, row, color in ((-w / 2, rule, GREY), (w / 2, ours, BLUE)):
        a.bar(i + dx, row[key], w, color=color)
        label = f"{row[key]:.2f}" if key == "f1" else f"{row[key]:.0%}"
        a.text(i + dx, row[key] + .02, label, ha="center", fontsize=6.5, color=INK)
a.set_xticks(range(len(metrics)), [name for _, name in metrics])
a.set_ylim(0, 1)
a.set_yticks([0, .5, 1], ["0", "50 %", "100 %"])
a.bar(0, 0, color=GREY, label="Rule ensemble")
a.bar(0, 0, color=BLUE, label="Learned detector")
a.legend(frameon=False, loc="upper left", handlelength=1, borderaxespad=0)
clean(a)

fa = [rule.nominal_false_episodes_per_stream_day, ours.nominal_false_episodes_per_stream_day]
b.bar([0, 1], fa, color=[GREY, BLUE], width=.6)
for i, v in enumerate(fa):
    b.text(i, v + .0012, f"{v:.3f}", ha="center", fontsize=6.5)
b.set_xticks([0, 1], ["Rules", "Ours"])
b.set_ylim(0, .056)
b.set_yticks([0, .025, .05], ["0", ".025", ".05"])
b.set_title("False alerts /\nstream-day", fontsize=7.5, color=INK2)
clean(b)

fig.tight_layout(pad=.3, w_pad=.6)
fig.savefig(HERE / "fig_detector_vs_rules.pdf")
plt.close(fig)
print("wrote", HERE / "fig_detector_vs_rules.pdf")
