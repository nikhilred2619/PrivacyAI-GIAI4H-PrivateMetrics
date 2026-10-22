"""Write SYNTHETIC labels.csv and patients.csv next to host.example.json so the
example can be registered and scored locally. Not real data.

Usage: python examples/host-config/make_synthetic.py [--items 1000] [--seed 1]
Then:  python -m privatemetrics host-register --config examples/host-config/host.example.json \
           --epsilon-total 20 --patient-map examples/host-config/patients.csv
"""
import argparse
import csv
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--items", type=int, default=1000)
ap.add_argument("--seed", type=int, default=1)
a = ap.parse_args()
rng = random.Random(a.seed)
ids = [f"syn_{i:06d}" for i in range(a.items)]
with (HERE / "labels.csv").open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Image", "dr_grade"])
    w.writerows((i, rng.choices(range(5), weights=[45, 15, 20, 10, 10])[0]) for i in ids)
with (HERE / "patients.csv").open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["item_id", "patient_id"])
    w.writerows((i, f"patient_{n // 2:06d}") for n, i in enumerate(ids))  # two eyes per patient
print(f"wrote {a.items} synthetic labels and a patient map (2 items per patient) to {HERE}")
