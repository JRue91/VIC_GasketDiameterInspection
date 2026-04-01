from __future__ import annotations

"""
Zaber rotary indexing with Cognex IL38 measurement via Modbus TCP.
Much faster than Telnet - triggers and reads in ~100ms instead of ~700ms.
Collects polar coordinates (r, theta) and performs circle fit analysis.
"""

import os
import time
import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import least_squares
from dataclasses import dataclass
from zaber_motion.ascii import Connection
from zaber_motion import Units, Library, DeviceDbSourceType
from zaber_motion.exceptions import DeviceDbFailedException, DeviceNotIdentifiedException

# Modbus TCP library
try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusTcpClient = None
    ModbusException = Exception

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
USE_ETHERNET = False
PORT = "COM4"
ETH_HOST = "192.168.0.50"
ETH_PORT = 23
DEVICE_ADDRESS = 1
# ============================

# ======= INDEXING PARAMETERS =======
AXIS_NUMBER = 1                 # axis to rotate on the device
INDEX_STEP_DEG = 10.0           # step size in degrees
TOTAL_ROTATION_DEG = 360.0      # total rotation to cover
SPEED_DEG_S = 60.0              # rotation speed in degrees/second
ACCEL_DEG_S2 = 500.0            # acceleration in deg/s^2
DECEL_DEG_S2 = 500.0            # deceleration in deg/s^2
DWELL_S = 0.25                  # pause after each step (seconds)
# ===================================

# ======= COGNEX IL38 MODBUS TCP CONFIG =======
COGNEX_HOST = "192.168.0.150"
COGNEX_MODBUS_PORT = 502        # Standard Modbus TCP port

# Modbus register addresses (from Cognex documentation)
MODBUS_STRING_COMMAND_REGISTER = 1001 # String Input register for native commands
MODBUS_STRING_RESPONSE_REGISTER = 1501 # String Output register for responses
MODBUS_MEASUREMENT_REGISTER = 6       # Output block word 6 (32-bit float starts here)
MODBUS_UNIT_ID = 1                    # Modbus unit/slave ID

# Native command to trigger acquisition
COGNEX_TRIGGER_COMMAND = "MT"         # Manual Trigger (same as Telnet command)
# =============================================


@dataclass
class MeasurementPoint:
    """Single measurement point in polar coordinates."""
    theta_deg: float
    radius_inches: float
    timestamp: float
    timing: dict = None  # Timing diagnostics


@dataclass
class CircleFitResult:
    """Results from circle fitting."""
    center_x: float
    center_y: float
    diameter: float
    residual_rms: float
    max_residual: float
    r_squared: float
    units: str = "inches"


def open_connection():
    """Open Zaber connection (Ethernet or Serial)."""
    if USE_ETHERNET:
        return Connection.open_tcp_ip(ETH_HOST, ETH_PORT)
    else:
        return Connection.open_serial_port(PORT)


# ======= COGNEX MODBUS TCP ROUTINES =======

class CognexModbusConnection:
    """Persistent Cognex Modbus TCP connection manager."""
    
    def __init__(self, host: str = COGNEX_HOST, port: int = COGNEX_MODBUS_PORT,
                 unit_id: int = MODBUS_UNIT_ID):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.client = None
        self._connected = False
        self._use_slave_param = None  # Will be determined on first call
        
    def connect(self):
        """Establish Modbus TCP connection."""
        if self._connected:
            return
        
        t0 = time.time()
        self.client = ModbusTcpClient(self.host, port=self.port)
        self.client.connect()
        
        if not self.client.is_socket_open():
            raise RuntimeError(f"Failed to connect to Cognex at {self.host}:{self.port}")
        
        connect_time = time.time() - t0
        self._connected = True
        print(f"[Cognex] Connected via Modbus TCP to {self.host}:{self.port} ({connect_time:.3f}s)")
    
    def disconnect(self):
        """Close Modbus TCP connection."""
        if self._connected and self.client:
            self.client.close()
            self._connected = False
            print("[Cognex] Disconnected")
    
    def trigger_and_read(self, measurement_register: int = MODBUS_MEASUREMENT_REGISTER,
                        string_register: int = MODBUS_STRING_COMMAND_REGISTER,
                        trigger_command: str = COGNEX_TRIGGER_COMMAND,
                        timeout: float = 2.0) -> tuple[float, dict]:
        """
        Trigger measurement and read result via Modbus TCP.
        
        Cognex IL38 specifics:
        - Trigger: Send native command "SW8" via string registers
        - Measurement: 32-bit float at output word 6 (registers 6-7)
        
        Returns: (measurement_in_inches, timing_dict)
        """
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")
        
        timing = {}
        
        # Step 1: Send trigger command via string registers
        t0 = time.time()
        try:
            # Add carriage return and line feed to command (like Telnet)
            full_command = trigger_command + "\r\n"
            command_bytes = full_command.encode('ascii')
            
            # Convert to list of 16-bit integers (big-endian)
            registers = []
            for i in range(0, len(command_bytes), 2):
                if i+1 < len(command_bytes):
                    reg_value = (command_bytes[i] << 8) | command_bytes[i+1]
                else:
                    # Odd length - pad with null
                    reg_value = (command_bytes[i] << 8) | 0x00
                registers.append(reg_value)
            
            print(f"  [DEBUG] Sending trigger command '{trigger_command}' (with \\r\\n) to register {string_register}")
            print(f"  [DEBUG] Register values: {[f'{r:04X}h' for r in registers]}")
            
            # Write the command to string registers
            result = self.client.write_registers(address=string_register, values=registers)
            if result.isError():
                raise ModbusException(f"Failed to write trigger command: {result}")
            
            print(f"  [DEBUG] Trigger command sent successfully")
            
        except Exception as e:
            raise RuntimeError(f"Trigger command failed: {e}")
        timing['trigger_write'] = time.time() - t0
        
        # Step 2: Check response register for acknowledgment (optional debug)
        try:
            time.sleep(0.05)
            response = self.client.read_holding_registers(address=MODBUS_STRING_RESPONSE_REGISTER, count=10)
            if not response.isError():
                # Decode response string
                response_bytes = b''
                for reg in response.registers:
                    response_bytes += bytes([(reg >> 8) & 0xFF, reg & 0xFF])
                response_str = response_bytes.decode('ascii', errors='ignore').rstrip('\x00')
                print(f"  [DEBUG] Cognex response: '{response_str}'")
        except Exception as e:
            print(f"  [DEBUG] Could not read response: {e}")
        
        # Step 3: Wait for acquisition to complete
        time.sleep(0.1)  # Give it time to trigger and acquire
        
        # Step 3: Poll for valid data
        t0 = time.time()
        start_wait = time.time()
        measurement_value = None
        
        while (time.time() - start_wait) < timeout:
            try:
                # Read 2 consecutive holding registers (word 6 and 7 = 32-bit float)
                result = self.client.read_holding_registers(address=measurement_register, count=2)
                    
                if result.isError():
                    time.sleep(0.01)
                    continue
                
                # Debug: print raw register values
                high_word = result.registers[0]
                low_word = result.registers[1]
                print(f"  [DEBUG] Raw registers: Word6={high_word:04X}h, Word7={low_word:04X}h")
                
                # Combine two 16-bit registers into 32-bit float
                # Word 6 = high 16 bits, Word 7 = low 16 bits
                raw_value = (high_word << 16) | low_word
                
                # Convert to IEEE 754 32-bit float
                import struct
                measurement_value = struct.unpack('!f', struct.pack('!I', raw_value))[0]
                
                print(f"  [DEBUG] Parsed float value: {measurement_value}")
                
                # Check if value is valid (not NaN or out of reasonable range)
                if not (np.isnan(measurement_value) or abs(measurement_value) > 1000):
                    break
                    
            except Exception as e:
                print(f"  [WARN] Read error: {e}")
            
            time.sleep(0.01)  # Poll every 10ms
        
        timing['wait_and_read'] = time.time() - t0
        
        if measurement_value is None or np.isnan(measurement_value):
            raise RuntimeError(f"Failed to read valid measurement after {timeout}s")
        
        return measurement_value, timing
    
    def read_direct(self, register: int, num_registers: int = 2) -> float:
        """
        Directly read a register value without triggering.
        Useful for debugging or reading pre-computed values.
        """
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")
        
        try:
            result = self.client.read_holding_registers(register, num_registers, unit=self.unit_id)
            if result.isError():
                raise ModbusException(f"Failed to read register {register}: {result}")
            
            # Combine registers into float
            raw_value = (result.registers[0] << 16) | result.registers[1]
            import struct
            value = struct.unpack('!f', struct.pack('!I', raw_value))[0]
            return value
        except Exception as e:
            raise RuntimeError(f"Direct read failed: {e}")


# ======= INDEXING AND MEASUREMENT =======

def index_scan_with_measurement(axis, cognex_conn: CognexModbusConnection,
                                step_deg: float, total_deg: float, 
                                speed_deg_s: float, accel_deg_s2: float, 
                                decel_deg_s2: float, dwell_s: float = 0.0) -> list[MeasurementPoint]:
    """
    Perform indexed rotation with Cognex measurement after each step.
    Uses persistent Modbus TCP connection.
    Returns list of MeasurementPoint objects.
    """
    measurements = []
    timing_summary = {
        'move_times': [],
        'cognex_times': [],
        'cognex_breakdown': []
    }
    
    # Wait until axis is idle before starting
    axis.wait_until_idle()
    
    print(f"[Scan] Motion parameters: {speed_deg_s}°/s, accel: {accel_deg_s2}°/s², decel: {decel_deg_s2}°/s²")

    # Build absolute position target list
    base_pos = axis.get_position(Units.ANGLE_DEGREES)
    direction = 1.0 if total_deg >= 0 else -1.0
    step_mag = abs(float(step_deg))
    total_mag = abs(float(total_deg))

    if step_mag <= 0:
        print("[ERROR] step_deg must be > 0")
        return measurements

    # Calculate number of steps
    num_steps = int(total_mag / step_mag)
    
    # Build target positions (excluding the final position that equals start)
    targets = [base_pos + direction * (i * step_mag) for i in range(1, num_steps)]

    # Initial measurement at starting position
    print(f"\n[Scan] Initial measurement at {base_pos:.3f}°")
    t_cognex_start = time.time()
    try:
        initial_radius, timing_dict = cognex_conn.trigger_and_read()
        t_cognex = time.time() - t_cognex_start
        
        measurements.append(MeasurementPoint(base_pos, initial_radius, time.time(), timing_dict))
        print(f"  → Radius: {initial_radius:.4f} inches")
        print(f"  → Cognex total time: {t_cognex:.3f}s")
        _print_timing_breakdown(timing_dict)
        timing_summary['cognex_times'].append(t_cognex)
        timing_summary['cognex_breakdown'].append(timing_dict)
    except Exception as e:
        print(f"  → Failed to read initial measurement: {e}")

    # Execute moves and measurements
    for i, tgt in enumerate(targets, start=1):
        print(f"\n[Scan] Step {i}/{len(targets)}: Moving to {tgt:.3f}°")
        
        t_move_start = time.time()
        axis.move_absolute(
            tgt, 
            unit=Units.ANGLE_DEGREES, 
            wait_until_idle=True, 
            velocity=speed_deg_s, 
            velocity_unit=Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND, 
            acceleration=accel_deg_s2, 
            acceleration_unit=Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED
        )
        t_move = time.time() - t_move_start
        timing_summary['move_times'].append(t_move)
        print(f"  → Move completed in {t_move:.3f}s")
        
        if dwell_s > 0:
            time.sleep(dwell_s)
        
        # Measure at this position
        current_pos = axis.get_position(Units.ANGLE_DEGREES)
        
        t_cognex_start = time.time()
        try:
            radius, timing_dict = cognex_conn.trigger_and_read()
            t_cognex = time.time() - t_cognex_start
            
            measurements.append(MeasurementPoint(current_pos, radius, time.time(), timing_dict))
            print(f"  → Position: {current_pos:.3f}°, Radius: {radius:.4f} inches")
            print(f"  → Cognex total time: {t_cognex:.3f}s")
            _print_timing_breakdown(timing_dict)
            timing_summary['cognex_times'].append(t_cognex)
            timing_summary['cognex_breakdown'].append(timing_dict)
        except Exception as e:
            print(f"  → Failed to read measurement at {current_pos:.3f}°: {e}")

    # Return to starting position (complete the rotation without measuring)
    final_position = base_pos + direction * total_mag
    print(f"\n[Scan] Returning to start position: {final_position:.3f}°")
    t_return_start = time.time()
    axis.move_absolute(
        final_position, 
        unit=Units.ANGLE_DEGREES, 
        wait_until_idle=True, 
        velocity=speed_deg_s, 
        velocity_unit=Units.ANGULAR_VELOCITY_DEGREES_PER_SECOND, 
        acceleration=accel_deg_s2, 
        acceleration_unit=Units.ANGULAR_ACCELERATION_DEGREES_PER_SECOND_SQUARED
    )
    t_return = time.time() - t_return_start
    print(f"  → Return completed in {t_return:.3f}s (no measurement)")
    timing_summary['move_times'].append(t_return)

    # Print timing summary
    _print_timing_summary(timing_summary)
    
    print(f"\n[Scan] Complete: {len(measurements)} valid measurements collected")
    return measurements


def _print_timing_breakdown(timing: dict):
    """Print detailed timing breakdown for a single Cognex read."""
    if not timing:
        return
    parts = []
    if 'trigger_write' in timing:
        parts.append(f"Trigger: {timing['trigger_write']*1000:.1f}ms")
    if 'wait_and_read' in timing:
        parts.append(f"Wait+Read: {timing['wait_and_read']*1000:.1f}ms")
    if parts:
        print(f"     {' | '.join(parts)}")


def _print_timing_summary(summary: dict):
    """Print overall timing statistics."""
    print("\n" + "="*60)
    print("TIMING SUMMARY")
    print("="*60)
    
    if summary['move_times']:
        move_times = np.array(summary['move_times'])
        print(f"Zaber Move Times:")
        print(f"  Average: {move_times.mean():.3f}s | "
              f"Min: {move_times.min():.3f}s | "
              f"Max: {move_times.max():.3f}s")
    
    if summary['cognex_times']:
        cognex_times = np.array(summary['cognex_times'])
        print(f"\nCognex Total Read Times:")
        print(f"  Average: {cognex_times.mean():.3f}s ({cognex_times.mean()*1000:.1f}ms) | "
              f"Min: {cognex_times.min():.3f}s ({cognex_times.min()*1000:.1f}ms) | "
              f"Max: {cognex_times.max():.3f}s ({cognex_times.max()*1000:.1f}ms)")
        
        # Breakdown averages
        if summary['cognex_breakdown']:
            avg_trigger = np.mean([d.get('trigger_write', 0) for d in summary['cognex_breakdown']])
            avg_wait_read = np.mean([d.get('wait_and_read', 0) for d in summary['cognex_breakdown']])
            
            print(f"\nCognex Average Breakdown:")
            print(f"  Trigger Write:  {avg_trigger*1000:.1f}ms ({100*avg_trigger/cognex_times.mean():.1f}%)")
            print(f"  Wait+Read:      {avg_wait_read*1000:.1f}ms ({100*avg_wait_read/cognex_times.mean():.1f}%)")
    
    print("="*60)


# ======= CIRCLE FITTING AND ANALYSIS =======

def fit_circle_to_polar_data(measurements: list[MeasurementPoint]) -> CircleFitResult:
    """Fit a circle to polar coordinate measurements using least squares."""
    if len(measurements) < 3:
        raise ValueError("Need at least 3 measurements to fit a circle")

    # Convert polar to Cartesian
    thetas = np.array([m.theta_deg for m in measurements])
    radii = np.array([m.radius_inches for m in measurements])
    
    theta_rad = np.deg2rad(thetas)
    x = radii * np.cos(theta_rad)
    y = radii * np.sin(theta_rad)

    def calc_residuals(params):
        xc, yc = params
        return np.sqrt((x - xc)**2 + (y - yc)**2)
    
    x_mean, y_mean = x.mean(), y.mean()
    
    result = least_squares(
        lambda p: calc_residuals(p) - calc_residuals(p).mean(),
        [x_mean, y_mean],
        method='lm'
    )
    
    xc, yc = result.x
    distances = calc_residuals([xc, yc])
    radius_fit = distances.mean()
    diameter = 2 * radius_fit
    
    residuals = distances - radius_fit
    residual_rms = np.sqrt(np.mean(residuals**2))
    max_residual = np.max(np.abs(residuals))
    
    ss_res = np.sum(residuals**2)
    ss_tot = np.sum((distances - distances.mean())**2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    
    return CircleFitResult(
        center_x=xc,
        center_y=yc,
        diameter=diameter,
        residual_rms=residual_rms,
        max_residual=max_residual,
        r_squared=r_squared,
        units="inches"
    )


def plot_measurements_and_fit(measurements: list[MeasurementPoint], 
                              fit_result: CircleFitResult,
                              save_path: str = "circle_fit_results.png",
                              show_plot: bool = False):
    """Create visualization of measurements and fitted circle."""
    thetas = np.array([m.theta_deg for m in measurements])
    radii = np.array([m.radius_inches for m in measurements])
    theta_rad = np.deg2rad(thetas)
    x = radii * np.cos(theta_rad)
    y = radii * np.sin(theta_rad)
    
    circle_theta = np.linspace(0, 2*np.pi, 100)
    circle_r = fit_result.diameter / 2
    circle_x = fit_result.center_x + circle_r * np.cos(circle_theta)
    circle_y = fit_result.center_y + circle_r * np.sin(circle_theta)
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    
    ax1.scatter(x, y, c=thetas, cmap='viridis', s=50, alpha=0.7, 
                edgecolors='black', linewidth=0.5, label='Measurements')
    ax1.plot(circle_x, circle_y, 'r-', linewidth=2, label='Fitted Circle')
    ax1.plot(fit_result.center_x, fit_result.center_y, 'r+', 
             markersize=15, markeredgewidth=2, label='Center')
    ax1.set_xlabel('X (inches)', fontsize=11)
    ax1.set_ylabel('Y (inches)', fontsize=11)
    ax1.set_title('Circle Fit (Cartesian View)', fontsize=12, fontweight='bold')
    ax1.axis('equal')
    ax1.grid(True, alpha=0.3)
    ax1.legend()
    
    sm = plt.cm.ScalarMappable(cmap='viridis', 
                               norm=plt.Normalize(vmin=thetas.min(), vmax=thetas.max()))
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax1)
    cbar.set_label('Angle (degrees)', fontsize=10)
    
    ax2_polar = plt.subplot(122, projection='polar')
    ax2_polar.scatter(theta_rad, radii, c=thetas, cmap='viridis', s=50, 
                      alpha=0.7, edgecolors='black', linewidth=0.5)
    ax2_polar.set_title('Measurements (Polar View)', fontsize=12, 
                        fontweight='bold', pad=20)
    ax2_polar.set_theta_zero_location('E')
    ax2_polar.set_theta_direction(1)
    
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    print(f"\n[Plot] Saved to {save_path}")
    
    if show_plot:
        plt.show()
    else:
        plt.close(fig)


def print_results(measurements: list[MeasurementPoint], fit_result: CircleFitResult):
    """Print analysis results in formatted output."""
    print("\n" + "="*60)
    print("CIRCLE FIT RESULTS")
    print("="*60)
    print(f"Number of measurements:  {len(measurements)}")
    print(f"\nFitted Circle Properties:")
    print(f"  Center (X, Y):         ({fit_result.center_x:.4f}, {fit_result.center_y:.4f}) inches")
    print(f"  Diameter:              {fit_result.diameter:.4f} inches")
    print(f"  Radius:                {fit_result.diameter/2:.4f} inches")
    print(f"\nFit Quality Metrics:")
    print(f"  RMS Residual:          {fit_result.residual_rms:.4f} inches")
    print(f"  Max Residual:          {fit_result.max_residual:.4f} inches")
    print(f"  R² (coefficient):      {fit_result.r_squared:.6f}")
    print(f"  Relative RMS Error:    {100*fit_result.residual_rms/(fit_result.diameter/2):.3f}%")
    print("="*60 + "\n")


# ======= MAIN ROUTINE =======

def main():
    """Main execution routine."""
    if ModbusTcpClient is None:
        print("\n[ERROR] pymodbus is not installed. Install with: pip install pymodbus")
        return
        
    print("\n" + "="*60)
    print("ZABER INDEXING SCAN WITH COGNEX (MODBUS TCP)")
    print("="*60)
    
    # Get user input for scan parameters
    print("\n[Setup] Enter scan parameters:")
    
    # Get step size
    while True:
        try:
            step_deg_input = input("  Step size (degrees) [default: 10.0]: ").strip()
            if step_deg_input == "":
                step_deg = INDEX_STEP_DEG
            else:
                step_deg = float(step_deg_input)
                if step_deg <= 0:
                    print("    Error: Step size must be positive. Try again.")
                    continue
            break
        except ValueError:
            print("    Error: Please enter a valid number. Try again.")
    
    # Get number of rotations
    while True:
        try:
            rotations_input = input("  Number of complete rotations (integer) [default: 1]: ").strip()
            if rotations_input == "":
                num_rotations = 1
            else:
                num_rotations = int(rotations_input)
                if num_rotations <= 0:
                    print("    Error: Number of rotations must be positive. Try again.")
                    continue
            break
        except ValueError:
            print("    Error: Please enter a valid integer. Try again.")
    
    total_deg = num_rotations * 360.0
    
    print(f"\n[Setup] Configuration:")
    print(f"  Step size: {step_deg}°")
    print(f"  Rotations: {num_rotations} ({total_deg}° total)")
    print(f"  Expected measurements: {int(total_deg / step_deg)}")
    
    # Create Cognex connection
    cognex_conn = CognexModbusConnection()
    
    try:
        # Connect to Cognex
        cognex_conn.connect()
        
        # Open Zaber connection
        with open_connection() as connection:
            print(f"\n[Zaber] Opened {'Ethernet' if USE_ETHERNET else 'Serial'} connection")

            # Get device and identify
            dev = connection.get_device(DEVICE_ADDRESS)
            try:
                print(f"[Zaber] Identifying device at address {DEVICE_ADDRESS}...")
                dev.identify()
            except (DeviceDbFailedException, DeviceNotIdentifiedException) as e:
                print(f"[Zaber] ERROR: {e}")
                return
            except Exception as e:
                print(f"[Zaber] Unexpected error: {e}")
                return

            # Get axis and home
            axis = dev.get_axis(AXIS_NUMBER)
            print(f"[Zaber] Homing axis {AXIS_NUMBER}...")
            axis.home()
            axis.wait_until_idle()
            print("[Zaber] Homing complete")

            # Perform indexed scan with measurements
            print(f"\n[Scan] Starting indexed scan:")
            print(f"  Step size:      {step_deg}°")
            print(f"  Total rotation: {total_deg}°")
            print(f"  Speed:          {SPEED_DEG_S}°/s")
            print(f"  Acceleration:   {ACCEL_DEG_S2}°/s²")
            print(f"  Deceleration:   {DECEL_DEG_S2}°/s²")
            print(f"  Dwell time:     {DWELL_S}s")
            
            measurements = index_scan_with_measurement(
                axis,
                cognex_conn,
                step_deg, 
                total_deg, 
                SPEED_DEG_S,
                ACCEL_DEG_S2,
                DECEL_DEG_S2,
                DWELL_S
            )

            if len(measurements) < 3:
                print("\n[ERROR] Insufficient valid measurements for circle fitting")
                return

            # Perform circle fit
            print("\n[Analysis] Performing circle fit...")
            try:
                fit_result = fit_circle_to_polar_data(measurements)
                
                # Display results
                print_results(measurements, fit_result)
                
                # Create plots
                plot_measurements_and_fit(measurements, fit_result, show_plot=False)
                
            except Exception as e:
                print(f"[ERROR] Circle fitting failed: {e}")
                return
    
    finally:
        # Disconnect from Cognex
        cognex_conn.disconnect()

    print("[Complete] Program finished successfully\n")


if __name__ == "__main__":
    main()