"""Two figures of the search: the scores of each cycle, and where the runs of every cycle sit in the A-B plane.

Input : data/cycle1-runs.csv ... data/cycle6-runs.csv
Output: derived_data/figures/cycle-scores.png, derived_data/figures/region-map.png
Run   : python scripts/make_figures.py   (from the project folder; needs matplotlib)
"""
import csv, glob, os, re
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cycles = {}
for path in glob.glob(os.path.join(PROJ, 'data', 'cycle*-runs.csv')):
    with open(path, newline='', encoding='utf-8') as f:
        cycles[int(re.search(r'cycle(\d+)', path).group(1))] = [(float(r['Additive A (wt%)']), float(r['Additive B (wt%)']), float(r['Score'])) for r in csv.DictReader(f)]
os.makedirs(os.path.join(PROJ, 'derived_data', 'figures'), exist_ok=True)
plt.rcParams.update({'font.size': 9, 'axes.spines.top': False, 'axes.spines.right': False})

fig, ax = plt.subplots(figsize=(6, 3.2), dpi=130)
for c, rows in sorted(cycles.items()):
    ys = [r[2] for r in rows]
    ax.scatter([c + (i % 5 - 2) * .06 for i in range(len(ys))], ys, s=16, alpha=.75, color='#1f6f8b')
    ax.hlines(sorted(ys)[len(ys) // 2], c - .3, c + .3, color='#c0392b', lw=1.6)
ax.set_xlabel('Cycle'); ax.set_ylabel('Score'); ax.set_xticks(sorted(cycles)); ax.set_ylim(-.02, 1.02)
fig.tight_layout(); fig.savefig(os.path.join(PROJ, 'derived_data', 'figures', 'cycle-scores.png')); plt.close(fig)

fig, ax = plt.subplots(figsize=(5.2, 3.8), dpi=130)
allr = [r for rows in cycles.values() for r in rows]
sc = ax.scatter([r[0] for r in allr], [r[1] for r in allr], c=[r[2] for r in allr], cmap='viridis', s=22, vmin=0, vmax=1)
fig.colorbar(sc, ax=ax, label='Score'); ax.set_xlabel('Additive A (wt%)'); ax.set_ylabel('Additive B (wt%)')
fig.tight_layout(); fig.savefig(os.path.join(PROJ, 'derived_data', 'figures', 'region-map.png')); plt.close(fig)
print(len(allr), 'runs')
