"""State-matrix characterization: measured power and diode current across all
four (laser ON/OFF) x (shutter OPEN/CLOSED) combinations. Direct synchronous
queries only -- no Monitor, no background thread, no Dash -- so this is
immune to any GUI-side caching bug and shows exactly what the device itself
reports. Uses the same approved scope as hardware_gui.py (laser enable +
shutter, full emission approved 2026-10-01). Run with:

python check_power_current_matrix.py

Pauses at every state. At each pause, record (a) what this script printed,
(b) the front panel's own displayed power and current, and (c) your external
power meter reading, all at the same moment. Compare afterward -- do not
assume any single reading is correct without that cross-check.

On any unexpected reply or exception: STOP. Do not rerun to retry -- a failed
session is permanently unusable. Follow the abort procedure.
"""

import json

from coherent_verdi import VerdiController

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200

# Same approved ceiling as hardware_gui.py's current stage.
POWER_LIMIT_W = 0.05
TEST_POWER_W = 0.05

ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"


def read_state(laser: VerdiController, label: str) -> None:
    state = {
        "state": label,
        "laser_state (0=standby,1=on,2=fault)": laser.read_laser_state(),
        "shutter_open": bool(laser.read("?S")),
        "power_w": laser.read_power_w(),
        "set_power_w": laser.read("?SP"),
        "diode_current_a": laser.read("?D1C"),
    }
    print("\n" + json.dumps(state, indent=2, allow_nan=False))


def checkpoint(label: str) -> None:
    input(
        f"\n>>> Record the front panel's displayed power/current AND your "
        f"external power meter reading now, for: {label}\n"
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

    # Drive to a known baseline first, regardless of whatever state it's
    # currently in from earlier manual testing.
    if pre["shutter_open"]:
        laser.set_shutter(open=False)
    if pre["laser_state"] != 0:
        laser.stop()
    laser.set_power_w(TEST_POWER_W)

    read_state(laser, "1: OFF, shutter CLOSED (baseline)")
    checkpoint("OFF, shutter CLOSED")

    laser.set_shutter(open=True)
    read_state(laser, "2: OFF, shutter OPEN")
    checkpoint("OFF, shutter OPEN")

    laser.set_shutter(open=False)
    status = laser.status()
    if status["faults"]:
        raise SystemExit(f"ABORT: active faults reported: {status['faults']}. Stop.")
    laser.start()
    read_state(laser, "3: ON, shutter CLOSED (manual: idles at minimum power)")
    checkpoint("ON, shutter CLOSED")

    print("\nAbout to OPEN the shutter -- actual emission. Confirm beam path")
    print("and power meter are ready per your approved scope before continuing.")
    input("Press Enter to open the shutter, Ctrl+C to stop...")
    laser.set_shutter(open=True)
    read_state(laser, "4: ON, shutter OPEN (actual emission)")
    checkpoint("ON, shutter OPEN -- this is the state that matters most")

    # Return to the safe resting state.
    laser.set_shutter(open=False)
    laser.stop()
    read_state(laser, "5: back to OFF, shutter CLOSED (safe resting state)")

    print("\nDone. Compare all 5 printed states against your independently")
    print("recorded front-panel and power-meter readings.")
