"""Read-only simulator CLI. Hardware operations belong in explicitly configured scripts."""

import argparse
import json
import sys
from collections.abc import Sequence
from time import sleep

from .controller import QUERIES, VerdiError
from .simulator import SimulatedVerdi


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["V2", "V5", "V6"], default="V5")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "diagnostics"):
        commands.add_parser(name)
    commands.add_parser("query").add_argument("query", choices=sorted(QUERIES))
    watch = commands.add_parser("watch")
    watch.add_argument("--count", type=int, default=10)
    watch.add_argument("--interval", type=float, default=1.0)
    commands.add_parser("gui").add_argument("--port", type=int, default=8050)
    args = parser.parse_args(argv)
    try:
        with SimulatedVerdi(args.model) as laser:
            if args.command in ("status", "diagnostics", "query"):
                result = (
                    laser.status()
                    if args.command == "status"
                    else laser.read_diagnostics()
                    if args.command == "diagnostics"
                    else laser.read(args.query)
                )
                print(json.dumps(result, indent=2, allow_nan=False))
                return 0

            from .gui import Monitor, create_app
            from .k10cr2_driver import K10CR2

            monitor = Monitor(laser)
            with K10CR2("55543994") as stage:  # use your stage's serial number
                app = create_app(
                    monitor,
                    stage,
                    waveplate_reference_deg=YOUR_CALIBRATED_REFERENCE_DEG,
                )
                stop = Event()

                def poll() -> None:
                    while not stop.is_set():
                        monitor.poll_once()
                        stop.wait(monitor.interval_s)

                worker = Thread(target=poll, name="verdi-monitor")
                worker.start()
                try:
                    app.run(
                        host="127.0.0.1",
                        port=args.port,
                        debug=False,
                        use_reloader=False,
                    )
                finally:
                    stop.set()
                    worker.join()  # Drain acquisition before disconnect, including Ctrl+C.
        return 0
    except KeyboardInterrupt:
        return 130
    except (VerdiError, ValueError, ImportError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
