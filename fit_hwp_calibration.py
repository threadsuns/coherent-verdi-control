"""Fit HWP sweeps from calibrate_hwp.py and write the calibration for the GUI.

    python fit_hwp_calibration.py calibration_data/hwp_sweep_*W_*.csv

Fits T(theta) = A*sin^2(2(theta - theta0)) + B to each run separately and to all
runs together, for two ratios of the enclosure power:
    vs setpoint     fraction_of_setpoint column; includes the isolator. This is
                    what the GUI needs (enclosure W = setpoint x T) and what is saved.
    vs input        transmission column (HWP + PBS only), from --input-power-w;
                    printed and kept in the JSON metadata for reference.
If A, B, theta0 agree across setpoints, one curve serves every power. Writes:
    hwp_calibration.json      combined vs-setpoint fit; load it in hardware_gui.py
    hwp_calibration_fit.html  measured points, fits and residuals (open in a browser)
Review both before pointing hardware_gui.py at the JSON.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from coherent_verdi.hwp_calibration import HwpCalibration, fit


def read_run(path: Path) -> dict[str, Any]:
    header: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as f:
        lines = []
        for line in f:
            if line.startswith("#"):
                key, _, value = line[1:].partition(":")
                header[key.strip()] = value.strip()
            else:
                lines.append(line)
        rows = list(csv.DictReader(lines))
    return {"path": path, "header": header, "rows": rows}


def points(
    run: dict[str, Any], column: str, include_unstable: bool
) -> tuple[list[float], list[float]]:
    angles, ts = [], []
    for row in run["rows"]:
        if not row.get(column):
            continue
        if not include_unstable and row.get("meter_stable") == "False":
            continue
        angles.append(float(row["angle_meas_deg"]))
        ts.append(float(row[column]))
    return angles, ts


def describe(cal: HwpCalibration) -> str:
    m = cal.metadata
    return (
        f"A={cal.amplitude:.4f}  B={cal.leakage:.5f}  max={cal.max_transmission:6.2%}  "
        f"theta0={cal.offset_deg:7.3f}°  rms={m['rms_residual']:.5f}  n={m['points']}"
    )


def write_plot(path: Path, fits: list[tuple[str, list[float], list[float], HwpCalibration]],
               combined: HwpCalibration) -> None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        subplot_titles=("Transmission vs stage angle", "Residual (measured - combined fit)"),
    )
    grid = [i * 0.25 for i in range(int(360 / 0.25) + 1)]
    lo = min(min(a) for _, a, _, _ in fits)
    hi = max(max(a) for _, a, _, _ in fits)
    grid = [g for g in grid if lo <= g <= hi]
    for label, angles, ts, _ in fits:
        fig.add_trace(go.Scatter(x=angles, y=ts, mode="markers", name=label,
                                 marker={"size": 5}), row=1, col=1)
        residuals = [t - combined.transmission(a) for a, t in zip(angles, ts)]
        fig.add_trace(go.Scatter(x=angles, y=residuals, mode="markers", name=f"{label} residual",
                                 marker={"size": 4}, showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=grid, y=[combined.transmission(g) for g in grid], mode="lines",
                             name="combined fit", line={"color": "black"}), row=1, col=1)
    fig.update_yaxes(title_text="P_enclosure / setpoint", tickformat=".0%", row=1, col=1)
    fig.update_yaxes(title_text="ΔT", row=2, col=1)
    fig.update_xaxes(title_text="Stage angle (deg)", row=2, col=1)
    fig.update_layout(title=f"HWP calibration: {describe(combined)}", height=800)
    fig.write_html(path)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("csv", nargs="+", type=Path, help="sweep CSVs from calibrate_hwp.py")
    p.add_argument("--out", type=Path, default=Path("hwp_calibration.json"))
    p.add_argument("--plot", type=Path, default=Path("hwp_calibration_fit.html"))
    p.add_argument("--include-unstable", action="store_true",
                   help="keep points whose meter reading never settled")
    args = p.parse_args(argv)

    fits = []  # vs setpoint: saved for the GUI
    all_angles: list[float] = []
    all_ts: list[float] = []
    hwp_angles: list[float] = []  # vs input (HWP + PBS only)
    hwp_ts: list[float] = []
    isolator: dict[str, str] = {}
    for path in args.csv:
        run = read_run(path)
        angles, ts = points(run, "fraction_of_setpoint", args.include_unstable)
        if len(ts) < 4:
            print(f"SKIP {path.name}: {len(ts)} usable points (needs laser setpoint + meter)")
            continue
        setpoint = run["header"].get("laser_setpoint_w", "?")
        label = f"{setpoint} W ({path.name})"
        cal = fit(angles, ts, source=str(path))
        print(label)
        print(f"    vs setpoint: {describe(cal)}")
        fits.append((label, angles, ts, cal))
        all_angles += angles
        all_ts += ts
        h_angles, h_ts = points(run, "transmission", args.include_unstable)
        if len(h_ts) >= 4:
            print(f"    vs input:    {describe(fit(h_angles, h_ts))}")
            hwp_angles += h_angles
            hwp_ts += h_ts
        fraction = run["header"].get("input_fraction_of_setpoint", "None")
        if fraction != "None":
            isolator[setpoint] = fraction
            print(f"    input (after isolator) / setpoint: {float(fraction):.2%}")

    if not fits:
        print("No usable runs.")
        return 1
    hwp_only = fit(hwp_angles, hwp_ts) if len(hwp_ts) >= 4 else None
    combined = fit(
        all_angles,
        all_ts,
        source="fit_hwp_calibration.py",
        metadata={
            "reference": "laser setpoint (enclosure W = setpoint x T)",
            "runs": [str(p) for p in args.csv],
            "input_fraction_of_setpoint_by_setpoint_w": isolator,
            "hwp_pbs_only": None
            if hwp_only is None
            else {
                "amplitude": hwp_only.amplitude,
                "leakage": hwp_only.leakage,
                "offset_deg": hwp_only.offset_deg,
                "rms_residual": hwp_only.metadata["rms_residual"],
            },
        },
    )
    print()
    print("COMBINED vs setpoint (saved)")
    print(f"    {describe(combined)}")
    if hwp_only is not None:
        print("COMBINED vs input (HWP + PBS only)")
        print(f"    {describe(hwp_only)}")
    offsets = [c.offset_deg for *_, c in fits]
    maxima = [c.max_transmission for *_, c in fits]
    print(
        f"Spread across runs: theta0 {max(offsets) - min(offsets):.3f}°, "
        f"max fraction of setpoint {max(maxima) - min(maxima):.2%}"
    )
    combined.save(args.out)
    print(f"\nWrote {args.out}")
    try:
        write_plot(args.plot, fits, combined)
        print(f"Wrote {args.plot}")
    except ImportError:
        print("plotly not installed; skipped the plot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
