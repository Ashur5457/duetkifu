"""Fit the log of the score with a quadratic surface in A and B, over cycles 1 to 5, and find its top.
A peak shaped like a bell becomes a quadratic in log scale. Runs scoring below 0.05 are left out (log of almost zero).
Also fits it again five times, each time without one cycle, to see how far the top moves.

Input : data/cycle1-runs.csv ... data/cycle5-runs.csv
Output: derived_data/surface.csv (one row per fit: all cycles, then without each cycle)
Run   : python scripts/fit_surface.py   (from the project folder; needs numpy)
"""
import csv, math, os
import numpy as np

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
runs = []
for c in range(1, 6):
    with open(os.path.join(PROJ, 'data', 'cycle%d-runs.csv' % c), newline='', encoding='utf-8') as f:
        runs += [(c, float(r['Additive A (wt%)']), float(r['Additive B (wt%)']), float(r['Score'])) for r in csv.DictReader(f)]


def fit(rows):
    rows = [r for r in rows if r[3] >= 0.05]
    X = np.array([[1, a, b, a * a, b * b, a * b] for _, a, b, _ in rows]); y = np.log([r[3] for r in rows])
    k = np.linalg.lstsq(X, y, rcond=None)[0]
    A = np.array([[2 * k[3], k[5]], [k[5], 2 * k[4]]]); oa, ob = np.linalg.solve(A, [-k[1], -k[2]])
    pred = X @ k; r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    top = math.exp(k[0] + k[1] * oa + k[2] * ob + k[3] * oa * oa + k[4] * ob * ob + k[5] * oa * ob)
    return [len(rows), round(oa, 2), round(ob, 2), round(top, 3), round(r2, 3)]


out = [['all'] + fit(runs)] + [['without %d' % c] + fit([r for r in runs if r[0] != c]) for c in range(1, 6)]
os.makedirs(os.path.join(PROJ, 'derived_data'), exist_ok=True)
with open(os.path.join(PROJ, 'derived_data', 'surface.csv'), 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f, lineterminator='\n'); w.writerow(['fit', 'runs', 'top_a', 'top_b', 'top_score', 'r2_log']); w.writerows(out)
print(out[0])
