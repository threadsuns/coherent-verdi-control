"""Stage C (narrow): power setpoint + shutter write test. Laser stays in STANDBY
the whole time -- this script never calls start()/L=1, by construction, not just
by discipline. Run with:

python hardware_write_test.py

Each step pauses for you to confirm against the front panel before continuing.
On any unexpected reply or exception: STOP. Do not rerun this script to retry --
a failed session is permanently unusable (no auto-reconnect by design). Follow
the site abort procedure and involve the operator.
"""

import json

from coherent_verdi import VerdiController

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200

# Both the setpoint under test and the hard ceiling: the driver will refuse any
# setpoint above this value, so this number IS the safety bound for the session.
TEST_POWER_W = 0.01

# Verified via check_fault_clear_reply.py and confirmed against the front panel
# with the operator on 2026-09-30.
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"


def show(label: str, status: dict) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(status, indent=2, allow_nan=False))


with VerdiController(
    PORT,
    model=MODEL,
    baudrate=BAUDRATE,
    allow_writes=True,
    power_limit_w=TEST_POWER_W,
    active_fault_clear_reply=ACTIVE_FAULT_CLEAR_REPLY,
) as laser:
    # Step 1: confirm a clean starting state before any write.
    before = laser.status()
    show("Status before any write", before)
    if before["laser_state"] != 0:
        raise SystemExit("ABORT: laser_state is not STANDBY (0). Stop; involve the operator.")
    if before["faults"]:
        raise SystemExit(f"ABORT: active faults reported: {before['faults']}. Stop.")
    if before["shutter_open"]:
        raise SystemExit("ABORT: shutter is already open. Stop; confirm physical state first.")
    input(
        f"Laser is in STANDBY, no faults, shutter closed. About to write setpoint "
        f"{TEST_POWER_W} W (laser stays in STANDBY). Press Enter to continue, "
        "Ctrl+C to stop..."
    )

    # Step 2: setpoint write. No optical effect expected -- laser is not enabled.
    laser.set_power_w(TEST_POWER_W)
    after_setpoint = laser.status()
    show("Status after setpoint write", after_setpoint)
    if after_setpoint["set_power_w"] != TEST_POWER_W:
        raise SystemExit(
            f"ABORT: readback {after_setpoint['set_power_w']} != requested {TEST_POWER_W}."
        )
    if after_setpoint["laser_state"] != 0:
        raise SystemExit("ABORT: laser_state changed unexpectedly. Stop; involve the operator.")

    input("Setpoint confirmed. About to OPEN the shutter. Press Enter to continue, Ctrl+C to stop...")

    # Step 3: shutter open. Confirm readback; laser remains in STANDBY throughout.
    laser.set_shutter(open=True)
    opened = laser.status()
    show("Status after shutter OPEN", opened)
    if not opened["shutter_open"]:
        raise SystemExit("ABORT: shutter did not report open. Stop; involve the operator.")
    if opened["laser_state"] != 0:
        raise SystemExit("ABORT: laser_state changed unexpectedly. Stop; involve the operator.")

    input("Compare physical shutter position to the OPEN reading. Press Enter to close it...")

    # Step 4: shutter close, returning to the safe resting state.
    laser.set_shutter(open=False)
    closed = laser.status()
    show("Status after shutter CLOSE", closed)
    if closed["shutter_open"]:
        raise SystemExit("ABORT: shutter did not report closed. Stop; involve the operator.")

    print("\nTest complete. Laser remained in STANDBY throughout; shutter is closed.")
