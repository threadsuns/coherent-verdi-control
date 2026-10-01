"""Isolated shutter-toggle test, laser OFF the entire time. Checks whether the
state-2 anomaly from check_power_current_matrix.py (laser OFF, shutter OPEN:
terminal diode_current_a read 14.12 A but the front panel showed 2.69 A) is a
reproducible real effect or a one-off front-panel refresh lag -- by repeating
the open/close transition several times and comparing each time.

Never calls start()/L=1 -- the laser cannot turn on in this script, by
construction, same as hardware_write_test.py. The physical keyswitch does NOT
need to be ON for this: if a shutter command is rejected, that itself tells
us the keyswitch gates shutter operation too (useful to know) -- turn the key
to ON and rerun if that happens.

Run with: python check_shutter_toggle.py

Pauses at each step. Record the front panel's displayed current at each pause
alongside what this script prints.
"""

import json

from coherent_verdi import VerdiController

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"

# Never used here (no set_power_w() calls) -- pinned explicitly as
# defense-in-depth rather than relying on the constructor's own default,
# which is the full model-rated power if left unset.
POWER_LIMIT_W = 0.05

ROUNDS = 4


def read_state(laser: VerdiController, label: str) -> None:
    state = {
        "state": label,
        "laser_state (0=standby,1=on,2=fault)": laser.read_laser_state(),
        "shutter_open": bool(laser.read("?S")),
        "diode_current_a": laser.read("?D1C"),
    }
    print("\n" + json.dumps(state, indent=2, allow_nan=False))


def checkpoint(label: str) -> None:
    input(
        f"\n>>> Record the front panel's displayed current now, for: {label}\n"
        "Press Enter once recorded to continue, Ctrl+C to stop..."
    )


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
        raise SystemExit(
            "ABORT: laser is not in STANDBY. This script only tests the shutter "
            "with the laser off -- stop it first (hardware_gui.py), then rerun."
        )

    if pre["shutter_open"]:
        laser.set_shutter(open=False)
    read_state(laser, "baseline: OFF, shutter CLOSED")
    checkpoint("baseline: OFF, shutter CLOSED")

    for i in range(1, ROUNDS + 1):
        laser.set_shutter(open=True)
        read_state(laser, f"round {i}: OFF, shutter OPEN")
        checkpoint(f"round {i}: OFF, shutter OPEN")

        laser.set_shutter(open=False)
        read_state(laser, f"round {i}: OFF, shutter CLOSED")
        checkpoint(f"round {i}: OFF, shutter CLOSED")

    print("\nDone. If diode_current_a (terminal) or the front panel's displayed")
    print("current moved with shutter position consistently across all rounds,")
    print("that's a real, reproducible effect. If the earlier 2.69 A reading")
    print("doesn't recur here, that points to a one-off front-panel refresh lag")
    print("rather than real behavior tied to shutter position.")
