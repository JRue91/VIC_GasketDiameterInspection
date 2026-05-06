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
  password). Cells: `B21` for diameter scans, `F25` for calibration. `MT`
  triggers a measurement; `GV<cell>` reads the value.

Settings can be edited live via the GUI (Edit → Settings…). They get pushed
into `common.*` module globals via `SettingsManager.apply_to_modules()` right
before each scan starts.

## Output conventions

All output files are timestamped (`YYYYMMDD_HHMMSS`) so re-runs never overwrite
prior results:

- Diameter scan: `<part_id>_<timestamp>.csv` and `<part_id>_circle_fit_result_<timestamp>.png`
- Calibration scan: `calibration_<cal_id>_<timestamp>.csv` (header carries `StepSize`)
- Calibration verify: `verify_report_<cal_id>_<N>runs_<timestamp>.{png,csv}`

The verify report consolidates N runs (set by the **Number of Runs** field in
the GUI) into one PNG (overlay plot, error plot, combined histogram, summary
table) plus one row-per-measurement CSV.

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
