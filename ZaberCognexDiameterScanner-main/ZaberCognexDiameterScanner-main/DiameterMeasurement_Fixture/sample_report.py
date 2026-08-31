from __future__ import annotations

"""
Offline sample-report generator.

Synthesizes plausible measurement data and feeds it through the *real* report
writers in DiameterScan / CalibrationScan / CalibrationVerify, so report
formatting can be edited and previewed with the Zaber stage and the Cognex
sensor disconnected. No hardware, no telnet, no serial.

    python sample_report.py                    # every report type
    python sample_report.py --report combined  # just one
    python sample_report.py --outcome fail     # force a FAIL verdict
    python sample_report.py --open             # open the PNGs when done

Output goes to ../sample_reports by default (override with --outdir) so the
production ../data, ../plots and ../calibration folders are never touched.

Synthetic model
---------------
A part has a true radius profile: nominal/2 plus eccentricity (1st harmonic)
plus out-of-round lobing (n-th harmonic). The chuck adds runout, which is what
a calibration scan captures in F25. The raw B21 reading is therefore

    raw(theta) = true_radius(theta) - runout(theta) + noise

so that DiameterScan.apply_calibration(), which adds back
(F25(theta) - mean(F25)), recovers the true profile. Calibrated fits come out
visibly tighter than raw ones, the same way they do on the rig.
"""

import argparse
import math
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Match gui.py: all report paths are relative to this folder.
os.chdir(Path(__file__).resolve().parent)

import CalibrationScan
import CalibrationVerify
import DiameterScan
from CalibrationScan import save_calibration
from CalibrationVerify import (
    compare, print_comparison, save_comparison_plot, save_multi_run_report,
)
from DiameterScan import (
    RunIds, apply_calibration, fit_circle, print_results, save_combined_report,
    save_csv, save_multi_rotation_report, save_plot, split_into_rotations,
)
from common import MeasurementPoint
from recipe import Recipe, evaluate_recipe

SAMPLE_DIR = Path("../sample_reports")

REPORT_KINDS = ("raw", "combined", "multirotation", "calibration", "verify",
                "verify_single")


# ---------------------------------------------------------------------------
# Synthetic part / fixture model
# ---------------------------------------------------------------------------

@dataclass
class PartModel:
    """Geometry knobs for the fake part and fixture, all in inches."""
    nominal_diameter: float = 5.000
    # Off-centre mount: 1st-harmonic radius swing.
    eccentricity: float = 0.0040
    eccentricity_phase_deg: float = 35.0
    # Out-of-roundness: n lobes around the part.
    lobe_count: int = 3
    lobe_amplitude: float = 0.0015
    lobe_phase_deg: float = 110.0
    # Chuck runout the calibration is meant to remove.
    runout_amplitude: float = 0.0060
    runout_phase_deg: float = 200.0
    runout_2nd_harmonic: float = 0.0012
    # Sensor noise, one sigma.
    noise: float = 0.0004
    # Scanner-to-surface standoff that F25 reads around.
    f25_standoff: float = 1.2500
    # Slow drift added per extra rotation (thermal / seating).
    drift_per_rotation: float = 0.0003


def _true_radius(model: PartModel, theta_deg: float) -> float:
    """Radius of the (perfectly mounted) part at this angle."""
    t = math.radians(theta_deg)
    return (
        model.nominal_diameter / 2.0
        + model.eccentricity * math.cos(t - math.radians(model.eccentricity_phase_deg))
        + model.lobe_amplitude * math.cos(
            model.lobe_count * t + math.radians(model.lobe_phase_deg))
    )


def _runout(model: PartModel, theta_deg: float) -> float:
    """Fixture runout at this angle, zero-mean by construction."""
    t = math.radians(theta_deg)
    return (
        model.runout_amplitude * math.cos(t - math.radians(model.runout_phase_deg))
        + model.runout_2nd_harmonic * math.cos(2 * t)
    )


def make_diameter_points(step_deg: float = 5.0, num_rotations: int = 1,
                         model: PartModel | None = None,
                         seed: int = 1234) -> list[MeasurementPoint]:
    """Fake B21 radius readings, one per index position, theta continuing
    past 360 across rotations exactly like sequencer() produces them."""
    model = model or PartModel()
    rng = random.Random(seed)
    per_rot = int(round(360.0 / step_deg))
    t0 = time.time()
    points = []
    for r in range(num_rotations):
        drift = r * model.drift_per_rotation
        for i in range(per_rot):
            theta = r * 360.0 + i * step_deg
            value = (
                _true_radius(model, theta)
                - _runout(model, theta)
                + drift
                + rng.gauss(0.0, model.noise)
            )
            points.append(MeasurementPoint(theta, value,
                                           t0 + len(points) * 0.35, 1))
    return points


def make_calibration_points(step_deg: float = 1.0,
                            model: PartModel | None = None,
                            seed: int = 4321) -> list[MeasurementPoint]:
    """Fake F25 standoff readings for a calibration surface map (0-359)."""
    model = model or PartModel()
    rng = random.Random(seed)
    t0 = time.time()
    points = []
    n = int(round(360.0 / step_deg))
    for i in range(n):
        theta = i * step_deg
        value = (model.f25_standoff + _runout(model, theta)
                 + rng.gauss(0.0, model.noise * 0.5))
        points.append(MeasurementPoint(theta, value, t0 + i * 0.35, 1))
    return points


def make_calibration_data(step_deg: float = 1.0, model: PartModel | None = None,
                          seed: int = 4321) -> list[tuple[float, float]]:
    """The (degree, value) form that apply_calibration()/compare() expect."""
    return [(m.theta_deg, m.value)
            for m in make_calibration_points(step_deg, model, seed)]


def make_recipe(model: PartModel, outcome: str = "pass",
                step_deg: float = 5.0, num_rotations: int = 1,
                cal_file_name: str | None = None) -> Recipe:
    """An in-memory Recipe (never written to ../recipes) whose limits are set
    so the synthetic part lands on the requested verdict."""
    tight = outcome == "fail"
    return Recipe(
        name="SAMPLE-GASKET" + ("-TIGHT" if tight else ""),
        cognex_job="sample_job.job",
        diameter_cell="B21",
        step_deg=int(step_deg),
        num_rotations=num_rotations,
        calibration_file=cal_file_name,
        # A failing recipe just moves the nominal off the part.
        nominal_diameter=model.nominal_diameter + (0.020 if tight else 0.0),
        tol_plus=0.0100,
        tol_minus=0.0100,
        rms_limit=0.0010 if tight else 0.0060,
        repeatability_limit=0.0002 if tight else 0.0050,
    )


# ---------------------------------------------------------------------------
# Output redirection
# ---------------------------------------------------------------------------

def redirect_outputs(outdir: Path) -> None:
    """Point every report writer at `outdir` instead of the production folders.

    The writers read their destinations from module-level globals at call time,
    so rebinding those globals is enough.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    DiameterScan.DATA_DIR = outdir
    DiameterScan.PLOTS_DIR = outdir
    CalibrationScan.CALIBRATION_DIR = outdir
    # CalibrationVerify imported CALIBRATION_DIR by name, so it needs its own.
    CalibrationVerify.CALIBRATION_DIR = outdir
    CalibrationVerify.VERIFY_PLOTS_DIR = outdir


# ---------------------------------------------------------------------------
# Report generators -- each returns the artifacts it wrote
# ---------------------------------------------------------------------------

def gen_raw(part_id, model, step_deg, seed, recipe, run_ids=None) -> list[Path]:
    """Single-rotation, uncalibrated: rolling summary CSV + circle-fit PNG."""
    meas = make_diameter_points(step_deg, 1, model, seed)
    fit = fit_circle(meas)
    verdict = evaluate_recipe(recipe, fit) if recipe else None
    if verdict:
        print("\n[Evaluation] " + verdict.summary())
    save_csv(part_id, meas, fit, recipe=recipe, verdict=verdict, run_ids=run_ids)
    print_results(part_id, meas, fit)
    save_plot(meas, fit, part_id, run_ids=run_ids)
    pngs = sorted(DiameterScan.PLOTS_DIR.glob(f"{part_id}_circle_fit_result_*.png"))
    return [pngs[-1]] if pngs else []


def gen_combined(part_id, model, step_deg, seed, recipe, run_ids=None) -> list[Path]:
    """Single rotation with calibration applied: the combined raw/calibrated report."""
    meas = make_diameter_points(step_deg, 1, model, seed)
    cal_data = make_calibration_data(1.0, model, seed + 1)
    cal_meas, f25_nominal = apply_calibration(meas, cal_data)
    raw_fit = fit_circle(meas)
    cal_fit = fit_circle(cal_meas)
    print_results(f"{part_id} (raw)", meas, raw_fit)
    print_results(f"{part_id} (calibrated)", cal_meas, cal_fit)
    verdict = evaluate_recipe(recipe, cal_fit) if recipe else None
    if verdict:
        print("\n[Evaluation] " + verdict.summary())
    # B19 is the sensor's own calibrated diameter; fake it near the fitted one.
    b19 = cal_fit.diameter + 0.0007
    csv_path, png_path = save_combined_report(
        part_id, meas, raw_fit, cal_meas, cal_fit,
        b19, f25_nominal, cal_data, "calibration_SAMPLE_offline.csv",
        recipe=recipe, verdict=verdict, run_ids=run_ids,
    )
    return [Path(csv_path), Path(png_path)]


def gen_multirotation(part_id, model, step_deg, seed, recipe, run_ids=None,
                      num_rotations=3, calibrated=True) -> list[Path]:
    """N rotations of the same part, optionally calibrated before splitting."""
    meas = make_diameter_points(step_deg, num_rotations, model, seed)
    cal_file_name = None
    b19 = f25_nominal = None
    if calibrated:
        cal_data = make_calibration_data(1.0, model, seed + 1)
        meas, f25_nominal = apply_calibration(meas, cal_data)
        cal_file_name = "calibration_SAMPLE_offline.csv"

    rotations = split_into_rotations(meas, step_deg, num_rotations)
    fits = [fit_circle(rot) for rot in rotations]
    for i, (rot, fit) in enumerate(zip(rotations, fits), start=1):
        print_results(f"{part_id} - rotation {i}", rot, fit)
    if calibrated:
        b19 = sum(f.diameter for f in fits) / len(fits) + 0.0007

    verdict = None
    if recipe:
        # gui.py evaluates the cross-rotation mean, with repeatability from the
        # per-rotation fits -- mirror that so the report metadata matches.
        agg = DiameterScan.CircleFitResult(
            center_x=sum(f.center_x for f in fits) / len(fits),
            center_y=sum(f.center_y for f in fits) / len(fits),
            diameter=sum(f.diameter for f in fits) / len(fits),
            residual_rms=sum(f.residual_rms for f in fits) / len(fits),
            max_residual=max(f.max_residual for f in fits),
            r_squared=sum(f.r_squared for f in fits) / len(fits),
        )
        verdict = evaluate_recipe(recipe, agg, rotation_fits=fits)
        print("\n[Evaluation] " + verdict.summary())

    csv_path, png_path = save_multi_rotation_report(
        part_id, rotations, fits,
        b19=b19, f25_nominal=f25_nominal, cal_file_name=cal_file_name,
        recipe=recipe, verdict=verdict, run_ids=run_ids,
    )
    return [Path(csv_path), Path(png_path)]


def gen_calibration(cal_id, model, seed, step_deg=1) -> list[Path]:
    """A calibration surface-map CSV, loadable by the Verify tab."""
    points = make_calibration_points(step_deg, model, seed)
    # The GUI parses this field with int() (gui.py:980), so real files carry
    # 'StepSize,1'. load_calibration() also int()s it back, and would choke on
    # '1.0' -- so write the int form the rig writes.
    return [Path(save_calibration(points, cal_id, int(step_deg)))]


def _verify_runs(model, seed, num_runs, step_deg):
    """Compare N synthetic re-measurements against the reference calibration.

    Each run re-measures the same surface: same runout, fresh noise, plus a
    small per-run seating shift so the overlay plot has something to show.
    """
    cal_data = make_calibration_data(step_deg, model, seed)
    all_run_results = []
    for run in range(num_runs):
        run_model = PartModel(**{**model.__dict__,
                                 "f25_standoff": model.f25_standoff + run * 0.0004})
        run_points = make_calibration_points(step_deg, run_model, seed + 100 + run)
        results = compare(cal_data, run_points)
        print(f"\n--- Run {run + 1} of {num_runs} ---")
        print_comparison(results)
        all_run_results.append(results)
    return all_run_results


def gen_verify(cal_id, model, seed, num_runs=3, step_deg=1) -> list[Path]:
    """A multi-run calibration verify report (PNG + row-per-measurement CSV).

    This is what the GUI's Calibration Verify tab writes.
    """
    all_run_results = _verify_runs(model, seed, num_runs, step_deg)
    plot_path, csv_path = save_multi_run_report(all_run_results, cal_id)
    return [Path(plot_path), Path(csv_path)]


def gen_verify_single(cal_id, model, seed, step_deg=1) -> list[Path]:
    """The single-run `verify_<cal_id>_<timestamp>.png` comparison plot.

    save_comparison_plot() is dead on the hardware paths: gui.py imports it but
    never calls it, and CalibrationVerify.main() goes straight to the multi-run
    report. This generator is its only remaining caller, kept so the format can
    still be previewed.
    """
    results = _verify_runs(model, seed, 1, step_deg)[0]
    save_comparison_plot(results, cal_id)   # returns None; it prints the path
    pngs = sorted(CalibrationVerify.VERIFY_PLOTS_DIR.glob(f"verify_{cal_id}_*.png"))
    return [pngs[-1]] if pngs else []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _open_files(paths) -> None:
    for p in paths:
        if p.suffix.lower() != ".png":
            continue
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # noqa: S606 - user asked to open it
            elif sys.platform == "darwin":
                subprocess.run(["open", str(p)], check=False)
            else:
                subprocess.run(["xdg-open", str(p)], check=False)
        except OSError as e:
            print(f"[Open] Could not open {p}: {e}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate sample reports from synthetic data (no hardware).")
    p.add_argument("--report", choices=("all",) + REPORT_KINDS, default="all",
                   help="Which report to generate (default: all).")
    p.add_argument("--part-id", default="SAMPLE-001")
    # Non-blank defaults on purpose: a bare `python sample_report.py` should
    # exercise the new traceability rows, and passing "" is the explicit
    # blank-path test.
    p.add_argument("--kanban-id", default="KB-0093",
                   help="Kanban ID stamped into the reports (pass '' to omit).")
    p.add_argument("--cycle", default="7",
                   help="Cycle stamped into the reports (pass '' to omit).")
    p.add_argument("--cavity", default="3",
                   help="Cavity stamped into the reports (pass '' to omit).")
    p.add_argument("--cal-id", default="SAMPLE",
                   help="Calibration ID used in calibration/verify filenames.")
    p.add_argument("--step", type=float, default=5.0,
                   help="Diameter scan step size in degrees (default: 5).")
    p.add_argument("--cal-step", type=int, default=1,
                   help="Calibration step size in degrees, integer like the GUI "
                        "(default: 1).")
    p.add_argument("--rotations", type=int, default=3,
                   help="Rotations for the multi-rotation report (default: 3).")
    p.add_argument("--runs", type=int, default=3,
                   help="Runs in the verify report (default: 3).")
    p.add_argument("--nominal", type=float, default=5.000,
                   help="Nominal part diameter in inches (default: 5.000).")
    p.add_argument("--noise", type=float, default=0.0004,
                   help="Sensor noise sigma in inches (default: 0.0004).")
    p.add_argument("--seed", type=int, default=1234,
                   help="RNG seed; same seed gives byte-identical data.")
    p.add_argument("--outcome", choices=("pass", "fail"), default="pass",
                   help="Force the recipe verdict (default: pass).")
    p.add_argument("--no-recipe", action="store_true",
                   help="Omit the recipe, exercising the no-recipe report layout.")
    p.add_argument("--outdir", type=Path, default=SAMPLE_DIR,
                   help=f"Output folder (default: {SAMPLE_DIR}).")
    p.add_argument("--open", action="store_true",
                   help="Open the generated PNGs when finished.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    outdir = args.outdir
    redirect_outputs(outdir)

    model = PartModel(nominal_diameter=args.nominal, noise=args.noise)
    recipe = None if args.no_recipe else make_recipe(
        model, args.outcome, args.step, args.rotations,
        cal_file_name=f"calibration_{args.cal_id}_offline.csv")
    run_ids = RunIds.of(args.kanban_id, args.cycle, args.cavity)

    kinds = REPORT_KINDS if args.report == "all" else (args.report,)
    written: list[Path] = []

    for kind in kinds:
        print("\n" + "=" * 70)
        print(f"SAMPLE REPORT: {kind}")
        print("=" * 70)
        if kind == "raw":
            written += gen_raw(args.part_id, model, args.step, args.seed, recipe,
                               run_ids)
        elif kind == "combined":
            written += gen_combined(args.part_id, model, args.step, args.seed,
                                    recipe, run_ids)
        elif kind == "multirotation":
            written += gen_multirotation(args.part_id, model, args.step, args.seed,
                                         recipe, run_ids,
                                         num_rotations=args.rotations)
        elif kind == "calibration":
            written += gen_calibration(args.cal_id, model, args.seed + 1, args.cal_step)
        elif kind == "verify":
            written += gen_verify(args.cal_id, model, args.seed + 1,
                                  num_runs=args.runs, step_deg=args.cal_step)
        elif kind == "verify_single":
            written += gen_verify_single(args.cal_id, model, args.seed + 1,
                                         step_deg=args.cal_step)

    print("\n" + "=" * 70)
    print(f"Wrote {len(written)} file(s) to {outdir.resolve()}")
    for path in written:
        print(f"  {path}")
    print("=" * 70 + "\n")

    if args.open:
        _open_files(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
