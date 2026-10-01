"""Stage D: full-emission dashboard. Run with:

python hardware_gui.py

Laser enable, shutter, and setpoint all work from the browser, up to
POWER_LIMIT_W. Approved 2026-10-01 by the operator for emission at this power
(see conversation/session notes) -- this is a materially bigger step than the
earlier write-only stage, since the laser can now actually produce light.

Before clicking Start: run check_laser_ready.py and independently compare its
servo/warmup readout against the front panel, per HARDWARE_VALIDATION.md
TEST-010F. The GUI's Start button itself refuses to fire only on the two
objectively-checkable preconditions (active faults, shutter open) -- it does
NOT judge LBO warmup or servo lock state for you.

Ctrl+C stops the server; it does not shut down the laser. Use Stop and Close
shutter from the GUI, or the site abort procedure, before disconnecting.

Diode current, measured power: characterized on real hardware 2026-10-01
(check_power_current_matrix.py, check_shutter_toggle.py,
check_shutter_toggle_key_on.py, check_current_transient.py). With the key in
STANDBY, diode current is flat (~2.6 A / 0 A) regardless of shutter position.
With the key ON, laser OFF (laser_state 0, confirmed via forced stop()):
diode current from the front panel and from RS-232 (?D1C) are consistently
INVERTED relative to shutter state -- panel reads ~14.75 A closed / 2.69 A
open; RS-232 reads ~2.46 A closed / ~14-14.8 A open -- reproduced 9/9 across
repeated toggles. RS-232's reading lags a shutter transition by roughly 0.5 s
(confirmed by rapid sampling: a clean step from 14.08 A to 2.46 A at
t=+0.43-0.52 s after shutter-open, not a gradual decay). Mechanism unconfirmed.
Measured power (?P) did not distinguish 0 mW from a confirmed 45.8 mW external
reading at this power level -- not a reliable emission indicator this low.
Across every one of these characterization runs, an external power meter
read 0 mW in every state except genuine laser-on + shutter-open emission --
diode current and measured power are not safety-relevant signals; use an
external power meter to confirm actual emission.

Half-waveplate (Thorlabs K10CR2, serial HWP_SERIAL): opened here, outside
nspyre. If it fails to connect, the laser GUI still runs and the HWP panel
shows NOT CONNECTED. It is never homed automatically; use the GUI's Home button.
"""

import logging
from threading import Event, Thread

from coherent_verdi import VerdiController
from coherent_verdi.gui import Monitor, Waveplate, create_app

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200
HTTP_PORT = 8050

HWP_SERIAL = "55543994"

# Approved ceiling for this stage. The driver refuses any setpoint above this
# value for the whole session, regardless of what's typed in the GUI.
POWER_LIMIT_W = 0.05

# Verified via check_fault_clear_reply.py and confirmed against the front panel
# with the operator on 2026-09-30. Do not change without repeating that check.
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"

logging.getLogger("werkzeug").setLevel(logging.ERROR)


def open_waveplate() -> Waveplate:
    """Never raises: a failed HWP connection must not take down the laser GUI."""
    try:
        from coherent_verdi.k10cr2_driver import K10CR2  # needs thorlabs_kinesis

        return Waveplate(K10CR2(HWP_SERIAL))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}".rstrip()
        print(f"WARNING: half-waveplate {HWP_SERIAL} not connected -- {error}")
        return Waveplate(None, connect_error=error)


with VerdiController(
    PORT,
    model=MODEL,
    baudrate=BAUDRATE,
    allow_writes=True,
    power_limit_w=POWER_LIMIT_W,
    active_fault_clear_reply=ACTIVE_FAULT_CLEAR_REPLY,
) as laser:
    monitor = Monitor(laser)
    waveplate = open_waveplate()
    app = create_app(monitor, waveplate)
    stop = Event()

    def poll() -> None:
        while not stop.is_set():
            monitor.poll_once()
            waveplate.poll_once()  # skipped while a move/home owns the stage
            stop.wait(monitor.interval_s)

    worker = Thread(target=poll, name="verdi-monitor")
    worker.start()
    try:
        app.run(host="127.0.0.1", port=HTTP_PORT, debug=False, use_reloader=False)
    finally:
        stop.set()
        worker.join()  # Drain acquisition before disconnect, including Ctrl+C.
        hwp_state = waveplate.snapshot()
        if hwp_state["connected"]:
            if hwp_state["busy"]:
                print(f"Waiting for half-waveplate ({hwp_state['busy']}) to finish...")
            waveplate.close()  # Waits for any in-flight move/home first.
            print(f"Half-waveplate {HWP_SERIAL} closed.")
