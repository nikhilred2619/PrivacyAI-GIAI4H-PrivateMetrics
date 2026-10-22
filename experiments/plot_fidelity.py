"""Figures for docs/REFERENCE_TASK_FIDELITY_REPORT.md, drawn only from the
committed results file (no new measurements).

Usage: python experiments/plot_fidelity.py [results.json]   (needs matplotlib)
Writes docs/figures/fidelity_privacy_utility.png, fidelity_gap_bands.png,
fidelity_bias_ablation.png.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "figures"
COLORS = {"perfect": "#4C72B0", "strong": "#DD8452", "moderate": "#55A868", "strong-partial": "#C44E52",
          "majority-constant": "#8172B3"}


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "experiments" / "results" / "reference_fidelity.json"
    r = json.loads(src.read_text())
    eps = [float(e) for e in r["epsilons"]]
    cell = {(c["epsilon"], c["fixture"], c["metric"]): c for c in r["cells"]}
    OUT.mkdir(parents=True, exist_ok=True)
    note = f"Local measurement, route {r['route_version']}, {r['trials_per_cell']} releases per point; synthetic predictions, public labels"

    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
    for ax, metric in zip(axes, ["accuracy", "qwk"], strict=True):
        for fx in r["fixtures"]:
            ax.plot(eps, [cell[(e, fx, metric)]["rmse"] for e in r["epsilons"]], marker="o", label=fx, color=COLORS.get(fx))
        ax.set_xscale("log", base=2)
        ax.set_xticks(eps, [e for e in r["epsilons"]])
        ax.set_xlabel("ε per release")
        ax.set_title(f"{metric}: RMSE of released vs plaintext")
        ax.axvline(float(r["declared_epsilon"]), color="grey", ls="--", lw=1)
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("RMSE")
    axes[1].legend(fontsize=8)
    fig.suptitle(note, fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "fidelity_privacy_utility.png", dpi=130)
    plt.close(fig)

    metrics = ["accuracy", "qwk", "referableSensitivity", "referableSpecificity"]
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.6), sharey=True)
    for ax, metric in zip(axes, metrics, strict=True):
        cs = [cell[(e, "strong", metric)] for e in r["epsilons"]]
        ax.fill_between(eps, [c["q025"] for c in cs], [c["q975"] for c in cs], alpha=0.2, color="#DD8452", label="2.5–97.5%")
        ax.fill_between(eps, [c["q25"] for c in cs], [c["q75"] for c in cs], alpha=0.4, color="#DD8452", label="25–75%")
        ax.plot(eps, [c["mean_gap"] for c in cs], marker="o", color="#8C3B00", label="mean gap")
        ax.axhline(0, color="black", lw=0.8)
        ax.set_xscale("log", base=2)
        ax.set_xticks(eps, [e for e in r["epsilons"]])
        ax.set_title(metric, fontsize=9)
        ax.set_xlabel("ε")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("released − plaintext")
    axes[0].legend(fontsize=7)
    fig.suptitle("strong fixture — " + note, fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "fidelity_gap_bands.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    for metric, color in (("accuracy", "#4C72B0"), ("qwk", "#C44E52")):
        ax.plot(eps, [cell[(e, "strong", metric)]["mean_gap"] for e in r["epsilons"]], marker="o", color=color,
                label=f"{metric}, released (projection)")
        ax.plot(eps, [r["ablation_clip_only"][e][metric]["mean_gap"] for e in r["epsilons"]], marker="s", ls="--",
                color=color, label=f"{metric}, clip-only ablation (not released)")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(eps, [e for e in r["epsilons"]])
    ax.set_xlabel("ε per release")
    ax.set_ylabel("mean gap")
    ax.set_title("Post-processing bias (strong fixture)", fontsize=10)
    ax.legend(fontsize=7)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "fidelity_bias_ablation.png", dpi=130)
    plt.close(fig)
    print(f"wrote 3 figures to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
