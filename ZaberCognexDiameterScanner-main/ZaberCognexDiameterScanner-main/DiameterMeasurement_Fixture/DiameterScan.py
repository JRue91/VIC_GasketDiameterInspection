from __future__ import annotations

"""
Zaber rotary indexing with Cognex IL38 measurement.
Sequential: Move -> Dwell -> Trigger -> Read
"""

import time
import asyncio
import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import least_squares
from dataclasses import dataclass
from datetime import datetime
import csv
from pathlib import Path
from zaber_motion import Units

from common import (
    telnetlib3, CognexConnection, MeasurementPoint,
    SPEED_DEG_S, ACCEL_DEG_S2, DWELL_S,
    open_zaber_connection, setup_zaber_axis,
)

# ======= DIAMETER SCAN CONFIG =======
INDEX_STEP_DEG = 5.0
COGNEX_CELL = "B21"

DATA_DIR = Path("../data")
PLOTS_DIR = Path("../plots")
MAX_RECORDS_PER_CSV = 250


@dataclass
class CircleFitResult:
    center_x: float
    center_y: float
    diameter: float
    residual_rms: float
    max_residual: float
    r_squared: float


async def sequencer(axis, conn, step_deg, num_steps, speed, accel, dwell, real_time_plot):
    """BRUTE FORCE SEQUENCER."""
    measurements = []
    base = axis.get_position(Units.ANGLE_DEGREES)
    
    if real_time_plot:
        plt.ion()
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
        ax2 = plt.subplot(122, projection='polar')
    
    print("\n" + "="*70)
    print("SEQUENCER START")
    print("="*70)
    
    # ========== POSITION 0 ==========
    print(f"\n>>> POSITION 0: {base:.3f}°")
    print("  [1] At home")
    print("  [2] Verify idle")
    axis.wait_until_idle()
    print("  [3] TRIGGER + READ")
    val, read_attempts, trigger_attempts = await conn.trigger_and_read(COGNEX_CELL)
    pos = axis.get_position(Units.ANGLE_DEGREES)
    measurements.append(MeasurementPoint(pos, val, time.time(), read_attempts))
    print(f"  [4] RECORDED: {pos:.3f}° = {val:.4f} inches (trigger attempts: {trigger_attempts})")
    
    if real_time_plot:
        _update_plot(measurements, ax1, ax2)
    
    # ========== POSITION 1 ==========
    target_1 = base + step_deg
    print(f"\n>>> POSITION 1: {target_1:.3f}°")
    print(f"  [1] MOVE (BLOCKING)")
    print(f"      is_busy before: {axis.is_busy()}")
    t_move = time.time()
    axis.move_absolute(target_1, Units.ANGLE_DEGREES, wait_until_idle=True,
                      velocity=speed, velocity_unit=Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND,
                      acceleration=accel, acceleration_unit=Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED)
    print(f"  [2] Move done ({time.time()-t_move:.3f}s)")
    print(f"      is_busy after: {axis.is_busy()}")
    print(f"  [3] DWELL {dwell}s")
    time.sleep(dwell)
    print(f"  [4] Dwell done")
    print(f"  [5] Verify idle")
    axis.wait_until_idle()
    print(f"      is_busy: {axis.is_busy()}")
    print(f"  [6] TRIGGER")
    await conn.trigger()
    print(f"  [7] Trigger done")
    
    # ========== POSITIONS 2 TO N-1 ==========
    for i in range(2, num_steps):
        target = base + i * step_deg
        print(f"\n>>> POSITION {i}: {target:.3f}°")
        
        print(f"  [1] MOVE (NON-BLOCKING)")
        print(f"      is_busy before: {axis.is_busy()}")
        t_move = time.time()
        axis.move_absolute(target, Units.ANGLE_DEGREES, wait_until_idle=False,
                          velocity=speed, velocity_unit=Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND,
                          acceleration=accel, acceleration_unit=Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED)
        print(f"      move_absolute returned")
        print(f"      is_busy after command: {axis.is_busy()}")
        
        print(f"  [2] READ previous (while moving)")
        try:
            val, attempts = await conn.read_once(COGNEX_CELL)
        except RuntimeError:
            print(f"      ! Read failed -- will re-trigger after move settles")
            axis.wait_until_idle()
            time.sleep(dwell)
            val, attempts, retrigger_attempts = await conn.trigger_and_read(COGNEX_CELL)
            print(f"      -> Recovered after {retrigger_attempts} trigger attempt(s)")
        prev_pos = base + (i-1) * step_deg
        measurements.append(MeasurementPoint(prev_pos, val, time.time(), attempts))
        print(f"  [3] RECORDED: {prev_pos:.3f}° = {val:.4f} inches")
        print(f"      is_busy after read: {axis.is_busy()}")

        if real_time_plot:
            _update_plot(measurements, ax1, ax2)

        print(f"  [4] WAIT for move")
        axis.wait_until_idle()
        print(f"  [5] Move done ({time.time()-t_move:.3f}s)")
        print(f"      is_busy: {axis.is_busy()}")

        print(f"  [6] DWELL {dwell}s")
        time.sleep(dwell)
        print(f"  [7] Dwell done")

        print(f"  [8] Verify idle")
        axis.wait_until_idle()
        print(f"      is_busy: {axis.is_busy()}")

        print(f"  [9] TRIGGER")
        await conn.trigger()
        print(f"  [10] Trigger done")
    
    # ========== FINAL READ ==========
    print(f"\n>>> FINAL READ")
    print(f"  [1] READ final trigger")
    try:
        val, attempts = await conn.read_once(COGNEX_CELL)
    except RuntimeError:
        print(f"      ! Read failed -- retrying with trigger + read")
        val, attempts, retrigger_attempts = await conn.trigger_and_read(COGNEX_CELL)
        print(f"      -> Recovered after {retrigger_attempts} trigger attempt(s)")
    final_pos = base + (num_steps - 1) * step_deg
    measurements.append(MeasurementPoint(final_pos, val, time.time(), attempts))
    print(f"  [2] RECORDED: {final_pos:.3f}° = {val:.4f} inches")
    
    if real_time_plot:
        _update_plot(measurements, ax1, ax2)
        plt.ioff()
        plt.close(fig)
    
    # ========== RETURN TO START ==========
    print(f"\n>>> RETURN TO START")
    final_target = base + num_steps * step_deg
    print(f"  [1] Moving to {final_target:.3f}°")
    axis.move_absolute(final_target, Units.ANGLE_DEGREES, wait_until_idle=True,
                      velocity=speed, velocity_unit=Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND,
                      acceleration=accel, acceleration_unit=Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED)
    print(f"  [2] At start position")
    
    print("\n" + "="*70)
    print(f"SEQUENCER COMPLETE: {len(measurements)} measurements")
    print("="*70)
    
    return measurements


def _update_plot(measurements, ax1, ax2):
    thetas = np.array([m.theta_deg for m in measurements])
    radii = np.array([m.value for m in measurements])
    theta_rad = np.deg2rad(thetas)
    x = radii * np.cos(theta_rad)
    y = radii * np.sin(theta_rad)
    
    ax1.clear()
    ax1.scatter(x, y, c=thetas, cmap='viridis', s=50, alpha=0.7, edgecolors='black', linewidth=0.5)
    ax1.set_xlabel('X (inches)')
    ax1.set_ylabel('Y (inches)')
    ax1.set_title('Real-Time (Cartesian)')
    ax1.axis('equal')
    ax1.grid(True, alpha=0.3)
    
    ax2.clear()
    ax2.scatter(theta_rad, radii, c=thetas, cmap='viridis', s=50, alpha=0.7, edgecolors='black', linewidth=0.5)
    ax2.set_theta_zero_location('E')
    ax2.set_theta_direction(1)
    
    # Set radial limits from 0 with padding above max
    r_max = radii.max()
    padding = 0.1 * r_max
    ax2.set_ylim(0, r_max + padding)
    
    plt.tight_layout()
    plt.pause(0.01)


def fit_circle(measurements):
    if len(measurements) < 3:
        raise ValueError("Need 3+ measurements")

    thetas = np.array([m.theta_deg for m in measurements])
    radii = np.array([m.value for m in measurements])
    theta_rad = np.deg2rad(thetas)
    x = radii * np.cos(theta_rad)
    y = radii * np.sin(theta_rad)

    def residuals(p):
        return np.sqrt((x - p[0])**2 + (y - p[1])**2)
    
    result = least_squares(lambda p: residuals(p) - residuals(p).mean(), [x.mean(), y.mean()], method='lm')
    xc, yc = result.x
    dists = residuals([xc, yc])
    diameter = 2 * dists.mean()
    
    res = dists - dists.mean()
    rms = np.sqrt(np.mean(res**2))
    max_res = np.max(np.abs(res))
    r2 = 1 - np.sum(res**2) / np.sum((dists - dists.mean())**2) if np.sum((dists - dists.mean())**2) > 0 else 0
    
    return CircleFitResult(xc, yc, diameter, rms, max_res, r2)


def save_plot(measurements, fit_result, part_id):
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    
    existing = list(PLOTS_DIR.glob(f"{part_id}_circle_fit_result_*.png"))
    num = 1
    if existing:
        nums = [int(f.stem.split('_')[-1]) for f in existing if f.stem.split('_')[-1].isdigit()]
        if nums:
            num = max(nums) + 1
    
    filename = PLOTS_DIR / f"{part_id}_circle_fit_result_{num}.png"
    
    thetas = np.array([m.theta_deg for m in measurements])
    radii = np.array([m.value for m in measurements])
    theta_rad = np.deg2rad(thetas)
    x = radii * np.cos(theta_rad)
    y = radii * np.sin(theta_rad)
    
    # Create 2x2 subplot grid
    fig = plt.figure(figsize=(16, 12))
    
    # QUADRANT 1 (Upper Left): Cartesian plot
    ax1 = plt.subplot(2, 2, 1)
    ax1.scatter(x, y, c=thetas, cmap='viridis', s=50, alpha=0.7, edgecolors='black', linewidth=0.5, label='Data')
    
    if fit_result:
        theta = np.linspace(0, 2*np.pi, 100)
        r = fit_result.diameter / 2
        cx = fit_result.center_x + r * np.cos(theta)
        cy = fit_result.center_y + r * np.sin(theta)
        ax1.plot(cx, cy, 'r-', linewidth=2, label='Fit')
        ax1.plot(fit_result.center_x, fit_result.center_y, 'r+', markersize=15, markeredgewidth=2, label='Center')
    
    ax1.set_xlabel('X (inches)', fontsize=11)
    ax1.set_ylabel('Y (inches)', fontsize=11)
    ax1.set_title(f'Circle Fit - Part {part_id}' if fit_result else f'Measurements - Part {part_id}', 
                  fontsize=13, fontweight='bold')
    ax1.axis('equal')
    ax1.grid(True, alpha=0.3)
    ax1.legend()
    
    # QUADRANT 2 (Upper Right): True polar (origin at center)
    ax2 = plt.subplot(2, 2, 2, projection='polar')
    ax2.scatter(theta_rad, radii, c=thetas, cmap='viridis', s=50, alpha=0.7, edgecolors='black', linewidth=0.5)
    ax2.set_theta_zero_location('E')
    ax2.set_theta_direction(1)
    ax2.set_title('True Polar Plot', fontsize=13, fontweight='bold', pad=20)
    
    # Scale from 0 with padding
    r_max = radii.max()
    padding = 0.1 * r_max
    ax2.set_ylim(0, r_max + padding)
    
    # QUADRANT 3 (Lower Left): Results table
    ax3 = plt.subplot(2, 2, 3)
    ax3.axis('off')
    
    if fit_result:
        table_data = [
            ['Measurement', 'Value'],
            ['', ''],
            ['Number of Points', f'{len(measurements)}'],
            ['Diameter', f'{fit_result.diameter:.4f} inches'],
            ['Radius', f'{fit_result.diameter/2:.4f} inches'],
            ['Center (X, Y)', f'({fit_result.center_x:.4f}, {fit_result.center_y:.4f})'],
            ['', ''],
            ['RMS Residual', f'{fit_result.residual_rms:.4f} inches'],
            ['Max Residual', f'{fit_result.max_residual:.4f} inches'],
            ['RMS Error', f'{100*fit_result.residual_rms/(fit_result.diameter/2):.3f}%'],
            ['R² Coefficient', f'{fit_result.r_squared:.6f}'],
        ]
        
        table = ax3.table(cellText=table_data, cellLoc='left', loc='center',
                         colWidths=[0.5, 0.5])
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 2)
        
        # Style header row
        for i in range(2):
            table[(0, i)].set_facecolor('#4CAF50')
            table[(0, i)].set_text_props(weight='bold', color='white')
        
        # Style alternating rows
        for i in range(2, len(table_data)):
            for j in range(2):
                if i % 2 == 0:
                    table[(i, j)].set_facecolor('#f0f0f0')
        
        ax3.set_title(f'Results - Part {part_id}', fontsize=13, fontweight='bold', pad=20)
    
    # QUADRANT 4 (Lower Right): Offset polar (centered on data)
    ax4 = plt.subplot(2, 2, 4, projection='polar')
    ax4.scatter(theta_rad, radii, c=thetas, cmap='viridis', s=50, alpha=0.7, edgecolors='black', linewidth=0.5)
    ax4.set_theta_zero_location('E')
    ax4.set_theta_direction(1)
    ax4.set_title('Offset Polar Plot (Data-Centered)', fontsize=13, fontweight='bold', pad=20)
    
    # Offset scale - centered on data range
    r_min, r_max = radii.min(), radii.max()
    r_range = r_max - r_min
    padding = 0.1 * r_range if r_range > 0 else 0.1
    ax4.set_ylim(r_min - padding, r_max + padding)
    
    plt.tight_layout()
    plt.savefig(filename, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"[Plot] Saved: {filename}")


def save_csv(part_id, measurements, fit_result):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    csvs = sorted(DATA_DIR.glob("diameter_measurements_*.csv"))
    csv_file = None
    
    if csvs:
        latest = csvs[-1]
        try:
            with open(latest) as f:
                if sum(1 for _ in f) - 1 < MAX_RECORDS_PER_CSV:
                    csv_file = latest
        except:
            pass
    
    if not csv_file:
        csv_file = DATA_DIR / f"diameter_measurements_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(csv_file, 'w', newline='') as f:
            csv.writer(f).writerow([
                'Timestamp', 'Part_ID', 'Num_Measurements', 'Center_X_inches', 'Center_Y_inches',
                'Diameter_inches', 'Radius_inches', 'RMS_Residual_inches', 'Max_Residual_inches',
                'R_Squared', 'Relative_RMS_Error_percent'
            ])
        print(f"[Data] Created: {csv_file}")
    
    with open(csv_file, 'a', newline='') as f:
        csv.writer(f).writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), part_id, len(measurements),
            f"{fit_result.center_x:.6f}", f"{fit_result.center_y:.6f}",
            f"{fit_result.diameter:.6f}", f"{fit_result.diameter/2:.6f}",
            f"{fit_result.residual_rms:.6f}", f"{fit_result.max_residual:.6f}",
            f"{fit_result.r_squared:.8f}",
            f"{100*fit_result.residual_rms/(fit_result.diameter/2):.4f}"
        ])
    print(f"[Data] Appended to: {csv_file}")


def print_results(part_id, measurements, fit_result):
    print("\n" + "="*60)
    print(f"RESULTS - Part {part_id}")
    print("="*60)
    print(f"Measurements: {len(measurements)}")
    print(f"Diameter:     {fit_result.diameter:.4f} inches")
    print(f"Radius:       {fit_result.diameter/2:.4f} inches")
    print(f"Center (X,Y): ({fit_result.center_x:.4f}, {fit_result.center_y:.4f})")
    print(f"RMS Residual: {fit_result.residual_rms:.4f} inches")
    print(f"RMS Error:    {100*fit_result.residual_rms/(fit_result.diameter/2):.3f}%")
    print(f"R²:           {fit_result.r_squared:.6f}")
    print("="*60)


def main():
    if not telnetlib3:
        print("ERROR: Install telnetlib3")
        return
    
    print("\n" + "="*60)
    print("ZABER COGNEX DIAMETER SCANNER")
    print("="*60)
    
    while True:
        part_id = input("\nPart ID: ").strip()
        if part_id:
            break
    
    step_input = input(f"Step size [default: {INDEX_STEP_DEG}]: ").strip()
    step_deg = float(step_input) if step_input else INDEX_STEP_DEG
    
    rot_input = input("Rotations [default: 1]: ").strip()
    num_rotations = int(rot_input) if rot_input else 1
    
    plot_input = input("Real-time plot? (y/n) [default: n]: ").strip().lower()
    real_time = plot_input in ('y', 'yes')
    
    zaber_conn = open_zaber_connection()

    async def run_measurement():
        with zaber_conn:
            axis = setup_zaber_axis(zaber_conn)

            cognex = CognexConnection()
            await cognex.connect()

            try:
                num_steps = int(num_rotations * 360.0 / step_deg)
                measurements = await sequencer(axis, cognex, step_deg, num_steps, SPEED_DEG_S, ACCEL_DEG_S2, DWELL_S, real_time)
                return measurements
            finally:
                await cognex.disconnect()
    
    measurements = asyncio.run(run_measurement())
    
    if len(measurements) < 3:
        print("\nERROR: Not enough measurements")
        return
    
    try:
        fit = fit_circle(measurements)
        save_csv(part_id, measurements, fit)
        print_results(part_id, measurements, fit)
        save_plot(measurements, fit, part_id)
    except Exception as e:
        print(f"ERROR: {e}")
        save_plot(measurements, None, part_id)
    
    print("\n[Complete]\n")


if __name__ == "__main__":
    main()