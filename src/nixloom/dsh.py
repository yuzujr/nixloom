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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import dsh_compat
from .config import Config, ConfigError, RuntimePaths

DEFAULT_VERSION = "0.2.0-rc.2"


@dataclass(frozen=True)
class ManagedPlugin:
    package: str
    version: str
    enabled: bool = True
    settings_id: str | None = None
    settings: dict[str, Any] | None = None
    patch_id: str | None = None
    patch_settings: dict[str, Any] | None = None
    remove_patch_keys: tuple[str, ...] = ()


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
    video = _video_settings(config)
    if video:
        result["NIXLOOM_DSH_VIDEO_WORKFLOWS"] = json.dumps(video)
        result["NIXLOOM_DSH_COMFYUI_URL"] = (
            f"http://127.0.0.1:{config.integer('ports.comfyui', 8188, minimum=1)}"
        )
    else:
        result.pop("NIXLOOM_DSH_VIDEO_WORKFLOWS", None)
        result.pop("NIXLOOM_DSH_COMFYUI_URL", None)
    library_path = os.environ.get("NIXLOOM_DSH_LIBRARY_PATH", "")
    if library_path:
        result["LD_LIBRARY_PATH"] = (
            library_path + ":" + result.get("LD_LIBRARY_PATH", "")
        )
    return result


def _video_settings(config: Config) -> dict[str, Any] | None:
    if not config.boolean("video.enabled", False):
        return None
    from .comfy import video_workflows

    return {
        "workflows": video_workflows(config),
        "frames": config.integer("video.frames", minimum=5),
        "size": config.string("video.size"),
    }


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
            "DSH is not installed for this version; start NixLoom to install it"
        )


def _installed_package_version(target: Path, package: str) -> str | None:
    manifest = target / "node_modules" / package / "package.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError):
        return None


def _allow_install_scripts(target: Path) -> None:
    allowed = (
        "@deepseek-ai/dsh-subprocess-local",
        "@google/genai",
        "koffi",
        "node-pty",
        "protobufjs",
    )
    manifest = target / "package.json"
    if manifest.exists():
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConfigError(f"invalid npm manifest {manifest}: {error}") from error
        if not isinstance(package, dict):
            raise ConfigError(f"npm manifest {manifest} must contain an object")
    else:
        package = {"private": True}
    policies = package.setdefault("allowScripts", {})
    if not isinstance(policies, dict):
        raise ConfigError(f"npm manifest {manifest} has an invalid allowScripts field")
    policies.update(dict.fromkeys(allowed, True))
    manifest.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


def managed_plugins(config: Config) -> list[ManagedPlugin]:
    return [
        ManagedPlugin(
            package="dsh-plugin-mobile-gateway",
            version="0.11.1",
            settings_id="mobile-gateway",
            settings={
                "gatewayMode": "persistent",
                "requireAuth": True,
                "adminLoopbackOnly": True,
                "lanEnabled": False,
            },
        ),
        ManagedPlugin(
            package="dsh-image-gen",
            version="0.8.5",
            enabled=config.boolean("images.enabled"),
            patch_id="image-gen",
            patch_settings=(
                _image_settings(config) if config.boolean("images.enabled") else None
            ),
            remove_patch_keys=(
                "openaiCompatBaseURL",
                "openaiCompatModel",
                "openaiCompatSizes",
            ),
        ),
    ]


def install(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    target = app_directory(config, paths)
    dsh_package = f"@deepseek-ai/dsh@{config.string('dsh.version', DEFAULT_VERSION)}"
    plugins = managed_plugins(config)
    packages = [dsh_package] + [
        f"{plugin.package}@{plugin.version}"
        for plugin in plugins
        if plugin.enabled
    ]
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
                app.mkdir()
                _allow_install_scripts(app)
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
        else:
            missing = [
                f"{plugin.package}@{plugin.version}"
                for plugin in plugins
                if plugin.enabled
                and _installed_package_version(target, plugin.package)
                != plugin.version
            ]
            if missing:
                _allow_install_scripts(target)
                subprocess.run(
                    [
                        "npm",
                        "install",
                        "--prefix",
                        str(target),
                        "--save-exact",
                        "--no-audit",
                        "--no-fund",
                        *missing,
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
    settings = [
        {
            "id": "webserver",
            "config": {
                "host": bind_host(),
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
    settings.extend(
        {"id": plugin.settings_id, "config": plugin.settings}
        for plugin in managed_plugins(config)
        if plugin.enabled and plugin.settings_id and plugin.settings is not None
    )
    return settings


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


def _sync_profile_bundle(
    profile: Path,
    manifest: dict[str, Any],
    bundles: list[str],
    plugin: ManagedPlugin,
    source: Path,
) -> None:
    dependencies = manifest.setdefault("dependencies", {})
    link = profile / "node_modules" / plugin.package
    if plugin.enabled:
        if not source.is_dir():
            raise ConfigError(
                f"DSH plugin {plugin.package} is missing from the NixLoom install"
            )
        if plugin.package not in bundles:
            bundles.append(plugin.package)
        dependencies[plugin.package] = f"file:{source}"
        link.parent.mkdir(exist_ok=True, mode=0o700)
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise ConfigError(f"DSH plugin path is not a NixLoom link: {link}")
        link.symlink_to(source, target_is_directory=True)
        return

    if plugin.package in bundles:
        bundles.remove(plugin.package)
    if dependencies.get(plugin.package) == f"file:{source}":
        del dependencies[plugin.package]
    if link.is_symlink() and link.resolve() == source.resolve():
        link.unlink()


def _sync_plugin_patch_settings(
    rows: list[dict[str, Any]], plugins: list[ManagedPlugin]
) -> None:
    for plugin in plugins:
        if not plugin.patch_id:
            continue
        matching = [row for row in rows if row.get("id") == plugin.patch_id]
        if not plugin.enabled or plugin.patch_settings is None:
            rows[:] = [row for row in rows if row.get("id") != plugin.patch_id]
            continue
        if matching:
            row = matching[0]
            settings = row.setdefault("config", {})
            if not isinstance(settings, dict):
                raise ConfigError(f"DSH {plugin.patch_id} config must be a mapping")
            for key in plugin.remove_patch_keys:
                settings.pop(key, None)
            settings.update(plugin.patch_settings)
        else:
            rows.append({"id": plugin.patch_id, "config": plugin.patch_settings})


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
    plugins = managed_plugins(config)
    for plugin in plugins:
        _sync_profile_bundle(
            profile,
            manifest,
            bundles,
            plugin,
            app_directory(config, paths) / "node_modules" / plugin.package,
        )
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
    _sync_plugin_patch_settings(rows, plugins)
    _write_yaml(patch, rows)
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
        "video": _video_settings(config),
        "managed_plugin_versions": [
            (plugin.package, plugin.version, plugin.enabled)
            for plugin in managed_plugins(config)
        ],
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
        raise ConfigError("DSH profile is stale; start NixLoom to refresh it")


def tailnet_enabled() -> bool:
    return os.environ.get("NIXLOOM_DSH_TAILNET") == "1"


def bind_host() -> str:
    if not tailnet_enabled():
        return "127.0.0.1"
    addresses = tailnet_identity()["addresses"]
    host = next((address for address in addresses if ":" not in address), "")
    if not host:
        raise ConfigError("Tailscale has no IPv4 address for the DSH web server")
    return host


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
    hostname = identity["hostname"].split(".", 1)[0]
    return f"http://{hostname}:{config.integer('ports.dsh', 3080)}/"


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
        local_hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
        hosts = [
            f"{identity['hostname']}:{port}",
            f"{identity['hostname'].split('.', 1)[0]}:{port}",
            *[
                f"{address}:{port}"
                for address in identity["addresses"]
                if ":" not in address
            ],
            *local_hosts,
        ]
        values["NIXLOOM_DSH_TAILNET_HOSTS"] = json.dumps(hosts)
        values["NIXLOOM_DSH_TAILNET_LOCAL_ADDRESSES"] = json.dumps(
            identity["addresses"]
        )
        values["NIXLOOM_DSH_TAILNET_PEERS"] = json.dumps(identity["peers"])
        values["NIXLOOM_DSH_LOCAL_PROXY_TARGET"] = bind_host()
        values["NIXLOOM_DSH_LOCAL_PROXY_PORT"] = str(port)
        args.extend(["--trusted-host", *hosts])
    os.execvpe(args[0], args, values)


def invoke(config: Config, paths: RuntimePaths, arguments: list[str]) -> None:
    require_installed(config, paths)
    os.chdir(workspace(config, paths))
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    args = command(config, paths, *arguments)
    os.execvpe(args[0], args, environment(config, paths))
