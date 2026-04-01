from __future__ import annotations

"""
Zaber OFFLINE-only connect + identify + home + index
+ Cognex IL38 trigger + read example (Ethernet) using **telnetlib3** (Python 3.13-safe)
"""

import os
import time
import asyncio
import contextlib
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
    print("[Zaber] WARNING: No local device database file found in C:/Zaber Devices Database."
          "Place devices-public.sqlite in that folder.")
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
SPEED_DEG_S = 60.0              # rotation speed in degrees/second
ACCEL_DEG_S2 = 720.0            # acceleration in deg/s^2 (helps small steps reach speed)
DWELL_S = 0.25                  # pause after each step (seconds)
# ===================================

# ======= COGNEX IL38 CONFIG (telnetlib3) =======
# Requires: pip install telnetlib3
try:
    import telnetlib3  # asyncio telnet client/server lib
except Exception:
    telnetlib3 = None

COGNEX_HOST = "192.168.0.150"   # <-- set to your profiler’s IP
COGNEX_PORT = 23                # Telnet ASCII port (commonly 23)
COGNEX_EOL: str = "\r\n"

COGNEX_USER: str = "admin"      # Login username
COGNEX_PASS: str = ""            # Blank password
# ===============================================


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


# ======= COGNEX IL38 ROUTINES (via telnetlib3) =======

async def _cognex_trigger_and_read_async(cell: str = "B21",
                                         host: str = COGNEX_HOST,
                                         port: int = COGNEX_PORT,
                                         eol: str = COGNEX_EOL,
                                         read_timeout: float = 3.0) -> float:
    """Async Telnet routine using telnetlib3 (Python 3.13 compatible).

    Always performs login (admin / blank password) immediately after connect,
    then sends TRIGGER and GET <cell>. Banners/echo/prompts are ignored, and the
    first real numeric token is parsed and returned.
    """
    if telnetlib3 is None:
        raise RuntimeError("telnetlib3 is not installed. Install with: pip install telnetlib3")

    def _pick_float(txt: str) -> tuple[float | None, bool]:
        """Extract a number from a line; prefer decimals over bare integers.
        Returns (value, is_decimal)."""
        buf: list[str] = []
        toks: list[str] = []
        for ch in txt:
            if ch.isdigit() or ch in "+-.eE":
                buf.append(ch)
            else:
                if buf:
                    toks.append("".join(buf))
                    buf = []
        if buf:
            toks.append("".join(buf))
        # prefer a token with a decimal point
        for t in toks:
            if "." in t:
                try:
                    return float(t), True
                except Exception:
                    pass
        # fallback: first numeric-looking token
        for t in toks:
            if any(c.isdigit() for c in t):
                try:
                    return float(t), False
                except Exception:
                    pass
        return None, False

    async def _drain_until_deadline(reader, deadline: float):
        lines: list[str] = []
        loop = asyncio.get_event_loop()
        while True:
            timeout = deadline - loop.time()
            if timeout <= 0:
                break
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if not line:
                break
            txt = line.strip()
            if txt:
                lines.append(txt)
        return lines

    print(f"[Cognex] Connecting to {host}:{port}, requesting cell {cell}...")
    reader, writer = await telnetlib3.open_connection(host=host, port=port, encoding='ascii')
    try:
        # 1) Drain any banner
        banner = await _drain_until_deadline(reader, asyncio.get_event_loop().time() + 1.0)
        for b in banner:
            print(f"[Cognex] << {b}")

        # 2) LOGIN (always send username + password even if no prompt is shown)
        writer.write(f"{COGNEX_USER}{eol}")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(f"{COGNEX_PASS}{eol}")
        await writer.drain()

        auth_lines = await _drain_until_deadline(reader, asyncio.get_event_loop().time() + 1.0)
        for n in auth_lines:
            print(f"[Cognex] << {n}")
        if any('invalid' in s.lower() or 'denied' in s.lower() for s in auth_lines):
            raise RuntimeError("Login failed (Invalid credentials)")

        # 3) Issue TRIGGER
        writer.write(f"MT{eol}")
        await writer.drain()
        _ = await _drain_until_deadline(reader, asyncio.get_event_loop().time() + 0.5)

        # 4) Request the cell
        writer.write(f"GV {cell}{eol}")
        await writer.drain()

        # 5) Read until a numeric value arrives; prefer a decimal or a line mentioning the cell
        loop = asyncio.get_event_loop()
        end = loop.time() + read_timeout
        last_txt = ''
        val: float | None = None
        got_decimal = False
        while loop.time() < end:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.6)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            txt = line.strip()
            if not txt:
                continue
            up = txt.upper()
            # Skip banners, echoes, prompts, and auth chatter
            if (up.startswith('WELCOME') or 'SESSION' in up or up.startswith('USER') or up.startswith('PASSWORD') or
                up.startswith('LOGIN') or up.startswith('OK') or up.startswith('ERR') or up.startswith('>') or
                up.startswith('PROMPT') or up.startswith('TRIGGER') or up.startswith('GET')):
                continue

            cand, is_dec = _pick_float(txt)
            if cand is None:
                continue

            last_txt = txt
            # Prefer a decimal value or a line that mentions the requested cell
            if is_dec or (cell.upper() in up):
                val = cand
                got_decimal = got_decimal or is_dec
                if is_dec:
                    break  # good enough
            elif val is None:
                # Provisional integer; keep looking for a decimal
                val = cand

        if val is None:
            raise RuntimeError(f"No numeric value received for {cell}. Last line: '{last_txt}'")

        print(f"[Cognex] Cell {cell} = {val:.3f} (raw: {last_txt})")
        return val
    finally:
        # Politely close
        try:
            writer.write(f"EXIT{eol}")
            await writer.drain()
            writer.write(f"LOGOUT{eol}")
            await writer.drain()
        except Exception:
            pass
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

def cognex_trigger_and_read(cell: str = "B21") -> float:
    """Synchronous wrapper for the async telnet function.
    Returns float('nan') on error and prints the exception.
    """
    try:
        return asyncio.run(_cognex_trigger_and_read_async(cell))
    except Exception as e:
        print(f"[Cognex] Error via telnetlib3 for {cell}: {e}")
        return float("nan")


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

        # Example Cognex trigger + read (uses telnetlib3)
        cognex_trigger_and_read("B21")


if __name__ == "__main__":
    main()
