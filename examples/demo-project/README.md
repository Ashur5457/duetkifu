# Demo project

A complete Duetkifu project folder with synthetic data (not experimental results): a search for the best mix of two additives, A and B, over six cycles.

- `report.json`: the demo report (cycles 1 to 3)
- `kifu.json`: the research record behind it, 25 moves (open **Kifu view**): six cycles, side questions that went nowhere (an idea that was wrong, a path whose data could not be trusted, an oven that failed, a price that was too high), two corrections, two independent audits with numbers that do not all match, a conclusion waiting for the last cycle, and moves you started yourself. Every number in it comes from the data files below
- `data/cycle1-runs.csv` to `cycle6-runs.csv`: raw data, one file per cycle (cycle 6 is half measured). `data/oven-log.csv` and `oven-repeat.csv`: the oven tests
- `scripts/`: `combine_cycles.py` (cycles 1 to 3, the report's dataset), `summarise_cycles.py` (one row per cycle) and `make_figures.py`, and their results in `derived_data/`. Each run is recorded as a step, so a chart or a move shows its data chain (click the *Source* line under a chart)
- `data/cycle4-runs.csv` and the later cycles are not imported into the report yet (try **Import** in the Folder tab)
- `habits/`: an SVG figure and a matplotlib style (try **Learn my habits**)

Start it with `python duetkifu.py examples/demo-project` from the repository folder, or open [`duetkifu.html`](../../duetkifu.html) in Chrome or Edge, click **Open project folder**, and choose this folder. The page saves your changes into `report.json` and `kifu.json`, so work on a copy if you want to keep the original. See the [tutorial](../../docs/tutorial.md).
