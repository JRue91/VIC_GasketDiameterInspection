from __future__ import annotations

"""
Shared configuration, Zaber setup, and Cognex telnet connection logic.
Used by DiameterScan.py, CalibrationScan.py, and CalibrationVerify.py.
"""

import os
import time
import asyncio
from dataclasses import dataclass
from zaber_motion.ascii import Connection
from zaber_motion import Units, Library, DeviceDbSourceType

# ======= ZABER DATABASE =======
DB_DIR = r"C:/Zaber Devices Database"
for db_path in [os.path.join(DB_DIR, "devices-public.sqlite")]:
    if os.path.isfile(db_path):
        try:
            Library.set_device_db_source(DeviceDbSourceType.FILE, db_path)
            break
        except:
            pass

# ======= CONFIG =======
USE_ETHERNET = False
PORT = "COM4"
DEVICE_ADDRESS = 1
AXIS_NUMBER = 1

SPEED_DEG_S = 30.0
ACCEL_DEG_S2 = 40.0
DWELL_S = .25

COGNEX_HOST = "192.168.0.150"
COGNEX_PORT = 23
COGNEX_USER = "admin"
COGNEX_PASS = ""
COGNEX_MAX_RETRIES = 2

try:
    import telnetlib3
except:
    telnetlib3 = None


@dataclass
class MeasurementPoint:
    theta_deg: float
    value: float
    timestamp: float
    attempts: int = 0


class CognexConnection:
    def __init__(self):
        self.reader = None
        self.writer = None
        self._connected = False

    async def connect(self):
        if self._connected:
            return
        self.reader, self.writer = await telnetlib3.open_connection(
            COGNEX_HOST, COGNEX_PORT, encoding='ascii'
        )
        print(f"[Cognex] Connected")

        await self._drain(1.0)
        self.writer.write(f"{COGNEX_USER}\r\n")
        await self.writer.drain()
        await asyncio.sleep(0.05)
        self.writer.write(f"{COGNEX_PASS}\r\n")
        await self.writer.drain()
        await self._drain(1.0)

        self._connected = True
        print(f"[Cognex] Logged in")

    async def disconnect(self):
        if self._connected and self.writer:
            try:
                self.writer.write("LOGOUT\r\n")
                await self.writer.drain()
            except:
                pass
            self.writer.close()
            await self.writer.wait_closed()
        self._connected = False
        print("[Cognex] Disconnected")

    async def trigger(self):
        """Send trigger and wait for acknowledgment."""
        t_start = time.time()
        print(f"      -> Sending MT command...")
        self.writer.write("MT\r\n")
        await self.writer.drain()
        t_sent = time.time()
        print(f"      -> MT sent ({(t_sent-t_start)*1000:.1f}ms)")

        print(f"      -> Waiting for acknowledgment...")
        deadline = asyncio.get_event_loop().time() + 5.0

        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
                if line:
                    txt = line.strip()
                    print(f"      -> Cognex: '{txt}'")
                    if txt in ('1', '0', '-1') or 'OK' in txt.upper():
                        t_ack = time.time()
                        print(f"      -> ACKNOWLEDGED ({(t_ack-t_start)*1000:.1f}ms total)")
                        break
            except asyncio.TimeoutError:
                continue

        await asyncio.sleep(0.05)

    async def read_once(self, cell):
        """Read a cell value once."""
        t_start = time.time()
        print(f"      -> Reading {cell}...")

        self.writer.write(f"GV{cell}\r\n")
        await self.writer.drain()

        timeout = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < timeout:
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
                if not line:
                    continue
                txt = line.strip()
                if not txt or txt[0] in 'WUPLOGTS>':
                    continue

                val = self._extract_float(txt)
                if val is not None:
                    t_done = time.time()
                    print(f"      -> Value: {val:.4f} ({(t_done-t_start)*1000:.1f}ms)")
                    return val, 1
            except asyncio.TimeoutError:
                continue

        raise RuntimeError("No value received")

    async def trigger_and_read(self, cell):
        """Trigger and read with retries on failure."""
        for attempt in range(1, COGNEX_MAX_RETRIES + 1):
            try:
                await self.trigger()
                await asyncio.sleep(0.05)
                val, read_attempts = await self.read_once(cell)
                return val, read_attempts, attempt
            except RuntimeError:
                print(f"      ! Attempt {attempt}/{COGNEX_MAX_RETRIES} failed -- no value received")
                if attempt < COGNEX_MAX_RETRIES:
                    print(f"      -> Retrying trigger + read...")
                    await asyncio.sleep(0.5)
        raise RuntimeError(f"No value received after {COGNEX_MAX_RETRIES} attempts")

    async def _drain(self, timeout):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.wait_for(self.reader.readline(), timeout=0.1)
            except:
                break

    @staticmethod
    def _extract_float(txt):
        buf = []
        for ch in txt:
            if ch.isdigit() or ch in '+-.eE':
                buf.append(ch)
            elif buf and '.' in ''.join(buf):
                try:
                    return float(''.join(buf))
                except:
                    pass
                buf = []
        if buf and '.' in ''.join(buf):
            try:
                return float(''.join(buf))
            except:
                pass
        return None


def open_zaber_connection():
    """Open and return a Zaber serial connection."""
    return Connection.open_serial_port(PORT)


def setup_zaber_axis(zaber_conn):
    """Get device, identify, home axis, and return the axis object."""
    print(f"\n[Zaber] Connected")
    dev = zaber_conn.get_device(DEVICE_ADDRESS)
    dev.identify()
    axis = dev.get_axis(AXIS_NUMBER)
    print("[Zaber] Homing...")
    axis.home()
    axis.wait_until_idle()
    print("[Zaber] Ready")
    return axis
