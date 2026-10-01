"""Rapid-fire re-read of diode current around BOTH shutter transitions (open
and close), key ON, laser OFF the entire time. Extends
check_current_transient.py, which only captured the open transition, to
check whether closing has the same ~0.5s settling lag, a different one, or
none at all.

Never calls start()/L=1 -- laser stays OFF throughout, by construction.

Run with: python check_current_transient_both.py

Requires the physical keyswitch to be ON (same precondition as the other
key-ON characterization scripts). Reads ?D1C repeatedly, as fast as the
RS-232 link allows, for a couple of seconds after EACH transition.
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

DURATION_S = 2.0
SAMPLE_COUNT = 20


def sample_after(laser: VerdiController, label: str, action) -> list[tuple[float, float]]:
    t0 = monotonic()
    action()
    acted_at = monotonic()
    print(f"\n{label} command completed at t=+{acted_at - t0:.3f}s")
    samples: list[tuple[float, float]] = []
    while monotonic() - acted_at < DURATION_S and len(samples) < SAMPLE_COUNT:
        t = monotonic() - acted_at
        current = laser.read("?D1C")
        samples.append((t, current))
        print(f"t=+{t:.3f}s  diode_current_a={current}")
    return samples


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
        laser.stop()  # Force standby rather than just checking.
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
        "Ready: key ON, laser OFF, shutter CLOSED, setpoint set. Let the "
        "reading settle, then press Enter to open the shutter and sample..."
    )
    open_samples = sample_after(laser, "shutter-open", lambda: laser.set_shutter(open=True))

    input(
        "\nShutter is open. Let the reading settle, then press Enter to "
        "close the shutter and sample the closing transition..."
    )
    close_samples = sample_after(laser, "shutter-close", lambda: laser.set_shutter(open=False))

    print("\n=== OPEN transition ===")
    print(
        json.dumps(
            [{"t_s": round(t, 3), "diode_current_a": c} for t, c in open_samples], indent=2
        )
    )
    print("\n=== CLOSE transition ===")
    print(
        json.dumps(
            [{"t_s": round(t, 3), "diode_current_a": c} for t, c in close_samples], indent=2
        )
    )
    print("\nCompare the step timing in both directions: same ~0.5s lag both ways,")
    print("asymmetric, or no lag on one side at all?")
