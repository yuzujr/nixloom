"""The NixLoom command-line control surface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__, comfy, dsh, operations, runtime, sillytavern
from .config import Config, ConfigError, RuntimePaths

UNIT_NAMES = {
    "runtime": "nixloom-runtime.service",
    "comfyui": "nixloom-runtime.service",
    "dsh": "nixloom-dsh.service",
    "sillytavern": "nixloom-sillytavern.service",
    "all": "nixloom.target",
}


class GNUHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Render argparse help like a conventional GNU command."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("max_help_position", 30)
        super().__init__(*args, **kwargs)

    def start_section(self, heading: str | None) -> None:
        headings = {
            "positional arguments": "Arguments",
            "options": "Options",
            "commands": "Commands",
        }
        super().start_section(headings.get(heading, heading))

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "Usage:", 1)


class GNUArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("formatter_class", GNUHelpFormatter)
        # Python 3.14 colors argparse help by default. Keep output stable when
        # redirected, logged, tested, or run with an older Python release.
        if sys.version_info >= (3, 14):
            kwargs.setdefault("color", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        if message.startswith("argument COMMAND: invalid choice: "):
            choice = message.removeprefix("argument COMMAND: invalid choice: ").split(
                " ", 1
            )[0]
            message = f"unknown command {choice}"
        self.exit(
            2,
            f"{self.prog}: {message}\nTry '{self.prog} --help' for more information.\n",
        )


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    unit: str
    url: str
    accepted: frozenset[int] = frozenset({200})


@dataclass(frozen=True)
class ServiceReport:
    spec: ServiceSpec
    active: str
    sub: str
    health: str

    @property
    def state(self) -> str:
        if self.active == "failed":
            return "failed"
        if self.active in {"activating", "reloading"} or self.sub in {
            "auto-restart",
            "start",
        }:
            return "starting"
        if self.active != "active":
            return "stopped"
        return "ready" if self.health == "ok" else "unhealthy"


def _context(config_path: str | None = None) -> tuple[RuntimePaths, Config]:
    paths = RuntimePaths.from_environment(config_path)
    return paths, Config.load(paths)


def _unit_installed(name: str) -> bool:
    return (
        subprocess.run(
            ["systemctl", "--user", "cat", UNIT_NAMES[name]],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _probe(
    url: str,
    accepted: frozenset[int] = frozenset({200}),
    *,
    timeout: float = 3,
) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return "ok" if response.status in accepted else f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return "ok" if error.code in accepted else f"HTTP {error.code}"
    except (OSError, urllib.error.URLError):
        return "down"


def _service_specs(config: Config) -> list[ServiceSpec]:
    llama_port = config.integer("ports.llama", minimum=1)
    specs = [
        ServiceSpec(
            "runtime",
            UNIT_NAMES["runtime"],
            f"http://127.0.0.1:{llama_port}/health",
        )
    ]
    if _unit_installed("dsh"):
        port = config.integer("ports.dsh", 3080, minimum=1)
        specs.append(
            ServiceSpec(
                "dsh",
                UNIT_NAMES["dsh"],
                f"http://127.0.0.1:{port}/",
            )
        )
    if _unit_installed("sillytavern"):
        port = config.integer("ports.sillytavern", minimum=1)
        specs.append(
            ServiceSpec(
                "sillytavern",
                UNIT_NAMES["sillytavern"],
                f"http://127.0.0.1:{port}/",
            )
        )
    if config.boolean("images.enabled"):
        specs.append(
            ServiceSpec(
                "comfyui",
                UNIT_NAMES["runtime"],
                f"http://127.0.0.1:{config.integer('ports.comfyui', 8188, minimum=1)}/",
            )
        )
    return specs


def _unit_state(unit: str) -> tuple[str, str]:
    result = operations.systemctl(
        "show", unit, "-p", "ActiveState", "-p", "SubState", check=False
    )
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    return values.get("ActiveState", "unknown"), values.get("SubState", "unknown")


def _service_report(spec: ServiceSpec, *, timeout: float = 3) -> ServiceReport:
    active, sub = _unit_state(spec.unit)
    health = (
        _probe(spec.url, spec.accepted, timeout=timeout)
        if active == "active"
        else "down"
    )
    return ServiceReport(spec, active, sub, health)


def _collect_reports(config: Config, *, timeout: float = 3) -> list[ServiceReport]:
    return [_service_report(spec, timeout=timeout) for spec in _service_specs(config)]


def _running_models(config: Config) -> list[str]:
    port = config.integer("ports.llama", minimum=1)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/running", timeout=3
        ) as response:
            document = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return []
    running = document.get("running", []) if isinstance(document, dict) else []
    return [
        f"{item['model']} ({item.get('state', 'unknown')})"
        for item in running
        if isinstance(item, dict) and isinstance(item.get("model"), str)
    ]


def _configured_model_ready(config: Config) -> bool:
    expected = f"{config.string('llm.id')} (ready)"
    return expected in _running_models(config)


def _wait_for_endpoint(spec: ServiceSpec, *, timeout: int) -> None:
    started = time.monotonic()
    announced = False
    while time.monotonic() - started < timeout:
        report = _service_report(spec, timeout=1)
        if report.health == "ok":
            elapsed = time.monotonic() - started
            if announced:
                print(f" ready ({elapsed:.1f}s)", flush=True)
            return
        if report.state == "failed":
            if announced:
                print(" failed", flush=True)
            raise ConfigError(
                f"{spec.name} service failed; inspect `nixloom logs {spec.name}`"
            )
        if not announced and time.monotonic() - started >= 2:
            print(f"Waiting for {spec.name}...", end="", flush=True)
            announced = True
        time.sleep(1)
    if announced:
        print(" timeout", flush=True)
    raise ConfigError(
        f"{spec.name} did not become healthy within {timeout}s; "
        f"inspect `nixloom logs {spec.name}`"
    )


def _warm_model(config: Config) -> None:
    model = config.string("llm.id")
    port = config.integer("ports.llama", minimum=1)
    print(f"  {model:<12} loading", end="", flush=True)
    started = time.monotonic()
    operations._json_request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        900,
    )
    print(f" → loaded ({time.monotonic() - started:.1f}s)", flush=True)


def _print_ready_endpoints(reports: list[ServiceReport], config: Config) -> None:
    for report in reports:
        if report.state == "ready":
            endpoint = report.spec.url.removesuffix("/health")
            print(f"  {report.spec.name:<12} {endpoint}")
            if report.spec.name == "dsh":
                url = dsh.tailnet_url(config)
                if url:
                    print(f"  {'tailnet':<12} {url}")


def command_start(args: argparse.Namespace) -> int:
    paths, config = _context(args.config)
    verb = "restart" if args.restart else "start"
    specs = _service_specs(config)
    if args.dry_run:
        print(f"Plan: {verb} NixLoom")
        print("  services: " + ", ".join(spec.name for spec in specs))
        print(f"  model:    {config.string('llm.id')} (warm after start)")
        print("  network:  no downloads")
        print("Dry run: nothing changed.")
        return 0
    if not args.restart:
        current = _collect_reports(config)
        if (
            current
            and all(report.state == "ready" for report in current)
            and _configured_model_ready(config)
        ):
            print(f"NixLoom ready · {config.string('llm.id')}")
            _print_ready_endpoints(current, config)
            return 0
    action = "Restarting" if args.restart else "Starting"
    if any(spec.name == "dsh" for spec in specs):
        dsh.require_installed(config, paths)
    print(f"{action} NixLoom...", flush=True)
    operations.systemctl(verb, "nixloom.target")
    # Starting an already-active target does not retry a failed Wanted unit.
    # Explicitly start the selected services so `nixloom start` also repairs a
    # partially failed stack.
    operations.systemctl("start", *(spec.unit for spec in specs))
    _wait_for_endpoint(specs[0], timeout=120)
    _warm_model(config)
    for spec in specs[1:]:
        _wait_for_endpoint(spec, timeout=60)
    reports = _collect_reports(config)
    unhealthy = [report.spec.name for report in reports if report.state != "ready"]
    if unhealthy:
        raise ConfigError("services are not ready: " + ", ".join(unhealthy))
    print("\nNixLoom ready")
    _print_ready_endpoints(reports, config)
    return 0


def command_status(args: argparse.Namespace) -> int:
    _, config = _context(args.config)
    reports = _collect_reports(config)
    ready = all(report.state == "ready" for report in reports)
    stopped = all(report.state == "stopped" for report in reports)
    models = _running_models(config) if reports and reports[0].state == "ready" else []
    model = ", ".join(models) if models else "unloaded"
    state = "ready" if ready else "stopped" if stopped else "degraded"
    print(f"NixLoom {state} · model: {model}\n")
    print(f"{'SERVICE':<13} {'STATE':<11} URL")
    for report in reports:
        url = report.spec.url.removesuffix("/health")
        print(f"{report.spec.name:<13} {report.state:<11} {url}")
    url = dsh.tailnet_url(config)
    if url:
        print(f"\nTailnet: {url}")
    if getattr(args, "verbose", False):
        print(f"\n{'SERVICE':<13} {'SYSTEMD':<18} HEALTH")
        for report in reports:
            print(
                f"{report.spec.name:<13} {report.active + '/' + report.sub:<18} {report.health}"
            )
    if not ready:
        failures = [
            report
            for report in reports
            if report.state != "ready" and report.state != "stopped"
        ]
        print(
            "\n"
            + (
                f"Logs: nixloom logs {failures[0].spec.name}"
                if failures
                else "Start: nixloom start"
            )
        )
    return 0 if ready else 1


def command_stop(args: argparse.Namespace) -> int:
    _, config = _context(args.config)
    reports = [_service_report(spec, timeout=1) for spec in _service_specs(config)]
    if reports and all(report.state == "stopped" for report in reports):
        print("NixLoom is already stopped.")
        return 0
    print("Stopping NixLoom...", flush=True)
    operations.systemctl(
        "stop", *(spec.unit for spec in reversed(_service_specs(config)))
    )
    operations.systemctl("stop", "nixloom.target")
    print("NixLoom stopped.")
    return 0


def command_logs(args: argparse.Namespace) -> None:
    if args.service == "all":
        _, config = _context(args.config)
        units = [spec.unit for spec in _service_specs(config)]
    else:
        units = [UNIT_NAMES[args.service]]
    command = ["journalctl", "--user", "--lines", str(args.lines)]
    for unit in units:
        command.extend(["-u", unit])
    command.append("--follow" if args.follow else "--no-pager")
    os.execvp(command[0], command)


def command_config(args: argparse.Namespace) -> None:
    if args.config_action is None:
        args.help_parser.print_help()
        return
    paths = RuntimePaths.from_environment(args.config)
    if args.config_action == "init":
        target = paths.config_dir / "config.yaml"
        if target.exists():
            print(f"Config already exists: {target}")
            return
        source = (paths.share / "config.yaml") if paths.share else paths.config_file
        if not source.is_file():
            raise ConfigError(f"packaged config template not found: {source}")
        if args.dry_run:
            print(f"copy {source} -> {target} (0600)")
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, target)
        target.chmod(0o600)
        print(f"Created {target}")
        return
    config = Config.load(paths)
    llama = runtime.llama_command(config, paths)
    swap, document = runtime.swap_command(config, paths)
    if "sillytavern" in config.value:
        sillytavern.command(config)
    if "dsh" in config.value:
        dsh.managed_settings(config)
    if args.verbose:
        print(runtime.render_command(llama))
        if config.boolean("images.enabled"):
            print(runtime.render_command(comfy.command(config, paths)))
        print(document.rstrip())
        print(runtime.render_command(swap))
    print("Config and generated launch commands are valid.")


def command_service(args: argparse.Namespace) -> None:
    paths, config = _context(args.config)
    if args.service_name == "runtime":
        command, document = runtime.swap_command(config, paths)
        if args.dry_run:
            print(document.rstrip())
            print(runtime.render_command(command))
            return
        from . import gateway

        gateway.run(config, paths)
    elif args.service_name == "llama":
        command = runtime.llama_command(config, paths, host=args.host, port=args.port)
        if args.dry_run:
            print(runtime.render_command(command))
            return
        runtime.check_assets(command, config, paths)
        runtime.execute(command)
    elif args.service_name == "comfyui":
        comfy.run(config, paths, dry_run=args.dry_run)
    elif args.service_name == "sillytavern":
        sillytavern.run(config, paths, dry_run=args.dry_run)
    elif args.service_name == "dsh":
        if args.prepare_only:
            dsh.install(config, paths, dry_run=args.dry_run)
        else:
            dsh.run(config, paths, dry_run=args.dry_run)


def command_dsh(args: argparse.Namespace) -> None:
    paths, config = _context(args.config)
    dsh.invoke(config, paths, args.arguments)


def command_backup(args: argparse.Namespace) -> None:
    paths, config = _context(args.config)
    destination = Path(
        args.destination
        or os.environ.get("NIXLOOM_BACKUP_DIR", Path.home() / "backups/nixloom")
    )
    running = (
        operations.systemctl(
            "is-active", "--quiet", "nixloom.target", check=False
        ).returncode
        == 0
    )
    if args.dry_run:
        print(f"backup config, DSH and SillyTavern state -> {destination}")
        print(f"temporarily stop running stack: {str(running).lower()}")
        return
    try:
        if running:
            print("Stopping NixLoom for a consistent snapshot...", flush=True)
            operations.systemctl("stop", "nixloom.target")
        print(f"Creating backup in {destination}...", flush=True)
        archive = operations.create_backup(config, paths, destination)
    finally:
        if running:
            print("Restoring NixLoom services...", flush=True)
            operations.systemctl("start", "nixloom.target")
    print(f"backup written: {archive}")


def command_test(args: argparse.Namespace) -> None:
    paths, config = _context(args.config)
    checks = ["chat", "reasoning", "vision"]
    if config.boolean("images.enabled") and not args.skip_image:
        checks.extend(["image generation", "image editing", "swap-back"])
    print("Running live checks: " + ", ".join(checks), flush=True)
    operations.live_test(
        config,
        paths,
        skip_image=args.skip_image,
    )


def parser() -> argparse.ArgumentParser:
    root = GNUArgumentParser(
        prog="nixloom",
        usage="%(prog)s [OPTION]... COMMAND [ARG]...",
        description="NixLoom — one local-AI control surface",
        epilog="""Examples:
  nixloom start                 Start the stack and load the configured model
  nixloom status                Show services, model state and URLs
  nixloom logs dsh -f            Follow the DSH journal

Run 'nixloom COMMAND --help' for details about a command.""",
    )
    root.add_argument(
        "-c", "--config", metavar="FILE", help="use FILE instead of the default config"
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.set_defaults(handler=lambda _: root.print_help())
    commands = root.add_subparsers(
        dest="command", required=False, title="Commands", metavar="COMMAND"
    )

    for name, help_text in (
        ("start", "Start services and load the configured LLM"),
        ("restart", "Restart services and reload the configured LLM"),
    ):
        item = commands.add_parser(
            name,
            help=help_text,
            description=help_text + ".",
            usage="%(prog)s [OPTION]...",
        )
        item.add_argument("--dry-run", action="store_true", help="show the plan only")
        item.set_defaults(handler=command_start, restart=name == "restart")
    commands.add_parser(
        "stop",
        help="Stop every NixLoom service",
        description="Stop every NixLoom service.",
        usage="%(prog)s",
    ).set_defaults(handler=command_stop)
    status = commands.add_parser(
        "status",
        help="Show services, model state and URLs",
        usage="%(prog)s [OPTION]...",
    )
    status.add_argument(
        "--verbose", action="store_true", help="include service diagnostics"
    )
    status.set_defaults(handler=command_status)
    logs = commands.add_parser(
        "logs",
        help="Read one service or the combined journal",
        description="Read one service or the combined NixLoom journal.",
        usage="%(prog)s [OPTION]... [SERVICE]",
    )
    logs.add_argument(
        "service",
        choices=UNIT_NAMES,
        nargs="?",
        default="all",
        metavar="SERVICE",
        help="runtime, dsh, sillytavern, or all (default: all)",
    )
    logs.add_argument("-f", "--follow", action="store_true", help="follow new entries")
    logs.add_argument(
        "-n",
        "--lines",
        type=int,
        default=100,
        metavar="N",
        help="show the last N entries (default: 100)",
    )
    logs.set_defaults(handler=command_logs)

    config = commands.add_parser(
        "config",
        help="Initialize or validate configuration",
        description="Initialize or validate the NixLoom configuration.",
        usage="%(prog)s ACTION [OPTION]...",
    )
    config.add_argument(
        "config_action",
        choices=("check", "init"),
        nargs="?",
        metavar="ACTION",
        help="check or init",
    )
    config.add_argument(
        "--verbose", action="store_true", help="show generated launch commands"
    )
    config.add_argument("--dry-run", action="store_true", help="show changes only")
    config.set_defaults(handler=command_config, help_parser=config)

    models = commands.add_parser(
        "models",
        help="Verify or download model assets",
        description="Verify or download pinned model assets.",
        usage="%(prog)s ACTION [ASSET]...",
    )
    models.add_argument(
        "models_action",
        choices=("check", "download"),
        nargs="?",
        metavar="ACTION",
        help="check or download",
    )
    models.add_argument("assets", nargs="*", metavar="ASSET", help="asset name")

    def models_handler(args: argparse.Namespace) -> None:
        if args.models_action is None:
            models.print_help()
            return
        paths, loaded = _context(args.config)
        action = (
            operations.check_models
            if args.models_action == "check"
            else operations.download_models
        )
        raise SystemExit(0 if action(loaded, paths, args.assets) else 1)

    models.set_defaults(handler=models_handler)

    frontend = commands.add_parser(
        "dsh",
        help="Invoke the managed DSH frontend",
        usage="%(prog)s [ARG]...",
    )
    frontend.add_argument("arguments", nargs=argparse.REMAINDER, metavar="ARG")
    frontend.set_defaults(handler=command_dsh)

    test = commands.add_parser(
        "test",
        help="Run live end-to-end regression checks",
        description="Run live end-to-end regression checks.",
        usage="%(prog)s [OPTION]...",
    )
    test.add_argument(
        "--skip-image", action="store_true", help="skip image generation and editing"
    )
    test.set_defaults(handler=command_test)

    backup = commands.add_parser(
        "backup",
        help="Back up private frontend state",
        description="Back up private frontend state without model files.",
        usage="%(prog)s [OPTION]... [DESTINATION]",
    )
    backup.add_argument(
        "destination", nargs="?", metavar="DESTINATION", help="backup directory"
    )
    backup.add_argument("--dry-run", action="store_true", help="show the plan only")
    backup.set_defaults(handler=command_backup)

    return root


def service_parser() -> argparse.ArgumentParser:
    """Parser for the deliberately hidden systemd process entry point."""
    service = GNUArgumentParser(
        prog="nixloom __service",
        usage="%(prog)s NAME [OPTION]...",
        description="Internal NixLoom service entry point.",
    )
    service.add_argument(
        "service_name",
        choices=("runtime", "llama", "comfyui", "dsh", "sillytavern"),
        metavar="NAME",
    )
    service.add_argument("--host", default="127.0.0.1", metavar="ADDRESS")
    service.add_argument("--port", metavar="PORT")
    service.add_argument("--dry-run", action="store_true")
    service.add_argument("--prepare-only", action="store_true")
    service.set_defaults(handler=command_service, config=None)
    return service


def main() -> int:
    try:
        argv = sys.argv[1:]
        args = (
            service_parser().parse_args(argv[1:])
            if argv[:1] == ["__service"]
            else parser().parse_args(argv)
        )
        result = args.handler(args)
        return result if isinstance(result, int) else 0
    except (
        ConfigError,
        OSError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
