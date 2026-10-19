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

Beam-path flip mirror (Thorlabs MFF101, serial FLIPPER_SERIAL): opened here like
the HWP; close the Kinesis app first (only one process can hold the device). On
the Epifluorescence path, enclosure power (HWP transmission x head power) is
capped at EPI_LIMIT_W by EpiPowerInterlock (see coherent_verdi/interlock.py):
waveplate moves, setpoint increases and switching to Epi are refused if they
would exceed it, and if the mirror is found on Epi over the cap (its own button,
Kinesis, front panel) the Verdi shutter is closed automatically. The cap needs a
measured HWP calibration; with the ideal model Epi is refused. A mirror that is
unreadable or not connected is treated as Epi for waveplate/setpoint checks.
"""

import logging
from pathlib import Path
from threading import Event, Thread

from coherent_verdi import VerdiController
from coherent_verdi.gui import Monitor, Waveplate, create_app
from coherent_verdi.hwp_calibration import (
    EnclosurePowerModel,
    HwpCalibration,
    HwpPowerCalibration,
)
from coherent_verdi.interlock import BeamPath, EpiPowerInterlock

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200
HTTP_PORT = 8050

HWP_SERIAL = "55543994"

FLIPPER_SERIAL = "37008463"
# Kinesis position number for each path; verified on hardware by the operator
# 2026-10-02 (python -m coherent_verdi.mff101_driver: position 1 = Epifluorescence).
FLIPPER_POSITIONS = {"Epifluorescence": 1, "TIR": 2}
# Maximum power out of the laser enclosure on the Epifluorescence path. TIR is uncapped.
EPI_LIMIT_W = 0.200

# Per-setpoint HWP+PBS calibration from fit_hwp_calibration.py (transmission vs the
# laser setpoint, one theta0). Missing/invalid file: calculator off, Epi refused.
HWP_CALIBRATION_FILE = Path(__file__).with_name("hwp_calibration.json")

# Downstream of the PBS, inside the enclosure. Not yet measured: 1.0 (lossless)
# until confirmed. P_out = setpoint x T_HWP+PBS(angle; setpoint) x AOM x spatial filter.
AOM_TRANSMISSION = 1.0
SPATIAL_FILTER_TRANSMISSION = 1.0


def load_power_model() -> EnclosurePowerModel | None:
    """Never raises: a bad calibration file must not take down the laser GUI."""
    try:
        return EnclosurePowerModel(
            HwpPowerCalibration.load(HWP_CALIBRATION_FILE),
            aom_transmission=AOM_TRANSMISSION,
            spatial_filter_transmission=SPATIAL_FILTER_TRANSMISSION,
        )
    except Exception as exc:
        print(f"WARNING: no HWP calibration loaded from {HWP_CALIBRATION_FILE} -- "
              f"{type(exc).__name__}: {exc}")
        return None


POWER_MODEL = load_power_model()
# The Epi interlock uses the conservative envelope (largest A and B of any calibrated
# setpoint) and ignores AOM/spatial-filter losses, so it never under-predicts power.
HWP_CALIBRATION = POWER_MODEL.calibration.envelope() if POWER_MODEL else HwpCalibration()

# Approved ceiling for this stage. The driver refuses any setpoint above this
# value for the whole session, regardless of what's typed in the GUI.
POWER_LIMIT_W = 5 # Watts

# Verified via check_fault_clear_reply.py and confirmed against the front panel
# with the operator on 2026-09-30. Do not change without repeating that check.
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"

logging.getLogger("werkzeug").setLevel(logging.ERROR)


def open_waveplate() -> Waveplate:
    """Never raises: a failed HWP connection must not take down the laser GUI."""
    try:
        from coherent_verdi.k10cr2_driver import K10CR2  # needs thorlabs_kinesis

        stage = K10CR2(HWP_SERIAL, calibration=HWP_CALIBRATION)
        return Waveplate(stage, calibration=HWP_CALIBRATION)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}".rstrip()
        print(f"WARNING: half-waveplate {HWP_SERIAL} not connected -- {error}")
        return Waveplate(None, connect_error=error, calibration=HWP_CALIBRATION)


def open_beam_path() -> BeamPath:
    """Never raises: a failed flipper connection must not take down the laser GUI."""
    try:
        from coherent_verdi.mff101_driver import MFF101  # needs Kinesis installed

        return BeamPath(MFF101(FLIPPER_SERIAL), FLIPPER_POSITIONS)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}".rstrip()
        print(f"WARNING: flip mirror {FLIPPER_SERIAL} not connected -- {error}")
        return BeamPath(None, FLIPPER_POSITIONS, connect_error=error)


with VerdiController(
    PORT,
    model=MODEL,
    baudrate=BAUDRATE,
    allow_writes=True,
    power_limit_w=POWER_LIMIT_W,
    active_fault_clear_reply=ACTIVE_FAULT_CLEAR_REPLY,
) as laser:
    monitor = Monitor(laser)
    waveplate = open_waveplate()  # before the flipper: both share Kinesis DeviceManager
    beam_path = open_beam_path()
    interlock = EpiPowerInterlock(
        monitor, waveplate, beam_path, limited_path="Epifluorescence", limit_w=EPI_LIMIT_W
    )
    app = create_app(monitor, waveplate, interlock, power_model=POWER_MODEL)
    stop = Event()

    def poll() -> None:
        while not stop.is_set():
            monitor.poll_once()
            waveplate.poll_once()  # skipped while a move/home owns the stage
            beam_path.poll_once()  # skipped while a switch owns the flipper
            alarm = interlock.enforce()  # closes the shutter on Epi over the cap
            if alarm:
                print(alarm)
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
        if beam_path.snapshot()["connected"]:
            beam_path.close()  # Waits for any in-flight switch first.
            print(f"Flip mirror {FLIPPER_SERIAL} closed.")
