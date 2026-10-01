"""Rapid-fire re-read of diode current immediately after one shutter-open
event (key ON, laser OFF), to see whether the front panel's ~2.69 A reading
is a decaying transient (climbing back toward ~14 A over roughly a second)
or something this script's own sequential queries never catch at all.
Complements check_shutter_toggle_key_on.py, which tests whether the effect
recurs across repeated cycles rather than its time-shape within one event.

Never calls start()/L=1 -- laser stays OFF throughout, by construction.

Run with: python check_current_transient.py

Requires the physical keyswitch to be ON (same precondition as
check_power_current_matrix.py). Reads ?D1C repeatedly, as fast as the RS-232
link allows, for a few seconds right after opening the shutter. Also watch
the front panel yourself during this window and note roughly when/whether it
visibly changes -- this script can't see the panel, only the RS-232 side.
"""

import json
from time import monotonic

from coherent_verdi import VerdiController

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"
POWER_LIMIT_W = 0.05
TEST_POWER_W = 0.05

DURATION_S = 3.0
SAMPLE_COUNT = 20

with VerdiController(
    PORT,
    model=MODEL,
    baudrate=BAUDRATE,
    allow_writes=True,
    power_limit_w=POWER_LIMIT_W,
    active_fault_clear_reply=ACTIVE_FAULT_CLEAR_REPLY,
) as laser:
    pre = laser.status()
    if pre["faults"]:
        raise SystemExit(f"ABORT: active faults reported: {pre['faults']}. Stop.")
    if pre["laser_state"] != 0:
        laser.stop()  # Force standby rather than just checking -- see conversation notes.
    if not pre["keyswitch_on"]:
        raise SystemExit(
            "ABORT: this test replicates the key-ON precondition. "
            "Turn the physical keyswitch to ON and rerun."
        )

    if pre["shutter_open"]:
        laser.set_shutter(open=False)
    laser.set_power_w(TEST_POWER_W)

    confirmed = laser.read_laser_state()
    if confirmed != 0:
        raise SystemExit(
            f"ABORT: stop() was sent but laser_state still reads {confirmed}, "
            "not STANDBY. Do not proceed; involve the operator."
        )

    input(
        "Ready: key ON, laser OFF, shutter CLOSED, setpoint set. Watch the "
        "front panel's current display now. Press Enter to open the shutter "
        "and start rapid sampling, Ctrl+C to stop..."
    )

    started = monotonic()
    laser.set_shutter(open=True)
    opened_at = monotonic()
    print(f"Shutter-open command completed at t=+{opened_at - started:.3f}s\n")

    samples = []
    while monotonic() - opened_at < DURATION_S and len(samples) < SAMPLE_COUNT:
        t = monotonic() - opened_at
        current = laser.read("?D1C")
        samples.append((t, current))
        print(f"t=+{t:.3f}s  diode_current_a={current}")

    laser.set_shutter(open=False)

    print(f"\nShutter closed. Collected {len(samples)} samples.")
    print(json.dumps([{"t_s": round(t, 3), "diode_current_a": c} for t, c in samples], indent=2))
    print("\nIf these values climb from a low starting point toward ~14 A over")
    print("this window, that's a decaying transient our query sometimes caught")
    print("mid-decay. If they stay flat near 14 A throughout, this script's own")
    print("read timing never caught whatever the front panel is reacting to,")
    print("which would point toward something specific to the panel's own")
    print("display/sampling, not a transient visible over RS-232.")
