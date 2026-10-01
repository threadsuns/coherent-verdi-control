"""Isolated shutter-toggle test replicating check_power_current_matrix.py's
exact state-1-to-2 preconditions: key ON, a non-zero power setpoint already
configured, laser OFF the entire time. That combination reproducibly showed
diode_current_a drop to 2.69 A on the front panel when the shutter opened --
twice, same value both times. check_shutter_toggle.py tested a DIFFERENT
combination (key STANDBY, no setpoint configured) and found zero shutter
sensitivity; it did not actually rule this one out. This script isolates
which part of the combination matters by repeating the same open/close
transition several times under the matrix script's exact starting state.

Never calls start()/L=1 -- the laser stays OFF throughout, by construction.

Run with: python check_shutter_toggle_key_on.py

Requires the physical keyswitch to be ON (same as check_power_current_matrix.py).
Pauses at each step. Record the front panel's displayed current at each pause.
"""

import json

from coherent_verdi import VerdiController

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"
POWER_LIMIT_W = 0.05
TEST_POWER_W = 0.05  # matches check_power_current_matrix.py's setpoint

ROUNDS = 4


def read_state(laser: VerdiController, label: str) -> None:
    state = {
        "state": label,
        "laser_state (0=standby,1=on,2=fault)": laser.read_laser_state(),
        "shutter_open": bool(laser.read("?S")),
        "set_power_w": laser.read("?SP"),
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
        laser.stop()  # Force standby rather than just checking -- see conversation notes.
    if not pre["keyswitch_on"]:
        raise SystemExit(
            "ABORT: this test specifically replicates the key-ON precondition. "
            "Turn the physical keyswitch to ON and rerun."
        )

    if pre["shutter_open"]:
        laser.set_shutter(open=False)
    laser.set_power_w(TEST_POWER_W)  # matches the matrix script's precondition

    confirmed = laser.read_laser_state()
    if confirmed != 0:
        raise SystemExit(
            f"ABORT: stop() was sent but laser_state still reads {confirmed}, "
            "not STANDBY. Do not proceed; involve the operator."
        )

    read_state(laser, "baseline: key ON, laser OFF, shutter CLOSED, setpoint set")
    checkpoint("baseline")

    for i in range(1, ROUNDS + 1):
        laser.set_shutter(open=True)
        read_state(laser, f"round {i}: key ON, laser OFF, shutter OPEN")
        checkpoint(f"round {i}: shutter OPEN")

        laser.set_shutter(open=False)
        read_state(laser, f"round {i}: key ON, laser OFF, shutter CLOSED")
        checkpoint(f"round {i}: shutter CLOSED")

    print("\nDone. If ~2.69 A recurred on every OPEN step, this exact")
    print("precondition combination reliably reproduces the effect. If it only")
    print("appeared on round 1 and not later rounds, that points to a one-time")
    print("transient tied to the FIRST shutter-open after key-ON/setpoint,")
    print("rather than something true of 'shutter open' in general.")
