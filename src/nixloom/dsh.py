"""npm-managed DeepSeek Harness frontend inside NixLoom's XDG directories."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from . import dsh_compat
from .config import Config, ConfigError, RuntimePaths

DEFAULT_VERSION = "0.2.0-rc.2"
IMAGE_PLUGIN_VERSION = "0.8.5"


def app_directory(config: Config, paths: RuntimePaths) -> Path:
    return paths.cache / "dsh/releases" / config.string("dsh.version", DEFAULT_VERSION)


def workspace(config: Config, paths: RuntimePaths) -> Path:
    value = config.string("dsh.workspace", "")
    return Path(value) if value else paths.data / "dsh/workspace"


def environment(config: Config, paths: RuntimePaths) -> dict[str, str]:
    result = os.environ.copy()
    result.update(
        DSH_HOME=str(paths.state / ".dsh"),
        NIXLOOM_LOCAL_API_KEY="nixloom-local",
        npm_config_cache=str(paths.cache / "dsh/npm"),
        npm_config_update_notifier="false",
        PNPM_HOME=str(paths.cache / "dsh/pnpm"),
        npm_config_store_dir=str(paths.cache / "dsh/pnpm-store"),
        NARB_NATIVE_CACHE_DIR=str(paths.cache / "dsh/native"),
        TAVILY_API_KEY=config.string("credentials.tavily_api_key", ""),
        NIXLOOM_DSH_WORKSPACE=str(workspace(config, paths)),
        NIXLOOM_DSH_BASH=os.environ.get("NIXLOOM_DSH_BASH")
        or shutil.which("bash")
        or "/run/current-system/sw/bin/bash",
    )
    if config.boolean("images.enabled"):
        result["NIXLOOM_DSH_IMAGE_WORKFLOWS"] = json.dumps(
            {
                "generate": config.string("images.generate"),
                "edit": config.string("images.edit"),
            }
        )
    else:
        result.pop("NIXLOOM_DSH_IMAGE_WORKFLOWS", None)
    library_path = os.environ.get("NIXLOOM_DSH_LIBRARY_PATH", "")
    if library_path:
        result["LD_LIBRARY_PATH"] = (
            library_path + ":" + result.get("LD_LIBRARY_PATH", "")
        )
    return result


def command(config: Config, paths: RuntimePaths, *arguments: str) -> list[str]:
    return [
        "node",
        "--expose-internals",
        "--require",
        str(Path(__file__).with_name("dsh_node.cjs")),
        str(app_directory(config, paths) / "node_modules/@deepseek-ai/dsh/lib/bin.js"),
        *arguments,
    ]


def require_installed(config: Config, paths: RuntimePaths) -> None:
    entry = app_directory(config, paths) / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    if not entry.is_file():
        raise ConfigError(
            "DSH is not installed for this version; rerun Home Manager activation"
        )


def install(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    target = app_directory(config, paths)
    packages = [f"@deepseek-ai/dsh@{config.string('dsh.version', DEFAULT_VERSION)}"]
    if config.boolean("images.enabled"):
        packages.append(f"dsh-image-gen@{IMAGE_PLUGIN_VERSION}")
    if dry_run:
        print(f"npm install {' '.join(packages)} -> {target}")
        print(f"DSH_HOME={paths.state / '.dsh'}")
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (target.parent / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            with tempfile.TemporaryDirectory(
                prefix=".install-", dir=target.parent
            ) as staging:
                app = Path(staging) / "app"
                print(f"Installing {' '.join(packages)}...", flush=True)
                subprocess.run(
                    [
                        "npm",
                        "install",
                        "--prefix",
                        str(app),
                        "--save-exact",
                        "--no-audit",
                        "--no-fund",
                        *packages,
                    ],
                    check=True,
                    env=environment(config, paths),
                )
                dsh_compat.install(app)
                os.replace(app, target)
        elif (
            config.boolean("images.enabled")
            and not (target / "node_modules/dsh-image-gen").is_dir()
        ):
            subprocess.run(
                [
                    "npm",
                    "install",
                    "--prefix",
                    str(target),
                    "--save-exact",
                    "--no-audit",
                    "--no-fund",
                    packages[-1],
                ],
                check=True,
                env=environment(config, paths),
            )
        require_installed(config, paths)
        dsh_compat.install(target)
        prepare(config, paths)
    print(f"DSH installed: {target}")


def managed_settings(config: Config) -> list[dict[str, Any]]:
    port = config.integer("ports.llama", minimum=1)
    provider = {
        "displayName": "NixLoom",
        "apiKeyEnv": "NIXLOOM_LOCAL_API_KEY",
        "api": "openai-completions",
        "baseURL": f"http://127.0.0.1:{port}/v1",
        "timeoutMs": 900000,
        "compat": {
            "supportsDeveloperRole": False,
            "maxTokensField": "max_tokens",
            "thinkingFormat": "qwen-chat-template",
        },
        "models": [
            {
                "id": config.string("llm.id"),
                "name": config.string("llm.id"),
                "input": ["text", "image"],
                "contextWindow": config.integer("llm.context", minimum=1),
                "maxTokens": config.integer("llm.max_tokens", minimum=1),
                "reasoningEfforts": {"off": None, "high": "high"},
            }
        ],
    }
    return [
        {
            "id": "webserver",
            "config": {
                "host": "0.0.0.0" if tailnet_enabled() else "127.0.0.1",
                "port": config.integer("ports.dsh", 3080, minimum=1),
            },
        },
        {"id": "web", "config": {"searchProvider": "tavily", "fetchProvider": "http"}},
        {"id": "llm-pi-ai", "config": {"providers": {"nixloom": provider}}},
        {
            "id": "agent-default-model",
            "config": {
                "provider": "nixloom",
                "model": config.string("llm.id"),
                "reasoningEffort": "off",
            },
        },
    ]


def _image_settings(config: Config) -> dict[str, Any]:
    from .comfy import workflows

    return {
        "provider": "comfyui",
        "comfyuiBaseURL": f"http://127.0.0.1:{config.integer('ports.comfyui', 8188, minimum=1)}",
        "comfyuiWorkflows": [
            {"name": name, "json": json.dumps(graph), "presetPrompt": ""}
            for name, graph in workflows(config).items()
        ],
        "comfyuiActiveWorkflow": config.string("images.generate"),
        "comfyuiTimeoutMs": 900000,
        "saveToWorkspace": True,
    }


def sync_settings(config: Config, target: Path) -> None:
    rows = yaml.safe_load(target.read_text(encoding="utf-8")) if target.exists() else []
    if rows is None:
        rows = []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ConfigError(f"{target} must contain a Cordis patch list")
    for managed in managed_settings(config):
        row = next((row for row in rows if row.get("id") == managed["id"]), None)
        if row is None:
            rows.append(managed)
            continue
        current = row.setdefault("config", {})
        if not isinstance(current, dict):
            raise ConfigError(f"DSH {managed['id']} config must be a mapping")
        if managed["id"] == "llm-pi-ai":
            providers = current.setdefault("providers", {})
            if not isinstance(providers, dict):
                raise ConfigError("DSH llm-pi-ai providers must be a mapping")
            providers.update(managed["config"]["providers"])
        else:
            current.update(managed["config"])
    _write_yaml(target, rows)


def _write_yaml(target: Path, value: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=target.name + ".", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            yaml.safe_dump(value, output, sort_keys=False, allow_unicode=True)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare(config: Config, paths: RuntimePaths) -> None:
    require_installed(config, paths)
    work = workspace(config, paths)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(
        command(config, paths, "--profile", "web", "--dump-config"),
        check=True,
        cwd=work,
        env=environment(config, paths),
        stdout=subprocess.DEVNULL,
    )
    profile = paths.state / ".dsh/profiles/web"
    manifest_path = profile / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundles = manifest["dsh"]["profile"]["bundles"]
    if config.boolean("images.enabled"):
        plugin = app_directory(config, paths) / "node_modules/dsh-image-gen"
        if not plugin.is_dir():
            raise ConfigError(
                "DSH image plugin is missing; rerun Home Manager activation"
            )
        if "dsh-image-gen" not in bundles:
            bundles.append("dsh-image-gen")
        manifest.setdefault("dependencies", {})["dsh-image-gen"] = f"file:{plugin}"
        modules = profile / "node_modules"
        modules.mkdir(exist_ok=True, mode=0o700)
        link = modules / "dsh-image-gen"
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise ConfigError(f"DSH image plugin path is not a NixLoom link: {link}")
        link.symlink_to(plugin, target_is_directory=True)
    else:
        if "dsh-image-gen" in bundles:
            bundles.remove("dsh-image-gen")
        plugin = app_directory(config, paths) / "node_modules/dsh-image-gen"
        dependencies = manifest.get("dependencies", {})
        if dependencies.get("dsh-image-gen") == f"file:{plugin}":
            del dependencies["dsh-image-gen"]
        link = profile / "node_modules/dsh-image-gen"
        if link.is_symlink() and link.resolve() == plugin.resolve():
            link.unlink()
    _write_json(manifest_path, manifest)
    patch = profile / "cordis.patch.yml"
    sync_settings(config, patch)
    rows = yaml.safe_load(patch.read_text(encoding="utf-8"))
    managed_ids = {"nixloom-tavily", "nixloom-workspace"}
    rows = [row for row in rows if row.get("id") not in managed_ids]
    rows.append(
        {
            "insert": [
                {
                    "id": "nixloom-tavily",
                    "name": str(Path(__file__).with_name("dsh_tavily.mjs")),
                },
                {
                    "id": "nixloom-workspace",
                    "name": str(Path(__file__).with_name("dsh_workspace.mjs")),
                },
            ]
        }
    )
    # Replace only our previous provider insertion; user plugin rows remain untouched.
    for row in rows[:-1]:
        if isinstance(row.get("insert"), list):
            row["insert"] = [
                item for item in row["insert"] if item.get("id") not in managed_ids
            ]
    rows = [row for row in rows if row != {"insert": []}]
    _write_yaml(patch, rows)
    if config.boolean("images.enabled"):
        rows = yaml.safe_load(patch.read_text(encoding="utf-8"))
        row = next((row for row in rows if row.get("id") == "image-gen"), None)
        if row is None:
            rows.append({"id": "image-gen", "config": _image_settings(config)})
        else:
            settings = row.setdefault("config", {})
            for key in (
                "openaiCompatBaseURL",
                "openaiCompatModel",
                "openaiCompatSizes",
            ):
                settings.pop(key, None)
            settings.update(_image_settings(config))
        _write_yaml(patch, rows)
    else:
        rows = yaml.safe_load(patch.read_text(encoding="utf-8"))
        _write_yaml(patch, [row for row in rows if row.get("id") != "image-gen"])
    _write_json(
        profile / ".nixloom-prepared.json",
        {"fingerprint": _profile_fingerprint(config, paths)},
    )


def _profile_fingerprint(config: Config, paths: RuntimePaths) -> str:
    managed = {
        "version": config.string("dsh.version", DEFAULT_VERSION),
        "workspace": str(workspace(config, paths)),
        "settings": managed_settings(config),
        "images": _image_settings(config) if config.boolean("images.enabled") else None,
        "plugins": str(Path(__file__).parent),
    }
    return hashlib.sha256(json.dumps(managed, sort_keys=True).encode()).hexdigest()


def require_prepared(config: Config, paths: RuntimePaths) -> None:
    require_installed(config, paths)
    dsh_compat.verify(app_directory(config, paths))
    stamp = paths.state / ".dsh/profiles/web/.nixloom-prepared.json"
    if not stamp.is_file() or json.loads(stamp.read_text()).get(
        "fingerprint"
    ) != _profile_fingerprint(config, paths):
        raise ConfigError("DSH profile is stale; rerun Home Manager activation")


def tailnet_enabled() -> bool:
    return os.environ.get("NIXLOOM_DSH_TAILNET") == "1"


def tailnet_identity() -> dict[str, Any]:
    result = subprocess.run(
        ["tailscale", "status", "--json"], check=True, text=True, capture_output=True
    )
    status = json.loads(result.stdout)
    node = status.get("Self", {})
    if status.get("BackendState") != "Running" or not node.get("TailscaleIPs"):
        raise ConfigError("Tailscale is not connected; connect it before starting DSH")
    if not node.get("UserID"):
        raise ConfigError(
            "Tailscale node has no owner identity; token-free access is unavailable"
        )
    peers = set(node["TailscaleIPs"])
    for peer in status.get("Peer", {}).values():
        if peer.get("UserID") == node.get("UserID"):
            peers.update(peer.get("TailscaleIPs", []))
    return {
        "addresses": node["TailscaleIPs"],
        "hostname": node["DNSName"].rstrip("."),
        "peers": sorted(peers),
    }


def tailnet_url(config: Config) -> str | None:
    if not tailnet_enabled():
        return None
    try:
        identity = tailnet_identity()
    except (ConfigError, OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    return f"http://{identity['hostname']}:{config.integer('ports.dsh', 3080)}/"


def _write_json(target: Path, value: Any) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=target.name + ".", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    args = command(
        config,
        paths,
        "web",
        "--no-open",
        "--port",
        str(config.integer("ports.dsh", 3080, minimum=1)),
    )
    if dry_run:
        print(f"DSH_HOME={paths.state / '.dsh'}")
        print(f"workspace={workspace(config, paths)}")
        print(" ".join(args))
        return
    require_prepared(config, paths)
    print(
        f"Starting DSH on http://127.0.0.1:{config.integer('ports.dsh', 3080)}",
        file=sys.stderr,
    )
    os.chdir(workspace(config, paths))
    values = environment(config, paths)
    values["NIXLOOM_DSH_LOCAL_ACCESS"] = "1"
    if tailnet_enabled():
        identity = tailnet_identity()
        port = config.integer("ports.dsh", 3080)
        hosts = [
            f"{identity['hostname']}:{port}",
            f"{identity['hostname'].split('.', 1)[0]}:{port}",
            *[
                f"{address}:{port}"
                for address in identity["addresses"]
                if ":" not in address
            ],
        ]
        values["NIXLOOM_DSH_TAILNET_HOSTS"] = json.dumps(hosts)
        values["NIXLOOM_DSH_TAILNET_PEERS"] = json.dumps(identity["peers"])
        args.extend(["--trusted-host", *hosts])
    os.execvpe(args[0], args, values)


def invoke(config: Config, paths: RuntimePaths, arguments: list[str]) -> None:
    require_installed(config, paths)
    os.chdir(workspace(config, paths))
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    args = command(config, paths, *arguments)
    os.execvpe(args[0], args, environment(config, paths))
