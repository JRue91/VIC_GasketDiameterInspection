"""Exercise the online-trigger / job-gate logic against a fake In-Sight.

No hardware: a stub reader/writer speaks just enough Native Mode (SO0/SO1, GF,
SW8, MT, GV) to drive CognexConnection through its states.

Run:  python test_cognex_trigger.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import CognexConnection


class FakeSensor:
    """Minimal In-Sight Native Mode responder."""

    def __init__(self, job="gasket_14in.job", online=True, sw8_status="1",
                 preamble=None, value="7.0123"):
        self.job = job
        self.online = online
        self.sw8_status = sw8_status
        self.value = value
        # Lines the sensor emits before it answers the next command, used to
        # simulate stale buffered output from an earlier exchange.
        self.preamble = list(preamble or [])
        self.out = []          # queued response lines
        self.sent = []         # commands the code under test issued

    def handle(self, cmd):
        self.sent.append(cmd)
        if self.preamble:
            self.out.extend(self.preamble)
            self.preamble = []
        if cmd == "SO1":
            self.online = True
            self.out.append("1")
        elif cmd == "SO0":
            self.online = False
            self.out.append("1")
        elif cmd == "GF":
            self.out.append("1")
            if self.job:
                self.out.append(self.job)
        elif cmd == "SW8":
            # In-Sight refuses the soft event when the sensor is Offline.
            self.out.append(self.sw8_status if self.online else "-1")
        elif cmd == "MT":
            self.out.append("1")
        elif cmd.startswith("GV"):
            if self.value is not None:
                self.out.append(self.value)
        else:
            self.out.append("0")


class FakeWriter:
    def __init__(self, sensor):
        self.sensor = sensor
        self._buf = ""

    def write(self, data):
        self._buf += data
        while "\r\n" in self._buf:
            line, self._buf = self._buf.split("\r\n", 1)
            if line:
                self.sensor.handle(line)

    async def drain(self):
        return


class FakeReader:
    def __init__(self, sensor):
        self.sensor = sensor

    async def readline(self):
        if self.sensor.out:
            return self.sensor.out.pop(0) + "\r\n"
        # Nothing pending: behave like a quiet socket so wait_for() times out.
        await asyncio.sleep(10)
        return ""


def make_conn(sensor):
    c = CognexConnection()
    c.reader = FakeReader(sensor)
    c.writer = FakeWriter(sensor)
    c._connected = True
    return c


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    note = f"  -- {detail}" if detail and not cond else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{note}")


async def main():
    # Keep the timeout paths quick; production default is 5s.
    common.COGNEX_TRIGGER_TIMEOUT_S = 0.5
    common.COGNEX_LOG_RAW = False

    print("\n== online mode: job loaded, sensor left Online ==")
    common.COGNEX_TRIGGER_MODE = "online"
    common.COGNEX_REQUIRE_JOB = True
    s = FakeSensor(online=False)                 # starts offline
    job = await make_conn(s).prepare_for_scan()
    check("returns loaded job name", job == "gasket_14in.job", repr(job))
    check("issued SO1 (went Online)", "SO1" in s.sent, s.sent)
    check("never issued SO0", "SO0" not in s.sent, s.sent)
    check("sensor is Online after prep", s.online is True)

    print("\n== online mode: SW8 triggers and reads while Online ==")
    s = FakeSensor(online=True)
    c = make_conn(s)
    await c.prepare_for_scan()
    val, _reads, _tries = await c.trigger_and_read("B21")
    check("SW8 sent (not MT)", "SW8" in s.sent and "MT" not in s.sent, s.sent)
    check("value read back", abs(val - 7.0123) < 1e-6, val)
    check("still Online after measuring", s.online is True)

    print("\n== a stale status line does not kill the trigger ==")
    # An old '-1' left in the buffer must not fail a trigger whose real ack
    # arrives right behind it -- this is what made scans die instantly.
    s = FakeSensor(online=True, preamble=["-1"])
    c = make_conn(s)
    val, _r, _t = await c.trigger_and_read("B21")
    check("recovers from stale status", abs(val - 7.0123) < 1e-6, val)

    print("\n== unrecognised command (status 0) is called out ==")
    s = FakeSensor(online=True, sw8_status="0")
    c = make_conn(s)
    try:
        await c._trigger_online()
        check("status 0 names the wrong-command case", False, "no exception")
    except RuntimeError as e:
        check("status 0 names the wrong-command case",
              "not recognised" in str(e) and "COGNEX_ONLINE_TRIGGER" in str(e), str(e))

    print("\n== refused command (status -1) is called out ==")
    s = FakeSensor(online=True, sw8_status="-1")
    c = make_conn(s)
    try:
        await c._trigger_online()
        check("status -1 names the job-trigger case", False, "no exception")
    except RuntimeError as e:
        check("status -1 names the job-trigger case",
              "refused" in str(e) and "Manual or Network" in str(e), str(e))

    print("\n== silent sensor reports the timeout ==")
    s = FakeSensor(online=True, sw8_status=None)
    s.handle = lambda cmd: s.sent.append(cmd)     # answer nothing at all
    c = make_conn(s)
    try:
        await c._trigger_online()
        check("timeout raises", False, "no exception")
    except RuntimeError as e:
        check("timeout raises", "No acknowledgment" in str(e), str(e))

    print("\n== the real cause survives the retry wrapper ==")
    s = FakeSensor(online=True, sw8_status="0")
    c = make_conn(s)
    try:
        await c.trigger_and_read("B21")
        check("final error carries the cause", False, "no exception")
    except RuntimeError as e:
        check("final error carries the cause", "not recognised" in str(e), str(e))
        check("final error is chained", isinstance(e.__cause__, RuntimeError))

    print("\n== an empty cell read says which cell ==")
    s = FakeSensor(online=True, value=None)
    c = make_conn(s)
    try:
        await c.read_once("B21")
        check("read failure names the cell", False, "no exception")
    except RuntimeError as e:
        check("read failure names the cell", "GVB21" in str(e), str(e))

    print("\n== job gate: no job loaded blocks the scan ==")
    s = FakeSensor(job="")
    try:
        await make_conn(s).prepare_for_scan()
        check("blocks when no job", False, "no exception")
    except RuntimeError as e:
        check("blocks when no job", "No job is loaded" in str(e), str(e))

    print("\n== job gate: wrong job blocks the scan ==")
    s = FakeSensor(job="gasket_10in.job")
    try:
        await make_conn(s).prepare_for_scan("gasket_14in.job")
        check("blocks on job mismatch", False, "no exception")
    except RuntimeError as e:
        check("blocks on job mismatch", "was expected" in str(e), str(e))

    print("\n== job gate: matching job passes (extension-insensitive) ==")
    s = FakeSensor(job="gasket_14in")
    job = await make_conn(s).prepare_for_scan("gasket_14in.job")
    check("accepts stem match", job == "gasket_14in", repr(job))

    print("\n== job gate: can be disabled ==")
    common.COGNEX_REQUIRE_JOB = False
    s = FakeSensor(job="")
    job = await make_conn(s).prepare_for_scan()
    check("returns None when gate off", job is None, repr(job))
    common.COGNEX_REQUIRE_JOB = True

    print("\n== offline mode still works (regression) ==")
    common.COGNEX_TRIGGER_MODE = "offline"
    s = FakeSensor(online=True)
    c = make_conn(s)
    await c.prepare_for_scan()
    val, _r, _t = await c.trigger_and_read("B21")
    check("issued SO0 (went Offline)", "SO0" in s.sent, s.sent)
    check("MT sent (not SW8)", "MT" in s.sent and "SW8" not in s.sent, s.sent)
    check("value read back", abs(val - 7.0123) < 1e-6, val)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
