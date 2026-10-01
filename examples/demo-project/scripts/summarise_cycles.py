"""One row per cycle: how many runs, the median and best score, where the best run was, and the share of runs scoring 0.7 or more.

Input : data/cycle1-runs.csv ... data/cycle6-runs.csv (raw data, one file per cycle)
Output: derived_data/cycle-summary.csv
Run   : python scripts/summarise_cycles.py   (from the project folder)
"""
import csv, glob, os, re, statistics

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = []
for path in sorted(glob.glob(os.path.join(PROJ, 'data', 'cycle*-runs.csv')), key=lambda p: int(re.search(r'cycle(\d+)', p).group(1))):
    with open(path, newline='', encoding='utf-8') as f:
        rows = [(float(r['Additive A (wt%)']), float(r['Additive B (wt%)']), float(r['Score'])) for r in csv.DictReader(f)]
    best = max(rows, key=lambda r: r[2])
    out.append([int(re.search(r'cycle(\d+)', path).group(1)), len(rows), round(statistics.median(r[2] for r in rows), 3), best[2], best[0], best[1],
                round(sum(r[2] >= 0.7 for r in rows) / len(rows), 3)])
os.makedirs(os.path.join(PROJ, 'derived_data'), exist_ok=True)
with open(os.path.join(PROJ, 'derived_data', 'cycle-summary.csv'), 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f, lineterminator='\n')
    w.writerow(['cycle', 'runs', 'median_score', 'best_score', 'best_a', 'best_b', 'share_ge_0.7'])
    w.writerows(out)
print(len(out), 'cycles')
