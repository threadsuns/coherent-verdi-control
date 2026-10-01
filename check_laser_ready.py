"""Pre-flight readiness check before using the Start button in hardware_gui.py.
Read-only, no writes. Run with:

python check_laser_ready.py

Per HARDWARE_VALIDATION.md TEST-010F: before enabling, confirm LBO warmup is
complete and the diode/LBO/etalon/vanadate servos are LOCKED, and capture fault
history first (start() resets faults/history, so this is your last chance to
see it). Servo codes below are decoded per manual Table 5-4 (p. 5-7):
0=OPEN, 1=LOCKED, 2=SEEKING, 3=FAULT (diode adds 4=OPTIMIZING, 5/6=CPEAKING).
Still independently compare this against the front panel with the operator --
this script's job is one point-in-time read, not the readiness decision.
"""

import json

from coherent_verdi import VerdiController

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"

SERVO_CODES = {
    0: "OPEN",
    1: "LOCKED",
    2: "SEEKING",
    3: "FAULT",
    4: "OPTIMIZING (diode only)",
    5: "CPEAKING (diode only, not V5 UNO/V6)",
    6: "CPEAKING2 (diode only, V5 UNO/V6 only)",
}


def servo(code: int) -> str:
    return f"{code} ({SERVO_CODES.get(code, 'undocumented code')})"


with VerdiController(
    PORT,
    model=MODEL,
    baudrate=BAUDRATE,
    allow_writes=False,
    active_fault_clear_reply=ACTIVE_FAULT_CLEAR_REPLY,
) as laser:
    # Capture fault history FIRST: start() resets faults/history (Table 5-3),
    # so this is the last chance to see it before an enable.
    fault_history = laser.read_faults(history=True)

    status = laser.status()

    readiness = {
        "keyswitch_on": status["keyswitch_on"],
        "laser_state (0=standby,1=on,2=fault)": status["laser_state"],
        "shutter_open": status["shutter_open"],
        "active_faults": status["faults"],
        "fault_history_before_this_check": fault_history,
        "power_w": status["power_w"],
        "set_power_w": status["set_power_w"],
        "diode_servo": servo(laser.read("?D1SS")),
        "lbo_servo": servo(status["lbo_servo"]),
        "etalon_servo": servo(laser.read("?ESS")),
        "vanadate_servo": servo(laser.read("?VSS")),
    }
    print(json.dumps(readiness, indent=2, allow_nan=False))

    not_locked = [
        name
        for name in ("diode_servo", "lbo_servo", "etalon_servo", "vanadate_servo")
        if not readiness[name].startswith("1 ")
    ]
    print()
    if not_locked:
        print(f"NOT READY: {', '.join(not_locked)} not LOCKED. Do not enable yet.")
    elif readiness["active_faults"]:
        print("NOT READY: active faults reported. Do not enable.")
    elif readiness["shutter_open"]:
        print("NOT READY: shutter is open. Close it before enabling.")
    else:
        print("All four servos show LOCKED, no active faults, shutter closed.")
        print("Still confirm this against the front panel with the operator")
        print("before using Start -- this is one point-in-time read, not a")
        print("substitute for their sign-off.")
