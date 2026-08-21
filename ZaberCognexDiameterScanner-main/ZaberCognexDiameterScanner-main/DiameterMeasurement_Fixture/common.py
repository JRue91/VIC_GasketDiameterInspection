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

# Trigger mode. In-Sight only accepts the "MT" manual trigger while the sensor
# is Offline, which takes it out of its normal run state for the whole scan.
# "online" instead leaves the sensor Online (SO1) and fires the soft-event
# trigger SW8, so the sensor keeps running its job the way it does in
# production. "offline" preserves the original SO0 + MT behaviour.
COGNEX_TRIGGER_MODE = "online"      # "online" (SO1 + SW8) or "offline" (SO0 + MT)
COGNEX_ONLINE_TRIGGER = "SW8"       # soft event used in online mode
COGNEX_OFFLINE_TRIGGER = "MT"       # manual trigger used in offline mode

# How long to wait for a trigger acknowledgment before giving up (seconds).
COGNEX_TRIGGER_TIMEOUT_S = 5.0

# Echo every raw line the sensor sends during trigger/read. The Native Mode
# reply that matters is often a bare status code that the parsers skip, so this
# is the only way to see what the sensor actually said when a trigger fails.
COGNEX_LOG_RAW = True

# Refuse to start a scan unless the sensor reports a loaded job. A sensor with
# no job returns garbage or nothing for the measurement cells, so this turns a
# confusing mid-scan failure into a clear pre-flight error.
COGNEX_REQUIRE_JOB = True

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
        self._last_online_reply = ""   # raw reply to the most recent SO0/SO1
        self._state_uncertain = ""     # set when SO0/SO1 was refused

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
        """Fire one acquisition using whichever trigger the mode calls for.

        Online mode sends the SW8 soft event, which In-Sight accepts only while
        the sensor is Online and the job's acquisition trigger is set to Manual
        or Network. It answers '1' on success and a negative status code on
        failure, so this path is strict: anything else raises RuntimeError and
        lets trigger_and_read() retry. Offline mode keeps the original lenient
        MT handling, whose reply varies by firmware.
        """
        if COGNEX_TRIGGER_MODE == "online":
            await self._trigger_online()
        else:
            await self._trigger_offline()

    async def _trigger_online(self):
        """SW8 soft-event trigger; the sensor stays Online.

        Waits up to COGNEX_TRIGGER_TIMEOUT_S for the '1' success status. A
        non-'1' status does NOT fail the trigger straight away: it can be a
        stale reply left in the buffer by an earlier command, so it is recorded
        and the read window stays open in case the real ack follows. Only when
        the window closes without a '1' does this raise, reporting the last bad
        status and every line the sensor sent, which is what you need to tell
        "command not recognised" (0) from "command refused" (-1) apart.
        """
        t_start = time.time()
        cmd = COGNEX_ONLINE_TRIGGER
        self.writer.write(f"{cmd}\r\n")
        await self.writer.drain()

        seen = []
        bad_status = None
        deadline = asyncio.get_event_loop().time() + COGNEX_TRIGGER_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if not line:
                continue
            txt = line.strip()
            if COGNEX_LOG_RAW:
                print(f"      -> [raw] {txt!r}")
            seen.append(txt)
            if not txt or txt == cmd or txt == '>':
                continue
            if not self._is_status_code(txt):
                # Output the running job pushed on its own -- not our reply.
                continue
            if txt == '1':
                print(f"      -> {cmd} ack ({(time.time()-t_start)*1000:.1f}ms)")
                await asyncio.sleep(0.05)
                return
            bad_status = txt

        waited = time.time() - t_start
        detail = f" Sensor sent: {seen}." if seen else " Sensor sent nothing."
        if self._state_uncertain:
            detail += f" Note: {self._state_uncertain}."
        if bad_status == "0":
            raise RuntimeError(
                f"'{cmd}' was not recognised by the sensor (status 0) after "
                f"{waited:.1f}s. This firmware may use a different soft-trigger "
                f"command -- set common.COGNEX_ONLINE_TRIGGER, or switch "
                f"Trigger Mode to 'offline' to go back to SO0 + MT.{detail}"
            )
        if bad_status is not None:
            # Deliberately does not claim to know what this status means. Only
            # 0 (unrecognised command) is reliably documented across In-Sight
            # firmware; the rest vary, so list the causes worth ruling out and
            # point at the probe instead of asserting one.
            raise RuntimeError(
                f"{cmd} was refused by the sensor (status {bad_status}) after "
                f"{waited:.1f}s.{detail} Causes worth ruling out: In-Sight "
                f"Explorer is connected and holding Full Access; the job's "
                f"AcquireImage Trigger is not Manual/Network; the telnet user "
                f"lacks Full Access. Run cognex_trigger_probe.py to find which "
                f"trigger this sensor accepts, or set Trigger Mode to 'offline' "
                f"to fall back to SO0 + MT."
            )
        raise RuntimeError(
            f"No acknowledgment for {cmd} within {COGNEX_TRIGGER_TIMEOUT_S:.1f}s."
            f"{detail}"
        )

    async def _trigger_offline(self):
        """MT manual trigger; requires the sensor to be Offline."""
        t_start = time.time()
        cmd = COGNEX_OFFLINE_TRIGGER
        print(f"      -> Sending {cmd} command...")
        self.writer.write(f"{cmd}\r\n")
        await self.writer.drain()
        t_sent = time.time()
        print(f"      -> {cmd} sent ({(t_sent-t_start)*1000:.1f}ms)")

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
        seen = []
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
                if COGNEX_LOG_RAW and txt:
                    print(f"      -> [raw] {txt!r}")
                    seen.append(txt)
                if not txt or txt[0] in 'WUPLOGTS>':
                    continue

                val = self._extract_float(txt)
                if val is not None:
                    t_done = time.time()
                    print(f"      -> Value: {val:.4f} ({(t_done-t_start)*1000:.1f}ms)")
                    return val, 1
            except asyncio.TimeoutError:
                continue

        detail = f" Sensor sent: {seen}." if seen else " Sensor sent nothing."
        raise RuntimeError(f"No value received from GV{cell}.{detail}")

    async def read_cell(self, cell):
        """Read a stored cell value (no MT trigger). Returns float."""
        val, _ = await self.read_once(cell)
        return val

    async def trigger_and_read(self, cell):
        """Trigger and read with retries on failure.

        Reports the underlying error on every attempt and carries the last one
        into the final exception. A bare "no value received" hides whether the
        trigger was refused or the cell read came back empty, which are very
        different faults.
        """
        last_error = None
        for attempt in range(1, COGNEX_MAX_RETRIES + 1):
            try:
                await self.trigger()
                await asyncio.sleep(0.05)
                val, read_attempts = await self.read_once(cell)
                return val, read_attempts, attempt
            except RuntimeError as e:
                last_error = e
                print(f"      ! Attempt {attempt}/{COGNEX_MAX_RETRIES} failed: {e}")
                if attempt < COGNEX_MAX_RETRIES:
                    print(f"      -> Retrying trigger + read...")
                    await asyncio.sleep(0.5)
        raise RuntimeError(
            f"Failed after {COGNEX_MAX_RETRIES} attempts. Last error: {last_error}"
        ) from last_error

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

    @staticmethod
    def _is_status_code(txt):
        """True if txt is a bare Native Mode status code (e.g. '1', '0', '-1')."""
        return txt.lstrip("-").isdigit()

    async def get_job(self):
        """Return the filename of the currently loaded job (Native Mode GF).

        GF replies with a status-code line ('1' on success) FOLLOWED by the
        filename on the next line, so skip the leading status code and return
        the first non-status line as the name.
        """
        self.writer.write("GF\r\n")
        await self.writer.drain()

        name, status = "", None
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=0.1)
            except asyncio.TimeoutError:
                if status is not None:  # got status, name isn't coming
                    break
                continue
            if not line:
                continue
            txt = line.strip()
            if not txt or txt == "GF" or txt == '>':
                continue
            if self._is_status_code(txt):
                # First status code is the GF result; any later status code
                # (e.g. '-5' emitted mid-load) is NOT a filename -- skip it.
                if status is None:
                    status = txt
                continue
            name = txt
            break

        print(f"[Cognex] Current job: '{name}' (status {status})")
        return name

    async def load_job(self, job_name, settle=1.0, verify_timeout=12.0):
        """Load a Cognex job by filename via Native Mode.

        Sequence: SO0 (offline) -> LF<job> (load) -> poll GF until the job
        confirms -> SO1 (online). In-Sight file commands return '1' on success.
        Firmware differs on whether LF wants the extension, so this tries the
        name as given and then the extension-stripped stem (e.g. "part.jobx"
        then "part").

        Loading a job makes the sensor emit delayed output, so rather than
        trusting one exactly-timed GF, confirm by polling GF (draining stale
        chatter between reads) until it reports the loaded job or the timeout
        elapses. Returns True only if the loaded job verifies.
        """
        # Candidate LF arguments: full name first, then the bare stem.
        stem = job_name.rsplit(".", 1)[0] if "." in job_name else job_name
        candidates = [job_name]
        if stem != job_name:
            candidates.append(stem)

        print(f"[Cognex] Loading job '{job_name}'...")

        await self._drain(0.3)  # clear any pending output before we start
        off = await self._command("SO0")
        if not off.startswith("1"):
            print(f"[Cognex] ! SO0 (offline) returned '{off}'")

        loaded = None
        for cand in candidates:
            await self._drain(0.2)
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

        # Poll GF until the job finishes loading and confirms (the sensor may
        # return error/empty while still compiling a large job).
        await asyncio.sleep(settle)
        ok = False
        deadline = asyncio.get_event_loop().time() + verify_timeout
        while asyncio.get_event_loop().time() < deadline:
            await self._drain(0.3)  # flush load-time chatter before the query
            current = await self.get_job()
            if current and stem.lower() in current.lower():
                ok = True
                break
            await asyncio.sleep(0.5)

        # Bring the sensor back online (best-effort, retried; drain first so we
        # read SO1's own reply and not leftover load output).
        for _ in range(3):
            await self._drain(0.2)
            on = await self._command("SO1")
            if on.startswith("1"):
                break
            print(f"[Cognex] ! SO1 (online) returned '{on}', retrying...")
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.2)

        if ok:
            print(f"[Cognex] Job '{job_name}' loaded and online.")
        else:
            print(f"[Cognex] ! Job load not confirmed within {verify_timeout:.0f}s "
                  f"(expected '{stem}').")
        return ok

    async def set_online(self, online=True, retries=3):
        """Put the sensor Online (SO1) or Offline (SO0).

        Retried because the sensor can be busy right after a job load and
        answer with an error status. Returns True once the command is acked.
        """
        want = 1 if online else 0
        label = "Online" if online else "Offline"
        for _ in range(retries):
            await self._drain(0.2)
            resp = await self._command(f"SO{want}")
            self._last_online_reply = resp
            if resp.startswith("1"):
                print(f"[Cognex] Sensor {label}")
                return True
            print(f"[Cognex] ! SO{want} returned '{resp}', retrying...")
            await asyncio.sleep(0.3)
        return False

    async def get_online(self):
        """Return True if the sensor is Online, False if Offline, None if unknown.

        Native Mode 'GO' (Get Online) answers '1' Online and '0' Offline. Note
        the ambiguity: '0' is also the generic "unrecognised command" reply, so
        on firmware without GO this reports Offline rather than unknown. Treat
        a False as "probably Offline" and let the trigger confirm.
        """
        await self._drain(0.2)
        resp = await self._command("GO")
        if resp == "1":
            return True
        if resp == "0":
            return False
        return None

    async def ensure_job_loaded(self, expected_job=None):
        """Confirm a job is loaded, raising RuntimeError if not.

        With no job the measurement cells hold stale or empty values, so a scan
        would silently produce garbage. Returns the loaded job's filename. When
        `expected_job` is given the loaded job must match it, compared on the
        extension-stripped stem because GF's spelling of the name varies.
        """
        await self._drain(0.3)  # don't mistake job output for the GF reply
        name = await self.get_job()
        if not name:
            raise RuntimeError(
                "No job is loaded on the Cognex. Load a job on the sensor, or "
                "select a recipe that specifies one, before running a scan."
            )
        if expected_job:
            stem = expected_job.rsplit(".", 1)[0] if "." in expected_job else expected_job
            if stem.lower() not in name.lower():
                raise RuntimeError(
                    f"Cognex has job '{name}' loaded, but '{expected_job}' was "
                    f"expected. Re-select the recipe to load the correct job."
                )
        return name

    async def prepare_for_scan(self, expected_job=None):
        """Put the sensor in the run state this trigger mode needs, then check
        that a job is loaded.

        Online mode leaves the sensor running its job (SO1) so measurements are
        taken under the same conditions as production; offline mode drops it to
        SO0 so the MT manual trigger is accepted. Returns the loaded job name,
        or None when COGNEX_REQUIRE_JOB is off.
        """
        online = COGNEX_TRIGGER_MODE == "online"
        print(f"[Cognex] Trigger mode: {COGNEX_TRIGGER_MODE} "
              f"({COGNEX_ONLINE_TRIGGER if online else COGNEX_OFFLINE_TRIGGER})")

        if not await self.set_online(online):
            # Not fatal. Native Mode has no "get online" query, so a refused
            # SO cannot be told apart from the sensor already being in the
            # state we want -- and In-Sight refuses Set Online outright when
            # the sensor was put Offline by hand in Explorer or by a Discrete
            # Input. Warn, record it, and let the trigger be the real test:
            # it fails loudly and specifically if the state is actually wrong.
            want = "Online" if online else "Offline"
            self._state_uncertain = (
                f"SO{1 if online else 0} was refused "
                f"('{self._last_online_reply}'), so the sensor may not be {want}"
            )
            print(f"[Cognex] ! Could not set {want}: "
                  f"SO{1 if online else 0} -> '{self._last_online_reply}'.")
            print(f"[Cognex]   Continuing -- the sensor may already be {want}. "
                  f"If it is not, the trigger below will say so.")
            print(f"[Cognex]   In-Sight refuses Set Online when the sensor was "
                  f"set Offline by hand in Explorer or by a Discrete Input; "
                  f"that latch clears only the same way.")
        else:
            self._state_uncertain = ""

        # SO's ack only says the command was accepted. GO reports the state
        # itself, so use it to catch a sensor that is genuinely in the wrong
        # one before the scan starts moving.
        actual = await self.get_online()
        if actual is not None and actual != online:
            is_now = "Online" if actual else "Offline"
            want = "Online" if online else "Offline"
            raise RuntimeError(
                f"The Cognex is {is_now} but this trigger mode needs it "
                f"{want} (GO -> {'1' if actual else '0'}). "
                f"In-Sight refuses Set Online when the sensor was set Offline "
                f"by hand in In-Sight Explorer or by a Discrete Input; that "
                f"latch clears only the same way. Set it {want} in Explorer, "
                f"or use Trigger Mode "
                f"'{'offline' if actual else 'online'}' to match how it is now."
            )
        if actual is not None:
            self._state_uncertain = ""

        if not COGNEX_REQUIRE_JOB:
            return None
        job = await self.ensure_job_loaded(expected_job)
        print(f"[Cognex] Job '{job}' is loaded -- ready to scan.")
        return job

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
