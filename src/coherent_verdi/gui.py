"""Optional caller-scheduled monitoring and Dash client; no implicit worker."""

from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

from dash import Dash, Input, Output, State, dcc, html

from .controller import VerdiController, finite_range


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


def _source_label(simulated: bool) -> str:
    return "SIMULATOR" if simulated else "PHYSICAL / UNVALIDATED"


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


def create_app(service: Monitor) -> Any:
    """Caller owns service lifecycle. Does not connect, start threads or send commands."""
    from dash import Dash, Input, Output, ctx, dcc, html

    laser = service._controller

    app = Dash(__name__, assets_folder=str(Path(__file__).with_name("assets")))
    app.title = "Verdi | Telemetry"

    def tile(title: str, identifier: str, subtitle: str) -> Any:
        return html.Div(
            [
                html.Label(title),
                html.Div("—", id=identifier, className="metric"),
                html.Small(subtitle),
            ],
            className="tile",
        )

    app.layout = html.Main(
        [
            html.Header(
                [
                    html.H1("Verdi / Telemetry"),
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
                    tile("MEASURED POWER", "power", "W · reported by controller"),
                    tile("POWER SETPOINT", "setpoint", "W · light regulation"),
                    tile("LASER STATE", "laser", "Reported state is not a safety guarantee"),
                    tile("SAFETY SHUTTER", "shutter", "Not an experimental modulator"),
                ],
                className="tiles",
            ),
            html.Div(
                [
                    html.Label("Requested power setpoint (W)", htmlFor="power-setpoint-input"),
                    dcc.Input(
                        id="power-setpoint-input",
                        type="number",
                        step="any",
                        placeholder="Enter power",
                    ),
                    html.Button(
                        "Apply setpoint",
                        id="apply-power-setpoint",
                        n_clicks=0,
                    ),
                    html.Div(id="power-setpoint-result", role="status"),
                ],
                className="setpoint-controls",
            ),
            html.Section(
                [
                    html.H2("Power history"),
                    dcc.Graph(id="power-graph", config={"displayModeBar": False}),
                ],
                className="panel",
            ),
            html.Section(
                [
                    html.Div(
                        [html.H2("Thermal diagnostics"), html.Div(id="temperatures")],
                        className="panel",
                    ),
                    html.Div(
                        [html.H2("Instrument status"), html.Div(id="instrument")],
                        className="panel",
                    ),
                    html.Div(
                        [
                            html.H2("Laser controls"),
                            html.Button("Start", id="laser-start", n_clicks=0),
                            html.Button("Open shutter", id="shutter-open", n_clicks=0),
                            html.Button("Close shutter", id="shutter-close", n_clicks=0),
                            html.Button("Stop", id="laser-stop", n_clicks=0),
                            html.Div(id="control-result", role="status"),
                        ],
                        className="panel",
                    ),
                ],
                className="lower",
            ),
            html.Footer("Monitoring only · physical behavior untested"),
            dcc.Interval(id="refresh", interval=1000, n_intervals=0),
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
            row("Diode current", f"{s['diode_current_a']:.1f} A" if s else "—"),
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
            f"{s['power_w']:.3f}" if s else "—",
            f"{s['set_power_w']:.4f}" if s else "—",
            ("STANDBY", "ON", "FAULT")[s["laser_state"]] if s else "UNKNOWN",
            ("OPEN" if s["shutter_open"] else "CLOSED") if s else "UNKNOWN",
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
        except (ValueError, OSError) as exc:
            return f"Rejected / failed: {exc}"
        return "Setpoint request completed; monitor readback will show the reported value."

    @app.callback(
        Output("control-result", "children"),
        Input("laser-start", "n_clicks"),
        Input("shutter-open", "n_clicks"),
        Input("shutter-close", "n_clicks"),
        Input("laser-stop", "n_clicks"),
        prevent_initial_call=True,
    )
    def apply_control(
        _start_clicks: int,
        _open_clicks: int,
        _close_clicks: int,
        _stop_clicks: int,
    ) -> str:
        try:
            if ctx.triggered_id == "laser-start":
                laser.start()
                return "Laser start command completed."
            if ctx.triggered_id == "shutter-open":
                laser.set_shutter(open=True)
                return "Shutter open command completed."
            if ctx.triggered_id == "shutter-close":
                laser.set_shutter(open=False)
                return "Shutter close command completed."
            if ctx.triggered_id == "laser-stop":
                laser.stop()
                return "Laser stop command completed."
        except Exception as exc:
            return f"Command failed: {exc}"

        return "No control command selected."

    return app
