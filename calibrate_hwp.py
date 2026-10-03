"""Half-waveplate transmission sweep: K10CR2 angle vs PM100D power, saved to CSV.

One run = one laser setpoint. Close hardware_gui.py and Thorlabs OPM first
(this script needs the rotator, the meter, and COM6 to itself). The laser is
opened READ-ONLY: set the power, start the laser and open the shutter yourself
(GUI or front panel) before running; this script never changes laser state.

The meter stays at the enclosure position (after the PBS) for the whole run.
--input-power-w is the power entering the HWP+PBS (after the isolator), measured
beforehand at this setpoint. Each row records two ratios of the meter reading:
    transmission           meter / input power   (HWP + PBS only)
    fraction_of_setpoint   meter / laser setpoint (whole chain; what the GUI uses)

Typical use:
    python calibrate_hwp.py --list-meters                              # find the PM100D
    python calibrate_hwp.py --dry-run --home --step 30                 # rotator only
    python calibrate_hwp.py --no-laser --home --step 30 --stop 90      # meter, no beam
    python calibrate_hwp.py --setpoint 1 --input-power-w 0.905 --home --step 0.5

Each row is written as soon as it is measured, so Ctrl+C keeps all data so far
(and stops the rotator). Fit the results with fit_hwp_calibration.py.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

HWP_SERIAL = "55543994"
LASER_PORT = "COM6"
LASER_MODEL = "V5"
LASER_BAUDRATE = 19200
ACTIVE_FAULT_CLEAR_REPLY = "SYSTEM OK"

COLUMNS = [
    "timestamp",
    "angle_cmd_deg",
    "angle_meas_deg",
    "meter_power_w",
    "meter_std_w",
    "meter_samples",
    "meter_stable",
    "transmission",
    "fraction_of_setpoint",
    "laser_setpoint_w",
    "laser_power_w",
]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--step", type=float, help="angle increment, deg (e.g. 30 to test, 0.5 real)")
    p.add_argument("--start", type=float, default=0.0, help="first angle, deg (default 0)")
    p.add_argument("--stop", type=float, default=180.0, help="last angle, deg (default 180)")
    p.add_argument("--setpoint", type=float, help="expected laser setpoint, W (checked)")
    p.add_argument("--home", action="store_true", help="home the rotator before sweeping")
    p.add_argument("--dry-run", action="store_true", help="rotator only: no meter, no laser")
    p.add_argument("--no-meter", action="store_true", help="don't use the power meter")
    p.add_argument("--no-laser", action="store_true", help="don't connect to the laser")
    p.add_argument(
        "--input-power-w",
        type=float,
        help="power entering the HWP+PBS (after the isolator), measured beforehand, W",
    )
    p.add_argument(
        "--allow-unhomed", action="store_true", help="sweep even if not homed (testing only)"
    )
    p.add_argument("--settle-s", type=float, default=3.0, help="wait after each move, s")
    p.add_argument("--stable-timeout-s", type=float, default=20.0, help="max settle time, s")
    p.add_argument("--rel-tol", type=float, default=0.005, help="settled spread, fraction")
    p.add_argument("--abs-tol-mw", type=float, default=1.0, help="settled spread floor, mW")
    p.add_argument("--wavelength-nm", type=float, default=532.0)
    p.add_argument("--meter-resource", help="VISA resource (default: the only Thorlabs USB)")
    p.add_argument("--list-meters", action="store_true", help="list Thorlabs VISA resources")
    p.add_argument("--out-dir", default="calibration_data", help="CSV folder")
    args = p.parse_args(argv)
    if args.dry_run:
        args.no_meter = args.no_laser = True
    if not args.list_meters:
        if args.step is None or args.step <= 0:
            p.error("--step must be a positive angle in degrees")
        if not 0 <= args.start < args.stop <= 360:
            p.error("need 0 <= --start < --stop <= 360")
        if args.step > args.stop - args.start:
            p.error("--step is larger than the sweep range")
        if not args.no_laser and (args.setpoint is None or args.setpoint <= 0):
            p.error("--setpoint (W) is required unless --no-laser/--dry-run")
        if args.input_power_w is not None and args.input_power_w <= 0:
            p.error("--input-power-w must be above zero")
    return args


def sweep_angles(start: float, stop: float, step: float) -> list[float]:
    count = int((stop - start) / step + 1e-9)
    return [round(start + i * step, 6) for i in range(count + 1)]


def _default_stage() -> Any:
    from coherent_verdi.k10cr2_driver import K10CR2  # needs thorlabs_kinesis

    return K10CR2(HWP_SERIAL)


def _default_meter(args: argparse.Namespace) -> Any:
    from coherent_verdi.pm100d_driver import PM100D

    return PM100D(args.meter_resource, wavelength_nm=args.wavelength_nm)


def _default_laser() -> Any:
    from coherent_verdi import VerdiController

    return VerdiController(
        LASER_PORT,
        model=LASER_MODEL,
        baudrate=LASER_BAUDRATE,
        allow_writes=False,  # read-only: this script never changes laser state
        active_fault_clear_reply=ACTIVE_FAULT_CLEAR_REPLY,
    )


def _list_meters(out: Callable[[str], None]) -> int:
    import pyvisa

    from coherent_verdi.pm100d_driver import find_meters

    rm = pyvisa.ResourceManager()
    found = find_meters(rm)
    if not found:
        out("No Thorlabs USB instruments visible to VISA. Close OPM and check the")
        out("Power Meter Driver Switcher has the PM100D on the NI-VISA driver.")
        return 1
    for resource in found:
        inst = rm.open_resource(resource, timeout=3000, read_termination="\n")
        try:
            out(f"{resource}  ->  {inst.query('*IDN?').strip()}")
        finally:
            inst.close()
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    open_stage: Callable[[], Any] = _default_stage,
    open_meter: Callable[[argparse.Namespace], Any] = _default_meter,
    open_laser: Callable[[], Any] = _default_laser,
    prompt: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    args = parse_args(argv)
    if args.list_meters:
        return _list_meters(out)

    angles = sweep_angles(args.start, args.stop, args.step)
    per_point_s = 0.6 if args.no_meter else args.settle_s + 1.5
    out(
        f"Sweep {angles[0]:g}° to {angles[-1]:g}° in {args.step:g}° steps: "
        f"{len(angles)} points, roughly {len(angles) * per_point_s / 60:.0f} min."
    )
    out(
        f"Rotator: yes{' (home first)' if args.home else ''} | "
        f"meter: {'no' if args.no_meter else 'yes'} | "
        f"laser: {'no' if args.no_laser else f'read-only, expecting {args.setpoint:g} W'}"
    )

    with ExitStack() as stack:
        laser = None
        setpoint_w = None
        if not args.no_laser:
            laser = stack.enter_context(open_laser())
            setpoint_w = float(laser.read("?SP"))
            if abs(setpoint_w - args.setpoint) > 0.01 * args.setpoint + 0.001:
                out(
                    f"ABORT: laser setpoint reads {setpoint_w:.4f} W, expected "
                    f"{args.setpoint:g} W. Set it first (GUI/front panel)."
                )
                return 2
            state = ("STANDBY", "ON", "FAULT")[laser.read_laser_state()]
            shutter = "OPEN" if laser.read("?S") else "CLOSED"
            out(f"Laser: setpoint {setpoint_w:.4f} W, state {state}, shutter {shutter}.")
        if args.input_power_w is not None:
            ratio = f" ({args.input_power_w / setpoint_w:.2%} of setpoint)" if setpoint_w else ""
            out(f"Input power (after isolator): {args.input_power_w:.4f} W{ratio}")
        elif not args.no_meter:
            out("No --input-power-w given: the transmission column will be blank.")

        meter = None
        if not args.no_meter:
            meter = open_meter(args)
            stack.callback(meter.close)
            out(f"Meter: {meter.idn} | sensor {meter.sensor} | {meter.wavelength_nm:g} nm")
            attenuation = meter.attenuation_db
            out(
                f"Meter attenuation: "
                f"{'unavailable' if attenuation is None else f'{attenuation:g} dB'} | "
                f"thermopile accelerator: {meter.accelerator or 'unavailable'}"
            )
            if attenuation:
                out(
                    f"WARNING: meter attenuation is {attenuation:g} dB, so every reading is "
                    "scaled. Set it to 0 dB on the PM100D unless that is intended."
                )

        stage = open_stage()
        stack.callback(stage.close)
        if args.home:
            out("Homing rotator...")
            stage.home()
        homed = bool(stage.get_status_bits() & stage.HOMED_STATUS_BIT)
        if not homed and not args.allow_unhomed:
            out("ABORT: rotator is not homed; angles would not match the calibration.")
            out("Re-run with --home (or --allow-unhomed for a test).")
            return 2
        out(f"Rotator at {stage.get_position():.3f}° ({'homed' if homed else 'NOT homed'}).")

        settle = {
            "min_wait_s": args.settle_s,
            "rel_tol": args.rel_tol,
            "abs_tol_w": args.abs_tol_mw / 1000,
            "timeout_s": args.stable_timeout_s,
        }

        prompt(f"\nReady to sweep {len(angles)} angles. Press Enter to start (Ctrl+C aborts)... ")

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = "dryrun" if args.dry_run else f"{args.setpoint:g}W" if args.setpoint else "nolaser"
        path = out_dir / f"hwp_sweep_{label}_{stamp}.csv"
        f = stack.enter_context(path.open("w", newline="", encoding="utf-8"))
        for key, value in {
            "started": datetime.now().isoformat(timespec="seconds"),
            "hwp_serial": HWP_SERIAL,
            "homed": homed,
            "expected_setpoint_w": args.setpoint,
            "laser_setpoint_w": setpoint_w,
            "input_power_w": args.input_power_w,
            "input_fraction_of_setpoint": args.input_power_w / setpoint_w
            if args.input_power_w and setpoint_w
            else None,
            "meter": getattr(meter, "idn", None),
            "sensor": getattr(meter, "sensor", None),
            "wavelength_nm": getattr(meter, "wavelength_nm", None),
            "attenuation_db": getattr(meter, "attenuation_db", None),
            "thermopile_accelerator": getattr(meter, "accelerator", None),
            "step_deg": args.step,
            "settle": settle,
        }.items():
            f.write(f"# {key}: {value}\n")
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        f.flush()

        laser_warned = False
        try:
            for i, angle in enumerate(angles, 1):
                measured = stage.move_to_position(angle)
                row: dict[str, Any] = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "angle_cmd_deg": angle,
                    "angle_meas_deg": round(measured, 4),
                }
                line = f"{i:>4}/{len(angles)}  {angle:8.3f}°  (at {measured:8.3f}°)"
                if meter is not None:
                    reading = meter.read_settled(**settle)
                    row.update(
                        meter_power_w=reading["power_w"],
                        meter_std_w=reading["std_w"],
                        meter_samples=reading["samples"],
                        meter_stable=reading["stable"],
                    )
                    line += f"  {reading['power_w'] * 1e3:10.2f} mW ± {reading['std_w'] * 1e3:.2f}"
                    # Every point, however small: low-transmission angles are data too.
                    if args.input_power_w:
                        row["transmission"] = reading["power_w"] / args.input_power_w
                        line += f"  T = {row['transmission']:8.3%}"
                    if setpoint_w:
                        row["fraction_of_setpoint"] = reading["power_w"] / setpoint_w
                    if not reading["stable"]:
                        line += "  UNSTABLE"
                if laser is not None:
                    try:
                        row["laser_setpoint_w"] = laser.read("?SP")
                        row["laser_power_w"] = laser.read_power_w()
                    except Exception as exc:  # logging only; keep the sweep going
                        if not laser_warned:
                            out(f"WARNING: laser readback failed ({exc}); continuing.")
                            laser_warned = True
                writer.writerow(row)
                f.flush()
                out(line)
        except KeyboardInterrupt:
            try:
                stage.stop_motion()
            finally:
                out(f"\nStopped by Ctrl+C. Rotator halted; data so far saved to {path}")
            return 130

    out(f"\nDone. {len(angles)} points saved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
