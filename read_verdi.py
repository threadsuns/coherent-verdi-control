"""Stage 1 read-only hardware check (HARDWARE_VALIDATION.md). Run with:
python read_verdi.py

Sends no LASER/SHUTTER/POWER write. Compare every printed value against the
front panel before trusting it. Stop and involve the operator on any mismatch,
timeout, or unexpected reply -- do not retry blindly.
"""

import json

from coherent_verdi import VerdiController

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200

with VerdiController(PORT, model=MODEL, baudrate=BAUDRATE, allow_writes=False) as laser:
    # TEST-010B: one identity query first. Confirm this alone before reading further.
    print("Software version:", laser.read("?SV"))
    input("Compare to the front panel. Press Enter to continue, Ctrl+C to stop...")

    # TEST-010C: state/power/temperature reads. No ?F/?FH here -- that needs an
    # independently verified active_fault_clear_reply first (see README step 6).
    reads = {
        "laser_state (0=standby,1=on,2=fault)": laser.read_laser_state(),
        "keyswitch_on": laser.read("?K"),
        "shutter_open": laser.read("?S"),
        "power_w": laser.read_power_w(),
        "set_power_w": laser.read("?SP"),
        "lbo_temp_c": laser.read("?LBOT"),
        "baseplate_temp_c": laser.read("?BT"),
        "etalon_temp_c": laser.read("?ET"),
        "vanadate_temp_c": laser.read("?VT"),
    }
    print(json.dumps(reads, indent=2, allow_nan=False))
