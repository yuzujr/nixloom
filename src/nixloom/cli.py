"""The NixLoom command-line control surface."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import openclaw, operations, runtime, sillytavern
from .config import Config, ConfigError, RuntimePaths

UNIT_NAMES = {
    "runtime": "nixloom-runtime.service",
    "openclaw": "nixloom-openclaw.service",
    "sillytavern": "nixloom-sillytavern.service",
    "all": "nixloom.target",
}


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


def _probe(url: str, accepted: frozenset[int] = frozenset({200})) -> str:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return "ok" if response.status in accepted else f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return "ok" if error.code in accepted else f"HTTP {error.code}"
    except (OSError, urllib.error.URLError):
        return "down"


def _warm(config: Config) -> None:
    port = config.integer("ports.llama", minimum=1)
    health = f"http://127.0.0.1:{port}/health"
    for _ in range(120):
        if _probe(health) == "ok":
            operations._json_request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {
                    "model": config.string("llm.id"),
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                900,
            )
            return
        time.sleep(1)
    raise ConfigError("runtime proxy did not become healthy within 120 seconds")


def command_start(args: argparse.Namespace) -> None:
    _, config = _context(args.config)
    verb = "restart" if args.restart else "start"
    if args.dry_run:
        print(f"systemctl --user {verb} nixloom.target")
        print(f"warm model {config.string('llm.id')} through ports.llama")
        return
    operations.systemctl(verb, "nixloom.target")
    _warm(config)
    print("NixLoom is ready.")


def command_status(args: argparse.Namespace) -> None:
    _, config = _context(args.config)
    for name in ("runtime", "openclaw", "sillytavern"):
        if name != "runtime" and not _unit_installed(name):
            continue
        result = operations.systemctl(
            "show", UNIT_NAMES[name], "-p", "ActiveState", "-p", "SubState", check=False
        )
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        print(
            f"{name:<12} {values.get('ActiveState', 'unknown'):<10} {values.get('SubState', 'unknown')}"
        )
    llama_port = config.integer("ports.llama", minimum=1)
    print(
        f"runtime      {_probe(f'http://127.0.0.1:{llama_port}/health'):<5} http://127.0.0.1:{llama_port}"
    )
    if _unit_installed("openclaw"):
        port = config.integer("ports.openclaw", minimum=1)
        print(
            f"openclaw     {_probe(f'http://127.0.0.1:{port}/healthz'):<5} http://127.0.0.1:{port}"
        )
    if _unit_installed("sillytavern"):
        port = config.integer("ports.sillytavern", minimum=1)
        state = _probe(f"http://127.0.0.1:{port}/", frozenset({200, 401}))
        print(f"sillytavern  {state:<5} http://127.0.0.1:{port}")


def command_config(args: argparse.Namespace) -> None:
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
    image = (
        runtime.image_command(config, paths)
        if config.boolean("images.enabled")
        else None
    )
    swap, document = runtime.swap_command(config, paths)
    if "openclaw" in config.value:
        openclaw.managed_settings(config)
    if "sillytavern" in config.value:
        sillytavern.command(config)
    if args.verbose:
        print(runtime.render_command(llama))
        if image:
            print(runtime.render_command(image))
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
        paths.cache.mkdir(parents=True, exist_ok=True)
        target = paths.cache / "llama-swap.yaml"
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(document, encoding="utf-8")
        os.replace(temporary, target)
        runtime.execute(command)
    elif args.service_name == "llama":
        command = runtime.llama_command(config, paths, host=args.host, port=args.port)
        if args.dry_run:
            print(runtime.render_command(command))
            return
        runtime.check_assets(command, config, paths)
        runtime.execute(command)
    elif args.service_name == "image":
        command = runtime.image_command(
            config, paths, host=args.host, port=args.port or 7860
        )
        if args.dry_run:
            print(runtime.render_command(command))
            return
        runtime.check_assets(command, config, paths)
        runtime.execute(command)
    elif args.service_name == "openclaw":
        openclaw.run(config, paths, dry_run=args.dry_run)
    elif args.service_name == "sillytavern":
        sillytavern.run(config, paths, dry_run=args.dry_run)


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
        print(f"backup config, OpenClaw and SillyTavern state -> {destination}")
        print(f"temporarily stop running stack: {str(running).lower()}")
        return
    try:
        if running:
            operations.systemctl("stop", "nixloom.target")
        archive = operations.create_backup(config, paths, destination)
    finally:
        if running:
            operations.systemctl("start", "nixloom.target")
    print(f"backup written: {archive}")


def command_test(args: argparse.Namespace) -> None:
    paths, config = _context(args.config)
    operations.live_test(
        config,
        paths,
        skip_image=args.skip_image,
        skip_agent=args.skip_agent,
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="nixloom", description="Modular local-AI runtime control"
    )
    root.add_argument("--config", help="configuration file override")
    commands = root.add_subparsers(dest="command", required=True)

    for name in ("start", "restart"):
        item = commands.add_parser(name)
        item.add_argument("--dry-run", action="store_true")
        item.set_defaults(handler=command_start, restart=name == "restart")
    commands.add_parser("stop").set_defaults(
        handler=lambda _: (
            operations.systemctl("stop", "nixloom.target"),
            print("NixLoom stopped."),
        )
    )
    commands.add_parser("status").set_defaults(handler=command_status)
    logs = commands.add_parser("logs")
    logs.add_argument("service", choices=UNIT_NAMES, nargs="?", default="all")
    logs.add_argument("--follow", "-f", action="store_true")
    logs.set_defaults(
        handler=lambda args: os.execvp(
            "journalctl",
            [
                "journalctl",
                "--user",
                "-u",
                UNIT_NAMES[args.service],
                "--lines",
                "100",
                *(["--follow"] if args.follow else ["--no-pager"]),
            ],
        )
    )

    config = commands.add_parser("config")
    config.add_argument("config_action", choices=("check", "init"))
    config.add_argument("--verbose", action="store_true")
    config.add_argument("--dry-run", action="store_true")
    config.set_defaults(handler=command_config)

    models = commands.add_parser("models")
    models.add_argument("models_action", choices=("check", "download"))
    models.add_argument("assets", nargs="*")

    def models_handler(args: argparse.Namespace) -> None:
        paths, loaded = _context(args.config)
        action = (
            operations.check_models
            if args.models_action == "check"
            else operations.download_models
        )
        raise SystemExit(0 if action(loaded, paths, args.assets) else 1)

    models.set_defaults(handler=models_handler)

    test = commands.add_parser("test")
    test.add_argument("--skip-image", action="store_true")
    test.add_argument("--skip-agent", action="store_true")
    test.set_defaults(handler=command_test)

    backup = commands.add_parser("backup")
    backup.add_argument("destination", nargs="?")
    backup.add_argument("--dry-run", action="store_true")
    backup.set_defaults(handler=command_backup)

    service = commands.add_parser("service", help=argparse.SUPPRESS)
    service.add_argument(
        "service_name", choices=("runtime", "llama", "image", "openclaw", "sillytavern")
    )
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port")
    service.add_argument("--dry-run", action="store_true")
    service.set_defaults(handler=command_service)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        args.handler(args)
        return 0
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
