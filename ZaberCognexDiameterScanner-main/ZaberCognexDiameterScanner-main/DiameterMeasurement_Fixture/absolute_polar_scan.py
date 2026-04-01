"""
absolute_polar_scan.py

Acquires profiler data at fixed absolute angles around a rotating device.

Sequence per step:
 1) Index Zaber axis by INDEX_STEP_DEG at SPEED_DPS (deg/s)
 2) Trigger Cognex profiler (Native Mode) and wait for completion
 3) Read RESULT_CELL (numeric)
 4) Store value into the absolute-angle bin for the *current physical angle*
 5) Repeat through 360°; optional multiple revolutions with per-angle averaging

This version uses ABSOLUTE ANGLES: each bin corresponds to a fixed physical angle
relative to the starting zero (or mechanical home), so you can average across
revolutions and compare runs directly.

Dependencies:
  pip install zaber-motion

Tested with Python 3.10+.
"""

from __future__ import annotations
import csv
import math
import time
import socket
import select
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ===================== USER SETTINGS =====================
# Zaber
ZABER_PORT = "COM4"                 # e.g., "COM4" (Windows) or "/dev/ttyUSB0" (Linux)
INDEX_STEP_DEG = 1.0                 # degrees per index (ideally a clean divisor of 360)
SPEED_DPS = 90.0                     # indexing speed in deg/s (will be clamped)
REVOLUTIONS = 1                      # how many full 360° passes to collect
ZERO_AT_START = True                 # treat current position as 0° at start (soft zero)
HOME_AT_START = False                # if True, home before starting (mechanical zero)

# Cognex (Native Mode)
COGNEX_HOST = "192.168.0.150"
COGNEX_PORT = 23
COGNEX_USER = "admin"
COGNEX_PASS = ""
RESULT_CELL = "B21"                  # example: "B3"

# Output files
CSV_SAMPLES = "polar_samples.csv"   # all samples (angle_deg, value, revolution, step_index)
CSV_AVERAGES = "polar_averages.csv" # per-angle averages after acquisition

# Polar plot preview
PLOT_SHOW = True                 # show an interactive polar plot window at the end
PLOT_SAVE_PATH = "polar_preview.png"  # set to "" to skip saving a PNG
PLOT_SAMPLES = False             # overlay raw samples as scatter points

# Timing safety margins
SETTLE_AFTER_MOVE_S = 0.00           # extra time after each index (usually not needed)

# ===================== ZABER WRAPPER =====================
from zaber_motion.ascii import Connection
from zaber_motion import Units

# --- Zaber Device Database (offline) ---
# Force the Zaber library to use the local store seeded by Offline Launcher.
try:
    from zaber_motion import Library
    Library.enable_device_db_store(r"C:\Users\Bbarnes\AppData\Local\Zaber Launcher Offline\zmlDeviceDb")
    print("[Zaber] Using offline device database store.")
except Exception as e:
    print(f"[Zaber] Warning: could not configure offline Device DB ({e}); library may try to use the online DB."); library may try to use the online DB.")

ZABER_MAX_SPEED_DPS = 3000.0  # per model spec (≈500 rpm)
ZABER_MIN_SPEED_DPS = 0.003434


def clamp_speed(dps: float) -> float:
    return max(ZABER_MIN_SPEED_DPS, min(ZABER_MAX_SPEED_DPS, float(dps)))


@dataclass
class ZaberAxis:
    conn: Connection
    device: any
    axis: any

    @classmethod
    def open(cls, port: str) -> "ZaberAxis":
        conn = Connection.open_serial_port(port)
        device = conn.detect_devices()[0]
        axis = device.get_axis(1)
        return cls(conn, device, axis)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def prepare(self, speed_dps: float, *, home: bool, soft_zero: bool) -> float:
        """Prepares axis. Returns starting angle (deg) used as zero reference if soft_zero.
        If home=True, performs a mechanical home first.
        """
        if home:
            self.axis.home()  # blocks until done
        self.axis.settings.set("maxspeed", clamp_speed(speed_dps), Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND)
        start_deg = self.axis.get_position(Units.ANGLE_DEGREES)
        if soft_zero:
            # define current position as 0 reference (software offset in our math)
            return start_deg
        return 0.0

    def index(self, delta_deg: float):
        self.axis.move_relative(delta_deg, Units.ANGLE_DEGREES)
        self.axis.wait_until_idle()

    def get_angle_deg(self) -> float:
        return float(self.axis.get_position(Units.ANGLE_DEGREES))


# ===================== COGNEX NATIVE MODE =====================
class CognexNative:
    def __init__(self, host: str, port: int = 23, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        # swallow any banner text if present
        self._read_available()

    def login(self, user: str, password: str):
        # send username and password, one line each (CRLF)
        self._write_line(user)
        time.sleep(0.1)
        self._write_line(password)
        time.sleep(0.15)
        self._read_available()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # --- helpers ---
    def _write_line(self, s: str):
        assert self.sock is not None
        self.sock.sendall(s.encode("ascii") + b"\r\n")

    def _read_available(self) -> str:
        """Non-blocking read of whatever is available right now."""
        assert self.sock is not None
        buf = bytearray()
        # use select to avoid blocking
        while True:
            rlist, _, _ = select.select([self.sock], [], [], 0)
            if not rlist:
                break
            try:
                chunk = self.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            # small pause to coalesce bursts
            time.sleep(0.01)
        return buf.decode("ascii", errors="ignore")

    def _exchange(self, cmd: str, settle: float = 0.05) -> str:
        self._write_line(cmd)
        t0 = time.time()
        buf = ""
        while time.time() - t0 < self.timeout:
            time.sleep(settle)
            chunk = self._read_available()
            if chunk:
                buf += chunk
                if "\n" in buf:
                    time.sleep(0.05)
                    buf += self._read_available()
                    break
                    return buf.strip()

    # --- commands ---
    def set_online(self, online: bool = True) -> bool:
        resp = self._exchange(f"SO{1 if online else 0}")
        return resp.startswith("1")

    def trigger_and_wait(self) -> bool:
        resp = self._exchange("SW8", settle=0.1)
        return resp.startswith("1")

    @staticmethod
    def _cell_to_gv(cell: str) -> str:
        cell = cell.strip().upper()
        col = "".join([c for c in cell if c.isalpha()]) or "A"
        row_digits = "".join([c for c in cell if c.isdigit()]) or "0"
        row = int(row_digits)
        return f"GV{col}{row:03d}"

    def get_cell_float(self, cell: str) -> float:
        cmd = self._cell_to_gv(cell)
        resp = self._exchange(cmd, settle=0.1)
        tokens: List[str] = []
        for t in resp.replace(",", "\n").splitlines():
            t = t.strip()
            if t:
                tokens.append(t)
        for tok in reversed(tokens):
            try:
                return float(tok)
            except ValueError:
                continue
        raise RuntimeError(f"Failed to parse numeric from {cmd!r} response: {resp!r}")

# ===================== ABSOLUTE ANGLE BINS =====================
@dataclass
class AngleBins:
    step_deg: float
    n_bins: int
    base_step_deg: float
    start_zero_deg: float
    # Accumulators per bin
    sums: List[float]
    counts: List[int]

    @classmethod
    def create(cls, step_deg: float, start_zero_deg: float = 0.0) -> "AngleBins":
        if step_deg <= 0 or step_deg > 360:
            raise ValueError("step_deg must be in (0, 360].")
        # Prefer an integer number of bins; adjust step slightly if needed
        n_bins = int(round(360.0 / step_deg))
        base_step = 360.0 / n_bins
        return cls(
            step_deg=step_deg,
            n_bins=n_bins,
            base_step_deg=base_step,
            start_zero_deg=start_zero_deg % 360.0,
            sums=[0.0] * n_bins,
            counts=[0] * n_bins,
        )

    def angle_to_bin(self, angle_deg: float) -> int:
        # Map a physical angle to bin index relative to our zero
        rel = (angle_deg - self.start_zero_deg) % 360.0
        k = int(round(rel / self.base_step_deg)) % self.n_bins
        return k

    def bin_center_deg(self, k: int) -> float:
        return (self.start_zero_deg + k * self.base_step_deg) % 360.0

    def add(self, angle_deg: float, value: float):
        k = self.angle_to_bin(angle_deg)
        self.sums[k] += float(value)
        self.counts[k] += 1
        return k

    def averages(self) -> List[Tuple[float, Optional[float], int]]:
        out: List[Tuple[float, Optional[float], int]] = []
        for k in range(self.n_bins):
            c = self.counts[k]
            avg = (self.sums[k] / c) if c > 0 else None
            out.append((self.bin_center_deg(k), avg, c))
        return out


# ===================== POLAR PLOT PREVIEW =====================

from typing import Optional


def make_polar_plot(bins: AngleBins, samples: Optional[List[Tuple[float, float]]] = None, *,
                     show: bool = True, save_path: Optional[str] = None, title: str = "Polar Preview") -> None:
    """Draw a polar plot of per-angle averages; optionally overlay raw samples.
    - bins: AngleBins with sums/counts filled
    - samples: list of (angle_deg, value) raw points to overlay (optional)
    - show: call plt.show() if True
    - save_path: if provided and non-empty, save a PNG there
    """
    try:
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[plot] matplotlib is not available: {e}")
        return

    avgs = bins.averages()
    ang_deg = [a for (a, v, c) in avgs if v is not None]
    radii  = [v for (a, v, c) in avgs if v is not None]
    if not ang_deg:
        print("[plot] No averaged data to plot.")
        return

    ang_rad = [math.radians(a) for a in ang_deg]
    # Close the loop for nicer rendering if we have more than one point
    if len(ang_rad) > 1:
        ang_rad.append(ang_rad[0])
        radii.append(radii[0])

    fig = plt.figure()
    ax = fig.add_subplot(111, projection='polar')
    ax.plot(ang_rad, radii)
    ax.set_title(title)

    # Optional raw sample overlay
    if samples:
        s_ang = [math.radians(a) for (a, _r) in samples]
        s_r   = [_r for (_a, _r) in samples]
        ax.scatter(s_ang, s_r, s=8, alpha=0.6)

    if save_path:
        try:
            fig.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"[plot] Saved polar preview -> {save_path}")
        except Exception as e:
            print(f"[plot] Failed to save figure: {e}")

    if show:
        try:
            plt.show()
        except Exception as e:
            print(f"[plot] Could not show figure: {e}")

    plt.close(fig)


# ===================== MAIN ACQUISITION =====================

def run_scan():
    # --- Setup Zaber ---
    z = ZaberAxis.open(ZABER_PORT)
    try:
        start_zero = z.prepare(SPEED_DPS, home=HOME_AT_START, soft_zero=ZERO_AT_START)
        if ZERO_AT_START and not HOME_AT_START:
            print(f"[ZABER] Using current position as 0° (soft zero = {start_zero:.4f}°)")
        elif HOME_AT_START:
            print("[ZABER] Homed to mechanical zero.")

        # --- Setup Cognex ---
        cam = CognexNative(COGNEX_HOST, COGNEX_PORT, timeout=2.0)
        cam.connect()
        try:
            cam.login(COGNEX_USER, COGNEX_PASS)
            if not cam.set_online(True):
                raise RuntimeError("Cognex: failed to go Online (SO1)")

            # --- Precompute bins ---
            bins = AngleBins.create(INDEX_STEP_DEG, start_zero_deg=start_zero if ZERO_AT_START else 0.0)
            if abs(bins.base_step_deg - INDEX_STEP_DEG) > 1e-6:
                print(f"[note] Adjusted effective bin size to {bins.base_step_deg:.6f}° "
                      f"({bins.n_bins} bins) for clean 360° coverage.")

            # --- CSV writers ---
            samples_file = open(CSV_SAMPLES, "w", newline="")
            samples_writer = csv.writer(samples_file)
            samples_writer.writerow(["angle_deg", "value", "revolution", "step_index", "bin_index"])

            # Determine initial absolute angle (for logging only)
            cur_angle = z.get_angle_deg()
            print(f"[START] Current angle = {cur_angle:.4f}°")

            collected_samples: List[Tuple[float, float]] = []

            for rev in range(REVOLUTIONS):
                print(f"\n[REV {rev+1}/{REVOLUTIONS}] Starting...")
                for step_idx in range(bins.n_bins):
                    # 1) Index
                    z.index(bins.base_step_deg)
                    if SETTLE_AFTER_MOVE_S > 0:
                        time.sleep(SETTLE_AFTER_MOVE_S)

                    # Query actual angle from device for absolute mapping
                    cur_angle = z.get_angle_deg()

                    # 2) Trigger & wait
                    if not cam.trigger_and_wait():
                        raise RuntimeError("Cognex: SW8 trigger failed")

                    # 3) Read result cell
                    value = cam.get_cell_float(RESULT_CELL)

                    # collect for optional scatter overlay
                    collected_samples.append((cur_angle, value))

                    # 4) Put into absolute bin
                    k = bins.add(cur_angle, value)

                    # 5) Log sample
                    samples_writer.writerow([f"{cur_angle:.6f}", f"{value:.6f}", rev, step_idx, k])
                    print(f"  step {step_idx+1:>3}/{bins.n_bins}: angle={cur_angle:8.3f}°, bin={k:>3}, value={value}")

            samples_file.close()

            # Write per-angle averages
            with open(CSV_AVERAGES, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["bin_center_deg", "avg_value", "count"])
                for ang, avg, cnt in bins.averages():
                    w.writerow([f"{ang:.6f}", (None if avg is None else f"{avg:.6f}"), cnt])

            # Polar preview (averages + optional sample scatter)
            if PLOT_SHOW or (PLOT_SAVE_PATH and len(PLOT_SAVE_PATH) > 0):
                make_polar_plot(bins, collected_samples if PLOT_SAMPLES else None,
                                show=PLOT_SHOW, save_path=PLOT_SAVE_PATH,
                                title="Polar Preview (averages)")

            print(f"\nDone. Samples -> {CSV_SAMPLES}; Averages -> {CSV_AVERAGES}")

        finally:
            cam.close()

    finally:
        z.close()


if __name__ == "__main__":
    run_scan()
    