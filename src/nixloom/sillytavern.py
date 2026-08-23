"""SillyTavern process and state synchronization."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, RuntimePaths

IMAGE_SETTING_KEYS = frozenset(
    {
        "source",
        "sdcpp_url",
        "model",
        "sampler",
        "scheduler",
        "steps",
        "scale",
        "width",
        "height",
        "negative_prompt",
        "prompt_prefix",
    }
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=path.parent
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=4)
            output.write("\n")
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _generation_settings(config: Config, base_url: str) -> dict[str, Any]:
    sampling = config.get("llm.sampling", required=True)
    assert isinstance(sampling, dict)
    return {
        "temperature": sampling["temperature"],
        "frequency_penalty": sampling["frequency_penalty"],
        "presence_penalty": sampling["presence_penalty"],
        "top_p": sampling["top_p"],
        "top_k": sampling["top_k"],
        "min_p": sampling["min_p"],
        "repetition_penalty": sampling["repeat_penalty"],
        "max_context_unlocked": True,
        "openai_model": config.string("llm.id"),
        "openai_max_context": config.integer("llm.context", minimum=1),
        "openai_max_tokens": config.integer("llm.max_tokens", minimum=1),
        "reverse_proxy": base_url,
    }


def _image_settings(config: Config, image_url: str) -> dict[str, Any]:
    _, profile = config.image_profile()
    try:
        width_text, height_text = str(profile["size"]).lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as error:
        raise ConfigError("image profile size must use WIDTHxHEIGHT") from error
    if width < 64 or height < 64:
        raise ConfigError("image profile dimensions must be at least 64x64")
    prompt_prefix = str(profile["prompt_prefix"]).strip(" ,")
    return {
        "source": "sdcpp",
        "sdcpp_url": image_url,
        "model": Path(str(profile["model_file"])).stem,
        "sampler": profile["sampler"],
        "scheduler": profile["scheduler"],
        "steps": profile["steps"],
        "scale": profile["cfg_scale"],
        "width": width,
        "height": height,
        "negative_prompt": profile["negative_prompt"],
        "prompt_prefix": prompt_prefix,
    }


def sync_settings(config: Config, settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    settings = _load_json(settings_path)
    llama_url = f"http://127.0.0.1:{config.integer('ports.llama', minimum=1)}/v1"
    image_url = (
        f"http://127.0.0.1:{config.integer('ports.llama', minimum=1)}/upstream/sd"
    )
    managed = _generation_settings(config, llama_url)
    preset_name = config.string("sillytavern.preset")
    preset_dir = settings_path.parent / "OpenAI Settings"
    target_path = preset_dir / f"{preset_name}.json"
    if target_path.exists():
        preset = _load_json(target_path)
    else:
        existing = settings.get("oai_settings", {})
        source_name = (
            existing.get("preset_settings_openai", "")
            if isinstance(existing, dict)
            else ""
        )
        source_path = preset_dir / f"{source_name}.json"
        preset = (
            _load_json(source_path)
            if source_name and source_path.exists()
            else dict(existing)
        )
    preset.update(managed)
    _write_json_atomic(target_path, preset)

    oai = settings.setdefault("oai_settings", {})
    if not isinstance(oai, dict):
        raise ConfigError("SillyTavern oai_settings must be an object")
    oai.update(managed)
    oai["preset_settings_openai"] = preset_name

    extension_settings = settings.setdefault("extension_settings", {})
    if not isinstance(extension_settings, dict):
        raise ConfigError("SillyTavern extension_settings must be an object")
    image = extension_settings.setdefault("sd", {})
    if not isinstance(image, dict):
        raise ConfigError("SillyTavern extension_settings.sd must be an object")
    if config.boolean("images.enabled"):
        image.update(_image_settings(config, image_url))
    else:
        for key in IMAGE_SETTING_KEYS:
            image.pop(key, None)

    profiles = (
        extension_settings.get("connectionManager", {}).get("profiles", [])
        if isinstance(extension_settings.get("connectionManager", {}), dict)
        else []
    )
    for profile in profiles if isinstance(profiles, list) else []:
        if not isinstance(profile, dict):
            continue
        if profile.get("api-url") == llama_url or (
            profile.get("api") == "openai" and profile.get("proxy") == "llama"
        ):
            profile.update(
                {
                    "api": "openai",
                    "api-url": llama_url,
                    "model": config.string("llm.id"),
                    "preset": preset_name,
                    "name": preset_name,
                }
            )
            break
    _write_json_atomic(settings_path, settings)
    return True


def environment(config: Config, paths: RuntimePaths) -> dict[str, str]:
    state = paths.state / ".sillytavern"
    values = {
        "XDG_DATA_HOME": str(state / "xdg-data"),
        "XDG_CONFIG_HOME": str(state / "xdg-config"),
        "XDG_CACHE_HOME": str(paths.cache / "sillytavern"),
        "XDG_STATE_HOME": str(state / "xdg-state"),
        # Do not impose network topology policy: this personal deployment is
        # allowed to reach its local backends and any user-configured endpoint.
        "SILLYTAVERN_PRIVATEADDRESSWHITELIST_ENABLED": "false",
    }
    password = config.string("sillytavern.auth_password", "")
    if password:
        values["SILLYTAVERN_BASICAUTHUSER_USERNAME"] = config.string(
            "sillytavern.auth_user"
        )
        values["SILLYTAVERN_BASICAUTHUSER_PASSWORD"] = password
    return values


def command(config: Config) -> list[str]:
    host = config.string("sillytavern.bind")
    listen = host not in {"127.0.0.1", "localhost", "::1"}
    result = [
        "sillytavern",
        "--port",
        str(config.integer("ports.sillytavern", minimum=1)),
        "--browserLaunchEnabled",
        "false",
        "--enableIPv4",
        "true",
        "--enableIPv6",
        "false",
        "--listen",
        str(listen).lower(),
        "--listenAddressIPv4",
        host,
        "--whitelist",
        "false",
    ]
    if config.string("sillytavern.auth_password", ""):
        result.extend(["--basicAuthMode", "true"])
    return result


def run(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    values = environment(config, paths)
    launch = command(config)
    if dry_run:
        for key, value in values.items():
            print(f"{key}=<set>" if "PASSWORD" in key else f"{key}={value}")
        print(" ".join(launch))
        return
    os.environ.update(values)
    for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        Path(values[key]).mkdir(parents=True, exist_ok=True)
    settings_path = (
        Path(values["XDG_DATA_HOME"]) / "SillyTavern/data/default-user/settings.json"
    )
    try:
        synchronized = sync_settings(config, settings_path)
        if synchronized:
            print(
                "SillyTavern local chat and stable-diffusion.cpp profiles synchronized.",
                file=sys.stderr,
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"warning: SillyTavern profile sync failed: {error}", file=sys.stderr)
    host = config.string("sillytavern.bind")
    port = config.integer("ports.sillytavern", minimum=1)
    print(f"Starting SillyTavern on http://{host}:{port}", file=sys.stderr)
    os.execvp(launch[0], launch)
