"""OpenClaw integration without owning OpenClaw's mutable user state."""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, RuntimePaths

IMAGE_SETTINGS = (
    "models.providers.openai",
    "agents.defaults.imageGenerationModel",
    "browser.ssrfPolicy.dangerouslyAllowPrivateNetwork",
)
YUANBAO_SETTINGS = (
    "plugins.entries.openclaw-plugin-yuanbao.enabled",
    "channels.yuanbao.enabled",
    "channels.yuanbao.appKey",
    "channels.yuanbao.appSecret",
)
TAVILY_SETTINGS = (
    "plugins.entries.tavily.enabled",
    "tools.web.search.provider",
)


def _setting(path: str, value: Any) -> dict[str, Any]:
    return {"path": path, "value": value}


def managed_settings(config: Config) -> list[dict[str, Any]]:
    model_id = config.string("llm.id")
    llama_port = config.integer("ports.llama", minimum=1)
    provider = {
        "baseUrl": f"http://127.0.0.1:{llama_port}/v1",
        "apiKey": "nixloom-local",
        "api": "openai-completions",
        "timeoutSeconds": 900,
        "models": [
            {
                "id": model_id,
                "name": model_id,
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": config.integer("llm.context", minimum=1),
                "maxTokens": config.integer("llm.max_tokens", minimum=1),
                "compat": {
                    "thinkingFormat": "qwen-chat-template",
                    "toolSchemaProfile": "llamacpp",
                },
            }
        ],
    }
    settings = [
        _setting("gateway.mode", "local"),
        _setting("gateway.port", config.integer("ports.openclaw", minimum=1)),
        _setting("gateway.bind", "loopback"),
        _setting("gateway.tailscale.mode", "serve"),
        _setting("gateway.controlUi.enabled", True),
        _setting("gateway.controlUi.allowedOrigins", ["*"]),
        _setting("gateway.controlUi.dangerouslyDisableDeviceAuth", True),
        _setting("gateway.auth.mode", "token"),
        _setting("models.mode", "merge"),
        _setting("models.providers.nixloom", provider),
        _setting("agents.defaults.model.primary", f"nixloom/{model_id}"),
        _setting("tools.loopDetection.enabled", True),
        _setting("tools.web.fetch.ssrfPolicy.allowRfc2544BenchmarkRange", True),
    ]
    workspace = config.string("openclaw.workspace", "")
    if workspace:
        if not Path(workspace).is_absolute():
            raise ConfigError(
                f"openclaw.workspace must be an absolute path: {workspace}"
            )
        settings.append(_setting("agents.defaults.workspace", workspace))

    if config.boolean("images.enabled"):
        profile_name, profile = config.image_profile()
        image_model = Path(str(profile["model_file"])).stem or profile_name
        image_provider = {
            "baseUrl": f"http://127.0.0.1:{llama_port}/upstream/sd/v1",
            "apiKey": "nixloom-local",
            "api": "openai-completions",
            "models": [
                {
                    "id": image_model,
                    "name": image_model,
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 4096,
                    "maxTokens": 1024,
                }
            ],
        }
        settings.extend(
            [
                _setting("models.providers.openai", image_provider),
                _setting(
                    "agents.defaults.imageGenerationModel",
                    {"primary": f"openai/{image_model}", "timeoutMs": 600000},
                ),
                _setting("browser.ssrfPolicy.dangerouslyAllowPrivateNetwork", True),
            ]
        )
    return settings


def _set_batch(settings: list[dict[str, Any]]) -> None:
    config = _read_config()
    for setting in settings:
        _set_config_value(config, setting["path"], setting["value"])
    _write_config(config)


def _config_path() -> Path:
    value = os.environ.get("OPENCLAW_CONFIG_PATH", "")
    if not value:
        raise ConfigError("OPENCLAW_CONFIG_PATH is not set")
    return Path(value)


def _read_config() -> dict[str, Any]:
    path = _config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read OpenClaw config {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"OpenClaw config must contain a JSON object: {path}")
    return value


def _write_config(value: dict[str, Any]) -> None:
    path = _config_path()
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _set_config_value(config: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target = config
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def _get_config_value(config: dict[str, Any], path: str) -> Any:
    value: Any = config
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _unset_config_value(config: dict[str, Any], path: str) -> bool:
    parts = path.split(".")
    parents: list[tuple[dict[str, Any], str]] = []
    target = config
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            return False
        parents.append((target, part))
        target = child
    if parts[-1] not in target:
        return False
    del target[parts[-1]]
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break
    return True


def unmanaged_settings(config: Config) -> list[str]:
    """Return formerly managed paths that must converge to absence."""
    paths: list[str] = []
    if not config.string("openclaw.workspace", ""):
        paths.append("agents.defaults.workspace")
    if not config.boolean("images.enabled"):
        paths.extend(IMAGE_SETTINGS)
    return paths


def _unset_paths(paths: list[str] | tuple[str, ...]) -> None:
    config = _read_config()
    changed = False
    for path in paths:
        changed = _unset_config_value(config, path) or changed
    if changed:
        _write_config(config)


def _ensure_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() or path.stat().st_size == 0:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".", dir=path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(secrets.token_hex(32))
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                pass
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    return path.read_text(encoding="utf-8").strip()


def _sync_plugin_path(plugin_path: Path | None, suffix: str) -> None:
    paths = _get_config_value(_read_config(), "plugins.load.paths")
    if not isinstance(paths, list):
        paths = []
    paths = [
        item for item in paths if isinstance(item, str) and not item.endswith(suffix)
    ]
    if plugin_path:
        paths.append(str(plugin_path))
    paths = sorted(set(paths))
    if paths:
        _set_batch([_setting("plugins.load.paths", paths)])
    else:
        _unset_paths(["plugins.load.paths"])


def _configure_yuanbao(config: Config) -> None:
    plugin_path_value = os.environ.get("NIXLOOM_OPENCLAW_PLUGIN_PATH", "")
    plugin_path = Path(plugin_path_value) if plugin_path_value else None
    if not plugin_path or not (plugin_path / "openclaw.plugin.json").is_file():
        raise ConfigError(
            f"the packaged Yuanbao plugin is unavailable: {plugin_path_value or '<unset>'}"
        )
    app_key = config.string("credentials.yuanbao_app_key")
    app_secret = config.string("credentials.yuanbao_app_secret")
    os.environ["YUANBAO_APP_KEY"] = app_key
    os.environ["YUANBAO_APP_SECRET"] = app_secret

    _sync_plugin_path(plugin_path, "/share/nixloom/openclaw-plugin-yuanbao")
    _set_batch(
        [
            _setting("plugins.entries.openclaw-plugin-yuanbao.enabled", True),
            _setting("channels.yuanbao.enabled", True),
            _setting("channels.yuanbao.appKey", app_key),
            _setting("channels.yuanbao.appSecret", app_secret),
        ]
    )


def _disable_yuanbao() -> None:
    _sync_plugin_path(None, "/share/nixloom/openclaw-plugin-yuanbao")
    _unset_paths(YUANBAO_SETTINGS)


def _configure_tavily(config: Config) -> None:
    plugin_path_value = os.environ.get("NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH", "")
    plugin_path = Path(plugin_path_value) if plugin_path_value else None
    api_key = config.string("credentials.tavily_api_key", "")
    if not api_key:
        _sync_plugin_path(None, "/share/nixloom/openclaw-plugin-tavily")
        _unset_paths(TAVILY_SETTINGS)
        return
    if not plugin_path or not (plugin_path / "openclaw.plugin.json").is_file():
        raise ConfigError(
            f"the packaged Tavily plugin is unavailable: {plugin_path_value or '<unset>'}"
        )
    _sync_plugin_path(plugin_path, "/share/nixloom/openclaw-plugin-tavily")
    _set_batch(
        [
            _setting("plugins.entries.tavily.enabled", True),
            _setting("tools.web.search.provider", "tavily"),
        ]
    )


def reconcile_settings(config: Config) -> None:
    """Apply the desired settings and remove managed values no longer desired."""
    _set_batch(managed_settings(config))
    _unset_paths(unmanaged_settings(config))
    if config.boolean("openclaw.yuanbao"):
        _configure_yuanbao(config)
    else:
        _disable_yuanbao()
    _configure_tavily(config)


def run(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    state = paths.state / ".openclaw"
    config_path = state / "openclaw.json"
    settings = managed_settings(config)
    if dry_run:
        print(f"OPENCLAW_STATE_DIR={state}")
        print(f"OPENCLAW_CONFIG_PATH={config_path}")
        print("OPENCLAW_GATEWAY_TOKEN=<generated>")
        print("MANAGED_CONFIG=" + json.dumps(settings, ensure_ascii=False))
        print(
            "UNSET_CONFIG=" + json.dumps(unmanaged_settings(config), ensure_ascii=False)
        )
        if not config.boolean("openclaw.yuanbao"):
            print("DISABLE_YUANBAO=true")
        print("openclaw gateway")
        return

    os.umask(0o077)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.touch(mode=0o600, exist_ok=True)
    if config_path.stat().st_size == 0:
        config_path.write_text("{}\n", encoding="utf-8")
        config_path.chmod(0o600)
    os.environ["OPENCLAW_STATE_DIR"] = str(state)
    os.environ["OPENCLAW_CONFIG_PATH"] = str(config_path)
    os.environ["OPENCLAW_GATEWAY_TOKEN"] = _ensure_token(
        paths.state / ".run/openclaw-gateway-token"
    )
    tavily = config.string("credentials.tavily_api_key", "")
    if tavily:
        os.environ["TAVILY_API_KEY"] = tavily
    reconcile_settings(config)
    # Packaged plugins live in the immutable Nix store and may contain
    # hard-linked files. OpenClaw only permits that layout in its Nix mode;
    # without this flag plugin discovery silently skips Yuanbao and Tavily.
    os.environ["OPENCLAW_NIX_MODE"] = "1"
    port = config.integer("ports.openclaw", minimum=1)
    print(f"Starting OpenClaw on http://127.0.0.1:{port}", file=sys.stderr)
    os.execvp("openclaw", ["openclaw", "gateway"])
