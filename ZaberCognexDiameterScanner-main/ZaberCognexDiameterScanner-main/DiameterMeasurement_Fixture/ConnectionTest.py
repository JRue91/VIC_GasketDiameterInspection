from __future__ import annotations

"""
Minimal Zaber device-detect example (works offline too)
- Fixes the undefined variable: you must OPEN a Connection first.
- Tries detect_devices(); if the Device Database is blocked, falls back to address 1.
- Set PORT (for USB/serial) or ETH_HOST/ETH_PORT (for Ethernet), then run.
"""

from zaber_motion.ascii import Connection
from zaber_motion.exceptions import DeviceDbFailedException

# ======= CONFIGURE YOUR LINK =======
USE_ETHERNET = False            # False = USB/Serial (COM port). True = Ethernet.
PORT = "COM4"                   # <-- set your COM port (e.g., COM3, COM4)
ETH_HOST = "192.168.0.50"       # <-- controller/stage IP if using Ethernet
ETH_PORT = 23                   # common Zaber ASCII/Telnet port (change if your controller uses another)
# ===================================


def open_connection():
    if USE_ETHERNET:
        return Connection.open_tcp_ip(ETH_HOST, ETH_PORT)
    else:
        return Connection.open_serial_port(PORT)


def main():
    # Make sure no other app (e.g., Zaber Launcher/Console) is holding the port.
    with open_connection() as Connection:
        print("Opened", "Ethernet" if USE_ETHERNET else "Serial", "Connection")
        try:
            devices = Connection.detect_devices()
            print(f"Found {len(devices)} device(s)")
            for dev in devices:
                # Avoid properties that require the online DB; address is always safe
                print(f"Homing all axes on device with address {dev.device_address}...")
                dev.all_axes.home()
        except DeviceDbFailedException as e:
            # If the online Device DB is blocked and no local DB is configured, detection can fail.
            # Fall back to the common case: single device at address 1
            print("Device DB unavailable; falling back to address 1. Details:", e)
            dev = Connection.get_device(1)
            print(f"Homing all axes on device with address {dev.device_address}...")
            dev.all_axes.home()


if __name__ == "__main__":
    main()