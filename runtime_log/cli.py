"""Command line: serve | import | replay | report | gc | list."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .archive import Archive
from .ethercat import EthercatPoller
from .offline import import_launch_dir, replay, resolve_run, text_summary
from .profile import load_profile
from .recorder import load_run
from .report import write_report

DEFAULT_PROFILE = Path(__file__).resolve().parent.parent / "profiles" / "rt_control.yaml"
DEFAULT_ARCHIVE = Path(os.environ.get("RUNTIME_LOG_ARCHIVE", "~/runtime-log/runs")).expanduser()


def _hz_pair(text: str) -> tuple[float, float]:
    parts = [float(part) for part in text.split(",")]
    if len(parts) == 1:
        parts *= 2
    if len(parts) != 2 or min(parts) < 0:
        raise argparse.ArgumentTypeError("use STARTUP_HZ,STEADY_HZ, e.g. 5,1 (0 disables)")
    return parts[0], parts[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="runtime-log", description="Run recorder for RT-Control (passive, read-only).")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--retention-days", type=float, default=7.0)
    parser.add_argument("--min-free-gb", type=float, default=10.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="record runs and serve the read-only index")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=18200)
    serve.add_argument("--no-web", action="store_true")
    serve.add_argument("--ethercat-hz", type=_hz_pair, help="master poll rate STARTUP,STEADY (default from profile; 0 disables)")
    serve.add_argument("--proc-hz", type=float, default=1.0)

    imp = commands.add_parser("import", help="archive a finished ROS launch log directory")
    imp.add_argument("log_dirs", nargs="+", type=Path)
    imp.add_argument("--scope", default="")
    imp.add_argument("--domain", default="?")
    imp.add_argument("--hardware", choices=["ethercat", "mock", ""], default="")
    imp.add_argument("--kernel-jsonl", type=Path, help="kernel journal JSON lines instead of calling journalctl")

    rep = commands.add_parser("replay", help="re-classify an archived run with the current profile and print the result")
    rep.add_argument("run")

    report = commands.add_parser("report", help="rewrite report.html of a run")
    report.add_argument("run")

    trig = commands.add_parser("trigger", help="save a black-box window now (all running recorders)")
    trig.add_argument("reason", nargs="?", default="manual")

    exp = commands.add_parser("experiment", help="start/stop a manual 1 kHz experiment recording")
    exp.add_argument("action", choices=["start", "stop"])
    exp.add_argument("name", nargs="?", default="manual")

    commands.add_parser("gc", help="apply retention and the disk-space guard now")
    commands.add_parser("list", help="print the run index")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    archive = Archive(args.archive, args.retention_days, args.min_free_gb)
    profile = load_profile(args.profile)

    if args.command == "serve":
        from .service import Service

        keep_off_isolated_cpus()
        from .web import serve as serve_web

        settings = profile.ethercat
        poller = EthercatPoller(master=int(settings.get("master", 0)), binary=settings.get("binary", "ethercat"))

        def on_finish(document: dict) -> None:
            run_dir = archive.root / document["id"]
            write_report(run_dir, *load_run(run_dir))

        if not args.no_web:
            serve_web(archive, args.host, args.port)
        Service(profile, archive, poller=poller, ethercat_hz=args.ethercat_hz, proc_hz=args.proc_hz, on_finish=on_finish).run_forever()
        return 0

    if args.command == "import":
        kernel_lines = args.kernel_jsonl.read_text().splitlines() if args.kernel_jsonl else None
        status = 0
        for log_dir in args.log_dirs:
            try:
                document = import_launch_dir(log_dir.expanduser(), profile, archive, args.scope, args.domain, args.hardware, kernel_lines)
                print(text_summary(document))
            except (OSError, ValueError) as error:
                print(f"skip {log_dir}: {error}", file=sys.stderr)
                status = 1
        return status

    if args.command == "replay":
        document, _ = replay(resolve_run(archive, args.run), profile)
        archive.update_index()
        print(text_summary(document))
        return 0

    if args.command == "report":
        run_dir = resolve_run(archive, args.run)
        print(write_report(run_dir, *load_run(run_dir)))
        return 0

    if args.command == "trigger":
        import time as _time

        control = archive.root.parent / "control"
        control.mkdir(parents=True, exist_ok=True)
        path = control / f"trigger-{_time.time():.3f}"
        path.write_text(args.reason, encoding="utf-8")
        print(f"requested: {path} (the service picks it up within 1 s)")
        return 0

    if args.command == "experiment":
        import time as _time

        control = archive.root.parent / "control"
        control.mkdir(parents=True, exist_ok=True)
        path = control / f"experiment-{args.action}-{_time.time():.3f}"
        path.write_text(args.name, encoding="utf-8")
        print(f"requested experiment {args.action} '{args.name}' (picked up within 1 s)")
        return 0

    if args.command == "gc":
        removed = archive.collect_garbage()
        archive.update_index()
        print(f"removed {len(removed)} runs" + (": " + ", ".join(removed) if removed else ""))
        return 0

    if args.command == "list":
        for row in archive.update_index():
            print(f'{row["id"]:<48} {row.get("result") or "":<11} {row.get("hardware") or "":<9} ready={row.get("ready_s")} anomalies={row.get("anomaly_total")} {row.get("failed_stage") or ""}')
        return 0
    return 2


def keep_off_isolated_cpus() -> None:
    """Run the recorder (and every thread/child it starts) only on non-isolated CPUs."""
    from .procfs import ProcFS, format_cpu_list

    isolated = ProcFS().isolated_cpus()
    allowed = os.sched_getaffinity(0) - isolated
    if isolated and allowed:
        os.sched_setaffinity(0, allowed)
        logging.getLogger("runtime_log").info("CPU affinity %s (isolated %s excluded)", format_cpu_list(allowed), format_cpu_list(isolated))


if __name__ == "__main__":
    sys.exit(main())
