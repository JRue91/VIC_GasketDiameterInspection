from __future__ import annotations

"""
Zaber OFFLINE-only connect + identify + home + index
- Uses **local Device Database** only (no detect_devices; no online fallback).
- Points to C:\Zaber Devices Database and tries .lzma first, then .sqlite.
- Opens the link, IDENTIFIES device at ADDRESS 1, homes the target axis, then indexes.
"""

import os
import time
from zaber_motion.ascii import Connection
from zaber_motion import Units, Library, DeviceDbSourceType
from zaber_motion.exceptions import DeviceDbFailedException, DeviceNotIdentifiedException

# ======= LOCAL ZABER DEVICE DATABASE (OFFLINE ONLY) =======
DB_DIR = r"C:/Zaber Devices Database"
DB_CANDIDATES = [
    os.path.join(DB_DIR, "devices-public.sqlite"),
   ]

_DB_SET = False
for _cand in DB_CANDIDATES:
    if os.path.isfile(_cand):
        try:
            Library.set_device_db_source(DeviceDbSourceType.FILE, _cand)
            print(f"[Zaber] Using local device database: {_cand}")
            _DB_SET = True
            break
        except Exception as e:
            print(f"[Zaber] ERROR setting local device database at {_cand}: {e}")
if not _DB_SET:
    print("[Zaber] WARNING: No local device database file found in C:/Zaber Devices Database.\n"
          "Place devices-public.sqlite.lzma (preferred) or devices-public.sqlite in that folder.")
# ===========================================================

# ======= LINK CONFIG =======
USE_ETHERNET = False            # False = USB/Serial (COM port). True = Ethernet.
PORT = "COM4"                   # <-- set your COM port (e.g., COM3, COM4)
ETH_HOST = "192.168.0.50"       # <-- controller/stage IP if using Ethernet
ETH_PORT = 23                   # common Zaber ASCII/Telnet port
DEVICE_ADDRESS = 1              # common default Zaber address
# ============================

# ======= INDEXING PARAMETERS =======
AXIS_NUMBER = 1                 # axis to rotate on the device
INDEX_STEP_DEG = 10.0           # step size in degrees (can be negative)
TOTAL_ROTATION_DEG = 360.0      # total rotation to cover (can be > 360)
SPEED_DEG_S = 50.0              # rotation speed in degrees/second
DWELL_S = 0.75                   # pause after each step (seconds)
# ===================================


def index_scan(axis, step_deg: float, total_deg: float, speed_deg_s: float, dwell_s: float = 0.0):
    """Rotate the axis in repeated relative steps until total_deg is reached.
    Works fully offline after device.identify()."""

    # 0) Ensure we’re not mid-move before (re)configuring
    axis.wait_until_idle()

    # 1) Apply motion parameters that this command actually uses
    try:
        # Set the commanded move speed (what move_relative uses)
        axis.settings.set('speed',    float(speed_deg_s), Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND)
        # Optionally also cap it (harmless if equal or higher)
        axis.settings.set('maxspeed', float(360.0), Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND)
    except Exception as e:
        print(f"[WARN] Could not set speed via settings: {e}. Proceeding with controller's current speed.")

    # 2) Compute step plan using the *current* step size
    remaining = abs(float(total_deg))
    direction = 1.0 if total_deg >= 0 else -1.0
    base_step = abs(float(step_deg)) * direction


    #3) Compute absolute move step plan
    # Build absolute target list from a single base reference to avoid drift
    base_pos = axis.get_position(Units.ANGLE_DEGREES)
    direction = 1.0 if total_deg >= 0 else -1.0
    step_mag = abs(float(step_deg))
    total_mag = abs(float(total_deg))

    if step_mag <= 0:
        print("[ERROR] step_deg must be > 0 for absolute scan.")
        return

    full_steps = int(total_mag // step_mag)
    remainder = total_mag - full_steps * step_mag

    targets = [base_pos + direction * (i * step_mag) for i in range(1, full_steps + 1)]
    if remainder > 1e-9:
        targets.append(base_pos + direction * total_mag)

    """
    step_count = 0
    while remaining > 1e-9:
        this_step = base_step if abs(base_step) <= remaining else direction * remaining
        print(f"Step {step_count+1}: move_rel {this_step:.6f} deg @ ~{speed_deg_s} deg/s")
        axis.move_relative(this_step, Units.ANGLE_DEGREES)   # blocking; still wait for safety
        axis.wait_until_idle()
        if dwell_s > 0:
            time.sleep(dwell_s)
        remaining -= abs(this_step)
        step_count += 1
    """
     # Execute absolute moves
    for i, tgt in enumerate(targets, start=1):
        print(f"Step {i}: move_abs -> {tgt:.6f} deg (from base {base_pos:.6f} deg)")
        #axis.move_absolute(tgt, Units.ANGLE_DEGREES)
        axis.move_absolute(tgt, unit = Units.ANGLE_DEGREES, wait_until_idle = True, velocity = speed_deg_s, velocity_unit = Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND, acceleration = 500, acceleration_unit = Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED)
        #axis.wait_until_idle()
        if dwell_s > 0:
            time.sleep(dwell_s)

    print(f"Indexing complete: {i} steps, total {total_deg} deg.")


def open_connection():
    if USE_ETHERNET:
        return Connection.open_tcp_ip(ETH_HOST, ETH_PORT)
    else:
        return Connection.open_serial_port(PORT)


def main():
    # Make sure no other app (e.g., Zaber Launcher/Console) is holding the port.
    with open_connection() as connection:
        print("Opened", "Ethernet" if USE_ETHERNET else "Serial", "connection")

        # === OFFLINE path: no detect_devices(), go straight to a known address ===
        dev = connection.get_device(DEVICE_ADDRESS)
        try:
            print(f"Identifying device at address {DEVICE_ADDRESS} using local DB...")
            dev.identify()  # uses the local DB we configured above
        except DeviceDbFailedException as e:
            print("[Zaber] Local Device Database error during identify(). Check file path/permissions.")
            print("Details:", e)
            return
        except Exception as e:
            print("[Zaber] identify() failed:", e)
            return

        # Home and index the chosen axis
        axis = dev.get_axis(AXIS_NUMBER)
        print("Homing axis...")
        axis.home()
        axis.wait_until_idle()

        index_scan(axis, INDEX_STEP_DEG, TOTAL_ROTATION_DEG, SPEED_DEG_S, DWELL_S)


if __name__ == "__main__":
    main()
