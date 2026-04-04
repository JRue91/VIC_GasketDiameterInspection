from __future__ import annotations

"""
Calibration surface mapping at 1-degree increments (0-359).
Reads Cognex cell M7 at each position and saves to a calibration CSV.
"""

import time
import asyncio
import csv
from datetime import datetime
from pathlib import Path
from zaber_motion import Units

from common import (
    telnetlib3, CognexConnection, MeasurementPoint,
    SPEED_DEG_S, ACCEL_DEG_S2, DWELL_S,
    open_zaber_connection, setup_zaber_axis,
)

# ======= CALIBRATION CONFIG =======
CALIBRATION_CELL = "F25"
CALIBRATION_STEP_DEG = 1.0
CALIBRATION_DIR = Path("../calibration")


async def calibration_scan(axis, cognex, step_deg, speed, accel, dwell, stop_event=None):
    """Scan 0-359 degrees at step_deg increments, reading CALIBRATION_CELL at each position."""
    num_steps = int(360.0 / step_deg)
    measurements = []
    base = axis.get_position(Units.ANGLE_DEGREES)

    print("\n" + "=" * 70)
    print(f"CALIBRATION SCAN: {num_steps} positions at {step_deg} deg increments")
    print(f"Reading Cognex cell: {CALIBRATION_CELL}")
    print("=" * 70)

    for i in range(num_steps):
        if stop_event and stop_event.is_set():
            print("\n[STOPPED] Scan cancelled by user.")
            break

        target = base + i * step_deg
        print(f"\n>>> POSITION {i}: {target:.3f} deg")

        if i > 0:
            print(f"  [1] MOVE")
            axis.move_absolute(target, Units.ANGLE_DEGREES, wait_until_idle=True,
                               velocity=speed, velocity_unit=Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND,
                               acceleration=accel, acceleration_unit=Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED)
            print(f"  [2] DWELL {dwell}s")
            time.sleep(dwell)

        axis.wait_until_idle()
        print(f"  [3] TRIGGER + READ")
        val, read_attempts, trigger_attempts = await cognex.trigger_and_read(CALIBRATION_CELL)
        pos = axis.get_position(Units.ANGLE_DEGREES)
        measurements.append(MeasurementPoint(pos, val, time.time(), read_attempts))
        print(f"  [4] RECORDED: {pos:.3f} deg = {val:.6f} (attempts: {trigger_attempts})")

    # Return to start
    print(f"\n>>> RETURN TO START")
    axis.move_absolute(base + 360.0, Units.ANGLE_DEGREES, wait_until_idle=True,
                       velocity=speed, velocity_unit=Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND,
                       acceleration=accel, acceleration_unit=Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED)
    print(f"  Done")

    print("\n" + "=" * 70)
    print(f"CALIBRATION COMPLETE: {len(measurements)} measurements")
    print("=" * 70)

    return measurements


def save_calibration(measurements, cal_id, step_deg):
    """Save calibration data to CSV with step size in the header."""
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    filename = CALIBRATION_DIR / f"calibration_{cal_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['StepSize', str(step_deg)])
        writer.writerow(['Degree', 'Value', 'Timestamp'])
        for m in measurements:
            writer.writerow([f"{m.theta_deg:.3f}", f"{m.value:.6f}", f"{m.timestamp:.3f}"])

    print(f"[Calibration] Saved: {filename}")
    return filename


def load_calibration(filepath):
    """Load calibration data from CSV. Returns (step_deg, data) where data is list of (degree, value) tuples."""
    data = []
    step_deg = None
    with open(filepath) as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if row[0] == 'StepSize':
                step_deg = int(row[1])
                continue
            if row[0] == 'Degree':
                continue
            data.append((float(row[0]), float(row[1])))
    # Fallback for older files without StepSize header
    if step_deg is None and len(data) >= 2:
        step_deg = int(round(abs(data[1][0] - data[0][0])))
    return step_deg, data


def main():
    if not telnetlib3:
        print("ERROR: Install telnetlib3")
        return

    print("\n" + "=" * 60)
    print("CALIBRATION SURFACE MAPPING")
    print("=" * 60)

    while True:
        cal_id = input("\nCalibration ID (e.g. 'ring_gauge_1'): ").strip()
        if cal_id:
            break

    step_input = input(f"Step size [default: {CALIBRATION_STEP_DEG}]: ").strip()
    step_deg = float(step_input) if step_input else CALIBRATION_STEP_DEG

    num_steps = int(360.0 / step_deg)
    print(f"\nThis will take {num_steps} measurements at {step_deg} deg increments.")
    confirm = input("Proceed? (y/n): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("Cancelled.")
        return

    zaber_conn = open_zaber_connection()

    async def run_calibration():
        with zaber_conn:
            axis = setup_zaber_axis(zaber_conn)

            cognex = CognexConnection()
            await cognex.connect()

            try:
                measurements = await calibration_scan(axis, cognex, step_deg, SPEED_DEG_S, ACCEL_DEG_S2, DWELL_S)
                return measurements
            finally:
                await cognex.disconnect()

    measurements = asyncio.run(run_calibration())

    if not measurements:
        print("\nERROR: No measurements collected")
        return

    cal_file = save_calibration(measurements, cal_id, step_deg)

    # Print summary
    values = [m.value for m in measurements]
    print("\n" + "=" * 60)
    print(f"CALIBRATION SUMMARY - {cal_id}")
    print("=" * 60)
    print(f"Points:    {len(measurements)}")
    print(f"Step size: {step_deg} deg")
    print(f"Min value: {min(values):.6f}")
    print(f"Max value: {max(values):.6f}")
    print(f"Range:     {max(values) - min(values):.6f}")
    print(f"Mean:      {sum(values)/len(values):.6f}")
    print(f"File:      {cal_file}")
    print("=" * 60)

    print("\n[Complete]\n")


if __name__ == "__main__":
    main()
