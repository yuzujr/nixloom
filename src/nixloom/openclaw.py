"""OpenClaw integration without owning OpenClaw's mutable user state."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, RuntimePaths


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
        _setting("gateway.bind", "lan"),
        _setting("gateway.controlUi.enabled", True),
        _setting("gateway.controlUi.allowedOrigins", ["*"]),
        _setting("gateway.controlUi.dangerouslyDisableDeviceAuth", True),
        _setting("gateway.auth.mode", "token"),
        _setting("models.mode", "merge"),
        _setting("models.providers.nixloom", provider),
        _setting("agents.defaults.model.primary", f"nixloom/{model_id}"),
    ]
    workspace = config.string("openclaw.workspace", "")
    if workspace:
        if not Path(workspace).is_absolute():
            raise ConfigError(f"openclaw.workspace must be an absolute path: {workspace}")
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
    subprocess.run(
        [
            "openclaw",
            "config",
            "set",
            "--batch-json",
            json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
            "--strict-json",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _ensure_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() or path.stat().st_size == 0:
        descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
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


def _configure_yuanbao(config: Config) -> None:
    plugin_path_value = os.environ.get("NIXLOOM_OPENCLAW_PLUGIN_PATH", "")
    plugin_path = Path(plugin_path_value) if plugin_path_value else None
    if not plugin_path or not (plugin_path / "openclaw.plugin.json").is_file():
        raise ConfigError(f"the packaged Yuanbao plugin is unavailable: {plugin_path_value or '<unset>'}")
    app_key = config.string("credentials.yuanbao_app_key")
    app_secret = config.string("credentials.yuanbao_app_secret")
    os.environ["YUANBAO_APP_KEY"] = app_key
    os.environ["YUANBAO_APP_SECRET"] = app_secret

    current = subprocess.run(
        ["openclaw", "config", "get", "plugins.load.paths", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        paths = json.loads(current.stdout) if current.returncode == 0 else []
    except json.JSONDecodeError:
        paths = []
    if not isinstance(paths, list):
        paths = []
    suffix = "/share/nixloom/openclaw-plugin-yuanbao"
    paths = [item for item in paths if isinstance(item, str) and not item.endswith(suffix)]
    paths.append(str(plugin_path))
    _set_batch([_setting("plugins.load.paths", sorted(set(paths)))])
    _set_batch(
        [
            _setting("plugins.entries.openclaw-plugin-yuanbao.enabled", True),
            _setting("channels.yuanbao.enabled", True),
            _setting("channels.yuanbao.appKey", app_key),
            _setting("channels.yuanbao.appSecret", app_secret),
        ]
    )


def run(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    state = paths.state / ".openclaw"
    config_path = state / "openclaw.json"
    settings = managed_settings(config)
    if dry_run:
        print(f"OPENCLAW_STATE_DIR={state}")
        print(f"OPENCLAW_CONFIG_PATH={config_path}")
        print("OPENCLAW_GATEWAY_TOKEN=<generated>")
        print("MANAGED_CONFIG=" + json.dumps(settings, ensure_ascii=False))
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
    _set_batch(settings)
    if config.boolean("openclaw.yuanbao"):
        _configure_yuanbao(config)
    port = config.integer("ports.openclaw", minimum=1)
    print(f"Starting OpenClaw on http://0.0.0.0:{port}", file=sys.stderr)
    os.execvp("openclaw", ["openclaw", "gateway"])
