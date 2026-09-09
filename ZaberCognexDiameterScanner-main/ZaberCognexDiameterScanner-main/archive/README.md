# Archived scan output

Snapshot of the scan data and reports generated up to the archive date. These
are historical **outputs** only — nothing here is read back by the fixture
software, so the folder can be moved or pruned without affecting a run.

## `20260909/`

Everything produced up to 2026-09-09, at the point the online-trigger work
merged to `main`.

| Folder | Came from | Contents |
| --- | --- | --- |
| `data/` | `../data/` | 11 `*_combined_*.csv`, 1 `*_multirotation_*.csv`, 1 rolling `diameter_measurements_*.csv` |
| `plots/` | `../plots/` | 68 PNGs — `*_circle_fit_result_N.png`, `*_combined_*.png`, `*_multirotation_*.png` |
| `calibration_plots/` | `../calibration/plots/` | 11 verify reports — `verify_*.png` and `verify_report_*.{png,csv}` |

`calibration_plots/` is flattened out of `calibration/plots/` so the archive
cannot be mistaken for a calibration source directory.

## What was deliberately *not* archived

- **`../calibration/calibration_*.csv`** — live inputs, not old reports.
  `CalibrationVerify.find_calibration_files()` globs them to populate the
  calibration dropdown; moving them would break Apply Calibration and
  Calibration Verify.
- **`../data/GageStudies.mpx`** (and `.bak`) — Minitab gage-study workbook, an
  analysis file rather than a fixture-generated scan or report.

## Effects on the next run

Both are consequences of an empty output folder, not breakage:

- The rolling summary CSV starts a new `diameter_measurements_<timestamp>.csv`.
  It would have rolled anyway — the archived file predates the current
  `SUMMARY_CSV_HEADER`, so `save_csv` would not have reused it.
- `<part_id>_circle_fit_result_N.png` numbering restarts at `N=1` per part ID,
  since `save_plot` picks `N` by globbing the live `plots/` folder. New files
  are written; archived ones are never overwritten.
