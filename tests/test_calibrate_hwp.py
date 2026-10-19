"""TEST-013: HWP calibration sweep, PM100D wrapper and fit scripts, all hardware-free."""

import importlib.util
import json
from pathlib import Path

import pytest

from coherent_verdi import pm100d_driver
from coherent_verdi.hwp_calibration import HwpCalibration, HwpPowerCalibration
from coherent_verdi.pm100d_driver import PM100D

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calibrate = load_script("calibrate_hwp")
fitter = load_script("fit_hwp_calibration")

HWP_TRUTH = HwpCalibration(amplitude=0.98, leakage=0.002, offset_deg=21.0)  # HWP + PBS only
SETPOINT_W = 2.0
INPUT_W = 1.81  # after the isolator: 90.5% of the setpoint


class FakeStage:
    HOMED_STATUS_BIT = 0x400

    def __init__(self, homed=True, interrupt_at=None):
        self.position = 37.0
        self.homed = homed
        self.interrupt_at = interrupt_at
        self.moves, self.events = [], []

    def home(self):
        self.events.append("home")
        self.position, self.homed = 0.0, True

    def get_status_bits(self):
        return self.HOMED_STATUS_BIT if self.homed else 0

    def get_position(self):
        return self.position

    def move_to_position(self, angle):
        if self.interrupt_at is not None and len(self.moves) == self.interrupt_at:
            raise KeyboardInterrupt
        self.moves.append(angle)
        self.position = angle + 0.0004
        return self.position

    def stop_motion(self):
        self.events.append("stop")

    def close(self):
        self.events.append("close")


class FakeMeter:
    idn = "Thorlabs,PM100D,P0000000,2.7.0"
    sensor = "S415C"
    wavelength_nm = 532.0
    attenuation_db = 0.0
    accelerator = "OFF"

    def __init__(self, stage):
        self.stage, self.closed = stage, False
        self.zeroed_at_move = None

    def zero(self):
        self.zeroed_at_move = len(self.stage.moves)
        return -0.00204

    def read_settled(self, **settle):
        power = HWP_TRUTH.power_at(self.stage.position, INPUT_W)
        return {"power_w": power, "std_w": 1e-4, "samples": 4, "stable": True}

    def close(self):
        self.closed = True


class FakeLaser:
    def __init__(self, setpoint=2.0):
        self.setpoint, self.entered, self.exited = setpoint, False, False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True

    def read(self, query):
        return {"?SP": self.setpoint, "?S": 1}[query]

    def read_laser_state(self):
        return 1

    def read_power_w(self):
        return self.setpoint * 0.99


def run(argv, stage, meter=None, laser=None, prompts=None):
    output, asked = [], []

    def prompt(text):
        asked.append(text)
        return ""

    code = calibrate.main(
        argv,
        open_stage=lambda: stage,
        open_meter=lambda args: meter,
        open_laser=lambda: laser,
        prompt=prompt,
        out=output.append,
    )
    return code, output, asked


def read_csv(directory):
    (path,) = Path(directory).glob("hwp_sweep_*.csv")
    return path, fitter.read_run(path)


def test_dry_run_moves_only_and_writes_angles(tmp_path):
    stage = FakeStage(homed=False)
    code, output, asked = run(
        ["--dry-run", "--home", "--step", "30", "--out-dir", str(tmp_path)], stage
    )
    assert code == 0
    assert stage.events[0] == "home" and stage.events[-1] == "close"
    assert stage.moves == [0, 30, 60, 90, 120, 150, 180]
    path, data = read_csv(tmp_path)
    assert path.name.startswith("hwp_sweep_dryrun_")
    assert [float(r["angle_cmd_deg"]) for r in data["rows"]] == stage.moves
    assert all(r["meter_power_w"] == "" for r in data["rows"])
    assert len(asked) == 1  # only "Ready to sweep"


def test_custom_range_and_fine_step(tmp_path):
    stage = FakeStage()
    argv = ["--dry-run", "--step", "0.25", "--start", "10", "--stop", "11"]
    run(argv + ["--out-dir", str(tmp_path)], stage)
    assert stage.moves == [10, 10.25, 10.5, 10.75, 11]


def test_unhomed_rotator_aborts_without_moving(tmp_path):
    stage = FakeStage(homed=False)
    code, output, _ = run(["--dry-run", "--step", "30", "--out-dir", str(tmp_path)], stage)
    assert code == 2 and stage.moves == [] and any("not homed" in line for line in output)
    assert stage.events == ["close"]


def test_allow_unhomed_for_testing(tmp_path):
    stage = FakeStage(homed=False)
    code, *_ = run(
        ["--dry-run", "--allow-unhomed", "--step", "90", "--out-dir", str(tmp_path)], stage
    )
    assert code == 0 and stage.moves == [0, 90, 180]


def test_setpoint_mismatch_aborts_before_touching_rotator(tmp_path):
    stage, laser = FakeStage(), FakeLaser(setpoint=3.0)
    code, output, _ = run(
        ["--setpoint", "2", "--no-meter", "--step", "30", "--out-dir", str(tmp_path)],
        stage, laser=laser,
    )
    assert code == 2 and stage.moves == [] and laser.exited
    assert any("expected 2 W" in line for line in output)


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run"],  # no step
        ["--dry-run", "--step", "0"],
        ["--dry-run", "--step", "10", "--start", "90", "--stop", "45"],
        ["--dry-run", "--step", "10", "--stop", "400"],
        ["--no-meter", "--step", "10"],  # laser used but no setpoint
        ["--setpoint", "1", "--input-power-w", "0", "--step", "10"],
    ],
)
def test_bad_arguments_rejected(argv):
    with pytest.raises(SystemExit):
        calibrate.parse_args(argv)


def test_full_sweep_then_fit_recovers_calibration(tmp_path, monkeypatch):
    stage = FakeStage()
    meter, laser = FakeMeter(stage), FakeLaser(setpoint=SETPOINT_W)
    code, output, asked = run(
        ["--setpoint", "2", "--input-power-w", "1.81", "--home", "--step", "2",
         "--out-dir", str(tmp_path)],
        stage, meter=meter, laser=laser,
    )
    assert code == 0 and meter.closed and laser.exited
    assert len(asked) == 1  # only "Ready to sweep": the meter never moves
    assert any("1.8100 W (90.50% of setpoint)" in line for line in output)
    path, data = read_csv(tmp_path)
    assert path.name.startswith("hwp_sweep_2W_")
    assert float(data["header"]["input_power_w"]) == INPUT_W
    assert float(data["header"]["input_fraction_of_setpoint"]) == pytest.approx(0.905)
    assert data["header"]["sensor"] == "S415C"
    assert data["header"]["attenuation_db"] == "0.0"
    assert data["header"]["thermopile_accelerator"] == "OFF"
    assert not any("WARNING" in line for line in output)
    row = data["rows"][0]
    assert float(row["laser_setpoint_w"]) == SETPOINT_W and row["meter_stable"] == "True"
    power = float(row["meter_power_w"])
    assert float(row["transmission"]) == pytest.approx(power / INPUT_W)
    assert float(row["fraction_of_setpoint"]) == pytest.approx(power / SETPOINT_W)

    out_json, plot = tmp_path / "cal.json", tmp_path / "fit.html"
    assert fitter.main([str(path), "--out", str(out_json), "--plot", str(plot)]) == 0
    model = HwpPowerCalibration.load(out_json)  # vs setpoint: isolator folded in
    assert model.offset_deg == pytest.approx(21.0, abs=0.01)
    assert model.calibrated_setpoints_w == [2.0]
    cal = model.at_setpoint(2.0)
    assert cal.amplitude == pytest.approx(0.98 * 0.905, abs=1e-3)
    assert cal.leakage == pytest.approx(0.002 * 0.905, abs=1e-4)
    meta = json.loads(out_json.read_text())["metadata"]
    assert meta["runs"] == [str(path)]
    assert meta["hwp_pbs_only_max_by_setpoint_w (approximate)"]["2"] == pytest.approx(
        0.982, abs=1e-3
    )
    assert meta["input_fraction_of_setpoint_by_setpoint_w (approximate)"]["2"] == (
        pytest.approx(0.905)
    )
    assert plot.stat().st_size > 0


def write_sweep(path, setpoint_w, cal, angles):
    """Minimal calibrate_hwp.py-style CSV with a fraction_of_setpoint column."""
    lines = [f"# laser_setpoint_w: {setpoint_w}\n", "angle_meas_deg,fraction_of_setpoint\n"]
    lines += [f"{a},{cal.transmission(a)}\n" for a in angles]
    path.write_text("".join(lines), encoding="utf-8")


def test_fit_builds_per_setpoint_calibration_with_shared_offset(tmp_path):
    angles = [i * 0.5 for i in range(361)]
    truths = {
        1.0: HwpCalibration(0.937, 0.0010, 26.28),
        3.0: HwpCalibration(0.947, 0.0005, 26.28),
        5.0: HwpCalibration(0.954, 0.0001, 26.28),
    }
    for sp, cal in truths.items():
        write_sweep(tmp_path / f"hwp_sweep_{sp:g}W_x.csv", sp, cal, angles)
    (tmp_path / "hwp_sweep_test_1W_x.csv").write_text("junk", encoding="utf-8")
    out_json = tmp_path / "cal.json"
    pattern = str(tmp_path / "hwp_sweep_[0-9]W_*.csv")  # wildcard expanded by the script
    assert fitter.main([pattern, "--out", str(out_json), "--plot", str(tmp_path / "p.html")]) == 0
    model = HwpPowerCalibration.load(out_json)
    assert model.calibrated_setpoints_w == [1.0, 3.0, 5.0]
    assert model.offset_deg == pytest.approx(26.28, abs=1e-3)
    for sp, truth in truths.items():
        cal = model.at_setpoint(sp)
        assert cal.amplitude == pytest.approx(truth.amplitude, abs=1e-6)
        assert cal.leakage == pytest.approx(truth.leakage, abs=1e-6)
    mid = model.at_setpoint(2.0)  # halfway between 1 and 3 W
    assert mid.amplitude == pytest.approx((0.937 + 0.947) / 2, abs=1e-6)


def test_nonzero_meter_attenuation_is_warned_and_recorded(tmp_path):
    stage = FakeStage()
    meter, laser = FakeMeter(stage), FakeLaser(setpoint=SETPOINT_W)
    meter.attenuation_db = 3.0
    meter.accelerator = None  # firmware without the query
    code, output, _ = run(
        ["--setpoint", "2", "--step", "90", "--out-dir", str(tmp_path)],
        stage, meter=meter, laser=laser,
    )
    assert code == 0
    assert any("attenuation: 3 dB" in line and "unavailable" in line for line in output)
    assert any("WARNING: meter attenuation is 3 dB" in line for line in output)
    _, data = read_csv(tmp_path)
    assert data["header"]["attenuation_db"] == "3.0"


def test_tiny_and_negative_readings_keep_transmission(tmp_path):
    stage = FakeStage()
    meter, laser = FakeMeter(stage), FakeLaser(setpoint=SETPOINT_W)
    readings = iter([2e-4, -1e-4, 1.5])  # near extinction, meter noise below zero, peak
    meter.read_settled = lambda **settle: {
        "power_w": next(readings), "std_w": 5e-5, "samples": 4, "stable": True
    }
    code, output, _ = run(
        ["--setpoint", "2", "--input-power-w", "1.81", "--step", "90",
         "--out-dir", str(tmp_path)],
        stage, meter=meter, laser=laser,
    )
    assert code == 0
    _, data = read_csv(tmp_path)
    assert [float(r["transmission"]) for r in data["rows"]] == pytest.approx(
        [2e-4 / 1.81, -1e-4 / 1.81, 1.5 / 1.81]
    )
    assert [float(r["fraction_of_setpoint"]) for r in data["rows"]] == pytest.approx(
        [1e-4, -5e-5, 0.75]
    )


def test_without_input_power_transmission_blank_but_setpoint_fraction_kept(tmp_path):
    stage = FakeStage()
    meter, laser = FakeMeter(stage), FakeLaser(setpoint=SETPOINT_W)
    code, output, _ = run(
        ["--setpoint", "2", "--step", "90", "--out-dir", str(tmp_path)],
        stage, meter=meter, laser=laser,
    )
    assert code == 0
    assert any("No --input-power-w" in line for line in output)
    _, data = read_csv(tmp_path)
    assert all(r["transmission"] == "" for r in data["rows"])
    assert all(r["fraction_of_setpoint"] != "" for r in data["rows"])


def test_zero_runs_with_beam_blocked_before_sweep_and_is_recorded(tmp_path):
    stage = FakeStage()
    meter, laser = FakeMeter(stage), FakeLaser(setpoint=SETPOINT_W)
    code, output, asked = run(
        ["--setpoint", "2", "--input-power-w", "1.81", "--zero", "--step", "90",
         "--out-dir", str(tmp_path)],
        stage, meter=meter, laser=laser,
    )
    assert code == 0
    assert meter.zeroed_at_move == 0  # before any sweep move
    assert "BLOCK the beam" in asked[0] and "UNBLOCK" in asked[1] and "Ready" in asked[2]
    assert any("offset -2.040 mW" in line for line in output)
    _, data = read_csv(tmp_path)
    assert data["header"]["zeroed_by_script"] == "True"
    assert float(data["header"]["zero_offset_w"]) == -0.00204


def test_without_zero_flag_meter_is_not_zeroed(tmp_path):
    stage = FakeStage()
    meter = FakeMeter(stage)
    run(["--no-laser", "--step", "90", "--out-dir", str(tmp_path)], stage, meter=meter)
    assert meter.zeroed_at_move is None
    _, data = read_csv(tmp_path)
    assert data["header"]["zeroed_by_script"] == "False"


def test_ctrl_c_stops_rotator_and_keeps_rows(tmp_path):
    stage = FakeStage(interrupt_at=3)
    code, output, _ = run(["--dry-run", "--step", "10", "--out-dir", str(tmp_path)], stage)
    assert code == 130
    assert "stop" in stage.events and stage.events[-1] == "close"
    _, data = read_csv(tmp_path)
    assert len(data["rows"]) == 3


# --- PM100D wrapper against a fake VISA instrument ---------------------------


class FakeInstrument:
    def __init__(self, replies, error='0,"No error"'):
        self.replies, self.error = replies, error
        self.written, self.closed = [], False
        self.timeout = 5000

    def query(self, command):
        if command == "SYST:ERR?":
            return self.error + "\n"
        reply = self.replies[command]
        return (reply.pop(0) if isinstance(reply, list) else reply) + "\n"

    def write(self, command):
        self.written.append(command)

    def close(self):
        self.closed = True


class FakeRM:
    def __init__(self, resources, inst):
        self.resources, self.inst = resources, inst

    def list_resources(self, query):
        assert query == pm100d_driver.THORLABS_USB_FILTER
        return tuple(self.resources)

    def open_resource(self, resource, **kwargs):
        return self.inst


REPLIES = {
    "*IDN?": "Thorlabs,PM100D,P0012345,2.7.0",
    "SYST:SENS:IDN?": "S415C,123",
    "SENS:CORR:WAV?": "532",
    "SENS:CORR:LOSS:INP:MAGN?": "0.000000E+00",
    "INP:THER:ACC:STAT?": "1",
}


def test_pm100d_configures_measurement_only():
    inst = FakeInstrument(dict(REPLIES))
    meter = PM100D(rm=FakeRM(["USB0::0x1313::0x8078::P0012345::INSTR"], inst))
    assert meter.idn.startswith("Thorlabs,PM100D") and meter.wavelength_nm == 532
    assert inst.written == [
        "SENS:CORR:WAV 532.0",
        "SENS:POW:UNIT W",
        "SENS:POW:RANG:AUTO ON",
        "SENS:AVER:COUN 100",
        "CONF:POW",
    ]
    assert not any("ZERO" in c for c in inst.written)
    assert meter.attenuation_db == 0.0 and meter.accelerator == "ON"
    assert inst.timeout == 5000  # restored after the short optional-query timeout


def test_pm100d_tolerates_firmware_without_optional_queries():
    replies = {k: v for k, v in REPLIES.items() if "LOSS" not in k and "THER" not in k}
    inst = FakeInstrument(replies)  # unknown queries raise, like a VISA timeout
    meter = PM100D("USB0::x", rm=FakeRM([], inst))
    assert meter.attenuation_db is None and meter.accelerator is None
    assert inst.timeout == 5000 and not inst.closed


@pytest.mark.parametrize("resources", [[], ["USB0::a", "USB0::b"]])
def test_pm100d_needs_exactly_one_meter_unless_named(resources):
    with pytest.raises(RuntimeError, match="exactly one"):
        PM100D(rm=FakeRM(resources, FakeInstrument(dict(REPLIES))))


def test_pm100d_configuration_error_closes_connection():
    inst = FakeInstrument(dict(REPLIES), error='-222,"Data out of range"')
    with pytest.raises(RuntimeError, match="out of range"):
        PM100D("USB0::x", rm=FakeRM([], inst))
    assert inst.closed


def test_read_settled_waits_for_stable_window(monkeypatch):
    monkeypatch.setattr(pm100d_driver, "sleep", lambda s: None)
    readings = ["1.20", "1.10", "1.050", "1.001", "1.000", "1.002", "1.001"]
    inst = FakeInstrument(dict(REPLIES) | {"MEAS:POW?": readings})
    meter = PM100D("USB0::x", rm=FakeRM([], inst))
    result = meter.read_settled(window=4, rel_tol=0.005)
    assert result["stable"] and result["samples"] == 7
    assert result["power_w"] == pytest.approx(1.001)


def test_read_settled_times_out_without_raising(monkeypatch):
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(pm100d_driver, "sleep", lambda s: None)
    monkeypatch.setattr(pm100d_driver, "monotonic", lambda: next(clock))
    noisy = [str(1 + 0.05 * (i % 2)) for i in range(50)]
    inst = FakeInstrument(dict(REPLIES) | {"MEAS:POW?": noisy})
    meter = PM100D("USB0::x", rm=FakeRM([], inst))
    result = meter.read_settled(timeout_s=20)
    assert result["stable"] is False and result["samples"] >= 4


def test_pm100d_zero_waits_for_completion_and_returns_offset(monkeypatch):
    monkeypatch.setattr(pm100d_driver, "sleep", lambda s: None)
    replies = dict(REPLIES) | {
        "SENS:CORR:COLL:ZERO:STAT?": ["1", "1", "0"],
        "SENS:CORR:COLL:ZERO:MAGN?": "-2.040000E-03",
    }
    inst = FakeInstrument(replies)
    meter = PM100D("USB0::x", rm=FakeRM([], inst))
    assert meter.zero() == pytest.approx(-0.00204)
    assert inst.written[-1] == "SENS:CORR:COLL:ZERO:INIT"
    assert replies["SENS:CORR:COLL:ZERO:STAT?"] == []  # polled until finished


def test_pm100d_zero_error_is_raised(monkeypatch):
    monkeypatch.setattr(pm100d_driver, "sleep", lambda s: None)
    inst = FakeInstrument(dict(REPLIES) | {"SENS:CORR:COLL:ZERO:STAT?": "0"})
    meter = PM100D("USB0::x", rm=FakeRM([], inst))
    inst.error = '-200,"Execution error"'
    with pytest.raises(RuntimeError, match="zero failed"):
        meter.zero()
