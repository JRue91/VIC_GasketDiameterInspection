from __future__ import annotations

"""
Shared configuration, Zaber setup, and Cognex telnet connection logic.
Used by DiameterScan.py, CalibrationScan.py, and CalibrationVerify.py.
"""

import os
import ssl
import time
import asyncio
import ftplib
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
COGNEX_MAX_RETRIES = 5
COGNEX_FTP_PORT = 21

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

    async def read_cell(self, cell):
        """Read a stored cell value (no MT trigger). Returns float."""
        val, _ = await self.read_once(cell)
        return val

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

    async def _command(self, cmd, timeout=5.0):
        """Send a raw Native Mode command and return the first response line.

        Used for job/file commands (LF/GF/SO0/SO1) whose replies are a status
        code or filename, not the numeric cell value that read_once expects.
        Skips blank lines, a bare prompt, and any echo of the command itself.
        """
        self.writer.write(f"{cmd}\r\n")
        await self.writer.drain()
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if not line:
                continue
            txt = line.strip()
            if not txt or txt == cmd or txt == '>':
                continue
            return txt
        return ""

    async def get_job(self):
        """Return the filename of the currently loaded job (Native Mode GF)."""
        resp = await self._command("GF")
        print(f"[Cognex] Current job: '{resp}'")
        return resp

    async def load_job(self, job_name, settle=1.5):
        """Load a Cognex job by filename via Native Mode.

        Sequence: SO0 (offline) -> LF<job> (load) -> SO1 (online) -> GF (verify).
        In-Sight file commands return '1' on success. Firmware differs on whether
        LF wants the extension, so this tries the name as given and then the
        extension-stripped stem (e.g. "part.jobx" then "part"). Returns True only
        if the loaded job verifies; leaves the sensor online on failure.
        """
        # Candidate LF arguments: full name first, then the bare stem.
        stem = job_name.rsplit(".", 1)[0] if "." in job_name else job_name
        candidates = [job_name]
        if stem != job_name:
            candidates.append(stem)

        print(f"[Cognex] Loading job '{job_name}'...")

        off = await self._command("SO0")
        if not off.startswith("1"):
            print(f"[Cognex] ! SO0 (offline) returned '{off}'")

        loaded = None
        for cand in candidates:
            resp = await self._command(f"LF{cand}")
            if resp.startswith("1"):
                loaded = cand
                print(f"[Cognex] LF accepted '{cand}'.")
                break
            print(f"[Cognex] ! LF returned '{resp}' for '{cand}'")

        if loaded is None:
            print(f"[Cognex] ! LF failed for all name variants of '{job_name}'")
            await self._command("SO1")  # restore online state before bailing
            return False

        # Give the job time to compile/initialize before going online.
        await asyncio.sleep(settle)

        on = await self._command("SO1")
        if not on.startswith("1"):
            print(f"[Cognex] ! SO1 (online) returned '{on}'")
        await asyncio.sleep(0.2)

        current = await self.get_job()
        # Verify against the bare stem so a with/without-extension GF reply matches.
        ok = bool(current) and stem.lower() in current.lower()
        if ok:
            print(f"[Cognex] Job '{job_name}' loaded and online.")
        else:
            print(f"[Cognex] ! Job verify mismatch: expected '{stem}', got '{current}'")
        return ok

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


class _ReuseTLS(ftplib.FTP_TLS):
    """FTP_TLS that reuses the control-channel TLS session on the data
    connection. Many embedded FTPS servers (incl. Cognex In-Sight) reject a
    data transfer whose TLS session was not resumed from the control channel.
    """

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session,
            )
        return conn, size


def list_jobs_ftp(host=None, user=None, password=None, port=None, timeout=10.0):
    """List *.job files on the Cognex In-Sight FTPS server.

    In-Sight requires explicit FTPS ("Non-anonymous sessions must use
    encryption"), so this negotiates AUTH TLS, secures the data channel, and
    reuses the control-channel TLS session for transfers. Certificate
    verification is disabled because the sensor ships a self-signed cert on a
    trusted local network. Reuses the Cognex host/credentials by default.
    Raises the underlying ftplib/socket/ssl error on failure so callers can
    surface a clear "could not reach sensor FTP" message.
    """
    host = host if host is not None else COGNEX_HOST
    user = user if user is not None else COGNEX_USER
    password = password if password is not None else COGNEX_PASS
    port = port if port is not None else COGNEX_FTP_PORT

    ctx = ssl._create_unverified_context()
    ftp = _ReuseTLS(context=ctx)
    ftp.connect(host, port, timeout=timeout)
    ftp.auth()                    # AUTH TLS on the control channel
    ftp.login(user, password)
    ftp.prot_p()                  # encrypt the data channel
    try:
        ftp.set_pasv(True)
        names = ftp.nlst()
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()

    return sorted(
        os.path.basename(n) for n in names
        if n.lower().endswith((".job", ".jobx"))
    )


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
