# Gasket Diameter Inspection System

Zaber rotary stage + Cognex IL38 laser sensor fixture for measuring gasket
diameter and surface profile.

## Layout

- `ZaberCognexDiameterScanner-main/ZaberCognexDiameterScanner-main/DiameterMeasurement_Fixture/` — Python source
  - `gui.py` — main entry point, Tkinter control panel
  - `common.py` — shared hardware config (Zaber port, Cognex host, etc.) and
    the `CognexConnection` telnet helper
  - `DiameterScan.py` — circle-fit diameter measurement
  - `CalibrationScan.py` — 0–359° surface map, writes calibration CSV
  - `CalibrationVerify.py` — re-scans against a stored calibration, writes
    a multi-run report (PNG + CSV)
  - `test_cognex_trigger.py` — hardware-free checks of the trigger modes and
    the job gate, against a stub In-Sight responder (`python test_cognex_trigger.py`)
  - `sample_report.py` — offline sample-report generator: synthesizes scan data
    and runs it through the real report writers, no hardware needed
    (`python sample_report.py`)
- `ZaberCognexDiameterScanner-main/ZaberCognexDiameterScanner-main/calibration/` — calibration CSVs and `plots/` for verify reports
- `ZaberCognexDiameterScanner-main/ZaberCognexDiameterScanner-main/data/` — diameter scan CSVs
- `ZaberCognexDiameterScanner-main/ZaberCognexDiameterScanner-main/plots/` — diameter scan plots
- `Cognex/`, `Electrical/` — vendor files and panel drawings (not Python)

## Run

```powershell
cd ZaberCognexDiameterScanner-main\ZaberCognexDiameterScanner-main\DiameterMeasurement_Fixture
python gui.py
```

The GUI has three tabs (Diameter Scan / Calibration Scan / Calibration Verify)
plus a Settings dialog (Edit menu) for runtime hardware overrides. The script
`os.chdir`s to its own folder, so all relative output paths (`../data`,
`../plots`, `../calibration`) resolve correctly regardless of where it's
launched from.

Individual modules can also be run directly from the same folder for CLI use
(`python CalibrationScan.py`, etc.).

## Hardware

- **Zaber**: serial connection (default COM4, device 1, axis 1). Device
  database expected at `C:/Zaber Devices Database/devices-public.sqlite`
  (loaded in `common.py` if present; otherwise zaber-motion fetches online).
- **Cognex IL38**: telnet (default 192.168.0.150:23, user `admin`, blank
  password). Cells: `B21` for diameter scans (radius reading from rotation
  axis, doubled by `fit_circle()` to report diameter), `F25` for calibration
  (scanner-to-surface distance), `B19` for the calibrated diameter (read
  live as metadata when applying calibration). `GV<cell>` reads the value (no
  trigger needed for stored cells like B19).

### Trigger mode (`common.COGNEX_TRIGGER_MODE`)

- **`online`** (default) — the sensor stays Online (`SO1`) running its job, and
  each measurement is fired with the `SW8` soft event. This is the production
  run state. The job's Acquire trigger must be set to **Manual** or **Network**
  or the sensor rejects `SW8` with a negative status.
- **`offline`** — the original behaviour: the sensor is taken Offline (`SO0`)
  and each measurement is fired with `MT`.

### Job gate (`common.COGNEX_REQUIRE_JOB`)

On by default: a scan will not start unless `GF` reports a loaded job, since a
jobless sensor returns stale or empty cells and the scan would silently produce
garbage. When a recipe is active, the loaded job must also match the recipe's
`cognex_job` (compared on the extension-stripped stem). Both failures raise
before any motion, and the GUI shows them as an error dialog.

`CognexConnection.prepare_for_scan()` applies the trigger mode and runs the job
gate; all three scan tabs call it right after connecting. Both are switchable at
runtime in Edit → Settings… → Cognex IL38 → Triggering.

### Diagnosing trigger failures

`COGNEX_TRIGGER_TIMEOUT_S` (default 5.0) is how long a trigger waits for its
acknowledgment. A non-`1` status does not fail the trigger straight away — the
read window stays open in case the real ack follows a stale buffered line — and
the resulting error names the status: `0` means the sensor did not recognise the
command (wrong `COGNEX_ONLINE_TRIGGER` for this firmware), `-1` means it refused
it (usually the job's Acquire trigger is not Manual/Network).

`COGNEX_LOG_RAW` (default on) echoes every raw line the sensor sends during
trigger and cell reads, since the Native Mode reply that matters is often a bare
status code the parsers skip. `trigger_and_read()` prints the real error on each
attempt and chains the last one into its final exception.

`cognex_trigger_probe.py` fires each candidate trigger at the sensor and reports
the raw reply plus whether the measurement cell actually changed — a command can
answer `1` without acquiring anything. It touches only the Cognex (no stage
motion, no job load/save):

```powershell
python cognex_trigger_probe.py --cell B21
```

Status-code meanings beyond `0` (unrecognised command) vary by firmware, so the
code does not try to interpret them — it reports the raw code and defers to the
probe.

**`SO1` returning a negative status:** Cognex documents that Set Online cannot
bring the sensor Online if it was set Offline *manually in In-Sight Explorer* or
*by a Discrete Input*. Native Mode cannot clear that latch — only the same route
that set it can. If `SO1` is refused, set the sensor Online in In-Sight Explorer.
Online trigger mode depends on the sensor being Online; `offline` mode does not.

A refused `SO1` is not treated as fatal on its own, because it also happens when
the sensor is *already* Online. `GO` (Get Online) is the authority: it answers
`1` Online and `0` Offline, and `prepare_for_scan()` fails the run only when `GO`
actually contradicts the mode.

**`SW8` returning `-2` while Online:** soft event 8 is only accepted when the
job's acquisition trigger source is a *software* trigger:

| Editor | Setting | Works with `SW8` |
| --- | --- | --- |
| In-Sight Vision Suite (this rig) | Trigger Source = **Software** | yes |
| In-Sight Vision Suite | Trigger Source = Input Line | no — returns `-2` |
| In-Sight Explorer | Trigger = Manual / Network | yes |
| In-Sight Explorer | Trigger = Continuous / External / Camera | no |

An Input Line source waits on a hardware edge that never arrives on this rig,
which also shows up as an acquisition counter that never advances on its own
while Online. This is a job-side fix in the editor, not a code change.

A neighbouring event such as `SW7` answering `1` does not contradict any of
this — those events are not acquisition triggers, so their success only means
the command family parses. Confirmed on this rig: with Trigger Source =
Software, `SW8` returns `1` and the acquisition counter increments, while `SW7`
returns `1` and it does not.

Settings can be edited live via the GUI (Edit → Settings…). They get pushed
into `common.*` module globals via `SettingsManager.apply_to_modules()` right
before each scan starts.

## Output conventions

All output files are timestamped (`YYYYMMDD_HHMMSS`) so re-runs never overwrite
prior results:

- Diameter scan (raw, 1 rotation): a row appended to the rolling
  `diameter_measurements_<timestamp>.csv` and `<part_id>_circle_fit_result_<N>.png`
- Diameter scan (with **Apply Calibration** checked, 1 rotation): `<part_id>_combined_<timestamp>.{csv,png}`
  — single combined report with raw + calibrated circle fits side-by-side, the
  per-angle offset curve, and a stats table. Offset applied per measurement is
  `corrected_B21(θ) = raw_B21(θ) + (F25_cal(θ) − mean(F25_cal))`. B19 is read
  live and recorded as metadata.
- Diameter scan (Rotations > 1): `<part_id>_multirotation_<timestamp>.{csv,png}`
  — each rotation is treated as an independent dataset. The flat scan is split
  into N chunks of `360/step_deg` measurements, theta normalized to [0, 360),
  and each rotation gets its own circle fit. The report shows all rotations
  overlaid (cartesian + polar), per-angle deviation from the cross-rotation
  mean, and a stats table including diameter range / stdev across rotations.
  When **Apply Calibration** is also checked, calibration is applied to the
  full flat list before splitting, so the comparison is between calibrated
  rotations.
- Calibration scan: `calibration_<cal_id>_<timestamp>.csv` (header carries `StepSize`)
- Calibration verify: `verify_report_<cal_id>_<N>runs_<timestamp>.{png,csv}`

The verify report consolidates N runs (set by the **Number of Runs** field in
the GUI) into one PNG (overlay plot, error plot, combined histogram, summary
table) plus one row-per-measurement CSV.

### Traceability IDs

The Diameter Scan tab carries three optional per-run fields beside Part ID —
**Kanban ID**, **Cycle**, **Cavity** — modelled by `DiameterScan.RunIds` and
passed to every diameter writer as `run_ids=`. They are **report-only**: Part ID
alone still drives every filename, which is why `RunIds` deliberately does not
hold it (a report's table can then never disagree with its own filename).

Blank means "not recorded" and is *omitted* rather than rendered as an empty or
`None` row — `RunIds.labeled()` is the single place that rule lives, so it
cannot drift between formats. The one exception is the rolling summary CSV,
where a fixed-width table forces blanks to be written as empty cells.

They are per-*run*, not per-product, so `apply_recipe()` must never touch them —
a recipe selection clearing them mid-shift would be a data-integrity bug.

### Statistics

All three diameter reports show RMS residual, standard deviation, and R².

- **Std dev** is σ of the per-point *diameter* about the fitted circle, exposed
  as `CircleFitResult.std_dev` (a property, so a hand-built result can never
  report a stale 0.0). It equals exactly `2 × residual_rms`. σ of the residuals
  themselves is mathematically identical to the RMS residual, so it is
  deliberately *not* reported as a separate figure.
- The multi-rotation report distinguishes two different σ: the per-rotation
  **`Std Dev`** column (within-rotation form error) and the aggregate
  **`Stdev rot-to-rot`** row (cross-rotation repeatability). Neither label may
  be shortened to a bare "Stdev".
- **R²** scores the circle model against the measured radius-vs-angle profile
  (`_circle_fit_r_squared()`). The previous formula compared the residuals
  against themselves — numerator and denominator were the same sum — so it
  returned exactly 0.0 on every scan, which is why every archived report reads
  `0.00000000`. Negative values are meaningful (a fit worse than the flat mean)
  and are deliberately not clamped. Zero-variance input (every reading
  identical, which real 4-point bring-up scans produce) returns 1.0, not NaN.

Adding a column to `SUMMARY_CSV_HEADER` is safe: `save_csv` reuses a rolling
file only when its header matches exactly, so a schema change rolls a new file
instead of corrupting an old one. The header list and the `writerow` beneath it
must stay in lockstep — nothing validates that.

## Offline report development

`sample_report.py` generates every report type from synthetic data so report
formatting can be edited with the Zaber stage and the Cognex sensor
disconnected. It builds fake `MeasurementPoint` lists and calls the *real*
writers (`save_csv`/`save_plot`, `save_combined_report`,
`save_multi_rotation_report`, `save_calibration`, `save_multi_run_report`,
`save_comparison_plot`), so what it produces is what a live scan produces —
same functions, same filenames, same columns.

```powershell
python sample_report.py                          # all six report types
python sample_report.py --report combined --open # one report, open the PNG
python sample_report.py --outcome fail           # force a FAIL verdict
python sample_report.py --no-recipe              # no-recipe report layout
python sample_report.py --kanban-id "" --cycle "" --cavity ""   # blank-ID layout
```

`--kanban-id` / `--cycle` / `--cavity` default to non-blank values so a bare run
exercises the traceability rows; passing `""` is the explicit blank-path test.

Output goes to `../sample_reports` (git-ignored, overridable with `--outdir`),
never to the production `../data`, `../plots` or `../calibration` folders — the
writers read their destinations from module globals, which
`redirect_outputs()` rebinds.

Report kinds: `raw`, `combined`, `multirotation`, `calibration`, `verify`
(the GUI's multi-run report) and `verify_single`, the single-run
`verify_<cal_id>_<timestamp>.png` from `CalibrationVerify.save_comparison_plot`.
That last writer is **dead on the hardware paths** — `gui.py` imports it but
never calls it, and `CalibrationVerify.main()` goes straight to the multi-run
report, so the `verify_*.png` files in `../calibration/plots` are historical.
The sample generator is its only remaining caller.
`--cal-step` is an int because the GUI parses that field with `int()`
and `load_calibration()` reads it back with `int()` — a `StepSize,1.0` header
is unloadable.

The synthetic part has eccentricity, n-lobe out-of-roundness and sensor noise,
and the fixture adds runout that the fake F25 calibration captures:
`raw(θ) = true_radius(θ) − runout(θ) + noise`. Because that is the inverse of
what `apply_calibration()` does, calibrated fits come out tighter than raw ones
the way they do on the rig. `--seed` makes a run reproducible; `--nominal`,
`--noise`, `--step`, `--rotations` and `--runs` size the data set. Recipes are
built in memory and never written to `../recipes`.

## Dependencies

`zaber-motion`, `telnetlib3`, `numpy`, `scipy`, `matplotlib`, `Pillow`. A
local `.venv` is checked in at the repo root.

## Conventions

- Don't break the existing CLI `main()` in each module — both the GUI and
  direct `python <module>.py` invocation are supported.
- Hardware globals live in `common.py`; per-script globals (cell addresses,
  output dirs) live in their own module. The GUI's `SettingsManager` is the
  single place that mutates these for a run.
- Scans accept an optional `stop_event` (threading.Event) so the GUI Stop
  button can interrupt cleanly between positions.
- Use `MeasurementPoint(theta_deg, value, timestamp, attempts)` from
  `common.py` as the standard data point.
