from __future__ import annotations

"""
Verify measurements against a calibration reference file.
Runs the same scan as CalibrationScan.py, then compares each position
against the stored calibration values.
"""

import time
import csv
import asyncio
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime
from zaber_motion import Units

from common import (
    telnetlib3, CognexConnection, MeasurementPoint,
    SPEED_DEG_S, ACCEL_DEG_S2, DWELL_S,
    open_zaber_connection, setup_zaber_axis,
)
from CalibrationScan import (
    CALIBRATION_CELL, CALIBRATION_STEP_DEG, CALIBRATION_DIR,
    calibration_scan, load_calibration,
)

VERIFY_PLOTS_DIR = Path("../calibration/plots")


def find_calibration_files():
    """List available calibration files."""
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(CALIBRATION_DIR.glob("calibration_*.csv"))
    return files


def compare(cal_data, verify_measurements):
    """Compare verification measurements against calibration reference.

    Matches by nearest degree. Returns list of dicts with comparison data.
    """
    cal_dict = {}
    for deg, val in cal_data:
        rounded = round(deg % 360, 1)
        cal_dict[rounded] = val

    results = []
    for m in verify_measurements:
        rounded = round(m.theta_deg % 360, 1)
        cal_val = cal_dict.get(rounded)
        if cal_val is not None:
            diff = m.value - cal_val
            results.append({
                'degree': m.theta_deg,
                'measured': m.value,
                'calibration': cal_val,
                'difference': diff,
            })
        else:
            # Find nearest calibration point
            cal_degs = np.array(list(cal_dict.keys()))
            nearest_deg = cal_degs[np.argmin(np.abs(cal_degs - rounded))]
            cal_val = cal_dict[nearest_deg]
            diff = m.value - cal_val
            results.append({
                'degree': m.theta_deg,
                'measured': m.value,
                'calibration': cal_val,
                'difference': diff,
            })

    return results


def print_comparison(results):
    """Print comparison summary."""
    diffs = [r['difference'] for r in results]
    abs_diffs = [abs(d) for d in diffs]

    print("\n" + "=" * 70)
    print("VERIFICATION RESULTS")
    print("=" * 70)
    print(f"  Points compared:     {len(results)}")
    print(f"  Mean difference:     {np.mean(diffs):.6f}")
    print(f"  Std deviation:       {np.std(diffs):.6f}")
    print(f"  Max difference:      {max(diffs):.6f}")
    print(f"  Min difference:      {min(diffs):.6f}")
    print(f"  Mean abs difference: {np.mean(abs_diffs):.6f}")
    print(f"  Max abs difference:  {max(abs_diffs):.6f}")
    print("=" * 70)

    # Flag any outliers (> 3 sigma)
    std = np.std(diffs)
    mean = np.mean(diffs)
    outliers = [r for r in results if abs(r['difference'] - mean) > 3 * std]
    if outliers:
        print(f"\n  OUTLIERS (>{3*std:.6f} from mean):")
        for o in outliers:
            print(f"    {o['degree']:.1f} deg: measured={o['measured']:.6f}  "
                  f"cal={o['calibration']:.6f}  diff={o['difference']:.6f}")
    else:
        print(f"\n  No outliers detected (3-sigma threshold: {3*std:.6f})")


def save_comparison_plot(results, cal_id):
    """Save a comparison plot showing calibration vs measured and the differences."""
    VERIFY_PLOTS_DIR.mkdir(parents=True, exist_ok=True)

    degs = [r['degree'] for r in results]
    measured = [r['measured'] for r in results]
    calibration = [r['calibration'] for r in results]
    diffs = [r['difference'] for r in results]

    fig, axes = plt.subplots(3, 1, figsize=(14, 12))

    # Plot 1: Overlay of calibration and measured values
    ax1 = axes[0]
    ax1.plot(degs, calibration, 'b-', linewidth=1, alpha=0.7, label='Calibration')
    ax1.plot(degs, measured, 'r-', linewidth=1, alpha=0.7, label='Measured')
    ax1.set_xlabel('Degree')
    ax1.set_ylabel('Value')
    ax1.set_title(f'Calibration vs Measured - {cal_id}', fontsize=13, fontweight='bold')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Plot 2: Difference (error) across degrees
    ax2 = axes[1]
    ax2.plot(degs, diffs, 'g-', linewidth=1)
    ax2.axhline(y=0, color='black', linestyle='--', linewidth=0.5)
    ax2.axhline(y=np.mean(diffs), color='red', linestyle='--', linewidth=0.5, label=f'Mean: {np.mean(diffs):.6f}')
    ax2.fill_between(degs,
                     np.mean(diffs) - np.std(diffs),
                     np.mean(diffs) + np.std(diffs),
                     alpha=0.2, color='red', label=f'1-sigma: {np.std(diffs):.6f}')
    ax2.set_xlabel('Degree')
    ax2.set_ylabel('Difference (Measured - Cal)')
    ax2.set_title('Error by Position', fontsize=13, fontweight='bold')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    # Plot 3: Histogram of differences
    ax3 = axes[2]
    ax3.hist(diffs, bins=50, edgecolor='black', alpha=0.7)
    ax3.axvline(x=np.mean(diffs), color='red', linestyle='--', linewidth=1.5, label=f'Mean: {np.mean(diffs):.6f}')
    ax3.set_xlabel('Difference')
    ax3.set_ylabel('Count')
    ax3.set_title('Error Distribution', fontsize=13, fontweight='bold')
    ax3.legend()
    ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    filename = VERIFY_PLOTS_DIR / f"verify_{cal_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    plt.savefig(filename, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"[Plot] Saved: {filename}")


def save_multi_run_report(all_run_results, cal_id):
    """Save a consolidated report (PNG plot + CSV) for one or more verification runs.

    `all_run_results` is a list where each element is a `results` list returned
    by `compare()` for one run. Returns (plot_path, csv_path).
    """
    VERIFY_PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    num_runs = len(all_run_results)

    csv_path = VERIFY_PLOTS_DIR / f"verify_report_{cal_id}_{num_runs}runs_{timestamp}.csv"
    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Run', 'Degree', 'Measured', 'Calibration', 'Difference'])
        for run_idx, results in enumerate(all_run_results, start=1):
            for r in results:
                writer.writerow([
                    run_idx,
                    f"{r['degree']:.3f}",
                    f"{r['measured']:.6f}",
                    f"{r['calibration']:.6f}",
                    f"{r['difference']:.6f}",
                ])
    print(f"[Report CSV] Saved: {csv_path}")

    fig, axes = plt.subplots(4, 1, figsize=(14, 16))
    colors = plt.cm.viridis(np.linspace(0, 0.85, max(num_runs, 2)))

    ax1 = axes[0]
    ref_results = all_run_results[0]
    ref_degs = [r['degree'] for r in ref_results]
    ref_cal = [r['calibration'] for r in ref_results]
    ax1.plot(ref_degs, ref_cal, 'b-', linewidth=1.8, alpha=0.9, label='Calibration')
    for i, results in enumerate(all_run_results):
        degs = [r['degree'] for r in results]
        measured = [r['measured'] for r in results]
        ax1.plot(degs, measured, color=colors[i], linewidth=0.9, alpha=0.75,
                 label=f'Run {i + 1}')
    ax1.set_xlabel('Degree')
    ax1.set_ylabel('Value')
    ax1.set_title(f'Calibration vs Measured ({num_runs} run{"s" if num_runs != 1 else ""}) - {cal_id}',
                  fontsize=13, fontweight='bold')
    ax1.legend(loc='best', fontsize=8, ncol=2)
    ax1.grid(True, alpha=0.3)

    ax2 = axes[1]
    ax2.axhline(y=0, color='black', linestyle='--', linewidth=0.5)
    for i, results in enumerate(all_run_results):
        degs = [r['degree'] for r in results]
        diffs = [r['difference'] for r in results]
        ax2.plot(degs, diffs, color=colors[i], linewidth=0.9, alpha=0.75,
                 label=f'Run {i + 1}')
    ax2.set_xlabel('Degree')
    ax2.set_ylabel('Difference (Measured - Cal)')
    ax2.set_title('Error by Position', fontsize=13, fontweight='bold')
    ax2.legend(loc='best', fontsize=8, ncol=2)
    ax2.grid(True, alpha=0.3)

    ax3 = axes[2]
    all_diffs = [r['difference'] for results in all_run_results for r in results]
    ax3.hist(all_diffs, bins=50, edgecolor='black', alpha=0.7)
    ax3.axvline(x=np.mean(all_diffs), color='red', linestyle='--', linewidth=1.5,
                label=f'Mean: {np.mean(all_diffs):.6f}')
    ax3.set_xlabel('Difference')
    ax3.set_ylabel('Count')
    ax3.set_title('Combined Error Distribution', fontsize=13, fontweight='bold')
    ax3.legend()
    ax3.grid(True, alpha=0.3)

    ax4 = axes[3]
    ax4.axis('off')
    table_header = ['Run', 'N', 'Mean', 'Std', 'Min', 'Max', 'Mean |diff|', 'Max |diff|']
    table_rows = []
    for i, results in enumerate(all_run_results, start=1):
        diffs = [r['difference'] for r in results]
        abs_diffs = [abs(d) for d in diffs]
        table_rows.append([
            str(i), str(len(diffs)),
            f"{np.mean(diffs):.6f}", f"{np.std(diffs):.6f}",
            f"{min(diffs):.6f}", f"{max(diffs):.6f}",
            f"{np.mean(abs_diffs):.6f}", f"{max(abs_diffs):.6f}",
        ])
    all_abs = [abs(d) for d in all_diffs]
    table_rows.append([
        'ALL', str(len(all_diffs)),
        f"{np.mean(all_diffs):.6f}", f"{np.std(all_diffs):.6f}",
        f"{min(all_diffs):.6f}", f"{max(all_diffs):.6f}",
        f"{np.mean(all_abs):.6f}", f"{max(all_abs):.6f}",
    ])
    tbl = ax4.table(cellText=table_rows, colLabels=table_header,
                    loc='center', cellLoc='center')
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.5)
    for col in range(len(table_header)):
        tbl[(len(table_rows), col)].set_facecolor('#e8f4ff')
        tbl[(len(table_rows), col)].set_text_props(weight='bold')
    ax4.set_title('Summary Statistics', fontsize=13, fontweight='bold', pad=20)

    plt.tight_layout()
    plot_path = VERIFY_PLOTS_DIR / f"verify_report_{cal_id}_{num_runs}runs_{timestamp}.png"
    plt.savefig(plot_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"[Report Plot] Saved: {plot_path}")

    return plot_path, csv_path


def main():
    if not telnetlib3:
        print("ERROR: Install telnetlib3")
        return

    print("\n" + "=" * 60)
    print("CALIBRATION VERIFICATION")
    print("=" * 60)

    # List available calibration files
    cal_files = find_calibration_files()
    if not cal_files:
        print("\nERROR: No calibration files found in", CALIBRATION_DIR)
        print("Run CalibrationScan.py first to create a calibration reference.")
        return

    print("\nAvailable calibration files:")
    for i, f in enumerate(cal_files):
        print(f"  [{i + 1}] {f.name}")

    while True:
        choice = input(f"\nSelect calibration file [1-{len(cal_files)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(cal_files):
            cal_file = cal_files[int(choice) - 1]
            break
        print("Invalid selection.")

    step_deg, cal_data = load_calibration(cal_file)
    print(f"\nLoaded {len(cal_data)} calibration points from {cal_file.name}")
    print(f"Step size from calibration file: {step_deg} deg")

    num_steps = int(360.0 / step_deg)
    runs_input = input("Number of verification runs [default: 1]: ").strip()
    try:
        num_runs = max(1, int(runs_input)) if runs_input else 1
    except ValueError:
        print("Invalid number of runs; defaulting to 1.")
        num_runs = 1

    print(f"\nThis will take {num_steps} measurements x {num_runs} run(s) at {step_deg} deg increments.")
    confirm = input("Proceed? (y/n): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("Cancelled.")
        return

    zaber_conn = open_zaber_connection()

    async def run_verification():
        with zaber_conn:
            axis = setup_zaber_axis(zaber_conn)

            cognex = CognexConnection()
            await cognex.connect()

            try:
                runs = []
                for i in range(num_runs):
                    print(f"\n###### RUN {i + 1} of {num_runs} ######")
                    runs.append(await calibration_scan(axis, cognex, step_deg, SPEED_DEG_S, ACCEL_DEG_S2, DWELL_S))
                return runs
            finally:
                await cognex.disconnect()

    runs = asyncio.run(run_verification())

    cal_id = cal_file.stem.replace("calibration_", "").rsplit("_", 2)[0]
    all_run_results = []
    for i, measurements in enumerate(runs, start=1):
        if not measurements:
            print(f"\nERROR: Run {i} collected no measurements; skipping.")
            continue
        results = compare(cal_data, measurements)
        print(f"\n--- Run {i} of {len(runs)} ---")
        print_comparison(results)
        all_run_results.append(results)

    if not all_run_results:
        print("\nERROR: No usable runs.")
        return

    save_multi_run_report(all_run_results, cal_id)

    print("\n[Complete]\n")


if __name__ == "__main__":
    main()
