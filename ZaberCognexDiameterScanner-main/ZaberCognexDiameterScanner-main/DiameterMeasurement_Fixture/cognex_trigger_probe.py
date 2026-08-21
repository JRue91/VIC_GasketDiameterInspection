from __future__ import annotations

"""
Find out which Native Mode trigger this sensor actually accepts.

Talks only to the Cognex -- the Zaber stage is never touched, nothing is
written to the sensor's filesystem, and no job is loaded or saved. For each
candidate trigger it reports the raw reply and, more importantly, whether the
measurement cell *changed*, which is the only real proof that an acquisition
happened. A command can answer '1' and still not trigger anything.

Run from this folder:

    python cognex_trigger_probe.py            # uses common.py's host/cell
    python cognex_trigger_probe.py --cell F25
"""

import argparse
import asyncio
import sys

import common
from common import CognexConnection

# Candidate triggers, in the order worth trying. SW8 is In-Sight's soft event 8;
# the rest cover spelling and firmware variations seen on other In-Sight models.
CANDIDATES = ["SW8", "SW 8", "SE8", "SW08", "SW7", "MT"]

# Commands probed for context rather than as triggers.
CONTEXT = ["GF", "GO", "GS"]


async def collect(conn, cmd, wait=1.5):
    """Send one raw command and return every line the sensor sends back."""
    await conn._drain(0.3)
    conn.writer.write(f"{cmd}\r\n")
    await conn.writer.drain()

    lines = []
    deadline = asyncio.get_event_loop().time() + wait
    while asyncio.get_event_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(conn.reader.readline(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        if not line:
            continue
        txt = line.strip()
        if txt and txt != cmd and txt != ">":
            lines.append(txt)
    return lines


async def read_cell(conn, cell):
    """Read a cell, returning None instead of raising when it comes back empty."""
    try:
        return await conn.read_cell(cell)
    except RuntimeError:
        return None


async def try_trigger(conn, cmd, cell, counter_cell=None):
    """Fire one candidate and report the reply plus any cell movement.

    `cell` alone is weak evidence: a stationary part re-measures to the same
    value, so an unchanged cell does NOT mean the trigger did nothing. Pass
    `counter_cell` -- anything that increments or timestamps on every
    acquisition -- for actual proof that an acquisition occurred.
    """
    watch = [cell] + ([counter_cell] if counter_cell else [])
    before = [await read_cell(conn, c) for c in watch]
    reply = await collect(conn, cmd)
    after = [await read_cell(conn, c) for c in watch]

    status = reply[0] if reply else "(no reply)"
    moves = [b is not None and a is not None and b != a
             for b, a in zip(before, after)]

    shown = "  ".join(f"{c}: {b} -> {a}" for c, b, a in zip(watch, before, after))
    print(f"  {cmd:<6} reply={reply!s:<20} {shown}"
          f"{'   <-- CHANGED' if any(moves) else ''}")
    return {
        "cmd": cmd, "status": status, "reply": reply,
        "changed": moves[0],
        "counter_changed": moves[1] if counter_cell else None,
    }


async def main(cell, host, counter_cell):
    if host:
        common.COGNEX_HOST = host

    # Raw echo would double up everything the probe already prints.
    common.COGNEX_LOG_RAW = False

    print(f"\nCognex trigger probe -- {common.COGNEX_HOST}:{common.COGNEX_PORT}, cell {cell}")
    print("=" * 72)

    conn = CognexConnection()
    await conn.connect()
    try:
        print("\n[1] Context")
        for cmd in CONTEXT:
            print(f"  {cmd:<6} -> {await collect(conn, cmd)}")
        print("      (GO is Get Online: 1 = Online, 0 = Offline. For the others")
        print("       a '0' reply means this firmware has no such command.)")

        print("\n[2] Sensor Online (SO1)")
        so1 = await collect(conn, "SO1")
        online_ok = bool(so1) and so1[0] == "1"
        print(f"  SO1    -> {so1}"
              f"{'' if online_ok else '   <-- REFUSED, sensor stays Offline'}")

        baseline = await read_cell(conn, cell)
        print(f"  {cell} currently reads: {baseline}")
        if baseline is None:
            print(f"  ! {cell} is not readable. Check the cell address and that a "
                  f"job is loaded -- trigger results below will be inconclusive.")

        print("\n[3] Candidate triggers, sensor ONLINE")
        online_results = [await try_trigger(conn, c, cell, counter_cell)
                          for c in CANDIDATES]

        print("\n[4] Baseline: sensor OFFLINE + MT (the known-good path)")
        print(f"  SO0    -> {await collect(conn, 'SO0')}")
        offline_mt = await try_trigger(conn, "MT", cell, counter_cell)

        print("\n  Restoring Online...")
        print(f"  SO1    -> {await collect(conn, 'SO1')}")
    finally:
        await conn.disconnect()

    # ---- verdict ----
    print("\n" + "=" * 72)
    print("RESULT")

    acked = [r for r in online_results if r["status"] == "1"]
    if counter_cell:
        proved = [r for r in online_results if r["counter_changed"]]
        control_worked = bool(offline_mt["counter_changed"])
    else:
        proved = [r for r in online_results if r["changed"]]
        control_worked = bool(offline_mt["changed"])

    if not online_ok:
        print(f"  The sensor REFUSED to go Online: SO1 -> {so1}.")
        print("  Fix this first. A soft trigger cannot work while the sensor is")
        print("  Offline, so every trigger result above was taken in the wrong")
        print("  state and proves nothing about online triggering.")
        print("  Cognex documents that Set Online CANNOT bring the sensor")
        print("  Online if it was set Offline manually in In-Sight Explorer or")
        print("  by a Discrete Input. That latch is only clearable the same way.")
        print("\n  Do this: open In-Sight Explorer, set the sensor Online, close")
        print("  Explorer, then re-run this probe. If SO1 then answers 1 the")
        print("  latch was the problem and online triggering can be tested for")
        print("  real. Until then use Trigger Mode 'offline'.")
    elif proved:
        best = proved[0]["cmd"]
        print(f"  Online triggering WORKS with: {best}")
        print(f"  Set common.COGNEX_ONLINE_TRIGGER = \"{best}\" (default is "
              f"{common.COGNEX_ONLINE_TRIGGER!r}).")
    elif not control_worked:
        print("  INCONCLUSIVE -- the offline MT control did not register either,")
        print("  so this run cannot tell a dead trigger from a live one.")
        if not counter_cell:
            print(f"\n  {cell} is a measurement of a stationary part, so it reads the")
            print("  same value no matter how many times you trigger. Re-run with a")
            print("  cell that changes on every acquisition:")
            print("\n      python cognex_trigger_probe.py --counter-cell <cell>")
            print("\n  Any counter or timestamp in the job will do. Rotating the part")
            print("  between triggers works too.")
        else:
            print(f"\n  {counter_cell} did not move for any command, including the")
            print("  known-good offline MT. Check that it really is an acquisition")
            print("  counter, and that the job is running.")
    elif acked:
        primary = next((r for r in online_results
                        if r["cmd"] == common.COGNEX_ONLINE_TRIGGER), None)
        others = [r for r in acked if r is not primary]

        print("  No candidate acquired while Online, but the offline MT control")
        print("  did -- so the sensor and cell are fine and this is specific to")
        print("  triggering in Online mode.")

        if primary is not None and primary["status"] != "1":
            print(f"\n  {primary['cmd']} was REFUSED (status {primary['status']}).")
            if others:
                accepted = ", ".join(r["cmd"] for r in others)
                print(f"  {accepted} was accepted (status 1) but acquired nothing,")
                print("  which means the command family is understood by the")
                print(f"  sensor and {primary['cmd']} specifically is being rejected.")
                print("  Those other events are not acquisition triggers, so their")
                print("  status 1 is not a working trigger -- do not switch to one.")
            print("\n  That localises the fault to the job, not the protocol:")
            print("  soft event 8 is only accepted when the job's acquisition")
            print("  trigger source is a software trigger.")
            print("    In-Sight Vision Suite : Trigger Source = Software")
            print("                            (NOT Input Line)")
            print("    In-Sight Explorer     : Trigger = Manual or Network")
            print("                            (NOT Continuous/External/Camera)")
            print("\n  An Input Line / External source waits on a hardware edge that")
            print("  never arrives here, which also explains a counter that never")
            print("  advances on its own while Online.")
            print("\n  Fix in the job editor, save the job, then re-run this probe.")
        else:
            best = acked[0]["cmd"]
            print(f"\n  {best} was accepted (status 1) but acquired nothing.")
            print("  The command is valid and the job is not acquiring on it --")
            print("  check the AcquireImage Trigger setting in the job.")
    else:
        codes = sorted({r["status"] for r in online_results})
        print(f"  No candidate triggered while Online. Status codes seen: {codes}")
        print("  Offline MT did register, so the sensor and cell are fine and the")
        print("  sensor simply will not soft-trigger as configured.")
        print("  Use Trigger Mode 'offline' until the job side is sorted.")

    print("=" * 72 + "\n")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cell", default="B21", help="measurement cell to watch (default B21)")
    ap.add_argument("--host", default=None, help="override the Cognex IP")
    ap.add_argument("--counter-cell", default=None,
                    help="cell that increments or timestamps on every "
                         "acquisition; the only sound proof a trigger fired")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.cell, args.host, args.counter_cell)))
