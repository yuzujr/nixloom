"""npm-managed DeepSeek Harness frontend inside NixLoom's XDG directories."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import Config, ConfigError, RuntimePaths

DEFAULT_VERSION = "0.2.0-rc.2"
IMAGE_PLUGIN_VERSION = "0.8.5"


def app_directory(config: Config, paths: RuntimePaths) -> Path:
    return paths.cache / "dsh/releases" / config.string("dsh.version", DEFAULT_VERSION)


def workspace(config: Config, paths: RuntimePaths) -> Path:
    value = config.string("dsh.workspace", "")
    return Path(value) if value else paths.data / "dsh/workspace"


def environment(paths: RuntimePaths) -> dict[str, str]:
    result = os.environ.copy()
    result.update(
        DSH_HOME=str(paths.state / ".dsh"),
        NIXLOOM_LOCAL_API_KEY="nixloom-local",
        npm_config_cache=str(paths.cache / "dsh/npm"),
        npm_config_update_notifier="false",
        PNPM_HOME=str(paths.cache / "dsh/pnpm"),
        npm_config_store_dir=str(paths.cache / "dsh/pnpm-store"),
        NARB_NATIVE_CACHE_DIR=str(paths.cache / "dsh/native"),
        DSH_IMAGE_GEN_OPENAI_COMPAT_KEY="nixloom-local",
    )
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


def web_url(config: Config) -> str:
    unit = "nixloom-dsh.service"
    invocation = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "InvocationID", "--value"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not invocation:
        raise ConfigError("Chat is stopped. Run `nixloom start`, then `nixloom open`.")
    journal = subprocess.run(
        [
            "journalctl",
            "--user",
            "-u",
            unit,
            f"_SYSTEMD_INVOCATION_ID={invocation}",
            "-n",
            "100",
            "--output",
            "cat",
            "--no-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    port = config.integer("ports.dsh", 3080, minimum=1)
    matches = re.findall(
        rf"dsh web: (http://127\.0\.0\.1:{port}/\?token=[A-Za-z0-9_-]+)", journal
    )
    if not matches:
        raise ConfigError(
            "Chat is still starting. Try `nixloom open` again shortly; if it keeps failing, run `nixloom logs dsh`."
        )
    return matches[-1]


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
                    env=environment(paths),
                )
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
                env=environment(paths),
            )
        require_installed(config, paths)
        _prepare_packages(config, paths)
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


def _prepare_packages(config: Config, paths: RuntimePaths) -> None:
    modules = app_directory(config, paths) / "node_modules"
    terminal = modules / "@deepseek-ai/dsh-terminal-bash/lib/index.js"
    source = terminal.read_text(encoding="utf-8")
    bash = os.environ.get(
        "NIXLOOM_DSH_BASH", shutil.which("bash") or "/run/current-system/sw/bin/bash"
    )
    replacement = f"const DEFAULT_BASH_SHELL = {json.dumps(bash)};"
    source, count = re.subn(
        r'const DEFAULT_BASH_SHELL = "(?:[^"\\]|\\.)*";',
        lambda _: replacement,
        source,
    )
    if count != 1:
        raise ConfigError("DSH Bash default changed; review the pinned npm version")
    terminal.write_text(source, encoding="utf-8")
    controller = modules / "@deepseek-ai/dsh-api-workspace-controller/lib/index.js"
    source = controller.read_text(encoding="utf-8")
    original = "async function defaultWorkspaceDirectory(documentsDirectory, signal, internals = {}) {"
    replacement = f"return {json.dumps(str(workspace(config, paths)))};"
    marker = "/* NixLoom default workspace */"
    if original in source and marker not in source:
        controller.write_text(
            source.replace(original, original + "\n" + marker + replacement),
            encoding="utf-8",
        )
    elif marker in source:
        controller.write_text(
            re.sub(
                re.escape(marker) + r"return [^\n]+;",
                lambda _: marker + replacement,
                source,
            ),
            encoding="utf-8",
        )
    else:
        raise ConfigError(
            "DSH workspace default changed; review the pinned npm version"
        )


def _image_settings(config: Config) -> dict[str, Any]:
    _, profile = config.image_profile()
    return {
        "provider": "openai-compat",
        "openaiCompatBaseURL": f"http://127.0.0.1:{config.integer('ports.llama', minimum=1)}/upstream/sd/v1",
        "openaiCompatModel": Path(str(profile["model_file"])).stem,
        "openaiCompatSizes": {"1:1": {"1K": str(profile["size"])}},
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
    _prepare_packages(config, paths)
    work = workspace(config, paths)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(
        command(config, paths, "--profile", "web", "--dump-config"),
        check=True,
        cwd=work,
        env=environment(paths),
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
    elif "dsh-image-gen" in bundles:
        bundles.remove("dsh-image-gen")
    _write_json(manifest_path, manifest)
    patch = profile / "cordis.patch.yml"
    sync_settings(config, patch)
    if config.boolean("images.enabled"):
        rows = yaml.safe_load(patch.read_text(encoding="utf-8"))
        row = next((row for row in rows if row.get("id") == "image-gen"), None)
        if row is None:
            rows.append({"id": "image-gen", "config": _image_settings(config)})
        else:
            row.setdefault("config", {}).update(_image_settings(config))
        _write_yaml(patch, rows)
    else:
        rows = yaml.safe_load(patch.read_text(encoding="utf-8"))
        _write_yaml(patch, [row for row in rows if row.get("id") != "image-gen"])


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
        "--host",
        "127.0.0.1",
        "--port",
        str(config.integer("ports.dsh", 3080, minimum=1)),
    )
    if dry_run:
        print(f"DSH_HOME={paths.state / '.dsh'}")
        print(f"workspace={workspace(config, paths)}")
        print(" ".join(args))
        return
    prepare(config, paths)
    print(
        f"Starting DSH on http://127.0.0.1:{config.integer('ports.dsh', 3080)}",
        file=sys.stderr,
    )
    os.chdir(workspace(config, paths))
    os.execvpe(args[0], args, environment(paths))


def invoke(config: Config, paths: RuntimePaths, arguments: list[str]) -> None:
    require_installed(config, paths)
    os.chdir(workspace(config, paths))
    args = command(config, paths, *arguments)
    os.execvpe(args[0], args, environment(paths))
