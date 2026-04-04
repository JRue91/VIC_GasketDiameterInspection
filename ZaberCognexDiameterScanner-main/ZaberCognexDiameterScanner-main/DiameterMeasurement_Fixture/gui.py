from __future__ import annotations

"""
Tkinter control panel GUI for the Gasket Diameter Inspection System.
Provides a unified interface for DiameterScan, CalibrationScan, and CalibrationVerify.
"""

import os
import sys
import io
import threading
import queue
import asyncio
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from pathlib import Path

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from PIL import Image

# Ensure working directory is the script's folder so relative paths work
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import common
from common import CognexConnection, open_zaber_connection, setup_zaber_axis
import DiameterScan
from DiameterScan import sequencer, fit_circle, save_plot, save_csv, print_results
import CalibrationScan
from CalibrationScan import calibration_scan, save_calibration, load_calibration
import CalibrationVerify
from CalibrationVerify import (
    find_calibration_files, compare, print_comparison, save_comparison_plot,
)


# ---------------------------------------------------------------------------
# Stdout / Stderr redirect
# ---------------------------------------------------------------------------

class RedirectStream(io.TextIOBase):
    """Replaces sys.stdout/stderr to route print() output into a queue."""

    def __init__(self, log_queue: queue.Queue, tag: str = ""):
        super().__init__()
        self._queue = log_queue
        self._tag = tag  # e.g. "ERROR" for stderr

    def write(self, text):
        if text:
            if self._tag:
                self._queue.put(f"[{self._tag}] {text}")
            else:
                self._queue.put(text)
        return len(text) if text else 0

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# Settings Panel (sidebar)
# ---------------------------------------------------------------------------

class SettingsPanel(ttk.Frame):
    """Sidebar panel with hardware configuration fields."""

    def __init__(self, parent):
        super().__init__(parent)
        self._vars: dict[str, tk.StringVar] = {}
        self._build()

    def _build(self):
        # --- Zaber ---
        zaber_lf = ttk.LabelFrame(self, text="Zaber Stage", padding=8)
        zaber_lf.pack(fill=tk.X, padx=4, pady=(4, 2))

        self._add_field(zaber_lf, "COM Port", "zaber_port", common.PORT)
        self._add_field(zaber_lf, "Device Address", "zaber_device", str(common.DEVICE_ADDRESS))
        self._add_field(zaber_lf, "Axis Number", "zaber_axis", str(common.AXIS_NUMBER))
        self._add_field(zaber_lf, "Speed (deg/s)", "speed", str(common.SPEED_DEG_S))
        self._add_field(zaber_lf, "Accel (deg/s\u00b2)", "accel", str(common.ACCEL_DEG_S2))
        self._add_field(zaber_lf, "Dwell (s)", "dwell", str(common.DWELL_S))

        # --- Cognex ---
        cognex_lf = ttk.LabelFrame(self, text="Cognex IL38", padding=8)
        cognex_lf.pack(fill=tk.X, padx=4, pady=(2, 2))

        self._add_field(cognex_lf, "IP Address", "cognex_host", common.COGNEX_HOST)
        self._add_field(cognex_lf, "Port", "cognex_port", str(common.COGNEX_PORT))
        self._add_field(cognex_lf, "Username", "cognex_user", common.COGNEX_USER)
        self._add_field(cognex_lf, "Password", "cognex_pass", common.COGNEX_PASS)
        self._add_field(cognex_lf, "Max Retries", "cognex_retries", str(common.COGNEX_MAX_RETRIES))

        # --- Restore defaults ---
        ttk.Button(self, text="Restore Defaults", command=self._restore_defaults).pack(
            pady=(6, 4), padx=4, fill=tk.X
        )

    def _add_field(self, parent, label_text, key, default):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=1)
        ttk.Label(row, text=label_text, width=16, anchor=tk.W).pack(side=tk.LEFT)
        var = tk.StringVar(value=default)
        ttk.Entry(row, textvariable=var, width=14).pack(side=tk.RIGHT, fill=tk.X, expand=True)
        self._vars[key] = var

    def get(self, key: str) -> str:
        return self._vars[key].get()

    def get_float(self, key: str) -> float:
        return float(self._vars[key].get())

    def get_int(self, key: str) -> int:
        return int(self._vars[key].get())

    def apply_to_modules(self):
        """Push current settings into the module-level globals before a scan."""
        common.PORT = self.get("zaber_port")
        common.DEVICE_ADDRESS = self.get_int("zaber_device")
        common.AXIS_NUMBER = self.get_int("zaber_axis")
        common.COGNEX_HOST = self.get("cognex_host")
        common.COGNEX_PORT = self.get_int("cognex_port")
        common.COGNEX_USER = self.get("cognex_user")
        common.COGNEX_PASS = self.get("cognex_pass")
        common.COGNEX_MAX_RETRIES = self.get_int("cognex_retries")

    def _restore_defaults(self):
        defaults = {
            "zaber_port": "COM4", "zaber_device": "1", "zaber_axis": "1",
            "speed": "30.0", "accel": "40.0", "dwell": "0.25",
            "cognex_host": "192.168.0.150", "cognex_port": "23",
            "cognex_user": "admin", "cognex_pass": "",
            "cognex_retries": "5",
        }
        for key, val in defaults.items():
            self._vars[key].set(val)


# ---------------------------------------------------------------------------
# Log Panel
# ---------------------------------------------------------------------------

class LogPanel(ttk.Frame):
    """Scrolled text area that displays redirected print() output."""

    def __init__(self, parent, log_queue: queue.Queue):
        super().__init__(parent)
        self._queue = log_queue

        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Log Output", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="Clear", command=self._clear).pack(side=tk.RIGHT, padx=4)

        self.text = scrolledtext.ScrolledText(
            self, wrap=tk.WORD, state=tk.DISABLED,
            font=("Consolas", 9), height=10,
        )
        self.text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self._poll()

    def _poll(self):
        try:
            while True:
                text = self._queue.get_nowait()
                self.text.configure(state=tk.NORMAL)
                self.text.insert(tk.END, text)
                self.text.see(tk.END)
                self.text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.after(50, self._poll)

    def _clear(self):
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# Plot Panel
# ---------------------------------------------------------------------------

class PlotPanel(ttk.Frame):
    """Embedded matplotlib canvas for displaying result PNGs."""

    def __init__(self, parent):
        super().__init__(parent)
        self.fig = Figure(figsize=(8, 5), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._show_placeholder()

    def _show_placeholder(self):
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        ax.text(0.5, 0.5, "No plot to display", ha="center", va="center",
                fontsize=14, color="gray")
        ax.axis("off")
        self.canvas.draw()

    def display_png(self, path: Path):
        """Load and display a PNG image."""
        if not path or not path.exists():
            self._show_placeholder()
            return
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        img = Image.open(path)
        ax.imshow(img)
        ax.axis("off")
        self.fig.tight_layout(pad=0)
        self.canvas.draw()

    def clear(self):
        self._show_placeholder()


# ---------------------------------------------------------------------------
# Status Bar
# ---------------------------------------------------------------------------

class StatusBar(ttk.Frame):
    """Shows current scan state."""

    def __init__(self, parent):
        super().__init__(parent)
        lf = ttk.LabelFrame(self, text="Status", padding=6)
        lf.pack(fill=tk.X, padx=4, pady=4)

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(lf, text="Scan:").pack(anchor=tk.W)
        self._label = ttk.Label(lf, textvariable=self._status_var, font=("Segoe UI", 10, "bold"))
        self._label.pack(anchor=tk.W, padx=(10, 0))

    def set(self, text: str):
        self._status_var.set(text)


# ---------------------------------------------------------------------------
# Diameter Scan Tab
# ---------------------------------------------------------------------------

class DiameterScanTab(ttk.Frame):
    """Tab for running diameter measurements."""

    def __init__(self, parent, app: "GasketInspectorApp"):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        form = ttk.LabelFrame(self, text="Diameter Scan Parameters", padding=10)
        form.pack(fill=tk.X, padx=8, pady=8)

        self.part_id_var = tk.StringVar()
        self.step_var = tk.StringVar(value="5.0")
        self.rotations_var = tk.StringVar(value="1")
        self.cell_var = tk.StringVar(value="B21")

        self._add_field(form, "Part ID *", self.part_id_var)
        self._add_field(form, "Step Size (deg)", self.step_var)
        self._add_field(form, "Rotations", self.rotations_var)
        self._add_field(form, "Cognex Cell", self.cell_var)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=8)
        self.start_btn = ttk.Button(btn_frame, text="Start Scan", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.app.request_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

    def _add_field(self, parent, label, var):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=label, width=18, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var).pack(side=tk.RIGHT, fill=tk.X, expand=True)

    def _start(self):
        part_id = self.part_id_var.get().strip()
        if not part_id:
            messagebox.showerror("Validation", "Part ID is required.")
            return
        try:
            step_deg = float(self.step_var.get())
            num_rotations = int(self.rotations_var.get())
            if step_deg <= 0 or num_rotations <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Validation", "Step size and rotations must be positive numbers.")
            return

        cell = self.cell_var.get().strip()
        if not cell:
            messagebox.showerror("Validation", "Cognex Cell is required.")
            return

        speed = self.app.settings.get_float("speed")
        accel = self.app.settings.get_float("accel")
        dwell = self.app.settings.get_float("dwell")

        self.app.start_scan(
            "Diameter Scan",
            self._run_thread,
            (part_id, step_deg, num_rotations, cell, speed, accel, dwell),
        )

    def _run_thread(self, part_id, step_deg, num_rotations, cell, speed, accel, dwell):
        self.app.settings.apply_to_modules()
        DiameterScan.COGNEX_CELL = cell

        zaber_conn = open_zaber_connection()

        async def run():
            with zaber_conn:
                axis = setup_zaber_axis(zaber_conn)
                cognex = CognexConnection()
                await cognex.connect()
                try:
                    num_steps = int(num_rotations * 360.0 / step_deg)
                    return await sequencer(axis, cognex, step_deg, num_steps,
                                           speed, accel, dwell, False)
                finally:
                    await cognex.disconnect()

        measurements = asyncio.run(run())

        if len(measurements) < 3:
            self.app._result_queue.put(("error", "Not enough measurements collected."))
            return

        fit = fit_circle(measurements)
        save_csv(part_id, measurements, fit)
        print_results(part_id, measurements, fit)
        save_plot(measurements, fit, part_id)

        plot_files = sorted(DiameterScan.PLOTS_DIR.glob(f"{part_id}_circle_fit_result_*.png"))
        plot_path = plot_files[-1] if plot_files else None

        self.app._result_queue.put(("complete", {"plot_path": plot_path}))


# ---------------------------------------------------------------------------
# Calibration Scan Tab
# ---------------------------------------------------------------------------

class CalibrationScanTab(ttk.Frame):
    """Tab for running calibration surface mapping."""

    def __init__(self, parent, app: "GasketInspectorApp"):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        form = ttk.LabelFrame(self, text="Calibration Scan Parameters", padding=10)
        form.pack(fill=tk.X, padx=8, pady=8)

        self.cal_id_var = tk.StringVar()
        self.step_var = tk.StringVar(value="1.0")
        self.cell_var = tk.StringVar(value="F25")

        self._add_field(form, "Calibration ID *", self.cal_id_var)
        self._add_field(form, "Step Size (deg)", self.step_var)
        self._add_field(form, "Cognex Cell", self.cell_var)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=8)
        self.start_btn = ttk.Button(btn_frame, text="Start Calibration", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.app.request_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

    def _add_field(self, parent, label, var):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=label, width=18, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var).pack(side=tk.RIGHT, fill=tk.X, expand=True)

    def _start(self):
        cal_id = self.cal_id_var.get().strip()
        if not cal_id:
            messagebox.showerror("Validation", "Calibration ID is required.")
            return
        try:
            step_deg = float(self.step_var.get())
            if step_deg <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Validation", "Step size must be a positive number.")
            return

        cell = self.cell_var.get().strip()
        if not cell:
            messagebox.showerror("Validation", "Cognex Cell is required.")
            return

        speed = self.app.settings.get_float("speed")
        accel = self.app.settings.get_float("accel")
        dwell = self.app.settings.get_float("dwell")

        self.app.start_scan(
            "Calibration Scan",
            self._run_thread,
            (cal_id, step_deg, cell, speed, accel, dwell),
        )

    def _run_thread(self, cal_id, step_deg, cell, speed, accel, dwell):
        self.app.settings.apply_to_modules()
        CalibrationScan.CALIBRATION_CELL = cell

        zaber_conn = open_zaber_connection()

        async def run():
            with zaber_conn:
                axis = setup_zaber_axis(zaber_conn)
                cognex = CognexConnection()
                await cognex.connect()
                try:
                    return await calibration_scan(axis, cognex, step_deg,
                                                  speed, accel, dwell)
                finally:
                    await cognex.disconnect()

        measurements = asyncio.run(run())

        if not measurements:
            self.app._result_queue.put(("error", "No measurements collected."))
            return

        save_calibration(measurements, cal_id)

        values = [m.value for m in measurements]
        print(f"\nCalibration Summary: {len(measurements)} points, "
              f"range={max(values)-min(values):.6f}, mean={sum(values)/len(values):.6f}")

        self.app._result_queue.put(("complete", {"plot_path": None}))


# ---------------------------------------------------------------------------
# Calibration Verify Tab
# ---------------------------------------------------------------------------

class CalibrationVerifyTab(ttk.Frame):
    """Tab for verifying measurements against a calibration reference."""

    def __init__(self, parent, app: "GasketInspectorApp"):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        form = ttk.LabelFrame(self, text="Calibration Verify Parameters", padding=10)
        form.pack(fill=tk.X, padx=8, pady=8)

        # Calibration file selector
        file_row = ttk.Frame(form)
        file_row.pack(fill=tk.X, pady=2)
        ttk.Label(file_row, text="Calibration File *", width=18, anchor=tk.W).pack(side=tk.LEFT)
        self.file_var = tk.StringVar()
        self.file_combo = ttk.Combobox(file_row, textvariable=self.file_var, state="readonly")
        self.file_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(file_row, text="Refresh", command=self._refresh_files).pack(side=tk.RIGHT)

        self.step_var = tk.StringVar(value="1.0")
        self._add_field(form, "Step Size (deg)", self.step_var)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=8)
        self.start_btn = ttk.Button(btn_frame, text="Start Verification", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.app.request_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        # Populate file list
        self._refresh_files()
        self.file_combo.bind("<<ComboboxSelected>>", self._on_file_selected)

    def _add_field(self, parent, label, var):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=label, width=18, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var).pack(side=tk.RIGHT, fill=tk.X, expand=True)

    def _refresh_files(self):
        self._cal_files = find_calibration_files()
        names = [f.name for f in self._cal_files]
        self.file_combo["values"] = names
        if names:
            self.file_combo.current(len(names) - 1)
            self._on_file_selected()

    def _on_file_selected(self, event=None):
        idx = self.file_combo.current()
        if idx < 0 or idx >= len(self._cal_files):
            return
        cal_data = load_calibration(self._cal_files[idx])
        if len(cal_data) >= 2:
            step = abs(cal_data[1][0] - cal_data[0][0])
            self.step_var.set(str(step))

    def _start(self):
        idx = self.file_combo.current()
        if idx < 0 or idx >= len(self._cal_files):
            messagebox.showerror("Validation", "Select a calibration file.")
            return
        try:
            step_deg = float(self.step_var.get())
            if step_deg <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Validation", "Step size must be a positive number.")
            return

        cal_file = self._cal_files[idx]
        speed = self.app.settings.get_float("speed")
        accel = self.app.settings.get_float("accel")
        dwell = self.app.settings.get_float("dwell")

        self.app.start_scan(
            "Calibration Verify",
            self._run_thread,
            (cal_file, step_deg, speed, accel, dwell),
        )

    def _run_thread(self, cal_file, step_deg, speed, accel, dwell):
        self.app.settings.apply_to_modules()

        cal_data = load_calibration(cal_file)
        print(f"Loaded {len(cal_data)} calibration points from {cal_file.name}")

        zaber_conn = open_zaber_connection()

        async def run():
            with zaber_conn:
                axis = setup_zaber_axis(zaber_conn)
                cognex = CognexConnection()
                await cognex.connect()
                try:
                    return await calibration_scan(axis, cognex, step_deg,
                                                  speed, accel, dwell)
                finally:
                    await cognex.disconnect()

        measurements = asyncio.run(run())

        if not measurements:
            self.app._result_queue.put(("error", "No measurements collected."))
            return

        results = compare(cal_data, measurements)
        print_comparison(results)

        cal_id = cal_file.stem.replace("calibration_", "").rsplit("_", 2)[0]
        save_comparison_plot(results, cal_id)

        # Find the latest verification plot
        verify_dir = CalibrationVerify.VERIFY_PLOTS_DIR
        plot_files = sorted(verify_dir.glob(f"verify_{cal_id}_*.png"))
        plot_path = plot_files[-1] if plot_files else None

        self.app._result_queue.put(("complete", {"plot_path": plot_path}))


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class GasketInspectorApp:
    """Main application window tying everything together."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gasket Diameter Inspection System")
        self.root.geometry("1200x800")
        self.root.minsize(900, 600)

        self._log_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._scan_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

        # Redirect stdout/stderr
        sys.stdout = RedirectStream(self._log_queue)
        sys.stderr = RedirectStream(self._log_queue, tag="ERROR")

        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._poll_results()

    def _build_ui(self):
        # Top-level horizontal pane: sidebar | main
        main_pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True)

        # --- Sidebar ---
        sidebar = ttk.Frame(main_pane, width=260)
        self.settings = SettingsPanel(sidebar)
        self.settings.pack(fill=tk.X)
        self.status_bar = StatusBar(sidebar)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        main_pane.add(sidebar, weight=0)

        # --- Main content ---
        content = ttk.Frame(main_pane)
        main_pane.add(content, weight=1)

        # Notebook (tabs)
        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill=tk.X, padx=4, pady=4)

        self.diameter_tab = DiameterScanTab(self.notebook, self)
        self.calibration_tab = CalibrationScanTab(self.notebook, self)
        self.verify_tab = CalibrationVerifyTab(self.notebook, self)

        self.notebook.add(self.diameter_tab, text="Diameter Scan")
        self.notebook.add(self.calibration_tab, text="Calibration Scan")
        self.notebook.add(self.verify_tab, text="Calibration Verify")

        # Vertical pane: plot | log
        v_pane = ttk.PanedWindow(content, orient=tk.VERTICAL)
        v_pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        self.plot_panel = PlotPanel(v_pane)
        v_pane.add(self.plot_panel, weight=3)

        self.log_panel = LogPanel(v_pane, self._log_queue)
        v_pane.add(self.log_panel, weight=1)

    # -- Scan lifecycle --

    def _all_tabs(self):
        return [self.diameter_tab, self.calibration_tab, self.verify_tab]

    def _set_buttons(self, running: bool):
        """Enable/disable Start and Stop buttons across all tabs."""
        for tab in self._all_tabs():
            tab.start_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
            tab.stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)

    def start_scan(self, name: str, target, args: tuple):
        """Launch a scan in a background thread."""
        if self._scan_thread and self._scan_thread.is_alive():
            messagebox.showwarning("Busy", "A scan is already running.")
            return

        self._stop_event.clear()
        self.status_bar.set(f"Running: {name}")
        self._set_buttons(running=True)
        self.plot_panel.clear()

        def _wrapper():
            try:
                target(*args)
            except Exception as e:
                self._result_queue.put(("error", f"{type(e).__name__}: {e}"))

        self._scan_thread = threading.Thread(target=_wrapper, daemon=True)
        self._scan_thread.start()

    def request_stop(self):
        """Signal the running scan to stop."""
        self._stop_event.set()
        self.status_bar.set("Stopping...")
        print("\n[GUI] Stop requested -- scan will halt after current position.\n")

    def _poll_results(self):
        """Check for results from the scan thread."""
        try:
            while True:
                msg_type, payload = self._result_queue.get_nowait()
                if msg_type == "complete":
                    self.status_bar.set("Complete")
                    self._set_buttons(running=False)
                    plot_path = payload.get("plot_path") if payload else None
                    if plot_path:
                        self.plot_panel.display_png(Path(plot_path))
                    print("\n[GUI] Scan complete.\n")
                elif msg_type == "error":
                    self.status_bar.set("Error")
                    self._set_buttons(running=False)
                    messagebox.showerror("Scan Error", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)

    def _on_closing(self):
        if self._scan_thread and self._scan_thread.is_alive():
            if not messagebox.askyesno(
                "Scan Running",
                "A scan is currently running. Force quit?\n"
                "This will attempt to safely disconnect hardware.",
            ):
                return
            self._stop_event.set()
            self._scan_thread.join(timeout=2.0)

        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = GasketInspectorApp()
    app.run()


if __name__ == "__main__":
    main()
