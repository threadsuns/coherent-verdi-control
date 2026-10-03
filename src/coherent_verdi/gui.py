"""Optional caller-scheduled monitoring and Dash client; no implicit worker."""

from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Any

from dash import Dash, Input, Output, State, dcc, html

from .controller import VerdiController, finite_range
from .hwp_calibration import HwpCalibration


class Monitor:
    """One caller polls; GUI clients read copied snapshots without waiting for I/O."""

    def __init__(
        self,
        controller: VerdiController,
        *,
        interval_s: float = 1.0,
        history_size: int = 600,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.interval_s = finite_range(interval_s, 0.001, 86400, "interval_s")
        if type(history_size) is not int or not 1 <= history_size <= 100000:
            raise ValueError("history_size must be an integer in [1, 100000]")
        self._controller = controller
        self._clock = clock
        self._started: float | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = Lock()

    def poll_once(self) -> dict[str, Any]:
        started = self._clock()
        sample: dict[str, Any] = {"attempted_at": datetime.now(UTC).isoformat()}
        try:
            sample.update(status=self._controller.status(), error=None)
        except Exception as exc:
            sample.update(status=None, error=f"{type(exc).__name__}: {exc}".rstrip())
        with self._lock:
            self._started = started
            self._history.append(sample)
            return deepcopy(sample)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model": self._controller.model,
                "simulated": self._controller.is_simulated,
                "history": deepcopy(list(self._history)),
                "age_s": max(0.0, self._clock() - self._started)
                if self._started is not None and self._history[-1]["status"]
                else None,
                "interval_s": self.interval_s,
            }


class Waveplate:
    """Serializes all K10CR2 half-waveplate access between the poller and GUI clicks.

    `stage` is an already-open K10CR2 (or None when it failed to connect). The
    caller owns opening it; close() releases it. Only one move/home runs at a
    time; the poller skips reads while one is in progress, and the move/home
    itself publishes the live angle instead.
    """

    MAX_ANGLE_DEG = 360.0
    PROGRESS_POLL_S = 0.2  # angle readback period during a move/home (~0.3 s with I/O)
    MOVE_TIMEOUT_S = 30.0  # driver defaults
    HOME_TIMEOUT_S = 60.0

    def __init__(
        self,
        stage: Any | None,
        *,
        connect_error: str | None = None,
        calibration: HwpCalibration | None = None,
    ) -> None:
        self._stage = stage
        # Angle <-> transmission model; usable for calculations even when disconnected.
        self.calibration = calibration if calibration is not None else HwpCalibration()
        self._io = Lock()  # held for every stage transaction
        self._lock = Lock()  # guards the cached state below
        self._state: dict[str, Any] = {
            "connected": stage is not None,
            "angle_deg": None,
            "homed": None,
            "busy": None,
            "error": None if stage is not None else (connect_error or "No stage configured"),
        }

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _read(self) -> None:
        """Caller holds self._io. Records read failures instead of raising."""
        assert self._stage is not None
        try:
            angle = self._stage.get_position()
            homed = bool(self._stage.get_status_bits() & self._stage.HOMED_STATUS_BIT)
            self._update(angle_deg=angle, homed=homed, error=None)
        except Exception as exc:
            self._update(error=f"{type(exc).__name__}: {exc}".rstrip())

    def poll_once(self) -> None:
        if self._stage is None or not self._io.acquire(blocking=False):
            return  # not connected, or a move/home owns the stage
        try:
            self._read()
        finally:
            self._io.release()

    def _run(self, label: str, action: Callable[[Any], object]) -> None:
        if self._stage is None:
            raise RuntimeError("half-waveplate stage is not connected")
        with self._lock:
            if self._state["busy"]:
                raise RuntimeError(f"stage busy ({self._state['busy']}); wait for it to finish")
            self._state["busy"] = label
        try:
            # Only a poll can hold the I/O lock here, and it finishes within ~0.5 s.
            if not self._io.acquire(timeout=5):
                raise RuntimeError("stage I/O is unavailable; try again")
            try:
                action(self._stage)
            finally:
                self._read()
                self._io.release()
        finally:
            self._update(busy=None)

    def _track(self, done: Callable[[int], bool], timeout_s: float, what: str) -> None:
        """Caller holds self._io. Publish the live angle until `done(position)` is true.

        Same completion checks as the driver's own wait loops, which can't report
        progress; the driver is started with wait=False and this does the waiting.
        """
        assert self._stage is not None
        deadline = monotonic() + timeout_s
        while True:
            position = self._stage.get_position_device_units()
            self._update(angle_deg=self._stage.device_units_to_deg(position))
            if done(position):
                return
            if monotonic() > deadline:
                raise TimeoutError(f"stage did not finish {what} within {timeout_s:.0f} s")
            sleep(self.PROGRESS_POLL_S)

    def move_to(self, angle_deg: object) -> float:
        angle = finite_range(
            angle_deg, 0, self.MAX_ANGLE_DEG, "waveplate angle"  # type: ignore[arg-type]
        )

        def move(stage: Any) -> None:
            target = stage.deg_to_device_units(angle)
            stage.move_to_position(angle, wait=False)
            self._track(lambda pos: abs(pos - target) <= 1, self.MOVE_TIMEOUT_S, "moving")

        self._run(f"moving to {angle:.2f}°", move)
        return angle

    def home(self) -> None:
        def home(stage: Any) -> None:
            def homed(_position: int) -> bool:
                bits = stage.get_status_bits()
                return bool(bits & stage.HOMED_STATUS_BIT) and not bits & stage.HOMING_STATUS_BIT

            stage.home(wait=False)
            self._track(homed, self.HOME_TIMEOUT_S, "homing")

        self._run("homing", home)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def close(self, timeout_s: float = 65.0) -> None:
        """Wait (bounded) for any in-flight move/home, then release the stage."""
        if self._stage is None:
            return
        acquired = self._io.acquire(timeout=timeout_s)
        try:
            self._stage.close()
        finally:
            self._stage = None
            self._update(connected=False, error="Stage closed")
            if acquired:
                self._io.release()


def _source_label(simulated: bool) -> str:
    return "SIMULATOR" if simulated else "NanoNMR-M Verdi V5"


LASER_STATE_COLORS = {"STANDBY": "#fb923c", "ON": "#4ade80", "FAULT": "#f87171"}
SHUTTER_COLORS = {"CLOSED": "#fb923c", "OPEN": "#3b82f6"}
HWP_MOVING_COLOR = "#facc15"


def _colored(text: str, colors: dict[str, str]) -> Any:
    """Wrap text in a colored span for states in `colors`; others render plain."""
    color = colors.get(text)
    return html.Span(text, style={"color": color}) if color else text


def _with_unit(value: str, unit: str) -> Any:
    """Render a value with its unit inline as a large suffix, not a small caption."""
    return [value, html.Span(unit, className="unit")]


def dashboard_data(service: Monitor) -> dict[str, Any]:
    """Read cached values only; rendering or opening a second tab never polls hardware."""
    snapshot = service.snapshot()
    samples = snapshot["history"]
    last = samples[-1] if samples else None
    status = last["status"] if last else None
    age = snapshot["age_s"]
    failed = last is not None and (last["status"] is None)
    stale = (
        status is not None
        and age is not None
        and age > max(5.0, 3 * snapshot["interval_s"] + 2 * status["duration_s"])
    )
    return {
        "simulated": snapshot["simulated"],
        "model": snapshot["model"],
        "health": "NO DATA"
        if last is None
        else ("ERROR" if failed else "STALE" if stale else "LIVE"),
        "error": (last["error"] or "Sample unavailable") if failed and last else None,
        "age_s": age,
        "status": status,
        "timestamps": [s["attempted_at"] for s in samples],
        "power_w": [s["status"]["power_w"] if s["status"] else None for s in samples],
        "set_power_w": [s["status"]["set_power_w"] if s["status"] else None for s in samples],
        "count": len(samples),
    }


def create_app(service: Monitor, waveplate: Waveplate | None = None) -> Any:
    """Caller owns service and waveplate lifecycles. Does not connect, start threads or
    send commands; without a waveplate the HWP controls show NOT CONNECTED."""
    from dash import Dash, Input, Output, ctx, dcc, html

    laser = service._controller
    hwp = waveplate if waveplate is not None else Waveplate(None)

    app = Dash(__name__, assets_folder=str(Path(__file__).with_name("assets")))
    app.title = "Verdi | Telemetry"

    def tile(title: str, identifier: str, subtitle: str, subtitle_id: str | None = None) -> Any:
        return html.Div(
            [
                html.Label(title),
                html.Div("—", id=identifier, className="metric"),
                html.Small(subtitle, id=subtitle_id) if subtitle_id else html.Small(subtitle),
            ],
            className="tile",
        )

    app.layout = html.Main(
        [
            html.Header(
                [
                    html.H1("NanoNMR-M Verdi V5 Laser Controller"),
                    html.Div(
                        _source_label(service.snapshot()["simulated"]),
                        id="source",
                        className="badge",
                    ),
                ]
            ),
            html.Div(
                [
                    html.Span("NO DATA", id="health", className="health"),
                    html.Span("Waiting for a sample", id="sample-age"),
                ],
                className="statusbar",
            ),
            html.Div(id="error", role="alert"),
            html.Div(id="connection-warning", role="alert"),
            html.Section(
                [
                    tile("MEASURED POWER", "power", "Reported by controller"),
                    tile("POWER SETPOINT", "setpoint", "Light regulation"),
                    tile("LASER STATE", "laser", "Reported state is not a safety guarantee"),
                    tile("SAFETY SHUTTER", "shutter", "Not an experimental modulator"),
                    tile("HWP ANGLE", "hwp-angle", "K10CR2 rotator", "hwp-angle-detail"),
                ],
                className="tiles",
            ),
            html.Div(
                [
                html.Div(
                    [
                        html.Div(
                            [
                                html.Label(
                                    "Requested power setpoint (W)",
                                    htmlFor="power-setpoint-input",
                                    style={"display": "block", "marginBottom": "0.5rem"},
                                ),
                                dcc.Input(
                                    id="power-setpoint-input",
                                    type="number",
                                    step="any",
                                    placeholder="Enter power",
                                    style={
                                        "marginTop": "0.75rem",
                                        "width": "100%",
                                        "minWidth": "180px",
                                        "maxWidth": "260px",
                                        "boxSizing": "border-box",
                                        "height": "64px",
                                        "padding": "0 0.9rem",
                                        "fontSize": "1rem",
                                        "lineHeight": "normal",
                                        "borderRadius": "10px",
                                        "border": "1px solid #3d4d61",
                                        "backgroundColor": "#0f1725",
                                        "color": "#edf4ff",
                                    },
                                ),
                                html.Button(
                                    "Apply setpoint",
                                    id="apply-power-setpoint",
                                    n_clicks=0,
                                    style={
                                        "marginTop": "0.75rem",
                                        "padding": "0.8rem 1rem",
                                        "fontSize": "0.96rem",
                                        "fontWeight": "600",
                                        "borderRadius": "10px",
                                        "border": "1px solid #516f95",
                                        "backgroundColor": "#1f4870",
                                        "color": "#edf4ff",
                                        "cursor": "pointer",
                                    },
                                ),
                                html.Div(
                                    id="power-setpoint-result",
                                    role="status",
                                    style={"marginTop": "0.75rem", "fontSize": "0.95rem"},
                                ),
                            ],
                            style={
                                "flex": "1 1 280px",
                                "minWidth": "220px",
                                "padding": "0.25rem 0",
                            },
                        ),
                        html.Div(
                            [
                                html.Label(
                                    "Requested enclosure output power (W)",
                                    htmlFor="enclosure-power-input",
                                    style={"display": "block", "marginBottom": "0.5rem"},
                                ),
                                dcc.Input(
                                    id="enclosure-power-input",
                                    type="number",
                                    min=0,
                                    step="any",
                                    placeholder="Enter enclosure power",
                                    style={
                                        "width": "100%",
                                        "minWidth": "180px",
                                        "maxWidth": "260px",
                                        "boxSizing": "border-box",
                                        "height": "64px",
                                        "padding": "0 0.9rem",
                                        "fontSize": "1rem",
                                        "lineHeight": "normal",
                                        "borderRadius": "10px",
                                        "border": "1px solid #3d4d61",
                                        "backgroundColor": "#0f1725",
                                        "color": "#edf4ff",
                                    },
                                ),
                                html.Button(
                                    "Apply enclosure power",
                                    id="apply-enclosure-power",
                                    n_clicks=0,
                                    style={
                                        "marginTop": "0.75rem",
                                        "padding": "0.8rem 1rem",
                                        "fontSize": "0.96rem",
                                        "fontWeight": "600",
                                        "borderRadius": "10px",
                                        "border": "1px solid #516f95",
                                        "backgroundColor": "#1f4870",
                                        "color": "#edf4ff",
                                        "cursor": "pointer",
                                    },
                                ),
                                html.Div(
                                    id="enclosure-power-result",
                                    role="status",
                                    style={"marginTop": "0.75rem", "fontSize": "0.95rem"},
                                ),
                            ],
                            style={
                                "flex": "1 1 280px",
                                "minWidth": "220px",
                                "padding": "0.25rem 0",
                            },
                        ),
                    ],
                    className="setpoint-controls",
                    style={
                    "flex": "2 1 560px",
                        "display": "flex",
                        "flexWrap": "wrap",
                        "gap": "1.25rem",
                        "padding": "1.1rem 1.2rem",
                        "borderRadius": "14px",
                        "backgroundColor": "#111c2a",
                        "border": "1px solid #263548",
                        "boxShadow": "inset 0 1px 0 rgba(255,255,255,0.03)",
                    },
                ),
                html.Div(
                    [
                        html.H2("Half-Waveplate"),
                        html.Div(
                            [
                                html.Label(
                                    "Target angle (0–360°)",
                                    htmlFor="hwp-angle-input",
                                    style={"display": "block"},
                                ),
                                dcc.Input(
                                    id="hwp-angle-input",
                                    type="number",
                                    min=0,
                                    max=360,
                                    step="any",
                                    placeholder="Enter angle",
                                    style={
                                        "width": "100%",
                                        "boxSizing": "border-box",
                                        "height": "64px",
                                        "padding": "0 0.9rem",
                                        "fontSize": "1rem",
                                        "lineHeight": "normal",
                                        "borderRadius": "10px",
                                        "border": "1px solid #3d4d61",
                                        "backgroundColor": "#0f1725",
                                        "color": "#edf4ff",
                                    },
                                ),
                                html.Button(
                                    "Move to angle",
                                    id="hwp-move",
                                    n_clicks=0,
                                    style={
                                        "padding": "0.7rem 1rem",
                                        "fontSize": "0.95rem",
                                        "fontWeight": "600",
                                        "borderRadius": "10px",
                                        "border": "1px solid #516f95",
                                        "backgroundColor": "#1f4870",
                                        "color": "#edf4ff",
                                        "cursor": "pointer",
                                        "width": "100%",
                                    },
                                ),
                                dcc.ConfirmDialogProvider(
                                    html.Button(
                                        "Home",
                                        id="hwp-home",
                                        n_clicks=0,
                                        style={
                                            "padding": "0.7rem 1rem",
                                            "fontSize": "0.95rem",
                                            "fontWeight": "600",
                                            "borderRadius": "10px",
                                            "border": "1px solid #516f95",
                                            "backgroundColor": "#3b3f58",
                                            "color": "#edf4ff",
                                            "cursor": "pointer",
                                            "width": "100%",
                                        },
                                    ),
                                    id="hwp-home-confirm",
                                    message=(
                                        "Home the half-waveplate? The stage rotates to "
                                        "its home position, which changes transmitted power."
                                    ),
                                ),
                                html.Small(
                                    "Moves block until the stage reaches its target "
                                    "(up to 30 s; homing up to 60 s). Buttons are "
                                    "disabled while the stage is moving.",
                                    style={"display": "block"},
                                ),
                            ],
                            style={
                                "display": "flex",
                                "flexDirection": "column",
                                "gap": "0.75rem",
                                "marginTop": "0.75rem",
                            },
                        ),
                        html.Div(
                            id="hwp-result",
                            role="status",
                            style={"marginTop": "0.75rem", "fontSize": "0.95rem"},
                        ),
                    ],
                    className="panel",
                    style={"flex": "1 1 260px", "minWidth": "220px"},
                ),
                ],
                style={
                    "display": "flex",
                    "flexWrap": "wrap",
                    "gap": "1rem",
                    "alignItems": "stretch",
                },
            ),
            html.Div(
                [
                    html.Div(
                        [html.H2("Thermal Diagnostics"), html.Div(id="temperatures")],
                        className="panel",
                        style={"flex": "1 1 360px", "minWidth": "260px"},
                    ),
                    html.Div(
                        [
                            html.H2("Instrument Status"),
                            html.Div(id="instrument"),
                        ],
                        className="panel",
                        style={"flex": "1 1 320px", "minWidth": "260px"},
                    ),
                    html.Div(
                        [
                            html.H2("Laser Controls"),
                            html.Div(
                                [
                                    dcc.ConfirmDialogProvider(
                                        html.Button(
                                            "Start",
                                            id="laser-start",
                                            n_clicks=0,
                                            style={
                                                "padding": "0.7rem 1rem",
                                                "fontSize": "0.95rem",
                                                "fontWeight": "600",
                                                "borderRadius": "10px",
                                                "border": "1px solid #516f95",
                                                "backgroundColor": "#1d8f6f",
                                                "color": "#edf4ff",
                                                "cursor": "pointer",
                                                "width": "100%",
                                            },
                                        ),
                                        id="laser-start-confirm",
                                        message=(
                                            "Start the laser? This begins physical laser "
                                            "emission."
                                        ),
                                    ),
                                    html.Button(
                                        "Open shutter",
                                        id="shutter-open",
                                        n_clicks=0,
                                        style={
                                            "padding": "0.7rem 1rem",
                                            "fontSize": "0.95rem",
                                            "fontWeight": "600",
                                            "borderRadius": "10px",
                                            "border": "2px solid #60a5fa",
                                            "backgroundColor": "#1d4ed8",
                                            "color": "#edf4ff",
                                            "cursor": "pointer",
                                            "width": "100%",
                                        },
                                    ),
                                    html.Button(
                                        "Close shutter",
                                        id="shutter-close",
                                        n_clicks=0,
                                        style={
                                            "padding": "0.7rem 1rem",
                                            "fontSize": "0.95rem",
                                            "fontWeight": "600",
                                            "borderRadius": "10px",
                                            "border": "2px solid #fb923c",
                                            "backgroundColor": "#b45309",
                                            "color": "#edf4ff",
                                            "cursor": "pointer",
                                            "width": "100%",
                                        },
                                    ),
                                    dcc.ConfirmDialogProvider(
                                        html.Button(
                                            "Stop",
                                            id="laser-stop",
                                            n_clicks=0,
                                            style={
                                                "padding": "0.7rem 1rem",
                                                "fontSize": "0.95rem",
                                                "fontWeight": "600",
                                                "borderRadius": "10px",
                                                "border": "1px solid #516f95",
                                                "backgroundColor": "#7c2d2d",
                                                "color": "#edf4ff",
                                                "cursor": "pointer",
                                                "width": "100%",
                                            },
                                        ),
                                        id="laser-stop-confirm",
                                        message="Stop the laser?",
                                    ),
                                    html.Small(
                                        "Diode current and measured power are not reliable "
                                        "emission indicators -- confirmed on real hardware "
                                        "2026-10-01. With key ON + laser OFF, diode current "
                                        "from the panel and from this GUI (RS-232) are "
                                        "consistently INVERTED relative to shutter state, "
                                        "with the RS-232 reading lagging a shutter change by "
                                        "~0.5s. Measured power did not distinguish 0 mW from "
                                        "a confirmed 45.8 mW reading at low test power. Use "
                                        "an external power meter to confirm actual emission.",
                                        style={"display": "block"},
                                    ),
                                ],
                                style={
                                    "display": "flex",
                                    "flexDirection": "column",
                                    "gap": "0.75rem",
                                    "marginTop": "0.75rem",
                                },
                            ),
                            html.Div(
                                id="control-result",
                                role="status",
                                style={"marginTop": "0.75rem", "fontSize": "0.95rem"},
                            ),
                        ],
                        className="panel",
                        style={"flex": "0 1 260px", "minWidth": "220px"},
                    ),
                ],
                style={
                    "display": "flex",
                    "flexWrap": "wrap",
                    "gap": "1rem",
                    "alignItems": "stretch",
                    "marginTop": "1rem",
                },
            ),
            html.Section(
                [
                    html.H2("Power history"),
                    dcc.Graph(id="power-graph", config={"displayModeBar": False}),
                ],
                className="panel",
                style={"marginTop": "1rem"},
            ),
            html.Footer("Monitoring only · physical behavior untested"),
            dcc.Interval(id="refresh", interval=300, n_intervals=0),
            html.Span(id="server-heartbeat", hidden=True),
        ],
        id="dashboard",
    )

    @app.callback(
        Output("health", "children"),
        Output("sample-age", "children"),
        Output("error", "children"),
        Output("power", "children"),
        Output("setpoint", "children"),
        Output("laser", "children"),
        Output("shutter", "children"),
        Output("power-graph", "figure"),
        Output("temperatures", "children"),
        Output("instrument", "children"),
        Output("server-heartbeat", "children"),
        Output("source", "children"),
        Input("refresh", "n_intervals"),
    )
    def refresh(_tick: int) -> tuple[Any, ...]:
        data = dashboard_data(service)
        s = data["status"]
        figure = {
            "data": [
                {
                    "type": "scatter",
                    "x": data["timestamps"],
                    "y": data[field],
                    "name": name,
                    "mode": "lines",
                    "line": line,
                    "connectgaps": False,
                }
                for field, name, line in (
                    ("power_w", "Measured power", {"color": "#65e3cc", "width": 2.5}),
                    ("set_power_w", "Setpoint", {"color": "#8999b1", "dash": "dot"}),
                )
            ],
            "layout": {
                "paper_bgcolor": "#151c28",
                "plot_bgcolor": "#151c28",
                "margin": {"l": 52, "r": 24, "t": 12, "b": 38},
                "height": 290,
                "font": {"family": "Segoe UI, sans-serif", "color": "#acb9ce"},
                "xaxis": {"title": {"text": "Time (UTC)"}, "gridcolor": "#283442"},
                "yaxis": {"title": {"text": "Power / W"}, "gridcolor": "#283442"},
                "legend": {"orientation": "h", "y": 1.14},
                "uirevision": "verdi-power",
            },
        }

        def row(label: str, value: str) -> Any:
            return html.Div([html.Span(label), html.Strong(value)], className="data-row")

        thermal = [
            row(label, f"{s[field]:.2f} °C" if s else "—")
            for label, field in [
                ("Diode 1", "diode_temp_c"),
                ("Heatsink", "heatsink_temp_c"),
                ("Baseplate", "baseplate_temp_c"),
                ("LBO", "lbo_temp_c"),
                ("Etalon", "etalon_temp_c"),
                ("Vanadate", "vanadate_temp_c"),
            ]
        ]
        instrument = [
            row("Configured model", data["model"]),
            row("Keyswitch", ("ON" if s["keyswitch_on"] else "OFF") if s else "—"),
            # Confirmed on real hardware 2026-10-01 (0.01-0.05 W tested): with key
            # ON + laser OFF, this and the front panel's own current reading are
            # consistently INVERTED relative to shutter state, with this (RS-232)
            # reading lagging a shutter change by ~0.5s. Not a reliable emission
            # indicator -- use an external power meter for that.
            row("Diode current", f"{s['diode_current_a']:.2f} A" if s else "—"),
            row("LBO servo", str(s["lbo_servo"]) if s else "—"),
            row(
                "Reported faults",
                ", ".join(map(str, s["faults"])) or "None reported" if s else "Unknown",
            ),
            row("History samples", str(data["count"])),
        ]
        age = (
            f"Sample age {data['age_s']:.1f} s"
            if data["age_s"] is not None
            else "No current sample"
        )
        return (
            data["health"],
            age,
            data["error"] or "",
            _with_unit(f"{s['power_w']:.3f}", " W") if s else "—",
            _with_unit(f"{s['set_power_w']:.4f}", " W") if s else "—",
            _colored(("STANDBY", "ON", "FAULT")[s["laser_state"]], LASER_STATE_COLORS)
            if s
            else "UNKNOWN",
            _colored("OPEN" if s["shutter_open"] else "CLOSED", SHUTTER_COLORS)
            if s
            else "UNKNOWN",
            figure,
            thermal,
            instrument,
            _tick,
            _source_label(data["simulated"]),
        )

    @app.callback(
        Output("power-setpoint-result", "children"),
        Input("apply-power-setpoint", "n_clicks"),
        State("power-setpoint-input", "value"),
        prevent_initial_call=True,
    )
    def apply_power_setpoint(_clicks: int, value: object) -> str:
        try:
            laser.set_power_w(value)
        except (ValueError, OSError, PermissionError) as exc:
            return f"Rejected / failed: {exc}"
        # Full re-poll, not a narrow patch: if the laser is ON, a setpoint
        # change also moves measured power and diode current as the servo
        # settles, so only patching set_power_w would leave those stale.
        service.poll_once()
        return "Setpoint request completed; monitor readback will show the reported value."

    @app.callback(
        Output("enclosure-power-result", "children"),
        Input("apply-enclosure-power", "n_clicks"),
        State("enclosure-power-input", "value"),
        prevent_initial_call=True,
    )
    def apply_enclosure_power(_clicks: int, value: object) -> str:
        snapshot = service.snapshot()
        samples = snapshot["history"]
        sample = samples[-1] if samples else None
        status = sample["status"] if sample else None
        age_s = snapshot["age_s"]

        if status is None or age_s is None:
            return "Cannot set enclosure power: no valid laser-head power reading."
        if age_s > max(5.0, 3 * snapshot["interval_s"] + 2 * status["duration_s"]):
            return "Cannot set enclosure power: laser-head power reading is stale."

        try:
            head_power_w = finite_range(
                status["power_w"], 0, float("inf"), "measured head power"
            )
            target_w = finite_range(value, 0, head_power_w, "enclosure power")
            theta_deg = hwp.calibration.angle_for_power(target_w, head_power_w)

            stage = getattr(laser, "rotation_stage", None)
            if stage is None:
                stage = getattr(laser, "stage", None)

            if stage is not None and hasattr(stage, "move_to_position"):
                stage.move_to_position(theta_deg)

            return (
                f"Stage angle: {theta_deg:.2f}° "
                f"(requested enclosure power {target_w:.3f} W)."
            )
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            return f"Rejected / failed: {exc}"

    @app.callback(
        Output("control-result", "children"),
        Input("laser-start-confirm", "submit_n_clicks"),
        Input("shutter-open", "n_clicks"),
        Input("shutter-close", "n_clicks"),
        Input("laser-stop-confirm", "submit_n_clicks"),
        prevent_initial_call=True,
    )
    def apply_control(
        _start_clicks: int,
        _open_clicks: int,
        _close_clicks: int,
        _stop_clicks: int,
    ) -> str:
        # Full re-poll after every action: Start/Stop/shutter can each move
        # several fields at once (laser state, diode current, measured power),
        # so a narrow single-field patch risks showing an inconsistent mix of
        # fresh and stale values. Correctness here matters more than shaving
        # off the last bit of latency.
        try:
            if ctx.triggered_id == "laser-start-confirm":
                # Objectively software-checkable preconditions from
                # HARDWARE_VALIDATION.md TEST-010F. This does not replace the
                # operator's own LBO warmup/servo-lock readiness confirmation --
                # it only refuses the two unambiguous cases: active faults, or
                # the shutter not already closed before enable.
                pre = laser.status()
                if pre["faults"]:
                    return f"Refused: active faults reported: {pre['faults']}. Do not enable."
                if pre["shutter_open"]:
                    return "Refused: shutter is open. Close it before enabling the laser."
                laser.start()
                service.poll_once()
                return "Laser start command completed."
            if ctx.triggered_id == "shutter-open":
                laser.set_shutter(open=True)
                service.poll_once()
                return "Shutter open command completed."
            if ctx.triggered_id == "shutter-close":
                laser.set_shutter(open=False)
                service.poll_once()
                return "Shutter close command completed."
            if ctx.triggered_id == "laser-stop-confirm":
                laser.stop()
                service.poll_once()
                return "Laser stop command completed."
        except Exception as exc:
            return f"Command failed: {exc}"

        return "No control command selected."

    @app.callback(
        Output("hwp-angle", "children"),
        Output("hwp-angle-detail", "children"),
        Output("hwp-move", "disabled"),
        Output("hwp-home", "disabled"),
        Input("refresh", "n_intervals"),
    )
    def refresh_waveplate(_tick: int) -> tuple[Any, ...]:
        # Cached state only; the caller's poll loop owns stage reads.
        w = hwp.snapshot()
        locked = not w["connected"] or bool(w["busy"])
        if not w["connected"]:
            return (
                html.Span("NOT CONNECTED", style={"color": "#fb923c", "fontSize": "0.6em"}),
                w["error"] or "Rotator unavailable",
                True,
                True,
            )
        angle = (
            _with_unit(f"{w['angle_deg']:.2f}", "°") if w["angle_deg"] is not None else "—"
        )
        if w["busy"]:
            # Yellow while moving/homing; idle reverts to the default (white) text.
            angle = html.Span(angle, style={"color": HWP_MOVING_COLOR})
            detail: Any = html.Span(f"Stage {w['busy']}…", style={"color": "#3b82f6"})
        elif w["error"]:
            detail = html.Span(f"Read error: {w['error']}", style={"color": "#f87171"})
        elif w["homed"] is False:
            detail = html.Span(
                "NOT HOMED — home before trusting angle", style={"color": "#fb923c"}
            )
        elif w["homed"]:
            detail = "Homed · K10CR2 rotator"
        else:
            detail = "K10CR2 rotator"
        return angle, detail, locked, locked

    @app.callback(
        Output("hwp-result", "children"),
        Input("hwp-move", "n_clicks"),
        Input("hwp-home-confirm", "submit_n_clicks"),
        State("hwp-angle-input", "value"),
        prevent_initial_call=True,
    )
    def apply_waveplate(_move_clicks: int, _home_clicks: int, value: object) -> str:
        try:
            if ctx.triggered_id == "hwp-move":
                angle = hwp.move_to(value)
                return f"Move to {angle:.2f}° completed."
            if ctx.triggered_id == "hwp-home-confirm":
                hwp.home()
                return "Homing completed."
        except Exception as exc:
            return f"Rejected / failed: {exc}"
        return "No waveplate command selected."

    return app
