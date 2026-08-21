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


async def try_trigger(conn, cmd, cell):
    """Fire one candidate and report reply + whether the cell moved."""
    before = await read_cell(conn, cell)
    reply = await collect(conn, cmd)
    after = await read_cell(conn, cell)

    status = reply[0] if reply else "(no reply)"
    changed = (before is not None and after is not None and before != after)

    print(f"  {cmd:<6} reply={reply!s:<20} "
          f"{cell}: {before} -> {after}"
          f"{'   <-- CELL CHANGED' if changed else ''}")
    return {"cmd": cmd, "status": status, "reply": reply, "changed": changed}


async def main(cell, host):
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
        print("      (a '0' reply just means this firmware has no such command)")

        print("\n[2] Sensor Online (SO1)")
        print(f"  SO1    -> {await collect(conn, 'SO1')}")

        baseline = await read_cell(conn, cell)
        print(f"  {cell} currently reads: {baseline}")
        if baseline is None:
            print(f"  ! {cell} is not readable. Check the cell address and that a "
                  f"job is loaded -- trigger results below will be inconclusive.")

        print("\n[3] Candidate triggers, sensor ONLINE")
        online_results = [await try_trigger(conn, c, cell) for c in CANDIDATES]

        print("\n[4] Baseline: sensor OFFLINE + MT (the known-good path)")
        print(f"  SO0    -> {await collect(conn, 'SO0')}")
        offline_mt = await try_trigger(conn, "MT", cell)

        print("\n  Restoring Online...")
        print(f"  SO1    -> {await collect(conn, 'SO1')}")
    finally:
        await conn.disconnect()

    # ---- verdict ----
    print("\n" + "=" * 72)
    print("RESULT")

    acked = [r for r in online_results if r["status"] == "1"]
    moved = [r for r in online_results if r["changed"]]

    if moved:
        best = moved[0]["cmd"]
        print(f"  Online triggering WORKS with: {best}")
        print(f"  Set common.COGNEX_ONLINE_TRIGGER = \"{best}\" (default is "
              f"{common.COGNEX_ONLINE_TRIGGER!r}).")
    elif acked:
        best = acked[0]["cmd"]
        print(f"  {best} was accepted (status 1) but the cell did not change.")
        print(f"  The command is valid; the job is most likely not acquiring on "
              f"it. Check the AcquireImage Trigger setting in the job.")
    else:
        codes = sorted({r["status"] for r in online_results})
        print(f"  No candidate triggered while Online. Status codes seen: {codes}")
        if offline_mt["changed"]:
            print("  Offline MT DID work, so the sensor and cell are fine -- the")
            print("  sensor simply will not soft-trigger in its current setup.")
            print("  Use Trigger Mode 'offline' until the job/access side is sorted.")
        else:
            print("  Offline MT did not work either, so this is not specific to")
            print("  online mode. Check the cell address and the loaded job first.")

        print("\n  Worth ruling out, in order:")
        print("   1. Job's AcquireImage Trigger set to Manual or Network?")
        print("      Continuous and External refuse soft triggers. This is the")
        print("      leading suspect whenever SO1 above succeeded, since that")
        print("      proves the session can already change sensor state.")
        print("   2. In-Sight Explorer / EasyBuilder connected to this sensor?")
        print("      It holds Full Access and leaves telnet read-only.")
        print("   3. Telnet user has Full Access rights on the sensor?")

    print("=" * 72 + "\n")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cell", default="B21", help="measurement cell to watch (default B21)")
    ap.add_argument("--host", default=None, help="override the Cognex IP")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.cell, args.host)))
