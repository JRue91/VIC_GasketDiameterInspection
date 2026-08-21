"""Exercise the new online-trigger / job-gate logic against a fake In-Sight.

No hardware: a stub reader/writer speaks just enough Native Mode (SO0/SO1, GF,
SW8, MT, GV) to drive CognexConnection through its states.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import CognexConnection


class FakeSensor:
    """Minimal In-Sight Native Mode responder."""

    def __init__(self, job="gasket_14in.job", online=True, sw8_status="1"):
        self.job = job
        self.online = online
        self.sw8_status = sw8_status
        self.out = []          # queued response lines
        self.sent = []         # commands the code under test issued

    def handle(self, cmd):
        self.sent.append(cmd)
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
            # In-Sight rejects the soft event when the sensor is Offline.
            self.out.append(self.sw8_status if self.online else "-1")
        elif cmd == "MT":
            self.out.append("1")
        elif cmd.startswith("GV"):
            self.out.append("7.0123")
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
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' -- ' + detail) if detail and not cond else ''}")


async def main():
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

    print("\n== online mode: rejected trigger surfaces an error ==")
    s = FakeSensor(online=True, sw8_status="-1")
    c = make_conn(s)
    await c.prepare_for_scan()
    try:
        await c._trigger_online()
        check("raises on negative status", False, "no exception")
    except RuntimeError as e:
        check("raises on negative status", "rejected" in str(e), str(e))

    print("\n== job gate: no job loaded blocks the scan ==")
    common.COGNEX_TRIGGER_MODE = "online"
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
