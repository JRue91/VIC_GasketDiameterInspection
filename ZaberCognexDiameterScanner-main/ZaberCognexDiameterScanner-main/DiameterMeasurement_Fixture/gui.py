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
    find_calibration_files, compare, print_comparison,
    save_comparison_plot, save_multi_run_report,
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
# Settings Dialog (popup)
# ---------------------------------------------------------------------------

class SettingsManager:
    """Manages hardware settings as StringVars and provides access/apply methods.

    The actual dialog is opened on demand via open_dialog().
    """

    DEFAULTS = {
        "zaber_port": "COM4", "zaber_device": "1", "zaber_axis": "1",
        "speed": "30.0", "accel": "40.0", "dwell": "0.25",
        "cognex_host": "192.168.0.150", "cognex_port": "23",
        "cognex_user": "admin", "cognex_pass": "",
        "cognex_retries": "5",
        "diameter_cell": "B21", "calibration_cell": "F25",
    }

    def __init__(self, root: tk.Tk):
        self._root = root
        self._vars: dict[str, tk.StringVar] = {}
        # Initialise vars with current module values
        self._vars["zaber_port"] = tk.StringVar(value=common.PORT)
        self._vars["zaber_device"] = tk.StringVar(value=str(common.DEVICE_ADDRESS))
        self._vars["zaber_axis"] = tk.StringVar(value=str(common.AXIS_NUMBER))
        self._vars["speed"] = tk.StringVar(value=str(common.SPEED_DEG_S))
        self._vars["accel"] = tk.StringVar(value=str(common.ACCEL_DEG_S2))
        self._vars["dwell"] = tk.StringVar(value=str(common.DWELL_S))
        self._vars["cognex_host"] = tk.StringVar(value=common.COGNEX_HOST)
        self._vars["cognex_port"] = tk.StringVar(value=str(common.COGNEX_PORT))
        self._vars["cognex_user"] = tk.StringVar(value=common.COGNEX_USER)
        self._vars["cognex_pass"] = tk.StringVar(value=common.COGNEX_PASS)
        self._vars["cognex_retries"] = tk.StringVar(value=str(common.COGNEX_MAX_RETRIES))
        self._vars["diameter_cell"] = tk.StringVar(value="B21")
        self._vars["calibration_cell"] = tk.StringVar(value="F25")

    # -- Public helpers (used by tabs / scan threads) --

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
        DiameterScan.COGNEX_CELL = self.get("diameter_cell")
        CalibrationScan.CALIBRATION_CELL = self.get("calibration_cell")

    def restore_defaults(self):
        for key, val in self.DEFAULTS.items():
            self._vars[key].set(val)

    # -- Dialog --

    def open_dialog(self):
        """Open a modal settings dialog."""
        dlg = tk.Toplevel(self._root)
        dlg.title("Settings")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self._root)

        notebook = ttk.Notebook(dlg)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        # --- Zaber tab ---
        zaber_frame = ttk.Frame(notebook, padding=12)
        notebook.add(zaber_frame, text="Zaber Stage")

        self._dialog_field(zaber_frame, "COM Port", "zaber_port", 0)
        self._dialog_field(zaber_frame, "Device Address", "zaber_device", 1)
        self._dialog_field(zaber_frame, "Axis Number", "zaber_axis", 2)
        self._dialog_field(zaber_frame, "Speed (deg/s)", "speed", 3)
        self._dialog_field(zaber_frame, "Accel (deg/s\u00b2)", "accel", 4)
        self._dialog_field(zaber_frame, "Dwell (s)", "dwell", 5)

        # --- Cognex tab ---
        cognex_frame = ttk.Frame(notebook, padding=12)
        notebook.add(cognex_frame, text="Cognex IL38")

        self._dialog_field(cognex_frame, "IP Address", "cognex_host", 0)
        self._dialog_field(cognex_frame, "Port", "cognex_port", 1)
        self._dialog_field(cognex_frame, "Username", "cognex_user", 2)
        self._dialog_field(cognex_frame, "Password", "cognex_pass", 3)
        self._dialog_field(cognex_frame, "Max Retries", "cognex_retries", 4)

        ttk.Separator(cognex_frame, orient=tk.HORIZONTAL).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=6,
        )
        ttk.Label(cognex_frame, text="Cell Addresses", font=("Segoe UI", 8, "italic")).grid(
            row=6, column=0, columnspan=2, sticky=tk.W,
        )
        self._dialog_field(cognex_frame, "Diameter Cell", "diameter_cell", 7)
        self._dialog_field(cognex_frame, "Calibration Cell", "calibration_cell", 8)

        # --- Buttons ---
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(btn_frame, text="Restore Defaults", command=self.restore_defaults).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)

        # Centre on parent
        dlg.update_idletasks()
        pw = self._root.winfo_width()
        ph = self._root.winfo_height()
        px = self._root.winfo_x()
        py = self._root.winfo_y()
        dw = dlg.winfo_width()
        dh = dlg.winfo_height()
        dlg.geometry(f"+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")

    def _dialog_field(self, parent, label_text, key, row):
        ttk.Label(parent, text=label_text, anchor=tk.W).grid(
            row=row, column=0, sticky=tk.W, padx=(0, 8), pady=2,
        )
        ttk.Entry(parent, textvariable=self._vars[key], width=20).grid(
            row=row, column=1, sticky=tk.EW, pady=2,
        )
        parent.columnconfigure(1, weight=1)


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
        self.step_var = tk.StringVar(value="5")
        self.rotations_var = tk.StringVar(value="1")

        self._add_field(form, "Part ID *", self.part_id_var)
        self._add_field(form, "Step Size (deg)", self.step_var)
        self._add_field(form, "Rotations", self.rotations_var)

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
            step_deg = int(self.step_var.get())
            num_rotations = int(self.rotations_var.get())
            if step_deg <= 0 or num_rotations <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Validation", "Step size and rotations must be positive whole numbers.")
            return

        speed = self.app.settings.get_float("speed")
        accel = self.app.settings.get_float("accel")
        dwell = self.app.settings.get_float("dwell")

        self.app.start_scan(
            "Diameter Scan",
            self._run_thread,
            (part_id, step_deg, num_rotations, speed, accel, dwell),
        )

    def _run_thread(self, part_id, step_deg, num_rotations, speed, accel, dwell):
        self.app.settings.apply_to_modules()
        stop_event = self.app._stop_event

        zaber_conn = open_zaber_connection()

        async def run():
            with zaber_conn:
                axis = setup_zaber_axis(zaber_conn)
                cognex = CognexConnection()
                await cognex.connect()
                try:
                    num_steps = int(num_rotations * 360.0 / step_deg)
                    return await sequencer(axis, cognex, step_deg, num_steps,
                                           speed, accel, dwell, False,
                                           stop_event=stop_event)
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
        self.step_var = tk.StringVar(value="1")

        self._add_field(form, "Calibration ID *", self.cal_id_var)
        self._add_field(form, "Step Size (deg)", self.step_var)

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
            step_deg = int(self.step_var.get())
            if step_deg <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Validation", "Step size must be a positive whole number.")
            return

        speed = self.app.settings.get_float("speed")
        accel = self.app.settings.get_float("accel")
        dwell = self.app.settings.get_float("dwell")

        self.app.start_scan(
            "Calibration Scan",
            self._run_thread,
            (cal_id, step_deg, speed, accel, dwell),
        )

    def _run_thread(self, cal_id, step_deg, speed, accel, dwell):
        self.app.settings.apply_to_modules()
        stop_event = self.app._stop_event

        zaber_conn = open_zaber_connection()

        async def run():
            with zaber_conn:
                axis = setup_zaber_axis(zaber_conn)
                cognex = CognexConnection()
                await cognex.connect()
                try:
                    return await calibration_scan(axis, cognex, step_deg,
                                                  speed, accel, dwell,
                                                  stop_event=stop_event)
                finally:
                    await cognex.disconnect()

        measurements = asyncio.run(run())

        if not measurements:
            self.app._result_queue.put(("error", "No measurements collected."))
            return

        save_calibration(measurements, cal_id, step_deg)

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

        # Step size display (read-only, loaded from calibration file)
        step_row = ttk.Frame(form)
        step_row.pack(fill=tk.X, pady=2)
        ttk.Label(step_row, text="Step Size (deg)", width=18, anchor=tk.W).pack(side=tk.LEFT)
        self._step_label = ttk.Label(step_row, text="--", anchor=tk.W)
        self._step_label.pack(side=tk.LEFT)

        # Number of runs
        runs_row = ttk.Frame(form)
        runs_row.pack(fill=tk.X, pady=2)
        ttk.Label(runs_row, text="Number of Runs", width=18, anchor=tk.W).pack(side=tk.LEFT)
        self.num_runs_var = tk.StringVar(value="1")
        ttk.Entry(runs_row, textvariable=self.num_runs_var).pack(side=tk.RIGHT, fill=tk.X, expand=True)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=8)
        self.start_btn = ttk.Button(btn_frame, text="Start Verification", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.app.request_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        # Populate file list
        self._refresh_files()
        self.file_combo.bind("<<ComboboxSelected>>", self._on_file_selected)

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
        step_deg, _data = load_calibration(self._cal_files[idx])
        self._step_label.configure(text=str(step_deg) if step_deg else "--")

    def _start(self):
        idx = self.file_combo.current()
        if idx < 0 or idx >= len(self._cal_files):
            messagebox.showerror("Validation", "Select a calibration file.")
            return

        try:
            num_runs = int(self.num_runs_var.get())
            if num_runs <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Validation", "Number of runs must be a positive whole number.")
            return

        cal_file = self._cal_files[idx]
        speed = self.app.settings.get_float("speed")
        accel = self.app.settings.get_float("accel")
        dwell = self.app.settings.get_float("dwell")

        self.app.start_scan(
            "Calibration Verify",
            self._run_thread,
            (cal_file, speed, accel, dwell, num_runs),
        )

    def _run_thread(self, cal_file, speed, accel, dwell, num_runs):
        self.app.settings.apply_to_modules()
        stop_event = self.app._stop_event

        step_deg, cal_data = load_calibration(cal_file)
        print(f"Loaded {len(cal_data)} calibration points from {cal_file.name} (step={step_deg} deg)")
        print(f"Will perform {num_runs} verification run(s).")

        zaber_conn = open_zaber_connection()

        async def run():
            with zaber_conn:
                axis = setup_zaber_axis(zaber_conn)
                cognex = CognexConnection()
                await cognex.connect()
                try:
                    runs = []
                    for i in range(num_runs):
                        if stop_event.is_set():
                            break
                        print(f"\n###### RUN {i + 1} of {num_runs} ######")
                        runs.append(await calibration_scan(
                            axis, cognex, step_deg, speed, accel, dwell,
                            stop_event=stop_event,
                        ))
                    return runs
                finally:
                    await cognex.disconnect()

        runs = asyncio.run(run())

        cal_id = cal_file.stem.replace("calibration_", "").rsplit("_", 2)[0]
        all_run_results = []
        for i, measurements in enumerate(runs, start=1):
            if not measurements:
                print(f"\n[Run {i}] No measurements collected; skipping.")
                continue
            results = compare(cal_data, measurements)
            print(f"\n--- Run {i} of {len(runs)} ---")
            print_comparison(results)
            all_run_results.append(results)

        if not all_run_results:
            self.app._result_queue.put(("error", "No measurements collected."))
            return

        plot_path, _csv_path = save_multi_run_report(all_run_results, cal_id)

        self.app._result_queue.put(("complete", {"plot_path": plot_path}))


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class GasketInspectorApp:
    """Main application window tying everything together."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gasket Diameter Inspection System")
        self.root.geometry("1100x800")
        self.root.minsize(800, 600)

        self._log_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._scan_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

        # Settings manager (no UI widget -- opens a dialog on demand)
        self.settings = SettingsManager(self.root)

        # Redirect stdout/stderr
        sys.stdout = RedirectStream(self._log_queue)
        sys.stderr = RedirectStream(self._log_queue, tag="ERROR")

        self._build_menu()
        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._poll_results()

    # -- Menu bar --

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        # File
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Exit", command=self._on_closing)
        menubar.add_cascade(label="File", menu=file_menu)

        # Edit
        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Settings...", command=self.settings.open_dialog)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        # Help
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def _show_about(self):
        messagebox.showinfo(
            "About",
            "Gasket Diameter Inspection System\n\n"
            "Zaber rotary stage + Cognex IL38 sensor\n"
            "Diameter measurement, calibration, and verification.",
        )

    # -- Main content --

    def _build_ui(self):
        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.X, padx=4, pady=4)

        self.diameter_tab = DiameterScanTab(self.notebook, self)
        self.calibration_tab = CalibrationScanTab(self.notebook, self)
        self.verify_tab = CalibrationVerifyTab(self.notebook, self)

        self.notebook.add(self.diameter_tab, text="Diameter Scan")
        self.notebook.add(self.calibration_tab, text="Calibration Scan")
        self.notebook.add(self.verify_tab, text="Calibration Verify")

        # Vertical pane: plot | log
        v_pane = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        v_pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        self.plot_panel = PlotPanel(v_pane)
        v_pane.add(self.plot_panel, weight=3)

        self.log_panel = LogPanel(v_pane, self._log_queue)
        v_pane.add(self.log_panel, weight=1)

        # Status bar at bottom
        self.status_bar = StatusBar(self.root)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

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
